#!/usr/bin/env python3
"""Offline Inbox planner. No mail or network operations.

commit-state saves Inbox counts after a run. Pass results.json written by
execute.py; hand-written files are refused. expected_counts is a forecast,
never saved state. See docs/setup.md for result formats and statuses.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import stat
import argparse
import ipaddress
from urllib.parse import urlsplit
from contextlib import contextmanager
from email.utils import getaddresses
import fcntl
import unicodedata
import math
import tempfile
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Known limits: POSIX only; spam/phishing flags use exact keys; locks do not
# refresh during execution; user-supplied regexes can be slow.

VERBS = frozenset({'trash', 'archive', 'move', 'spam', 'unsubscribe', 'report'})
PLAN_VERBS = VERBS | {'create-folder'}
RULE_LIMIT_CAP = 50
BODY_LIMIT = 2000
QUESTION_LIMIT = 500
STALE_SECONDS = 45 * 60
TAKEOVER_MARGIN = 10 * 60
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
        known_keys(held, {'token', 'created', 'backend', 'id', 'etag', 'drive_created', 'lost'}, 'run.lock')
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
    return lock_setting(parse_toml(load_inputs([path])[0], path))


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
    # Pure local decisions: the assistant transports exactly one issued call.
    import execute
    clock = time.time() if now is None else now.timestamp()
    setting = store_lock_setting(store)
    if setting['kind'] != 'onedrive':
        raise ValueError('config.lock: expected onedrive')
    path = store / 'lock-exchange.json'
    with lock_gate(store):
        def issue(phase, *, created_item=None, **extra):
            call = dict(account=setting['account'], folder=setting['folder'], name=LOCK_NAME,
                        operation='create' if phase in ('create', 'recreate') else 'delete' if phase in ('takeover', 'release') else 'read')
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
            if operation == 'release':
                return issue('release', if_match=held['etag'])
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
                trim_logs(store)
                execute.atomic(store / 'run.lock', dict(token=token, created=min(clock, item['drive_created']), backend=setting, **item))
                path.unlink()
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
            if phase in ('takeover', 'release') and data == {'status': 'deleted'}:
                if pending['call'].get('operation') != 'delete' or not pending['call'].get('if_match'):
                    raise ValueError('lock exchange: conditional delete was not issued; not held')
                if phase == 'takeover':
                    return issue('recreate')
                require_lock(store, token, now=clock)
                if pending['call']['if_match'] != read_lock(store, now=clock)['etag']:
                    raise ValueError('lock exchange: changed holder; lost lock')
                (store / 'run.lock').unlink()
                path.unlink()
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


# Fixed durable set, also the upload order. Run-local plans and checkpoints stay local.
STATE_FILES = ('numbers.json', 'unsubscribe-secret', 'unsubscribe-failures.json',
               'decisions.jsonl', 'calls.jsonl', 'history.log', 'run-message.md',
               'rules.toml', 'never-touch.toml', 'state.json')
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
    return parse_toml(load_inputs([store / 'config.toml'])[0], store / 'config.toml')


def require_fetched(store, token):
    setting = state_setting(state_config(store))
    if setting['kind'] != 'local':
        path = store / 'state-exchange.json'
        data = parse_json(read_text(path), path) if path.exists() else {}
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
    if name in ('rules.toml', 'never-touch.toml'):
        try:
            entries = parse_rules(text, protection=name == 'never-touch.toml')
        except ValueError as error:
            raise ValueError(str(folder / name) + ': ' + str(error)) from None
        if any(a not in config['accounts'] for r in entries for a in r.get('accounts', [])):
            raise ValueError(str(folder / name) + ': unconfigured account')
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
                            or part['outcome'] not in {'executed', 'failed', 'skipped', 'unexecuted', 'issued_unknown', 'answered'}):
                        raise ValueError(name + ': invalid decision')
                    if part['target'] is not None:
                        folder_path(part['target'])
            else:
                if (not isinstance(data, dict) or set(data) != {'sequence', 'time', 'run', 'account', 'id', 'verb', 'outcome'}
                        or type(data['sequence']) is not int or data['sequence'] < 1
                        or type(data['time']) not in (int, float) or not math.isfinite(data['time'])
                        or any(not isinstance(data[k], str) for k in ('run', 'account', 'id', 'verb', 'outcome'))):
                    raise ValueError(name + ': invalid call log')
    elif name == 'history.log':
        for line in text.splitlines():
            if not re.fullmatch(r'[0-9T:.+Z-]{20,40} (?:skipped: no new mail|abandoned: earlier lock exchange)', line):
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


def drive_state(store, operation, token, observation=None, *, lock_observation=None, now=None):
    """In-process decisions only: no subprocess or network; assistant transports calls."""
    import execute
    config = state_config(store)
    setting = state_setting(config)
    if setting['kind'] == 'local':
        return {'done': True}
    path = store / 'state-exchange.json'
    incoming = store / 'state-incoming'
    outgoing = store / 'state-outgoing'
    if operation == 'send' and observation is None:
        require_fetched(store, token)
        result = drive_lock(store, 'state-send', token, lock_observation, now=now)
        if isinstance(result, dict):
            return result
    with lock_gate(store):
        require_lock(store, token, now=now.timestamp() if now else None)
        data = parse_json(read_text(path), path) if path.exists() else {}
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
                    if n in contents and not (n == 'run-message.md' and contents[n] == b''):
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
    raw = state_bytes(Path(path))
    if len(raw) > LOCK_OBSERVATION_LIMIT:
        raise ValueError(str(path) + ': state observation: exceeds 4096 bytes')
    value = parse_json(raw, path)
    if not isinstance(value, dict):
        raise ValueError(str(path) + ': state observation: expected object')
    return value


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
            if set(rule) - {'id', 'action', 'accounts', 'sender', 'domain', 'subject', 'has_attachment', 'exclusions', 'target', 'after_unsubscribe', 'body', 'question', 'limit'}:
                known_keys(rule, {'id', 'action', 'accounts', 'sender', 'domain', 'subject', 'has_attachment', 'exclusions', 'target', 'after_unsubscribe', 'body', 'question', 'limit'}, 'rule')
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
        kind = config['accounts'][account].get('quiet_kind', 'ids')
        if kind == 'history':
            if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,128}', value):
                raise ValueError('state.json._positions.' + account + ': expected 1 to 128 plain digits')
        elif (not isinstance(value, list) or len(value) > NEWEST_IDS or
              any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', i) for i in value)
              or len(value) != len(set(value))):
            raise ValueError('state.json._positions.' + account + ': expected up to 10 unique ASCII ids (1 to 512 letters, digits, underscore or hyphen)')


def read_observation(path, config, saved):
    # Bounded, in-process parser; gateway data is never a call or report template.
    try:
        path = Path(path)
        if not path.is_file():
            return None
        with path.open('rb') as stream:
            raw = stream.read(OBSERVATION_LIMIT + 1)
        if len(raw) > OBSERVATION_LIMIT:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != set(config['accounts']):
            return None
        positions = {}
        quiet = True
        for account, fields in config['accounts'].items():
            entry = data[account]
            kind = fields.get('quiet_kind', 'ids')
            known_keys(entry, {'outcome', 'position', 'arrivals'} if kind == 'history' else {'outcome', 'ids'}, 'observation')
            if entry.get('outcome') != 'ok':
                return None
            if kind == 'history':
                positions[account] = entry.get('position')
                arrivals = entry.get('arrivals')
                if (not isinstance(arrivals, list) or len(arrivals) > 1000 or
                        any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', i) for i in arrivals)):
                    return None
                quiet = quiet and not arrivals
            else:
                positions[account] = entry.get('ids')
        validate_positions(positions, config)
        for account, value in positions.items():
            if config['accounts'][account].get('quiet_kind', 'ids') == 'history':
                if account in saved and int(value) < int(saved[account]):
                    return None
            else:
                quiet = quiet and value == saved.get(account)
        return positions, quiet
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def quiet_check(store, config, previous, observation, scheduled, now):
    import execute
    path = store / 'quiet-check.json'
    token = read_lock(store)['token'] if (store / 'run.lock').exists() else None
    if token is not None and path.exists():
        cached = parse_json(read_text(path), path)
        if cached['token'] == token:
            return cached['decision']
    saved = previous.get('_positions', {})
    observed = read_observation(observation, config, saved) if observation is not None else None
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
        with (store / 'history.log').open('a', encoding='utf-8') as out:
            out.write(now.astimezone(timezone.utc).isoformat() + ' skipped: no new mail\n')
        decision = {'decision': 'skip', 'reason': 'no new mail'}
    if token is not None:
        execute.atomic(path, dict(token=token, decision=decision, positions=observed[0] if observed else None))
    return decision


def validate_inputs(listing, config, previous):
    known_keys(config, {'accounts', 'language', 'inbox_limit', 'timezone', 'schedule', 'lock', 'state'}, 'config')
    lock_setting(config)
    state_setting(config)
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
        if account == '_positions':
            raise ValueError('config.accounts._positions: reserved state key')
        known_keys(fields, {'address', 'executor_app', 'quiet_kind'}, 'config.accounts.' + account)
        for key, value in fields.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError('config.accounts.' + account + '.' + key + ': expected a non-empty string')
        if fields.get('quiet_kind', 'ids') not in ('history', 'ids'):
            raise ValueError('config.accounts.' + account + '.quiet_kind: expected history or ids')
    known_keys(listing, {'accounts', 'messages', 'folders'}, 'Inbox')
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
    known_keys(previous, set(accounts) | {'_positions'}, 'state.json')
    if '_positions' in previous:
        validate_positions(previous['_positions'], config)
    if any(type(n) is not int or n < 0 for a, n in previous.items() if a != '_positions'):
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
    not_listed = [a for a in config['accounts'] if a not in totals]
    if set(totals) - set(config['accounts']) or not set(totals) & set(config['accounts']):
        return {'actions': [],
                'decisions': [{'account': m['account'], 'id': m['id'], 'reason': 'Run stopped'} for m in messages],
                'reasons': list(stop_reasons) + ['Listing has an unconfigured account' if set(totals) - set(config['accounts']) else 'No configured account listed'],
                'not_listed': not_listed,
                'counts': {a: sum(m['account'] == a for m in messages) for a in totals}}
    keys = [(m['account'], m['id']) for m in messages]
    if len(set(keys)) != len(keys) or any(m['account'] not in totals or m.get('folder') != 'Inbox' for m in messages):
        raise ValueError('Inbox.messages: Listing must contain unique Inbox ids from declared accounts; expected unique (account, id) pairs, folder Inbox, and accounts present in Inbox.accounts')
    counts = {a: sum(m['account'] == a for m in messages) for a in totals}
    reasons = list(stop_reasons)
    if len(messages) > config.get('inbox_limit', 100):
        reasons.append('Inbox limit exceeded')
    for account, count in counts.items():
        if count == 0 and (previous or {}).get(account, 0) >= 3:
            reasons.append('Unexpected empty Inbox: ' + account)
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
                key = (rule['id'], message['account'])
                if 'question' not in rule:
                    hits[key] = hits.get(key, 0) + 1
                verb = rule['action']
                action = {'account': message['account'], 'id': message['id'], 'action': verb, 'rule': rule['id']}
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
    for rule in rules:
        count = sum(a['rule'] == rule['id'] and a['action'] != 'create-folder' for a in actions)
        if count > rule.get('limit', 10):
            reasons.append(f"Per-rule action limit exceeded: {rule['id']} ({count} of {rule.get('limit', 10)})")
    if sum(a['action'] == 'unsubscribe' for a in actions) > 10:
        reasons.append('Unsubscribe limit exceeded')
    if any(n > totals[a] * 0.2 for (_, a), n in hits.items()):
        reasons.append('Rule exceeds twenty percent of account total')
    if reasons:
        actions = []
        decisions = [{'account': m['account'], 'id': m['id'], 'reason': 'Run stopped'} for m in messages]
    validate_actions(actions, listing)
    expected = dict(counts)
    for action in actions:
        if action['action'] in {'trash', 'archive', 'move', 'spam'}:
            expected[action['account']] -= 1
    return {'actions': actions, 'decisions': decisions, 'reasons': reasons, 'counts': counts,
            'expected_counts': expected, 'not_listed': not_listed}


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


def number_decisions(store, plan, listing):
    import execute
    data = read_numbers(store)
    protected = {(i['account'], i['id']) for i in plan['decisions'] if i['reason'] == 'Protected by never-touch'}
    present = {(i['account'], i['id']) for i in plan['decisions'] + plan.get('reply', [])} - protected
    data['items'] = [i for i in data['items'] if i['account'] not in listing['accounts'] or (i['account'], i['id']) in present]
    lookup = {(i['account'], i['id']): i['number'] for i in data['items']}
    for item in plan['decisions']:
        if item['reason'] == 'Protected by never-touch':
            continue
        key = (item['account'], item['id'])
        if key not in lookup:
            lookup[key] = data['next']
            data['items'].append(dict(number=data['next'], account=key[0], id=key[1]))
            data['next'] += 1
        item['number'] = lookup[key]
    execute.atomic(store / 'numbers.json', data)


def parse_reply(text):
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
    result['reply'] = entries
    return result


STRINGS = {
    'en': ('Needs you', 'Planned actions', 'New rules', 'None', 'Stopped'),
    'es': ('Requiere tu decisión', 'Acciones previstas', 'Reglas nuevas', 'Ninguno', 'Detenido'),
}


def safe_text(value):
    # Mail text remains a single escaped Markdown text line, never raw HTML.
    value = ''.join('[control]' if unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} else c for c in str(value))
    # Linear-time link breaking, Unicode-aware: any dot before a letter, including full-width dots.
    value = re.sub(r'(?<=[^\W_])[.。．｡](?=[^\W\d_])', '[dot]', value)
    value = value.replace('://', '[colon]//').replace('@', '[at]')
    return re.sub(r'([\\`*_{}\[\]()<>!|#])', r'\\\1', ' '.join(value.split()))


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


def render_report(plan, listing, language='en'):
    decision, planned, new, empty, stopped = STRINGS[language]
    lookup = {(m['account'], m['id']): m for m in listing['messages']}
    def describe(item):
        m = lookup[item['account'], item['id']]
        return safe_text(item['account'] + ': ' + m['sender'] + ' | ' + m['subject'])
    lines = ['Inbox: ' + str(sum(plan['counts'].values())) + ' total | ' + ' · '.join(safe_text(a) + ' ' + str(n) for a, n in plan['counts'].items())]
    lines = [safe_text(a) + ': not listed this run' for a in plan.get('not_listed', [])] + lines
    lines += [stopped + ': ' + safe_text(r) for r in plan['reasons']]
    lines += ['', '**' + decision + '**', '']
    unsafe = []
    hosts = {}
    for a in plan['actions']:
        if a['action'] != 'unsubscribe':
            continue
        m = lookup[a['account'], a['id']]
        host = safe_unsubscribe_host(a['url'])
        if not host or m.get('spam') or m.get('phishing'):
            unsafe.append(a | {'reason': 'Unsafe unsubscribe'})
        else:
            hosts[a['account'], a['id']] = host
    lines += [(str(a['number']) + '. ' if 'number' in a else '- ') + describe(a) + ' (' + safe_text(a['reason']) + ')' + (' | ' + safe_text(a['question']) if 'question' in a else '') for i, a in enumerate(plan['decisions'] + unsafe, 1)] or [empty]
    lines += ['', '**' + planned + '**', '']
    lines += ['- ' + a['action'] + ': ' + describe(a) + (' | ' + code_path(a['target_path']) if 'target_path' in a else '') + (' | ' + safe_text(hosts.get((a['account'], a['id']), '')) if 'url' in a else '') for a in plan['actions'] if a['action'] != 'unsubscribe' or (a['account'], a['id']) in hosts] or [empty]
    lines += ['', '**' + new + '**', '', empty, '']
    return '\n'.join(lines)


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
        known_keys(plan, {'actions', 'decisions', 'reasons', 'counts', 'expected_counts', 'not_listed', 'first_run', 'reply'}, str(plan_path))
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
            known_keys(action, {'account', 'id', 'action', 'rule', 'target', 'target_path', 'url'}, label)
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
        executed = []
        if results_path is not None:
            results = read(results_path)
            known_keys(results, {'results'}, str(results_path))
            if not isinstance(results.get('results'), list):
                raise ValueError(str(results_path) + '.results: expected an array')
            seen = set()
            for i, entry in enumerate(results['results']):
                label = str(results_path) + '.results.' + str(i)
                known_keys(entry, {'account', 'id', 'action', 'status', 'folder_id'}, label)
                for k in ('account', 'id', 'action', 'status'):
                    if not isinstance(entry.get(k), str):
                        raise ValueError(label + '.' + k + ': expected a string')
                key = (entry['account'], entry['id'], entry['action'])
                if key not in actions or key in seen:
                    raise ValueError(label + '.id: expected a unique action matching the plan account, id and action')
                seen.add(key)
                if entry['status'] not in {'executed', 'failed', 'skipped', 'unexecuted', 'unsubscribe_not_executable', 'unsubscribe_not_attempted', 'issued_unknown'}:
                    raise ValueError(label + '.status: expected executed, failed, skipped, unexecuted, unsubscribe_not_executable, unsubscribe_not_attempted or issued_unknown')
                if entry['status'] == 'executed' and entry['action'] in {'trash', 'archive', 'move', 'spam'}:
                    executed.append(entry['account'])
        if results_path is not None and (not execution.path.exists() or results['results'] != execution.state['results']):
            raise ValueError(str(results_path) + ': expected results recorded by the step machine for this token and plan')
        config_path = store / 'config.toml'
        try:
            config = parse_toml(load_inputs([config_path])[0], config_path)
        except ValueError as error:
            raise ValueError(str(config_path) + ': expected valid TOML: ' + str(error)) from error
        state = store / 'state.json'
        previous = read(state) if state.exists() else {}
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
        if not plan.get('reply'):
            previous.pop('_positions', None)
        path = store / 'quiet-check.json'
        cached = read(path) if path.exists() else {}
        positions = cached.get('positions') if cached.get('token') == token else None
        if (not plan.get('reply') and not plan['reasons'] and positions is not None and counts.keys() == config['accounts'].keys()
                and execution.path.exists()
                and len(execution.state['results']) == len(plan['actions'])):
            validate_positions(positions, config)
            previous['_positions'] = positions
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
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=store, delete=False) as out:
                temporary = Path(out.name)
                json.dump({**previous, **counts}, out, sort_keys=True)
            temporary.replace(state)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main(argv=None, *, now=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest='command', required=True)
    lock = sub.add_parser('lock', allow_abbrev=False)
    lock.add_argument('--observation')
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
    commit = sub.add_parser('commit-state', allow_abbrev=False)
    commit.add_argument('--token', required=True)
    commit.add_argument('--plan', required=True)
    commit.add_argument('--results')
    commit.add_argument('--lock-observation')
    quiet = sub.add_parser('quiet-check', allow_abbrev=False)
    quiet.add_argument('--token', required=True)
    quiet.add_argument('--observation')
    quiet.add_argument('--scheduled', action='store_true')
    for command_name in ('plan', 'reply'):
        plan = sub.add_parser(command_name, allow_abbrev=False)
        if command_name == 'reply':
            plan.add_argument('--reply', required=True)
        plan.add_argument('--token', required=True)
        plan.add_argument('--inbox', required=True)
        plan.add_argument('--scheduled', action='store_true')
        plan.add_argument('--first-run', action='store_true', help='Allow a missing state file on the first run')
        for name in ('config', 'rules', 'never-touch', 'guardrails', 'plan-out', 'report-out'):
            plan.add_argument('--' + name)
    args = parser.parse_args(argv)
    for name in ('inbox', 'config', 'rules', 'never_touch', 'guardrails', 'plan_out', 'report_out', 'plan', 'results', 'reply'):
        if getattr(args, name, None) == '':
            raise ValueError('--' + name.replace('_', '-') + ': expected a non-empty path')
    store = private_store()
    if args.command == 'lock':
        result = drive_lock(store, 'acquire', observation=args.observation, now=now) if store_lock_setting(store)['kind'] == 'onedrive' else acquire(store)
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
    elif args.command == 'commit-state':
        if store_lock_setting(store)['kind'] == 'onedrive':
            result = drive_lock(store, 'commit', args.token, args.lock_observation, now=now)
            if isinstance(result, dict):
                print(json.dumps(result, sort_keys=True))
                return
        commit_state(store, args.token, args.plan, args.results, now=now, lock_verified=True)
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
            if args.observation is None:
                reads = {}
                for account, fields in config['accounts'].items():
                    if fields.get('quiet_kind', 'ids') == 'history':
                        reads[account] = {'read': 'Inbox arrivals since saved position and current position',
                                          'since': previous.get('_positions', {}).get(account)}
                        if reads[account]['since'] is None:
                            reads[account]['read'] = 'current position only'
                    else:
                        reads[account] = {'read': 'newest Inbox ids in newest-first order', 'limit': NEWEST_IDS}
                print(json.dumps({'reads': reads}, sort_keys=True))
            else:
                print(json.dumps(quiet_check(store, config, previous, args.observation, args.scheduled, now), sort_keys=True))
    else:
        sources = [getattr(args, n.replace('-', '_')) or store / (n + ('.md' if n == 'guardrails' else '.toml')) for n in ('config', 'rules', 'never-touch', 'guardrails')]
        protected = {Path(p).resolve() for p in sources + [args.inbox]} | {store / n for n in ('state.json', 'run.lock', '.lock-gate', 'calls.jsonl', 'results.json', 'execution-report.md', 'unsubscribe-failures.json', 'unsubscribe-secret', 'numbers.json', 'reply.json', 'reply.txt', 'decisions.jsonl', 'history.log', 'run-message.md', 'quiet-observation.json', 'positions-observation.json', 'quiet-check.json', 'lock-exchange.json', 'lock-observation.json', 'lock-commit-observation.json', 'state-exchange.json', 'state-incoming', 'state-outgoing', 'state-observation.json', 'upload-observation.json', 'state-lock-observation.json', 'inbox.json', 'config.toml', 'rules.toml', 'never-touch.toml', 'guardrails.md')}
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
            for flag, name in (('--plan-out', args.plan_out), ('--report-out', args.report_out)):
                if name and Path(name).expanduser().resolve() in protected:
                    raise ValueError(flag + ': expected a path other than an input, state or lock file')
            with lock_gate(store):
                require_lock(store, args.token)
                require_fetched(store, args.token)
                texts = load_inputs(sources)
                parsed = []
                for name, text, parse in zip(('config', 'rules', 'never-touch'), texts, (tomllib.loads, parse_rules, lambda t: parse_rules(t, protection=True))):
                    path = getattr(args, name.replace('-', '_')) or store / (name + '.toml')
                    try:
                        parsed.append(parse_toml(text, path) if name == 'config' else parse(text))
                    except ValueError as error:
                        raise ValueError(str(path) + ': ' + str(error)) from error
                config, rules, holds = parsed
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
                for account, count in previous.items():
                    if account == '_positions':
                        continue
                    if type(count) is not int or count < 0:
                        raise ValueError(str(state) + ': state.json.' + account + ': expected a non-negative integer count')
                stops = ['Missing state; use --first-run only for an explicit first run'] if not state.exists() and not args.first_run else []
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
                        result = build_plan(listing, [], holds, config, previous, ['Reply refused: ' + ' '.join(str(error).split())])
                number_decisions(store, result, listing)
                result['first_run'] = args.first_run
                report = render_report(result, listing, config.get('language', 'en'))
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
                        print(str(entry['number']) + '. [' + safe_text(entry['account']) + '] ' + safe_text(m['sender']) + ': "' + safe_text(m['subject']) + '" ' + entry['verb'])
                payload = json.dumps(result, ensure_ascii=True, indent=2) + '\n'
                outputs = []
                temporary = []
                try:
                    for name, content in ((args.plan_out, payload), (args.report_out, report)):
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
                        source.replace(destination)
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
            for name in (args.plan_out, args.report_out):
                if name:
                    try:
                        destination = private_store(name, output=True)
                        if destination.is_file() and destination not in protected:
                            destination.unlink()
                    except (ValueError, OSError):
                        pass
            try:
                release(store, args.token)
            except (ValueError, FileNotFoundError):
                pass
            raise


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as error:
        print('Refused: ' + ' '.join(str(error).split()), file=sys.stderr)
        sys.exit(1)
