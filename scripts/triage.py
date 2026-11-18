#!/usr/bin/env python3
"""Inbox planner.

See docs/quickstart.md.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import stat
import argparse
import ipaddress
from urllib.parse import urlsplit
from contextlib import contextmanager
from email.utils import getaddresses, quote
import fcntl
import unicodedata
import math
import tempfile
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import time
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERBS = frozenset({'trash', 'archive', 'move', 'spam', 'unsubscribe', 'report', 'draft'})
PLAN_VERBS = (VERBS - {'draft', 'report'}) | {'create-folder', 'create-draft', 'send-draft'}
DRAFT_LIMIT = 10
BRAKE_FLOOR = 10
BODY_LIMIT = 2000
STALE_SECONDS = 45 * 60
TAKEOVER_MARGIN = 10 * 60
REFRESH_MARGIN = 5 * 60
LOG_TAIL_BYTES = 256 * 1024
LOG_NAMES = ('calls.jsonl', 'history.log', 'decisions.jsonl')


def read_text(path, encoding='utf-8'):
    try:
        return Path(path).read_text(encoding=encoding)
    except UnicodeError as error:
        raise ValueError(str(path) + ': expected valid ' + encoding + ' text') from error


def state_timestamp(value, source):
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError) as error:
        raise ValueError(str(source) + ': expected an ISO 8601 UTC timestamp') from error


def parse_json(text, source):
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as error:
        raise ValueError(str(source) + ': expected valid JSON') from error


def decision_batch(line):
    try:
        value = json.loads(line)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def parse_toml(text, source):
    try:
        return tomllib.loads(text)
    except ValueError as error:
        raise ValueError(str(source) + ': expected valid TOML') from error


def trim_logs(store):
    # Only at acquisition or completed fetch, before this run can append records.
    import execute
    data = read_numbers(store)
    if 'answers' not in data and ((store / 'decisions.jsonl').exists() or (store / 'numbers.json').exists()):
        data['answers'] = answer_records(read_answers(store, data))
        execute.atomic(store / 'numbers.json', data)
    for name in LOG_NAMES:
        path = store / name
        if not path.exists():
            continue
        raw = state_bytes(path)
        if len(raw) > LOG_TAIL_BYTES:
            lines = raw.splitlines(keepends=True)
            tail = []
            size = 0
            for line in reversed(lines):
                if size + len(line) > LOG_TAIL_BYTES:
                    break
                tail.append(line)
                size += len(line)
            execute.atomic_text(path, b''.join(reversed(tail)).decode('utf-8'))


def private_store(path=None, *, output=False):
    if path is None and os.environ.get('PIGEON_HOME') == '':
        raise ValueError('PIGEON_HOME: expected an existing folder, not an empty value')
    store = Path(path or os.environ.get('PIGEON_HOME', Path.home() / '.config/pigeon')).expanduser().resolve()
    if not output and path is None and not store.is_dir():
        raise ValueError('Private store ' + str(store) + ': expected an existing folder. Create it, or set PIGEON_HOME to an existing folder.')
    parent = store.parent if output else store
    while not parent.exists():
        parent = parent.parent
    git_env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    probe = subprocess.run(['git', '-C', str(parent), 'rev-parse', '--show-toplevel'], capture_output=True, text=True, env=git_env)
    if probe.returncode == 0:
        ignored = subprocess.run(['git', '-C', probe.stdout.strip(), 'check-ignore', '-q', str(store) + ('' if output else os.sep)], env=git_env)
        if ignored.returncode:
            raise ValueError('Private store ' + str(store) + ': expected a git-ignored folder inside a work tree; ignore it or set PIGEON_HOME to an existing folder outside the work tree')
    elif 'not a git repository' not in probe.stderr:
        raise ValueError('Private store ' + str(store) + ': git work-tree check failed')
    if not output:
        store.mkdir(parents=True, exist_ok=True, mode=0o700)
    return store


@contextmanager
def lock_gate(store):
    # This inode is never removed: every acquire, check and release uses it.
    with (store / '.lock-gate').open('a', encoding='utf-8') as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        yield gate


def read_lock(store, *, now=None):
    clock = time.time() if now is None else now
    try:
        path = store / 'run.lock'
        if not path.stat().st_mode & 0o444:
            raise ValueError('Run lock is unreadable and held')
        held = parse_json(read_text(path), path)
        known_keys(held, {'token', 'created', 'started', 'backend', 'id', 'etag', 'drive_created', 'lost'}, 'run.lock')
        if 'lost' in held and held['lost'] is not True:
            raise ValueError('invalid loss flag')
        if 'backend' in held:
            if lock_setting({'lock': held['backend']})['kind'] != 'onedrive':
                raise ValueError('invalid backend')
            lock_item(dict(status='ok', id=held['id'], etag=held['etag'], name=LOCK_NAME,
                           created=datetime.fromtimestamp(held['drive_created'], timezone.utc).isoformat()), clock)
        if (not isinstance(held['token'], str) or not held['token']
                or type(held['created']) not in (int, float)
                or not math.isfinite(held['created']) or held['created'] > clock):
            raise ValueError('Run lock is invalid and held')
        return held
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(str(store / 'run.lock') + ': expected token string and finite non-future created timestamp; lock held') from error


def acquire(store):
    with lock_gate(store):
        try:
            held = read_lock(store)
        except FileNotFoundError:
            held = None
        if held is not None:
            if time.time() - held['created'] <= STALE_SECONDS + TAKEOVER_MARGIN:
                raise ValueError(str(store / 'run.lock') + ': Run lock is busy until ' + time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(held['created'] + STALE_SECONDS + TAKEOVER_MARGIN)) + '; another run holds it. Wait for release or expiry.')
            (store / 'run.lock').unlink()
        trim_logs(store)
        token = secrets.token_hex(24)
        with (store / 'run.lock').open('x', encoding='utf-8') as out:
            json.dump({'token': token, 'created': time.time()}, out)
        (store / 'execution-report.md').unlink(missing_ok=True)
        return token


def require_lock(store, token, *, now=None):
    try:
        held = read_lock(store, now=now)
    except FileNotFoundError as error:
        raise ValueError(str(store / 'run.lock') + ': expected a lock held by this token, but no lock exists; stop writes') from error
    if held.get('lost'):
        raise ValueError('run.lock: drive lock ownership was lost; stop writes')
    if store_lock_setting(store) != held.get('backend', {'kind': 'local'}):
        raise ValueError('run.lock: configured backend differs; stop writes')
    if not secrets.compare_digest(held['token'], token) or (time.time() if now is None else now) - held['created'] > STALE_SECONDS:
        raise ValueError(str(store / 'run.lock') + ': token expected to match the holder and be unexpired; stop writes')


def refresh(store, token, *, now=None):
    import execute
    clock = time.time() if now is None else now.timestamp()
    with lock_gate(store):
        require_lock(store, token, now=clock)
        held = read_lock(store, now=clock)
        execute.atomic(store / 'run.lock', held | {'created': clock, 'started': held.get('started', held['created'])})
    return token


SIMULATION_DIR = '.dry-run'


def clean_simulation(store):
    import shutil
    folder = store / SIMULATION_DIR
    if folder.is_symlink() or folder.is_file():
        folder.unlink()
    elif folder.exists():
        shutil.rmtree(folder)


def release(store, token):
    if store_lock_setting(store)['kind'] == 'onedrive':
        raise ValueError('run.lock: release requires the conditional drive exchange')
    with lock_gate(store):
        try:
            held = read_lock(store)
        except FileNotFoundError:
            return 'already released'
        if not secrets.compare_digest(held['token'], token):
            raise ValueError(str(store / 'run.lock') + ': token expected to match the holder; only the holder may release the lock')
        (store / 'run.lock').unlink()
        try:
            clean_simulation(store)
        except OSError:
            return 'released; dry-run cleanup failed'
        return 'released'


LOCK_NAME = 'pigeon-run-lock'
LOCK_OBSERVATION_LIMIT = 4096
LOCK_FUTURE_TOLERANCE = 5


def lock_setting(config):
    value = config.get('lock', {'kind': 'local'})
    known_keys(value, {'kind', 'account', 'folder'}, 'config.lock')
    kind = value.get('kind')
    if kind == 'local':
        if set(value) != {'kind'}:
            raise ValueError('config.lock: local expects only kind')
    elif kind != 'onedrive':
        raise ValueError('config.lock.kind: that drive cannot hold the lock; expected local or onedrive')
    else:
        if set(value) != {'kind', 'account', 'folder'}:
            raise ValueError('config.lock: expected kind, account and folder')
        for key in ('account', 'folder'):
            if not lock_text(value[key], 512):
                raise ValueError('config.lock.' + key + ': expected 1 to 512 ASCII letters, digits, underscore, hyphen, dot, colon or exclamation mark')
    return value


def store_lock_setting(store):
    path = store / 'config.toml'
    if not path.exists():
        raise ValueError('config.toml: copy config/config.example.toml into the working folder and configure it before taking the lock')
    config = parse_toml(load_inputs([path])[0], path)
    check_version(config, path)
    return lock_setting(config)


def lock_text(value, limit):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_!.:-]{1,' + str(limit) + '}', value) is not None


def lock_item(data, clock):
    if set(data) != {'status', 'id', 'etag', 'created', 'name'}:
        raise ValueError('lock observation: expected status, id, etag, created and name; not held')
    if not lock_text(data['id'], 512) or not isinstance(data['etag'], str) or not re.fullmatch(r'[A-Za-z0-9_!.,:{}"-]{1,512}', data['etag']):
        raise ValueError('lock observation: invalid id or etag; not held')
    if data['name'] != LOCK_NAME:
        raise ValueError('lock observation: wrong name; not held')
    value = data['created']
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r'[0-9T:.+Z-]{20,40}', value):
        raise ValueError('lock observation: invalid creation time; not held')
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.utcoffset() != timedelta(0):
            raise ValueError('UTC required')
        created = date.timestamp()
    except (ValueError, OverflowError):
        raise ValueError('lock observation: invalid UTC creation time; not held') from None
    if created > clock + LOCK_FUTURE_TOLERANCE:
        raise ValueError('lock observation: future creation time; not held')
    return dict(id=data['id'], etag=data['etag'], drive_created=created)


def lock_observation(path):
    try:
        if isinstance(path, dict):
            if len(json.dumps(path).encode()) > LOCK_OBSERVATION_LIMIT or not isinstance(path.get('status'), str):
                raise ValueError()
            return path
        path = Path(path)
        if not path.is_file() or not path.stat().st_mode & 0o444:
            raise ValueError()
        with path.open('rb') as stream:
            raw = stream.read(LOCK_OBSERVATION_LIMIT + 1)
        if len(raw) > LOCK_OBSERVATION_LIMIT:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('status'), str):
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError(str(path) + ': lock observation: expected bounded valid JSON; not held') from None


def drive_lock(store, operation, token=None, observation=None, *, now=None):
    import execute
    clock = time.time() if now is None else now.timestamp()
    setting = store_lock_setting(store)
    if setting['kind'] != 'onedrive':
        raise ValueError('config.lock: expected onedrive')
    path = store / 'lock-exchange.json'
    with lock_gate(store):
        def issue(phase, *, created_item=None, **extra):
            call = dict(account=setting['account'], folder=setting['folder'], name=LOCK_NAME,
                        operation='create' if phase in ('create', 'recreate') else 'delete' if phase in ('takeover', 'release', 'refresh') else 'read')
            if call['operation'] == 'create':
                call['conflict'] = 'fail'
            call.update(extra)
            record = dict(operation=operation, phase=phase, token=token, setting=setting, call=call,
                          started=pending['started'] if observation is not None else clock)
            if created_item is not None:
                record['created_item'] = created_item
            execute.atomic(path, record)
            return {'call': call}
        if observation is None:
            if path.exists():
                abandoned = parse_json(read_text(path), path)
                if operation != 'acquire' or clock - abandoned['started'] <= STALE_SECONDS:
                    raise ValueError('lock exchange: pending or uncertain call; never retry in this run')
                trim_logs(store)
                with (store / 'history.log').open('a', encoding='utf-8') as out:
                    out.write(datetime.fromtimestamp(clock, timezone.utc).isoformat() + ' abandoned: earlier lock exchange\n')
                path.unlink()
            if operation == 'acquire':
                if (store / 'run.lock').exists():
                    held = read_lock(store, now=clock)
                    if held.get('backend') != setting or clock - held['created'] <= STALE_SECONDS:
                        raise ValueError('run.lock: existing local record; do not bypass it')
                    (store / 'run.lock').unlink()
                token = secrets.token_hex(24)
                return issue('create')
            require_lock(store, token, now=clock)
            held = read_lock(store, now=clock)
            if operation in ('release', 'refresh'):
                if operation == 'refresh':
                    execute.atomic(store / 'run.lock', held | {'lost': True})
                return issue(operation, if_match=held['etag'])
            return issue('verify')
        if not path.exists():
            raise ValueError('lock exchange: no issued call; not held')
        pending = parse_json(read_text(path), path)
        if (pending['operation'] != operation or pending['setting'] != setting
                or (operation != 'acquire' and pending['token'] != token)):
            raise ValueError('lock exchange: observation does not match issued call; not held')
        # Consume before parsing. Uncertain writes can never be issued again.
        execute.atomic(path, pending | {'phase': 'uncertain'})
        token = pending['token']
        phase = pending['phase']
        try:
            data = lock_observation(observation)
            status = data['status']
            if phase in ('create', 'recreate') and status == 'created':
                item = lock_item(data, clock)
                if clock - item['drive_created'] > STALE_SECONDS:
                    raise ValueError('lock observation: created item already expired; not held')
                return issue('confirm', created_item={k: item[k] for k in ('id', 'drive_created')})
            if phase == 'confirm' and status == 'ok':
                item = lock_item(data, clock)
                if any(item[k] != pending['created_item'][k] for k in ('id', 'drive_created')):
                    raise ValueError('lock observation: created item changed; not held')
                if clock - item['drive_created'] > STALE_SECONDS:
                    raise ValueError('lock observation: created item already expired; not held')
                if operation != 'refresh':
                    trim_logs(store)
                execute.atomic(store / 'run.lock', dict(token=token, created=min(clock, item['drive_created']), started=read_lock(store).get('started', read_lock(store)['created']) if operation == 'refresh' else min(clock, item['drive_created']), backend=setting, **item))
                path.unlink()
                if operation != 'refresh':
                    (store / 'execution-report.md').unlink(missing_ok=True)
                return token
            if phase == 'create' and data == {'status': 'conflict'}:
                return issue('read')
            if phase == 'read' and status == 'ok':
                item = lock_item(data, clock)
                if clock - item['drive_created'] <= STALE_SECONDS + TAKEOVER_MARGIN:
                    # A definite busy read leaves no pending call, so it must not block later runs.
                    path.unlink()
                    raise ValueError('Run lock is busy; not held')
                return issue('takeover', if_match=item['etag'])
            if phase in ('takeover', 'release', 'refresh') and data == {'status': 'deleted'}:
                if pending['call'].get('operation') != 'delete' or not pending['call'].get('if_match'):
                    raise ValueError('lock exchange: conditional delete was not issued; not held')
                if phase in ('takeover', 'refresh'):
                    return issue('recreate')
                require_lock(store, token, now=clock)
                if pending['call']['if_match'] != read_lock(store, now=clock)['etag']:
                    raise ValueError('lock exchange: changed holder; lost lock')
                (store / 'run.lock').unlink()
                path.unlink()
                try:
                    clean_simulation(store)
                except OSError:
                    return 'released; dry-run cleanup failed'
                return 'released'
            if phase == 'verify' and status == 'ok':
                item = lock_item(data, clock)
                require_lock(store, token, now=clock)
                held = read_lock(store, now=clock)
                if item != {k: held[k] for k in item}:
                    raise ValueError('lock observation: changed holder; lost lock')
                path.unlink()
                return 'verified'
            raise ValueError('lock observation: failed, uncertain or lost lock; not held')
        except (ValueError, OSError, KeyError, TypeError) as error:
            if phase == 'confirm':
                raise ValueError('lock confirmation: not held; no delete issued; leave item to expire after 45 minutes: ' + str(error)) from None
            if operation != 'acquire' and (store / 'run.lock').exists():
                held = parse_json(read_text(store / 'run.lock'), store / 'run.lock')
                execute.atomic(store / 'run.lock', held | {'lost': True})
            raise


# Fixed download set and upload order. Only an open question carries its plan and listing.
STATE_FILES = ('numbers.json', 'unsubscribe-secret', 'unsubscribe-failures.json',
               'decisions.jsonl', 'calls.jsonl', 'history.log', 'run-message.md',
               'rules.md', 'never-touch.toml', 'tool-paths.json', 'plan.json', 'inbox.json', 'state.json')
STATE_LIMIT = 4 * 1024 * 1024
LEGACY_RULES = 'rules.toml is no longer read; the rules belong in rules.md as a table'


def state_setting(config):
    value = config.get('state', {'kind': 'local'})
    if not isinstance(value, dict):
        raise ValueError('config.state: expected table')
    kind = value.get('kind')
    if kind == 'local':
        if set(value) != {'kind'}:
            raise ValueError('config.state: local expects only kind')
    elif kind not in ('onedrive', 'gdrive'):
        raise ValueError('config.state.kind: expected local, onedrive or gdrive')
    else:
        if set(value) != {'kind', 'account', 'folder'}:
            raise ValueError('config.state: expected kind, account and folder')
        for key in ('account', 'folder'):
            if not lock_text(value[key], 512):
                raise ValueError('config.state.' + key + ': expected 1 to 512 ASCII handle characters')
        if lock_setting(config)['kind'] != 'onedrive':
            raise ValueError('config.state: drive state requires the OneDrive lock')
    return value


def state_config(store):
    config = parse_toml(load_inputs([store / 'config.toml'])[0], store / 'config.toml')
    check_version(config, store / 'config.toml')
    return config


def require_fetched(store, token):
    setting = state_setting(state_config(store))
    if setting['kind'] != 'local':
        path = store / 'state-exchange.json'
        data = parse_json(read_text(path), path) if path.exists() else {}
        known_keys(data, set(data) if isinstance(data, dict) else set(), str(path))
        if data.get('token') != token or data.get('phase') != 'ready' or data.get('setting') != setting:
            raise ValueError('state-exchange.json: planning requires a completed fetch in the ready phase for this token and configuration; release lock')


def state_bytes(path):
    # lstat prevents following links and opening pipes/devices.
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or not mode & 0o444:
        raise ValueError(str(path) + ': state file: expected readable regular file, no symlink')
    with path.open('rb') as stream:
        raw = stream.read(STATE_LIMIT + 1)
    if len(raw) > STATE_LIMIT:
        raise ValueError(str(path) + ': state file: exceeds 4194304 bytes')
    try:
        raw.decode('utf-8')
    except UnicodeError as error:
        raise ValueError(str(path) + ': expected UTF-8 text') from error
    return raw


def validate_state_file(name, raw, config, folder):
    text = raw.decode('utf-8')
    if name in ('plan.json', 'inbox.json'):
        if text:
            data = parse_json(text, folder / name)
            known_keys(data, set(data) if isinstance(data, dict) else set(), name)
            if name == 'inbox.json':
                validate_inputs(data, config, {})
    elif name == 'rules.md':
        read_rules(text, folder / name, config['accounts'])
    elif name == 'never-touch.toml':
        try:
            entries = parse_holds(text)
        except ValueError as error:
            raise ValueError(str(folder / name) + ': ' + str(error)) from None
        if any(a not in config['accounts'] for r in entries for a in r.get('accounts', [])):
            raise ValueError(str(folder / name) + ': unconfigured account')
    elif name == 'tool-paths.json':
        data = parse_json(text, folder / name)
        validate_tool_paths(data, config, drop=True)
        (folder / name).write_text(json.dumps(data), encoding='utf-8')
    elif name == 'state.json':
        validate_inputs({'accounts': {}, 'messages': []}, config, parse_json(text, folder / name))
    elif name == 'numbers.json':
        read_numbers(folder)
    elif name == 'unsubscribe-secret':
        if not re.fullmatch('[0-9a-f]{64}', text):
            raise ValueError('unsubscribe-secret: expected 64 lowercase hex characters')
    elif name == 'unsubscribe-failures.json':
        validate_unsubscribe_memory(parse_json(text, folder / name))
    elif name in ('decisions.jsonl', 'calls.jsonl'):
        for line in text.splitlines():
            try:
                data = parse_json(line, folder / name)
            except ValueError:
                if name == 'decisions.jsonl':
                    continue
                raise
            if name == 'decisions.jsonl':
                if not isinstance(data, dict) or set(data) - {'_run_counter', '_next_number'} != {'run', 'time', 'parts'} or not lock_text(data['run'], 512):
                    raise ValueError(name + ': invalid batch')
                stamp = state_timestamp(data['time'], folder / name)
                if stamp.utcoffset() != timedelta(0) or not isinstance(data['parts'], list):
                    raise ValueError(name + ': expected UTC time and parts')
                for part in data['parts']:
                    if (not isinstance(part, dict) or set(part) != {'number', 'account', 'id', 'verb', 'target', 'outcome'}
                            or type(part['number']) is not int or part['number'] < 1
                            or part['account'] not in config['accounts'] or not isinstance(part['id'], str)
                            or part['verb'] not in {'trash', 'archive', 'spam', 'move', 'leave', 'yes', 'no', 'send-draft', 'forward'}
                            or part['outcome'] not in {'executed', 'failed', 'skipped', 'unexecuted', 'issued_unknown', 'unconfirmed', 'answered'}):
                        raise ValueError(name + ': invalid decision')
                    if part['target'] is not None:
                        folder_path(part['target'])
            else:
                if (not isinstance(data, dict) or set(data) - {'tool'} != {'sequence', 'time', 'run', 'account', 'id', 'verb', 'outcome'}
                        or type(data['sequence']) is not int or data['sequence'] < 1
                        or type(data['time']) not in (int, float) or not math.isfinite(data['time'])
                        or any(not isinstance(data[k], str) for k in ('run', 'account', 'id', 'verb', 'outcome'))):
                    raise ValueError(name + ': invalid call log')
    elif name == 'history.log':
        for line in text.splitlines():
            if not re.fullmatch(r'[0-9T:.+Z-]{20,40} (?:skipped: no new mail|abandoned: earlier lock exchange|approval (?:asked: [0-9a-f]{64}(?: [0-9a-f]{64})?|(?:skipped|expired|replaced|stale): [0-9a-f]{64})|approval approved: [0-9a-f]{64} [0-9a-f]{64})', line):
                raise ValueError('history.log: invalid skip line')
            if state_timestamp(line.split(' ')[0], folder / name).utcoffset() != timedelta(0):
                raise ValueError('history.log: expected UTC')
    elif name == 'run-message.md':
        if (text and not text.strip()) or any(unicodedata.category(c) in {'Cc', 'Cs'} and c not in '\n\t' for c in text):
            raise ValueError('run-message.md: expected nonempty UTF-8 message without controls')


def validate_unsubscribe_memory(memory):
    known_keys(memory, {'version', 'senders'}, 'unsubscribe-failures.json')
    if 'version' in memory and (type(memory['version']) is not int or memory['version'] != 2):
        raise ValueError('unsubscribe-failures.json.version: expected 2')
    values = memory.get('senders')
    if (not isinstance(values, list) or any(not isinstance(x, str) or not re.fullmatch('[0-9a-f]{64}', x) for x in values)
            or len(set(values)) != len(values)):
        raise ValueError('unsubscribe-failures.json.senders: expected unique lowercase digests')


def drive_state(store, operation, token, observation=None, *, lock_observation=None, now=None, batch=False, verified=False):
    import execute
    config = state_config(store)
    setting = state_setting(config)
    if setting['kind'] == 'local':
        return {'done': True}
    path = store / 'state-exchange.json'
    incoming = store / 'state-incoming'
    outgoing = store / 'state-outgoing'
    if operation == 'send' and batch and observation is not None:
        observed = state_observation(observation)
        data = parse_json(read_text(path), path)
        if data.get('token') != token or data.get('phase') != 'send' or data.get('index') != 0:
            raise ValueError('state send: no pending batch; never retry')
        if set(observed) != set(data['names']):
            observed = {name: {'status': 'failed'} for name in data['names']}
        result = {'done': True}
        for name in data['names']:
            result = drive_state(store, 'send', token, observed[name] if isinstance(observed[name], dict) else {'status': 'failed'}, now=now)
        return result
    if operation == 'send' and observation is None and not verified:
        require_fetched(store, token)
        result = drive_lock(store, 'state-send', token, lock_observation, now=now)
        if isinstance(result, dict):
            return result
    with lock_gate(store):
        require_lock(store, token, now=now.timestamp() if now else None)
        data = parse_json(read_text(path), path) if path.exists() else {}
        known_keys(data, set(data) if isinstance(data, dict) else set(), str(path))
        def save():
            execute.atomic(path, data)
        def issue(name, verb):
            return dict(operation=verb, kind=setting['kind'], account=setting['account'],
                        folder=setting['folder'], name=name,
                        path=str((incoming if verb == 'download' else outgoing) / name))
        if operation == 'fetch' and observation is None:
            if data.get('token') == token:
                raise ValueError('state fetch: already issued; never retry; release lock')
            incoming.mkdir(mode=0o700, exist_ok=True)
            if incoming.is_symlink():
                raise ValueError('state incoming: expected real folder; release lock')
            leftovers = list(incoming.iterdir())
            if any(not stat.S_ISREG(p.lstat().st_mode) for p in leftovers):
                raise ValueError('state incoming: expected only regular files directly in folder; release lock')
            for p in leftovers:
                p.unlink()
            data = dict(token=token, setting=setting, phase='fetch')
            save()
            return {'calls': [issue(n, 'download') for n in STATE_FILES]}
        if data.get('token') != token or data.get('setting') != setting:
            raise ValueError('state exchange: expected matching token and settings; release lock')
        if operation == 'fetch':
            if data['phase'] != 'fetch':
                raise ValueError('state fetch: no pending fetch; release lock')
            data['phase'] = 'refused'
            save()
            observed = state_observation(observation)
            if set(observed) != set(STATE_FILES):
                raise ValueError('state fetch: expected exactly the fixed names; release lock')
            if any(v not in ('fetched', 'absent') for v in observed.values()):
                raise ValueError('state fetch: failed or uncertain download; release lock')
            if all(v == 'absent' for v in observed.values()) and any((store / n).exists() for n in ('state.json', 'numbers.json', *LOG_NAMES)):
                raise ValueError('drive store empty but local records exist')
            expected = {n for n in STATE_FILES if observed[n] == 'fetched'}
            if {p.name for p in incoming.iterdir()} != expected:
                raise ValueError('state incoming: unexpected or missing file; release lock')
            contents = {n: state_bytes(incoming / n) for n in STATE_FILES if n in expected}
            for n, raw in contents.items():
                validate_state_file(n, raw, config, incoming)
            # Validate the entire batch before changing any destination; roll back I/O failures.
            original = {n: state_bytes(store / n) if (store / n).exists() else None for n in STATE_FILES}
            # Acquisition can record recovery before remote history is fetched.
            # Merge only those fixed-format local evidence lines, then trim at this boundary.
            fetched_hashes = {n: hashlib.sha256(contents[n]).hexdigest() if n in contents else None for n in STATE_FILES}
            prior_history = (original['history.log'] or b'').decode('utf-8').splitlines()
            history = contents.get('history.log', b'').decode('utf-8').splitlines()
            for line in prior_history:
                if (re.fullmatch(r'[0-9T:.+Z-]{20,40} abandoned: earlier lock exchange', line)
                        and line not in history):
                    history.append(line)
            if history:
                contents['history.log'] = ('\n'.join(history) + '\n').encode('utf-8')
            try:
                for n in STATE_FILES:
                    if n in contents and not (n in ('run-message.md', 'plan.json', 'inbox.json') and contents[n] == b''):
                        execute.atomic_text(store / n, contents[n].decode('utf-8'))
                    elif n not in ('rules.md', 'never-touch.toml'):
                        (store / n).unlink(missing_ok=True)
            except OSError:
                for n, raw in original.items():
                    if raw is None:
                        (store / n).unlink(missing_ok=True)
                    else:
                        execute.atomic_text(store / n, raw.decode('utf-8'))
                raise
            for n in expected:
                (incoming / n).unlink()
            trim_logs(store)
            data.update(phase='ready', hashes=fetched_hashes)
            save()
            result = {'done': True}
            if observed.get('rules.md') == 'absent' and (store / 'rules.toml').exists():
                result['rules'] = LEGACY_RULES
            return result
        if observation is None:
            contents = {n: state_bytes(store / n) for n in STATE_FILES if (store / n).exists()}
            plan_path = store / 'plan.json'
            plan = parse_json(contents['plan.json'].decode('utf-8'), plan_path) if contents.get('plan.json') else {}
            events = [line.split(' approval ', 1)[1] for line in contents.get('history.log', b'').decode('utf-8').splitlines() if ' approval ' in line]
            waiting = (plan.get('approval_state') == 'awaiting approval' and not plan.get('dry_run')
                       and events and events[-1] == question_event(plan)
                       and not question_closed(store, plan))
            if waiting and 'inbox.json' not in contents:
                raise ValueError('approval: awaiting plan requires inbox.json for drive state')
            if not waiting:
                for name in ('plan.json', 'inbox.json'):
                    contents.pop(name, None)
                    if data['hashes'][name] is not None:
                        contents[name] = b''
            for n, raw in contents.items():
                validate_state_file(n, raw, config, store)
            if 'run-message.md' not in contents and data['hashes']['run-message.md'] is not None:
                contents['run-message.md'] = b''
            outgoing.mkdir(mode=0o700, exist_ok=True)
            if outgoing.is_symlink():
                raise ValueError('state outgoing: expected real folder')
            names = [n for n in STATE_FILES if n in contents and hashlib.sha256(contents[n]).hexdigest() != data['hashes'][n]]
            for n in names:
                execute.atomic_text(outgoing / n, contents[n].decode('utf-8'))
            data.update(phase='send', names=names, sizes={n: len(contents[n]) for n in names},
                        sent_hashes={n: hashlib.sha256(contents[n]).hexdigest() for n in names}, failed=[], index=0)
        else:
            if data['phase'] != 'send' or data['index'] >= len(data['names']):
                raise ValueError('state send: no pending upload; never retry')
            name = data['names'][data['index']]
            # Consume before parsing: a bad observation is an uncertain upload, never replayed.
            data['index'] += 1
            data['failed'].append(name)
            save()
            try:
                observed = state_observation(observation)
                if (set(observed) == {'status', 'size'} and observed['status'] == 'done'
                        and type(observed['size']) is int and observed['size'] == data['sizes'][name]):
                    data['failed'].remove(name)
                    data['hashes'][name] = data['sent_hashes'][name]
            except (ValueError, OSError, TypeError):
                pass
        if data['index'] < len(data['names']):
            save()
            if batch:
                return {'calls': [issue(name, 'upload') for name in data['names']]}
            return {'call': issue(data['names'][data['index']], 'upload')}
        data['phase'] = 'sent' if data['failed'] else 'ready'
        save()
        result = {'done': True, 'failed': data['failed']}
        if data['failed']:
            warning = 'Saved files on the drive are out of date: ' + ', '.join(data['failed'])
            message = store / 'run-message.md'
            execute.atomic_text(message, warning + '\n' + (read_text(message) if message.exists() else ''))
            result['warning'] = warning
        return result


def state_observation(path):
    # Bounded JSON observations; all fields are checked against issued operations.
    raw = json.dumps(path).encode() if isinstance(path, dict) else state_bytes(Path(path))
    if len(raw) > LOCK_OBSERVATION_LIMIT:
        raise ValueError(str(path) + ': state observation: exceeds 4096 bytes')
    value = parse_json(raw, path)
    if not isinstance(value, dict):
        raise ValueError(str(path) + ': state observation: expected object')
    return value


def batch_lock(store, observation=None, *, now=None):
    if observation is None:
        result = drive_lock(store, 'acquire', now=now)
    else:
        path = store / 'lock-exchange.json'
        pending = parse_json(read_text(path), path)
        if pending['phase'] in ('create', 'recreate'):
            observed = state_observation(observation)
            if set(observed) != {'create', 'read'}:
                raise ValueError('lock batch: expected create and read observations')
            result = drive_lock(store, 'acquire', observation=observed['create'], now=now)
            if result.get('call', {}).get('operation') != 'read':
                raise ValueError('lock batch: expected confirmation read')
            result = drive_lock(store, 'acquire', observation=observed['read'], now=now)
        else:
            result = drive_lock(store, 'acquire', observation=observation, now=now)
    if isinstance(result, dict) and result['call']['operation'] == 'create':
        call = result['call']
        read = {k: v for k, v in call.items() if k != 'conflict'} | {'operation': 'read'}
        return {'calls': [call, read]}
    return result


def finish_run(store, token, plan=None, observation=None, *, now=None):
    import execute
    if plan is not None:
        require_plan_token(execute.read(plan), token)
    if plan is not None and execute.read(plan).get('dry_run'):
        raise ValueError('dry-run plan: finish-run is refused')
    if store_lock_setting(store)['kind'] == 'local':
        if observation is not None:
            raise ValueError('finish-run: local finish takes no observation')
        if plan is not None:
            commit_state(store, token, plan, now=now)
            message = render_message(store, token, plan, None, now=now)
        else:
            message = ''
        release(store, token)
        return dict(done=True, message=message)
    path = store / 'finish-exchange.json'
    if observation is None:
        require_fetched(store, token)
        if path.exists() and execute.read(path).get('token') == token:
            raise ValueError('finish-run: already issued; never retry')
        return drive_lock(store, 'finish', token, now=now)
    observed = state_observation(observation)
    if path.exists() and execute.read(path).get('token') == token:
        pending = execute.read(path)
        if pending['phase'] != 'issued':
            raise ValueError('finish-run: observations already consumed')
        execute.atomic(path, pending | {'phase': 'consumed'})
        if set(observed) != {'uploads', 'release'}:
            raise ValueError('finish-run: expected uploads and release observations')
        sent = drive_state(store, 'send', token, observed['uploads'], now=now, batch=True) if pending['uploads'] else {'done': True}
        drive_lock(store, 'release', token, observed['release'], now=now)
        return sent | {'message': (sent.get('warning', '') + '\n' if sent.get('warning') else '') + pending['message']}
    drive_lock(store, 'finish', token, observed, now=now)
    message = ''
    if plan is not None:
        commit_state(store, token, plan, now=now, lock_verified=True)
        message = render_message(store, token, plan, None, now=now)
    uploads = drive_state(store, 'send', token, now=now, batch=True, verified=True).get('calls', [])
    released = drive_lock(store, 'release', token, now=now)
    execute.atomic(path, dict(token=token, phase='issued', uploads=bool(uploads), message=message))
    return {'calls': uploads + [released['call']]}


RULE_KINDS = {'never-touch.toml': {'accounts': {'type': 'array',
                                   'allowed_values': None,
                                   'limits': {'min_items': 1},
                                   'actions': ['protect'],
                                   'meaning': 'Configured account handles to match.'},
                      'sender': {'type': 'string',
                                 'allowed_values': None,
                                 'limits': {'min_length': 1},
                                 'actions': ['protect'],
                                 'meaning': 'Exact sender address, ignoring case.'},
                      'domain': {'type': 'string',
                                 'allowed_values': None,
                                 'limits': {'min_length': 1},
                                 'actions': ['protect'],
                                 'meaning': 'Sender domain and its subdomains.'},
                      'subject': {'type': 'string',
                                  'allowed_values': None,
                                  'limits': {'min_length': 1},
                                  'actions': ['protect'],
                                  'meaning': 'Case insensitive Python regex on subject; must not match empty text.'},
                      'has_attachment': {'type': 'boolean',
                                         'allowed_values': None,
                                         'limits': {},
                                         'actions': ['protect'],
                                         'meaning': 'Match the attachment flag; absent means false. This condition needs sender, domain, subject or judgment beside it.'},
                      'judgment': {'type': 'string',
                                   'allowed_values': None,
                                   'limits': {'min_length': 1, 'max_length': 500},
                                   'actions': ['protect'],
                                   'meaning': 'Never-touch category included in the matched message question.'}}}


def validate_hold_fields(entry):
    table = RULE_KINDS['never-touch.toml']
    known_keys(entry, set(table), 'never-touch')
    types = {'string': str, 'boolean': bool, 'integer': int, 'array': list}
    for key, value in entry.items():
        field = table[key]
        if type(value) is not types[field['type']]:
            raise ValueError(key + ': expected ' + ('a non-empty string' if key in ('sender', 'domain', 'subject', 'body') else field['type']))
        if field['allowed_values'] is not None and value not in field['allowed_values']:
            raise ValueError(key + ': expected one of ' + ', '.join(field['allowed_values']))
        for bound, limit in field['limits'].items():
            if ((bound == 'min' and value < limit) or (bound == 'max' and value > limit)
                    or (bound in ('min_length', 'min_items') and len(value) < limit)
                    or (bound in ('max_length', 'max_items') and len(value) > limit)):
                raise ValueError(key + ': expected ' + bound + ' ' + str(limit))


def validate_holds(holds):
    for i, hold in enumerate(holds, 1):
        try:
            if not isinstance(hold, dict):
                raise ValueError('Never-touch entry must be a table')
            validate_hold_fields(hold)
            if 'judgment' in hold and not hold['judgment'].strip():
                raise ValueError('judgment: expected a non-blank category')
            validate_predicate(hold, category='judgment' in hold)
        except ValueError as error:
            raise ValueError(('never-touch entry ' + str(i)) + ': ' + str(error)) from error


def validate_predicate(rule, *, category=False):
    if 'accounts' in rule and (not isinstance(rule['accounts'], list) or not rule['accounts']
                               or any(not isinstance(a, str) for a in rule['accounts'])):
        raise ValueError('accounts: Accounts must be a non-empty list of configured account handles')
    if 'has_attachment' in rule and not isinstance(rule['has_attachment'], bool):
        raise ValueError('has_attachment must be boolean')
    for key in ('sender', 'domain', 'subject', 'body'):
        if key in rule and (not isinstance(rule[key], str) or not rule[key].strip()):
            raise ValueError(key + ': expected a non-empty string')
    if not category and not any(k in rule for k in ('sender', 'domain', 'subject', 'body')):
        raise ValueError('Never-touch requires sender, domain, subject or a category')
    for key in ('subject',):
        try:
            if key in rule and re.search(rule[key], '', re.IGNORECASE):
                raise ValueError(key + ': Predicate matches empty text; expected a regex that does not match empty text')
        except re.error as error:
            raise ValueError(key + ': Invalid ' + key + ' regex; expected a valid Python regular expression') from error


def parse_holds(text):
    data = parse_toml(text, 'never-touch.toml')
    known_keys(data, {'rules', 'allow_empty'}, 'rule file')
    if 'allow_empty' in data and type(data['allow_empty']) is not bool:
        raise ValueError('allow_empty: expected boolean')
    entries = data.get('rules', [])
    if not isinstance(entries, list) or (not entries and data.get('allow_empty') is not True):
        raise ValueError('rules: expected an array of tables' if not isinstance(entries, list) else 'Never-touch requires entries or allow_empty = true')
    validate_holds(entries)
    return entries


def table_cells(line):
    """Split pipe rows while preserving code spans and escaped pipes."""
    line = line.strip()
    if '|' not in line:
        return None
    cells, cell, fence = [], '', ''
    i = int(line.startswith('|'))
    tick_runs = list(re.finditer(r'`+', line))
    last_tick = {len(run[0]): run.start() for run in tick_runs}
    while i < len(line):
        c = line[i]
        if c == '\\' and i + 1 < len(line) and line[i + 1] == '|':
            cell += '|'
            i += 2
            continue
        if c == '`':
            end = i + 1
            while end < len(line) and line[end] == '`':
                end += 1
            ticks = line[i:end]
            if not fence and last_tick[len(ticks)] > i:
                fence = ticks
            elif fence == ticks:
                fence = ''
            cell += ticks
            i = end
            continue
        if c == '|' and not fence:
            cells.append(cell.strip())
            cell = ''
        else:
            cell += c
        i += 1
    if cell or not line.endswith('|'):
        cells.append(cell.strip())
    return cells


RULE_HEADER = ('id', 'patrón', 'acción', 'alcance', 'fuente')
ACTION_WORDS = {'trash': 'trash', 'archive': 'archive', 'spam': 'spam',
                'move': 'move', 'mover': 'move', 'unsubscribe': 'unsubscribe', 'baja': 'unsubscribe'}
DOMAIN_TOKEN = r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]*[a-z0-9])?'
CODE_SPAN = r'(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)'
FILE_REFERENCE = re.compile(r"\]\(<?(?!\w+://)([^<>\n)]+)>?\)|`([^`]+\.(?:md|txt|csv))`|(?<![\w/@.])([\w./-]+\.(?:md|txt|csv))\b", re.I)


def row_patterns(cell):
    text, warning, exception = cell.partition('⚠')
    spans = list(re.finditer(CODE_SPAN, text))
    parts = spans or re.finditer(r'[^\s,]+', text)
    tokens, subject, note = [], None, ''
    for part in parts:
        word = (part[2] if spans else part[0]).strip('_"\'()')
        word = word.removeprefix('**').removesuffix('**').strip('_"\'()')
        mold = re.match(r'(?:subject|asunto):', word, re.I)
        if mold:
            remainder = word[mold.end():] + ('' if spans else text[part.end():])
            subject = remainder.strip().strip('_"\'()')
            if not spans:
                break
            continue
        token = word.casefold().rstrip(';:')
        domain = token.removeprefix('*.').removeprefix('@')
        if token in {'y', 'o', 'and', 'or', '/', ',', '+'}:
            continue
        if re.fullmatch(r'[^\s@<>;,`*]+@' + DOMAIN_TOKEN, token) and '.' in token.rsplit('@', 1)[1]:
            tokens.append(('sender', token))
        elif re.fullmatch(DOMAIN_TOKEN, domain) and ('.' in domain or token.startswith('*.')):
            tokens.append(('domain', token.removeprefix('@')))
        elif re.fullmatch(r'[^`]+\.(?:md|txt|csv)', word, re.I):
            continue
        elif not spans:
            note = text[part.start():].strip()
            break
        else:
            raise ValueError('unrecognised backticked token')
    if spans:
        note = re.sub(CODE_SPAN, '', text).strip(' ,;-—')
    return tokens, subject, ' '.join(filter(None, [note, warning + exception])).strip()


def row_steps(cell):
    steps, notes = [], []
    start = 0
    parts = []
    for match in re.finditer(CODE_SPAN + r'|(\+)', cell):
        if match[3]:
            parts.append(cell[start:match.start()])
            start = match.end()
    parts.append(cell[start:])
    for part in parts:
        part = part.strip()
        leading = re.match(r'[\w-]+', part)
        verb = ACTION_WORDS.get(leading[0].rstrip('-').casefold()) if leading else None
        if not verb:
            steps.append({'agent': part})
            continue
        rest = part[leading.end():].lstrip(',;:.').strip()
        step = {'verb': verb}
        if verb == 'move':
            folder = re.match(CODE_SPAN + r'|(\S+)', rest)
            if not folder:
                raise ValueError('move without a folder')
            try:
                step['target'] = folder_path(folder[2] or folder[3], template=True)
            except ValueError:
                raise ValueError('invalid move folder') from None
            rest = rest[folder.end():]
        if verb == 'unsubscribe':
            rest = re.sub(r'^(?:https?://\S+|https\b)', '', rest, flags=re.I)
        notes.append(rest.strip(' ,;'))
        steps.append(step)
    kit = [step['verb'] for step in steps if 'verb' in step]
    if sum(verb in REMOVALS for verb in kit) > 1:
        raise ValueError('more than one filing step')
    if any(verb in REMOVALS for verb in kit[:-1]):
        raise ValueError('kit step after filing')
    if kit.count('unsubscribe') > 1:
        raise ValueError('more than one unsubscribe step')
    return steps, ' '.join(filter(None, notes))


def read_rules(text, path, accounts, diagnostics=None):
    rules, seen, skipped, warnings, tables = [], set(), [], [], []
    directory = Path(path).resolve().parent
    state, header_line, fence = '', 0, ''
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        marker = re.match(r'(`{3,}|~{3,})', stripped)
        if marker:
            if fence:
                if marker[1][0] == fence:
                    fence = ''
            else:
                fence = marker[1][0]
            state = ''
            continue
        if fence:
            continue
        cells = table_cells(line)
        if state == 'header':
            if cells is not None and len(cells) == 5 and all(re.fullmatch(r':?-{3,}:?', c) for c in cells):
                state = 'rows'
            else:
                warnings.append(f'rules: header at line {header_line} has no separator')
                state = ''
            continue
        if cells is None:
            state = ''
            continue
        header = tuple(re.match(r'\w*', unicodedata.normalize('NFC', c).casefold())[0] for c in cells)
        if header == RULE_HEADER:
            state, header_line = 'header', number
            tables.append(number)
            continue
        if state != 'rows' or not stripped.startswith('|'):
            state = ''
            continue
        ident, pattern, action, scope, source = (cells + [''] * 5)[:5]
        name = ident or f'line {number}'
        while name in seen:
            name += f' (line {number})'
        seen.add(name)
        try:
            if len(cells) > 5:
                raise ValueError('more than five cells')
            tokens, subject, note = row_patterns(pattern)
            if not tokens and subject is None:
                raise ValueError('no sender, domain or subject')
            if subject is not None and not subject.replace('*', '').strip():
                raise ValueError('subject pattern has no literal text')
            if any(kind == 'domain' and value.startswith('*.') and value.count('.') == 1 for kind, value in tokens):
                raise ValueError('wildcard covers a top-level domain')
            steps, action_note = row_steps(action)
            handles = list(accounts) if scope.casefold() == 'todas' else list(dict.fromkeys(scope.replace(',', ' ').split()))
            known = [handle for handle in handles if handle in accounts]
            if not known:
                raise ValueError('no configured account in scope')
            reference_text = re.sub(r'(?<!\]\()https?://\S+', '', pattern + ' ' + action, flags=re.I)
            refs = (a or b or c for a, b, c in FILE_REFERENCE.findall(reference_text))
            non_files = {value for _, value in tokens} | {step['target'].casefold() for step in steps if 'target' in step}
            files = []
            for ref in dict.fromkeys(refs):
                if ref.casefold() in non_files:
                    continue
                if Path(ref).is_absolute() or '..' in Path(ref).parts:
                    raise ValueError('file reference outside the rules folder')
                resolved = (directory / ref).resolve()
                if not resolved.is_relative_to(directory):
                    raise ValueError('file reference outside the rules folder')
                files.append(dict(path=str(resolved), ref=ref))
        except ValueError as error:
            skipped.append(f'Rule {chat_text(name)} (line {number}) not used: {error}')
            continue
        if len(known) < len(handles):
            warnings.append(f'Rule {chat_text(name)} (line {number}): unknown account ignored: ' +
                            ', '.join(chat_text(handle) for handle in handles if handle not in accounts))
        base = dict(id=name, line=number, accounts=known, steps=steps, source=source,
                    note=' '.join(filter(None, [note, action_note])), files=files)
        if subject is not None:
            base['subject'] = subject
        rules.extend(base | ({kind: value} if kind else {}) for kind, value in (tokens or [(None, None)]))
    if diagnostics is not None:
        diagnostics.extend([f'rules: {len({r["line"] for r in rules})} rows read, {len(skipped)} not used',
                            *[f'rules: table at line {n}' for n in tables], *skipped, *warnings])
    return rules


def subject_matches(pattern, subject):
    parts = pattern.casefold().split('*')
    text = subject.casefold()
    if len(parts) == 1:
        return parts[0] in text
    first, *middle, last = parts
    if not text.startswith(first) or not text.endswith(last):
        return False
    end = len(text) - len(last)
    position = len(first)
    for part in middle:
        found = text.find(part, position, end)
        if found < 0:
            return False
        position = found + len(part)
    return position <= end


def normalize_sender(value):
    try:
        addresses = getaddresses([value])
        if len(addresses) != 1:
            return None
        address = addresses[0][1].casefold().rstrip('.')
        if not re.fullmatch(r"[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+", address):
            return None
        return address
    except (TypeError, ValueError):
        return None


def folder_path(value, *, template=False):
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError('target: expected a folder path of 1 to 255 characters')
    checked = value
    if template:
        checked = checked.replace('{year}', '2026').replace('{month}', '01')
    if (any(c in checked for c in '{}\\∕⁄／⧸＼﹨╱⟋⧵╲⟍') or
            any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in checked) or
            len(checked.split('/')) > 8 or
            any(x.strip() in ('', '.', '..') for x in checked.split('/'))):
        raise ValueError('target: expected at most 8 nonempty, non-dot path segments, no controls or unknown placeholders')
    return value


def matches(message, rule, protection=False):
    if (not protection or 'accounts' in rule) and message['account'] not in rule.get('accounts', []):
        return False
    sender = (normalize_sender(message['sender']) or message['sender']) if protection else message['sender']
    sender = sender.casefold()
    if protection:
        local, domain = sender.rsplit('@', 1)
        sender = local.split('+', 1)[0] + '@' + domain
    if 'sender' in rule and sender != rule['sender'].casefold().rstrip('.'):
        return False
    if 'domain' in rule:
        domain = sender.rsplit('@', 1)[-1]
        wanted = rule['domain'].casefold().rstrip('.')
        if not protection and any(not label for label in domain.split('.')):
            return False
        if protection:
            if domain != wanted and not domain.endswith('.' + wanted):
                return False
        elif wanted.startswith('*.'):
            if not domain.endswith(wanted[1:]):
                return False
        elif domain != wanted:
            return False
    if 'subject' in rule:
        if protection:
            if not re.search(rule['subject'], message['subject'], re.IGNORECASE):
                return False
        elif not subject_matches(rule['subject'], message['subject']):
            return False
    if 'body' in rule:
        if 'body' not in message:
            return False
        # Escaping every operator makes this a literal search without backtracking.
        if not re.search(re.escape(rule['body']), message['body'], re.IGNORECASE):
            return False
    if 'is_reply' in rule and ('is_reply' not in message or message['is_reply'] != rule['is_reply']):
        return False
    if 'recipients_min' in rule or 'recipients_max' in rule:
        if 'recipients' not in message:
            return False
        count = len({a.casefold() for a in message['recipients']})
        if not rule.get('recipients_min', 1) <= count <= rule.get('recipients_max', 1000):
            return False
    if 'has_attachment' in rule and message.get('has_attachment', False) != rule['has_attachment']:
        return False
    return True


def received_date(message):
    try:
        date = datetime.fromisoformat(message.get('date', '').replace('Z', '+00:00'))
        return date if date.tzinfo is not None else None
    except ValueError:
        return None


def select_rule(message, rules):
    return min((r for r in rules if matches(message, r)), default=None,
               key=lambda r: (0 if 'sender' in r else 1 if 'domain' in r else 2, r.get('line', 0)))


def validate_actions(actions, listing):
    allowed = {(m['account'], m['id']) for m in listing['messages'] if m.get('folder') == 'Inbox'}
    for action in actions:
        if action['action'] not in PLAN_VERBS or (action['account'], action['id']) not in allowed:
            raise ValueError('Action is outside this account Inbox listing')


def known_keys(data, allowed, label):
    if not isinstance(data, dict):
        raise ValueError(label + ': expected a table/object')
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(label + '.' + str(sorted(unknown)[0]) + ': expected keys ' + ', '.join(sorted(allowed)))


NEWEST_IDS = 10
OBSERVATION_LIMIT = 65536



def schedule_slot(config, now):
    local = now.astimezone(ZoneInfo(config.get('timezone', 'UTC')))
    candidates = []
    for days in (0, 1, 2):
        day = local.date() - timedelta(days=days)
        for slot in config['schedule']['weekdays' if day.weekday() < 5 else 'weekends']:
            hour, minute = map(int, slot.split(':'))
            for fold in (0, 1):
                start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=local.tzinfo, fold=fold)
                # Nonexistent wall times are not occurrences of a slot.
                if (datetime.fromtimestamp(start.timestamp(), local.tzinfo).replace(fold=fold) == start
                        and start.timestamp() <= now.timestamp()):
                    candidates.append(start)
    return max(candidates, key=lambda d: d.timestamp())


def validate_positions(positions, config):
    known_keys(positions, set(config['accounts']), 'state.json._positions')
    for account, value in positions.items():
        fields = config['accounts'][account]
        kind = fields.get('quiet_kind', 'history' if fields.get('provider')=='gmail' else 'ids')
        if kind == 'history':
            if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,128}', value):
                raise ValueError('state.json._positions.' + account + ': expected 1 to 128 plain digits')
        elif (not isinstance(value, list) or len(value) > NEWEST_IDS or
              any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z0-9_=+/-]{1,512}', i) for i in value)
              or len(value) != len(set(value))):
            raise ValueError('state.json._positions.' + account + ': expected up to 10 unique ASCII ids (1 to 512 letters, digits or _ - = + /)')


def read_observation(path, config, saved, *, partial=False):
    # Errors identify fields only; gateway values never become diagnostics.
    account = ', '.join(safe_text(a) for a in config['accounts'])
    field = 'observation'
    try:
        if isinstance(path, dict):
            data = path
        else:
            path = Path(path)
            if not path.is_file() or not path.stat().st_mode & 0o444:
                raise ValueError()
            with path.open('rb') as stream:
                raw = stream.read(OBSERVATION_LIMIT + 1)
            if len(raw) > OBSERVATION_LIMIT:
                raise ValueError()
            data = json.loads(raw)
        field = 'accounts'
        if not isinstance(data, dict) or (set(data) - set(config['accounts']) if partial else set(data) != set(config['accounts'])):
            raise ValueError()
        positions, quiet_accounts = {}, []
        quiet = True
        for account, fields in config['accounts'].items():
            try:
                entry = data[account]
                kind = fields.get('quiet_kind', 'history' if fields.get('provider')=='gmail' else 'ids')
                field = 'fields'
                known_keys(entry, {'outcome', 'position', 'arrivals', 'reason'} if kind == 'history' else {'outcome', 'ids', 'reason'}, 'observation')
                field = 'outcome'
                if entry.get('outcome') == 'error':
                    reason = entry.get('reason', 'provider error (no status)')
                    if not isinstance(reason, str) or not re.fullmatch(r'(?:provider error \([A-Za-z0-9 _-]{1,40}\)|pigeon error \(listing answer\))', reason): reason = 'provider error (no status)'
                    print('Quiet check: ' + safe_text(account) + ': ' + reason + '; full run', file=sys.stderr)
                    if not partial: return None
                    continue
                if entry.get('outcome') != 'ok':
                    raise ValueError()
                field = 'position' if kind == 'history' else 'ids'
                position = entry.get(field)
                validate_positions({account: position}, config)
                if kind == 'history':
                    if account in saved and int(position) < int(saved[account]):
                        raise ValueError()
                    field = 'arrivals'
                    arrivals = entry.get('arrivals')
                    if (not isinstance(arrivals, list) or len(arrivals) > 1000 or
                            any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z0-9_=+/-]{1,512}', i) for i in arrivals)):
                        raise ValueError()
                    unchanged = account in saved and not arrivals
                else:
                    unchanged = position == saved.get(account)
                quiet = quiet and (not arrivals if kind == "history" else unchanged)
                positions[account] = position
                if unchanged: quiet_accounts.append(account)
            except (ValueError, TypeError, KeyError, RecursionError):
                print('Quiet check: ' + safe_text(account) + ': invalid ' + field + '; full run', file=sys.stderr)
                if not partial: return None
        return (positions, len(quiet_accounts) == len(config['accounts']), quiet_accounts) if partial else (positions, quiet)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print('Quiet check: ' + safe_text(account) + ': invalid ' + field + '; full run', file=sys.stderr)
        return None


def gateway_account(config, account):
    fields = config['accounts'][account]
    if 'executor_account' not in fields:
        return {}
    return {k: fields[k] for k in ('executor_app', 'executor_account') if k in fields}


def full_slots(config, now):
    full = config['schedule']['always_full']
    day = 'weekends' if schedule_slot(config, now).weekday() >= 5 else 'weekdays'
    return full.get(day, []) if isinstance(full, dict) else full


def quiet_check(store, config, previous, observation, scheduled, now, *, dry_run=False):
    import execute
    path = store / 'quiet-check.json'
    token = read_lock(store)['token'] if (store / 'run.lock').exists() else None
    if not dry_run and token is not None and path.exists():
        cached = parse_json(read_text(path), path)
        known_keys(cached, {'token', 'decision', 'positions'}, str(path))
        if cached.get('token') == token:
            return cached['decision']
    saved = previous.get('_positions', {})
    observed = read_observation(observation, config, saved, partial=True) if observation is not None else None
    if 'schedule' not in config:
        decision = {'decision': 'full', 'reason': 'no schedule'}
    elif not scheduled:
        decision = {'decision': 'full', 'reason': 'on demand'}
    elif schedule_slot(config, now).strftime('%H:%M') in full_slots(config, now):
        decision = {'decision': 'full', 'reason': 'always-full slot'}
    elif (store / 'run-message.md').exists():
        decision = {'decision': 'full', 'reason': 'undelivered message'}
    elif set(saved) != set(config['accounts']):
        decision = {'decision': 'full', 'reason': 'missing saved positions'}
    elif observed is None:
        decision = {'decision': 'full', 'reason': 'invalid or unavailable observation'}
    elif not observed[1]:
        decision = {'decision': 'full', 'reason': 'arrival or changed ids'}
    else:
        if not dry_run:
            with (store / 'history.log').open('a', encoding='utf-8') as out:
                out.write(now.astimezone(timezone.utc).isoformat() + ' skipped: no new mail\n')
        decision = {'decision': 'skip', 'reason': 'no new mail'}
    if observed is not None and not observed[0]:
        decision = {'decision': 'full', 'reason': 'account errors'}
    if (observed and scheduled and 'schedule' in config
            and schedule_slot(config, now).strftime('%H:%M') not in full_slots(config, now)
            and not (store / 'run-message.md').exists()):
        decision['quiet_accounts'] = observed[2]
    gateway = store/'quiet-gateway.json'
    if not dry_run and gateway.exists():
        data = parse_json(read_text(gateway), gateway)
        if data.get('token') == token:
            decision['retries'] = {a: ('retried once, still failing' if data['observations'][a]['outcome'] == 'error' else 'retried once, succeeded') for a in data.get('retried', [])}
    if dry_run:
        decision['dry_run'] = True
        decision['positions'] = observed[0] if observed else {}
    elif token is not None:
        execute.atomic(path, dict(token=token, decision=decision, positions=observed[0] if observed else None))
    return decision


def message_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_=+/-]{1,512}', value) is not None


def draft_digest(value, provider, *, allow_attachments=False):
    """Hash provider values only; preserve text, normalise newlines and addresses."""
    import base64
    def text(value):
        if not isinstance(value, str) or len(value) > 1000000:
            raise ValueError('draft reading: expected bounded text')
        return value.replace('\r\n', '\n').replace('\r', '\n')
    if not isinstance(value, dict):
        raise ValueError('draft reading: expected object')
    if provider == 'gmail':
        payload = value.get('message', {}).get('payload')
        if not isinstance(payload, dict) or not isinstance(payload.get('headers'), list):
            raise ValueError('draft reading: expected message payload')
        headers = {}
        for header in payload['headers']:
            name = header['name'].casefold()
            headers.setdefault(name, []).append(text(header['value']))
        identity = [headers.get(k, []) for k in ('from', 'reply-to')]
        subject = '\n'.join(headers.get('subject', []))
        recipients = [sorted({address.casefold() for _, address in getaddresses(headers.get(k, []))}) for k in ('to', 'cc', 'bcc')]
        attachments = []
        def body(part):
            if part.get('filename') or part.get('body', {}).get('attachmentId'):
                if not allow_attachments:
                    raise ValueError('draft reading: rule draft has attachment; send by hand')
                attachments.append([text(part.get('filename', '')), text(part.get('mimeType', '')), part.get('body', {}).get('size')])
            if 'parts' in part:
                return [body(p) for p in part['parts']]
            data = part.get('body', {}).get('data', '')
            raw = base64.b64decode(data + '=' * (-len(data) % 4), altchars=b'-_', validate=True)
            return [part.get('mimeType', ''), text(raw.decode('utf-8'))]
        content = body(payload)
    elif provider == 'outlook':
        identity = [value.get('from'), value.get('replyTo', [])]
        text(json.dumps(identity, sort_keys=True))
        attachments = value.get('attachments', [])
        if not isinstance(attachments, list) or (value.get('hasAttachments') and not attachments):
            raise ValueError('draft reading: attachment metadata required')
        if attachments and not allow_attachments:
            raise ValueError('draft reading: rule draft has attachment; send by hand')
        attachments = [[text(a['name']), text(a['contentType']), a['size']] for a in attachments]
        subject = text(value['subject'])
        recipients = []
        for key in ('toRecipients', 'ccRecipients', 'bccRecipients'):
            entries = value[key]
            if not isinstance(entries, list):
                raise ValueError('draft reading: expected recipient arrays')
            recipients.append(sorted({text(e['emailAddress']['address']).casefold() for e in entries}))
        content = [text(value['body']['contentType']).casefold(), text(value['body']['content'])]
    else:
        raise ValueError('draft reading: expected supported provider')
    if not any(recipients):
        raise ValueError('draft reading: expected recipients')
    return hashlib.sha256(json.dumps([recipients, subject, content, identity, attachments], ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def validate_drafts(records, config):
    if not isinstance(records, list):
        raise ValueError('state.json._drafts: expected array')
    seen = set()
    for record in records:
        known_keys(record, {'account', 'id', 'draft_id', 'rule', 'created', 'digest', 'issued', 'status'}, 'state.json._drafts')
        if (set(record) - {'digest', 'issued', 'status'} != {'account', 'id', 'draft_id', 'rule', 'created'}
                or record['account'] not in config['accounts'] or not message_id(record['id'])
                or (record['draft_id'] is not None and not message_id(record['draft_id']))
                or not isinstance(record['rule'], str)):
            raise ValueError('state.json._drafts: expected account, message id, draft id, rule and creation time')
        if 'digest' in record and (not isinstance(record['digest'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['digest']) or record['draft_id'] is None):
            raise ValueError('state.json._drafts.digest: expected SHA-256 of a known draft')
        if 'issued' in record:
            state_timestamp(record['issued'], 'state.json._drafts.issued')
            if 'digest' in record:
                raise ValueError('state.json._drafts.issued: issued draft cannot be sendable')
        if 'status' in record and (not isinstance(record['status'], str) or record['status'] not in {'sent', 'gone', 'hand-sent', 'unknown', 'forward-requested', 'refused'}):
            raise ValueError('state.json._drafts.status: expected finished draft status')
        if record.get('status') and 'digest' in record:
            raise ValueError('state.json._drafts.status: finished draft cannot be sendable')
        state_timestamp(record['created'], 'state.json._drafts.created')
        key = record['account'], record['id']
        if key in seen:
            raise ValueError('state.json._drafts: duplicate message')
        seen.add(key)


def validate_calendar_config(config):
    calendars = config.get('calendars', {})
    known_keys(calendars, set(calendars) if isinstance(calendars, dict) else set(), 'config.calendars')
    winners = 0
    pairs = set()
    for name, fields in calendars.items():
        known_keys(fields, {'executor_app', 'executor_account', 'wins'}, 'config.calendars.' + name)
        if not isinstance(fields.get('executor_app'), str) or not fields['executor_app'].strip():
            raise ValueError('calendar executor_app: expected nonempty string')
        if 'executor_account' in fields:
            value = fields['executor_account']
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
                raise ValueError('calendar executor_account: expected account handle')
            pair = fields['executor_app'], value
            if pair in pairs:
                raise ValueError('calendar executor_account: expected unique app/account pair')
            pairs.add(pair)
        if type(fields.get('wins', False)) is not bool:
            raise ValueError('calendar wins: expected boolean')
        winners += fields.get('wins', False)
    if winners > 1:
        raise ValueError('calendars: at most one wins')
    settings = config.get('calendar', {})
    known_keys(settings, {'days', 'work_start', 'work_end', 'quiet_start', 'quiet_end'}, 'config.calendar')
    if type(settings.get('days', 3)) is not int or not 1 <= settings.get('days', 3) <= 14:
        raise ValueError('calendar.days: expected integer 1 to 14')
    for prefix in ('work', 'quiet'):
        start, end = settings.get(prefix + '_start'), settings.get(prefix + '_end')
        if start is None and end is None:
            continue
        if any(not isinstance(v, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', v) for v in (start, end)) or start >= end:
            raise ValueError('calendar.' + prefix + ': expected paired increasing HH:MM times')


def calendar_events(data, config):
    known_keys(data, set(config.get('calendars', {})), 'calendar.json')
    for name, events in data.items():
        if isinstance(events, dict) and set(events) == {'failed'} and events['failed'] is True:
            continue
        if not isinstance(events, list):
            raise ValueError('calendar.json: expected event array or {"failed": true}')
        seen = set()
        for event in events:
            required = {'id', 'start', 'end', 'all_day', 'response', 'cancelled'}
            known_keys(event, required | {'subject'}, 'calendar event')
            if not required <= set(event) or not message_id(event['id']) or event['id'] in seen:
                raise ValueError('calendar event: expected complete fields and unique message-style id')
            seen.add(event['id'])
            if any(type(event[k]) is not bool for k in ('all_day', 'cancelled')) or event['response'] not in ('pending', 'accepted', 'tentative', 'declined', 'organizer') or not isinstance(event.get('subject', ''), str):
                raise ValueError('calendar event: invalid flags, response or subject')
            times = []
            for key in ('start', 'end'):
                value = event[key]
                if not isinstance(value, str):
                    raise ValueError('calendar event: expected ISO dates or offset times')
                if event['all_day']:
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                        raise ValueError('calendar event: expected all-day date')
                    parsed = datetime.strptime(value, '%Y-%m-%d')
                else:
                    parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo is None:
                        raise ValueError('calendar event: expected offset time')
                times.append(parsed.timestamp() if not event['all_day'] else parsed)
            if times[1] <= times[0]:
                raise ValueError('calendar event: end must follow start')
    return data


FORMAT_VERSION = 1


def check_version(data, source):
    value = data.get('version', FORMAT_VERSION)
    if type(value) is not int or value < 1 or value > FORMAT_VERSION:
        raise ValueError(f'{source}: version {value!r} found; supported version {FORMAT_VERSION}')


def validate_inputs(listing, config, previous):
    check_version(config, 'config.toml')
    check_version(previous, 'state.json')
    known_keys(config, {'version', 'accounts', 'language', 'inbox_limit', 'timezone', 'schedule', 'lock', 'state', 'relist_inbox', 'calendar', 'calendars', 'approval_hours', 'allow_send'}, 'config')
    if type(config.get('allow_send', False)) is not bool:
        raise ValueError('config.allow_send: expected boolean')
    approval_hours(config)
    validate_calendar_config(config)
    lock_setting(config)
    state_setting(config)
    if config.get('relist_inbox', 'all') not in ('all', 'new_plus_count'):
        raise ValueError('config.relist_inbox: expected all or new_plus_count')
    if not isinstance(config.get('language', 'en'), str) or config.get('language', 'en') not in STRINGS:
        raise ValueError('config.language: expected en or es')
    if type(config.get('inbox_limit', 100)) is not int or config.get('inbox_limit', 100) <= 0:
        raise ValueError('config.inbox_limit: expected a positive integer')
    if not isinstance(config.get('timezone', 'UTC'), str) or not config.get('timezone', 'UTC').strip():
        raise ValueError('config.timezone: expected a non-empty time zone string')
    try:
        ZoneInfo(config.get('timezone', 'UTC'))
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError('config.timezone: expected a known IANA time zone, such as UTC') from error
    if 'schedule' in config:
        schedule = config['schedule']
        known_keys(schedule, {'weekdays', 'weekends', 'always_full'}, 'config.schedule')
        full = schedule.get('always_full')
        if isinstance(full, dict):
            known_keys(full, {'weekdays', 'weekends'}, 'config.schedule.always_full')
        for key, times in list((k, schedule.get(k)) for k in ('weekdays', 'weekends')) + list(
                (('always_full.' + k, v) for k, v in full.items()) if isinstance(full, dict)
                else [('always_full', full)]):
            if (not isinstance(times, list) or any(not isinstance(t, str) or
                    not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', t) for t in times)
                    or len(times) != len(set(times))
                    or (key in ('weekdays', 'weekends') and not times)):
                raise ValueError('config.schedule.' + key + ': expected unique HH:MM times (nonempty day slots)')
            if key.startswith('always_full'):
                slots = schedule.get(key.split('.')[-1], []) if '.' in key else schedule['weekdays'] + schedule['weekends']
                if not set(times) <= set(slots):
                    raise ValueError('config.schedule.' + key + ': expected configured slot times')
    accounts = config.get('accounts')
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError('config.accounts: expected a non-empty table')
    for account, fields in accounts.items():
        if account in {'version', '_positions', '_drafts', '_run_counter', '_next_number'}:
            raise ValueError('config.accounts._positions: reserved state key')
        known_keys(fields, {'name', 'address', 'executor_app', 'executor_account', 'quiet_kind', 'provider'}, 'config.accounts.' + account)
        for key, value in fields.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError('config.accounts.' + account + '.' + key + ': expected a non-empty string')
        if 'name' in fields and (len(fields['name']) > 80 or fields['name'] != fields['name'].strip() or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in fields['name'])):
            raise ValueError('config.accounts.' + account + '.name: expected a plain single-line string of 1 to 80 characters')
        if 'executor_app' in fields and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', fields['executor_app']):
            raise ValueError('config.accounts.' + account + '.executor_app: expected safe app slug')
        if 'provider' in fields and fields['provider'] not in ('gmail', 'outlook'):
            raise ValueError('config.accounts.' + account + '.provider: expected gmail or outlook')
        if fields.get('provider') and 'quiet_kind' in fields and fields['quiet_kind'] != ('history' if fields['provider']=='gmail' else 'ids'):
            raise ValueError('config.accounts.' + account + '.quiet_kind: expected history for gmail or ids for outlook')
        if 'executor_account' in fields:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', fields['executor_account']):
                raise ValueError('config.accounts.' + account + '.executor_account: expected 1 to 128 ASCII letters, digits, underscore or hyphen')
            if any(other != account and isinstance(f, dict) and
                   (f.get('executor_app'), f.get('executor_account')) ==
                   (fields.get('executor_app'), fields['executor_account'])
                   for other, f in accounts.items()):
                raise ValueError('config.accounts.' + account + '.executor_account: expected a unique executor_app and executor_account pair')
        if fields.get('quiet_kind', 'history' if fields.get('provider')=='gmail' else 'ids') not in ('history', 'ids'):
            raise ValueError('config.accounts.' + account + '.quiet_kind: expected history or ids')
    known_keys(listing, {'accounts', 'messages', 'folders', 'skipped', 'quiet'}, 'Inbox')
    quiet = listing.get('quiet', [])
    if not isinstance(quiet, list) or any(a not in config['accounts'] for a in quiet) or set(quiet) & set(listing.get('accounts', {})):
        raise ValueError('Inbox.quiet: expected absent configured accounts')
    skipped = listing.get('skipped', {})
    if not isinstance(skipped, dict) or set(skipped) - set(config['accounts']) or any(not isinstance(r, str) or not re.fullmatch(r'(?:provider error \([A-Za-z0-9 _-]{1,40}\)|pigeon error \((?:manual skip|listing answer|no new mail|after_send reading)\))', r) for r in skipped.values()):
        raise ValueError('Inbox.skipped: expected account skip reasons')
    if set(skipped) & set(listing.get('accounts', {})) or any(m.get('account') in skipped for m in listing.get('messages', [])):
        raise ValueError('Inbox.skipped: skipped account must be absent')
    if not isinstance(listing.get('accounts'), dict):
        raise ValueError('Inbox.accounts: expected account totals object')
    for account, total in listing['accounts'].items():
        if type(total) is not int or total < 0:
            raise ValueError('Inbox.accounts.' + account + ': expected a non-negative integer')
    if not isinstance(listing.get('messages'), list):
        raise ValueError('Inbox.messages: expected an array')
    for i, message in enumerate(listing['messages']):
        label = 'Inbox.messages.' + str(i)
        required = {'account', 'id', 'folder', 'sender', 'subject'}
        flags = {'has_attachment', 'spam', 'phishing', 'unsubscribe_one_click', 'is_reply'}
        strings = required | {'body', 'sender_name', 'unsubscribe_url', 'date', 'conversation_id', 'internet_message_id'}
        known_keys(message, strings | flags | {'recipients'}, label)
        if 'recipients' in message:
            recipients = message['recipients']
            if (not isinstance(recipients, list) or len(recipients) > 2000 or any(not isinstance(a, str) or len(a) > 254 or normalize_sender(a) != a.casefold() or any(unicodedata.category(c).startswith('C') for c in a) for a in recipients)):
                raise ValueError(label + '.recipients: expected at most 2000 bare addresses, each at most 254 characters')
        for key in required | (set(message) - {'recipients'}):
            if key not in message or (type(message[key]) is not bool if key in flags else not isinstance(message[key], str)):
                raise ValueError(label + '.' + key + ': expected ' + ('boolean' if key in flags else 'string'))
        if 'body' in message and len(message['body']) > BODY_LIMIT:
            raise ValueError(label + '.body: expected at most 2000 characters')
    folders = listing.get('folders', {})
    known_keys(folders, set(accounts), 'Inbox.folders')
    for account, entries in folders.items():
        if not isinstance(entries, list):
            raise ValueError('Inbox.folders.' + account + ': expected an array')
        for folder in entries:
            known_keys(folder, {'id', 'name'}, 'Inbox.folders.' + account)
            if any(not isinstance(folder.get(k), str) for k in ('id', 'name')):
                raise ValueError('Inbox.folders.' + account + ': expected id and name strings')
    for account, fields in accounts.items():
        name = fields.get('name', account).casefold()
        if any(other != account and name in {other.casefold(), value.get('name', other).casefold()}
               for other, value in accounts.items()):
            raise ValueError('config.accounts.' + account + '.name: expected a unique name distinct from other account handles')
    known_keys(previous, set(accounts) | {'version', '_positions', '_drafts', '_run_counter', '_next_number'}, 'state.json')
    if '_run_counter' in previous and read_run_counter(previous['_run_counter']) is None:
        raise ValueError('state.json._run_counter: expected non-negative integer')
    validate_drafts(previous.get('_drafts', []), config)
    if '_positions' in previous:
        validate_positions(previous['_positions'], config)
    if any(type(n) is not int or n < 0 for a, n in previous.items() if a not in {'version', '_positions', '_drafts', '_run_counter', '_next_number'}):
        raise ValueError('state.json: expected non-negative integer counts')


def hand_sent_calls(listing, rules, config, previous, store=None, unchecked=None):
    """Read only known drafts and the original's conversation; no mail text."""
    calls, records = [], []
    messages = {(m['account'], m['id']): m for m in listing['messages']}
    for record in previous.get('_drafts', []):
        key = record['account'], record['id']
        rule = next((r for r in rules if r['id'] == record['rule'] and r.get('action') == 'draft' and 'after_send' in r), None)
        if rule is None or key not in messages or record.get('issued') or record.get('status'):
            continue
        fields = config['accounts'][key[0]]
        conversation = messages[key].get('conversation_id')
        if record['draft_id'] is None or not conversation or fields.get('provider') not in ('gmail', 'outlook') or not fields.get('executor_app') or not fields.get('executor_account'):
            if unchecked is not None:
                unchecked[key[0]] = 'pigeon error (after_send reading)'
            continue
        gmail = fields['provider'] == 'gmail'
        draft = dict(path=dict(userId='me', id=record['draft_id']), query=dict(fields='id')) if gmail else dict(path={'message-id':record['draft_id']}, query={'$select':['id', 'isDraft']})
        sent = dict(path=dict(userId='me', id=conversation), query=dict(format='metadata', fields='id,messages(id,internalDate,labelIds)')) if gmail else dict(path={'mailFolder-id':'sentitems'}, query={'$select':['id', 'sentDateTime'], '$filter':"conversationId eq '" + conversation.replace("'", "''") + "'", '$top':100})
        for tail, inner in [('gmail.users.drafts.get' if gmail else 'meMessage.meGetMessages', draft), ('gmail.users.threads.get' if gmail else 'meMailFolder.meMailFoldersListMessages', sent)]:
            calls.append(dict(path=tool_path(fields['executor_app'], tail), input=dict(accountId=fields['executor_account'], input=inner)))
        if store is not None:
            selected = saved_tool(store, key[0], 'sent-read')
            if selected:
                calls[-1]['path'] = selected['tool']
        checked_tool(config, key[0], 'sent-read', calls[-1]['path'])
        records.append(dict(record, conversation_id=conversation) if gmail else record)
    if len(calls) > LISTING_BATCH:
        raise ValueError('after_send: too many open records for one reading; nothing filed')
    return calls, records


