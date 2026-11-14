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
PLAN_VERBS = (VERBS - {'draft', 'report'}) | {'create-folder', 'create-draft'}
DRAFT_LIMIT = 10
RULE_LIMIT_CAP = 50
BRAKE_FLOOR = 10
BODY_LIMIT = 2000
QUESTION_LIMIT = 500
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


def parse_toml(text, source):
    try:
        return tomllib.loads(text)
    except ValueError as error:
        raise ValueError(str(source) + ': expected valid TOML') from error


def trim_logs(store):
    # Only at acquisition or completed fetch, before this run can append records.
    import execute
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
               'rules.toml', 'never-touch.toml', 'tool-paths.json', 'plan.json', 'inbox.json', 'state.json')
STATE_LIMIT = 4 * 1024 * 1024


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
    elif name in ('rules.toml', 'never-touch.toml'):
        try:
            entries = parse_rules(text, protection=name == 'never-touch.toml')
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
            data = parse_json(line, folder / name)
            if name == 'decisions.jsonl':
                if not isinstance(data, dict) or set(data) != {'run', 'time', 'parts'} or not lock_text(data['run'], 512):
                    raise ValueError(name + ': invalid batch')
                stamp = state_timestamp(data['time'], folder / name)
                if stamp.utcoffset() != timedelta(0) or not isinstance(data['parts'], list):
                    raise ValueError(name + ': expected UTC time and parts')
                for part in data['parts']:
                    if (not isinstance(part, dict) or set(part) != {'number', 'account', 'id', 'verb', 'target', 'outcome'}
                            or type(part['number']) is not int or part['number'] < 1
                            or part['account'] not in config['accounts'] or not isinstance(part['id'], str)
                            or part['verb'] not in {'trash', 'archive', 'spam', 'move', 'leave', 'yes', 'no'}
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
            if not re.fullmatch(r'[0-9T:.+Z-]{20,40} (?:skipped: no new mail|abandoned: earlier lock exchange|approval (?:asked|skipped|expired|replaced|stale): [0-9a-f]{64}|approval approved: [0-9a-f]{64} [0-9a-f]{64})', line):
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
                    elif n not in ('rules.toml', 'never-touch.toml'):
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
            return {'done': True}
        if observation is None:
            contents = {n: state_bytes(store / n) for n in STATE_FILES if (store / n).exists()}
            plan_path = store / 'plan.json'
            plan = parse_json(contents['plan.json'].decode('utf-8'), plan_path) if contents.get('plan.json') else {}
            events = [line.split(' approval ', 1)[1] for line in contents.get('history.log', b'').decode('utf-8').splitlines() if ' approval ' in line]
            waiting = (plan.get('approval_state') == 'awaiting approval' and not plan.get('dry_run')
                       and events and events[-1] == 'asked: ' + str(plan.get('run_id'))
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


def validate_holds(holds):
    for i, hold in enumerate(holds, 1):
        try:
            if not isinstance(hold, dict):
                raise ValueError('Never-touch entry must be a table')
            if set(hold) - {'accounts', 'sender', 'domain', 'subject', 'has_attachment'}:
                known_keys(hold, {'accounts', 'sender', 'domain', 'subject', 'has_attachment'}, 'never-touch')
            validate_predicate(hold)
        except ValueError as error:
            raise ValueError(('never-touch entry ' + str(i)) + ': ' + str(error)) from error


def validate_predicate(rule):
    if 'accounts' in rule and (not isinstance(rule['accounts'], list) or not rule['accounts']
                               or any(not isinstance(a, str) for a in rule['accounts'])):
        raise ValueError('accounts: Accounts must be a non-empty list of configured account handles')
    if 'has_attachment' in rule and not isinstance(rule['has_attachment'], bool):
        raise ValueError('has_attachment must be boolean')
    if 'exclusions' in rule and (not isinstance(rule['exclusions'], list)
                                or any(not isinstance(x, str) for x in rule['exclusions'])):
        raise ValueError('exclusions: Exclusions must be a list of strings')

    for key in ('sender', 'domain', 'subject', 'body'):
        if key in rule and (not isinstance(rule[key], str) or not rule[key].strip()):
            raise ValueError(key + ': expected a non-empty string')
    if not any(k in rule for k in ('sender', 'domain', 'subject', 'body')):
        raise ValueError('Rule requires a message predicate: expected sender, domain, subject or body')
    for key in ('subject',):
        try:
            if key in rule and re.search(rule[key], '', re.IGNORECASE):
                raise ValueError(key + ': Predicate matches empty text; expected a regex that does not match empty text')
        except re.error as error:
            raise ValueError(key + ': Invalid ' + key + ' regex; expected a valid Python regular expression') from error


def parse_rules(text, *, protection=False):
    data = parse_toml(text, 'never-touch.toml' if protection else 'rules.toml')
    known_keys(data, {'rules', 'allow_empty'} if protection else {'rules'}, 'rule file')
    if 'allow_empty' in data and type(data['allow_empty']) is not bool:
        raise ValueError('allow_empty: expected boolean')
    if not protection and 'rules' not in data:
        raise ValueError('rules: expected an array (rules = [] for no rules)')
    entries = data.get('rules', [])
    if not isinstance(entries, list) or (protection and not entries and data.get('allow_empty') is not True):
        raise ValueError('rules: expected an array of tables' if not isinstance(entries, list) else 'Never-touch requires entries or allow_empty = true')
    (validate_holds if protection else validate_rules)(entries)
    return entries


def table_cells(line):
    """Split pipe rows while preserving code spans and escaped pipes."""
    line = line.strip()
    if not line.startswith('|'):
        return None
    cells, cell, fence = [], '', ''
    i = 1
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
            if not fence:
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


def convert_rules(table, scopes_path, out_path, review_path=None):
    scopes = parse_toml(read_text(scopes_path), 'scopes')
    if any(not isinstance(v, list) or not v or
           any(not isinstance(a, str) or not a.strip() for a in v) for v in scopes.values()):
        raise ValueError('scopes: expected nonempty lists of account handles')
    out = Path(out_path)
    if out.exists() or out.is_symlink():
        raise ValueError('out: file already exists')
    if review_path and Path(review_path).resolve() in {out.resolve(), Path(table).resolve(), Path(scopes_path).resolve()}:
        raise ValueError('review: expected a distinct output path')
    lines = read_text(table).splitlines()
    entries, blocks, reviews, seen = [], [], [], set()
    rows = active = disabled = 0
    in_table = False
    for index, line in enumerate(lines):
        cells = table_cells(line)
        separator = cells and len(cells) == 5 and all(re.fullmatch(r':?-{3,}:?', c) for c in cells)
        if separator:
            in_table = index > 0 and len(table_cells(lines[index - 1]) or []) == 5
            continue
        if not cells:
            in_table = False
            continue
        # Headers immediately preceding separators belong to the next table.
        following = table_cells(lines[index + 1]) if index + 1 < len(lines) else None
        if following and len(following) == 5 and all(re.fullmatch(r':?-{3,}:?', c) for c in following):
            continue
        if not in_table:
            continue
        rows += 1
        identity = cells[0] if cells else ''
        reason = None
        if identity in seen:
            reason = 'duplicate id'
        seen.add(identity)
        try:
            if reason:
                raise ValueError(reason)
            if len(cells) != 5:
                raise ValueError('expected five cells')
            identity, pattern, action, scope, source = cells
            if not identity.strip() or any(unicodedata.category(c) == 'Cc' for c in identity):
                raise ValueError('invalid id')
            spans = list(re.finditer(r'(`+)(.+?)\1(?!`)', pattern))
            tokens = [m[2] for m in spans]
            note, start = '', 0
            for span in spans:
                note += pattern[start:span.start()]
                start = span.end()
            note += pattern[start:]
            if not tokens:
                raise ValueError('no tokens')
            predicates = []
            for token in tokens:
                if token.startswith('subject:') and token[8:].strip():
                    predicates.append({'subject': re.escape(token[8:])})
                elif re.fullmatch(r"[^\s@<>;,`]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", token):
                    predicates.append({'sender': token})
                elif re.fullmatch(r'@?(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}', token):
                    predicates.append({'domain': token.lstrip('@')})
                else:
                    raise ValueError('unknown pattern')
            if len(tokens) > 1 and any('subject' in p for p in predicates):
                raise ValueError('ambiguous patterns')
            accounts = []
            for name in scope.split(','):
                if name.strip() not in scopes:
                    raise ValueError('unknown scope')
                accounts.extend(a for a in scopes[name.strip()] if a not in accounts)
            parts = [p.strip() for p in action.split('+')]
            base = parts[0].casefold()
            details = {}
            if base in ('trash', 'archive', 'spam'):
                verb = base
            elif re.fullmatch(r'(?:move|mover)\s+.+', parts[0], re.I):
                verb = 'move'
                details['target'] = folder_path(parts[0].split(None, 1)[1], template=True)
            elif base in ('unsubscribe', 'baja') or re.fullmatch(r'baja https://\S+', parts[0], re.I):
                verb = 'unsubscribe'
            else:
                raise ValueError('unknown action')
            if len(parts) > 1:
                if len(parts) != 2 or verb != 'unsubscribe' or parts[1].casefold() not in ('trash', 'archive'):
                    raise ValueError('unknown action')
                details['after_unsubscribe'] = parts[1].casefold()
            batch = []
            for n, predicate in enumerate(predicates, 1):
                rule = dict(id=identity if n == 1 else f'{identity}-{n}', accounts=accounts,
                            action=verb, **predicate, **details)
                if verb == 'trash':
                    rule['exclusions'] = []
                if note.strip():
                    rule['enabled'] = False
                batch.append(rule)
            try:
                validate_rules(batch)
            except ValueError:
                raise ValueError('invalid rule') from None
            if any(r['id'] in {e['id'] for e in entries} for r in batch):
                raise ValueError('duplicate id')
            entries.extend(batch)
            comments = []
            for label, text in (('Source', source), ('Note', note.strip())):
                if text:
                    clean = ''.join(c if unicodedata.category(c) not in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} else ' ' for c in text)
                    comments.append('# ' + label + ': ' + clean)
            for rule in batch:
                blocks.append('\n'.join(comments + ['[[rules]]'] +
                              [k + ' = ' + (str(v).lower() if isinstance(v, bool) else json.dumps(v, ensure_ascii=False))
                               for k, v in rule.items()]))
            if note.strip():
                disabled += 1
            else:
                active += 1
        except ValueError as error:
            # Only fixed reasons are emitted; cells never become diagnostic text.
            reason = str(error)
            if reason.startswith('target:'):
                reason = 'invalid target'
            reviews.append(f'{safe_text(identity)}\tline {index + 1}\t{reason}')
    content = '\n\n'.join(blocks) + '\n' if blocks else 'rules = []\n'
    parse_rules(content)
    review = '\n'.join(reviews) + ('\n' if reviews else '')
    if review_path:
        with Path(review_path).open('x', encoding='utf-8') as target:
            target.write(review)
    with out.open('x', encoding='utf-8') as target:
        os.chmod(target.name, 0o600)
        try:
            target.write(content)
            target.flush()
            parse_rules(read_text(out))
        except (ValueError, OSError):
            out.unlink()
            raise
    if not review_path:
        print(review, end='')
    print(f'rows read: {rows}, active: {active}, switched off: {disabled}, sent to review: {len(reviews)}')


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


def validate_rules(rules):
    ids = set()
    for i, rule in enumerate(rules, 1):
        try:
            if not isinstance(rule, dict):
                raise ValueError('Rule entry must be a table')
            if any(not isinstance(rule.get(k), str) or not rule[k].strip() for k in ('id', 'action')):
                raise ValueError('Rule id and action must be non-empty strings')
            if 'target' in rule and (rule['action'] != 'move' or not isinstance(rule['target'], str)):
                raise ValueError('rule.target: expected a folder name string on move rules only')
            if set(rule) - {'id', 'action', 'accounts', 'sender', 'domain', 'subject', 'has_attachment', 'exclusions', 'target', 'after_unsubscribe', 'body', 'question', 'limit', 'enabled', 'draft', 'draft_text', 'draft_to'}:
                known_keys(rule, {'id', 'action', 'accounts', 'sender', 'domain', 'subject', 'has_attachment', 'exclusions', 'target', 'after_unsubscribe', 'body', 'question', 'limit', 'enabled', 'draft', 'draft_text', 'draft_to'}, 'rule')
            if rule['action'] == 'draft':
                if rule.get('draft') not in ('reply', 'forward'):
                    raise ValueError('draft: expected reply or forward')
                text = rule.get('draft_text')
                if not isinstance(text, str) or not 1 <= len(text) <= 4000 or not text.strip():
                    raise ValueError('draft_text: expected 1 to 4000 characters')
                recipients = rule.get('draft_to')
                if rule['draft'] == 'forward':
                    if (not isinstance(recipients, list) or not 1 <= len(recipients) <= 5
                            or any(not isinstance(a, str) or normalize_sender(a) != a.casefold() or any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in a) for a in recipients)):
                        raise ValueError('draft_to: expected 1 to 5 bare addresses')
                elif 'draft_to' in rule:
                    raise ValueError('draft_to: refused on reply')
            else:
                for key in ('draft', 'draft_text', 'draft_to'):
                    if key in rule:
                        raise ValueError(key + ': expected action draft')
            if 'enabled' in rule and type(rule['enabled']) is not bool:
                raise ValueError('enabled: expected boolean')
            if 'limit' in rule and (type(rule['limit']) is not int or not 1 <= rule['limit'] <= RULE_LIMIT_CAP):
                raise ValueError('limit: expected an integer from 1 to 50')
            if 'target' in rule:
                folder_path(rule['target'], template=True)
            if 'question' in rule and (rule['action'] != 'report'
                    or not isinstance(rule['question'], str) or not rule['question'].strip()
                    or len(rule['question']) > QUESTION_LIMIT):
                raise ValueError('question: expected a non-empty string of at most 500 characters on report rules only')
            if 'after_unsubscribe' in rule and (rule['action'] != 'unsubscribe' or rule['after_unsubscribe'] not in ('trash', 'archive')):
                raise ValueError('after_unsubscribe: expected trash or archive on unsubscribe rules')
            if rule['id'] in ids:
                raise ValueError('Duplicate rule id; expected a unique id')
            ids.add(rule['id'])
            if rule['action'] not in VERBS:
                raise ValueError('rule.action: expected one of ' + ', '.join(sorted(VERBS)))
            if not rule.get('accounts'):
                raise ValueError('accounts: Rule requires explicit account scope; expected a non-empty array of configured account handles')
            if rule['action'] == 'trash' and 'exclusions' not in rule:
                raise ValueError('Trash rule requires exclusions: expected an array of strings (use [] for none)')
            validate_predicate(rule)
        except ValueError as error:
            raise ValueError(('rule ' + repr(rule.get('id', i)) if isinstance(rule, dict) else 'rule ' + str(i)) + ': ' + str(error)) from error


