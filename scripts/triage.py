#!/usr/bin/env python3
"""Offline Inbox planner. No mail or network operations.

commit-state saves Inbox counts after a run. Pass results.json written by
execute.py; hand-written files are refused. expected_counts is a forecast,
never saved state. See docs/setup.md for result formats and statuses.
"""
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
STALE_SECONDS = 45 * 60


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


def read_lock(store):
    try:
        path = store / 'run.lock'
        if not path.stat().st_mode & 0o444:
            raise ValueError('Run lock is unreadable and held')
        held = json.loads(path.read_text(encoding='utf-8'))
        known_keys(held, {'token', 'created'}, 'run.lock')
        if (not isinstance(held['token'], str) or not held['token']
                or type(held['created']) not in (int, float)
                or not math.isfinite(held['created']) or held['created'] > time.time()):
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
            if time.time() - held['created'] <= STALE_SECONDS:
                raise ValueError(str(store / 'run.lock') + ': Run lock is busy until ' + time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(held['created'] + STALE_SECONDS)) + '; another run holds it. Wait for release or expiry.')
            (store / 'run.lock').unlink()
        token = secrets.token_hex(24)
        with (store / 'run.lock').open('x', encoding='utf-8') as out:
            json.dump({'token': token, 'created': time.time()}, out)
        (store / 'execution-report.md').unlink(missing_ok=True)
        return token


def require_lock(store, token):
    try:
        held = read_lock(store)
    except FileNotFoundError as error:
        raise ValueError(str(store / 'run.lock') + ': expected a lock held by this token, but no lock exists; stop writes') from error
    if not secrets.compare_digest(held['token'], token) or time.time() - held['created'] > STALE_SECONDS:
        raise ValueError(str(store / 'run.lock') + ': token expected to match the holder and be unexpired; stop writes')


def release(store, token):
    with lock_gate(store):
        try:
            held = read_lock(store)
        except FileNotFoundError:
            return 'already released'
        if not secrets.compare_digest(held['token'], token):
            raise ValueError(str(store / 'run.lock') + ': token expected to match the holder; only the holder may release the lock')
        (store / 'run.lock').unlink()
        return 'released'


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

    for key in ('sender', 'domain', 'subject'):
        if key in rule and (not isinstance(rule[key], str) or not rule[key].strip()):
            raise ValueError(key + ': expected a non-empty string')
    if not any(k in rule for k in ('sender', 'domain', 'subject')):
        raise ValueError('Rule requires a message predicate: expected sender, domain or subject')
    try:
        if 'subject' in rule and re.search(rule['subject'], '', re.IGNORECASE):
            raise ValueError('subject: Subject matches empty text; expected a regex that does not match empty text')
    except re.error as error:
        raise ValueError('subject: Invalid subject regex; expected a valid Python regular expression') from error


def parse_rules(text, *, protection=False):
    data = tomllib.loads(text)
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
            if set(rule) - {'id', 'action', 'accounts', 'sender', 'domain', 'subject', 'has_attachment', 'exclusions', 'target', 'after_unsubscribe'}:
                known_keys(rule, {'id', 'action', 'accounts', 'sender', 'domain', 'subject', 'has_attachment', 'exclusions', 'target', 'after_unsubscribe'}, 'rule')
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
    if 'has_attachment' in rule and message.get('has_attachment', False) != rule['has_attachment']:
        return False
    return not any(word.casefold() in message['subject'].casefold() for word in rule.get('exclusions', []))


def validate_actions(actions, listing):
    allowed = {(m['account'], m['id']) for m in listing['messages'] if m.get('folder') == 'Inbox'}
    for action in actions:
        if action['action'] not in VERBS or (action['account'], action['id']) not in allowed:
            raise ValueError('Action is outside this account Inbox listing')


def known_keys(data, allowed, label):
    if not isinstance(data, dict):
        raise ValueError(label + ': expected a table/object')
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(label + '.' + str(sorted(unknown)[0]) + ': expected keys ' + ', '.join(sorted(allowed)))