def hand_sent_results(records, values, rules, config):
    filing, closed, failed = {}, [], {}
    for index, record in enumerate(records):
        draft, sent = values[index * 2:index * 2 + 2]
        account = record['account']
        problem = None
        try:
            absent = provider_failed(draft) and provider_error(draft) == 'provider error (404)'
            if not absent:
                if provider_failed(draft):
                    problem = provider_error(draft)
                    raise ValueError()
                payload = draft.get('value')
                if not isinstance(payload, dict) or payload.get('id') != record['draft_id']:
                    raise ValueError()
                if config['accounts'][account]['provider'] == 'gmail' or payload.get('isDraft') is True:
                    continue
                if payload.get('isDraft') is not False:
                    raise ValueError()
            if provider_failed(sent):
                problem = provider_error(sent)
                raise ValueError()
            payload = sent.get('value')
            gmail = config['accounts'][account]['provider'] == 'gmail'
            if not isinstance(payload, dict) or any(k in payload for k in ('nextPageToken', '@' + 'odata.nextLink')):
                raise ValueError()
            if gmail and payload.get('id') != record.get('conversation_id'):
                raise ValueError()
            entries = payload.get('messages' if gmail else 'value', [])
            if not isinstance(entries, list) or len(entries) > 100:
                raise ValueError()
            dates = []
            for entry in entries:
                if not isinstance(entry, dict) or not message_id(entry.get('id')):
                    raise ValueError()
                if gmail:
                    if not isinstance(entry.get('labelIds'), list):
                        raise ValueError()
                    if 'SENT' not in entry['labelIds']:
                        continue
                    raw = entry.get('internalDate')
                    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or len(raw) > 16:
                        raise ValueError()
                    date = datetime.fromtimestamp(int(raw) / 1000, timezone.utc)
                else:
                    raw = entry.get('sentDateTime')
                    if not isinstance(raw, str):
                        raise ValueError()
                    date = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                    if date.tzinfo is None:
                        raise ValueError()
                dates.append(date)
            key = account, record['id']
            closed.append(list(key))
            if any(date > datetime.fromisoformat(record['created']) for date in dates):
                rule = next(r for r in rules if r['id'] == record['rule'])
                filing[key] = dict(id=rule['id'], action=rule['after_send'], reason='Owner sent the draft')
        except (ValueError, TypeError, KeyError, OverflowError, OSError, StopIteration):
            failed[account] = problem or 'pigeon error (after_send reading)'
    return filing, closed, failed