def matches(message, rule, protection=False):
    if not protection and not rule.get('enabled', True):
        return False
    if (not protection or 'accounts' in rule) and message['account'] not in rule.get('accounts', []):
        return False
    sender = message['sender'].casefold()
    if protection:
        local, domain = sender.rsplit('@', 1)
        sender = local.split('+', 1)[0] + '@' + domain
    if 'sender' in rule and sender != rule['sender'].casefold().rstrip('.'):
        return False
    if 'domain' in rule:
        domain = sender.rsplit('@', 1)[-1]
        wanted = rule['domain'].casefold().rstrip('.')
        if domain != wanted and not (protection and domain.endswith('.' + wanted)):
            return False
    if 'subject' in rule and not re.search(rule['subject'], message['subject'], re.IGNORECASE):
        return False
    if 'body' in rule:
        if 'body' not in message:
            return False
        # Escaping every operator makes this a literal search without backtracking.
        if not re.search(re.escape(rule['body']), message['body'], re.IGNORECASE):
            return False
    if 'has_attachment' in rule and message.get('has_attachment', False) != rule['has_attachment']:
        return False
    return not any(word.casefold() in text.casefold()
                   for word in rule.get('exclusions', [])
                   for text in [message['subject']] + ([message['body']] if 'body' in rule else []))


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
    elif schedule_slot(config, now).strftime('%H:%M') in config['schedule']['always_full']:
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
            and schedule_slot(config, now).strftime('%H:%M') not in config['schedule']['always_full']
            and not (store / 'run-message.md').exists()):
        decision['quiet_accounts'] = observed[2]
    if dry_run:
        decision['dry_run'] = True
        decision['positions'] = observed[0] if observed else {}
    elif token is not None:
        execute.atomic(path, dict(token=token, decision=decision, positions=observed[0] if observed else None))
    return decision


def message_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_=+/-]{1,512}', value) is not None