def validate_inputs(listing, config, previous):
    known_keys(config, {'accounts', 'language', 'inbox_limit', 'timezone'}, 'config')
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
    accounts = config.get('accounts')
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError('config.accounts: expected a non-empty table')
    for account, fields in accounts.items():
        known_keys(fields, {'address', 'executor_app'}, 'config.accounts.' + account)
        for key, value in fields.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError('config.accounts.' + account + '.' + key + ': expected a non-empty string')
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
        strings = required | {'body', 'sender_name', 'unsubscribe_url'}
        known_keys(message, strings | flags, label)
        for key in required | set(message):
            if key not in message or (type(message[key]) is not bool if key in flags else not isinstance(message[key], str)):
                raise ValueError(label + '.' + key + ': expected ' + ('boolean' if key in flags else 'string'))
    folders = listing.get('folders', {})
    known_keys(folders, set(accounts), 'Inbox.folders')
    for account, entries in folders.items():
        if not isinstance(entries, list):
            raise ValueError('Inbox.folders.' + account + ': expected an array')
        for folder in entries:
            known_keys(folder, {'id', 'name'}, 'Inbox.folders.' + account)
            if any(not isinstance(folder.get(k), str) for k in ('id', 'name')):
                raise ValueError('Inbox.folders.' + account + ': expected id and name strings')
    known_keys(previous, set(accounts), 'state.json')
    if any(type(n) is not int or n < 0 for n in previous.values()):
        raise ValueError('state.json: expected non-negative integer counts')


def build_plan(listing, rules, never_touch, config, previous=None, stop_reasons=()):
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
        if sender is None:
            reason = 'Invalid sender'
        elif any(matches(message, hold, protection=True) for hold in never_touch):
            reason = 'Protected by never-touch'
        else:
            for rule in rules:
                if not matches(message, rule):
                    continue
                key = (rule['id'], message['account'])
                hits[key] = hits.get(key, 0) + 1
                verb = rule['action']
                action = {'account': message['account'], 'id': message['id'], 'action': verb, 'rule': rule['id']}
                if verb == 'move':
                    folders = [f for f in listing.get('folders', {}).get(message['account'], []) if f['name'] == rule.get('target')]
                    if len(folders) != 1:
                        reason = 'Move target is ambiguous or missing'
                        break
                    action['target'] = folders[0]['id']
                if verb == 'unsubscribe':
                    action['url'] = message.get('unsubscribe_url', '')
                    if not safe_unsubscribe_host(action['url']) or message.get('spam') or message.get('phishing'):
                        reason = 'Unsafe unsubscribe'
                        break
                if verb == 'report':
                    reason = 'Rule requests a decision'
                    break
                actions.append(action)
                if verb == 'unsubscribe' and 'after_unsubscribe' in rule:
                    actions.append(dict(account=action['account'], id=action['id'],
                                        action=rule['after_unsubscribe'], rule=rule['id']))
                reason = None
                break
        if reason:
            decisions.append({'account': message['account'], 'id': message['id'], 'reason': reason})
    if sum(a['action'] in {'trash', 'move', 'archive', 'spam'} for a in actions) > 50:
        reasons.append('Destructive action limit exceeded')
    if any(sum(a['rule'] == r['id'] for a in actions) > 10 for r in rules):
        reasons.append('Per-rule action limit exceeded')
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


STRINGS = {
    'en': ('Requires your decision', 'Planned actions', 'New rules', 'None', 'Stopped'),
    'es': ('Requiere tu decisión', 'Acciones previstas', 'Reglas nuevas', 'Ninguno', 'Detenido'),
}


def safe_text(value):
    # Mail text remains a single escaped Markdown text line, never raw HTML.
    value = ''.join('[control]' if unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} else c for c in str(value))
    # Linear-time link breaking, Unicode-aware: any dot before a letter, including full-width dots.
    value = re.sub(r'(?<=[^\W_])[.。．｡](?=[^\W\d_])', '[dot]', value)
    value = value.replace('://', '[colon]//').replace('@', '[at]')
    return re.sub(r'([\\`*_{}\[\]()<>!|#])', r'\\\1', ' '.join(value.split()))


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
    lines += [str(i) + '. ' + describe(a) + ' (' + safe_text(a['reason']) + ')' for i, a in enumerate(plan['decisions'] + unsafe, 1)] or [empty]
    lines += ['', '**' + planned + '**', '']
    lines += ['- ' + a['action'] + ': ' + describe(a) + (' | ' + safe_text(hosts.get((a['account'], a['id']), '')) if 'url' in a else '') for a in plan['actions'] if a['action'] != 'unsubscribe' or (a['account'], a['id']) in hosts] or [empty]
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
        except OSError as error:
            raise OSError(str(p) + ': expected a readable file') from error
        if not content.strip():
            raise ValueError(str(p) + ': expected a non-empty safety file')
        result.append(content)
    return result