def after_send_listing(listing, failed):
    if not failed:
        return listing
    known_keys(failed, set(failed) if isinstance(failed, dict) else set(), 'hand_sent.failed')
    return dict(listing, accounts={a:n for a,n in listing['accounts'].items() if a not in failed},
                messages=[m for m in listing['messages'] if m['account'] not in failed],
                folders={a:f for a,f in listing.get('folders', {}).items() if a not in failed},
                skipped=listing.get('skipped', {}) | failed)


def question_answers(document, run_token):
    if document is None:
        return {}, '', []
    if (not isinstance(document, dict) or set(document) != {'run', 'answers'}
            or not isinstance(document['run'], str) or not isinstance(document['answers'], list)):
        return {}, 'answers file unreadable', []
    if not run_token or document['run'] != run_token:
        return {}, 'answers are from another run', []
    answers, malformed = {}, []
    for position, entry in enumerate(document['answers'], 1):
        if isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in ('account', 'id')):
            key = (entry['account'], entry['id'])
            answers.setdefault(key, []).append(entry)
        else:
            malformed.append('answer ' + str(position))
    return answers, '', malformed


def answer_problem(entry):
    """Name the field that makes an answer unreadable, so the owner can fix it."""
    unexpected = sorted(set(entry) - {'account', 'id', 'proceed', 'note'})
    if unexpected:
        return 'unexpected ' + ', '.join(unexpected)
    if type(entry.get('proceed')) is not bool:
        return 'proceed'
    if not isinstance(entry.get('note'), str) or not entry['note'].strip() or len(entry['note']) > 200:
        return 'note'
    return ''


def same_question(question, rule):
    """An answer authorizes the row the question showed, not whatever the table says now."""
    if rule is None:
        return False
    then = [step['verb'] for step in rule['steps'] if 'verb' in step] if 'steps' in rule else [rule['action']]
    return (question.get('row'), question.get('then'), question.get('text')) == (rule['id'], then, rule.get('note', ''))


def read_asked(store, token, dry_run=False):
    """This run's questions, so an answer stays bound to the row it showed."""
    path = store / ('.dry-run' if dry_run else '') / 'questions.json'
    try:
        document = parse_json(read_text(path), path)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or document.get('run') != token or not isinstance(document.get('questions'), list):
        return None
    return {(q['account'], q['id']): q for q in document['questions']
            if isinstance(q, dict) and isinstance(q.get('account'), str) and isinstance(q.get('id'), str)}


