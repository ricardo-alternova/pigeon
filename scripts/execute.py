#!/usr/bin/env python3
"""Offline execution step machine; the assistant transports issued calls."""
import argparse
import fcntl
import hashlib
import hmac
import secrets
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import triage
import unsubscribe


def read(path):
    return triage.parse_json(triage.load_inputs([path])[0], path)


def atomic(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as out:
            temporary = Path(out.name)
            json.dump(value, out, ensure_ascii=True)
            out.flush()
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class UncertainRequest(ValueError):
    pass


class AssistantProvider:
    """Transport observations, never a gateway connection."""
    def __init__(self, observation):
        triage.known_keys(observation, {'outcome', 'inbox_ids', 'folder_id'}, 'observation')
        if observation.get('outcome') not in ('ok', 'error', 'unknown'):
            raise ValueError('observation.outcome: expected ok, error or unknown (create-folder only)')
        ids = observation.get('inbox_ids')
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise ValueError('observation.inbox_ids: expected complete post-action Inbox id array for the issued account')
        self.observation = observation

    def perform(self, call):
        return self.observation['outcome']

    def list_inbox_ids(self, account):
        return self.observation['inbox_ids']


class Execution:
    def __init__(self, store, token, plan_path):
        self.store, self.token = store, token
        self.path = store / ('execution-' + hashlib.sha256(token.encode()).hexdigest() + '.json')
        self.plan_path = Path(plan_path).resolve()
        outputs = {store / n for n in ('results.json', 'execution-report.md', 'calls.jsonl')}
        if self.plan_path in outputs or self.plan_path == self.path:
            raise ValueError(str(plan_path) + ': expected a plan path distinct from execution outputs')
        triage.require_fetched(store, token)
        self.plan = read(self.plan_path)
        self.signature = hashlib.sha256(json.dumps(self.plan, sort_keys=True).encode()).hexdigest()
        self.state = read(self.path) if self.path.exists() else dict(signature=self.signature, pending=None, results=[], failures={})
        self.validate_checkpoint()

    def validate_folder_id(self, value, results, label):
        known = {f['id'] for entries in read(self.store / 'inbox.json').get('folders', {}).values() for f in entries}
        known.update(e['folder_id'] for e in results if 'folder_id' in e)
        if (not isinstance(value, str) or not value.strip() or len(value) > 512
                or any(triage.unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in value)
                or value in known):
            raise ValueError(label + '.folder_id: expected a new unique folder id of 1 to 512 characters without controls or separators')

    def validate_checkpoint(self):
        label = str(self.path)
        triage.known_keys(self.state, {'signature', 'pending', 'results', 'failures'}, label)
        if self.state.get('signature') != self.signature:
            raise ValueError(label + '.signature: expected unchanged plan during execution')
        failures = self.state.get('failures')
        triage.known_keys(failures, triage.PLAN_VERBS, label + '.failures')
        for verb, count in failures.items():
            if type(count) is not int or count < 0:
                raise ValueError(label + '.failures.' + verb + ': expected a non-negative integer')
        results = self.state.get('results')
        if not isinstance(results, list) or len(results) > len(self.plan['actions']):
            raise ValueError(label + '.results: expected a prefix of planned results')
        statuses = {'executed', 'failed', 'skipped', 'unexecuted', 'unsubscribe_not_executable', 'unsubscribe_not_attempted', 'issued_unknown'}
        for i, entry in enumerate(results):
            source = label + '.results.' + str(i)
            triage.known_keys(entry, {'account', 'id', 'action', 'status', 'folder_id'}, source)
            call = self.plan['actions'][i]
            if (any(entry.get(k) != call[k] for k in ('account', 'id', 'action'))
                    or not isinstance(entry.get('status'), str) or entry['status'] not in statuses):
                raise ValueError(source + ': expected matching planned action and status ' + ', '.join(sorted(statuses)))
            if call['action'] == 'create-folder' and entry['status'] == 'executed':
                self.validate_folder_id(entry.get('folder_id'), results[:i], source)
            elif 'folder_id' in entry:
                raise ValueError(source + '.folder_id: expected only on successful creation')
        if 'pending' not in self.state:
            raise ValueError(label + '.pending: expected null or the next planned action')
        pending = self.state['pending']
        if pending is not None:
            triage.known_keys(pending, {'account', 'id', 'action', 'rule', 'url', 'target', 'target_path'}, label + '.pending')
            if len(results) == len(self.plan['actions']) or pending != self.plan['actions'][len(results)]:
                raise ValueError(label + '.pending: expected the next planned action unchanged')

    def validate(self):
        listing = read(self.store / 'inbox.json')
        triage.validate_actions(self.plan['actions'], listing)
        config = triage.parse_toml(triage.load_inputs([self.store / 'config.toml'])[0], self.store / 'config.toml')
        rules = triage.parse_rules(triage.load_inputs([self.store / 'rules.toml'])[0])
        holds = triage.parse_rules(triage.load_inputs([self.store / 'never-touch.toml'])[0], protection=True)
        previous = read(self.store / 'state.json') if (self.store / 'state.json').exists() else {}
        first_run = self.plan.get('first_run', False)
        if type(first_run) is not bool:
            raise ValueError(str(self.plan_path) + '.first_run: expected boolean')
        stops = ['Missing state; use --first-run only for an explicit first run'] if not (self.store / 'state.json').exists() and not first_run else []
        if 'reply' in self.plan:
            saved = read(self.store / 'reply.json')
            if saved.get('token') != self.token:
                raise ValueError('reply.json: expected current token')
            expected = triage.reply_plan(self.store, listing, holds, config, previous, saved['parts'], stops)
            if self.plan['reply'] != expected['reply']:
                raise ValueError('reply: expected saved user selections')
        elif self.plan['reasons'] and self.plan['reasons'][0].startswith('Reply refused:'):
            expected = triage.build_plan(listing, [], holds, config, previous, self.plan['reasons'])
        else:
            expected = triage.build_plan(listing, rules, holds, config, previous, stops)
        if self.plan['actions'] != expected['actions'] or self.plan['counts'] != expected['counts']:
            raise ValueError(str(self.plan_path) + '.actions: expected actions and counts from current private inputs and Inbox listing')

    def log(self, call, outcome):
        append_log(self.store, self.token, call, outcome)

    def save(self):
        atomic(self.path, self.state)
        atomic(self.store / 'results.json', {'results': self.state['results']})
        self.report()

    def report(self):
        render_summary(self.store, self.token, self.signature, self.plan_path)

    def record(self, call, status, **extra):
        self.state['results'].append({k: call[k] for k in ('account', 'id', 'action')} | {'status': status} | extra)

    def unsubscribe(self, call, gate):
        path = self.store / 'unsubscribe-failures.json'
        memory = read(path) if path.exists() else {'senders': []}
        triage.validate_unsubscribe_memory(memory)
        message = next(m for m in read(self.store / 'inbox.json')['messages']
                       if (m['account'], m['id']) == (call['account'], call['id']))
        secret = self.store / 'unsubscribe-secret'
        if 'version' not in memory:
            # Unsalted digests cannot be converted without the original senders.
            memory = {'version': 2, 'senders': []}
            if path.exists():
                atomic(path, memory)
        if not secret.exists():
            if memory['senders']:
                raise ValueError(secret.name + ': expected existing secret for keyed memory')
            with open(secret, 'x', encoding='ascii', opener=lambda name, flags: os.open(name, flags, 0o600)) as out:
                out.write(secrets.token_hex(32))
        key_text = triage.read_text(secret, encoding='ascii')
        if len(key_text) != 64 or any(c not in '0123456789abcdef' for c in key_text):
            raise ValueError(secret.name + ': expected 32-byte lowercase hex secret')
        sender = hmac.new(bytes.fromhex(key_text), (call['account'] + '\0' + triage.normalize_sender(message['sender'])).encode(), hashlib.sha256).hexdigest()
        if sender in memory['senders'] or message.get('spam') or message.get('phishing'):
            status = 'unsubscribe_not_attempted'
        elif call['url'].lower().startswith('mailto:') or not call['url']:
            status = 'unsubscribe_not_executable'
        elif not call['url'].lower().startswith('https:'):
            status = 'unsubscribe_not_attempted'
        else:
            fcntl.flock(gate, fcntl.LOCK_UN)
            try:
                status = unsubscribe.request(call['url'], message.get('unsubscribe_one_click', False))
            finally:
                fcntl.flock(gate, fcntl.LOCK_EX)
            try:
                triage.require_lock(self.store, self.token)
            except ValueError as error:
                raise UncertainRequest(str(error)) from error
            if read(self.path) != self.state:
                raise UncertainRequest(str(self.path) + ': checkpoint changed during request; outcome unknown')
        if status == 'failed' and sender not in memory['senders']:
            memory['senders'].append(sender)
            atomic(path, memory)
        return status

    def step(self, provider=None, *, local=False):
        with triage.lock_gate(self.store) as gate:
            try:
                triage.require_lock(self.store, self.token)
            except ValueError as error:
                remaining = [a | {'status': 'issued_unknown' if i == 0 and self.state['pending'] is not None else 'unexecuted'}
                             for i, a in enumerate(self.plan['actions'][len(self.state['results']):])]
                # No shared-store writes after losing ownership.
                return {'done': True, 'stop': str(error), 'remaining': remaining}
            if self.path.exists():
                self.state = read(self.path)
                self.validate_checkpoint()
            if self.plan['reasons']:
                self.save()
                return {'done': True}
            try:
                self.validate()
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(str(self.plan_path) + '.actions: expected valid planned Inbox actions; ' + str(error)) from error
            pending = self.state['pending']
            if local:
                if pending is None or pending['action'] != 'unsubscribe':
                    raise ValueError('pending: expected an issued unsubscribe command')
                try:
                    status = self.unsubscribe(pending, gate)
                except UncertainRequest as error:
                    later = self.plan['actions'][len(self.state['results']) + 1:]
                    return {'done': True, 'stop': str(error), 'remaining': [pending | {'status': 'issued_unknown'}] + [a | {'status': 'unexecuted'} for a in later]}
                if status in ('executed', 'failed'):
                    self.state['failures']['unsubscribe'] = 0 if status == 'executed' else self.state['failures'].get('unsubscribe', 0) + 1
                self.record(pending, status)
                self.state['pending'] = None
                self.log(pending, status)
                self.save()
                return {'status': status}
            if provider is not None:
                if pending is not None and pending['action'] == 'unsubscribe':
                    raise ValueError('unsubscribe: expected local command, never a gateway observation')
                if pending is None:
                    raise ValueError(str(self.path) + '.pending: expected an issued call before result')
                if pending['action'] == 'create-folder':
                    observation = provider.observation
                    if observation['outcome'] == 'ok':
                        self.validate_folder_id(observation.get('folder_id'), self.state['results'], 'observation')
                    elif 'folder_id' in observation:
                        raise ValueError('observation.folder_id: expected only on successful creation')
                elif isinstance(provider, AssistantProvider) and (provider.observation['outcome'] == 'unknown' or 'folder_id' in provider.observation):
                    raise ValueError('observation: unknown and folder_id are for create-folder only')
                if isinstance(provider, AssistantProvider):
                    ids = provider.list_inbox_ids(pending['account'])
                    removed = {e['id'] for e in self.state['results'] if e['account'] == pending['account'] and e['status'] == 'executed' and e['action'] in {'trash', 'archive', 'move', 'spam'}}
                    required = {m['id'] for m in read(self.store / 'inbox.json')['messages'] if m['account'] == pending['account']} - removed - ({pending['id']} if pending['action'] != 'create-folder' else set())
                    if len(ids) != len(set(ids)) or not required <= set(ids):
                        raise ValueError('observation.inbox_ids: expected complete Inbox reading retaining other not-yet-removed ids for this account')
                outcome = provider.perform(pending)
                if pending['action'] == 'create-folder':
                    status = {'ok': 'executed', 'error': 'failed', 'unknown': 'issued_unknown'}[outcome]
                    extra = {'folder_id': provider.observation['folder_id']} if status == 'executed' else {}
                    self.state['failures']['create-folder'] = 0 if status == 'executed' else self.state['failures'].get('create-folder', 0) + 1
                    self.record(pending, status, **extra)
                    self.state['pending'] = None
                    self.log(pending, status)
                    self.save()
                    return {'status': status}
                absent = pending['id'] not in provider.list_inbox_ids(pending['account'])
                status = 'executed' if outcome == 'ok' and absent else ('failed' if outcome == 'error' else 'unexecuted')
                verb = pending['action']
                self.state['failures'][verb] = 0 if status == 'executed' else self.state['failures'].get(verb, 0) + 1
                self.record(pending, status)
                self.state['pending'] = None
                self.log(pending, status)
                self.save()
                return {'status': status}
            if pending is not None:
                raise ValueError(str(self.path) + '.pending: expected result before another next')
            while len(self.state['results']) < len(self.plan['actions']):
                call = self.plan['actions'][len(self.state['results'])]
                if call['action'] == 'move' and 'target_path' in call:
                    creation = next(e for a, e in zip(self.plan['actions'], self.state['results'])
                                    if a['action'] == 'create-folder' and a['account'] == call['account'] and a['target_path'] == call['target_path'])
                    if creation['status'] != 'executed':
                        self.record(call, 'unexecuted')
                        self.log(call, 'unexecuted')
                        self.save()
                        continue
                if self.state['failures'].get(call['action'], 0) >= 2:
                    self.record(call, 'skipped')
                    self.log(call, 'skipped')
                    self.save()
                    continue
                self.state['pending'] = call
                self.log(call, 'issued')
                self.save()
                if call['action'] == 'unsubscribe':
                    # Carry the store so the command works in a fresh shell.
                    return {'run': ['env', 'PIGEON_HOME=' + str(self.store), 'python3', str(Path(__file__).resolve()), 'unsubscribe',
                                    '--token', self.token, '--plan', str(self.plan_path)]}
                issued = dict(call)
                if call['action'] == 'move' and 'target_path' in call:
                    issued['target'] = creation['folder_id']
                return issued | {'verify': {'account': call['account'], 'read': 'complete Inbox ids; never re-read the old id'}}
            self.save()
            return {'done': True}



def render_summary(store, token, signature=None, plan_path=None):
    # Only this token's checkpoint can supply handled totals.
    path = store / ('execution-' + hashlib.sha256(token.encode()).hexdigest() + '.json')
    checkpoint = Execution(store, token, plan_path or store / 'plan.json').state if path.exists() else None
    if checkpoint is not None and signature is not None and checkpoint['signature'] != signature:
        raise ValueError(str(path) + '.signature: expected current plan')
    results = checkpoint['results'] if checkpoint is not None else []
    counts = {}
    for entry in results:
        if entry['status'] == 'executed' and entry['action'] != 'create-folder':
            verb = entry['action']
            counts[verb] = counts.get(verb, 0) + 1
    lines = ['Run: ' + triage.safe_text(token), 'Plan: ' + (checkpoint['signature'] if checkpoint else 'no execution checkpoint'), '']
    if checkpoint is None:
        lines += ['No execution step has been recorded for this run.', '']
    lines += ['**Handled automatically**', '']
    lines += [triage.safe_text(v) + ': ' + str(n) for v, n in sorted(counts.items())] or ['none']
    lines += ['', '**Issued calls to check (outcome unknown)**', '']
    pending = checkpoint.get('pending') if checkpoint else None
    lines += [triage.safe_text(json.dumps({k: pending[k] for k in ('account', 'id', 'action')} | {'status': 'issued_unknown'}, ensure_ascii=True))] if pending else ['none']
    lines += ['', '**Actions not completed**', '']
    incomplete = []
    if checkpoint:
        for i, call in enumerate(Execution(store, token, plan_path or store / 'plan.json').plan['actions']):
            status = results[i]['status'] if i < len(results) else ('issued_unknown' if pending == call else 'unexecuted')
            if status != 'executed':
                incomplete.append(triage.safe_text(json.dumps(call | {'status': status}, ensure_ascii=True)))
    lines += incomplete or ['none']
    lines += ['', '**Folders requiring your help**', '']
    instructions = []
    if checkpoint:
        for i, call in enumerate(Execution(store, token, plan_path or store / 'plan.json').plan['actions']):
            if call['action'] != 'create-folder':
                continue
            result = results[i] if i < len(results) else None
            if (result and result['status'] != 'executed') or pending == call:
                instruction = 'Check whether this folder exists; create it if absent: ' if pending == call or result['status'] == 'issued_unknown' else 'Create this folder: '
                instructions.append(triage.safe_text(call['account']) + ': ' + instruction + triage.code_path(call['target_path']) + '; dependent messages stay in Inbox this run.')
    lines += instructions or ['none']
    lines += ['', '**Unplanned calls**', '']
    log = store / 'calls.jsonl'
    unplanned = [triage.parse_json(line, log) for line in triage.read_text(log).splitlines()] if log.exists() else []
    lines += [triage.safe_text(json.dumps(e, ensure_ascii=True)) for e in unplanned
              if e['run'] == token and e['outcome'] == 'unplanned'] or ['none']
    atomic_text(store / 'execution-report.md', '\n'.join(lines) + '\n')

def atomic_text(path, text):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(text)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def append_log(store, token, call, outcome):
    path = store / 'calls.jsonl'
    sequence = len(triage.read_text(path).splitlines()) + 1 if path.exists() else 1
    entry = dict(sequence=sequence, time=time.time(), run=token, account=call['account'], id=call['id'],
                 verb=call['action'], outcome=outcome)
    # JSON quoting escapes controls; Markdown rendering additionally uses safe_text.
    with path.open('a', encoding='utf-8') as out:
        out.write(json.dumps(entry, ensure_ascii=True) + '\n')
        out.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('next', 'result', 'observed', 'unsubscribe'):
        command = sub.add_parser(name, allow_abbrev=False)
        command.add_argument('--token', required=True)
        if name != 'observed':
            command.add_argument('--plan', required=True)
        if name == 'result':
            command.add_argument('--observation', required=True, help='JSON object: outcome (ok/error for removals; ok/error/unknown for creation), inbox_ids, and folder_id only for successful creation')
        if name == 'observed':
            command.add_argument('--plan', help='Current plan path; defaults to private plan.json')
            for key in ('account', 'id', 'verb'):
                command.add_argument('--' + key, required=True)
    args = parser.parse_args()
    store = triage.private_store()
    if args.command == 'observed':
        with triage.lock_gate(store):
            triage.require_lock(store, args.token)
            if (store / ('execution-' + hashlib.sha256(args.token.encode()).hexdigest() + '.json')).exists():
                Execution(store, args.token, args.plan or store / 'plan.json')
            append_log(store, args.token, dict(account=args.account, id=args.id, action=args.verb), 'unplanned')
            render_summary(store, args.token, plan_path=args.plan)
        print(json.dumps({'recorded': True}))
    else:
        execution = Execution(store, args.token, args.plan)
        provider = None
        if args.command == 'result':
            try:
                observation = json.loads(args.observation)
            except ValueError as error:
                raise ValueError('--observation: expected a JSON object with outcome and inbox_ids') from error
            provider = AssistantProvider(observation)
        print(json.dumps(execution.step(provider, local=args.command == 'unsubscribe'), ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as error:
        print('Refused: ' + ' '.join(str(error).split()), file=sys.stderr)
        sys.exit(1)