def commit_state(store, token, plan_path, results_path=None):
    with lock_gate(store):
        require_lock(store, token)
        def read(path):
            try:
                return json.loads(load_inputs([path])[0])
            except ValueError as error:
                raise ValueError(str(path) + ': ' + str(error)) from error
        plan = read(plan_path)
        known_keys(plan, {'actions', 'decisions', 'reasons', 'counts', 'expected_counts', 'not_listed', 'first_run'}, str(plan_path))
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
            known_keys(action, {'account', 'id', 'action', 'rule', 'target', 'url'}, label)
            if any(not isinstance(action.get(k), str) for k in ('account', 'id', 'action')):
                raise ValueError(label + ': expected account, id and action strings')
            if action['action'] not in VERBS or action['account'] not in counts:
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
                known_keys(entry, {'account', 'id', 'action', 'status'}, label)
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
            config = tomllib.loads(load_inputs([config_path])[0])
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
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=store, delete=False) as out:
                temporary = Path(out.name)
                json.dump({**previous, **counts}, out)
            temporary.replace(state)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('lock')
    for name in ('release', 'check'):
        command = sub.add_parser(name)
        command.add_argument('--token', required=True)
    commit = sub.add_parser('commit-state', allow_abbrev=False)
    commit.add_argument('--token', required=True)
    commit.add_argument('--plan', required=True)
    commit.add_argument('--results')
    plan = sub.add_parser('plan', allow_abbrev=False)
    plan.add_argument('--token', required=True)
    plan.add_argument('--inbox', required=True)
    plan.add_argument('--first-run', action='store_true', help='Allow a missing state file on the first run')
    for name in ('config', 'rules', 'never-touch', 'guardrails', 'plan-out', 'report-out'):
        plan.add_argument('--' + name)
    args = parser.parse_args(argv)
    for name in ('inbox', 'config', 'rules', 'never_touch', 'guardrails', 'plan_out', 'report_out', 'plan', 'results'):
        if getattr(args, name, None) == '':
            raise ValueError('--' + name.replace('_', '-') + ': expected a non-empty path')
    store = private_store()
    if args.command == 'lock':
        print(acquire(store))
    elif args.command == 'release':
        print(release(store, args.token))
    elif args.command == 'check':
        with lock_gate(store):
            require_lock(store, args.token)
        print('Lock is held and unexpired')
    elif args.command == 'commit-state':
        commit_state(store, args.token, args.plan, args.results)
    else:
        sources = [getattr(args, n.replace('-', '_')) or store / (n + ('.md' if n == 'guardrails' else '.toml')) for n in ('config', 'rules', 'never-touch', 'guardrails')]
        protected = {Path(p).resolve() for p in sources + [args.inbox]} | {store / n for n in ('state.json', 'run.lock', '.lock-gate', 'calls.jsonl', 'results.json', 'execution-report.md', 'unsubscribe-failures.json', 'unsubscribe-secret', 'inbox.json', 'config.toml', 'rules.toml', 'never-touch.toml', 'guardrails.md')}
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
                texts = load_inputs(sources)
                parsed = []
                for name, text, parse in zip(('config', 'rules', 'never-touch'), texts, (tomllib.loads, parse_rules, lambda t: parse_rules(t, protection=True))):
                    path = getattr(args, name.replace('-', '_')) or store / (name + '.toml')
                    try:
                        parsed.append(parse(text))
                    except ValueError as error:
                        raise ValueError(str(path) + ': ' + str(error)) from error
                config, rules, holds = parsed
                try:
                    listing = json.loads(load_inputs([args.inbox])[0])
                except ValueError as error:
                    raise ValueError(str(args.inbox) + ': expected a JSON Inbox object') from error
                state = store / 'state.json'
                try:
                    previous = json.loads(state.read_text(encoding='utf-8')) if state.exists() else {}
                except ValueError as error:
                    raise ValueError(str(state) + ': expected a JSON account-count object') from error
                if not isinstance(previous, dict):
                    raise ValueError(str(state) + ': expected account keys and non-negative integer counts')
                for account, count in previous.items():
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
                result['first_run'] = args.first_run
                report = render_report(result, listing, config.get('language', 'en'))
                payload = json.dumps(result, ensure_ascii=True, indent=2) + '\n'
                sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
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
    except (ValueError, OSError, KeyError, TypeError) as error:
        print('Refused: ' + ' '.join(str(error).split()), file=sys.stderr)
        sys.exit(1)