def validate_drafts(records, config):
    if not isinstance(records, list):
        raise ValueError('state.json._drafts: expected array')
    seen = set()
    for record in records:
        known_keys(record, {'account', 'id', 'draft_id', 'rule', 'created'}, 'state.json._drafts')
        if (set(record) != {'account', 'id', 'draft_id', 'rule', 'created'}
                or record['account'] not in config['accounts'] or not message_id(record['id'])
                or (record['draft_id'] is not None and not message_id(record['draft_id']))
                or not isinstance(record['rule'], str)):
            raise ValueError('state.json._drafts: expected account, message id, draft id, rule and creation time')
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
    known_keys(config, {'version', 'accounts', 'language', 'inbox_limit', 'timezone', 'schedule', 'lock', 'state', 'relist_inbox', 'calendar', 'calendars', 'approval_hours'}, 'config')
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
        for key in ('weekdays', 'weekends', 'always_full'):
            times = schedule.get(key)
            if (not isinstance(times, list) or any(not isinstance(t, str) or
                    not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', t) for t in times)
                    or len(times) != len(set(times))
                    or (key != 'always_full' and not times)):
                raise ValueError('config.schedule.' + key + ': expected unique HH:MM times (nonempty day slots)')
        if not set(schedule['always_full']) <= set(schedule['weekdays'] + schedule['weekends']):
            raise ValueError('config.schedule.always_full: expected configured slot times')
    accounts = config.get('accounts')
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError('config.accounts: expected a non-empty table')
    for account, fields in accounts.items():
        if account in {'version', '_positions', '_drafts'}:
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
    if not isinstance(skipped, dict) or set(skipped) - set(config['accounts']) or any(not isinstance(r, str) or not re.fullmatch(r'(?:provider error \([A-Za-z0-9 _-]{1,40}\)|pigeon error \((?:manual skip|listing answer|no new mail)\))', r) for r in skipped.values()):
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
        flags = {'has_attachment', 'spam', 'phishing', 'unsubscribe_one_click'}
        strings = required | {'body', 'sender_name', 'unsubscribe_url', 'date'}
        known_keys(message, strings | flags, label)
        for key in required | set(message):
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
    known_keys(previous, set(accounts) | {'version', '_positions', '_drafts'}, 'state.json')
    validate_drafts(previous.get('_drafts', []), config)
    if '_positions' in previous:
        validate_positions(previous['_positions'], config)
    if any(type(n) is not int or n < 0 for a, n in previous.items() if a not in {'version', '_positions', '_drafts'}):
        raise ValueError('state.json: expected non-negative integer counts')


def build_plan(listing, rules, never_touch, config, previous=None, stop_reasons=(), *, selections=None):
    validate_inputs(listing, config, previous if previous is not None else {})
    validate_rules(rules)
    validate_holds(never_touch)
    for rule in rules + never_touch:
        if any(a not in config['accounts'] for a in rule.get('accounts', [])):
            raise ValueError(('rules.toml: rule ' + repr(rule['id']) if 'id' in rule else 'never-touch.toml: entry ' + str(never_touch.index(rule) + 1)) + ': account ' + repr(next(a for a in rule['accounts'] if a not in config['accounts'])) + ' expected one of config.toml accounts: ' + ', '.join(config['accounts']))
    messages = listing['messages']
    totals = listing['accounts']
    not_listed = [a for a in config['accounts'] if a not in totals and a not in listing.get('quiet', [])]
    if set(totals) - set(config['accounts']) or not set(totals) & set(config['accounts']):
        return {'account_names': {a: f.get('name', a) for a, f in config['accounts'].items()}, 'relist_inbox': config.get('relist_inbox', 'all'),
                'remaining': [dict(account=m['account'], id=m['id'], sender=m['sender'], subject=m['subject'], reason='Run stopped') for m in messages], 'actions': [],
                'decisions': [{'account': m['account'], 'id': m['id'], 'reason': 'Protected by never-touch' if any(matches(m, h, protection=True) for h in never_touch) else 'Run stopped'} for m in messages],
                'reasons': list(stop_reasons) + ['Listing has an unconfigured account' if set(totals) - set(config['accounts']) else 'No configured account listed'],
                'not_listed': not_listed, 'skip_reasons': {a:listing.get('skipped', {}).get(a, 'pigeon error (listing answer)') for a in not_listed},
                'counts': {a: sum(m['account'] == a for m in messages) for a in totals}}
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
    actions, decisions, hits = [], [], {}
    for original in messages:
        sender = normalize_sender(original.get('sender'))
        message = dict(original, sender=sender)
        reason = 'No matching rule'
        question = {}
        if sender is None:
            reason = 'Invalid sender'
        elif any(matches(message, hold, protection=True) for hold in never_touch):
            reason = 'Protected by never-touch'
        else:
            candidates = rules if selections is None else [selections[(message["account"], message["id"])]] if (message["account"], message["id"]) in selections else []
            for rule in candidates:
                if selections is None and not matches(message, rule):
                    continue
                if rule['action'] == 'draft' and any(d['account'] == message['account'] and d['id'] == message['id'] for d in (previous or {}).get('_drafts', [])):
                    reason = 'Draft waiting'
                    break
                key = (rule['id'], message['account'])
                if 'question' not in rule:
                    hits[key] = hits.get(key, 0) + 1
                verb = rule['action']
                action = {'account': message['account'], 'id': message['id'], 'action': verb, 'rule': rule['id']}
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
                        raise ValueError('Inbox.folders.' + message['account'] + '.name: expected full path, not a destination leaf name')
                    if len(folders) > 1 or not target:
                        reason = 'Move target is ambiguous or missing'
                        break
                    if folders:
                        action['target'] = folders[0]['id']
                    elif '{year}' in rule['target'] or '{month}' in rule['target']:
                        action['target_path'] = folder_path(target)
                        actions.append(action | {'action': 'create-folder'})
                    else:
                        reason = 'Move target is ambiguous or missing'
                        break
                if verb == 'unsubscribe':
                    action['url'] = message.get('unsubscribe_url', '')
                    if not safe_unsubscribe_host(action['url']) or message.get('spam') or message.get('phishing'):
                        reason = 'Unsafe unsubscribe'
                        break
                if verb == 'report':
                    reason = 'Rule requests a decision'
                    question = {'question': rule['question']} if 'question' in rule else {}
                    break
                actions.append(action)
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
    for rule in rules:
        count = sum(a['rule'] == rule['id'] and a['action'] != 'create-folder' for a in actions)
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
        decisions = [{'account': m['account'], 'id': m['id'], 'reason': 'Protected by never-touch' if any(matches(m, h, protection=True) for h in never_touch) else 'Run stopped; rule refused by brake' if (m['account'], m['id']) in refused else 'Run stopped'} for m in messages]
    validate_actions(actions, listing)
    expected = dict(counts)
    for action in actions:
        if action['action'] in {'trash', 'archive', 'move', 'spam'}:
            expected[action['account']] -= 1
    result = {'actions': actions, 'decisions': decisions, 'reasons': reasons, 'counts': counts,
            'expected_counts': expected, 'not_listed': not_listed, 'skip_reasons': {a:listing.get('skipped', {}).get(a, 'pigeon error (listing answer)') for a in not_listed},
            'relist_inbox': config.get('relist_inbox', 'all'),
            'account_names': {a: f.get('name', a) for a, f in config['accounts'].items()}}
    if reasons and not unconditional:
        result['approval_state'] = 'awaiting approval'
        result['approval_thresholds'] = thresholds
    result['remaining'] = remaining_items(result, listing)
    for item in result['remaining']:
        for record in (previous or {}).get('_drafts', []):
            if (item['account'], item['id']) == (record['account'], record['id']):
                item['reason'] = 'Draft waiting since ' + record['created'] + ('; draft may exist; check Drafts by hand' if record['draft_id'] is None else '')
    return result


REMOVALS = {'trash', 'archive', 'move', 'spam'}


def recover_drafts(store, previous, token):
    previous = dict(previous)
    drafts = list(previous.get('_drafts', []))
    known = {(d['account'], d['id']) for d in drafts}
    path = store / 'calls.jsonl'
    entries = [parse_json(line, path) for line in (read_text(path).splitlines() if path.exists() else [])]
    failed = {(e['run'], e['account'], e['id']) for e in entries if e['verb'] == 'create-draft' and e['outcome'] == 'failed'}
    for entry in entries:
        key = (entry['account'], entry['id'])
        if (entry['run'] != token and entry['verb'] == 'create-draft' and entry['outcome'] == 'issued' and key not in known
                and (entry['run'], *key) not in failed):
            drafts.append(dict(account=key[0], id=key[1], draft_id=None, rule='uncertain-draft',
                               created=datetime.fromtimestamp(entry['time'], timezone.utc).isoformat()))
            known.add(key)
    if drafts: previous['_drafts'] = drafts
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
        if prior.get('reason', '').startswith('Draft waiting since '):
            item['reason'] = prior['reason']
        if statuses.get(key) == 'issued_unknown' and any(a['action'] == 'create-draft' and (a['account'], a['id']) == key for a in plan['actions']):
            item['reason'] = 'check Drafts by hand'
        items.append(item)
    return items


REPLY_LIMIT = 2000


def read_numbers(store):
    path = store / 'numbers.json'
    data = parse_json(read_text(path), path) if path.exists() else {'next': 1, 'items': []}
    known_keys(data, {'next', 'items'}, 'numbers.json')
    if type(data.get('next')) is not int or data['next'] < 1 or not isinstance(data.get('items'), list):
        raise ValueError('numbers.json: expected next positive integer and items array')
    seen, keys = set(), set()
    for item in data['items']:
        known_keys(item, {'number', 'account', 'id'}, 'numbers.json.items')
        n = item.get('number')
        if (type(n) is not int or not 0 < n < data['next'] or n in seen
                or any(not isinstance(item.get(k), str) for k in ('account', 'id'))):
            raise ValueError('numbers.json: expected unique saved numbers and account/id strings')
        key = (item['account'], item['id'])
        if key in keys:
            raise ValueError('numbers.json: duplicate message')
        seen.add(n)
        keys.add(key)
    return data


def number_decisions(store, plan, listing, *, read_only=False):
    import execute
    data = read_numbers(store)
    protected = {(i['account'], i['id']) for i in plan['decisions'] + plan.get('remaining', []) if i['reason'] == 'Protected by never-touch'}
    present = {(i['account'], i['id']) for i in plan.get('remaining', plan['decisions']) + plan.get('reply', [])} - protected
    data['items'] = [i for i in data['items'] if i['account'] not in listing['accounts'] or (i['account'], i['id']) in present]
    lookup = {(i['account'], i['id']): i['number'] for i in data['items']}
    log = store / 'decisions.jsonl'
    kept = set()
    for line in read_text(log).splitlines() if log.exists() and any(i['reason'] == 'No matching rule' for i in plan.get('remaining', [])) else []:
        for part in parse_json(line, log)['parts']:
            if part['verb'] in {'leave', 'yes', 'no'} and part['outcome'] == 'answered':
                kept.add((part['account'], part['id']))
    for item in plan.get('remaining', plan['decisions']):
        if item['reason'] == 'Protected by never-touch':
            continue
        key = (item['account'], item['id'])
        if plan.get('approval_state') == 'awaiting approval' and any((a['account'], a['id']) == key for a in plan['actions']):
            continue
        if key in kept and item['reason'] == 'No matching rule':
            item['reason'] = 'Kept by owner reply'
        if not read_only or key in lookup:
            item.setdefault('previously_listed', key in lookup)
        if read_only:
            continue
        if key not in lookup and item['account'] in listing.get('skipped', {}) and key not in {(d['account'], d['id']) for d in plan['decisions']}:
            continue
        if key not in lookup:
            lookup[key] = data['next']
            data['items'].append(dict(number=data['next'], account=key[0], id=key[1]))
            data['next'] += 1
        item['number'] = lookup[key]
        for decision in plan['decisions']:
            if (decision['account'], decision['id']) == key:
                decision['number'] = lookup[key]
    if not read_only:
        execute.atomic(store / 'numbers.json', data)


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
        if verb.split()[0] in {'send', 'resend', 'transmit', 'submit', 'deliver', 'dispatch', 'reply', 'forward', 'email', 'mail'}:
            raise ValueError('The owner sends drafts by hand from the mailbox.')
        fields = command.split(maxsplit=2)
        if len(fields) == 3 and fields[0].casefold() == 'move' and fields[1].casefold() == 'to':
            verb, target = 'move', folder_path(fields[2].strip())
        elif verb not in {'trash', 'archive', 'spam', 'leave', 'yes', 'no'}:
            raise ValueError('Unknown reply verb or malformed part')
        parts.append(dict(number=number, verb=verb, target=target))
    return parts


def reply_plan(store, listing, holds, config, previous, parts, stops=()):
    # Reuse listing validation, never-touch precedence, ceilings and brakes.
    numbers = {i['number']: i for i in read_numbers(store)['items']}
    messages = {(m['account'], m['id']): m for m in listing['messages']}
    selections, entries = {}, []
    for part in parts:
        item = numbers.get(part['number'])
        if item is None:
            raise ValueError('Unknown number')
        key = (item['account'], item['id'])
        if key not in messages:
            raise ValueError('Number is not in the fresh Inbox for its saved account')
        message = dict(messages[key], sender=normalize_sender(messages[key]['sender']))
        if message['sender'] is None or any(matches(message, h, protection=True) for h in holds):
            raise ValueError('Number is protected by never-touch or has an invalid sender')
        entry = dict(part, account=key[0], id=key[1])
        entries.append(entry)
        if part['verb'] in {'leave', 'yes', 'no'}:
            continue
        selections[key] = dict(id='numbered-reply', accounts=[key[0]], sender=message['sender'], action=part['verb'], exclusions=[], **({'target': part['target']} if part['target'] is not None else {}))
    # One reply batch has the existing default per-rule limit of ten.
    ceiling_rule = dict(id='numbered-reply', accounts=list(config['accounts']), subject='reply', action='report')
    result = build_plan(listing, [ceiling_rule], holds, config, previous, stops, selections=selections)
    if len(result['actions']) != len(selections) and not result['reasons']:
        raise ValueError('Reply action could not be planned')
    if result['reasons']:
        raise ValueError('; '.join(result['reasons']))
    for item in result['decisions']:
        if any((e['account'], e['id']) == (item['account'], item['id']) and e['verb'] in {'leave', 'yes', 'no'} for e in entries):
            item['reason'] = 'Kept by owner reply'
    result['remaining'] = remaining_items(result, listing)
    for item in result['remaining']:
        for record in (previous or {}).get('_drafts', []):
            if (item['account'], item['id']) == (record['account'], record['id']):
                item['reason'] = 'Draft waiting since ' + record['created'] + ('; draft may exist; check Drafts by hand' if record['draft_id'] is None else '')
    result['reply'] = entries
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


def reply_hint(items, language, listing=None):
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
    return [('Reply with a number and action, for example: ' if language == 'en' else 'Responde con un número y una acción, por ejemplo: ') + examples]


def action_label(action, listing, language='en', planned=False):
    verb = 'draft' if action['action'] == 'create-draft' and language == 'en' else action['action']
    if language == 'es':
        verb = {'trash': 'eliminar', 'archive': 'archivar', 'move': 'mover', 'spam': 'marcar como spam', 'unsubscribe': 'dar de baja', 'create-draft': 'crear borrador'}.get(verb, verb)
    if action['action'] == 'move':
        target = action.get('target_path', action.get('target', ''))
        target = next((f['name'] for f in listing.get('folders', {}).get(action['account'], []) if f['id'] == target), target)
        verb += (' to ' if language == 'en' else ' a ') + chat_text(target)
    return (('would ' if language == 'en' else 'previsto: ') if planned else '') + verb


def message_outcomes(plan, listing, items, observed=None, language='en', *, known_left=None, final_listing=None):
    """Classify message presentation once for real and rehearsal messages."""
    if final_listing is not None:
        if final_listing:
            present = {(m['account'], m['id']) for m in final_listing['messages']}
            known_left = {(m['account'], m['id']) for m in listing['messages']
                          if m['account'] in final_listing['accounts'] and (m['account'], m['id']) not in present}
        else:
            known_left = {(a['account'], a['id']) for a, r in zip(plan['actions'], observed or [])
                          if a['action'] in REMOVALS and r['status'] == 'executed'}
    marks = {(i['account'], i['id']): i for i in plan.get('remaining', [])}
    messages = {(m['account'], m['id']): m for m in listing['messages']}
    remaining = {(i['account'], i['id']): dict(i) for i in items}
    handled, unfinished = {}, {}
    for index, action in enumerate(plan['actions']):
        if action['action'] == 'create-folder': continue
        if observed is not None and (index >= len(observed) or observed[index]['status'] != 'executed'):
            status = observed[index]['status'] if index < len(observed) else 'unexecuted'
            unfinished.setdefault((action['account'], action['id']), []).append((action, status))
            continue
        handled.setdefault((action['account'], action['id']), []).append(action)
    outcomes = []
    for key in dict.fromkeys([*remaining, *handled, *unfinished]):
        item = remaining[key] if key in remaining else dict(messages[key], reason='No matching rule')
        item['previously_listed'] = marks.get(key, {}).get('previously_listed', item.get('previously_listed', False))
        hidden = plan.get('relist_inbox') == 'new_plus_count' and item['previously_listed']
        kind = ('handled' if key in handled else 'protected' if item['reason'] == 'Protected by never-touch'
                else 'previously listed' if hidden else 'kept by owner reply' if marks.get(key, {}).get('reason') == 'Kept by owner reply'
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
        failure = ', '.join(action_label(a, listing, language) + ' ' + status_words[status]
                            for a, status in unfinished.get(key, []))
        if inline:
            item['reason'] = ', '.join(action_label(a, listing, language, planned=True) for a in handled[key])
            item.pop('number', None)
        elif key in handled and key in remaining:
            labels = {'create-draft': 'draft created', 'unsubscribe': 'unsubscribed'} if language == 'en' else {'create-draft': 'borrador creado', 'unsubscribe': 'baja realizada'}
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
        if item['account'] in listing.get('skipped', {}):
            reason = re.sub(r': (?:provider|pigeon) error \([^)]*\)', '', reason)
        if language == 'es':
            for source, target in {'Protected by never-touch': 'Protegido por never-touch', 'Run stopped': 'Ejecución detenida', 'action not confirmed': 'acción no confirmada', 'rule refused by brake': 'regla rechazada por el freno', 'failed': 'falló', 'unexecuted': 'sin ejecutar', 'issued_unknown': 'resultado desconocido'}.items():
                reason = reason.replace(source, target)
        if section == 'handled':
            completed = handled.get(key, []) or planned
            tag = ', '.join(action_label(a, listing, language) for a in completed)
            if failure: tag += ' (' + failure + ')'
        else:
            tag = '' if reason == 'No matching rule' else reason
            if failure:
                done = ', '.join(action_label(a, listing, language) for a in handled.get(key, []))
                tag = (done + '; ' if done else '') + failure
                item.pop('number', None)
            if not owner_items(plan, [item], numbered=True) and not inline:
                item.pop('number', None)
        outcomes.append(dict(item=item, kind=kind, actions=handled.get(key, []), failure=failure,
                             visible=visible, hidden=hidden, remaining=section == 'inbox', section=section, tag=tag,
                             owner=section == 'inbox' and visible and (
                                 bool(owner_items(plan, [item])) and not any(a['action'] in REMOVALS for a in planned)
                                 and (plan.get('approval_state') != 'awaiting approval' or not planned)
                                 if plan.get('dry_run') else 'number' in item)))
    return outcomes


def render_remaining(plan, listing, language='en', *, outcomes=None):
    lines, empty_accounts, shown = [], [], []
    if outcomes is None:
        outcomes = message_outcomes(plan, listing, plan.get('remaining', remaining_items(plan, listing)), None if plan.get('dry_run') else [], language)
    for account in plan['counts']:
        if account not in listing['accounts']: continue
        account_outcomes = [o for o in outcomes if o['item']['account'] == account and o['section'] == 'inbox']
        items = [o['item'] for o in account_outcomes]
        name = account_name(plan, account)
        if not items:
            if account not in listing.get('skipped', {}):
                empty_accounts.append(name)
            continue
        lines.append(name + ': ' + str(len(items)))
        hidden = 0
        for outcome in account_outcomes:
            item = outcome['item']
            if outcome['hidden'] and not outcome['visible']: hidden += 1
            if not outcome['visible']: continue
            shown.append(item)
            reason = outcome['tag']
            lines.append((str(item['number']) + '. ' if 'number' in item else '- ') +
                         chat_text(item['sender'])[:160] + ' · ' + chat_text(item['subject'])[:240] +
                         (' (' + chat_text(reason) + ')' if reason else '') +
                         (' · ' + chat_text(item['question']) if 'question' in item else ''))
        if plan.get('relist_inbox') == 'new_plus_count':
            lines.append(str(hidden) + (' previously listed' if language == 'en' else ' ya enumerados'))
    if empty_accounts:
        lines.append(', '.join(empty_accounts) + (': empty' if language == 'en' else ': vacío'))
    if plan.get('approval_state') != 'awaiting approval':
        lines += reply_hint(shown, language, listing)
    return lines


def render_drafts(plan, listing, results=None):
    actions = [(i, a) for i, a in enumerate(plan['actions']) if a['action'] == 'create-draft']
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
    events, pending, unread = [], [], []
    for name in sorted(config['calendars']):
        if name not in data or data[name] == {'failed': True}:
            unread.append(chat_text(name) + ': not read; overlaps against it were not checked')
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
    lines = ['', 'Calendar', '', 'Invitations waiting for an answer']
    for item in sorted(pending):
        a, b = item[5:]
        marks = []
        clock = a.strftime('%H:%M')
        if not item[4]['all_day'] and 'work_start' in settings and (clock < settings['work_start'] or clock >= settings['work_end'] or (b.date() == a.date() and b.strftime('%H:%M') > settings['work_end'])):
            marks.append('outside working hours')
        if not item[4]['all_day'] and 'quiet_start' in settings:
            qstart = a.replace(hour=int(settings['quiet_start'][:2]), minute=int(settings['quiet_start'][3:]), second=0, microsecond=0).timestamp()
            qend = a.replace(hour=int(settings['quiet_end'][:2]), minute=int(settings['quiet_end'][3:]), second=0, microsecond=0).timestamp()
            if a.timestamp() < qend and b.timestamp() > qstart:
                marks.append('inside quiet window')
        lines.append('- ' + describe(item) + (' · ' + ', '.join(marks) if marks else ''))
    lines.append('Overlaps')
    ordered = sorted(events)
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            if second[0] >= first[3]:
                break
            winner = next((x[1] for x in (first, second) if config['calendars'][x[1]].get('wins')), None)
            lines.append('- ' + describe(first) + ' / ' + describe(second) + (' · ' + chat_text(winner) + ' stays' if winner else ''))
    lines += unread
    return lines


def render_report(plan, listing, language='en', store=None, *, new_question=False):
    if plan.get('approval_state') == 'awaiting approval' and store is not None:
        return awaiting_message(store, plan, listing, language, new_question=new_question)
    empty, stopped = STRINGS[language]
    planned, new = (SECTION_LABELS[language][k] for k in ('planned', 'rules'))
    lookup = {(m['account'], m['id']): m for m in listing['messages']}
    lines = ['Inbox: ' + str(sum(plan['counts'].values())) + ' total' + (' · ' if plan['counts'] else '') + ' · '.join(account_name(plan, a) + ' ' + str(n) for a, n in plan['counts'].items())]
    lines = account_problems(plan, listing, store, language) + lines
    lines += [account_name(plan, a) + ': no new mail' for a in listing.get('quiet', [])]
    if plan.get('approval_state') != 'awaiting approval':
        lines += [stopped + ': ' + account_reason(plan, r) for r in plan['reasons']]
    lines += render_drafts(plan, listing)
    if listing['accounts']: lines += ['', SECTION_LABELS[language]['inbox'], ''] + render_remaining(dict(plan, dry_run=True) if plan.get('approval_state') == 'awaiting approval' else plan, listing, language)
    lines += plan.get('calendar_section', [])
    hosts = {(a['account'], a['id']): safe_unsubscribe_host(a['url']) for a in plan['actions'] if 'url' in a}
    lines += ['', planned, '']
    for account in plan['counts']:
        actions = [a for a in plan['actions'] if a['account'] == account]
        if not actions: continue
        lines.append(account_name(plan, account))
        for action in actions:
            m = lookup[action['account'], action['id']]
            lines.append('- ' + action['action'] + ': ' + chat_text(m['sender'])[:160] + ' · ' + chat_text(m['subject'])[:240] + (' · ' + chat_text(action['target_path']) if 'target_path' in action else '') + (' · ' + chat_text(hosts.get((account, action['id']), '')) if 'url' in action else ''))
    if not plan['actions']: lines.append(empty)
    lines += ['', new, '', empty, '']
    if plan.get('approval_state') == 'awaiting approval':
        lines.append(approval_question(plan, language))
    return '\n'.join(lines)


def account_problems(plan, listing, store, language):
    problems = {a: ('not listed this run', plan.get('skip_reasons', {}).get(a, 'pigeon error (listing answer)')) for a in plan.get('not_listed', [])}
    problems.update({a: ('final reading failed', r) for a, r in listing.get('skipped', {}).items() if a in plan['counts']})
    retried = set()
    for filename in ('listing-exchange.json', 'final-listing-exchange.json', 'quiet-gateway.json'):
        path = store / filename if store is not None else None
        if path is not None and path.exists():
            data = parse_json(read_text(path), path)
            if data.get('token') == read_lock(store)['token']:
                retried.update(a for a, n in data.get('attempts', {}).items() if n)
                retried.update(data.get('retried', []))
    lines = []
    for a, (status, reason) in problems.items():
        if language == 'es':
            status = 'no incluida en esta ejecución' if status == 'not listed this run' else 'falló la lectura final'
        lines.append(account_name(plan, a) + ': ' + status + '; ' + reason + (('; retried once' if language == 'en' else '; reintentada una vez') if a in retried else ''))
    return lines


def message_opening(store, plan=None, outcomes=()):
    config = state_config(store)
    language = config.get('language', 'en')
    started = datetime.fromtimestamp(read_lock(store).get('started', read_lock(store)['created']), ZoneInfo(config.get('timezone', 'UTC')))
    plan = plan or dict(decisions=[], remaining=[], counts={}, not_listed=[])
    plan = dict(plan, account_names={a: f.get('name', a) for a, f in config['accounts'].items()})
    count = owner_count(outcomes)
    need = (str(count) + (' needs you' if count == 1 else ' need you') if count else 'nothing needs you') if language == 'en' else (str(count) + (' requiere tu decisión' if count == 1 else ' requieren tu decisión') if count else 'nada requiere tu decisión')
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



def awaiting_message(store, plan, listing, language, *, new_question=False):
    preview = dict(plan, dry_run=True)
    outcomes = message_outcomes(preview, listing, remaining_items(plan, listing), language=language)
    first = message_opening(store, plan, outcomes).splitlines()[0].rsplit(' · ', 2)[0]
    waiting = 'awaiting approval' if language == 'en' else 'en espera de aprobación'
    first += ' · ' + waiting
    if plan.get('dry_run'): first += ' · ' + ('dry run' if language == 'en' else 'simulación')
    created = time.time() if new_question else question_created(store, plan)
    expired = (not new_question and question_closed(store, plan)) or time.time() - created < 0 or time.time() - created > approval_hours(state_config(store)) * 3600
    if expired:
        empty = 'nothing needs you' if language == 'en' else 'nada requiere tu decisión'
        expired_label = 'approval question expired' if language == 'en' else 'pregunta de aprobación caducada'
        first = first.replace(empty + ' · ' + waiting, expired_label).replace(waiting, expired_label)
    lines = [first] + account_problems(plan, listing, store, language)
    lines += [account_name(plan, a) + ': no new mail' for a in listing.get('quiet', [])]
    lines += ['', SECTION_LABELS[language]['inbox'], '']
    lines += render_remaining(preview, listing, language, outcomes=outcomes)
    lines += plan.get('calendar_section', [])
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
    path = Path(args.plan_out or store / 'plan.json').resolve()
    with lock_gate(store):
        require_lock(store, args.token)
        require_fetched(store, args.token)
        plan = parse_json(read_text(path), path) if path.is_file() else {}
        waiting = plan.get('approval_state') == 'awaiting approval' and not plan.get('dry_run')
        if not waiting and text not in words:
            return False
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
    marker = ' approval asked: ' + str(plan.get('run_id'))
    stamps = [state_timestamp(line.split(' ')[0], history).timestamp()
              for line in (read_text(history).splitlines() if history.exists() else []) if line.endswith(marker)]
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


def approval_event(store, plan, event):
    line = datetime.now(timezone.utc).isoformat() + ' approval ' + event + ': ' + plan['run_id']
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
        if plan.get('dry_run') or plan.get('approval_state') != 'awaiting approval':
            raise ValueError('approval: expected a plan awaiting approval')
        if plan.get('run_id') != run_id:
            raise ValueError('approval: run id does not match awaiting plan')
        history_path = store / 'history.log'
        events = [line.split(' approval ', 1)[1] for line in read_text(history_path).splitlines() if ' approval ' in line] if history_path.exists() else []
        asked = [event[7:] for event in events if event.startswith('asked: ')]
        if not asked or asked[-1] != run_id or any(event.split(' ')[:2] == [kind + ':', run_id] for event in events for kind in ('approved', 'skipped', 'expired', 'replaced', 'stale')):
            raise ValueError('approval: plan replaced or approval already used or expired; plan again')
        check_approval_age(store, plan)
        if validate_only:
            return plan
        if reading is None:
            raise ValueError('approval: fresh Inbox reading required')
        validate_inputs(reading, state_config(store), {})
        accounts = {a['account'] for a in plan['actions']}
        if (set(reading['accounts']) != accounts or reading.get('skipped')
                or accounts - {m['account'] for m in reading['messages']}):
            raise ValueError('approval: fresh Inbox reading failed; question stays open')
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
    require_lock(store, token)
    try:
        return _render_message(store, token, plan_path, results_path, now=now)
    except (ValueError, OSError, KeyError, TypeError, AttributeError, IndexError):
        try:
            failure = parse_json(read_text(store/'run-refusal.json'), 'run-refusal.json')
            if failure.get('token') == token:
                return message_opening(store) + 'Run failed at ' + chat_text(failure['stage']) + ': ' + chat_text(failure['refusal']) + '\n'
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            pass
        return message_opening(store) + 'Run failed at message: pigeon error (run data refused)\n'


def owner_count(outcomes):
    return sum(o['owner'] for o in outcomes)


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
        outcomes = message_outcomes(plan, listing, remaining_items(plan, listing), [], language, known_left=set())
        reason = failure_line or 'Run failed at execution: pigeon error (results unavailable)'
        lines = render_remaining(plan, listing, language, outcomes=outcomes)
        return message_opening(store, plan, outcomes) + reason + '\n' + '\n'.join(lines) + '\n'
    if plan.get('dry_run') is True:
        require_fetched(store, token)
        listing = execute.read(store / 'inbox.json')
        config, rules, holds = load_safety([store / n for n in ('config.toml', 'rules.toml', 'never-touch.toml', 'guardrails.md')])
        previous = execute.read(store / 'state.json') if (store / 'state.json').exists() else {}
        stops = ['Missing state; prior run records exist'] if not (store / 'state.json').exists() and not plan.get('first_run') else []
        expected = build_plan(listing, rules, holds, config, previous, stops)
        number_decisions(store, expected, listing, read_only=True)
        if any(plan.get(k) != expected[k] for k in expected):
            raise ValueError('dry-run plan: expected unchanged private inputs and listing')
        language = config.get('language', 'en')
        if plan.get('approval_state') == 'awaiting approval':
            return awaiting_message(store, plan, listing, language)
        labels = SECTION_LABELS[language]
        lines = ['Dry run', 'Replies to this dry run are not acted on; no reply number is live.'] if language == 'en' else ['Simulación', 'Las respuestas a esta simulación no se aplican; ningún número está activo.']
        outcomes = message_outcomes(plan, listing, remaining_items(plan, listing), language=language)
        count = owner_count(outcomes)
        need = (str(count) + (' needs you' if count == 1 else ' need you') if count else 'nothing needs you') if language == 'en' else (str(count) + (' requiere tu decisión' if count == 1 else ' requieren tu decisión') if count else 'nada requiere tu decisión')
        total = len({(a['account'], a['id']) for a in plan['actions'] if a['action'] != 'create-folder'})
        summary = (str(total) + (' message' if total == 1 else ' messages') + ' would be handled · dry run') if language == 'en' else (str(total) + (' mensaje previsto' if total == 1 else ' mensajes previstos') + ' · simulación')
        if plan['reasons']:
            summary += ' · ' + STRINGS[language][1].lower()
        opening = ' · '.join(message_opening(store).splitlines()[0].split(' · ')[:2])
        lines = [opening + ' · ' + need + ' · ' + summary] + lines
        lines[1:1] = account_problems(plan, listing, store, language)
        lines += [account_name(plan, a) + ': no new mail' for a in listing.get('quiet', [])]
        lines += [STRINGS[language][1] + ': ' + account_reason(plan, r) for r in plan['reasons']]
        lines += render_drafts(plan, listing)
        if listing['accounts']: lines += ['', labels['inbox']]
        lines += render_remaining(plan, listing, language, outcomes=outcomes)
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
    # Read saved numbers only: rendering never allocates or changes them.
    numbers = {(i['account'], i['id']): i['number'] for i in read_numbers(store)['items']}
    old = {(i['account'], i['id']): i for i in plan.get('remaining', [])}
    for item in remaining:
        key = item['account'], item['id']
        if key in numbers and item['reason'] != 'Protected by never-touch':
            item['number'] = numbers[key]
        item['previously_listed'] = old.get(key, {}).get('previously_listed', False)
    outcomes = message_outcomes(plan, execute.read(store / 'inbox.json'), remaining, observed, language,
                                final_listing=execution.state.get('final_listing', {}))
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
                + (' · nothing needs you · 0 handled · stopped\nInbox changed since the question; nothing was done; the next run plans and asks again.' if language == 'en' else ' · nada requiere tu decisión · 0 gestionados · detenido\nLa bandeja de entrada cambió desde la pregunta; no se hizo nada; la próxima ejecución planifica y pregunta de nuevo.'))
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
    total = sum(o['section'] == 'handled' or bool(o['actions']) for o in outcomes)
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
    lines[1:1] = problems + list(dict.fromkeys(stop_notes))
    if plan.get('approval_state') == 'approved':
        lines.insert(1, ('Owner approved exceeding these thresholds. ' if language == 'en' else 'El propietario aprobó superar estos límites. ') + threshold_text(plan, language) + '.')
    if handled:
        lines += ['', labels['handled']]
        for account, actions in handled.items():
            lines.append(account_name(plan, account))
            for outcome in actions:
                m = outcome['item']
                lines.append('- ' + outcome['tag'] + ': ' + chat_text(m['sender'])[:160] + ' · ' + chat_text(m['subject'])[:240])
    lines += render_drafts(plan, execute.read(store / 'inbox.json'), observed)
    confirmed = dict(plan, remaining=remaining)
    lines += [account_name(plan, a) + ': no new mail' for a in listing.get('quiet', [])]
    if listing['accounts']: lines += ['', labels['inbox'], ''] + render_remaining(confirmed, listing, language, outcomes=outcomes)
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


def load_safety(sources):
    """Read safety files, collecting independent errors before refusing."""
    problems, values = [], []
    for index, path in enumerate(sources):
        try:
            text = load_inputs([path])[0]
            value = parse_toml(text, path) if index < 3 else text
            values.append(value)
        except (ValueError, OSError) as error:
            problems.append(str(error))
            values.append(None)
    config, rules_data, holds_data, _ = values
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
    entries = []
    for index, data in ((1, rules_data), (2, holds_data)):
        parsed = []
        if data is not None:
            try:
                known_keys(data, {'rules', 'allow_empty'} if index == 2 else {'rules'}, 'rule file')
                raw = data.get('rules', [])
                if not isinstance(raw, list) or not raw:
                    parse_rules(read_text(sources[index]), protection=index == 2)
                else:
                    seen = set()
                    for number, entry in enumerate(raw, 1):
                        try:
                            (validate_rules if index == 1 else validate_holds)([entry])
                            if index == 1 and entry['id'] in seen:
                                raise ValueError('rule ' + repr(entry['id']) + ': Duplicate rule id; expected a unique id')
                            if index == 1:
                                seen.add(entry['id'])
                            if isinstance(config, dict) and isinstance(config.get('accounts'), dict):
                                for account in entry.get('accounts', []):
                                    if account not in config['accounts']:
                                        raise ValueError(('rule ' + repr(entry['id']) if index == 1 else 'entry ' + str(number)) + ': account ' + repr(account) + ' expected one of config.toml accounts: ' + ', '.join(config['accounts']))
                            parsed.append(entry)
                        except ValueError as error:
                            problems.append(str(sources[index]) + ': ' + str(error))
                if 'allow_empty' in data and type(data['allow_empty']) is not bool:
                    raise ValueError('allow_empty: expected boolean')
            except ValueError as error:
                problems.append(str(sources[index]) + ': ' + str(error))
        entries.append(parsed)
    if problems:
        raise ValueError('\n'.join(dict.fromkeys(problems)))
    return config, *entries


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
        known_keys(case, {'name', 'account', 'sender', 'subject', 'has_attachment', 'body', 'expect', 'expect_rule'}, 'cases.toml case')
        name = case.get('name')
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError('cases.toml: expected unique nonempty case name')
        names.add(name)
        if case.get('account') not in config['accounts'] or case.get('expect') not in VERBS | {'none', 'never-touch'}:
            raise ValueError('cases.toml: expected configured account and supported expectation')
        if 'expect_rule' in case and (not isinstance(case['expect_rule'], str) or not case['expect_rule']):
            raise ValueError('cases.toml: expected rule id string')
        message = {k:v for k,v in case.items() if k in {'account', 'sender', 'subject', 'has_attachment', 'body'}}
        message.update(id='case', folder='Inbox')
        validate_inputs(dict(accounts={case['account']:1},messages=[message]),config,{})
        sender = normalize_sender(message['sender'])
        message['sender'] = sender
        found, rule_id = 'none', None
        if sender is not None:
            if any(matches(message, h, protection=True) for h in holds):
                found = 'never-touch'
            else:
                rule = next((r for r in rules if matches(message, r)), None)
                if rule is not None: found, rule_id = rule['action'], rule['id']
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
        known_keys(plan, {'actions', 'decisions', 'reasons', 'counts', 'expected_counts', 'not_listed', 'skip_reasons', 'first_run', 'reply', 'remaining', 'relist_inbox', 'calendar_section', 'scheduled', 'account_names', 'run_token', 'run_id', 'approval_state', 'approval_created', 'approved_thresholds', 'approval_thresholds'}, str(plan_path))
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
            known_keys(action, {'account', 'id', 'action', 'rule', 'target', 'target_path', 'url', 'draft', 'draft_text', 'draft_to'}, label)
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
            known_keys(entry, {'account', 'id', 'action', 'status', 'folder_id', 'reason', 'draft_id', 'created'}, label)
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
        if plan.get('reply') and not plan['reasons']:
            log = store / 'decisions.jsonl'
            batches = [parse_json(line, log) for line in read_text(log).splitlines()] if log.exists() else []
            if not any(b['run'] == token for b in batches):
                statuses = {(e['account'], e['id']): e['status'] for e in execution.state['results']}
                pending = execution.state['pending']
                if pending is not None:
                    statuses[pending['account'], pending['id']] = 'issued_unknown'
                batch = dict(run=token, time=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
                             parts=[e | {'outcome': statuses.get((e['account'], e['id']), 'answered' if e['verb'] in {'leave', 'yes', 'no'} else 'unexecuted')} for e in plan['reply']])
                with log.open('a', encoding='utf-8') as out:
                    out.write(json.dumps(batch, ensure_ascii=True) + '\n')
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
        records = {(d['account'], d['id']): d for d in previous.get('_drafts', [])}
        for action, result in zip(plan['actions'], observed):
            if action['action'] == 'create-draft' and result['status'] in {'executed', 'issued_unknown'}:
                records[action['account'], action['id']] = dict(account=action['account'], id=action['id'], draft_id=result.get('draft_id'), rule=action['rule'], created=result.get('created', datetime.fromtimestamp(read_lock(store).get('started', read_lock(store)['created']), timezone.utc).isoformat()))
        present = {(m['account'], m['id']) for m in listing['messages']}
        removed = {(r['account'], r['id']) for r in observed if r['action'] in REMOVALS and r['status'] == 'executed'}
        records = [d for key, d in records.items() if d['account'] not in listing['accounts'] or key in present - removed]
        if records or '_drafts' in previous:
            previous['_drafts'] = records
        number_decisions(store, confirmed, listing, read_only=plan.get('approval_state') == 'awaiting approval')
        language = config.get('language', 'en')
        report = render_report(plan if plan.get('approval_state') == 'awaiting approval' else confirmed, listing, language, store)
        marker = '**' + SECTION_LABELS[language]['inbox'] + '**'
        report = report.replace(marker, '\n'.join(render_drafts(plan, read(store / 'inbox.json'), observed)) + '\n' + marker, 1)
        (store / 'report.md').write_text(report, encoding='utf-8')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=store, delete=False) as out:
                temporary = Path(out.name)
                json.dump({**previous, **counts, 'version': FORMAT_VERSION}, out, sort_keys=True)
            temporary.replace(state)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        (store / 'run-refusal.json').unlink(missing_ok=True)