def build_plan(listing, rules, never_touch, config, previous=None, stop_reasons=(), *, selections=None, answers=None, run_token=None, questions=None, asked=None, hand_sent=None, store=None):
    validate_inputs(listing, config, previous if previous is not None else {})
    if hand_sent is not None:
        known_keys(hand_sent, {'closed', 'filed', 'failed', 'unchecked'}, 'hand_sent')
        if 'failed' in hand_sent:
            known_keys(hand_sent['failed'], set(config['accounts']), 'hand_sent.failed')
        if any(not isinstance(hand_sent.get(k), list) for k in ('closed', 'filed')):
            raise ValueError('hand_sent: expected closed and filed original lists')
    if hand_sent is not None and hand_sent.get('failed'):
        listing = after_send_listing(listing, hand_sent['failed'])
        validate_inputs(listing, config, previous if previous is not None else {})
    validate_holds(never_touch)
    for index, hold in enumerate(never_touch, 1):
        for account in hold.get('accounts', []):
            if account not in config['accounts']:
                raise ValueError(f'never-touch.toml: entry {index}: account {account!r} expected one of config.toml accounts: ' + ', '.join(config['accounts']))
    filing = {}
    if hand_sent is not None:
        known_keys(hand_sent, {'closed', 'filed', 'failed', 'unchecked'}, 'hand_sent')
        for key in hand_sent.get('closed', []):
            if not isinstance(key, list) or len(key) != 2 or any(not isinstance(v, str) for v in key) or key[0] in hand_sent.get('failed', {}) or not any([d['account'], d['id']] == key for d in (previous or {}).get('_drafts', [])):
                raise ValueError('hand_sent: expected recorded original')
        for key in hand_sent.get('filed', []):
            if key not in hand_sent.get('closed', []):
                raise ValueError('hand_sent: filing requires closed record')
            record = next(d for d in previous['_drafts'] if [d['account'], d['id']] == key)
            rule = next((r for r in rules if r['id'] == record['rule'] and r.get('action') == 'draft' and 'after_send' in r), None)
            if rule is None:
                raise ValueError('hand_sent: after_send rule required')
            filing[tuple(key)] = dict(id=rule['id'], action=rule['after_send'], reason='Owner sent the draft')
    exact_holds = [hold for hold in never_touch if 'judgment' not in hold]
    categories = [hold for hold in never_touch if 'judgment' in hold]
    answer_entries, answer_error, malformed = question_answers(answers, run_token)
    asked_any = False
    messages = listing['messages']
    totals = listing['accounts']
    not_listed = [a for a in config['accounts'] if a not in totals and a not in listing.get('quiet', [])]
    if set(totals) - set(config['accounts']) or not set(totals) & set(config['accounts']):
        return {'account_names': {a: f.get('name', a) for a, f in config['accounts'].items()}, 'relist_inbox': config.get('relist_inbox', 'all'),
                'remaining': [dict(account=m['account'], id=m['id'], sender=m['sender'], subject=m['subject'], reason='Run stopped') for m in messages], 'actions': [],
                'decisions': [{'account': m['account'], 'id': m['id'], 'reason': 'Protected by never-touch' if any(matches(m, h, protection=True) for h in exact_holds) else 'Run stopped'} for m in messages],
                'reasons': list(stop_reasons) + ['Listing has an unconfigured account' if set(totals) - set(config['accounts']) else 'No configured account listed'],
                'not_listed': not_listed, 'skip_reasons': {a:listing.get('skipped', {}).get(a, 'pigeon error (listing answer)') for a in not_listed},
                'counts': {a: sum(m['account'] == a for m in messages) for a in totals},
                **({'hand_sent': hand_sent} if hand_sent is not None else {})}
    keys = [(m['account'], m['id']) for m in messages]
    if len(set(keys)) != len(keys) or any(m['account'] not in totals or m.get('folder') != 'Inbox' for m in messages):
        raise ValueError('Inbox.messages: Listing must contain unique Inbox ids from declared accounts; expected unique (account, id) pairs, folder Inbox, and accounts present in Inbox.accounts')
    counts = {a: sum(m['account'] == a for m in messages) for a in totals}
    reasons = list(stop_reasons)
    unconditional = bool(reasons)
    thresholds = []
    if len(messages) > config.get('inbox_limit', 100):
        reasons.append('Inbox limit exceeded')
        thresholds.append(dict(kind='inbox', count=len(messages), limit=config.get('inbox_limit', 100)))
    for account, count in counts.items():
        if count == 0 and (previous or {}).get(account, 0) >= 3:
            reasons.append('Unexpected empty Inbox: ' + account)
            unconditional = True
        if totals[account] < count:
            raise ValueError('Inbox.accounts.' + account + ': Account total is smaller than Inbox count; expected an all-folder total at least ' + str(count))
    closed = closed_drafts(store) if store is not None else {}
    closed.update({tuple(k): float('inf') for k in (hand_sent or {}).get('closed', [])})
    actions, decisions, hits = [], [], {}
    for original in messages:
        sender = normalize_sender(original.get('sender'))
        message = dict(original, sender=sender)
        reason = 'No matching rule'
        question = {}
        if sender is None:
            reason = 'Invalid sender'
        elif any(matches(message, hold, protection=True) for hold in exact_holds):
            reason = 'Protected by never-touch'
        else:
            key = message['account'], message['id']
            selected = filing.get(key) or (select_rule(message, rules) if selections is None else selections.get(key))
            changed = asked is not None and key in asked and not same_question(asked[key], selected)
            if changed:
                asked_any = True
                reason = 'the rule changed after the question'
            for rule in [selected] if selected is not None and not changed else []:
                steps = rule.get('steps', [])
                agent = [step['agent'] for step in steps if 'agent' in step]
                kit = [step for step in steps if 'verb' in step]
                text = rule.get('note', '')
                category_text = [hold['judgment'] for hold in categories if matches(message, hold, protection=True)]
                if 'steps' in rule:
                    missing = next((file for file in rule['files'] if not Path(file['path']).is_file()), None)
                    if missing is not None:
                        reason = f"Needs {missing['ref']}, which is not available"
                        break
                note = ''
                if text or agent or category_text:
                    asked_any = True
                    if questions is not None:
                        questions.append(dict(account=key[0], id=key[1], sender=original['sender'],
                                              subject=message['subject'], date=message.get('date', ''),
                                              row=rule['id'], text=text, steps=agent,
                                              then=[step['verb'] for step in kit] if 'steps' in rule else [rule['action']],
                                              never_touch=category_text))
                    submitted = answer_entries.pop(key, [])
                    entry = submitted[0] if len(submitted) == 1 else {}
                    problem = 'duplicate' if len(submitted) > 1 else answer_problem(entry) if submitted else ''
                    if answer_error or not submitted:
                        reason = answer_error or rule['id'] + ' not answered'
                        question = {'note': reason}
                    elif problem:
                        reason = rule['id'] + ' answer unreadable: ' + problem
                        question = {'note': reason}
                    else:
                        note = entry['note'].strip()
                        reason = '' if entry['proceed'] and ('steps' not in rule or kit) else rule['id'] + ': ' + safe_text(note)
                        question = {'note': note} if reason else {}
                    if reason:
                        if category_text and (not note or not entry['proceed']):
                            reason = 'Protected by never-touch'
                        break
                if 'steps' in rule:
                    rule = rule | dict(action=kit[-1]['verb'], target=kit[-1].get('target'))
                if rule.get('keep_latest'):
                    date = received_date(message)
                    peers = [dict(m, sender=normalize_sender(m['sender'])) for m in messages if m['account'] == message['account'] and normalize_sender(m['sender']) is not None]
                    dates = [received_date(m) for m in peers if matches(m, rule)]
                    valid = [d for d in dates if d is not None]
                    if date is None or not valid or date == max(valid):
                        break
                if rule['action'] == 'draft' and (message['account'], message['id']) in closed:
                    break
                if rule['action'] == 'draft' and any(d['account'] == message['account'] and d['id'] == message['id'] for d in (previous or {}).get('_drafts', [])):
                    reason = 'No matching rule' if any(d.get('status') in {'sent', 'gone', 'hand-sent', 'refused'} and (d['account'], d['id']) == (message['account'], message['id']) for d in (previous or {}).get('_drafts', [])) else 'Draft waiting'
                    break
                key = (rule['id'], message['account'])
                if 'question' not in rule and 'steps' not in rule:
                    hits[key] = hits.get(key, 0) + 1
                if len(kit) == 2:
                    url = message.get('unsubscribe_url', '')
                    if not safe_unsubscribe_host(url) or message.get('spam') or message.get('phishing'):
                        reason = 'Unsafe unsubscribe'
                        break
                verb = rule['action']
                action = {'account': message['account'], 'id': message['id'], 'action': verb, 'rule': rule['id']}
                if note:
                    action['note'] = note
                if 'reason' in rule:
                    action['reason'] = rule['reason']
                if verb == 'send-draft':
                    action.update(target=rule['target'], digest=rule['digest'], draft=rule['draft'], draft_to=rule['draft_to'])
                if verb == 'draft':
                    if not message_id(message['id']):
                        raise ValueError('Inbox.messages.id: expected a valid message id for draft creation')
                    action.update(action='create-draft', draft=rule['draft'], draft_text=rule['draft_text'], draft_to=rule.get('draft_to', []))
                if verb == 'move':
                    target = rule.get('target')
                    if target and ('{year}' in target or '{month}' in target):
                        try:
                            date = datetime.fromisoformat(message.get('date', '').replace('Z', '+00:00'))
                            if not 1990 <= date.year <= 2100:
                                raise ValueError('date year outside fixed range')
                        except ValueError:
                            reason = 'Move date is missing or invalid'
                            break
                        target = target.replace('{year}', f'{date.year:04d}').replace('{month}', f'{date.month:02d}')
                    if message['account'] not in listing.get('folders', {}):
                        reason = 'Listing lacked folder information; message stays in Inbox'
                        break
                    entries = listing['folders'][message['account']]
                    folders = [f for f in entries if target and f['name'].casefold() == target.casefold()]
                    # Without the full path, a leaf matching a nested destination cannot identify its path.
                    if not folders and target and '/' in target and any(f['name'].casefold() == target.rsplit('/', 1)[1].casefold() for f in entries):
                        if 'steps' in rule:
                            reason = 'Move target is ambiguous or missing'
                            break
                        raise ValueError('Inbox.folders.' + message['account'] + '.name: expected full path, not a destination leaf name')
                    if len(folders) > 1 or not target:
                        reason = 'Move target is ambiguous or missing'
                        break
                    if folders:
                        action['target'] = folders[0]['id']
                    elif ('steps' not in rule or len(kit) == 1) and ('{year}' in rule['target'] or '{month}' in rule['target']):
                        action['target_path'] = folder_path(target)
                        actions.append(action | {'action': 'create-folder'})
                    else:
                        reason = 'Move target is ambiguous or missing'
                        break
                if verb == 'unsubscribe':
                    action['url'] = message.get('unsubscribe_url', '')
                    if not safe_unsubscribe_host(action['url']) or message.get('spam') or message.get('phishing'):
                        fallback = rule.get('unsubscribe_fallback', 'ask')
                        if fallback == 'ask' or message.get('spam') or message.get('phishing'):
                            reason = 'Unsafe unsubscribe'
                            break
                        verb = fallback
                        action.update(action=fallback, reason='No usable unsubscribe link found')
                        action.pop('url')
                if verb == 'report':
                    reason = 'Rule requests a decision'
                    question = {'question': rule['question']} if 'question' in rule else {}
                    break
                if kit:
                    hits[key] = hits.get(key, 0) + 1
                if len(kit) == 2:
                    actions.append(dict(account=message['account'], id=message['id'], rule=rule['id'],
                                        action='unsubscribe', url=url))
                actions.append(action)
                if rule['id'] == 'numbered-forward' and selections is not None:
                    actions.append(dict(account=action['account'], id=action['id'], action='send-draft', rule='numbered-forward', target='forward-created-draft', digest='0' * 64, draft='forward', draft_to=rule['draft_to']))
                if verb == 'send-draft' and 'after_send' in rule:
                    actions.append(dict(account=action['account'], id=action['id'], action=rule['after_send'], rule=rule['id']))
                if verb == 'unsubscribe' and 'after_unsubscribe' in rule:
                    actions.append(dict(account=action['account'], id=action['id'],
                                        action=rule['after_unsubscribe'], rule=rule['id']))
                reason = None
                break
        if reason:
            decisions.append({'account': message['account'], 'id': message['id'], 'reason': reason, **question})
    # Choose spelling by rule order, independently of Inbox ordering.
    rank = {r['id']: i for i, r in enumerate(rules)}
    paths = {}
    for action in actions:
        if 'target_path' in action:
            key = (action['account'], action['target_path'].casefold())
            if key not in paths or rank[action['rule']] < rank[paths[key]['rule']]:
                paths[key] = action
    names = {key: action['target_path'] for key, action in paths.items()}
    creates = set()
    unique = []
    for action in actions:
        if 'target_path' in action:
            key = (action['account'], action['target_path'].casefold())
            action['target_path'] = names[key]
            if action['action'] == 'create-folder':
                if key in creates:
                    continue
                creates.add(key)
        unique.append(action)
    actions = unique
    if sum(a['action'] in {'trash', 'move', 'archive', 'spam'} for a in actions) > 50:
        reasons.append('Destructive action limit exceeded')
        thresholds.append(dict(kind='destructive', count=sum(a['action'] in REMOVALS for a in actions), limit=50))
    for rule in {rule['id']: rule for rule in rules}.values():
        count = sum((a['rule'] == rule['id'] or (selections is not None and rule['id'] == 'numbered-reply' and a['rule'] == 'numbered-forward')) and a['action'] not in {'create-folder', 'send-draft'} and not (selections is not None and a['rule'] == 'numbered-forward') for a in actions)
        if count > rule.get('limit', 10):
            reasons.append(f"Per-rule action limit exceeded: {rule['id']} ({count} of {rule.get('limit', 10)})")
            thresholds.append(dict(kind="rule", rule=rule["id"], count=count, limit=rule.get("limit", 10)))
    if sum(a['action'] == 'create-draft' for a in actions) > DRAFT_LIMIT:
        reasons.append('Draft limit exceeded')
        thresholds.append(dict(kind='draft', count=sum(a['action'] == 'create-draft' for a in actions), limit=DRAFT_LIMIT))
    if sum(a['action'] == 'unsubscribe' for a in actions) > 10:
        reasons.append('Unsubscribe limit exceeded')
        thresholds.append(dict(kind='unsubscribe', count=sum(a['action'] == 'unsubscribe' for a in actions), limit=10))
    for (rule, account), count in sorted(hits.items()):
        allowed = max(BRAKE_FLOOR, totals[account] // 5)
        if count > allowed:
            reasons.append(f'Rule exceeds twenty percent of account total: {rule}, {account} ({count} matches, total {totals[account]}, allowed {allowed})')
            thresholds.append(dict(kind='brake', account=account, rule=rule, count=count, total=totals[account], limit=allowed))
    refused = {(a['account'], a['id']) for a in actions if any(
        r.startswith('Rule exceeds twenty percent of account total: ' + a['rule'] + ', ' + a['account'] + ' (')
        for r in reasons)}
    if unconditional:
        actions = []
        protections = {(d['account'], d['id']): d for d in decisions if d['reason'] == 'Protected by never-touch'}
        decisions = [protections.get((m['account'], m['id']), {'account': m['account'], 'id': m['id'], 'reason': 'Protected by never-touch' if any(matches(m, h, protection=True) for h in exact_holds) else 'Run stopped; rule refused by brake' if (m['account'], m['id']) in refused else 'Run stopped'}) for m in messages]
    validate_actions(actions, listing)
    expected = dict(counts)
    for action in actions:
        if action['action'] in {'trash', 'archive', 'move', 'spam'}:
            expected[action['account']] -= 1
    result = {'actions': actions, 'decisions': decisions, 'reasons': reasons, 'counts': counts,
            'expected_counts': expected, 'not_listed': not_listed, 'skip_reasons': {a:listing.get('skipped', {}).get(a, 'pigeon error (listing answer)') for a in not_listed},
            'relist_inbox': config.get('relist_inbox', 'all'),
            'account_names': {a: f.get('name', a) for a, f in config['accounts'].items()}}
    if answer_entries or malformed:
        result['ignored_answers'] = [f'{account} {message_id}' for account, message_id in answer_entries] + malformed
    if answer_error and not any(d['reason'] == answer_error for d in decisions):
        result['answers_error'] = answer_error
    if asked_any:
        result['answers_used'] = True
    if reasons and not unconditional:
        result['approval_state'] = 'awaiting approval'
        result['approval_thresholds'] = thresholds
    if hand_sent is not None:
        result['hand_sent'] = hand_sent
    result['remaining'] = remaining_items(result, listing)
    for item in result['remaining']:
        for record in (previous or {}).get('_drafts', []):
            if (item['account'], item['id']) == (record['account'], record['id']) and (item['account'], item['id']) not in closed and record.get('status') not in {'sent', 'gone', 'hand-sent'}:
                item['reason'] = draft_note(record, allow_send=config.get('allow_send', False))
    return result


REMOVALS = {'trash', 'archive', 'move', 'spam'}


def closed_drafts(store):
    path = store / 'calls.jsonl'
    entries = [parse_json(line, path) for line in (read_text(path).splitlines() if path.exists() else [])]
    closed = {}
    for entry in entries:
        key = entry['account'], entry['id']
        if entry['verb'] == 'create-draft':
            if entry['outcome'] == 'closed':
                closed[key] = entry['time']
    return closed


def recover_drafts(store, previous=None, token=None, *, plan=None, observed=(), listing=None):
    """One source for saved, recovered and current-run draft records."""
    from execute import read
    if previous is None:
        previous = read(store / 'state.json') if (store / 'state.json').exists() else {}
    previous = dict(previous)
    drafts = [dict(d) for d in previous.get('_drafts', [])]
    known = {(d['account'], d['id']) for d in drafts}
    path = store / 'calls.jsonl'
    entries = [parse_json(line, path) for line in (read_text(path).splitlines() if path.exists() else [])]
    closed = closed_drafts(store)
    failed = {(e['run'], e['account'], e['id']) for e in entries if e['verb'] == 'create-draft' and e['outcome'] == 'failed'}
    for entry in entries:
        key = (entry['account'], entry['id'])
        if (entry['run'] != token and entry['verb'] == 'create-draft' and entry['outcome'] == 'issued' and key not in known
                and key not in closed and (entry['run'], *key) not in failed):
            drafts.append(dict(account=key[0], id=key[1], draft_id=None, rule='uncertain-draft',
                               created=datetime.fromtimestamp(entry['time'], timezone.utc).isoformat()))
            known.add(key)
    for draft in drafts:
        if any(e['run'] != token and e['verb'] == 'send-draft' and e['outcome'] == 'issued'
               and (e['account'], e['id']) == (draft['account'], draft['id'])
               and e['time'] >= datetime.fromisoformat(draft['created']).timestamp() for e in entries):
            draft.pop('digest', None)
    if drafts: previous['_drafts'] = drafts
    if plan is None:
        return previous
    records = {(d['account'], d['id']): d for d in previous.get('_drafts', [])}
    for action, result in zip(plan['actions'], observed):
        if action['action'] == 'create-draft' and result['status'] in {'executed', 'issued_unknown'}:
            existing = records.get((action['account'], action['id']), {})
            issued = existing.get('issued')
            status = existing.get('status')
            records[action['account'], action['id']] = dict(account=action['account'], id=action['id'], draft_id=result.get('draft_id'), rule=action['rule'], created=result.get('created', datetime.fromtimestamp(read_lock(store).get('started', read_lock(store)['created']), timezone.utc).isoformat()))
            if status:
                records[action['account'], action['id']]['status'] = status
            if issued:
                records[action['account'], action['id']]['issued'] = issued
            elif 'digest' in result:
                records[action['account'], action['id']]['digest'] = result['digest']
    if not plan['reasons']:
        for key in plan.get('hand_sent', {}).get('closed', []):
            if tuple(key) in records:
                records[tuple(key)]['status'] = 'hand-sent' if key in plan.get('hand_sent', {}).get('filed', []) else 'gone'
                records[tuple(key)].pop('digest', None)
    for action, result in zip(plan['actions'], observed):
        if action['action'] == 'send-draft':
            key = action['account'], action['id']
            if result['status'] == 'executed':
                if key in records:
                    records[key]['status'] = 'sent'
                    records[key].pop('digest', None)
            elif key in records:
                records[key]['status'] = ('refused' if result['status'] == 'failed' and not any(e['run'] == token and e['verb'] == 'send-draft' and e['outcome'] == 'issued' and (e['account'], e['id']) == key for e in entries) else 'unknown')
                records[key].pop('digest', None)
    records = list(records.values())
    if listing is not None:
        present = {(m['account'], m['id']) for m in listing['messages']}
        full_draft_accounts = set(listing['accounts']) - set(listing.get('quiet', [])) - set(listing.get('skipped', {}))
        records = [d for d in records if d['account'] not in full_draft_accounts or (d['account'], d['id']) in present]
    if records or '_drafts' in previous:
        previous['_drafts'] = records
    return previous


def remaining_items(plan, listing, results=None, *, final=False):
    decisions = {(i['account'], i['id']): i for i in plan['decisions']}
    removed = {(i['account'], i['id']) for i in (plan['actions'] if results is None else results)
               if i['action'] in REMOVALS and (results is None or i['status'] == 'executed')}
    statuses = {(i['account'], i['id']): (('Draft waiting since ' + i.get('created', '') if i['action'] == 'create-draft' else 'Unsubscribe leaves message in Inbox') if i['status'] == 'executed' else i['status'])
                for i in results or [] if i['action'] != 'create-folder'}
    for entry in results or []:
        if entry['action'] != 'create-folder' and entry.get('reason'):
            statuses[entry['account'], entry['id']] += ': ' + entry['reason']
    items = []
    for m in listing['messages']:
        key = (m['account'], m['id'])
        if key in removed and not final:
            continue
        item = dict(account=m['account'], id=m['id'], sender=m['sender'], subject=m['subject'])
        item.update(decisions.get(key, {'reason': 'Returned to Inbox after removal' if final and key in removed else statuses.get(key, 'New arrival during run' if final else 'unexecuted' if results is not None else 'Unsubscribe leaves message in Inbox')}))
        prior = next((i for i in plan.get('remaining', []) if (i['account'], i['id']) == key), {})
        if prior.get('reason', '').startswith(('Draft waiting since ', 'A send was issued;', 'A forward was issued;')):
            item['reason'] = prior['reason']
        if statuses.get(key) == 'issued_unknown' and any(a['action'] == 'create-draft' and (a['account'], a['id']) == key for a in plan['actions']):
            item['reason'] = 'check Drafts by hand'
        items.append(item)
    return items


REPLY_LIMIT = 2000


def read_run_counter(value):
    return value if type(value) is int and 0 <= value <= 2 ** 53 else None


def read_numbers(store, data=None):
    path = store / 'numbers.json'
    if data is None:
        data = parse_json(read_text(path), path) if path.exists() else {'next': 1, 'items': []}
    known_keys(data, {'next', 'items', 'committed_run', 'answers', '_run_counter', 'ungrouped_run'}, 'numbers.json')
    if '_run_counter' in data and read_run_counter(data['_run_counter']) is None:
        raise ValueError('numbers.json._run_counter: expected non-negative integer')
    if type(data.get('next')) is not int or data['next'] < 1 or not isinstance(data.get('items'), list):
        raise ValueError('numbers.json: expected next positive integer and items array')
    if 'committed_run' in data and not isinstance(data['committed_run'], str):
        raise ValueError('numbers.json.committed_run: expected run token string')
    seen, keys = set(), set()
    for item in data['items']:
        known_keys(item, {'number', 'account', 'id', 'members', 'presentation', 'run_token'}, 'numbers.json.items')
        if 'run_token' in item and not isinstance(item['run_token'], str):
            raise ValueError('numbers.json.items.run_token: expected run token string')
        if 'presentation' in item:
            if not isinstance(item['presentation'], list) or not item['presentation']:
                raise ValueError('numbers.json.items.presentation: expected nonempty array')
            if any(not isinstance(o, dict) or not isinstance(o.get('item'), dict)
                   or not isinstance(o.get('copies'), list) or not o['copies']
                   or any(not isinstance(m, dict) for m in o['copies'])
                   or not isinstance(o.get('tag'), str) or o.get('section') != 'inbox'
                   or o.get('visible') is not True or o.get('hidden') is not False or o.get('remaining') is not True
                   or o.get('owner') is not True or type(o.get('saved_number')) is not int
                   or o.get('saved_number') != item['number']
                   or type(o['item'].get('number')) is not int or o['item']['number'] != item['number']
                   or o['item'] != o['copies'][0]
                   for o in item['presentation']):
                raise ValueError('numbers.json.items.presentation: expected saved owner line outcomes')
            for outcome in item['presentation']:
                known_keys(outcome, {'item', 'kind', 'actions', 'failure', 'visible', 'hidden', 'remaining',
                                     'section', 'tag', 'owner', 'copies', 'copy_sections', 'other_accounts',
                                     'account_name', 'saved_number', 'ungrouped'}, 'numbers.json.items.presentation')
            rendered = [(m.get('account'), m.get('id')) for o in item['presentation']
                        for m in o.get('copies', [o.get('item', {})])]
            if rendered != [(m['account'], m['id']) for m in item.get('members', [])]:
                raise ValueError('numbers.json.items.presentation: expected exact saved members')
        if 'members' in item:
            if not isinstance(item['members'], list) or not item['members']:
                raise ValueError('numbers.json.items.members: expected nonempty array')
            member_keys = set()
            for member in item['members']:
                known_keys(member, {'account', 'id'}, 'numbers.json.items.members')
                if any(not isinstance(member.get(k), str) for k in ('account', 'id')):
                    raise ValueError('numbers.json.items.members: expected account/id strings')
                key = (member['account'], member['id'])
                if key in member_keys:
                    raise ValueError('numbers.json.items.members: duplicate message')
                member_keys.add(key)
            if item['members'][0] != dict(account=item.get('account'), id=item.get('id')):
                raise ValueError('numbers.json.items.members: expected representative first')
        n = item.get('number')
        if (type(n) is not int or not 0 < n < data['next'] or n in seen
                or any(not isinstance(item.get(k), str) for k in ('account', 'id'))):
            raise ValueError('numbers.json: expected unique saved numbers and account/id strings')
        members = {(m['account'], m['id']) for m in item.get('members', [item])}
        if members & keys:
            raise ValueError('numbers.json: duplicate message')
        seen.add(n)
        keys.update(members)
    if 'answers' in data:
        read_answers(store, data)
    return data


def answer_records(answers):
    return [dict(account=a, id=i, verb=v) for (a, i), v in sorted(answers.items())]


def read_answers(store, data=None, *, recover=False):
    data = read_numbers(store) if data is None else data
    if 'answers' in data:
        records = data['answers']
        if not isinstance(records, list):
            raise ValueError('numbers.json.answers: expected an array')
        answers = {}
        for item in records:
            if (not isinstance(item, dict) or set(item) != {'account', 'id', 'verb'}
                    or not all(isinstance(item[k], str) for k in item)
                    or item['verb'] not in {'leave', 'yes', 'no', 'unknown'}
                    or (item['account'], item['id']) in answers):
                raise ValueError('numbers.json.answers: expected unique account/id owner answers')
            answers[item['account'], item['id']] = item['verb']
        if not recover:
            return answers
    else:
        answers = {}
    # Take over published-store answers, or recover batches newer than numbers.
    log = store / 'decisions.jsonl'
    for line in read_text(log).splitlines() if log.exists() else []:
        batch = decision_batch(line)
        if 'answers' in data and (read_run_counter(batch.get('_run_counter')) or 0) <= (read_run_counter(data.get('_run_counter')) or 0):
            continue
        for part in batch.get('parts', []):
            if part['verb'] in {'leave', 'yes', 'no'} and part['outcome'] == 'answered':
                answers[part['account'], part['id']] = part['verb']
    if 'answers' not in data:
        for item in data['items']:
            for member in item.get('members', [item]):
                answers.setdefault((member['account'], member['id']), 'unknown')
    return answers


def number_decisions(store, plan, listing, *, read_only=False, language='en', outcome_plan=None, observed=None, final_listing=None, owner_answers=None, run_counter=None, draft_records=None):
    import execute
    data = read_numbers(store)
    # The answer note is display-only; the saved number keeps no note text.
    notes = {(a['account'], a['id']): a['note'] for a in (plan.get('answers') or {}).get('answers', [])
             if isinstance(a, dict) and isinstance(a.get('note'), str)}
    state_path = store / 'state.json'
    previous = parse_json(read_text(state_path), state_path) if state_path.exists() else {}
    log = store / 'decisions.jsonl'
    counters = [(read_run_counter(decision_batch(line).get('_run_counter')) or 0) for line in read_text(log).splitlines() if decision_batch(line).get('run') != plan.get('run_token')] if log.exists() else []
    witnesses = [previous.get('_next_number')]
    if log.exists():
        witnesses.extend(decision_batch(line).get('_next_number') for line in read_text(log).splitlines())
    data['next'] = max([data['next'], *(v for v in witnesses if read_run_counter(v) is not None and v >= 1)])
    stale = max([(read_run_counter(previous.get('_run_counter')) or 0), *counters]) > (read_run_counter(data.get('_run_counter')) or 0)
    if stale:
        if read_only:
            plan['ungrouped_run'] = True
        data['items'] = []
        plan = dict(plan, relist_inbox='all')
        for item in plan.get('remaining', plan['decisions']):
            item.pop('previously_listed', None)
        data['ungrouped_run'] = plan.get('run_token', '')
    protected = {(i['account'], i['id']) for i in plan['decisions'] + plan.get('remaining', []) if i['reason'] == 'Protected by never-touch'}
    present = {(i['account'], i['id']) for i in plan.get('remaining', plan['decisions']) + plan.get('reply', [])} - protected
    data['items'] = [i for i in data['items'] if i['account'] not in listing['accounts'] or (i['account'], i['id']) in present]
    lookup = {(m['account'], m['id']): i['number'] for i in data['items'] for m in i.get('members', [i])}
    answers = read_answers(store, recover=True) if owner_answers is None else owner_answers
    data['answers'] = answer_records(answers)
    answers = {key: (('unknown', *key) if verb == 'unknown' else verb) for key, verb in answers.items()}
    for item in plan.get('remaining', plan['decisions']):
        if item['reason'] == 'Protected by never-touch':
            continue
        key = (item['account'], item['id'])
        if plan.get('approval_state') == 'awaiting approval' and any((a['account'], a['id']) == key for a in plan['actions']):
            continue
        if answers.get(key) in ('leave', 'yes', 'no'):
            reason = item['reason']
            item['reason'] = 'Kept by owner reply' if reason == 'No matching rule' else reason if 'Kept by owner reply' in reason else reason + '; Kept by owner reply'
        if not read_only or key in lookup:
            item.setdefault('previously_listed', key in lookup)
        if read_only:
            continue
        if key not in lookup and item['account'] in listing.get('skipped', {}) and key not in {(d['account'], d['id']) for d in plan['decisions']}:
            continue
        item['number'] = lookup.get(key, 0)
    if read_only and run_counter is not None:
        saved = read_numbers(store)
        saved['_run_counter'] = run_counter
        saved['next'] = data['next']
        if stale:
            saved['items'] = []
            saved['ungrouped_run'] = plan['run_token']
        execute.atomic(store / 'numbers.json', saved)
    if not read_only:
        preview = dict(plan, dry_run=True) if plan.get('approval_state') == 'awaiting approval' else plan
        outcomes = message_outcomes(dict(outcome_plan or preview, relist_inbox='all') if stale else outcome_plan or preview, listing, plan.get('remaining', plan['decisions']), observed if outcome_plan is not None else None if preview.get('dry_run') else [], language, final_listing=final_listing, owner_answers=answers, ungrouped=stale, store=store, draft_records=draft_records)
        reply_numbers = {e['number'] for e in plan.get('reply', [])}
        inbox_keys = {(m['account'], m['id']) for o in outcomes if o['section'] == 'inbox' for m in o.get('copies', [o['item']])}
        failed = {(e['account'], e['id']) for e in observed or [] if e['status'] != 'executed'}
        hidden_keys = {(m['account'], m['id']) for o in outcomes if o['hidden'] and not o['visible']
                       for m in o.get('copies', [o['item']])}
        saved_items = [i for i in data['items'] if all(m['account'] not in listing['accounts'] or (m['account'], m['id']) in hidden_keys
                      for m in i.get('members', [i])) or (
            i['number'] in reply_numbers and (outcome_plan is None or (
                any((m['account'], m['id']) in inbox_keys for m in i.get('members', [i]))
                and not any((m['account'], m['id']) in failed for m in i.get('members', [i])))))]
        saved_items = [i for i in saved_items if not protected & {
            (m['account'], m['id']) for m in i.get('members', [i])}]
        saved_items = [i for i in saved_items if len({
            answers.get((m['account'], m['id'])) for m in i.get('members', [i])}) == 1]
        groups = [g for account in plan['counts']
                  for g in presentation_groups([o for o in outcomes if o['item']['account'] == account], owner_answers=answers) if g[0]['owner']]
        memberships = [[dict(account=m['account'], id=m['id']) for o in g for m in o.get('copies', [o['item']])] for g in groups]
        # Retire overlapping old numbers whole; new groups contain listed members only.
        current = {(m['account'], m['id']) for members in memberships for m in members}
        saved_items = [i for i in saved_items if not current & {
            (m['account'], m['id']) for m in i.get('members', [i])}]
        saved = [next((i for i in data['items'] if i.get('members', [dict(account=i['account'], id=i['id'])]) == members), None) for members in memberships]
        sequence = [i['number'] if i else None for i in saved]
        reuse = bool(sequence) and sequence[0] is not None and sequence == list(range(sequence[0], sequence[0] + len(sequence)))
        assigned = {}
        for index, (group, members) in enumerate(zip(groups, memberships)):
            number = sequence[index] if reuse else data['next']
            if not reuse: data['next'] += 1
            if number not in {i['number'] for i in saved_items}:
                saved_items.append(dict(number=number, **members[0], members=members))
            for outcome in group:
                outcome['item']['number'] = number
            record = next(i for i in saved_items if i['number'] == number)
            record['presentation'] = json.loads(json.dumps(group))
            for saved_outcome in record['presentation']:
                key = saved_outcome['item']['account'], saved_outcome['item']['id']
                note = safe_text(notes[key]) if key in notes else ''
                for copy in [saved_outcome['item'], *saved_outcome['copies']]:
                    if (copy['account'], copy['id']) == key:
                        copy.pop('note', None)
                        if note:
                            copy['reason'] = copy['reason'].replace(': ' + note, '').replace('; ' + note, '').strip()
                if note:
                    saved_outcome['tag'] = saved_outcome['tag'].replace(': ' + note, '').replace('; ' + note, '').strip()
            for saved_outcome in record['presentation']:
                saved_outcome['saved_number'] = number
            if 'run_token' in plan:
                record['run_token'] = plan['run_token']
            for outcome in group:
                outcome['saved_number'] = number
            assigned.update({(m['account'], m['id']): number for m in members})
        for item in plan.get('remaining', []) + plan['decisions']:
            key = item['account'], item['id']
            if key in assigned: item['number'] = assigned[key]
        data['items'] = saved_items
        if run_counter is not None:
            data['_run_counter'] = run_counter
        if outcome_plan is not None:
            data['committed_run'] = plan['run_token']
        if outcome_plan is not None or 'run_token' not in plan:
            execute.atomic(store / 'numbers.json', data)
        return outcomes


def parse_reply(text):
    if re.search(r'\b(calendar|event|invitation|accept|decline)\b', text, re.I):
        raise ValueError('The owner answers invitations in the calendar itself.')
    # Bounded linear scans; no backtracking, unbounded integer conversion or clock.
    if not text.strip() or len(text) > REPLY_LIMIT:
        raise ValueError('Reply must contain 1 to 2000 characters')
    if any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} and c != '\n' for c in text):
        raise ValueError('Reply contains control characters')
    parts, seen = [], set()
    for raw in text.replace(';', ',').replace('\n', ',').split(','):
        words = raw.strip().split(maxsplit=1)
        if len(words) != 2 or not words[0].isascii() or not words[0].isdigit() or len(words[0]) > 9:
            raise ValueError('Expected number and trash, archive, spam, move to <folder>, leave, yes or no')
        number = int(words[0])
        if number in seen:
            raise ValueError('Same number appears twice')
        seen.add(number)
        command = words[1].strip()
        verb, target = command.casefold(), None
        if verb == 'send-draft':
            raise ValueError('Unknown reply verb or malformed part')
        if verb in {'send', 'enviar'}:
            verb = 'send-draft'
        fields = command.split()
        if fields[0].casefold() in {'forward', 'reenviar'}:
            if len(fields) != 2 or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', fields[1]):
                raise ValueError('Expected forward HANDLE for one configured account')
            verb, target = 'forward', fields[1]
        if verb.split()[0] in {'resend', 'transmit', 'submit', 'deliver', 'dispatch', 'reply', 'email', 'mail'}:
            raise ValueError('The owner sends drafts by hand from the mailbox.')
        fields = command.split(maxsplit=2)
        if len(fields) == 3 and fields[0].casefold() == 'move' and fields[1].casefold() == 'to':
            verb, target = 'move', folder_path(fields[2].strip())
        elif verb not in {'trash', 'archive', 'spam', 'leave', 'yes', 'no', 'send-draft', 'forward'}:
            raise ValueError('Unknown reply verb or malformed part')
        parts.append(dict(number=number, verb=verb, target=target))
    return parts


def reply_plan(store, listing, holds, config, previous, parts, stops=(), *, numbers=None, answers=None, run_token=None, questions=None, asked=None):
    # Reuse listing validation, never-touch precedence, ceilings and brakes.
    source = read_numbers(store, numbers)
    selected = {p['number'] for p in parts}
    source = dict(next=source['next'], items=sorted([i for i in source['items'] if i['number'] in selected], key=lambda i: i['number']))
    numbers = {i['number']: i for i in source['items']}
    messages = {(m['account'], m['id']): m for m in listing['messages']}
    sending = any(p['verb'] in {'send-draft', 'forward'} for p in parts)
    if sending and config.get('allow_send') is not True:
        raise ValueError('config.allow_send: sending is disabled; send it by hand')
    selections, entries = {}, []
    for part in parts:
        item = numbers.get(part['number'])
        if item is None:
            raise ValueError('Unknown number')
        members = item.get('members', [dict(account=item['account'], id=item['id'])])
        if part['verb'] == 'forward' and len(members) != 1:
            raise ValueError('Forward requires exactly one message')
        for member in members:
            key = (member['account'], member['id'])
            if key not in messages:
                if part['verb'] in {'send-draft', 'forward'}:
                    raise ValueError('Send group member unavailable; send it by hand')
                if len(members) == 1:
                    raise ValueError('Number is not in the fresh Inbox for its saved account')
                continue
            message = dict(messages[key], sender=normalize_sender(messages[key]['sender']))
            if message['sender'] is None or any(matches(message, h, protection=True) for h in holds if 'judgment' not in h):
                raise ValueError('Number is protected by never-touch or has an invalid sender')
            entry = dict(part, account=key[0], id=key[1])
            entries.append(entry)
            if part['verb'] in {'leave', 'yes', 'no'}:
                continue
            selections[key] = dict(id='numbered-reply', accounts=[key[0]], sender=message['sender'], action=part['verb'], **({'target': part['target']} if part['target'] is not None else {}))
            if part['verb'] in {'send-draft', 'forward'}:
                issued = next((d for d in previous.get('_drafts', []) if d.get('issued') and (d['account'], d['id']) == key), None)
                if issued:
                    forward = issued['rule'] == 'numbered-forward'
                    if issued.get('status') == 'refused':
                        refusal = draft_note(issued, config.get('language', 'en'))
                    elif issued.get('status') == 'sent':
                        refusal = ('The message was forwarded' if forward else 'The draft was sent') if config.get('language', 'en') == 'en' else ('El mensaje fue reenviado' if forward else 'El borrador fue enviado')
                    elif config.get('language', 'en') == 'en':
                        refusal = ('A forward' if forward else 'A send') + ' was issued; the result is unknown; check Sent and Drafts by hand'
                    else:
                        refusal = ('Se emitió un reenvío' if forward else 'Se emitió un envío') + '; el resultado es desconocido; comprueba Sent y Drafts a mano'
                    raise ValueError(refusal)
            if part['verb'] == 'forward':
                target = part['target']
                fields = config['accounts'][key[0]]
                if target == key[0] or target not in config['accounts']:
                    raise ValueError('Forward requires another configured account')
                address = config['accounts'][target].get('address')
                if not isinstance(address, str) or len(address) > 254 or normalize_sender(address) != address.casefold() or any(unicodedata.category(c).startswith('C') for c in address):
                    raise ValueError('Forward target requires accounts.HANDLE.address as one bare address')
                if fields.get('provider') != 'outlook' or not fields.get('executor_app') or not fields.get('executor_account'):
                    raise ValueError('Forward requires the configured mailbox forward-draft operation; nothing sent')
                if any((d['account'], d['id']) == key for d in previous.get('_drafts', [])):
                    raise ValueError('A recorded draft already exists; send it by hand')
                selections[key] = dict(id='numbered-forward', accounts=[key[0]], sender=message['sender'], action='draft', draft='forward', draft_text='', draft_to=[address])
            if part['verb'] == 'send-draft':
                records = [d for d in previous.get('_drafts', []) if (d['account'], d['id']) == key]
                fields = config['accounts'][key[0]]
                if len(records) != 1 or 'digest' not in records[0] or not fields.get('provider') or not fields.get('executor_app') or not fields.get('executor_account'):
                    raise ValueError('No sendable recorded draft; send it by hand')
                selections[key]['target'] = records[0]['draft_id']
                selections[key]['digest'] = records[0]['digest']
                rules = read_rules(read_text(store / 'rules.md'), store / 'rules.md', config['accounts'])
                rule = next((r for r in rules if r['id'] == records[0]['rule'] and r.get('action') == 'draft'), None)
                if rule is None:
                    raise ValueError('Recorded draft rule unavailable; send it by hand')
                if 'after_send' in rule:
                    selections[key]['after_send'] = rule['after_send']
                selections[key].update(draft=rule['draft'], draft_to=rule.get('draft_to', []) if rule['draft'] == 'forward' else [message['sender']])
    for key, selection in selections.items():
        if (selection['action'] == 'send-draft' or selection.get('id') == 'numbered-forward') and not saved_tool(store, key[0], 'send-draft'):
            raise ValueError('Save the existing-draft send tool first; nothing sent')
    if sum(p['action'] in {'send-draft', 'draft'} for p in selections.values()) > 5:
        raise ValueError('At most 5 sends per reply run; nothing sent')

    # One reply batch has the existing default per-rule limit of ten.
    ceiling_rule = dict(id='numbered-reply', accounts=list(config['accounts']), subject='reply', action='report')
    result = build_plan(listing, [ceiling_rule], holds, config, previous, stops, selections=selections, answers=answers, run_token=run_token, questions=questions, asked=asked)
    if questions:
        return result
    if sum(a['action'] == ('create-draft' if selections[(a['account'], a['id'])]['action'] == 'draft' else selections[(a['account'], a['id'])]['action']) for a in result['actions']) + sum((d['account'], d['id']) in selections and d['reason']=='Protected by never-touch' for d in result['decisions']) != len(selections) and not result['reasons']:
        raise ValueError('Reply action could not be planned')
    if sending and result.get('approval_state') == 'awaiting approval':
        if any(selection['action'] != 'send-draft' and selection.get('id') != 'numbered-forward' for selection in selections.values()):
            raise ValueError('Reply with the send alone and with the other actions separately' if config.get('language', 'en') == 'en' else 'Responde con el envío solo y con las otras acciones por separado')
        # Approval to send does not approve filing beyond the ordinary thresholds.
        omitted = any(a['action'] in REMOVALS for a in result['actions'])
        send_only = {k: {f:v for f,v in selection.items() if f != 'after_send'} for k, selection in selections.items()
                     if selection['action'] == 'send-draft' or selection.get('id') == 'numbered-forward'}
        result = build_plan(listing, [ceiling_rule], holds, config | dict(inbox_limit=max(config.get('inbox_limit', 100), len(listing['messages']) + 1)), previous, stops, selections=send_only, answers=answers, run_token=run_token)
        for action in result['actions']:
            if omitted and action['action'] == 'send-draft':
                action['reason'] = 'Other filing omitted under safety thresholds; file by hand'
    if result['reasons'] and result.get('approval_state') != 'awaiting approval':
        raise ValueError('; '.join(result['reasons']))
    for item in result['decisions']:
        if any((e['account'], e['id']) == (item['account'], item['id']) and e['verb'] in {'leave', 'yes', 'no'} for e in entries):
            item['reason'] = 'Kept by owner reply'
    result['remaining'] = remaining_items(result, listing)
    for item in result['remaining']:
        for record in (previous or {}).get('_drafts', []):
            if (item['account'], item['id']) == (record['account'], record['id']) and record.get('status') not in {'sent', 'gone', 'hand-sent'}:
                item['reason'] = draft_note(record, allow_send=config.get('allow_send', False))
    result['reply'] = entries
    result['reply_source'] = dict(numbers=source, parts=parts)
    return result


STRINGS = {
    'en': ('None', 'Stopped'),
    'es': ('Ninguno', 'Detenido'),
}


SECTION_LABELS = {
    'en': {'inbox': 'Still in Inbox (by account)', 'planned': 'Planned actions',
           'rules': 'New rules', 'handled': 'Handled automatically', 'notes': 'Run notes'},
    'es': {'inbox': 'Todavía en Recibidos (por cuenta)', 'planned': 'Acciones previstas',
           'rules': 'Reglas nuevas', 'handled': 'Gestionado automáticamente', 'notes': 'Notas de ejecución'},
}


def safe_text(value):
    # Mail text remains a single escaped Markdown text line, never raw HTML.
    value = ''.join('[control]' if unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} else c for c in str(value))
    # Linear-time link breaking, Unicode-aware: any dot before a letter, including full-width dots.
    value = re.sub(r'(?<=[^\W_])[.。．｡](?=[^\W\d_])', '[dot]', value)
    value = value.replace('://', '[colon]//').replace('@', '[at]')
    return re.sub(r'([\\`*_{}\[\]()<>!|#])', r'\\\1', ' '.join(value.split()))


def chat_text(value):
    return ' '.join(''.join(' ' if c in '\r\n' else '' if unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} else c for c in str(value)).split())


def code_path(value):
    # A delimiter longer than any embedded backtick run keeps literal paths inert.
    delimiter = '`' * (max((len(s) for s in re.findall(r'`+', value)), default=0) + 1)
    return delimiter + ' ' + value + ' ' + delimiter


