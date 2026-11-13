from mock_provider import save_plan
import http.client
import http.server
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import execute
import triage
import unsubscribe as client

PUBLIC = str(ipaddress.ip_address(0x08080808))
LOOP = str(ipaddress.ip_address(0x7f000001))


def answers(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443)) for a in addresses]


class ClientTests(unittest.TestCase):
    def test_refusals(self):
        cases = [
            ('https://example.net/', [LOOP]),
            ('https://example.net/', [PUBLIC, LOOP]),
            ('https://example.net/', ['::ffff:' + LOOP]),
            ('https://' + 'user' + '@' + 'example.net/', [PUBLIC]),
            ('https://example.net:444/', [PUBLIC]),
            ('http://example.net/', [PUBLIC]),
            ('https://example.net/', [str(ipaddress.ip_address(0x64400001))]),
            ('https://example.net/', ['2001:db8::1']),
            ('https://[' + '::1' + ']/', ['::1']),
        ]
        for url, addresses in cases:
            with self.subTest(url=url, addresses=addresses):
                transport = unittest.mock.Mock(return_value=(200, {}, b''))
                self.assertEqual(client.request(url, resolver=lambda *a, **k: answers(*addresses), transport=transport), 'failed')
                transport.assert_not_called()

    def test_pin_and_local_server(self):
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.command, self.headers, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer((LOOP, 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            resolver = unittest.mock.Mock(side_effect=[answers(PUBLIC), answers(LOOP)])
            def transport(host, address, method, target):
                self.assertEqual((host, address), ('example.net', PUBLIC))
                connection = http.client.HTTPConnection(LOOP, server.server_port, timeout=2)
                try:
                    connection.request(method, target, b'List-Unsubscribe=One-Click')
                    r = connection.getresponse()
                    return r.status, {}, r.read()
                finally:
                    connection.close()
            self.assertEqual(client.request('https://example.net/', True, resolver=resolver, transport=transport), 'executed')
            self.assertEqual(resolver.call_count, 1)
            self.assertEqual(seen[0][2], b'List-Unsubscribe=One-Click')
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_real_exchange_pins_socket_and_tls_name(self):
        raw = unittest.mock.MagicMock()
        context = unittest.mock.MagicMock()
        connection = unittest.mock.MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheaders.return_value = []
        response.read.return_value = b'ok'
        with patch.object(client.socket, 'socket', return_value=raw), patch.object(client.ssl, 'create_default_context', return_value=context), patch.object(client.http.client, 'HTTPSConnection', return_value=connection), patch.object(client.socket, 'getaddrinfo', side_effect=AssertionError('second DNS lookup')):
            client.exchange('example.net', PUBLIC, 'POST', '/leave')
        raw.connect.assert_called_once_with((PUBLIC, 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname='example.net')
        self.assertEqual(connection.request.call_args.kwargs['body'], b'List-Unsubscribe=One-Click')
        response.read.assert_called_once_with(client.LIMIT + 1)

    def test_redirect_and_login(self):
        for response in [
            (302, {'location': 'http://example.net/'}, b''),
            (302, {'location': 'https://private.invalid/'}, b''),
            (302, {'location': '/again'}, b''),
            (401, {}, b''), (407, {}, b''),
            (200, {'www-authenticate': 'Basic'}, b''),
            (200, {}, b'<form><input type="password"></form>'),
        ]:
            with self.subTest(response=response[:2]):
                transport = unittest.mock.Mock(return_value=response)
                def resolver(host, *a, **k):
                    return answers(LOOP if host == 'private.invalid' else PUBLIC)
                self.assertEqual(client.request('https://example.net/', resolver=resolver, transport=transport), 'failed')
                self.assertLessEqual(transport.call_count, 4)

    def test_redirect_methods(self):
        for code in (301, 302, 303, 307, 308):
            transport = unittest.mock.Mock(side_effect=[(code, {'location': '/end'}, b''), (204, {}, b'')])
            self.assertEqual(client.request('https://example.net/', True, resolver=lambda *a, **k: answers(PUBLIC), transport=transport), 'executed')
            self.assertEqual(transport.call_args.args[2], 'POST' if code in (307, 308) else 'GET')


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        self.token = triage.acquire(self.store)
        (self.store / 'config.toml').write_text('[accounts.cedar]\n')
        (self.store / 'rules.toml').write_text('[[rules]]\nid="leave"\naccounts=["cedar"]\nsubject="Weekly"\naction="unsubscribe"\nafter_unsubscribe="trash"\n')
        (self.store / 'never-touch.toml').write_text('allow_empty=true\n')
        self.message = dict(account='cedar', id='m1', folder='Inbox', sender='a@example.net', subject='Weekly', unsubscribe_url='https://example.net/', unsubscribe_one_click=True)

    def machine(self):
        listing = dict(accounts={'cedar': 100}, messages=[self.message])
        execute.atomic(self.store / 'inbox.json', listing)
        plan = triage.build_plan(listing, triage.parse_rules((self.store / 'rules.toml').read_text()), [], {'accounts': {'cedar': {}}})
        plan['first_run'] = True
        save_plan(self.store, self.token, self.store / 'plan.json', plan)
        return execute.Execution(self.store, self.token, self.store / 'plan.json')

    def test_unsafe_unsubscribe_is_decision_without_companion(self):
        for url, flags in [('mailto:leave@example.net', {}), ('http://example.net/', {}),
                           ('https://example.net/', {'spam': True}),
                           ('https://example.net/', {'phishing': True})]:
            self.message.update(unsubscribe_url=url, spam=False, phishing=False)
            self.message.update(flags)
            machine = self.machine()
            self.assertEqual(machine.plan['actions'], [])
            self.assertEqual(machine.plan['decisions'][0]['reason'], 'Unsafe unsubscribe')

    def test_run_command_carries_store_into_fresh_shell(self):
        command = self.machine().step()['run']
        # A remembered sender is refused before any lookup, so no network is used.
        secret = '0' * 64
        (self.store / 'unsubscribe-secret').write_text(secret)
        digest = execute.hmac.new(bytes.fromhex(secret), b'cedar\0a@example.net', execute.hashlib.sha256).hexdigest()
        execute.atomic(self.store / 'unsubscribe-failures.json', {'version': 2, 'senders': [digest]})
        home = self.store / 'home'
        home.mkdir()
        # Fresh shell: no PIGEON_HOME, and the default store does not exist.
        env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'unsubscribe_not_attempted')

    def test_non_https_not_attempted_or_remembered(self):
        machine = self.machine()
        # Test the execution defence separately from the planner.
        call = dict(machine.plan['actions'][0], url='http://example.net/')
        with triage.lock_gate(self.store) as gate, patch.object(client, 'request', side_effect=AssertionError('network')):
            self.assertEqual(machine.unsubscribe(call, gate), 'unsubscribe_not_attempted')
        self.assertFalse((self.store / 'unsubscribe-failures.json').exists())
        self.assertEqual(machine.state['failures'], {})

    def test_failed_remembered_and_separate(self):
        machine = self.machine()
        machine.step()
        with patch.object(client, 'request', return_value='failed') as request:
            self.assertEqual(machine.step(local=True)['status'], 'failed')
            self.assertEqual(machine.step()['action'], 'trash')
            self.assertEqual(execute.read(self.store / 'results.json')['results'][0]['status'], 'failed')
            triage.release(self.store, self.token)
            self.token = triage.acquire(self.store)
            machine = self.machine()
            machine.step()
            self.assertEqual(machine.step(local=True)['status'], 'unsubscribe_not_attempted')
            self.assertEqual(request.call_count, 1)
        entries = [json.loads(s) for s in (self.store / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual(entries[1]['verb'], 'unsubscribe')
        self.assertEqual(entries[1]['outcome'], 'failed')

    def test_private_redirect_records_failure_before_companion(self):
        machine = self.machine()
        machine.step()
        original = client.request
        def request(url, one_click):
            return original(url, one_click,
                            resolver=lambda host, *a, **k: answers(LOOP if host == 'private.invalid' else PUBLIC),
                            transport=lambda *a: (302, {'location': 'https://private.invalid/'}, b''))
        with patch.object(client, 'request', side_effect=request):
            self.assertEqual(machine.step(local=True), {'status': 'failed'})
        self.assertEqual(execute.read(self.store / 'results.json')['results'],
                         [dict(account='cedar', id='m1', action='unsubscribe', status='failed')])
        self.assertEqual(machine.step()['action'], 'trash')
        self.assertEqual(len(execute.read(self.store / 'results.json')['results']), 1)

    def test_flags_and_strict_store(self):
        for flag in ('spam', 'phishing'):
            self.message[flag] = True
            machine = self.machine()
            call = dict(account='cedar', id='m1', action='unsubscribe', rule='leave', url='https://example.net/')
            with triage.lock_gate(self.store) as gate, patch.object(client, 'request', side_effect=AssertionError('network')):
                self.assertEqual(machine.unsubscribe(call, gate), 'unsubscribe_not_attempted')
            self.message[flag] = False
            triage.release(self.store, self.token)
            self.token = triage.acquire(self.store)
        execute.atomic(self.store / 'unsubscribe-failures.json', {'senders': [], 'typo': True})
        machine = self.machine()
        machine.step()
        with self.assertRaisesRegex(ValueError, 'typo'):
            machine.step(local=True)

    def test_gateway_cannot_claim_success(self):
        machine = self.machine()
        machine.step()
        with self.assertRaisesRegex(ValueError, 'local command'):
            machine.step(execute.AssistantProvider(dict(outcome='ok', inbox_ids=[])))

    def test_checkpoint_unknown_keys_refused(self):
        machine = self.machine()
        machine.step()
        original = execute.read(machine.path)
        for key, value in [('failuers', {}), ('failures', {'trahs': 2})]:
            state = dict(original)
            state[key] = value
            execute.atomic(machine.path, state)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'execution-.*' + ('trahs' if key == 'failures' else key)):
                execute.Execution(self.store, self.token, self.store / 'plan.json').step(local=True)

    def test_checkpoint_pending_matches_plan_on_reload(self):
        machine = self.machine()
        machine.step()
        state = execute.read(machine.path)
        state['pending'] = dict(state['pending'], url='mailto:other@example.org')
        execute.atomic(machine.path, state)
        with self.assertRaisesRegex(ValueError, 'pending'):
            machine.step(local=True)

    def test_checkpoint_results_prefix_refused(self):
        machine = self.machine()
        machine.step()
        state = execute.read(machine.path)
        state['results'] = [dict(account='cedar', id='other', action='unsubscribe', status='failed')]
        state['pending'] = None
        execute.atomic(machine.path, state)
        with self.assertRaisesRegex(ValueError, 'results'):
            execute.Execution(self.store, self.token, self.store / 'plan.json')

    def test_checkpoint_nested_types_and_unknown_result_keys(self):
        machine = self.machine()
        machine.step()
        original = execute.read(machine.path)
        malformed = [
            ('failures', {'trash': True}, 'failures.trash'),
            ('failures', {'trash': -1}, 'failures.trash'),
            ('results', {}, 'results'),
            ('pending', dict(original['pending'], typo=True), 'pending.typo'),
            ('results', [dict(account='cedar', id='m1', action='unsubscribe', status='failed', typo=True)], 'results.0.typo'),
        ]
        for key, value, expected in malformed:
            execute.atomic(machine.path, dict(original, **{key: value}))
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                execute.Execution(self.store, self.token, self.store / 'plan.json')

    def test_never_attempted_does_not_brake_or_remember(self):
        self.message['unsubscribe_url'] = 'mailto:leave@example.net'
        machine = self.machine()
        listing = execute.read(self.store / 'inbox.json')
        listing['messages'] = [dict(self.message, id=str(i), sender='sender' + str(i) + '@example.net',
                                    unsubscribe_url='https://example.net/' if i == 2 else 'https://example.net/') for i in range(3)]
        execute.atomic(self.store / 'inbox.json', listing)
        rules = triage.parse_rules((self.store / 'rules.toml').read_text())
        rules[0].pop('after_unsubscribe')
        (self.store / 'rules.toml').write_text('[[rules]]\nid="leave"\naccounts=["cedar"]\nsubject="Weekly"\naction="unsubscribe"\n')
        plan = triage.build_plan(listing, rules, [], {'accounts': {'cedar': {}}})
        plan['first_run'] = True
        save_plan(self.store, self.token, self.store / 'plan.json', plan)
        machine = execute.Execution(self.store, self.token, self.store / 'plan.json')
        for _ in range(2):
            machine.step()
            with patch.object(client, 'request', return_value='unsubscribe_not_attempted'):
                self.assertEqual(machine.step(local=True)['status'], 'unsubscribe_not_attempted')
        self.assertFalse((self.store / 'unsubscribe-failures.json').exists())
        self.assertIn('run', machine.step())
        with patch.object(client, 'request', return_value='executed') as request:
            self.assertEqual(machine.step(local=True)['status'], 'executed')
            request.assert_called_once()

class DeadlineTests(unittest.TestCase):
    def test_overall_deadline_across_redirects(self):
        import time
        def transport(*args):
            time.sleep(.06)
            return 302, {'location': '/again'}, b''
        start = time.monotonic()
        with patch.object(client, 'DEADLINE', .1, create=True):
            self.assertEqual(client.request('https://example.net/', resolver=lambda *a, **k: answers(PUBLIC), transport=transport), 'failed')
        self.assertLess(time.monotonic() - start, .2)

    def test_header_cap(self):
        import io
        class Sock:
            def makefile(self, *args):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nX: ' + b'a' * client.LIMIT + b'\r\n\r\n')
        with self.assertRaises(http.client.HTTPException):
            getattr(client, 'BoundedResponse', http.client.HTTPResponse)(Sock()).begin()

class NetworkGateTests(unittest.TestCase):
    setUp = FlowTests.setUp
    machine = FlowTests.machine
    def test_network_releases_gate_and_rechecks_lock(self):
        import fcntl
        machine = self.machine()
        machine.step()
        before = machine.path.read_bytes()
        def request(*args):
            with (self.store / '.lock-gate').open('a') as gate:
                fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = triage.read_lock(self.store)
                held['created'] -= 2701
                execute.atomic(self.store / 'run.lock', held)
            return 'failed'
        with patch.object(client, 'request', side_effect=request):
            result = machine.step(local=True)
        self.assertIn('stop', result)
        self.assertEqual([(a['action'], a['status']) for a in result['remaining']], [('unsubscribe', 'issued_unknown'), ('trash', 'unexecuted')])
        self.assertEqual(machine.path.read_bytes(), before)
        self.assertFalse((self.store / 'unsubscribe-failures.json').exists())

class LargeResponseTests(unittest.TestCase):
    def test_large_success_uses_status_and_sample(self):
        self.assertEqual(client.request('https://example.net/', resolver=lambda *a, **k: answers(PUBLIC), transport=lambda *a: (200, {}, b'x' * (client.LIMIT + 1))), 'executed')

class KeyedMemoryTests(unittest.TestCase):
    setUp = FlowTests.setUp
    machine = FlowTests.machine
    def test_failure_memory_key_and_legacy_discard(self):
        import hashlib
        old = hashlib.sha256(b'cedar\0a@example.net').hexdigest()
        execute.atomic(self.store / 'unsubscribe-failures.json', {'senders': [old]})
        machine = self.machine()
        machine.step()
        with patch.object(client, 'request', return_value='failed') as request:
            self.assertEqual(machine.step(local=True)['status'], 'failed')
            request.assert_called_once()
        memory = execute.read(self.store / 'unsubscribe-failures.json')
        self.assertEqual(memory['version'], 2)
        self.assertNotIn(old, memory['senders'])
        import hmac
        key = bytes.fromhex((self.store / 'unsubscribe-secret').read_text())
        self.assertEqual(memory['senders'], [hmac.new(key, b'cedar\0a@example.net', hashlib.sha256).hexdigest()])
        self.assertEqual((self.store / 'unsubscribe-secret').stat().st_mode & 0o777, 0o600)