TOOL_KINDS = PLAN_VERBS | {'message-read', 'profile', 'ids', 'attachments', 'labels', 'message', 'history', 'folders'}


# Read recipes and action tools are closed per provider and call kind.
PROVIDER_TOOLS = {
    'gmail': {
        'profile': {'gmail.users.getProfile'},
        'ids': {'gmail.users.messages.list'}, 'attachments': {'gmail.users.messages.list'},
        'labels': {'gmail.users.labels.list'}, 'message': {'gmail.users.messages.get'},
        'message-read': {'gmail.users.messages.get'}, 'history': {'gmail.users.history.list'},
        'trash': {'gmail.users.messages.trash'},
        'archive': {'gmail.users.messages.modify'}, 'move': {'gmail.users.messages.modify'},
        'spam': {'gmail.users.messages.modify'}, 'create-folder': {'gmail.users.labels.create'},
        'create-draft': {'gmail.users.drafts.create'},
    },
    'outlook': {
        'profile': {'meUser.meUserGetUser'},
        'ids': {'meMailFolder.meMailFoldersListMessages'},
        'folders': {'meMailFolder.meListMailFolders', 'meMailFolder.meMailFoldersListChildFolders'},
        'message-read': {'meMessage.meGetMessages'},
        'trash': {'meMessage.meMessagesMessageMove'}, 'archive': {'meMessage.meMessagesMessageMove'},
        'move': {'meMessage.meMessagesMessageMove'}, 'spam': {'meMessage.meMessagesMessageMove'},
        'create-folder': {'meMailFolder.meCreateMailFolders', 'meMailFolder.meMailFoldersCreateChildFolders'},
        'create-draft': {'meMessage.meMessagesMessageCreateReply', 'meMessage.meMessagesMessageCreateForward'},
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
    return any(provider_failed(value.get(k)) for k in ('error', 'value'))


def provider_error(value):
    nodes = []
    def collect(node):
        if isinstance(node, dict):
            nodes.append(node)
            for key in ('error', 'value'):
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
    if (not match or (match[1] or match[2]) != fields.get('executor_app')
            or kind not in TOOL_KINDS or any(word in path.casefold() for word in ('send', 'delete'))
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
            query = dict(format='metadata', metadataHeaders=['From', 'Subject', 'List-Unsubscribe', 'List-Unsubscribe-Post'])
    else:
        tool = 'meMailFolder.' + ('meMailFoldersListMessages' if kind == 'ids' else
                                'meMailFoldersListChildFolders' if id else 'meListMailFolders')
        if kind == 'ids':
            path = {'mailFolder-id':'inbox'}
            query = {'$select':['id', 'from', 'subject', 'receivedDateTime', 'hasAttachments'],
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


def listing_exchange(store, token, results=None, *, ids_only=False, accounts=None, manual_part=None, skip=(), approval=None):
    """Persist issued reads before returning them; mail values remain data."""
    import execute
    with lock_gate(store):
        require_lock(store, token)
        require_fetched(store, token)
        config, rules, holds = load_safety([store / n for n in ('config.toml','rules.toml','never-touch.toml','guardrails.md')])
        filename = store / ('approval-reading.json' if approval else 'final-listing-exchange.json' if ids_only else 'listing-exchange.json')
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
        quiet_cache = parse_json(read_text(quiet_path), quiet_path) if quiet_path.exists() else {}
        quiet_names = quiet_cache.get('decision', {}).get('quiet_accounts', []) if quiet_cache.get('token') == token and not ids_only else []
        manual = [a for a in names if a not in skip and a not in quiet_names and not ids_only and any('body' in r and a in r.get('accounts', []) for r in rules + holds)]
        part = None
        if manual:
            if manual_part is None:
                raise ValueError('listing: body accounts require --manual-part: ' + ', '.join(manual))
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
                        calls=[listing_call(config, j, ids_only, store) for j in jobs], failed=state['failed'])
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
        execute.atomic(store / ('approval-inbox.json' if approval else 'final-inbox.json' if ids_only else 'inbox.json'), listing)
        return dict(done=True, counts={a:len(d['ids']) for a,d in state['data'].items() if a not in state['failed']}, failed=state['failed'])


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
        if dry_run: return dict(observations=state['observations'])
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
    if arguments and (arguments[0] == 'dry-run' or arguments[0] == 'quiet-check' and '--dry-run' in arguments):
        return
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
    converter = sub.add_parser('convert-rules', allow_abbrev=False)
    for flag in ('table', 'scopes', 'out', 'review'):
        converter.add_argument('--' + flag, required=flag != 'review')
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
    quiet = sub.add_parser('quiet-check', allow_abbrev=False)
    quiet.add_argument('--token', required=True)
    quiet.add_argument('--observation')
    quiet.add_argument('--results', help='Gateway array: docs/quickstart.md#gateway-results')
    quiet.add_argument('--scheduled', action='store_true')
    quiet.add_argument('--dry-run', action='store_true')
    quiet.add_argument('--progress', help='Saved dry-run quiet response')
    for command_name in ('plan', 'reply'):
        plan = sub.add_parser(command_name, allow_abbrev=False)
        if command_name == 'plan':
            plan.add_argument('--dry-run', action='store_true')
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
    for name in ('inbox', 'config', 'rules', 'never_touch', 'guardrails', 'plan_out', 'report_out', 'plan', 'results', 'reply'):
        if getattr(args, name, None) == '':
            raise ValueError('--' + name.replace('_', '-') + ': expected a non-empty path')
    if args.command == 'convert-rules':
        convert_rules(args.table, args.scopes, args.out, args.review)
        return
    if args.command == 'setup':
        store = private_store(output=True)
        examples = {'config.toml': 'config/config.example.toml',
                    'guardrails.md': 'config/guardrails.example.md',
                    'rules.toml': 'rules/rules.example.toml',
                    'never-touch.toml': 'rules/never-touch.example.toml'}
        for name in examples:
            if (store / name).exists() or (store / name).is_symlink():
                raise ValueError(str(store / name) + ': expected no existing setup file')
        root = Path(__file__).resolve().parents[1]
        contents = {name: read_text(root / source) for name, source in examples.items()}
        contents['guardrails.md'] = re.sub(r'\]\(\.\./([^)]*)\)', lambda m: '](' + str(root) + '/' + m[1] + ')', contents['guardrails.md'])
        store.mkdir(parents=True, exist_ok=True, mode=0o700)
        store.chmod(0o700)
        for name, content in contents.items():
            with (store / name).open('x', encoding='utf-8') as out:
                os.chmod(out.name, 0o600)
                out.write(content)
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
        config, rules, holds = load_safety([store / n for n in ('config.toml', 'rules.toml', 'never-touch.toml', 'guardrails.md')])
        state_path = store / 'state.json'
        if state_path.exists():
            validate_inputs(dict(accounts={},messages=[]),config,parse_json(read_text(state_path),state_path))
        print(f'version: {config.get("version", FORMAT_VERSION)}')
        for account, fields in config['accounts'].items():
            available = all(fields.get(k) for k in ('provider', 'executor_app', 'executor_account'))
            manual = any('body' in r and account in r.get('accounts', []) for r in rules + holds)
            print(account + ': scripted listing ' + ('manual part required' if manual else 'available' if available else 'unavailable'))
        print(f'switched off: {sum(not r.get("enabled", True) for r in rules)}')
        print(f'ok: {len(config["accounts"])} accounts, {len(rules)} rules, {len(holds)} never-touch entries')
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
        print(json.dumps(listing_exchange(store, args.token, args.results, ids_only=args.ids_only, manual_part=args.manual_part, skip=args.skip), ensure_ascii=True))
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
                    print(json.dumps(quiet_check(store, config, previous, result['observations'], args.scheduled, now, dry_run=True), sort_keys=True))
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
        sources = [getattr(args, n.replace('-', '_')) or store / (n + ('.md' if n == 'guardrails' else '.toml')) for n in ('config', 'rules', 'never-touch', 'guardrails')]
        protected = {Path(p).resolve() for p in sources + [args.inbox]} | {store / n for n in ('state.json', 'run.lock', '.lock-gate', 'calls.jsonl', 'results.json', 'execution-report.md', 'unsubscribe-failures.json', 'unsubscribe-secret', 'numbers.json', 'reply.json', 'reply.txt', 'decisions.jsonl', 'history.log', 'run-message.md', 'quiet-observation.json', 'positions-observation.json', 'quiet-check.json', 'lock-exchange.json', 'lock-observation.json', 'lock-commit-observation.json', 'state-exchange.json', 'finish-exchange.json', 'final-inbox.json', 'state-incoming', 'state-outgoing', 'state-observation.json', 'upload-observation.json', 'state-lock-observation.json', 'tool-paths.json', 'cases.toml', 'run-refusal.json', 'inbox.json', 'config.toml', 'rules.toml', 'never-touch.toml', 'guardrails.md')}
        if args.command == 'reply':
            protected.add(Path(args.reply).resolve())
        protected = {p.resolve() for p in protected}
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
                config, rules, holds = load_safety(sources)
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
                    if account in {'version', '_positions', '_drafts'}:
                        continue
                    if type(count) is not int or count < 0:
                        raise ValueError(str(state) + ': state.json.' + account + ': expected a non-negative integer count')
                first_run = not state.exists() and not any((store / name).exists() for name in ('numbers.json', *LOG_NAMES))
                stops = ['Missing state; prior run records exist'] if not state.exists() and not first_run else []
                try:
                    result = build_plan(listing, rules, holds, config, previous, stops)
                except ValueError as error:
                    message = str(error)
                    if message.startswith('rules.toml:') or message.startswith('never-touch.toml:'):
                        name, message = message.split(':', 1)
                        source = (args.rules or store / name) if name == 'rules.toml' else (args.never_touch or store / name)
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
                        result = reply_plan(store, listing, holds, config, previous, parts, stops)
                        import execute
                        execute.atomic(store / 'reply.json', dict(token=args.token, parts=parts))
                    except ValueError as error:
                        refusal = str(error)
                        result = build_plan(listing, [], holds, config, previous, ['Reply refused: ' + ' '.join(refusal.split())])
                if args.command == 'plan' and config.get('calendars'):
                    data = parse_json(load_inputs([args.calendar])[0], args.calendar) if args.calendar else {}
                    result['calendar_section'] = calendar_section(data, config, now or datetime.now(timezone.utc))
                result['run_id'] = secrets.token_hex(32)
                result['run_token'] = args.token
                if result.get('approval_state') == 'awaiting approval':
                    result['approval_created'] = (now or datetime.now(timezone.utc)).timestamp()
                if getattr(args, 'dry_run', False):
                    result['dry_run'] = True
                    number_decisions(store, result, listing, read_only=True)
                else:
                    number_decisions(store, result, listing)
                result['first_run'] = first_run
                if args.scheduled: result['scheduled'] = True
                report = render_report(result, listing, config.get('language', 'en'), store, new_question=True)
                if args.scheduled and 'schedule' in config:
                    now = now or datetime.now(timezone.utc)
                    slot = schedule_slot(config, now)
                    delay = int((now.timestamp() - slot.timestamp()) // 60)
                    if now.timestamp() - slot.timestamp() > 15 * 60:
                        report = 'Late start: slot ' + slot.strftime('%Y-%m-%d %H:%M %Z') + ', delay ' + str(delay) + ' minutes\n' + report
                sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
                if args.command == 'reply':
                    lookup = {(m['account'], m['id']): m for m in listing['messages']}
                    if result['reasons']:
                        print(safe_text(result['reasons'][0]) + ' Type each listed number once followed by trash, archive, spam, move to <existing folder>, leave, yes or no; separate parts with commas. For a stale, protected or unavailable item, request a fresh run and use an eligible listed number. Next: execute.py next, then commit-state, send-state and release under this lock.')
                    for entry in result.get('reply', []):
                        if entry['verb'] in {'leave', 'yes', 'no'}:
                            continue
                        m = lookup[entry['account'], entry['id']]
                        print(str(entry['number']) + '. [' + chat_text(entry['account']) + '] ' + chat_text(m['sender']) + ': "' + chat_text(m['subject']) + '" ' + entry['verb'])
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
                        asked = [event[7:] for event in events if event.startswith('asked: ')]
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