def safe_unsubscribe_host(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').rstrip('.')
        valid = (parsed.scheme == 'https' and parsed.port in (None, 443)
                 and parsed.username is None and parsed.password is None
                 and re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', host)
                 and not host.endswith('.localhost')
                 and not re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', host.rsplit('.', 1)[-1], re.I)
                 and not any(ord(c) <= 32 or ord(c) >= 127 for c in url)
                 and '\\' not in url)
        if valid:
            try:
                ipaddress.ip_address(host)
                valid = False
            except ValueError:
                pass
    except ValueError:
        valid = False
    return host if valid else None


def account_name(plan, account):
    return chat_text(plan.get('account_names', {}).get(account, account))


def account_reason(plan, value):
    names = plan.get('account_names', {})
    return re.sub(r'(?<![\w.-])(' + '|'.join(re.escape(a) for a in names) + r')(?![\w.-])', lambda m: names[m[0]], chat_text(value)) if names else chat_text(value)


def reply_hint(items, language, listing=None, allow_send=False):
    numbered = [i for i in items if 'number' in i]
    if not numbered:
        return []
    n = str(numbered[0]['number'])
    folders = (listing or {}).get('folders', {}).get(numbered[0]['account'], [])
    target = None
    for folder in folders:
        name = folder.get('name', '')
        try:
            parts = parse_reply(n + ' move to ' + name)
        except ValueError:
            continue
        if (len(parts) == 1 and parts[0]['target'] == name
                and sum(f['name'].casefold() == name.casefold() for f in folders) == 1):
            target = name
            break
    examples = ', '.join(n + ' ' + action for action in (['trash', 'archive', 'spam'] + (['move to ' + target] if target else []) + ['leave', 'yes', 'no']))
    if allow_send:
        examples += ', ' + n + (' send, ' if language == 'en' else ' enviar, ') + n + (' forward HANDLE' if language == 'en' else ' reenviar HANDLE')
    return [('Reply with a number and action, for example: ' if language == 'en' else 'Responde con un número y una acción, por ejemplo: ') + examples]


def action_label(action, listing, language='en', planned=False):
    verb = 'draft' if action['action'] == 'create-draft' and language == 'en' else action['action']
    if action['action'] == 'send-draft':
        verb = ('send' if planned else 'sent') if language == 'en' else ('enviar' if planned else 'enviado')
    if language == 'es':
        verb = {'trash': 'eliminar', 'archive': 'archivar', 'move': 'mover', 'spam': 'marcar como spam', 'unsubscribe': 'dar de baja', 'create-draft': 'crear borrador'}.get(verb, verb)
    if action['action'] == 'move':
        target = action.get('target_path', action.get('target', ''))
        target = next((f['name'] for f in listing.get('folders', {}).get(action['account'], []) if f['id'] == target), target)
        verb += (' to ' if language == 'en' else ' a ') + chat_text(target)
    if action.get('note'):
        verb += ' (' + safe_text(action['note']) + ')'
    if action.get('reason'):
        reason = ('Owner sent the draft' if language == 'en' else 'El propietario envió el borrador') if action['reason'] == 'Owner sent the draft' else ('No usable unsubscribe link found' if language == 'en' else 'No se encontró un enlace de baja utilizable')
        if action['reason'] == 'Other filing omitted under safety thresholds; file by hand':
            reason = action['reason'] if language == 'en' else 'Otras acciones de archivo omitidas; archiva a mano'
        verb += ' (' + reason + ')'
    return (('would ' if language == 'en' else 'previsto: ') if planned else '') + verb


def presentation_listing(original, current):
    """Use current metadata for survivors, retaining original data only for removed mail."""
    keys = {(m['account'], m['id']) for m in current['messages']}
    return dict(current, messages=current['messages'] + [m for m in original['messages'] if (m['account'], m['id']) not in keys])


def draft_note(record, language='en', *, allow_send=False):
    """Describe the record left by this run, never a pre-send plan reason."""
    if not record or record.get('status') in {'sent', 'gone', 'hand-sent'}:
        return ''
    if record.get('status') == 'refused':
        return ('El borrador no se envió porque cambió o no se pudo leer; envíalo a mano' if language == 'es' else 'The draft was not sent because it changed or could not be read; send it by hand')
    if record.get('issued'):
        if language == 'es':
            return ('Se emitió un reenvío' if record['rule'] == 'numbered-forward' else 'Se emitió un envío') + '; el resultado es desconocido; comprueba Sent y Drafts a mano'
        return ('A forward' if record['rule'] == 'numbered-forward' else 'A send') + ' was issued; the result is unknown; check Sent and Drafts by hand'
    return 'Draft waiting since ' + record['created'] + ('; draft may exist; check Drafts by hand' if record['draft_id'] is None else '; send it by hand' if allow_send and 'digest' not in record else '')


def message_outcomes(plan, listing, items, observed=None, language='en', *, known_left=None, final_listing=None, excluded=(), owner_answers=None, ungrouped=False, store=None, draft_records=None):
    """Classify message presentation once for real and rehearsal messages."""
    if final_listing is not None:
        if final_listing:
            present = {(m['account'], m['id']) for m in final_listing['messages']}
            known_left = {(m['account'], m['id']) for m in listing['messages']
                          if m['account'] in final_listing['accounts'] and (m['account'], m['id']) not in present}
        else:
            known_left = {(a['account'], a['id']) for a, r in zip(plan['actions'], observed or [])
                          if a['action'] in REMOVALS and r['status'] == 'executed'}
    from execute import read
    drafts = draft_records if draft_records is not None else (recover_drafts(store, token=plan.get('run_token'), plan=plan, observed=observed or []).get('_drafts', []) if store is not None else [])
    answers = read_answers(store, recover=True) if store is not None and owner_answers is None else owner_answers or {}
    allow_send = parse_toml(read_text(store / 'config.toml'), store / 'config.toml').get('allow_send', False) if store is not None and (store / 'config.toml').is_file() else False
    marks = {(i['account'], i['id']): i for i in plan.get('remaining', [])}
    messages = {(m['account'], m['id']): m for m in listing['messages']}
    metadata = dict(messages)
    for message in (final_listing or {}).get('messages', []):
        key = (message['account'], message['id'])
        metadata[key] = metadata.get(key, {}) | message
    remaining = {(i['account'], i['id']): dict(i) for i in items}
    handled, unfinished = {}, {}
    for index, action in enumerate(plan['actions']):
        if action['action'] == 'create-folder': continue
        if observed is not None and (index >= len(observed) or observed[index]['status'] != 'executed'):
            status = observed[index]['status'] if index < len(observed) else 'unexecuted'
            unfinished.setdefault((action['account'], action['id']), []).append((action, status))
            continue
        handled.setdefault((action['account'], action['id']), []).append(action)
    decision_notes = {(d['account'], d['id']): d['note'] for d in plan['decisions'] if 'note' in d}
    outcomes = []
    for key in dict.fromkeys([*remaining, *handled, *unfinished]):
        if key in excluded:
            continue
        item = remaining[key] if key in remaining else dict(messages[key], reason='No matching rule')
        item.update({k: metadata.get(key, {}).get(k, '') for k in ('date', 'conversation_id', 'internet_message_id', 'spam', 'phishing')})
        kept = answers.get(key) in ('leave', 'yes', 'no') or 'Kept by owner reply' in marks.get(key, {}).get('reason', '')
        if item['reason'] == 'No matching rule' and kept:
            item['reason'] = 'Kept by owner reply'
        item['previously_listed'] = marks.get(key, {}).get('previously_listed', item.get('previously_listed', False))
        hidden = (plan.get('relist_inbox') == 'new_plus_count' and item['previously_listed']
                  and key not in decision_notes and not item['reason'].startswith(('A send was issued;', 'A forward was issued;')))
        kind = ('handled' if key in handled else 'protected' if item['reason'] == 'Protected by never-touch'
                else 'previously listed' if hidden else 'kept by owner reply' if kept
                else 'numbered for owner' if owner_items(plan, [item], numbered=observed is not None) else 'not answerable')
        inline = plan.get('dry_run') and kind == 'handled'
        visible = not hidden or kind == 'handled' or key in unfinished
        status_words = ({'failed': 'failed', 'unconfirmed': 'action not confirmed',
                         'unexecuted': 'not attempted', 'unsubscribe_not_attempted': 'not attempted',
                         'unsubscribe_not_executable': 'not executable', 'issued_unknown': 'result unknown',
                         'skipped': 'skipped'} if language == 'en' else
                        {'failed': 'falló', 'unconfirmed': 'acción no confirmada',
                         'unexecuted': 'no intentado', 'unsubscribe_not_attempted': 'no intentado',
                         'unsubscribe_not_executable': 'no ejecutable', 'issued_unknown': 'resultado desconocido',
                         'skipped': 'omitido'})
        record = next((d for d in drafts if (d['account'], d['id']) == key), None)
        note = draft_note(record, language, allow_send=allow_send)
        failures = [(a, status) for a, status in unfinished.get(key, [])
                    if not (record and record.get('issued') and a['action'] == 'send-draft')]
        failure = ', '.join(action_label(a, listing, language) + ' ' + status_words[status]
                            for a, status in failures)
        if any(a['action'] == 'send-draft' for a, _ in failures):
            failure += '; check by hand; send it by hand only after checking' if language == 'en' else '; comprobar a mano antes de enviar'
        if inline:
            item['reason'] = ', '.join(action_label(a, listing, language, planned=True) for a in handled[key])
            item.pop('number', None)
        elif key in handled and key in remaining:
            labels = {'create-draft': 'draft created', 'send-draft': 'sent', 'unsubscribe': 'unsubscribed'} if language == 'en' else {'create-draft': 'borrador creado', 'send-draft': 'enviado', 'unsubscribe': 'baja realizada'}
            done = ', '.join(labels.get(a['action'], item['reason']) for a in handled[key])
            if key in unfinished:
                item.pop('number', None)
                item['reason'] = done + '; ' + failure
            else:
                item['reason'] = done
        planned = [a for a in plan['actions'] if a['action'] != 'create-folder' and (a['account'], a['id']) == key]
        left = key not in remaining if known_left is None else key in known_left
        section = 'handled' if left and planned and not plan.get('dry_run') else 'inbox'
        if planned:
            visible, hidden = True, False
        reason = item['reason'].replace('unconfirmed', 'action not confirmed')
        # Draft/send reasons belong to planning; replace them from this run's records.
        reason = re.sub(r'^(?:Draft waiting since [^;]+(?:; draft may exist; check Drafts by hand|; send it by hand)?|A (?:send|forward) was issued; the result is unknown; check Sent and Drafts by hand)(?:; )?', '', reason)
        if record and record.get('status') == 'refused':
            reason = reason.removeprefix(draft_note(record)).removeprefix('; ')
        if not reason:
            reason = 'No matching rule'
        if item['account'] in listing.get('skipped', {}):
            reason = re.sub(r': (?:provider|pigeon) error \([^)]*\)', '', reason)
        if language == 'es':
            for source, target in {'A send was issued; the result is unknown; check Sent and Drafts by hand': 'Se emitió un envío; el resultado es desconocido; comprueba Sent y Drafts a mano', 'A forward was issued; the result is unknown; check Sent and Drafts by hand': 'Se emitió un reenvío; el resultado es desconocido; comprueba Sent y Drafts a mano', 'Protected by never-touch': 'Protegido por never-touch', 'Run stopped': 'Ejecución detenida', 'action not confirmed': 'acción no confirmada', 'rule refused by brake': 'regla rechazada por el freno', 'failed': 'falló', 'unexecuted': 'sin ejecutar', 'issued_unknown': 'resultado desconocido'}.items():
                reason = reason.replace(source, target)
        if section == 'handled':
            completed = handled.get(key, []) or planned
            tag = ', '.join(action_label(a, listing, language) for a in completed)
            if failure: tag += ' (' + failure + ')'
        else:
            tag = '' if reason == 'No matching rule' or (reason == ('Run stopped' if language == 'en' else 'Ejecución detenida') and not kept and any(r.startswith('Reply refused:') for r in plan['reasons'])) else reason
            if key in unfinished:
                done = ', '.join(action_label(a, listing, language) for a in handled.get(key, [])
                                 if not (record and record.get('issued') and a['action'] == 'create-draft'))
                tag = (reason + '; ' if reason.startswith(('Draft waiting since ', 'Protected by never-touch', 'Protegido por never-touch', 'A send was issued;', 'A forward was issued;', 'Se emitió')) else '') + (done + ('; ' if failure else '') if done else '') + failure
                item.pop('number', None)
            if not owner_items(plan, [item], numbered=True) and not inline and not (record and record.get('issued')):
                item.pop('number', None)
        if section == 'inbox' and kept and 'Kept by owner reply' not in tag:
            tag = (tag + '; ' if tag else '') + 'Kept by owner reply'
        if section == 'inbox' and note and (record.get('issued') or failure or item['reason'].startswith('Draft waiting since ')):
            # A newly created draft already has its action outcome on this line.
            if record.get('issued') or not any(a['action'] == 'create-draft' for a in handled.get(key, [])):
                tag = note + ('; ' + tag if tag and tag != note else '')
            if record.get('issued'):
                visible, hidden = True, False
        if any(d.get('reason') == 'Protected by never-touch' and (d['account'], d['id']) == key for d in plan['decisions']):
            protection = 'Protected by never-touch' if language == 'en' else 'Protegido por never-touch'
            if protection not in tag:
                tag += ('; ' if tag else '') + protection
        if key in decision_notes and decision_notes[key] not in tag:
            tag += ('; ' if tag else '') + safe_text(decision_notes[key])
        outcomes.append(dict(item=item, kind=kind, actions=handled.get(key, []), failure=failure,
                             visible=visible, hidden=hidden, remaining=section == 'inbox', section=section, tag=tag,
                             owner=section == 'inbox' and visible and (
                                 bool(owner_items(plan, [item])) and not any(a['action'] in REMOVALS for a in planned)
                                 and (plan.get('approval_state') != 'awaiting approval' or not planned)
                                 if plan.get('dry_run') else 'number' in item)))
    # Configuration order owns the printed copy; planning remains per account.
    order = {a: i for i, a in enumerate(plan.get('account_names', plan.get('counts', {})))}
    merged = []
    for outcome in sorted(outcomes, key=lambda o: order.get(o['item']['account'], len(order))):
        match = next((o for o in merged if not ungrouped and not any(m.get('spam') or m.get('phishing') for m in [*o['copies'], outcome['item']]) and (o['section'], o['tag'], o['owner'], o['item'].get('question')) == (outcome['section'], outcome['tag'], outcome['owner'], outcome['item'].get('question')) and all((owner_answers or {}).get((m['account'], m['id'])) == (owner_answers or {}).get((outcome['item']['account'], outcome['item']['id'])) and same_message(m, outcome['item']) for m in o['copies'])), None)
        if match is None:
            outcome['copies'] = [outcome['item']]
            outcome['copy_sections'] = [outcome['section']]
            merged.append(outcome)
        else:
            match['copies'].append(outcome['item'])
            match['copy_sections'].append(outcome['section'])
            name = account_name(plan, outcome['item']['account'])
            match.setdefault('other_accounts', []).append(name)
            match['account_name'] = account_name(plan, match['item']['account'])
    if ungrouped:
        for outcome in merged: outcome['ungrouped'] = True
    return merged


def same_message(first, second):
    if first['account'] == second['account']:
        return False
    left, right = first.get('internet_message_id'), second.get('internet_message_id')
    if (not normalize_sender(first['sender']) or normalize_sender(first['sender']) != normalize_sender(second['sender'])
            or first['subject'] != second['subject']):
        return False
    if left and right:
        return left == right
    try:
        times = [datetime.fromisoformat(m.get('date', '')) for m in (first, second)]
        if any(t.tzinfo is None for t in times): return False
        return abs((times[0] - times[1]).total_seconds()) <= 300
    except (ValueError, TypeError, OverflowError):
        return False


def presentation_groups(outcomes, *, owner_answers=None):
    """Partition outcomes without changing their classification or message numbers."""
    groups, keys = [], {}
    for outcome in outcomes:
        item = outcome['item']
        if 'saved_number' in outcome:
            key = ('saved', outcome['saved_number'])
            if key not in keys:
                keys[key] = len(groups)
                groups.append([])
            groups[keys[key]].append(outcome)
            continue
        if outcome.get('ungrouped') or item.get('spam') or item.get('phishing'):
            groups.append([outcome])
            continue
        partition = ((owner_answers or {}).get((item['account'], item['id'])), item['account'], outcome['section'], outcome['tag'], outcome['owner'],
                     'number' in item, outcome['visible'], outcome['hidden'], item.get('question'),
                     tuple((a['action'], a.get('rule'), a.get('target_path'), a.get('target')) for a in outcome.get('actions', [])))
        identities = [('sender', normalize_sender(item['sender'])), ('conversation', (normalize_sender(item['sender']), item.get('conversation_id')) if item.get('conversation_id') else None)]
        identities = [partition + (kind, value) for kind, value in identities if value]
        matches = sorted({keys[k] for k in identities if k in keys})
        index = matches[0] if matches else len(groups)
        if not matches: groups.append([])
        for other in matches[1:]:
            groups[index].extend(groups[other])
            groups[other] = []
            for key in keys:
                if keys[key] == other: keys[key] = index
        groups[index].append(outcome)
        for key in identities: keys[key] = index
    return [g for g in groups if g]


def group_text(group, language):
    item = group[0]['item']
    others = list(dict.fromkeys(n for o in group for n in o.get('other_accounts', [])))
    handled = group[0].get('section') == 'handled'
    names = ([group[0].get('account_name', item['account'])] + others) if handled else others
    suffix = (' · ' + (('in: ' if language == 'en' else 'en: ') if handled else ('also in: ' if language == 'en' else 'también en: ')) + ', '.join(map(chat_text, names))) if others else ''
    if len(group) == 1:
        return chat_text(item['sender'])[:160] + ' · ' + chat_text(item['subject'])[:240] + suffix
    def timestamp(outcome):
        try:
            value = datetime.fromisoformat(outcome['item'].get('date', ''))
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
        except (ValueError, OverflowError, OSError):
            return float('-inf')
    latest = max(group, key=timestamp)['item']
    named = [o for o in group if getaddresses([o['item']['sender']])[0][0]]
    voters = named or group
    counts = {}
    for outcome in voters:
        sender = getaddresses([outcome['item']['sender']])[0][0]
        counts[sender] = counts.get(sender, 0) + 1
    sender = max(voters, key=lambda o: (counts[getaddresses([o['item']['sender']])[0][0]], timestamp(o)))['item']['sender']
    return (chat_text(sender)[:160] + ' (' + str(len(group)) + ') · '
            + ('latest: ' if language == 'en' else 'más reciente: ') + chat_text(latest['subject'])[:240] + suffix)


def render_remaining(plan, listing, language='en', *, outcomes=None, store=None):
    lines, empty_accounts, shown = [], [], []
    if outcomes is None:
        outcomes = message_outcomes(plan, listing, plan.get('remaining', remaining_items(plan, listing)), None if plan.get('dry_run') else [], language, store=store)
    for account in plan['counts']:
        if account not in listing['accounts']: continue
        account_outcomes = [o for o in outcomes if o['item']['account'] == account and o['section'] == 'inbox']
        if plan.get('approval_state') == 'awaiting approval':
            account_outcomes = [dict(o, item={k:v for k,v in o['item'].items() if k != 'number'}) for o in account_outcomes]
        items = [o['item'] for o in account_outcomes]
        name = account_name(plan, account)
        if not items:
            copies = sum(1 for o in outcomes for m, section in zip(o.get('copies', []), o.get('copy_sections', [])) if m['account'] == account and section == 'inbox')
            if copies:
                lines.append(name + ': ' + str(copies))
                continue
            if account not in listing.get('skipped', {}):
                empty_accounts.append(name)
            continue
        count = sum(1 for o in outcomes for m, section in zip(o.get('copies', [o['item']]), o.get('copy_sections', [o['section']])) if m['account'] == account and section == 'inbox')
        lines.append(name + ': ' + str(count))
        hidden = 0
        for group in presentation_groups(account_outcomes):
            outcome = group[0]
            item = outcome['item']
            if outcome['hidden'] and not outcome['visible']: hidden += len(group)
            if not outcome['visible']: continue
            if plan.get('dry_run'):
                item = {k:v for k,v in item.items() if k != 'number'}
            shown.append(item)
            reason = outcome['tag']
            lines.append((str(item['number']) + '. ' if 'number' in item else '- ') +
                         group_text(group, language) +
                         (' (' + chat_text(reason) + ')' if reason else '') +
                         (' · ' + chat_text(item['question']) if 'question' in item else ''))
        if plan.get('relist_inbox') == 'new_plus_count':
            lines.append(str(hidden) + (' previously listed' if language == 'en' else ' ya enumerados'))
    if empty_accounts:
        lines.append(', '.join(empty_accounts) + (': empty' if language == 'en' else ': vacío'))
    if plan.get('approval_state') != 'awaiting approval':
        lines += reply_hint(shown, language, listing, bool(store is not None and state_config(store).get('allow_send', False)))
    return lines


def render_sends(plan, listing, results=None, language='en'):
    sent = [(a, None if results is None else results[i]) for i, a in enumerate(plan['actions'])
            if a['action'] == 'send-draft' and (plan.get('dry_run') or (results is not None and i < len(results) and results[i]['status'] == 'executed'))]
    if not sent:
        return []
    messages = {(m['account'], m['id']): m for m in listing['messages']}
    es = language == 'es'
    lines = ['', (('Simulación: enviaría' if es else 'Dry run: would send') if plan.get('dry_run') else ('Mensajes enviados' if es else 'Sent messages')), '']
    for action, result in sent:
        message = messages[action['account'], action['id']]
        lines.append(chat_text(account_name(plan, action['account'])) + ': ' + chat_text(message['sender'])[:160] + ' · ' + chat_text(message['subject'])[:240]
                     + ' · ' + ({'reply':'respuesta', 'forward':'reenvío'}[action['draft']] if es else action['draft']) + (' · Destinatarios: ' if es else ' · Recipients: ') + ', '.join(chat_text(a)[:254] for a in action['draft_to']))
        if action.get('reason') == 'Other filing omitted under safety thresholds; file by hand':
            lines.append('Otras acciones de archivo omitidas; archiva a mano' if es else action['reason'])
        if plan.get('dry_run'):
            filing = next((a['action'] for a in plan['actions'] if a['action'] in REMOVALS and (a['account'], a['id']) == (action['account'], action['id'])), None)
            if filing: lines.append('after_send: ' + filing + ' (only after confirmed send)')
    return lines


def render_drafts(plan, listing, results=None):
    actions = []
    for i, action in enumerate(plan['actions']):
        if action['action'] != 'create-draft':
            continue
        if action['rule'] == 'numbered-forward' and plan.get('reply_source'):
            sent = any(a['action'] == 'send-draft' and (a['account'], a['id']) == (action['account'], action['id'])
                       and results is not None and index < len(results) and results[index]['status'] == 'executed'
                       for index, a in enumerate(plan['actions']))
            if plan.get('dry_run') or sent:
                continue
        actions.append((i, action))
    if not actions:
        return []
    lines = ['', 'Drafts', '']
    for i, action in actions:
        status = 'would create' if plan.get('dry_run') else (results[i]['status'] if results is not None and i < len(results) else 'planned')
        lines.append(chat_text(account_name(plan, action['account']) + ': ' + action['draft']) + ' · ' + status)
        if action['draft_to']:
            lines.append('Recipients: ' + ', '.join(action['draft_to']))
        if status == 'issued_unknown':
            lines.append('check Drafts by hand')
        lines += action['draft_text'].splitlines()
    return lines


def calendar_section(data, config, now):
    if not config.get('calendars'):
        return []
    calendar_events(data, config)
    zone = ZoneInfo(config.get('timezone', 'UTC'))
    today = now.astimezone(zone).date()
    settings = config.get('calendar', {})
    start = datetime.combine(today, datetime.min.time(), zone).timestamp()
    end = datetime.combine(today + timedelta(days=settings.get('days', 3)), datetime.min.time(), zone).timestamp()
    es = config.get('language', 'en') == 'es'
    events, pending, unread = [], [], []
    for name in sorted(config['calendars']):
        if name not in data or data[name] == {'failed': True}:
            unread.append(chat_text(name) + (': no leído; no se comprobaron los solapamientos con este calendario' if es else ': not read; overlaps against it were not checked'))
            continue
        for event in data[name]:
            if event['cancelled'] or event['response'] == 'declined':
                continue
            if event['all_day']:
                a = datetime.combine(datetime.strptime(event['start'], '%Y-%m-%d').date(), datetime.min.time(), zone)
                b = datetime.combine(datetime.strptime(event['end'], '%Y-%m-%d').date(), datetime.min.time(), zone)
            else:
                a, b = (datetime.fromisoformat(event[k]).astimezone(zone) for k in ('start', 'end'))
            item = (a.timestamp(), name, event['id'], b.timestamp(), event, a, b)
            if event['response'] == 'pending':
                pending.append(item)
            if not event['all_day'] and a.timestamp() < end and b.timestamp() > start:
                events.append(item)
    def describe(item):
        _, name, ident, _, event, a, b = item
        return chat_text(name + ' · ' + ident + ' · ' + a.isoformat() + ' — ' + b.isoformat() + ' · ' + event.get('subject', ''))
    lines = ['', 'Calendario' if es else 'Calendar', '', 'Invitaciones pendientes de respuesta' if es else 'Invitations waiting for an answer']
    for item in sorted(pending):
        a, b = item[5:]
        marks = []
        clock = a.strftime('%H:%M')
        if not item[4]['all_day'] and 'work_start' in settings and (clock < settings['work_start'] or clock >= settings['work_end'] or (b.date() == a.date() and b.strftime('%H:%M') > settings['work_end'])):
            marks.append('fuera del horario laboral' if es else 'outside working hours')
        if not item[4]['all_day'] and 'quiet_start' in settings:
            qstart = a.replace(hour=int(settings['quiet_start'][:2]), minute=int(settings['quiet_start'][3:]), second=0, microsecond=0).timestamp()
            qend = a.replace(hour=int(settings['quiet_end'][:2]), minute=int(settings['quiet_end'][3:]), second=0, microsecond=0).timestamp()
            if a.timestamp() < qend and b.timestamp() > qstart:
                marks.append('dentro del horario de silencio' if es else 'inside quiet window')
        lines.append('- ' + describe(item) + (' · ' + ', '.join(marks) if marks else ''))
    lines.append('Solapamientos' if es else 'Overlaps')
    ordered = sorted(events)
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            if second[0] >= first[3]:
                break
            winner = next((x[1] for x in (first, second) if config['calendars'][x[1]].get('wins')), None)
            lines.append('- ' + describe(first) + ' / ' + describe(second) + (' · ' + chat_text(winner) + (' tiene prioridad' if es else ' stays') if winner else ''))
    lines += unread
    return lines


def render_report(plan, listing, language='en', store=None, *, new_question=False, outcomes=None, draft_records=None):
    empty, stopped = STRINGS[language]
    planned, new = (SECTION_LABELS[language][k] for k in ('planned', 'rules'))
    lookup = {(m['account'], m['id']): m for m in listing['messages']}
    lines = ['Inbox: ' + str(sum(plan['counts'].values())) + ((' message' if sum(plan['counts'].values()) == 1 else ' messages') if language == 'en' else (' mensaje' if sum(plan['counts'].values()) == 1 else ' mensajes')) + (' · ' if plan['counts'] else '') + ' · '.join(account_name(plan, a) + ' ' + str(n) for a, n in plan['counts'].items())]
    lines = plan.get('rule_diagnostics', []) + account_problems(plan, listing, store, language) + lines
    lines += [account_name(plan, a) + ': no new mail' for a in listing.get('quiet', [])]
    if plan.get('ignored_answers'):
        label = 'Answers ignored' if language == 'en' else 'Respuestas ignoradas'
        lines.append(label + ': ' + ', '.join(plan['ignored_answers']))
    if plan.get('answers_error'):
        lines.append(plan['answers_error'].capitalize())
    if plan.get('approval_state') != 'awaiting approval':
        lines += [stopped + ': ' + account_reason(plan, r) for r in plan['reasons']]
    if plan.get('dry_run'): lines += render_sends(plan, listing)
    lines += render_drafts(plan, listing)
    if outcomes is None:
        preview = dict(plan, dry_run=True) if plan.get('approval_state') == 'awaiting approval' else plan
        outcomes = message_outcomes(preview, listing, plan.get('remaining', remaining_items(plan, listing)), None if preview.get('dry_run') else [], language, store=store, draft_records=draft_records)
    if not any(o.get('saved_number') for o in outcomes if o.get('owner')):
        outcomes = [dict(o, item={k:v for k,v in o['item'].items() if k != 'number'}) for o in outcomes]
    if listing['accounts']: lines += ['', SECTION_LABELS[language]['inbox'], ''] + render_remaining(dict(plan, dry_run=True) if plan.get('approval_state') == 'awaiting approval' else plan, listing, language, outcomes=outcomes, store=store)
    lines += plan.get('calendar_section', [])
    completed = [o for o in outcomes or [] if o['section'] == 'handled'] if not plan['actions'] else []
    if completed:
        lines += ['', SECTION_LABELS[language]['handled']]
        for account in plan['counts']:
            groups = presentation_groups([o for o in completed if o['item']['account'] == account])
            if groups: lines.append(account_name(plan, account))
            for group in groups:
                lines.append('- ' + group[0]['tag'] + ': ' + group_text(group, language))
    hosts = {(a['account'], a['id']): safe_unsubscribe_host(a['url']) for a in plan['actions'] if 'url' in a}
    lines += ['', planned, '']
    for account in plan['counts']:
        actions = [a for a in plan['actions'] if a['account'] == account]
        if not actions: continue
        lines.append(account_name(plan, account))
        groups = {}
        for action in actions:
            m = lookup[action['account'], action['id']]
            key = (action['action'], action['rule'], normalize_sender(m['sender']), action.get('target_path'), action.get('target'), hosts.get((account, action['id'])), action.get('note'))
            groups.setdefault(key, []).append((action, m))
        for members in groups.values():
            action = members[0][0]
            text = group_text([dict(item=m) for _, m in members], language)
            lines.append('- ' + action_label(action, listing, language) + ': ' + text + (' · ' + chat_text(action['target_path']) if 'target_path' in action else '') + (' · ' + chat_text(hosts.get((account, action['id']), '')) if 'url' in action else ''))
    if not plan['actions']: lines.append(empty)
    lines += ['', new, '', empty, '']
    if plan.get('approval_state') == 'awaiting approval':
        lines.append(approval_question(plan, language))
    return '\n'.join(lines)


def account_problems(plan, listing, store, language):
    problems = {a: ('not listed this run', plan.get('skip_reasons', {}).get(a, 'pigeon error (listing answer)')) for a in plan.get('not_listed', [])}
    problems.update({a: ('after_send reading skipped', r) for a, r in plan.get('hand_sent', {}).get('unchecked', {}).items()})
    problems.update({a: ('final reading failed', r) for a, r in listing.get('skipped', {}).items() if a in plan['counts']})
    retried = set()
    for filename in ('listing-exchange.json', 'final-listing-exchange.json', 'quiet-gateway.json'):
        path = store / filename if store is not None else None
        if path is not None and plan.get('dry_run') and filename == 'listing-exchange.json':
            rehearsal = store / SIMULATION_DIR / filename
            if rehearsal.exists(): path = rehearsal
        if path is not None and path.exists():
            data = parse_json(read_text(path), path)
            if data.get('token') == read_lock(store)['token']:
                retried.update(a for a, n in data.get('attempts', {}).items() if n)
                retried.update(data.get('retried', []))
                if not plan.get('counts') and not plan.get('not_listed'):
                    problems.update({a: ('not listed this run', r) for a, r in data.get('failed', {}).items()})
                    problems.update({a: ('not listed this run', entry.get('reason', 'pigeon error (listing answer)')) for a, entry in data.get('observations', {}).items() if entry.get('outcome') == 'error'})
    lines = []
    for a, (status, reason) in problems.items():
        if language == 'es':
            status = 'no incluida en esta ejecución' if status == 'not listed this run' else 'lectura after_send omitida' if status == 'after_send reading skipped' else 'falló la lectura final'
        lines.append(account_name(plan, a) + ': ' + status + '; ' + reason + (('; retried once, still failing' if language == 'en' else '; reintentada una vez, sigue fallando') if a in retried else ''))
    for a in sorted(retried - problems.keys()):
        lines.append(account_name(plan, a) + (': retried once, succeeded' if language == 'en' else ': reintentada una vez, correcta'))
    return lines


def message_opening(store, plan=None, outcomes=(), *, failed=False):
    config = state_config(store)
    language = config.get('language', 'en')
    started = datetime.fromtimestamp(read_lock(store).get('started', read_lock(store)['created']), ZoneInfo(config.get('timezone', 'UTC')))
    plan = plan or dict(decisions=[], remaining=[], counts={}, not_listed=[])
    plan = dict(plan, account_names={a: f.get('name', a) for a, f in config['accounts'].items()})
    count = owner_count(outcomes)
    need = (str(count) + (' needs you' if count == 1 else ' need you') if count else 'nothing needs you') if language == 'en' else (str(count) + (' requiere tu decisión' if count == 1 else ' requieren tu decisión') if count else 'nada requiere tu decisión')
    if failed: need = 'run failed' if language == 'en' else 'ejecución fallida'
    opening = '📬 Triage · ' + started.strftime('%A, %Y-%m-%d, %H:%M %Z')
    return opening + ' · ' + need + (' · 0 handled · stopped' if language == 'en' else ' · 0 gestionados · ' + STRINGS[language][1].lower()) + '\n' + ''.join(line + '\n' for line in account_problems(plan, {}, store, language))


def threshold_text(plan, language):
    texts = []
    for threshold in plan.get('approval_thresholds', []):
        kind, count, limit = (threshold[k] for k in ('kind', 'count', 'limit'))
        rule = chat_text(threshold.get('rule', ''))
        if kind == 'inbox':
            accounts = ', '.join(account_name(plan, a) + ' ' + str(n) for a, n in plan['counts'].items())
            text = (f'{count} message{"s" if count != 1 else ""} in Inbox {"are" if count != 1 else "is"} over the limit of {limit}' if language == 'en' else f'{count} mensaje{"s" if count != 1 else ""} en Recibidos {"superan" if count != 1 else "supera"} el límite de {limit}') + ' (' + accounts + ')'
        elif kind == 'brake':
            account = account_name(plan, threshold['account'])
            total = plan['counts'][threshold['account']]
            text = (f'{account}, rule {rule}: {count} match{"es" if count != 1 else ""}; {total} message{"s" if total != 1 else ""} in Inbox; limit {limit}' if language == 'en' else f'{account}, regla {rule}: {count} coincidencia{"s" if count != 1 else ""}; {total} mensaje{"s" if total != 1 else ""} en Recibidos; límite {limit}')
        else:
            labels = ({'destructive': ('destructive action', 'destructive actions'),
                       'rule': ('action for rule ' + rule, 'actions for rule ' + rule),
                       'draft': ('draft', 'drafts'), 'unsubscribe': ('unsubscribe', 'unsubscribes')}
                      if language == 'en' else
                      {'destructive': ('acción destructiva', 'acciones destructivas'),
                       'rule': ('acción de la regla ' + rule, 'acciones de la regla ' + rule),
                       'draft': ('borrador', 'borradores'), 'unsubscribe': ('baja', 'bajas')})
            label = labels[kind][count != 1]
            text = (f'{count} {label} {"exceeds" if count == 1 else "exceed"} the limit of {limit}' if language == 'en' else f'{count} {label} {"supera" if count == 1 else "superan"} el límite de {limit}')
        if rule == 'numbered-reply':
            text = text.replace('for rule numbered-reply', 'from your reply').replace('rule numbered-reply', 'your reply').replace('de la regla numbered-reply', 'de tu respuesta').replace('regla numbered-reply', 'tu respuesta')
        texts.append(text)
    return '. '.join(texts)


def approval_question(plan, language='en'):
    counts = {}
    for action in plan['actions']:
        if action['action'] != 'create-folder':
            counts[action['action']] = counts.get(action['action'], 0) + 1
    kinds = ', '.join(str(n) + ' ' + (('move' if language == 'en' else 'mover') if kind == 'move' else action_label(dict(action=kind), {}, language)) for kind, n in sorted(counts.items()))
    proceed, skip = APPROVAL_WORDS[language]
    total = sum(counts.values())
    breakdown = ' (' + kinds + ')' if kinds else ''
    details = threshold_text(plan, language)
    dry = plan.get('dry_run')
    if language == 'en':
        question = f'⚠️ {details}. Planned: {total} action{"s" if total != 1 else ""}{breakdown}.'
        ending = (f'The choices would be "{proceed}" or "{skip}"; no reply is acted on.' if dry
                  else f'Reply "{proceed}" to run it, or "{skip}" to leave everything.')
        prefix = 'A real run would ask: ' if dry else ''
    else:
        question = f'⚠️ {details}. Previsto: {total} {"acciones" if total != 1 else "acción"}{breakdown}.'
        ending = (f'Las opciones serían "{proceed}" u "{skip}"; las respuestas no se aplican.' if dry
                  else f'Responde "{proceed}" para ejecutarlo, u "{skip}" para dejar todo igual.')
        prefix = 'Una ejecución real preguntaría: ' if dry else ''
    return prefix + question + ' ' + ending



def awaiting_message(store, plan, listing, language, *, new_question=False, outcomes=None):
    preview = dict(plan, dry_run=True)
    if outcomes is None:
        outcomes = message_outcomes(preview, listing, remaining_items(plan, listing), language=language, store=store)
    first = ' · '.join(message_opening(store, plan, outcomes).splitlines()[0].split(' · ')[:2])
    waiting = 'awaiting approval' if language == 'en' else 'en espera de aprobación'
    first += ' · ' + waiting
    if plan.get('dry_run'): first += ' · ' + ('dry run' if language == 'en' else 'simulación')
    created = time.time() if new_question else question_created(store, plan)
    expired = (not new_question and question_closed(store, plan)) or time.time() - created < 0 or time.time() - created > approval_hours(state_config(store)) * 3600
    if expired:
        empty = 'nothing needs you' if language == 'en' else 'nada requiere tu decisión'
        expired_label = 'approval question expired' if language == 'en' else 'pregunta de aprobación caducada'
        first = first.replace(empty + ' · ' + waiting, expired_label).replace(waiting, expired_label)
    lines = [first]
    if plan.get('dry_run'):
        lines += (['Dry run', 'Replies to this dry run are not acted on; no reply number is live.'] if language == 'en' else ['Simulación', 'Las respuestas a esta simulación no se aplican; ningún número está activo.'])
    lines += plan.get('rule_diagnostics', [])[1:] + account_problems(plan, listing, store, language)
    lines += [account_name(plan, a) + ': no new mail' for a in listing.get('quiet', [])]
    lookup = {(m['account'], m['id']): m for m in listing['messages']}
    primary = set()
    for outcome in outcomes:
        seen = set()
        for copy in outcome.get('copies', [outcome['item']]):
            for action in plan['actions']:
                if (action['account'], action['id']) != (copy['account'], copy['id']):
                    continue
                signature = (action['action'], action['rule'])
                if signature not in seen:
                    primary.add((action['account'], action['id'], *signature))
                    seen.add(signature)
    for account, count in plan['counts'].items():
        lines.append(account_name(plan, account) + ': ' + str(count) + ((' message' if count == 1 else ' messages') + ' in Inbox' if language == 'en' else (' mensaje' if count == 1 else ' mensajes') + ' en Recibidos'))
        groups = {}
        for action in plan['actions']:
            if action['account'] == account and action['action'] != 'create-folder' and (account, action['id'], action['action'], action['rule']) in primary:
                groups.setdefault((action['action'], action['rule'], action.get('note')), []).append(action)
        for (kind, rule, _), actions in groups.items():
            examples = list(dict.fromkeys(chat_text(lookup.get((account, a['id']), {}).get('sender', ''))[:160]
                                          for a in actions))[:3]
            examples = [e for e in examples if e]
            lines.append('- ' + action_label(actions[0], listing, language, planned=True) + ' · '
                         + (('your reply' if language == 'en' else 'tu respuesta') if rule == 'numbered-reply' else ('rule ' if language == 'en' else 'regla ') + chat_text(rule))
                         + ': ' + str(len(actions)) + (' · ' + ', '.join(examples) if examples else ''))
    lines += [('Approval question expired; the next run asks again.' if language == 'en' else 'La pregunta de aprobación caducó; la próxima ejecución volverá a preguntar.') if expired else approval_question(plan, language)]
    return '\n'.join(lines) + '\n'


APPROVAL_WORDS = {'en': ('proceed', 'skip'), 'es': ('proceder', 'omitir')}


def approval_reply(store, args):
    try:
        with Path(args.reply).open(encoding='utf-8') as stream:
            raw = stream.read(REPLY_LIMIT + 1)
            text = raw.strip().casefold() if len(raw) <= REPLY_LIMIT else ''
    except UnicodeError:
        text = ''
    if text.endswith(('.', '!')):
        text = text[:-1]
    config = state_config(store)
    validate_inputs({'accounts': {}, 'messages': []}, config, {})
    language = config.get('language', 'en')
    words = APPROVAL_WORDS['en'] + APPROVAL_WORDS['es']
    path = (store / 'plan.json') if args.dry_run else Path(args.plan_out or store / 'plan.json').resolve()
    with lock_gate(store):
        require_lock(store, args.token)
        require_fetched(store, args.token)
        plan = parse_json(read_text(path), path) if path.is_file() else {}
        waiting = plan.get('approval_state') == 'awaiting approval' and not plan.get('dry_run')
        if not waiting and text not in words:
            return False
        if args.dry_run:
            raise ValueError('dry-run reply: cannot answer a real approval question')
        if args.scheduled:
            raise ValueError('Replies require a user-initiated run')
        if not waiting:
            raise ValueError('Reply refused: no plan awaits approval')
        if text not in words:
            raise ValueError('Reply refused: answer proceed or skip (proceder or omitir); the plan is still waiting')
        if args.question != plan.get('run_id'):
            raise ValueError('Reply refused: question missing or replaced; relay the current question before answering')
        check_approval_age(store, plan)
        if text in ('proceed', 'proceder'):
            command = ['env', 'PIGEON_HOME=' + str(store), 'python3', 'scripts/execute.py', 'next', '--token', args.token,
                       '--plan', str(path), '--approve', plan['run_id']]
            print(shlex.join(command))
            return True
    skip_approval(store, args.token, path)
    print('Nothing changes.' if language == 'en' else 'Nada cambia.')
    return True


def question_created(store, plan):
    if plan.get('dry_run'):
        return plan['approval_created']
    history = store / 'history.log'
    stamps = [state_timestamp(line.split(' ')[0], history).timestamp()
              for line in (read_text(history).splitlines() if history.exists() else []) if line.split(' approval ', 1)[-1].split(' ')[:2] == ['asked:', str(plan.get('run_id'))]]
    if stamps:
        return min(stamps)
    raise ValueError('approval: expected recorded question time')


def question_closed(store, plan):
    history = store / 'history.log'
    return history.exists() and any(
        line.split(' approval ', 1)[1].split(' ')[:2] == [kind + ':', str(plan.get('run_id'))]
        for line in read_text(history).splitlines() if ' approval ' in line
        for kind in ('approved', 'skipped', 'expired', 'replaced', 'stale'))


def check_approval_age(store, plan):
    if question_closed(store, plan):
        raise ValueError('approval: question closed or expired; plan again')
    created = question_created(store, plan)
    if type(created) not in (float, int) or not math.isfinite(created):
        raise ValueError('approval: expected planning time')
    age = time.time() - created
    if age < 0 or age > approval_hours(state_config(store)) * 3600:
        history = store / 'history.log'
        marker = ' approval expired: ' + plan['run_id']
        if not history.exists() or not any(marker in line for line in read_text(history).splitlines()):
            approval_event(store, plan, 'expired')
        raise ValueError('approval: question expired; the next run asks again')


def approval_hours(config):
    hours = config.get('approval_hours', 12)
    if type(hours) is not int or not 1 <= hours <= 72:
        raise ValueError('config.approval_hours: expected an integer from 1 to 72')
    return hours


def question_event(plan):
    marker = 'asked: ' + str(plan.get('run_id'))
    if 'answers' in plan:
        marker += ' ' + hashlib.sha256(json.dumps(plan['answers'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return marker


def check_question_answers(store, plan):
    events = [line.split(' approval ', 1)[1] for line in read_text(store / 'history.log').splitlines() if ' approval ' in line]
    asked = [event for event in events if event.split(' ')[:2] == ['asked:', str(plan.get('run_id'))]]
    if asked and any(event != question_event(plan) for event in asked):
        raise ValueError('approval: answers changed since the question; plan again')


def approval_event(store, plan, event):
    line = datetime.now(timezone.utc).isoformat() + ' approval ' + event + ': ' + plan['run_id']
    if event == 'asked':
        line = line.rsplit(' approval ', 1)[0] + ' approval ' + question_event(plan)
    if event == 'approved':
        line += ' ' + hashlib.sha256(plan['run_token'].encode()).hexdigest()
    line += '\n'
    with (store / 'history.log').open('a', encoding='utf-8') as out:
        out.write(line)


def skip_approval(store, token, path):
    import execute
    with lock_gate(store):
        require_lock(store, token)
        require_fetched(store, token)
        path = Path(path).resolve()
        plan = execute.read(path)
        if plan.get('approval_state') != 'awaiting approval' or plan.get('dry_run'):
            raise ValueError('approval: expected a plan awaiting approval')
        execute.atomic(path, dict(plan, approval_state='skipped'))
        approval_event(store, plan, 'skipped')


def approve_plan(store, token, path, run_id, *, validate_only=False, reading=None):
    # This is the only route that may bind an existing plan to a new lock.
    import execute
    with lock_gate(store):
        require_lock(store, token)
        require_fetched(store, token)
        path = Path(path).resolve()
        plan = execute.read(path)
        if 'judgment_token' in plan or 'judgments' in plan:
            raise ValueError('this plan was made by an older version; plan again')
        if plan.get('dry_run') or plan.get('approval_state') != 'awaiting approval':
            raise ValueError('approval: expected a plan awaiting approval')
        if plan.get('run_id') != run_id:
            raise ValueError('approval: run id does not match awaiting plan')
        history_path = store / 'history.log'
        events = [line.split(' approval ', 1)[1] for line in read_text(history_path).splitlines() if ' approval ' in line] if history_path.exists() else []
        asked = [event.split(' ')[1] for event in events if event.startswith('asked: ')]
        if not asked or asked[-1] != run_id or any(event.split(' ')[:2] == [kind + ':', run_id] for event in events for kind in ('approved', 'skipped', 'expired', 'replaced', 'stale')):
            raise ValueError('approval: plan replaced or approval already used or expired; plan again')
        check_approval_age(store, plan)
        check_question_answers(store, plan)
        answers = plan.get('answers')
        if plan.get('answers_used') and isinstance(answers, dict) and answers.get('run') == plan.get('run_token') and plan['run_token'] != token:
            raise ValueError('approval: answers are from another run; plan again')
        if validate_only:
            return plan
        if reading is None:
            raise ValueError('approval: fresh Inbox reading required')
        validate_inputs(reading, state_config(store), {})
        accounts = {a['account'] for a in plan['actions']}
        if (set(reading['accounts']) != accounts or reading.get('skipped')
                or accounts - {m['account'] for m in reading['messages']}):
            config = state_config(store)
            language = config.get('language', 'en')
            state_path = store / 'approval-reading.json'
            exchange = execute.read(state_path) if state_path.exists() else {}
            details = []
            extra = (set(reading['accounts']) | set(reading.get('skipped', {}))) - accounts
            if extra:
                details.append(('accounts outside planned accounts: ' if language == 'en' else 'cuentas fuera de las cuentas previstas: ') + ', '.join(sorted(extra)))
            for account in config['accounts']:
                if account not in accounts: continue
                if account not in reading.get('skipped', {}) and any(m['account'] == account for m in reading['messages']):
                    continue
                detail = account_name(dict(account_names={a: f.get('name', a) for a, f in config['accounts'].items()}), account) + ': ' + reading.get('skipped', {}).get(account, 'pigeon error (empty reading)')
                if exchange.get('token') == token and exchange.get('attempts', {}).get(account):
                    detail += '; retried once, still failing' if language == 'en' else '; reintentada una vez, sigue fallando'
                details.append(detail)
            state_path.unlink(missing_ok=True)
            raise ValueError(('approval: fresh Inbox reading failed; ' + '; '.join(details) + '; nothing was done; question stays open. Reply "proceed" again before the question expires.' if language == 'en' else 'aprobación: falló la lectura nueva de Recibidos; ' + '; '.join(details) + '; no se hizo nada; la pregunta sigue abierta. Responde "proceder" de nuevo antes de que caduque.'))
        present = {(m['account'], m['id']) for m in reading['messages']}
        if any((a['account'], a['id']) not in present for a in plan['actions'] if a['action'] != 'create-folder'):
            approval_event(store, plan, 'stale')
            execute.atomic(path, dict(plan, run_token=token, approval_state='skipped'))
            (store / ('execution-' + hashlib.sha256(token.encode()).hexdigest() + '.json')).unlink(missing_ok=True)
            return dict(done=True, status='stale', message='Inbox changed since the question; the next run plans and asks again.')
        candidate = dict(plan, run_token=token, approval_state='approved',
                         approved_thresholds=plan['reasons'], reasons=[])
        checkpoint = store / ('execution-' + hashlib.sha256(token.encode()).hexdigest() + '.json')
        if checkpoint.exists():
            prior = execute.Execution(store, token, path)
            if prior.state['results'] or prior.state['pending'] is not None:
                raise ValueError('approval: execution already began; plan again')
        execute.atomic(path, candidate)
        if checkpoint.exists():
            checkpoint.unlink()
        approval_event(store, candidate, 'approved')


def require_plan_token(plan, token):
    if plan.get('run_token') != token:
        raise ValueError('plan.run_token: expected current run token; plan again')


def render_message(store, token, plan_path, results_path, *, now=None):
    import execute
    require_lock(store, token)
    try:
        text = _render_message(store, token, plan_path, results_path, now=now)
        candidate = execute.read(plan_path) if Path(plan_path).exists() else {}
        if read_numbers(store).get('ungrouped_run') == token or candidate.get('dry_run') and candidate.get('ungrouped_run') is True:
            language = state_config(store).get('language', 'en')
            lines = text.splitlines()
            lines.insert(1, 'Saved numbers were out of date; nothing is grouped in this run.' if language == 'en' else 'Los números guardados estaban desactualizados; no se agrupa nada en esta ejecución.')
            text = '\n'.join(lines) + '\n'
        return text
    except (ValueError, OSError, KeyError, TypeError, AttributeError, IndexError):
        try:
            failure = parse_json(read_text(store/'run-refusal.json'), 'run-refusal.json')
            if failure.get('token') == token:
                return message_opening(store, failed=True) + 'Run failed at ' + chat_text(failure['stage']) + ': ' + chat_text(failure['refusal']) + '\n'
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            pass
        return message_opening(store) + 'Run failed at message: pigeon error (run data refused)\n'


def owner_count(outcomes):
    return sum(g[0]['owner'] for g in presentation_groups(outcomes))


def owner_items(plan, items, *, numbered=False):
    protected = {(i['account'], i['id']) for i in plan['decisions'] if i['reason'] == 'Protected by never-touch'}
    return [i for i in items if (i['account'], i['id']) not in protected
            and i['reason'] != 'Protected by never-touch'
            and not (plan.get('relist_inbox') == 'new_plus_count' and i.get('previously_listed'))
            and (not numbered or ('number' in i and not re.match(r'^(?:failed|unexecuted|unconfirmed|issued_unknown|skipped)(?::|$)', i['reason'])))]


def _render_message(store, token, plan_path, results_path, *, now=None):
    import execute
    require_lock(store, token)
    failure_path = store / 'run-refusal.json'
    failure = execute.read(failure_path) if failure_path.exists() else {}
    failure_line = ('Run failed at ' + chat_text(failure.get('stage', 'plan')) + ': ' + chat_text(failure.get('refusal', 'pigeon error (plan unavailable)'))) if failure.get('token') == token else None
    candidate = execute.read(plan_path) if Path(plan_path).exists() else {}
    current_plan = candidate.get('run_token') == token
    if candidate.get('approval_state') == 'awaiting approval' and failure_line and 'question expired' in failure_line:
        return awaiting_message(store, candidate, execute.read(store / 'inbox.json'), state_config(store).get('language', 'en'))
    if Path(plan_path).exists() and not current_plan and not failure_line:
        return message_opening(store) + 'Run failed at plan: plan.run_token: expected current run token; plan again\n'
    checkpoint = store / ('execution-' + hashlib.sha256(token.encode()).hexdigest() + '.json')
    if not current_plan:
        refusal = failure.get('refusal', 'pigeon error (plan unavailable)') if failure.get('token') == token else 'pigeon error (plan unavailable)'
        stage = failure.get('stage', 'plan') if failure.get('token') == token else 'plan'
        return message_opening(store) + 'Run failed at ' + chat_text(stage) + ': ' + chat_text(refusal) + '\n'
    plan = candidate
    if plan.get('approval_state') == 'awaiting approval' and not plan.get('dry_run'):
        return awaiting_message(store, plan, execute.read(store / 'inbox.json'), state_config(store).get('language', 'en'))
    if not checkpoint.exists() and not plan.get('dry_run'):
        listing = execute.read(store / 'inbox.json')
        language = state_config(store).get('language', 'en')
        items = [{k:v for k,v in i.items() if k != 'number'} for i in remaining_items(plan, listing)]
        outcomes = message_outcomes(plan, listing, items, [], language, known_left=set(), store=store)
        reason = failure_line or 'Run failed at execution: pigeon error (results unavailable)'
        lines = render_remaining(plan, listing, language, outcomes=outcomes, store=store)
        return message_opening(store, plan, outcomes, failed=True) + reason + '\n' + '\n'.join(lines) + '\n'
    if plan.get('dry_run') is True:
        require_fetched(store, token)
        listing = plan['dry_listing'] if 'dry_listing' in plan else execute.read(store / 'inbox.json')
        config, rules, holds = load_safety([store / n for n in ('config.toml', 'rules.md', 'never-touch.toml', 'guardrails.md')])
        previous = execute.read(store / 'state.json') if (store / 'state.json').exists() else {}
        stops = ['Missing state; prior run records exist'] if not (store / 'state.json').exists() and not plan.get('first_run') else []
        if 'reply_source' in plan:
            source = plan['reply_source']
            expected = reply_plan(store, listing, holds, config, previous, source['parts'], stops, numbers=source['numbers'],
                                  answers=plan.get('answers'), run_token=token, asked=read_asked(store, token, dry_run=True))
        elif plan['reasons'] and plan['reasons'][0].startswith('Reply refused:'):
            expected = build_plan(listing, [], holds, config, previous, plan['reasons'])
        else:
            expected = build_plan(listing, rules, holds, config, previous, stops, answers=plan.get('answers'), run_token=token,
                                  asked=read_asked(store, token, dry_run=True), hand_sent=plan.get('hand_sent'), store=store)
        number_decisions(store, expected, listing, read_only=True)
        if any(plan.get(k) != expected[k] for k in expected):
            raise ValueError('dry-run plan: expected unchanged private inputs and listing')
        language = config.get('language', 'en')
        if plan.get('approval_state') == 'awaiting approval':
            return awaiting_message(store, plan, listing, language)
        labels = SECTION_LABELS[language]
        lines = ['Dry run', 'Replies to this dry run are not acted on; no reply number is live.'] if language == 'en' else ['Simulación', 'Las respuestas a esta simulación no se aplican; ningún número está activo.']
        outcomes = message_outcomes(plan, listing, remaining_items(plan, listing), language=language, ungrouped=plan.get('ungrouped_run') is True, store=store)
        total = len({(a['account'], a['id']) for a in plan['actions'] if a['action'] != 'create-folder'})
        summary = (str(total) + (' message' if total == 1 else ' messages') + ' would be handled · dry run') if language == 'en' else (str(total) + (' mensaje previsto' if total == 1 else ' mensajes previstos') + ' · simulación')
        if plan['reasons']:
            summary += ' · ' + STRINGS[language][1].lower()
        opening = ' · '.join(message_opening(store, plan, outcomes).splitlines()[0].split(' · ')[:3])
        lines = [opening + ' · ' + summary] + lines
        lines[1:1] = plan.get('rule_diagnostics', [])[1:] + account_problems(plan, listing, store, language)
        lines += [account_name(plan, a) + ': no new mail' for a in listing.get('quiet', [])]
        lines += [STRINGS[language][1] + ': ' + account_reason(plan, r) for r in plan['reasons']]
        lines += render_sends(plan, listing, language=language)
        lines += render_drafts(plan, listing)
        if listing['accounts']: lines += ['', labels['inbox']]
        lines += render_remaining(plan, listing, language, outcomes=outcomes, store=store)
        lines += plan.get('calendar_section', [])
        return '\n'.join(lines) + '\n'
    execution = execute.Execution(store, token, plan_path)
    results = execute.read(results_path or store / 'results.json')
    if results != {'results': execution.state['results']}:
        raise ValueError('message: results must match this token and plan checkpoint')
    plan = execution.plan
    listing = execution.current_listing(execute.read(store / 'inbox.json'))
    config = parse_toml(load_inputs([store / 'config.toml'])[0], store / 'config.toml')
    plan = dict(plan, account_names={a: f.get('name', a) for a, f in config['accounts'].items()})
    language = config.get('language', 'en')
    labels = SECTION_LABELS[language]
    observed = list(results['results'])
    pending = execution.state['pending']
    if pending is not None:
        observed.append(pending | {'status': 'issued_unknown'})
    remaining = execution.remaining_items(listing, observed)
    metadata = presentation_listing(execute.read(store / 'inbox.json'), listing)
    saved = read_numbers(store)
    committed = saved.get('committed_run') == token
    records = [i for i in saved['items']
               if committed and i.get('run_token') == token and 'presentation' in i]
    covered = {(m['account'], m['id']) for i in records for m in i['members']}
    for item in remaining:
        item.pop('number', None)
    outcomes = message_outcomes(plan, metadata, remaining, observed, language,
                                final_listing=execution.state.get('final_listing', {}), excluded=covered, ungrouped=saved.get('ungrouped_run') == token, store=store)
    for record in records:
        outcomes.extend(record['presentation'])
    # The saved number keeps no note text; the message shows it from this run's plan.
    answer_notes = {(d['account'], d['id']): d['note'] for d in plan['decisions'] if 'note' in d}
    reasons = {(d['account'], d['id']): d['reason'] for d in plan['decisions']}
    for outcome in outcomes:
        key = outcome['item']['account'], outcome['item']['id']
        note = safe_text(answer_notes[key]) if key in answer_notes else ''
        if not note or note in outcome['tag']:
            continue
        reason = reasons.get(key, '')
        prefix = reason[:-len(note) - 2] if reason.endswith(': ' + note) else outcome['item']['reason']
        if prefix and prefix in outcome['tag']:
            outcome['tag'] = outcome['tag'].replace(prefix, prefix + ': ' + note, 1)
    handled = {}
    for outcome in outcomes:
        if outcome['section'] == 'handled':
            handled.setdefault(outcome['item']['account'], []).append(outcome)
    stop_notes = [account_reason(plan, r) for r in plan['reasons']]
    if plan.get('approval_state') == 'skipped' and any(
            line.endswith(' approval stale: ' + str(plan.get('run_id')))
            for line in read_text(store/'history.log').splitlines()):
        started = datetime.fromtimestamp(read_lock(store).get('started', read_lock(store)['created']), ZoneInfo(config.get('timezone', 'UTC')))
        return ('📬 Triage · ' + started.strftime('%A, %Y-%m-%d, %H:%M %Z')
                + (' · approval stale · 0 handled · stopped\nInbox changed since the question; nothing was done; the next run plans and asks again.' if language == 'en' else ' · aprobación desactualizada · 0 gestionados · detenido\nRecibidos cambió desde la pregunta; no se hizo nada; la próxima ejecución planifica y pregunta de nuevo.'))
    if execution.stop_reason():
        stop_notes.append(account_reason(plan, execution.stop_reason()))
    notes = []
    for index, action in enumerate(plan['actions']):
        status = (observed[index]['status'] if index < len(observed) else 'unexecuted')
        if action['action'] == 'create-folder':
            instruction = 'Check whether this folder exists; create it if absent: ' if status == 'issued_unknown' else 'Create this folder: '
            notes.append(account_name(plan, action['account']) + ': ' + instruction + chat_text(action['target_path']) + ('; ' + observed[index]['reason'] if index < len(observed) and observed[index].get('reason') else ''))
    if len(results['results']) != len(plan['actions']) and not (plan['reasons'] or execution.stop_reason()):
        stop_notes.append('Run ended before all actions completed' if language == 'en' else 'La ejecución terminó antes de completar todas las acciones')
    if failure_line: stop_notes.append(account_reason(plan, failure_line))
    log = store / 'calls.jsonl'
    for line in read_text(log).splitlines() if log.exists() else []:
        entry = parse_json(line, log)
        if entry['run'] == token and entry['outcome'] == 'unplanned':
            notes.append('Unplanned call: ' + chat_text(json.dumps(dict(entry, account=account_name(plan, entry['account'])), ensure_ascii=True)))
    started = datetime.fromtimestamp(read_lock(store).get('started', read_lock(store)['created']), ZoneInfo(config.get('timezone', 'UTC')))
    count = owner_count(outcomes)
    need = (str(count) + (' needs you' if count == 1 else ' need you') if count else 'nothing needs you') if language == 'en' else (str(count) + (' requiere tu decisión' if count == 1 else ' requieren tu decisión') if count else 'nada requiere tu decisión')
    if not committed:
        need = 'run not saved' if language == 'en' else 'ejecución sin guardar'
    total = sum(len(o.get('copies', [o['item']])) for o in outcomes
                if o['section'] == 'handled' or o['actions'])
    stopped = bool(plan['reasons']) or bool(execution.stop_reason()) or len(results['results']) != len(plan['actions'])
    summary = str(total) + (' handled' if language == 'en' else ' gestionado' if total == 1 else ' gestionados')
    if stopped: summary += ' · ' + STRINGS[language][1].lower()
    lines = []
    if plan.get('scheduled') and 'schedule' in config:
        slot = schedule_slot(config, started)
        delay = int((started.timestamp() - slot.timestamp()) // 60)
        if delay > 15:
            lines.append(('Late start: ' if language == 'en' else 'Inicio tardío: ') + slot.strftime('%Y-%m-%d %H:%M %Z') + ', ' + str(delay) + ' min')
    lines.insert(0, '📬 Triage · ' + started.strftime('%A, %Y-%m-%d, %H:%M %Z') + ' · ' + need + ' · ' + summary)
    problems = account_problems(plan, listing, store, language)
    lines[1:1] = plan.get('rule_diagnostics', [])[1:] + problems + list(dict.fromkeys(stop_notes))
    if plan.get('approval_state') == 'approved':
        lines.insert(1, ('Owner approved exceeding these thresholds. ' if language == 'en' else 'El propietario aprobó superar estos límites. ') + threshold_text(plan, language) + '.')
    if handled:
        lines += ['', labels['handled']]
        for account, actions in handled.items():
            lines.append(account_name(plan, account))
            for group in presentation_groups(actions):
                lines.append('- ' + group[0]['tag'] + ': ' + group_text(group, language))
    lines[1:1] = render_sends(plan, execute.read(store / 'inbox.json'), observed, language)
    lines += render_drafts(plan, execute.read(store / 'inbox.json'), observed)
    confirmed = dict(plan, remaining=remaining)
    lines += [account_name(plan, a) + ': no new mail' for a in listing.get('quiet', [])]
    if listing['accounts']: lines += ['', labels['inbox'], ''] + render_remaining(confirmed, listing, language, outcomes=outcomes, store=store)
    lines += plan.get('calendar_section', [])
    if notes:
        lines += ['', labels['notes'], ''] + notes
    return '\n'.join(lines) + '\n'


def load_inputs(paths):
    # Missing, empty, unreadable or malformed safety files abort before planning.
    result = []
    for path in paths:
        p = Path(path)
        try:
            if not p.exists():
                raise FileNotFoundError(str(p) + ': expected an existing readable file')
            if not p.is_file() or not p.stat().st_mode & 0o444:
                raise ValueError(str(p) + ': expected a readable file')
            content = p.read_text(encoding='utf-8')
        except UnicodeError as error:
            raise ValueError(str(p) + ': expected UTF-8 text') from error
        except OSError as error:
            raise OSError(str(p) + ': expected a readable file') from error
        if not content.strip():
            raise ValueError(str(p) + ': expected a non-empty safety file')
        result.append(content)
    return result


def load_safety(sources, diagnostics=None):
    """Read safety files, collecting independent errors before refusing."""
    problems, values = [], []
    for index, path in enumerate(sources):
        try:
            text = load_inputs([path])[0]
            value = parse_toml(text, path) if index == 0 else text
            values.append(value)
        except (ValueError, OSError) as error:
            problems.append(str(error))
            values.append(None)
    config, rules_text, holds_text, _ = values
    if config is not None:
        # Validate independent top-level settings and each account separately.
        accounts = config.get('accounts')
        probes = [dict(accounts={'probe': {}}, **{k: v}) for k, v in config.items() if k not in ('accounts', 'state', 'lock')]
        probes.append({k: config[k] for k in ('accounts', 'lock', 'state') if k in config})
        if isinstance(accounts, dict):
            probes.extend({'accounts': {a: fields}} for a, fields in accounts.items())
        for probe in probes:
            try:
                validate_inputs({'accounts': {}, 'messages': []}, probe, {})
            except ValueError as error:
                problems.append(str(sources[0]) + ': ' + str(error))
    rules, holds = [], []
    if rules_text is not None and config is not None and isinstance(config.get('accounts'), dict):
        rules = read_rules(rules_text, sources[1], config['accounts'], diagnostics)
    if holds_text is not None:
        try:
            holds = parse_holds(holds_text)
            if isinstance(config, dict) and isinstance(config.get('accounts'), dict):
                for number, hold in enumerate(holds, 1):
                    for account in hold.get('accounts', []):
                        if account not in config['accounts']:
                            problems.append(f"{sources[2]}: entry {number}: account {account!r} expected one of config.toml accounts: " + ', '.join(config['accounts']))
        except ValueError as error:
            problems.append(str(sources[2]) + ': ' + str(error))
    if problems:
        raise ValueError('\n'.join(dict.fromkeys(problems)))
    return config, rules, holds


def rule_cases(store, config, rules, holds):
    path = store / 'cases.toml'
    if not path.exists():
        return [], 0
    data = parse_toml(load_inputs([path])[0], path)
    known_keys(data, {'cases'}, 'cases.toml')
    cases = data.get('cases')
    if not isinstance(cases, list): raise ValueError('cases.toml: expected cases array')
    failures, names = [], set()
    for case in cases:
        known_keys(case, {'name', 'account', 'sender', 'subject', 'has_attachment', 'body', 'expect', 'expect_rule', 'date', 'recipients', 'is_reply'}, 'cases.toml case')
        name = case.get('name')
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError('cases.toml: expected unique nonempty case name')
        names.add(name)
        if case.get('account') not in config['accounts'] or case.get('expect') not in VERBS | {'none', 'never-touch'}:
            raise ValueError('cases.toml: expected configured account and supported expectation')
        if 'expect_rule' in case and (not isinstance(case['expect_rule'], str) or not case['expect_rule']):
            raise ValueError('cases.toml: expected rule id string')
        message = {k:v for k,v in case.items() if k in {'account', 'sender', 'subject', 'has_attachment', 'body', 'date', 'recipients', 'is_reply'}}
        message.update(id='case', folder='Inbox')
        validate_inputs(dict(accounts={case['account']:1},messages=[message]),config,{})
        sender = normalize_sender(message['sender'])
        message['sender'] = sender
        found, rule_id = 'none', None
        if sender is not None:
            plan = build_plan(dict(accounts={case['account']: 1}, messages=[message]), rules, holds, config)
            actions = [a for a in plan['actions'] if (a['account'], a['id']) == (case['account'], 'case')]
            if actions:
                found, rule_id = actions[-1]['action'], actions[0]['rule']
            elif plan['decisions'] and plan['decisions'][0]['reason'] == 'Protected by never-touch':
                found = 'never-touch'
            else:
                rule = select_rule(message, rules)
                rule_id = rule['id'] if rule is not None else None
        if found != case['expect'] or ('expect_rule' in case and case['expect_rule'] != rule_id):
            expected_rule = case.get('expect_rule')
            failures.append(safe_text(name) + ': expected ' + case['expect'] + (' rule ' + safe_text(expected_rule) if expected_rule else '') + ', found ' + found + (' rule ' + safe_text(rule_id) if rule_id else ''))
    return failures, len(cases)


def commit_state(store, token, plan_path, results_path=None, *, now=None, lock_verified=False):
    with lock_gate(store):
        require_lock(store, token)
        if store_lock_setting(store)['kind'] == 'onedrive' and not lock_verified:
            raise ValueError('run.lock: fresh drive read required before commit')
        def read(path):
            try:
                return parse_json(load_inputs([path])[0], path)
            except ValueError as error:
                raise ValueError(str(path) + ': ' + str(error)) from error
        plan = read(plan_path)
        require_plan_token(plan, token)
        if plan.get('dry_run'):
            raise ValueError('dry-run plan: commit-state is refused')
        known_keys(plan, {'actions', 'decisions', 'reasons', 'counts', 'expected_counts', 'not_listed', 'skip_reasons', 'first_run', 'reply', 'reply_source', 'remaining', 'relist_inbox', 'calendar_section', 'scheduled', 'account_names', 'run_token', 'run_id', 'approval_state', 'approval_created', 'approved_thresholds', 'approval_thresholds', 'answers', 'answers_error', 'answers_used', 'ignored_answers', 'hand_sent', 'rule_diagnostics'}, str(plan_path))
        for key in ('actions', 'reasons', 'not_listed'):
            if not isinstance(plan.get(key), list):
                raise ValueError(str(plan_path) + '.' + key + ': expected an array')
        counts = plan.get('counts')
        known_keys(counts, set(counts) if isinstance(counts, dict) else set(), str(plan_path) + '.counts')
        if any(type(n) is not int or n < 0 for n in counts.values()):
            raise ValueError(str(plan_path) + '.counts: expected non-negative integer counts')
        actions = set()
        for i, action in enumerate(plan['actions']):
            label = str(plan_path) + '.actions.' + str(i)
            known_keys(action, {'account', 'id', 'action', 'rule', 'target', 'target_path', 'url', 'draft', 'draft_text', 'draft_to', 'digest', 'reason', 'note'}, label)
            if any(not isinstance(action.get(k), str) for k in ('account', 'id', 'action')):
                raise ValueError(label + ': expected account, id and action strings')
            if action['action'] not in PLAN_VERBS or action['account'] not in counts:
                raise ValueError(label + '.action: expected a known action in a counted account')
            key = (action['account'], action['id'], action['action'])
            if key in actions:
                raise ValueError(label + '.id: expected a unique action')
            actions.add(key)
        import execute
        execution = execute.Execution(store, token, plan_path)
        execution.validate()
        if execution.state.get('single_reads') and 'final_listing' not in execution.state:
            raise ValueError('commit-state: final whole Inbox reading required; run execute.py final')
        expected_results = (store / 'results.json').resolve()
        results_path = expected_results if results_path is None else results_path
        if Path(results_path).resolve() != expected_results:
            raise ValueError(str(results_path) + ': expected this run’s results file ' + str(expected_results))
        if not expected_results.is_file():
            raise ValueError(str(expected_results) + ': expected results recorded by the step machine for this token and plan; run execute.py next')
        executed = []
        results = read(results_path)
        known_keys(results, {'results'}, str(results_path))
        if not isinstance(results.get('results'), list):
            raise ValueError(str(results_path) + '.results: expected an array')
        seen = set()
        for i, entry in enumerate(results['results']):
            label = str(results_path) + '.results.' + str(i)
            known_keys(entry, {'account', 'id', 'action', 'status', 'folder_id', 'reason', 'draft_id', 'created', 'digest'}, label)
            for k in ('account', 'id', 'action', 'status'):
                if not isinstance(entry.get(k), str):
                    raise ValueError(label + '.' + k + ': expected a string')
            key = (entry['account'], entry['id'], entry['action'])
            if key not in actions or key in seen:
                raise ValueError(label + '.id: expected a unique action matching the plan account, id and action')
            seen.add(key)
            if entry['status'] not in {'executed', 'failed', 'skipped', 'unexecuted', 'unsubscribe_not_executable', 'unsubscribe_not_attempted', 'issued_unknown', 'unconfirmed'}:
                raise ValueError(label + '.status: expected executed, failed, skipped, unexecuted, unsubscribe_not_executable, unsubscribe_not_attempted issued_unknown or unconfirmed')
            if entry['status'] == 'executed' and entry['action'] in {'trash', 'archive', 'move', 'spam'}:
                executed.append(entry['account'])
        if (not execution.path.exists() or results['results'] != execution.state['results']):
            raise ValueError(str(results_path) + ': expected results recorded by the step machine for this token and plan')
        config_path = store / 'config.toml'
        try:
            config = parse_toml(load_inputs([config_path])[0], config_path)
        except ValueError as error:
            raise ValueError(str(config_path) + ': expected valid TOML: ' + str(error)) from error
        state = store / 'state.json'
        previous = recover_drafts(store, read(state) if state.exists() else {}, token)
        log = store / 'decisions.jsonl'
        counters = [(read_run_counter(decision_batch(line).get('_run_counter')) or 0) for line in read_text(log).splitlines()] if log.exists() else []
        witnesses = [(state.name, read_run_counter(previous.get('_run_counter')) or 0),
                     ('numbers.json', read_run_counter(read_numbers(store).get('_run_counter')) or 0),
                     ('decisions.jsonl', max(counters, default=0))]
        source, highest = max(witnesses, key=lambda pair: pair[1])
        if highest == 2 ** 53:
            raise ValueError(source + '._run_counter: counter limit reached')
        run_counter = highest + 1
        # Reuse the same config and saved-count validation as planning.
        try:
            validate_inputs({'accounts': {}, 'messages': []}, config, previous)
        except ValueError as error:
            source = state if str(error).startswith('state.json') else config_path
            raise ValueError(str(source) + ': ' + str(error)) from error
        counts = {a: n for a, n in counts.items() if a in config['accounts']}
        if not plan['reasons']:
            for account in executed:
                if account in counts:
                    counts[account] -= 1
        if any(n < 0 for n in counts.values()):
            raise ValueError(str(plan_path) + '.counts: expected at least as many observed messages as executed removals')
        if execution.stop_reason():
            change = execution.state['outside_change']
            counts[change['account']] -= len(change['ids'])
        if 'final_listing' in execution.state:
            for account in execution.state['final_listing'].get('skipped', {}):
                previous.get('_positions', {}).pop(account, None)
            counts = {a: sum(m['account'] == a for m in execution.state['final_listing']['messages']) for a in counts if a in execution.state['final_listing']['accounts']}
        path = store / 'quiet-check.json'
        cached = read(path) if path.exists() else {}
        known_keys(cached, {'token', 'decision', 'positions'}, str(path))
        positions = cached.get('positions') if cached.get('token') == token else None
        if (not execution.stop_reason() and not plan.get('reply') and not plan['reasons'] and positions is not None
                and execution.path.exists() and len(execution.state['results']) == len(plan['actions'])):
            validate_positions(positions, config)
            confirmed = {a for a in counts if all(r['status'] not in ('issued_unknown', 'unexecuted', 'unconfirmed') for r in execution.state['results'] if r['account'] == a)}
            previous.setdefault('_positions', {}).update({a:p for a,p in positions.items() if a in confirmed})
        if plan.get('approval_state') in ('awaiting approval', 'skipped'):
            for account in counts:
                previous.get('_positions', {}).pop(account, None)
        answers = read_answers(store, recover=True)
        if plan.get('reply') and not plan['reasons']:
            for part in plan['reply']:
                if part['verb'] in {'leave', 'yes', 'no'}:
                    answers[part['account'], part['id']] = part['verb']
        batch = None
        if plan.get('reply') and not plan['reasons']:
            log = store / 'decisions.jsonl'
            batches = [decision_batch(line) for line in read_text(log).splitlines()] if log.exists() else []
            if not any(b.get('run') == token for b in batches):
                statuses = {(e['account'], e['id']): e['status'] for e in execution.state['results']}
                pending = execution.state['pending']
                if pending is not None:
                    statuses[pending['account'], pending['id']] = 'issued_unknown'
                batch = dict(run=token, _run_counter=run_counter, time=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
                             parts=[e | {'outcome': statuses.get((e['account'], e['id']), 'answered' if e['verb'] in {'leave', 'yes', 'no'} else 'unexecuted')} for e in plan['reply']])
        listing = execution.current_listing(read(store / 'inbox.json'))
        observed = list(execution.state['results'])
        if execution.state['pending'] is not None:
            observed.append(execution.state['pending'] | {'status': 'issued_unknown'})
        confirmed = dict(plan, actions=[], remaining=execution.remaining_items(listing, observed),
                         not_listed=list(dict.fromkeys(plan.get('not_listed', []) + list(listing.get('skipped', {})))),
                         skip_reasons=plan.get('skip_reasons', {}) | listing.get('skipped', {}),
                         reasons=plan['reasons'] + ([execution.stop_reason()] if execution.stop_reason() else []))
        old = {(i['account'], i['id']): i for i in plan.get('remaining', [])}
        for item in confirmed['remaining']:
            item['previously_listed'] = old.get((item['account'], item['id']), {}).get('previously_listed', False)
        previous = recover_drafts(store, previous, token, plan=plan, observed=observed, listing=listing)
        records = previous.get('_drafts', [])
        if not plan['reasons']:
            for key in plan.get('hand_sent', {}).get('closed', []):
                execute.append_log(store, token, dict(account=key[0], id=key[1], action='create-draft'), 'closed')
        for action, result in zip(plan['actions'], observed):
            if action['action'] == 'send-draft' and result['status'] == 'executed':
                execute.append_log(store, token, dict(account=action['account'], id=action['id'], action='create-draft'), 'closed')
        fresh = after_send_listing(execution.state.get('final_listing', read(store / 'inbox.json')), plan.get('hand_sent', {}).get('failed', {}))
        full_accounts = set(fresh['accounts']) - set(fresh.get('quiet', [])) - set(fresh.get('skipped', {}))
        fresh_keys = {(m['account'], m['id']) for m in fresh['messages']}
        answers = {key: verb for key, verb in answers.items() if key[0] not in full_accounts or key in fresh_keys}
        metadata = presentation_listing(read(store / 'inbox.json'), listing)
        outcomes = number_decisions(store, confirmed, metadata, read_only=plan.get('approval_state') == 'awaiting approval', language=config.get('language', 'en'),
                                    outcome_plan=plan, observed=observed, final_listing=execution.state.get('final_listing', {}), owner_answers=answers, run_counter=run_counter, draft_records=records)
        next_number = read_numbers(store)['next']
        if batch is not None:
            batch['_next_number'] = next_number
            with log.open('a', encoding='utf-8') as out:
                out.write(json.dumps(batch, ensure_ascii=True) + '\n')
        state_snapshot = json.dumps({**previous, **counts, 'version': FORMAT_VERSION, '_run_counter': run_counter, '_next_number': next_number}, sort_keys=True)
        language = config.get('language', 'en')
        report = render_report(plan if plan.get('approval_state') == 'awaiting approval' else confirmed, listing, language, store, outcomes=outcomes, draft_records=records)
        marker = '**' + SECTION_LABELS[language]['inbox'] + '**'
        report = '\n'.join(render_sends(plan, read(store / 'inbox.json'), observed, language)) + '\n' + report if any(a['action'] == 'send-draft' for a in plan['actions']) else report
        report = report.replace(marker, '\n'.join(render_drafts(plan, read(store / 'inbox.json'), observed)) + '\n' + marker, 1)
        (store / 'report.md').write_text(report, encoding='utf-8')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=store, delete=False) as out:
                temporary = Path(out.name)
                out.write(state_snapshot)
            temporary.replace(state)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        (store / 'run-refusal.json').unlink(missing_ok=True)


TOOL_KINDS = PLAN_VERBS | {'send-draft', 'sent-read', 'message-read', 'profile', 'ids', 'attachments', 'labels', 'message', 'history', 'folders'}


# Read recipes and action tools are closed per provider and call kind.
PROVIDER_TOOLS = {
    'gmail': {
        'sent-read': {'gmail.users.threads.get'},
        'profile': {'gmail.users.getProfile'},
        'ids': {'gmail.users.messages.list'}, 'attachments': {'gmail.users.messages.list'},
        'labels': {'gmail.users.labels.list'}, 'message': {'gmail.users.messages.get'},
        'message-read': {'gmail.users.messages.get'}, 'history': {'gmail.users.history.list'},
        'trash': {'gmail.users.messages.trash'},
        'archive': {'gmail.users.messages.modify'}, 'move': {'gmail.users.messages.modify'},
        'spam': {'gmail.users.messages.modify'}, 'create-folder': {'gmail.users.labels.create'},
        'create-draft': {'gmail.users.drafts.create'},
        'send-draft': {'gmail.users.drafts.send'},
    },
    'outlook': {
        'sent-read': {'meMailFolder.meMailFoldersListMessages'},
        'profile': {'meUser.meUserGetUser'},
        'ids': {'meMailFolder.meMailFoldersListMessages'},
        'folders': {'meMailFolder.meListMailFolders', 'meMailFolder.meMailFoldersListChildFolders'},
        'message-read': {'meMessage.meGetMessages'},
        'trash': {'meMessage.meMessagesMessageMove'}, 'archive': {'meMessage.meMessagesMessageMove'},
        'move': {'meMessage.meMessagesMessageMove'}, 'spam': {'meMessage.meMessagesMessageMove'},
        'create-folder': {'meMailFolder.meCreateMailFolders', 'meMailFolder.meMailFoldersCreateChildFolders'},
        'create-draft': {'meMessage.meMessagesMessageCreateReply', 'meMessage.meMessagesMessageCreateForward'},
        'send-draft': {'meMessage.meMessagesMessageSend'},
    },
}


def tool_path(app, tail):
    prefix = 'tools.' + app if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', app) else 'tools[' + json.dumps(app) + ']'
    return prefix + '.' + tail


def provider_failed(value):
    if not isinstance(value, dict): return False
    if value.get('ok') is False or 'error' in value: return True
    for key in ('code', 'status', 'statusCode'):
        status = value.get(key)
        if (type(status) is int or isinstance(status, str) and status.isdigit()) and int(status) >= 400:
            return True
    return any(provider_failed(value.get(k)) for k in ('error', 'value', 'response'))


def provider_error(value):
    nodes = []
    def collect(node):
        if isinstance(node, dict):
            nodes.append(node)
            for key in ('error', 'value', 'response'):
                collect(node.get(key))
    collect(value)
    for node in nodes:
        for key in ('status', 'statusCode', 'code'):
            code = node.get(key)
            if (type(code) is int and code >= 100 and len(str(code)) <= 40) or (isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,40}', code)):
                return 'provider error (' + str(code) + ')'
    for node in nodes:
        error = node.get('error')
        if isinstance(error, dict):
            message = error.get('message')
            match = re.search(r'HTTP ([1-5][0-9]{2})(?![0-9])', message) if isinstance(message, str) else None
            if match:
                return 'provider error (' + match[1] + ')'
    for node in nodes:
        error = node.get('error')
        kind = error.get('kind') if isinstance(error, dict) else None
        if isinstance(kind, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,40}', kind):
            return 'provider error (' + kind + ')'
    return 'provider error (no status)'


def checked_tool(config, account, kind, path):
    fields = config.get('accounts', {}).get(account, {})
    match = re.fullmatch(r'tools(?:\.([A-Za-z_][A-Za-z0-9_-]*)|\["([A-Za-z_][A-Za-z0-9_-]*)"\])\.([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)', path) if isinstance(path, str) and len(path) <= 200 else None
    send_draft = (kind == 'send-draft' and config.get('allow_send') is True
                  and match is not None and match[3] in PROVIDER_TOOLS.get(fields.get('provider'), {}).get('send-draft', set()))
    if (not match or (match[1] or match[2]) != fields.get('executor_app')
            or kind not in TOOL_KINDS or (kind == 'send-draft' and not send_draft)
            or (not send_draft and any(word in path.casefold() for word in ('send', 'delete')))
            or (kind != 'create-draft' and any(word in path.casefold() for word in ('reply', 'forward')))):
        raise ValueError('tool-paths.json: expected own account app and safe tool for ' + kind)
    tail = match[3]
    if tail in {'meMessage.meMoveMessages', 'meMessage.meCreateReply', 'meMessage.meCreateForward'}:
        raise ValueError('tool-paths.json: expected current provider tool')
    provider = fields.get('provider')
    if provider in PROVIDER_TOOLS and tail not in PROVIDER_TOOLS[provider].get(kind, set()):
        raise ValueError('tool-paths.json: expected provider tool for ' + kind)
    if kind == 'create-draft' and provider not in PROVIDER_TOOLS and not re.search(r'(?:draft.*create|create.*(?:draft|reply|forward))', tail, re.I):
        raise ValueError('tool-paths.json: expected a draft-creating tool')
    return tool_path(match[1] or match[2], tail)


def validate_tool_paths(data, config, *, drop=False):
    known_keys(data, set(data) if isinstance(data, dict) else set(), 'tool-paths.json')
    for account, entries in list(data.items()):
        if not isinstance(entries, dict):
            raise ValueError('tool-paths.json: expected account tool map')
        for kind, path in list(entries.items()):
            try:
                entries[kind] = checked_tool(config, account, kind, path)
            except ValueError as error:
                if not drop: raise
                print('Refused: ' + str(error) + '; dropped ' + safe_text(account) + '/' + safe_text(kind), file=sys.stderr)
                del entries[kind]


def saved_tool(store, account, kind):
    path = store / 'tool-paths.json'
    data = parse_json(read_text(path), path) if path.exists() else {}
    validate_tool_paths(data, state_config(store), drop=True)
    tool = data.get(account, {}).get(kind)
    return {'tool': tool} if tool else {}


LISTING_BATCH = 50
LISTING_PAGES = 1000


def mail_call(fields, kind, *, page=None, id=None, parent=''):
    gmail = fields['provider'] == 'gmail'
    query = {}
    path = {'userId': 'me'} if gmail else {}
    if gmail:
        tool = {'profile':'getProfile', 'ids':'messages.list', 'attachments':'messages.list',
                'labels':'labels.list', 'message':'messages.get', 'history':'history.list'}[kind]
        tool = 'gmail.users.' + tool
        if kind in ('ids', 'attachments', 'history'):
            query = dict(maxResults=100)
            if kind == 'history':
                query.update(startHistoryId=id, labelId='INBOX', historyTypes=['messageAdded', 'labelAdded'])
            else:
                query['labelIds'] = ['INBOX']
            if kind == 'attachments': query['q'] = 'has:attachment'
            if page is not None: query['pageToken'] = page
        if kind == 'message':
            path['id'] = id
            query = dict(format='metadata', metadataHeaders=['From', 'Subject', 'Message-ID', 'List-Unsubscribe', 'List-Unsubscribe-Post'])
    else:
        tool = 'meMailFolder.' + ('meMailFoldersListMessages' if kind == 'ids' else
                                'meMailFoldersListChildFolders' if id else 'meListMailFolders')
        if kind == 'ids':
            path = {'mailFolder-id':'inbox'}
            query = {'$select':['id', 'from', 'subject', 'receivedDateTime', 'hasAttachments', 'conversationId', 'internetMessageId'],
                     '$orderby':['receivedDateTime desc'], '$top':100, '$skip':page or 0}
        else:
            if id: path = {'mailFolder-id':id}
            query = {'$select':['id', 'displayName', 'totalItemCount', 'childFolderCount'],
                     '$top':100, '$skip':page or 0, 'includeHiddenFolders':'true'}
    if not gmail and kind == 'profile':
        tool = 'meUser.meUserGetUser'
        path = {}
        query = {'$select': ['mail', 'userPrincipalName']}
    inner = {}
    if path: inner['path'] = path
    if query: inner['query'] = query
    return dict(path=tool_path(fields['executor_app'], tool),
                input=dict(accountId=fields['executor_account'], input=inner))


def listing_call(config, job, ids_only, store=None):
    call = mail_call(config['accounts'][job['account']], ('ids' if job['kind']=='arrivals' else job['kind']), page=job.get('page'), id=job.get('id'))
    if ids_only and job['kind'] == 'ids' and config['accounts'][job['account']]['provider']=='outlook':
        call['input']['input']['query']['$select'] = ['id']
    if store is not None:
        saved = saved_tool(store, job['account'], 'ids' if job['kind']=='arrivals' else job['kind'])
        if saved: call['path'] = saved['tool']
    return call


def gateway_results(path, count):
    expected = 'gateway results: expected a JSON array with one result per call in call order, each with boolean ok (true and value, or false and error)'
    try:
        values = parse_json(read_text(path), 'gateway results')
    except ValueError:
        raise ValueError(expected) from None
    if (not isinstance(values, list) or len(values) != count
            or any(not isinstance(v, dict) or type(v.get('ok')) is not bool for v in values)):
        raise ValueError(expected)
    return values


def listing_exchange(store, token, results=None, *, ids_only=False, accounts=None, manual_part=None, skip=(), approval=None, dry_run=False):
    """Persist issued reads before returning them; mail values remain data."""
    import execute
    with lock_gate(store):
        require_lock(store, token)
        require_fetched(store, token)
        config, rules, holds = load_safety([store / n for n in ('config.toml','rules.md','never-touch.toml','guardrails.md')])
        if dry_run and (ids_only or approval):
            raise ValueError('listing --dry-run: expected a full rehearsal listing')
        target = store / SIMULATION_DIR if dry_run else store
        if dry_run:
            if target.is_symlink() or target.exists() and not target.is_dir():
                raise ValueError('listing --dry-run: expected a real rehearsal directory')
            target.mkdir(mode=0o700, exist_ok=True)
        filename = target / ('approval-reading.json' if approval else 'final-listing-exchange.json' if ids_only else 'listing-exchange.json')
        state = parse_json(read_text(filename), filename) if filename.exists() else {}
        if approval and state.get('approval') != approval:
            if results is not None: raise ValueError('approval: no pending fresh reading')
            state = {}
        if state.get('token') == token:
            skip = set(skip) | {a for a,r in state.get('failed', {}).items() if r == 'pigeon error (manual skip)'}
        names = list(accounts if accounts is not None else config['accounts'])
        if set(skip) - set(config['accounts']):
            raise ValueError('listing: unknown skipped account')
        quiet_path = store / 'quiet-check.json'
        quiet_cache = parse_json(read_text(quiet_path), quiet_path) if quiet_path.exists() and not dry_run else {}
        quiet_names = quiet_cache.get('decision', {}).get('quiet_accounts', []) if quiet_cache.get('token') == token and not ids_only else []
        manual = [a for a in names if a not in skip and a not in quiet_names and not ids_only and any(any(k in r for k in ('body', 'recipients_min', 'recipients_max', 'is_reply')) and a in r.get('accounts', [a]) for r in rules + holds)]
        part = None
        if manual:
            if manual_part is None:
                kinds = [label for keys, label in [(('body',), 'body'), (('recipients_min', 'recipients_max', 'is_reply'), 'conditional')]
                         if any(any(k in r for k in keys) and any(a in r.get('accounts', [a]) for a in manual) for r in rules + holds)]
                raise ValueError('listing: ' + ', '.join(k + ' entries' for k in kinds) + ' require --manual-part: ' + ', '.join(manual))
            part = parse_json(read_text(manual_part), manual_part)
            validate_inputs(part, config, {})
            if set(part['accounts']) != set(manual) or any(m['account'] not in manual for m in part['messages']) or set(part.get('folders', {})) - set(manual):
                raise ValueError('listing: manual part must contain exactly: ' + ', '.join(manual))
            build_plan(part, rules, holds, config, {})
        elif manual_part is not None:
            raise ValueError('listing: no manual accounts expected')
        names = [a for a in names if a not in manual and a not in quiet_names]
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if state.get('token') != token:
            if results is not None: raise ValueError('listing: no pending batch')
            state = dict(token=token, config=fingerprint, round=0, queue=[], pending=[], failed={a:'pigeon error (manual skip)' for a in skip}, attempts={},
                         data={a:dict(ids=[], attachments=[], messages=[], folders=[], total=0, seen={}) for a in names})
            if approval: state['approval'] = approval
            for a in names:
                for kind in (['ids'] if ids_only else ['profile','ids','attachments','labels'] if config['accounts'][a].get('provider')=='gmail' else ['profile','ids','folders']):
                    state['queue'].append(dict(account=a,kind=kind))
        state['failed'].update({a:'pigeon error (manual skip)' for a in skip})
        for account in names:
            if account in state['failed']: continue
            if not all(config['accounts'][account].get(k) for k in ('provider','executor_app','executor_account')):
                raise ValueError('listing: ' + account + ': build the whole listing by hand; mixed listings refused')
        if state['config'] != fingerprint or set(state['data']) != set(names):
            raise ValueError('listing: configuration changed')
        if results is not None:
            if approval and (not state['pending'] or Path(results).stat().st_mtime < state.get('issued', float('inf')) or state.get('issued', 0) < read_lock(store).get('started', read_lock(store)['created'])):
                raise ValueError('approval: expected a fresh answer to the pending reading')
            values = gateway_results(results, len(state['pending']))
            # A message response carries the requested ID; validate before consuming any result.
            for job, value in zip(state['pending'], values):
                if job['kind']=='message' and isinstance(value,dict) and value.get('ok') is True and isinstance(value.get('value'),dict) and message_id(value['value'].get('id')) and value['value']['id'] != job['id']:
                    raise ValueError('listing: results out of order or mismatched message id')
            for job, value in zip(state['pending'], values):
                a, kind = job['account'], job['kind']
                if a in state['failed']: continue
                try:
                    if provider_failed(value): raise ValueError()
                    if not isinstance(value,dict) or value.get('ok') is not True or not isinstance(value.get('value'),dict): raise ValueError()
                    consume_listing(state, job, value['value'], config, ids_only)
                except Exception:
                    state['failed'][a] = (provider_error(value) if provider_failed(value) else 'pigeon error (listing answer)')
            state['pending'] = []
            for a in list(state['failed']):
                if state['failed'][a] in ('pigeon error (manual skip)', 'pigeon error (no new mail)') or a not in state['data'] or state.setdefault('attempts', {}).get(a, 0): continue
                state['attempts'][a] = 1
                if ids_only: state['arrivals_checked'] = False
                state.setdefault('retry_errors', {})[a] = state['failed'][a]
                del state['failed'][a]
                state['data'][a] = dict(ids=[], attachments=[], messages=[], folders=[], total=0, seen={})
                state['queue'] = [j for j in state['queue'] if j['account'] != a]
                kinds = ['ids'] if ids_only else ['profile','ids','attachments','labels'] if config['accounts'][a]['provider']=='gmail' else ['profile','ids','folders']
                state['queue'] += [dict(account=a,kind=k) for k in kinds]
        if ids_only and not approval and not state['pending'] and not state['queue'] and not state.get('arrivals_checked'):
            original = parse_json(read_text(store/'inbox.json'), 'inbox')
            for a,d in state['data'].items():
                if a in state['failed']: continue
                old = {m['id'] for m in original['messages'] if m['account']==a}
                new = [i for i in d['ids'] if i not in old]
                d['new'] = new
                if new:
                    state['queue'] += ([dict(account=a,kind='message',id=i) for i in new] + [dict(account=a,kind='attachments')] if config['accounts'][a]['provider']=='gmail' else [dict(account=a,kind='arrivals')])
            state['arrivals_checked'] = True
        if state['pending']:
            jobs = state['pending']
        else:
            state['queue'] = [j for j in state['queue'] if j['account'] not in state['failed']]
            jobs = state['queue'][:LISTING_BATCH]
            state['queue'] = state['queue'][LISTING_BATCH:]
            state['pending'] = jobs
            if jobs:
                state['round'] += 1
                if approval: state['issued'] = time.time()
        execute.atomic(filename, state)
        if jobs:
            return dict(round=state['round'], batches_remaining=(len(state['queue'])+LISTING_BATCH-1)//LISTING_BATCH,
                        calls=[listing_call(config, j, ids_only, store) for j in jobs], failed=state['failed'] | state.get('retry_errors', {}),
                        retries={a: 'retried once, retry pending' for a in state.get('retry_errors', {})})
        listing = part or dict(accounts={},messages=[],folders={})
        listing.setdefault('folders', {})
        listing['skipped'] = dict(state['failed'])
        if quiet_names: listing['quiet'] = quiet_names
        for a,d in state['data'].items():
            if a in state['failed']: continue
            listing['accounts'][a] = (parse_json(read_text(store/'inbox.json'), 'inbox')['accounts'][a] if ids_only else d['total'])
            listing['folders'][a] = d['folders']
            if ids_only and approval:
                listing['messages'] += [dict(account=a, id=i, folder='Inbox', sender='', subject='') for i in d['ids']]
            elif ids_only:
                original = parse_json(read_text(store/'inbox.json'), 'inbox')
                old = {m['id']:m for m in original['messages'] if m['account']==a}
                for m in d['messages']:
                    if config['accounts'][a]['provider']=='gmail': m['has_attachment'] = m['id'] in d['attachments']
                    old[m['id']] = m
                if any(i not in old for i in d['ids']):
                    raise ValueError('final: arrival fields missing for ' + a)
                listing['messages'] += [old[i] for i in d['ids']]
            else:
                for m in d['messages']:
                    if config['accounts'][a]['provider']=='gmail': m['has_attachment'] = m['id'] in d['attachments']
                listing['messages'] += d['messages']
        validate_inputs(listing, config, {})
        execute.atomic(target / ('approval-inbox.json' if approval else 'final-inbox.json' if ids_only else 'inbox.json'), listing)
        return dict(done=True, counts={a:len(d['ids']) for a,d in state['data'].items() if a not in state['failed']}, **({'failed': state['failed']} if state['failed'] or not any(state.get('attempts', {}).values()) else {}),
                    retries={a: 'retried once, still failing' if a in state['failed'] else 'retried once, succeeded' for a, n in state.get('attempts', {}).items() if n})


def outlook_identity(value, fields):
    address = fields.get('address')
    if not address or not any(isinstance(value.get(k), str) and value[k].casefold() == address.casefold() for k in ('mail', 'userPrincipalName')):
        raise ValueError('Outlook identity mismatch or missing address')


def consume_listing(state, job, value, config, ids_only):
    a, kind = job['account'], job['kind']
    d = state['data'][a]
    gmail = config['accounts'][a]['provider']=='gmail'
    def integer(v):
        if type(v) is not int or v < 0: raise ValueError()
        return v
    def string(v):
        if not isinstance(v,str): raise ValueError()
        return v
    def identifier(v):
        if not message_id(v): raise ValueError()
        return v
    def enqueue(**kw): state['queue'].append(dict(account=a, **kw))
    if kind == 'arrivals':
        fresh = dict(account=a,kind='ids', **({'page':job['page']} if 'page' in job else {}))
        temp = dict(data={a:dict(ids=[],attachments=[],messages=[],folders=[],total=0,seen=d.setdefault('arrival_seen',{}))},queue=[])
        consume_listing(temp, fresh, value, config, False)
        d['messages'] += [m for m in temp['data'][a]['messages'] if m['id'] in d['new']]
        state['queue'] += [dict(j,kind='arrivals') for j in temp['queue']]
        return
    if kind == 'profile' and not gmail:
        outlook_identity(value, config['accounts'][a])
        return
    if kind == 'profile':
        d['total'] = integer(value['messagesTotal'])
        if 'address' in config['accounts'][a] and value.get('emailAddress','').casefold() != config['accounts'][a]['address'].casefold(): raise ValueError()
        return
    if kind == 'message':
        if identifier(value['id']) != job['id']: raise ValueError()
        if 'INBOX' not in value.get('labelIds', []):
            return
        headers = value['payload']['headers']
        if not isinstance(headers,list): raise ValueError()
        h = {}
        for entry in headers:
            name, text = string(entry['name']).lower(), string(entry['value'])
            if name in h: raise ValueError()
            h[name] = text
        if not isinstance(value['internalDate'],str) or not re.fullmatch(r'[0-9]{1,16}',value['internalDate']): raise ValueError()
        m = dict(account=a,id=job['id'],folder='Inbox',sender=h.get('from', ''),subject=h.get('subject') or '',
                 date=datetime.fromtimestamp(int(value['internalDate'])/1000,timezone.utc).isoformat(),
                 unsubscribe_one_click=h.get('list-unsubscribe-post','').strip().casefold()=='list-unsubscribe=one-click')
        if 'threadId' in value: m['conversation_id'] = string(value['threadId'])
        if 'message-id' in h: m['internet_message_id'] = h['message-id']
        urls = re.findall(r'<(https://[^<>\s]+)>',h.get('list-unsubscribe',''),re.I)
        if urls: m['unsubscribe_url'] = urls[0]
        validate_inputs(dict(accounts={a:d['total']},messages=[m]),config,{})
        d['messages'].append(m)
        return
    key = 'labels' if kind=='labels' else 'messages' if gmail else 'value'
    rows = value.get(key, [])
    if not isinstance(rows,list): raise ValueError()
    if kind=='labels':
        for row in rows:
            identifier(row['id']); string(row['name'])
            if row['type'] not in ('user','system'): raise ValueError()
            if row['type']=='user': d['folders'].append(dict(id=row['id'],name=row['name']))
        return
    seen_key = kind + ':' + job.get('id','')
    seen = d['seen'].setdefault(seen_key,[])
    marker = job.get('page','first')
    if marker in seen or len(seen)>=LISTING_PAGES: raise ValueError()
    seen.append(marker)
    for row in rows:
        ident = identifier(row['id'])
        if kind in ('ids','attachments'):
            target = d['attachments'] if kind=='attachments' else d['ids']
            if ident in target: raise ValueError()
            target.append(ident)
            if kind=='ids' and not ids_only:
                if gmail: enqueue(kind='message',id=ident)
                else:
                    sender = row['from']['emailAddress']
                    address = string(sender['address'])
                    name = string(sender.get('name',''))
                    if type(row['hasAttachments']) is not bool: raise ValueError()
                    m = dict(account=a,id=ident,folder='Inbox',sender=('"' + quote(name) + '" <' + address + '>') if name else address,
                             subject=string(row.get('subject') or ''),date=string(row['receivedDateTime']),has_attachment=row['hasAttachments'])
                    for source, target in (('conversationId', 'conversation_id'), ('internetMessageId', 'internet_message_id')):
                        if source in row: m[target] = string(row[source])
                    validate_inputs(dict(accounts={a:0},messages=[m]),config,{})
                    d['messages'].append(m)
        else:
            if any(f['id']==ident for f in d['folders']): raise ValueError()
            name = string(row['displayName'])
            full = job.get('parent','') + name
            try:
                folder_path(full)
            except ValueError:
                pass
            else:
                d['folders'].append(dict(id=ident,name=full))
            d['total'] += integer(row['totalItemCount'])
            if integer(row['childFolderCount']): enqueue(kind='folders',id=ident,parent=full+'/')
    page = value.get('nextPageToken') if gmail else (job.get('page',0)+100 if len(rows)==100 else None)
    if page is not None:
        if gmail and (not isinstance(page,str) or not page or page in seen): raise ValueError()
        if len(seen)>=LISTING_PAGES: raise ValueError()
        enqueue(**{k:v for k,v in job.items() if k!='account' and k!='page'},page=page)


def quiet_gateway(store, token, config, previous, results=None, *, dry_run=False, progress=None):
    import execute
    path = store/'quiet-gateway.json'
    state = (progress or {}) if dry_run else (parse_json(read_text(path),path) if path.exists() else {})
    if state.get('token') != token:
        if results is not None: raise ValueError('quiet-check: no pending batch')
        state = dict(token=token,round=0,queue=[],pending=[],observations={},seen={})
        for a,f in config['accounts'].items():
            if not all(f.get(k) for k in ('provider','executor_app','executor_account')):
                raise ValueError('quiet-check: mixed providers require manual observations')
            kind = 'profile' if f['provider']=='gmail' else 'ids'
            if f['provider'] == 'outlook': state['queue'].append(dict(account=a,kind='profile'))
            state['queue'].append(dict(account=a,kind=kind))
            if kind=='profile' and a in previous.get('_positions',{}):
                state['queue'].append(dict(account=a,kind='history',id=previous['_positions'][a]))
            state['observations'][a] = dict(outcome='ok',position=None,arrivals=[]) if kind=='profile' else dict(outcome='ok',ids=[])
        state['initial'] = list(state['queue'])
        state['retried'] = []
    if results is not None:
        values = gateway_results(results, len(state['pending']))
        for j,v in zip(state['pending'],values):
            a=j['account']; entry=state['observations'][a]
            try:
                if provider_failed(v) or v['ok'] is not True: raise ValueError()
                value=v['value']
                if j['kind']=='profile' and config['accounts'][a]['provider']=='outlook':
                    outlook_identity(value, config['accounts'][a])
                elif j['kind']=='profile':
                    pos=value['historyId']
                    if not isinstance(pos,str) or not pos.isascii() or not pos.isdigit(): raise ValueError()
                    entry['position']=pos
                elif j['kind']=='ids':
                    rows=value.get('value',[])
                    if not isinstance(rows,list) or len(rows)>NEWEST_IDS: raise ValueError()
                    ids=[r['id'] for r in rows]
                    if len(set(ids))!=len(ids) or any(not message_id(i) for i in ids): raise ValueError()
                    entry['ids']=ids
                else:
                    rows=value.get('history',[])
                    if not isinstance(rows,list): raise ValueError()
                    for h in rows:
                        for k in ('messagesAdded','labelsAdded'):
                            for event in h.get(k,[]):
                                m=event['message']; ident=m['id']
                                if not message_id(ident): raise ValueError()
                                if 'INBOX' in event.get('labelIds',m.get('labelIds',[])) and ident not in entry['arrivals']: entry['arrivals'].append(ident)
                    page=value.get('nextPageToken')
                    seen=state['seen'].setdefault(a,[])
                    if page is not None:
                        if not isinstance(page,str) or not page or page in seen or len(seen)>=LISTING_PAGES: raise ValueError()
                        seen.append(page); state['queue'].append(j|dict(page=page))
            except (ValueError,TypeError,KeyError,AttributeError):
                entry['outcome']='error'
                entry['reason'] = provider_error(v) if provider_failed(v) else 'pigeon error (listing answer)'

        state['pending']=[]
        failed = {a for a, entry in state['observations'].items() if entry['outcome'] == 'error'}
        state['queue'] = [j for j in state['queue'] if j['account'] not in failed]
        for a in config['accounts']:
            if a in failed and a not in state['retried']:
                state['retried'].append(a)
                state['queue'] += [j for j in state['initial'] if j['account'] == a]
                state['seen'].pop(a, None)
                state['observations'][a] = dict(outcome='ok',position=None,arrivals=[]) if config['accounts'][a]['provider']=='gmail' else dict(outcome='ok',ids=[])
    if not state['pending']:
        state['pending']=state['queue'][:LISTING_BATCH];state['queue']=state['queue'][LISTING_BATCH:]
        if state['pending']: state['round']+=1
    if not dry_run: execute.atomic(path,state)
    if not state['pending']:
        if dry_run:
            return dict(observations=state['observations'], retries={a: ('retried once, still failing' if state['observations'][a]['outcome'] == 'error' else 'retried once, succeeded') for a in state['retried']})
        observation=store/'quiet-observation.json'
        execute.atomic(observation,state['observations'])
        return observation
    calls=[]
    for j in state['pending']:
        call=mail_call(config['accounts'][j['account']],j['kind'],page=j.get('page'),id=j.get('id'))
        if j['kind']=='ids':
            call['input']['input']['query'].update({'$select':['id'],'$top':NEWEST_IDS})
        calls.append(call)
    response = dict(calls=calls,round=state['round'],batches_remaining=(len(state['queue'])+LISTING_BATCH-1)//LISTING_BATCH)
    if dry_run: response['progress'] = state
    return response


def record_refusal(arguments, error):
    if arguments and (arguments[0] == 'dry-run' or '--dry-run' in arguments):
        return
    if str(error) == 'batch: expected result --results before another next': return
    if '--token' not in arguments: return
    try:
        import execute
        store = private_store()
        token = arguments[arguments.index('--token') + 1]
        with lock_gate(store):
            require_lock(store, token)
            refusal = re.sub(r'(?:/[^/:\n]+)+', lambda m: Path(m[0]).name, ' '.join(str(error).split()))[:500]
            execute.atomic(store / 'run-refusal.json', dict(token=token, stage=arguments[0], refusal=refusal))
    except (ValueError, OSError, IndexError):
        pass


def main(argv=None, *, now=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(arguments, now=now)
    except Exception as error:
        record_refusal(arguments, error)
        raise


def _main(argv=None, *, now=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('validate', allow_abbrev=False)
    for name in ('tool-save', 'tool-show', 'tool-drop'):
        tool = sub.add_parser(name, allow_abbrev=False)
        if name != 'tool-show':
            tool.add_argument('--token', required=True)
            tool.add_argument('--account', required=True)
            tool.add_argument('--kind', required=True, choices=sorted(TOOL_KINDS))
        if name == 'tool-save': tool.add_argument('--tool', required=True)
    sub.add_parser('setup', allow_abbrev=False)
    lock = sub.add_parser('lock', allow_abbrev=False)
    lock.add_argument('--observation')
    lock.add_argument('--batch', action='store_true')
    lock.add_argument('--refresh', action='store_true')
    lock.add_argument('--token')
    for name in ('release', 'check'):
        command = sub.add_parser(name)
        command.add_argument('--token', required=True)
        if name == 'release':
            command.add_argument('--observation')
    for name in ('fetch-state', 'send-state'):
        command = sub.add_parser(name, allow_abbrev=False)
        command.add_argument('--token', required=True)
        command.add_argument('--observation')
        command.add_argument('--lock-observation')
    finish = sub.add_parser('finish-run', allow_abbrev=False)
    finish.add_argument('--token', required=True)
    finish.add_argument('--plan')
    finish.add_argument('--observation')
    commit = sub.add_parser('commit-state', allow_abbrev=False)
    commit.add_argument('--token', required=True)
    commit.add_argument('--plan', required=True)
    commit.add_argument('--results', help='Recorded execution results, not gateway results; docs/quickstart.md#gateway-results')
    commit.add_argument('--lock-observation')
    message = sub.add_parser('message', allow_abbrev=False)
    message.add_argument('--token', required=True)
    message.add_argument('--plan', required=True)
    message.add_argument('--results', help='Recorded execution results, not gateway results; docs/quickstart.md#gateway-results')
    listing = sub.add_parser('listing', allow_abbrev=False)
    listing.add_argument('--token', required=True)
    listing.add_argument('--results', help='Gateway array: docs/quickstart.md#gateway-results')
    listing.add_argument('--manual-part')
    listing.add_argument('--skip', action='append', default=[])
    listing.add_argument('--ids-only', action='store_true')
    listing.add_argument('--dry-run', action='store_true')
    quiet = sub.add_parser('quiet-check', allow_abbrev=False)
    quiet.add_argument('--token', required=True)
    quiet.add_argument('--observation')
    quiet.add_argument('--results', help='Gateway array: docs/quickstart.md#gateway-results')
    quiet.add_argument('--scheduled', action='store_true')
    quiet.add_argument('--dry-run', action='store_true')
    quiet.add_argument('--progress', help='Saved dry-run quiet response')
    for command_name in ('plan', 'reply'):
        plan = sub.add_parser(command_name, allow_abbrev=False)
        plan.add_argument('--dry-run', action='store_true')
        plan.add_argument('--answers')
        if command_name == 'plan':
            plan.add_argument('--draft-check-results')
        if command_name == 'plan':
            plan.add_argument('--calendar')
        if command_name == 'reply':
            plan.add_argument('--reply', required=True)
            plan.add_argument('--question')
        plan.add_argument('--token', required=True)
        plan.add_argument('--inbox', required=True)
        plan.add_argument('--scheduled', action='store_true')
        for name in ('config', 'rules', 'never-touch', 'guardrails', 'plan-out', 'report-out'):
            plan.add_argument('--' + name)
    args = parser.parse_args(argv)
    for name in ('inbox', 'config', 'rules', 'never_touch', 'guardrails', 'plan_out', 'report_out', 'plan', 'results', 'reply', 'answers', 'draft_check_results'):
        if getattr(args, name, None) == '':
            raise ValueError('--' + name.replace('_', '-') + ': expected a non-empty path')
    if args.command == 'setup':
        store = private_store(output=True)
        examples = {'config.toml': 'config/config.example.toml',
                    'guardrails.md': 'config/guardrails.example.md',
                    'rules.md': 'rules/rules.example.md',
                    'never-touch.toml': 'rules/never-touch.example.toml'}
        root = Path(__file__).resolve().parents[1]
        contents = {name: read_text(root / source) for name, source in examples.items()}
        contents['guardrails.md'] = re.sub(r'\]\(\.\./([^)]*)\)', lambda m: '](' + str(root) + '/' + m[1] + ')', contents['guardrails.md'])
        store.mkdir(parents=True, exist_ok=True, mode=0o700)
        store.chmod(0o700)
        for name in examples:
            if (store / name).exists() or (store / name).is_symlink():
                continue
            with (store / name).open('x', encoding='utf-8') as out:
                os.chmod(out.name, 0o600)
                out.write(contents[name])
        print(str(store) + ': next run python3 scripts/triage.py validate')
        return
    store = private_store()
    if args.command == 'reply' and approval_reply(store, args):
        return
    if args.command in ('tool-save', 'tool-show', 'tool-drop'):
        import execute
        with lock_gate(store):
            config = state_config(store)
            path = store / 'tool-paths.json'
            data = parse_json(read_text(path), path) if path.exists() else {}
            validate_tool_paths(data, config, drop=True)
            if args.command != 'tool-show':
                require_lock(store, args.token)
                require_fetched(store, args.token)
                if args.command == 'tool-save' and args.account not in config['accounts']: raise ValueError('tool account: expected configured handle')
                if args.command == 'tool-save':
                    data.setdefault(args.account, {})[args.kind] = args.tool
                else:
                    data.get(args.account, {}).pop(args.kind, None)
                validate_tool_paths(data, config)
                execute.atomic(path, data)
            print(json.dumps(data, sort_keys=True))
    elif args.command == 'validate':
        diagnostics = []
        config, rules, holds = load_safety([store / n for n in ('config.toml', 'rules.md', 'never-touch.toml', 'guardrails.md')], diagnostics)
        if (store / 'rules.toml').exists():
            diagnostics.append(LEGACY_RULES)
        print('\n'.join(diagnostics))
        for rule in rules:
            if 'after_send' in rule and any(not config['accounts'][a].get('provider') for a in rule['accounts']):
                raise ValueError('after_send: configured provider required for every rule account')
        state_path = store / 'state.json'
        if state_path.exists():
            validate_inputs(dict(accounts={},messages=[]),config,parse_json(read_text(state_path),state_path))
        print(f'version: {config.get("version", FORMAT_VERSION)}')
        for account, fields in config['accounts'].items():
            available = all(fields.get(k) for k in ('provider', 'executor_app', 'executor_account'))
            manual = any(any(k in r for k in ('body', 'recipients_min', 'recipients_max', 'is_reply')) and account in r.get('accounts', [account]) for r in rules + holds)
            print(account + ': scripted listing ' + ('manual part required' if manual else 'available' if available else 'unavailable'))
        print(f'ok: {len(config["accounts"])} accounts, {len({r["id"] for r in rules})} rules, {len(holds)} never-touch entries')
        failures, count = rule_cases(store, config, rules, holds)
        for failure in failures: print(failure)
        print(f'cases: {count}, failed: {len(failures)}')
        if failures: return 1
    elif args.command == 'lock':
        if args.batch and args.refresh:
            raise ValueError('--batch: use acquisition only')
        if args.refresh and not args.token:
            raise ValueError('--refresh: expected --token of the holder')
        if args.token and not args.refresh:
            raise ValueError('--token: requires --refresh')
        if args.batch and store_lock_setting(store)['kind'] == 'onedrive':
            result = batch_lock(store, args.observation, now=now)
            print(json.dumps(result, sort_keys=True) if isinstance(result, dict) else result)
            return
        result = drive_lock(store, 'refresh' if args.refresh else 'acquire', args.token, args.observation, now=now) if store_lock_setting(store)['kind'] == 'onedrive' else (refresh(store, args.token, now=now) if args.refresh else acquire(store))
        print(json.dumps(result, sort_keys=True) if isinstance(result, dict) else result)
    elif args.command == 'release':
        result = drive_lock(store, 'release', args.token, args.observation, now=now) if store_lock_setting(store)['kind'] == 'onedrive' else release(store, args.token)
        print(json.dumps(result, sort_keys=True) if isinstance(result, dict) else result)
    elif args.command == 'check':
        with lock_gate(store):
            require_lock(store, args.token)
        print('Lock is held and unexpired')
    elif args.command in ('fetch-state', 'send-state'):
        print(json.dumps(drive_state(store, 'fetch' if args.command == 'fetch-state' else 'send', args.token, args.observation, lock_observation=args.lock_observation, now=now), sort_keys=True))
    elif args.command == 'finish-run':
        print(json.dumps(finish_run(store, args.token, args.plan, args.observation, now=now), ensure_ascii=True))
    elif args.command == 'commit-state':
        require_lock(store, args.token)
        plan = parse_json(load_inputs([args.plan])[0], args.plan)
        require_plan_token(plan, args.token)
        if plan.get('dry_run'):
            raise ValueError('dry-run plan: commit-state is refused')
        if store_lock_setting(store)['kind'] == 'onedrive':
            result = drive_lock(store, 'commit', args.token, args.lock_observation, now=now)
            if isinstance(result, dict):
                print(json.dumps(result, sort_keys=True))
                return
        commit_state(store, args.token, args.plan, args.results, now=now, lock_verified=True)
    elif args.command == 'message':
        with lock_gate(store):
            print(render_message(store, args.token, args.plan, args.results, now=now), end='')
    elif args.command == 'listing':
        print(json.dumps(listing_exchange(store, args.token, args.results, ids_only=args.ids_only, manual_part=args.manual_part, skip=args.skip, dry_run=args.dry_run), ensure_ascii=True))
    elif args.command == 'quiet-check':
        with lock_gate(store):
            require_lock(store, args.token)
            require_fetched(store, args.token)
            config_path = store / 'config.toml'
            config = parse_toml(load_inputs([config_path])[0], config_path)
            state = store / 'state.json'
            previous = parse_json(load_inputs([state])[0], state) if state.exists() else {}
            try:
                validate_inputs({'accounts': {}, 'messages': []}, config, previous)
            except ValueError as error:
                raise ValueError(str(config_path) + ': ' + str(error)) from error
            now = now or datetime.now(timezone.utc)
            if args.results is not None or (args.observation is None and all(f.get('provider') for f in config['accounts'].values())):
                if args.progress and not args.dry_run: raise ValueError('--progress: requires --dry-run')
                progress = parse_json(load_inputs([args.progress])[0], args.progress).get('progress') if args.progress else None
                result = quiet_gateway(store, args.token, config, previous, args.results, dry_run=args.dry_run, progress=progress)
                if args.dry_run and 'observations' in result:
                    print(json.dumps(quiet_check(store, config, previous, result['observations'], args.scheduled, now, dry_run=True) | dict(retries=result['retries']), sort_keys=True))
                    return
                if isinstance(result, dict):
                    print(json.dumps(result, ensure_ascii=True))
                else:
                    print(json.dumps(quiet_check(store, config, previous, result, args.scheduled, now), sort_keys=True))
            elif args.observation is None:
                reads = {}
                for account, fields in config['accounts'].items():
                    if fields.get('quiet_kind', 'history' if fields.get('provider')=='gmail' else 'ids') == 'history':
                        reads[account] = {'read': 'Inbox arrivals since saved position and current position',
                                          'since': previous.get('_positions', {}).get(account)}
                        if reads[account]['since'] is None:
                            reads[account]['read'] = 'current position only'
                    else:
                        reads[account] = {'read': 'newest Inbox ids in newest-first order', 'limit': NEWEST_IDS}
                for account in reads:
                    reads[account].update(gateway_account(config, account))
                print(json.dumps({'reads': reads}, sort_keys=True))
            else:
                print(json.dumps(quiet_check(store, config, previous, args.observation, args.scheduled, now, dry_run=args.dry_run), sort_keys=True))
    else:
        sources = [getattr(args, n.replace('-', '_')) or store / (n + ('.md' if n in {'rules', 'guardrails'} else '.toml')) for n in ('config', 'rules', 'never-touch', 'guardrails')]
        protected = {Path(p).resolve() for p in sources + [args.inbox]} | {store / n for n in ('state.json', 'run.lock', '.lock-gate', 'calls.jsonl', 'results.json', 'execution-report.md', 'unsubscribe-failures.json', 'unsubscribe-secret', 'numbers.json', 'reply.json', 'reply.txt', 'decisions.jsonl', 'history.log', 'run-message.md', 'quiet-observation.json', 'positions-observation.json', 'quiet-check.json', 'lock-exchange.json', 'lock-observation.json', 'lock-commit-observation.json', 'state-exchange.json', 'finish-exchange.json', 'final-inbox.json', 'state-incoming', 'state-outgoing', 'state-observation.json', 'upload-observation.json', 'state-lock-observation.json', 'tool-paths.json', 'cases.toml', 'run-refusal.json', 'inbox.json', 'config.toml', 'rules.md', 'never-touch.toml', 'guardrails.md')}
        if args.command == 'reply':
            protected.add(Path(args.reply).resolve())
        if args.answers:
            protected.add(Path(args.answers).resolve())
        if getattr(args, 'draft_check_results', None):
            protected.add(Path(args.draft_check_results).resolve())
        question_path = store / ('.dry-run' if args.dry_run else '') / 'questions.json'
        if (store / 'rules.toml').exists():
            protected.add(store / 'rules.toml')
        protected = {p.resolve() for p in protected}
        question_is_input = question_path.resolve() in protected
        protected.add(question_path.resolve())
        protected |= {p.resolve() for p in store.glob('execution-*.json')}
        for name in (args.plan_out, args.report_out):
            if name:
                candidate = Path(name).expanduser().resolve()
                if candidate.parent == store and candidate.name.startswith('execution-') and candidate.suffix == '.json':
                    protected.add(candidate)
        # Keep the gate for the whole read/plan/write interval, excluding takeover.
        try:
            if getattr(args, 'dry_run', False) and args.report_out:
                raise ValueError('--report-out: a dry run writes no report; omit --report-out')
            for flag, name in (('--plan-out', args.plan_out), ('--report-out', args.report_out)):
                if name and Path(name).expanduser().resolve() in protected:
                    raise ValueError(flag + ': expected a path other than an input, state or lock file')
            with lock_gate(store):
                require_lock(store, args.token)
                require_fetched(store, args.token)
                if getattr(args, 'dry_run', False) and args.plan_out:
                    destination = Path(args.plan_out).expanduser().resolve()
                    if destination.is_file():
                        old = parse_json(read_text(destination), destination)
                        if not old.get('dry_run') and old.get('approval_state') in ('awaiting approval', 'approved'):
                            protected.add(destination)
                            raise ValueError('dry run: real approval plan at --plan-out; use a separate rehearsal file')
                diagnostics = []
                config, rules, holds = load_safety(sources, diagnostics)
                if (store / 'rules.toml').exists():
                    diagnostics.append(LEGACY_RULES)
                try:
                    listing = json.loads(load_inputs([args.inbox])[0])
                except ValueError as error:
                    raise ValueError(str(args.inbox) + ': expected a JSON Inbox object') from error
                state = store / 'state.json'
                try:
                    previous = json.loads(read_text(state)) if state.exists() else {}
                except ValueError as error:
                    raise ValueError(str(state) + ': expected a JSON account-count object') from error
                if not isinstance(previous, dict):
                    raise ValueError(str(state) + ': expected account keys and non-negative integer counts')
                previous = recover_drafts(store, previous, args.token)
                for account, count in previous.items():
                    if account in {'version', '_positions', '_drafts', '_run_counter', '_next_number'}:
                        continue
                    if type(count) is not int or count < 0:
                        raise ValueError(str(state) + ': state.json.' + account + ': expected a non-negative integer count')
                first_run = not state.exists() and not any((store / name).exists() for name in ('numbers.json', *LOG_NAMES))
                stops = ['Missing state; prior run records exist'] if not state.exists() and not first_run else []
                answers = None
                questions = [] if args.answers is None else None
                asked = None
                if args.answers is not None:
                    try:
                        answers = parse_json(load_inputs([args.answers])[0], args.answers)
                    except (OSError, ValueError, RecursionError):
                        answers = {}
                    if answers is None:
                        answers = {}
                    # The answer authorizes the row this run's question showed, not a later edit.
                    asked = read_asked(store, args.token, dry_run=args.dry_run)
                hand_sent = None
                if args.command == 'plan':
                    validate_inputs(listing, config, previous)
                    unchecked = {}
                    calls, records = hand_sent_calls(listing, rules, config, previous, store, unchecked)
                    if calls:
                        import execute
                        exchange_path = store / ('.dry-run' if args.dry_run else '') / 'listing-exchange.json'
                        binding = hashlib.sha256(json.dumps([args.token, listing, rules, holds, config, records, calls], sort_keys=True).encode()).hexdigest()
                        if not args.draft_check_results:
                            exchange_path.parent.mkdir(mode=0o700, exist_ok=True)
                            exchange = execute.read(exchange_path) if exchange_path.exists() else {}
                            exchange['draft_check'] = dict(binding=binding, requested=datetime.now(timezone.utc).timestamp())
                            execute.atomic(exchange_path, exchange)
                            print(json.dumps(dict(calls=calls, next='plan with --draft-check-results FILE')))
                            return
                        exchange = execute.read(exchange_path) if exchange_path.exists() else {}
                        request = exchange.get('draft_check', {})
                        known_keys(request, {'binding', 'requested'}, 'listing-exchange.json.draft_check')
                        if type(request.get('requested')) not in (int, float) or not math.isfinite(request['requested']) or request['requested'] < 0:
                            raise ValueError('listing-exchange.json.draft_check.requested: expected a finite request time')
                        if request.get('binding') != binding or Path(args.draft_check_results).stat().st_mtime < request.get('requested', float('inf')):
                            raise ValueError('draft-check-results: request a fresh reading for this run first')
                        values = gateway_results(args.draft_check_results, len(calls))
                        filing, closed, failed = hand_sent_results(records, values, rules, config)
                        hand_sent = dict(closed=[k for k in closed if k[0] not in failed], filed=[list(k) for k in filing if k[0] not in failed])
                        if failed:
                            hand_sent['failed'] = failed
                            listing = after_send_listing(listing, failed)
                    elif args.draft_check_results:
                        raise ValueError('draft-check-results: no open after_send records')
                    if unchecked:
                        hand_sent = (hand_sent or dict(closed=[], filed=[])) | dict(unchecked=unchecked)
                try:
                    result = build_plan(listing, rules if args.command == 'plan' else [], holds, config, previous, stops, answers=answers, run_token=args.token,
                                        questions=questions if args.command == 'plan' else None, asked=asked, hand_sent=hand_sent, store=store)
                except ValueError as error:
                    message = str(error)
                    if message.startswith('rules.md:') or message.startswith('never-touch.toml:'):
                        name, message = message.split(':', 1)
                        source = (args.rules or store / name) if name == 'rules.md' else (args.never_touch or store / name)
                    elif message.startswith('config'):
                        source = args.config or store / 'config.toml'
                    elif message.startswith('state.json'):
                        source = state
                    else:
                        source = args.inbox
                    raise ValueError(str(source) + ': ' + message.strip()) from error
                if args.command == 'reply':
                    try:
                        if args.scheduled:
                            raise ValueError('Replies require a user-initiated run')
                        try:
                            with Path(args.reply).open(encoding='utf-8') as stream:
                                reply_text = stream.read(REPLY_LIMIT + 1)
                        except UnicodeError:
                            raise ValueError('--reply: expected UTF-8 text') from None
                        parts = parse_reply(reply_text)
                        result = reply_plan(store, listing, holds, config, previous, parts, stops,
                                            answers=answers, run_token=args.token, questions=questions, asked=asked)
                        import execute
                        if not args.dry_run and not questions:
                            execute.atomic(store / 'reply.json', dict(token=args.token, parts=parts))
                    except ValueError as error:
                        refusal = str(error)
                        result = build_plan(listing, [], holds, config, previous, ['Reply refused: ' + ' '.join(refusal.split())])
                if questions:
                    import execute
                    if question_is_input or question_path.parent.is_symlink():
                        raise ValueError('questions: expected a private output path distinct from run inputs')
                    if args.plan_out:
                        output = private_store(args.plan_out, output=True)
                        if output.is_file():
                            old = parse_json(read_text(output), output)
                            if (isinstance(old, dict) and old.get('run_token') == args.token
                                    and old.get('dry_run', False) == args.dry_run):
                                output.unlink()
                    question_path.parent.mkdir(mode=0o700, exist_ok=True)
                    execute.atomic(question_path, dict(run=args.token, questions=questions))
                    print(json.dumps(dict(questions=str(question_path))))
                    return
                result['rule_diagnostics'] = diagnostics
                if args.command == 'plan' and config.get('calendars'):
                    data = parse_json(load_inputs([args.calendar])[0], args.calendar) if args.calendar else {}
                    result['calendar_section'] = calendar_section(data, config, now or datetime.now(timezone.utc))
                if answers is not None and result.get('answers_used'):
                    result['answers'] = answers
                result['run_id'] = secrets.token_hex(32)
                result['run_token'] = args.token
                if result.get('approval_state') == 'awaiting approval':
                    result['approval_created'] = (now or datetime.now(timezone.utc)).timestamp()
                if getattr(args, 'dry_run', False):
                    result['dry_run'] = True
                    result['dry_listing'] = listing
                    outcomes = number_decisions(store, result, listing, read_only=True)
                else:
                    outcomes = number_decisions(store, result, listing, language=config.get('language', 'en'))
                if outcomes is not None:
                    for outcome in outcomes:
                        outcome.pop('saved_number', None)
                result['first_run'] = first_run
                if args.scheduled: result['scheduled'] = True
                report = render_report(result, listing, config.get('language', 'en'), store, new_question=True, outcomes=outcomes)
                if args.scheduled and 'schedule' in config:
                    now = now or datetime.now(timezone.utc)
                    slot = schedule_slot(config, now)
                    delay = int((now.timestamp() - slot.timestamp()) // 60)
                    if now.timestamp() - slot.timestamp() > 15 * 60:
                        report = 'Late start: slot ' + slot.strftime('%Y-%m-%d %H:%M %Z') + ', delay ' + str(delay) + ' minutes\n' + report
                sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
                if args.command == 'reply':
                    if result['reasons'] and result.get('approval_state') != 'awaiting approval':
                        print(chat_text(result['reasons'][0]).rstrip('.') + '. Type each listed number once followed by trash, archive, spam, move to <existing folder>, leave, yes or no' + (', send or forward HANDLE' if config.get('allow_send', False) else '') + '; separate parts with commas. For a stale, protected or unavailable item, request a fresh run and use an eligible listed number.' + (' Dry run: render the rehearsal message and simulate it; no reply is acted on.' if args.dry_run else ' Next: execute.py next, commit-state, message, send-state and release under this lock.'))
                    source = result.get('reply_source')
                    if source:
                        language = config.get('language', 'en')
                        for part in source['parts']:
                            entries = [e for e in result['reply'] if e['number'] == part['number']]
                            saved = next(i for i in source['numbers']['items'] if i['number'] == part['number'])
                            selected_accounts = {m['account'] for m in saved.get('members', [saved])}
                            protected = [d for d in result['decisions'] if d['reason']=='Protected by never-touch' and any((d['account'],d['id'])==(e['account'],e['id']) for e in entries)]
                            protected_keys = {(d['account'], d['id']) for d in protected}
                            counts = {a: sum(e['account'] == a and (e['account'], e['id']) not in protected_keys for e in entries) for a in config['accounts'] if a in selected_accounts}
                            skipped = len(saved.get('members', [saved])) - len(entries)
                            accounts = ', '.join(account_name(result, a) + ': ' + str(n) + ((' message' if n == 1 else ' messages') if language == 'en' else (' mensaje' if n == 1 else ' mensajes')) for a, n in counts.items())
                            verb = ({'send-draft': 'send' if language == 'en' else 'enviar', 'forward': 'forward' if language == 'en' else 'reenviar'}.get(part['verb'], part['verb'])) + (' to ' + part['target'] if part['target'] else '')
                            print(str(part['number']) + ' ' + verb + ': ' + accounts + '; ' + str(skipped) + (' skipped as gone.' if language == 'en' else ' omitido' + ('s' if skipped != 1 else '') + ' por no estar en Recibidos.'))
                            for decision in protected:
                                message = next(m for m in listing['messages'] if (m['account'], m['id']) == (decision['account'], decision['id']))
                                print(str(part['number']) + ': ' + chat_text(message['sender'])[:160] + ' · ' + chat_text(message['subject'])[:240] + ' · ' + ('Protected by never-touch' if language == 'en' else 'Protegido por never-touch') + (' · ' + safe_text(decision['note']) if 'note' in decision else ''))
                        if result.get('approval_state') == 'awaiting approval':
                            print('Nothing has run yet.' if language == 'en' else 'Todavía no se ha ejecutado nada.')
                            print(approval_question(result, language))
                payload = json.dumps(result, ensure_ascii=True, indent=2) + '\n'
                outputs = []
                temporary = []
                try:
                    for name, content in ((args.plan_out, payload), (None if getattr(args, 'dry_run', False) else args.report_out, report)):
                        if name is None:
                            continue
                        destination = private_store(name, output=True)
                        if not destination.parent.is_dir() or destination.is_dir():
                            raise ValueError(str(destination) + ': output expected a file in an existing folder')
                        if destination in [p for p, _ in outputs]:
                            raise ValueError(str(destination) + ': Output paths must differ; expected distinct --plan-out, --report-out and state paths')
                        outputs.append((destination, content))
                    for destination, content in outputs:
                        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent, delete=False) as out:
                            temporary.append(Path(out.name))
                            out.write(content)
                    for source, (destination, _) in zip(temporary, outputs):
                        if not getattr(args, 'dry_run', False) and destination == Path(args.plan_out or '').resolve() and destination.exists():
                            old = parse_json(read_text(destination), destination)
                            if old.get('approval_state') == 'awaiting approval':
                                if time.time() - question_created(store, old) > approval_hours(config) * 3600:
                                    marker = ' approval expired: ' + old['run_id']
                                    history = store / 'history.log'
                                    if not history.exists() or not any(marker in line for line in read_text(history).splitlines()):
                                        approval_event(store, old, 'expired')
                        source.replace(destination)
                    if not getattr(args, 'dry_run', False) and args.plan_out:
                        history = store / 'history.log'
                        events = [line.split(' approval ', 1)[1] for line in read_text(history).splitlines() if ' approval ' in line] if history.exists() else []
                        asked = [event.split(' ')[1] for event in events if event.startswith('asked: ')]
                        if asked and not any(event.split(' ')[:2] == [kind + ':', asked[-1]] for event in events for kind in ('approved', 'skipped', 'expired', 'replaced', 'stale')):
                            approval_event(store, {'run_id': asked[-1]}, 'replaced')
                    if not getattr(args, 'dry_run', False) and args.plan_out and result.get('approval_state') == 'awaiting approval':
                        approval_event(store, result, 'asked')
                    if getattr(args, 'dry_run', False) and result.get('approval_state') == 'awaiting approval':
                        print('question_id: ' + result['run_id'])
                    print(('' if args.plan_out else payload) + ('' if args.report_out else report), end='', flush=True)
                except Exception:
                    for destination, _ in outputs:
                        if destination != state and destination.is_file():
                            destination.unlink()
                    raise
                finally:
                    for source in temporary:
                        source.unlink(missing_ok=True)
        except Exception:
            for name in (args.plan_out, None if getattr(args, 'dry_run', False) else args.report_out):
                if name:
                    try:
                        destination = private_store(name, output=True)
                        if destination.is_file() and destination not in protected:
                            destination.unlink()
                    except (ValueError, OSError):
                        pass
            raise


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AttributeError, IndexError, StopIteration) as error:
        print('Refused: malformed input file: expected complete records with valid field types (' + type(error).__name__ + ')', file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as error:
        print('Refused: ' + ' '.join(str(error).split()), file=sys.stderr)
        sys.exit(1)
