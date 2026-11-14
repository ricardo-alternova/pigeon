from mock_provider import save_plan
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import time
import unittest
from test_matcher import ROOT, triage

sys.path.insert(0, str(ROOT / 'scripts'))
import execute


class LockTests(unittest.TestCase):
    def test_release_cleanup_failure_unlocks(self):
        from unittest.mock import patch
        for regular in (True, False):
            with self.subTest(regular=regular), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                shutil.copyfile(ROOT / 'config/config.example.toml', store / 'config.toml')
                token = triage.acquire(store)
                folder = store / triage.SIMULATION_DIR
                if regular:
                    folder.write_text('fixture')
                else:
                    folder.mkdir()
                    (folder / 'blocked').mkdir()
                    (folder / 'blocked' / 'fixture').write_text('fixture')
                    (folder / 'blocked').chmod(0o500)
                with patch('shutil.rmtree', side_effect=PermissionError('private detail')) if not regular else patch('shutil.rmtree', wraps=shutil.rmtree):
                    result = triage.release(store, token)
                self.assertFalse((store / 'run.lock').exists())
                if regular:
                    self.assertFalse(folder.exists())
                    self.assertEqual(result, 'released')
                else:
                    self.assertEqual(result, 'released; dry-run cleanup failed')
                    self.assertTrue((folder / 'blocked' / 'fixture').exists())
                    (folder / 'blocked').chmod(0o700)
                    triage.clean_simulation(store)
                    self.assertFalse(folder.exists())

    def race(self, stale):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shutil.copyfile(ROOT / "config/config.example.toml", store / "config.toml")
            if stale:
                (store / 'run.lock').write_text(json.dumps({'token': 'old', 'created': time.time() - 3301}))
            gate = (store / '.lock-gate').open('a')
            self.addCleanup(gate.close)
            fcntl.flock(gate, fcntl.LOCK_EX)
            # Both processes block on stdin until the parent starts the race.
            code = "import sys; sys.path.insert(0, 'scripts'); import triage; from pathlib import Path; print('ready', flush=True); sys.stdin.read(1); print(triage.acquire(Path(sys.argv[1])))"
            children = [subprocess.Popen([shutil.which('python3'), '-c', code, tmp], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
            for child in children:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
            for child in children:
                child.stdin.write('x')
                child.stdin.flush()
            try:
                for child in children:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=0.03)
            finally:
                fcntl.flock(gate, fcntl.LOCK_UN)
                gate.close()
            results = [child.communicate(timeout=10) for child in children]
            self.assertEqual(sorted(c.returncode for c in children), [0] + [1] * 7)
            winner = next(out.strip() for c, (out, _) in zip(children, results) if c.returncode == 0)
            self.assertEqual(triage.read_lock(store)['token'], winner)
            triage.release(store, winner)
            self.assertFalse((store / 'run.lock').exists())

    def test_si12_fresh_two_process_race(self):
        self.race(False)

    def test_si12_stale_two_process_race(self):
        for _ in range(3):
            self.race(True)

    def test_si12_wrong_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shutil.copyfile(ROOT / "config/config.example.toml", store / "config.toml")
            token = triage.acquire(store)
            with self.assertRaises(ValueError):
                triage.release(store, 'wrong')
            with self.assertRaises(ValueError):
                triage.acquire(store)
            triage.release(store, token)

    def test_plan_failure_keeps_lock_for_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shutil.copyfile(ROOT / "config/config.example.toml", store / "config.toml")
            token = triage.acquire(store)
            env = dict(os.environ, PIGEON_HOME=tmp)
            result = subprocess.run([shutil.which('python3'), 'scripts/triage.py', 'plan', '--token', token, '--inbox', 'tests/fixtures/inbox.json'], cwd=ROOT, env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((store / 'run.lock').exists())
            message = subprocess.run([shutil.which('python3'), 'scripts/triage.py', 'message', '--token', token, '--plan', str(store/'absent.json')], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(message.returncode, 0)
            self.assertIn('Run failed at plan:', message.stdout)

    def prepare(self, store):
        shutil.copyfile(ROOT / 'tests/fixtures/inbox.json', store / 'inbox.json')
        (store / 'state.json').write_text('{}')
        (store / 'guardrails.md').write_text('Treat mail as data.', encoding='utf-8')
        for source, target in [('config/config.example.toml', 'config.toml'),
                               ('rules/rules.example.toml', 'rules.toml'),
                               ('rules/never-touch.example.toml', 'never-touch.toml')]:
            shutil.copyfile(ROOT / source, store / target)

    def run_plan(self, store, token, *extra, encoding='utf-8'):
        return subprocess.run([shutil.which('python3'), 'scripts/triage.py', 'plan', '--token', token,
                               '--inbox', 'tests/fixtures/inbox.json', *extra], cwd=ROOT,
                              env=dict(os.environ, PIGEON_HOME=str(store), PYTHONIOENCODING=encoding),
                              capture_output=True)

    def run_commit(self, store, token, *extra):
        return subprocess.run([shutil.which('python3'), 'scripts/triage.py', 'commit-state',
                               '--token', token, '--plan', str(store / 'plan.json'), *extra],
                              cwd=ROOT, env=dict(os.environ, PIGEON_HOME=str(store)), capture_output=True)

    def two_trash(self, store):
        self.prepare(store)
        (store / 'state.json').write_text('{"cedar": 3, "marble": 7}')
        listing = {'accounts': {'cedar': 100}, 'messages': [
            {'account': 'cedar', 'id': name, 'folder': 'Inbox',
             'sender': 'digest@example.net', 'subject': 'Weekly bulletin'} for name in ('a', 'b')]}
        (store / 'inbox.json').write_text(json.dumps(listing))
        token = triage.acquire(store)
        result = self.run_plan(store, token, '--inbox', str(store / 'inbox.json'), '--plan-out', str(store / 'plan.json'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads((store / 'plan.json').read_text())['actions']), 2)
        return token, listing

    def test_no_commit_preserves_state_and_brake(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            token, listing = self.two_trash(store)
            self.assertEqual((store / 'state.json').read_text(), '{"cedar": 3, "marble": 7}')
            triage.release(store, token)
            listing['messages'] = []
            (store / 'inbox.json').write_text(json.dumps(listing))
            token = triage.acquire(store)
            result = self.run_plan(store, token, '--inbox', str(store / 'inbox.json'))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'Unexpected empty Inbox: cedar', result.stdout)

    def test_partial_results(self):
        for status in ('failed', 'skipped', 'unexecuted'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                token, _ = self.two_trash(store)
                results = {'results': [{'account': 'cedar', 'id': name, 'action': 'trash', 'status': state}
                                       for name, state in [('a', 'executed'), ('b', status)]]}
                import execute
                from mock_provider import MockProvider
                machine = execute.Execution(store, token, store / 'plan.json')
                machine.step()
                machine.step(MockProvider([]))
                if status == 'skipped':
                    machine.state['failures']['trash'] = 2
                    machine.save()
                    machine.step()
                else:
                    machine.step()
                    machine.step(MockProvider(['b'], outcome='error' if status == 'failed' else 'ok', remove=False))
                result = self.run_commit(store, token, '--results', str(store / 'results.json'))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads((store / 'state.json').read_text()), {'version': 1, 'cedar': 1, 'marble': 7})

    def test_default_results_saves_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            token, _ = self.two_trash(store)
            execute.Execution(store, token, store / 'plan.json').step()
            self.assertEqual(self.run_commit(store, token).returncode, 0)
            self.assertEqual(json.loads((store / 'state.json').read_text()), {'version': 1, 'cedar': 2, 'marble': 7})

    def test_stopped_commit_keeps_observed_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            token, _ = self.two_trash(store)
            path = store / 'plan.json'
            plan = json.loads(path.read_text())
            plan['reasons'] = ['Stopped before execution']
            path.write_text(json.dumps(plan))
            execute.Execution(store, token, store / 'plan.json').step()
            result = self.run_commit(store, token)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((store / 'state.json').read_text()), {'version': 1, 'cedar': 2, 'marble': 7})

    def test_executed_nonremoval_never_decrements(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            (store / 'rules.toml').write_text('[[rules]]\nid="leave"\naccounts=["cedar"]\naction="unsubscribe"\nsubject="Weekly"\n')
            listing = json.loads((ROOT / 'tests/fixtures/inbox.json').read_text())
            listing['messages'][0]['unsubscribe_url'] = 'https://example.net/leave'
            (store / 'inbox.json').write_text(json.dumps(listing))
            token = triage.acquire(store)
            result = self.run_plan(store, token, '--inbox', str(store / 'inbox.json'), '--plan-out', str(store / 'plan.json'))
            self.assertEqual(result.returncode, 0, result.stderr)
            path = store / 'results.json'
            entry = dict(account='cedar', id='m1', action='unsubscribe', status='executed')
            import execute
            from unittest.mock import patch
            machine = execute.Execution(store, token, store / 'plan.json')
            machine.step()
            with patch.object(execute.unsubscribe, 'request', return_value='executed'):
                machine.step(local=True)
            self.assertEqual(self.run_commit(store, token, '--results', str(path)).returncode, 0)
            self.assertEqual(json.loads((store / 'state.json').read_text())['cedar'], 3)
            entry['decrement'] = True
            path.write_text(json.dumps({'results': [entry]}))
            result = self.run_commit(store, token, '--results', str(path))
            self.assertEqual(result.returncode, 1)
            self.assertIn(b'.decrement:', result.stderr)

    def test_results_guards(self):
        for change in ({'id': 'absent'}, {'account': 'marble'}, {'unknown': True},
                       {'action': 'unsubscribe', 'decrement': True}, {'status': 'done'}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                token, _ = self.two_trash(store)
                before = (store / 'state.json').read_bytes()
                entry = dict(account='cedar', id='a', action='trash', status='executed')
                entry.update(change)
                path = store / 'results.json'
                path.write_text(json.dumps({'results': [entry]}))
                result = self.run_commit(store, token, '--results', str(path))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(str(path).encode(), result.stderr)
                self.assertIn(b'expected', result.stderr)
                self.assertNotIn(b'Traceback', result.stderr)
                self.assertEqual((store / 'state.json').read_bytes(), before)

    def test_commit_requires_lock(self):
        for held in (True, False):
            with self.subTest(held=held), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                token, _ = self.two_trash(store)
                if not held:
                    triage.release(store, token)
                before = (store / 'state.json').read_bytes()
                result = self.run_commit(store, 'wrong')
                self.assertEqual(result.returncode, 1)
                self.assertIn(b'run.lock', result.stderr)
                self.assertEqual((store / 'state.json').read_bytes(), before)


    def test_missing_state_first_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            (store / 'state.json').unlink()
            token = triage.acquire(store)
            result = self.run_plan(store, token, '--plan-out', str(store / 'plan.json'))
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads((store / 'plan.json').read_text())
            self.assertTrue(plan['first_run'])
            self.assertTrue(plan['actions'])
            self.assertFalse(plan['reasons'])
            execute.Execution(store, token, store / 'plan.json').step()
            self.assertEqual(self.run_commit(store, token).returncode, 0)
            triage.release(store, token)
            token = triage.acquire(store)
            result = self.run_plan(store, token, '--plan-out', str(store / 'plan.json'))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads((store / 'plan.json').read_text())['first_run'])
            triage.release(store, token)
            (store / 'state.json').unlink()
            for name in ('numbers.json', 'history.log', 'calls.jsonl', 'decisions.jsonl'):
                with self.subTest(name=name):
                    # Check each prior-record sentinel independently.
                    for other in ('numbers.json', 'history.log', 'calls.jsonl', 'decisions.jsonl'):
                        (store / other).unlink(missing_ok=True)
                    (store / name).write_text('{"next":1,"items":[]}' if name == 'numbers.json' else '')
                    token = triage.acquire(store)
                    result = self.run_plan(store, token)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('Missing state', result.stdout.decode())
                    self.assertIn('"actions": []', result.stdout.decode())
                    triage.release(store, token)

    def test_malformed_state_even_first_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            for text in ('{', '[]', 'null', '{"cedar": true}', '{"cedar": -4}', '{"cedar": NaN}', '{"cedar": "3"}'):
                (store / 'state.json').write_text(text)
                token = triage.acquire(store)
                result = self.run_plan(store, token)
                self.assertNotEqual(result.returncode, 0, text)
                self.assertEqual(result.stdout, b'')
                self.assertTrue((store / 'run.lock').exists())
                triage.release(store, token)

    def test_check_cli_and_repeated_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shutil.copyfile(ROOT / "config/config.example.toml", store / "config.toml")
            token = triage.acquire(store)
            def call(command, value=token):
                return subprocess.run([shutil.which('python3'), 'scripts/triage.py', command, '--token', value],
                                      cwd=ROOT, env=dict(os.environ, PIGEON_HOME=tmp),
                                      capture_output=True, text=True)
            self.assertEqual(call('check').returncode, 0)
            self.assertNotEqual(call('check', 'wrong').returncode, 0)
            held = triage.read_lock(store)
            held['created'] -= 2701
            (store / 'run.lock').write_text(json.dumps(held))
            self.assertNotEqual(call('check').returncode, 0)
            self.assertEqual(call('release').returncode, 0)
            result = call('release')
            self.assertEqual(result.returncode, 0)
            self.assertIn('already released', result.stdout)
            result = call('check')
            self.assertEqual(result.returncode, 1)
            self.assertIn(str(store / 'run.lock') + ': expected a lock held by this token, but no lock exists', result.stderr)
            self.assertNotIn('Errno', result.stderr)

    def test_plan_requires_lock(self):
        for state in ('missing', 'wrong', 'expired'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                self.prepare(store)
                token = 'absent'
                if state != 'missing':
                    token = triage.acquire(store)
                    if state == 'wrong':
                        token = 'wrong'
                    else:
                        held = triage.read_lock(store)
                        held['created'] -= 2701
                        (store / 'run.lock').write_text(json.dumps(held))
                result = self.run_plan(store, token)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b'')

    def test_local_refresh_clock_and_wrong_holder(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shutil.copyfile(ROOT / 'config/config.example.toml', store / 'config.toml')
            with patch.object(triage.time, 'time', return_value=10000):
                token = triage.acquire(store)
            with self.assertRaises(ValueError):
                triage.refresh(store, 'wrong', now=datetime.fromtimestamp(12000, timezone.utc))
            triage.refresh(store, token, now=datetime.fromtimestamp(12000, timezone.utc))
            triage.require_lock(store, token, now=14000)
            with self.assertRaises(ValueError):
                triage.refresh(store, token, now=datetime.fromtimestamp(14701, timezone.utc))

    def test_expiry_boundary(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shutil.copyfile(ROOT / "config/config.example.toml", store / "config.toml")
            with patch.object(triage.time, 'time', return_value=10000):
                token = triage.acquire(store)
            with patch.object(triage.time, 'time', return_value=12699):
                triage.require_lock(store, token)
            with patch.object(triage.time, 'time', return_value=12701):
                with self.assertRaises(ValueError):
                    triage.require_lock(store, token)

    def test_acquire_obeys_gate(self):
        # Hold the actual POSIX gate: an ungated acquire completes before release.
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            code = "import sys; sys.path.insert(0, 'scripts'); import triage; from pathlib import Path; print('ready', flush=True); print(triage.acquire(Path(sys.argv[1])), flush=True)"
            with (store / '.lock-gate').open('a') as gate:
                fcntl.flock(gate, fcntl.LOCK_EX)
                child = subprocess.Popen([shutil.which('python3'), '-c', code, tmp], cwd=ROOT,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(), 'ready')
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=0.15)
                finally:
                    fcntl.flock(gate, fcntl.LOCK_UN)
                    child.communicate(timeout=10)

    def test_failed_publication_leaves_no_plan(self):
        for failure in ('report', 'state', 'rename'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                self.prepare(store)
                token = triage.acquire(store)
                plan = store / 'plan.json'
                report = store / ('missing/report.md' if failure == 'report' else 'report.md')
                if failure == 'rename':
                    report.mkdir()
                if failure == 'state':
                    (store / 'state.json').unlink()
                    (store / 'state.json').mkdir()
                result = self.run_plan(store, token, '--plan-out', str(plan), '--report-out', str(report))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(plan.exists())
                self.assertEqual(result.stdout, b'')
                self.assertTrue((store / 'run.lock').exists())

    def test_stdout_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            token = triage.acquire(store)
            result = self.run_plan(store, token, encoding='latin-1')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Still in Inbox (by account)', result.stdout.decode('utf-8'))
            triage.release(store, token)

    def test_invalid_lock_held(self):
        for content in ('{"tok"', json.dumps({'token': 'old', 'created': time.time() + 10000})):
            with tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                (store / 'run.lock').write_text(content)
                with self.assertRaises(ValueError):
                    triage.acquire(store)
                self.assertEqual((store / 'run.lock').read_text(), content)

    def test_state_rename_failure(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            token = triage.acquire(store)
            plan = store / 'plan.json'
            self.assertEqual(self.run_plan(store, token, '--plan-out', str(plan)).returncode, 0)
            execute.Execution(store, token, plan).step()
            before = (store / 'state.json').read_bytes()
            with patch.dict(os.environ, PIGEON_HOME=tmp), patch.object(Path, 'replace', side_effect=OSError('Injected state publication failure')):
                with self.assertRaises(OSError):
                    triage.main(['commit-state', '--token', token, '--plan', str(plan)])
            self.assertEqual((store / 'state.json').read_bytes(), before)
            self.assertTrue(plan.exists())

    def test_unreadable_lock_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            triage.acquire(store)
            (store / 'run.lock').chmod(0)
            with self.assertRaises(ValueError):
                triage.acquire(store)

    def test_final_print_preserves_state(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            token = triage.acquire(store)
            with patch.dict(os.environ, PIGEON_HOME=tmp), patch('builtins.print', side_effect=BrokenPipeError):
                with self.assertRaises(BrokenPipeError):
                    triage.main(['plan', '--token', token, '--inbox', str(ROOT / 'tests/fixtures/inbox.json'),
                                 '--plan-out', str(store / 'plan.json')])
            self.assertTrue((store / 'state.json').is_file())
            self.assertIsInstance(json.loads((store / 'state.json').read_text()), dict)
            self.assertTrue((store / 'run.lock').exists())

    def test_output_paths_cannot_name_inputs_state_or_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            for name in ('config.toml', 'rules.toml', 'never-touch.toml', 'guardrails.md', 'upload-observation.json', 'state.json', 'run.lock', 'calls.jsonl', 'results.json', 'execution-report.md', 'unsubscribe-failures.json', 'unsubscribe-secret', 'execution-probe.json'):
                for flag in ('--plan-out', '--report-out'):
                    token = triage.acquire(store)
                    if not (store / name).exists():
                        (store / name).write_text('preserve')
                    before = (store / name).read_bytes()
                    for used in ('wrong', token):
                        result = self.run_plan(store, used, flag, str(store / name))
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(b'expected a path other than an input', result.stderr)
                        if name != 'run.lock' or used == 'wrong':
                            self.assertEqual((store / name).read_bytes(), before, (name, flag, used))
                    self.assertTrue((store / 'run.lock').exists())
                    triage.release(store, token)

    def test_state_records_only_configured_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            listing = json.loads((ROOT / 'tests/fixtures/inbox.json').read_text())
            listing['accounts']['slate'] = 5
            (store / 'extra.json').write_text(json.dumps(listing))
            (store / 'inbox.json').write_text(json.dumps(listing))
            token = triage.acquire(store)
            result = self.run_plan(store, token, '--inbox', str(store / 'extra.json'), '--plan-out', str(store / 'plan.json'))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads((store / 'plan.json').read_text())['reasons'])
            execute.Execution(store, token, store / 'plan.json').step()
            self.assertEqual(self.run_commit(store, token).returncode, 0)
            self.assertEqual(set(json.loads((store / 'state.json').read_text())), {'version', 'cedar', 'marble'})
            self.assertEqual(self.run_plan(store, token).returncode, 0)

    def test_invalid_regex_cli_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            (store / 'rules.toml').write_text('[[rules]]\nid="bad"\naccounts=["cedar"]\naction="archive"\nsubject="(unclosed"\n')
            token = triage.acquire(store)
            result = self.run_plan(store, token)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr.startswith(b'Refused: '))
            self.assertIn(b"rules.toml: rule 'bad': subject: Invalid subject regex", result.stderr)
            self.assertNotIn(b'Traceback', result.stderr)

    def test_stopped_run_saves_observed_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            (store / 'state.json').write_text('{"cedar": 3, "marble": 3}')
            listing = json.loads((ROOT / 'tests/fixtures/inbox.json').read_text())
            listing['messages'] = []
            inbox = store / 'inbox.json'
            inbox.write_text(json.dumps(listing))
            for stopped in (True, False):
                token = triage.acquire(store)
                result = self.run_plan(store, token, '--inbox', str(inbox), '--plan-out', str(store / 'plan.json'))
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads((store / 'plan.json').read_text())
                self.assertEqual(bool(plan['reasons']), stopped)
                execute.Execution(store, token, store / 'plan.json').step()
                self.assertEqual(self.run_commit(store, token).returncode, 0)
                self.assertEqual(json.loads((store / 'state.json').read_text()), {'version': 1, 'cedar': 0, 'marble': 0})
                triage.release(store, token)

    def test_unlisted_account_keeps_saved_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            self.prepare(store)
            (store / 'state.json').write_text('{"cedar": 3, "marble": 3}')
            listing = json.loads((ROOT / 'tests/fixtures/inbox.json').read_text())
            inbox = store / 'inbox.json'
            omitted = dict(listing, accounts={'marble': 5}, messages=[m for m in listing['messages'] if m['account'] == 'marble'])
            empty = dict(listing, accounts={'cedar': 5, 'marble': 5}, messages=omitted['messages'])
            for data in (omitted, omitted, empty):
                inbox.write_text(json.dumps(data))
                token = triage.acquire(store)
                result = self.run_plan(store, token, '--inbox', str(inbox), '--plan-out', str(store / 'plan.json'))
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads((store / 'plan.json').read_text())
                if data is omitted:
                    self.assertFalse(plan['reasons'])
                    self.assertEqual(plan['not_listed'], ['cedar'])
                    self.assertEqual(json.loads((store / 'state.json').read_text())['cedar'], 3)
                triage.release(store, token)
            # The account missing from two listings is still braked when it next lists zero.
            self.assertIn('Unexpected empty Inbox: cedar', json.loads((store / 'plan.json').read_text())['reasons'])

class PathTypoTests(unittest.TestCase):
    prepare = LockTests.prepare
    run_plan = LockTests.run_plan
    def test_all_plan_path_flags(self):
        for flag in ('inbox', 'config', 'rules', 'never-touch', 'guardrails', 'plan-out', 'report-out'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                self.prepare(store)
                token = triage.acquire(store)
                result = self.run_plan(store, token, '--' + flag, str(store / 'missing' / 'file'))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'Refused:', result.stderr)
                self.assertNotIn(b'Traceback', result.stderr)
                self.assertIn(b'missing', result.stderr)
                self.assertIn(b'expected', result.stderr)

    def test_lock_settings_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            for held in ({'token': 'test', 'created': True},
                         {'token': 'test', 'created': time.time(), 'unknown': 1}):
                (store / 'run.lock').write_text(json.dumps(held))
                with self.assertRaisesRegex(ValueError, 'run.lock.*expected'):
                    triage.read_lock(store)

    def test_refusals_identify_source_and_setting(self):
        cases = [
            ('rules.toml', lambda s: 'rules = false', 'rules: expected an array of tables'),
            ('config.toml', lambda s: s + '\nlangauge="en"\n', 'config.accounts.marble.langauge'),
            ('config.toml', lambda s: s.replace('inbox_limit = 100', 'inbox_limit = false'), 'config.inbox_limit'),
            ('rules.toml', lambda s: s.replace('"cedar", "marble"', '"ceder"'), "rule 'digest': account 'ceder'"),
            ('rules.toml', lambda s: s.replace('subject = "Weekly"', 'subject = 42'), "rule 'digest': subject: expected a non-empty string"),
            ('rules.toml', lambda s: s.replace('has_attachment = true', 'has_attachment = "yes"'), "rule 'receipts': has_attachment"),
            ('rules.toml', lambda s: s.replace('id = "receipts"', 'id = "digest"'), "rule 'digest': Duplicate rule id"),
            ('rules.toml', lambda s: s.replace('exclusions = ["invoice", "question"]', ''), "rule 'digest': Trash rule requires exclusions"),
            ('never-touch.toml', lambda s: s.replace('"cedar", "marble"', '"ceder"'), "entry 1: account 'ceder'"),
            ('never-touch.toml', lambda s: 'allow_empty = "yes"', 'allow_empty: expected boolean'),
            ('state.json', lambda s: '{"cedar": -1}', 'state.json.cedar: expected a non-negative integer count'),
        ]
        for filename, mutate, expected in cases:
            with self.subTest(filename=filename, expected=expected), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                self.prepare(store)
                path = store / filename
                path.write_text(mutate(path.read_text()))
                result = self.run_plan(store, triage.acquire(store))
                text = result.stderr.decode()
                self.assertEqual(result.returncode, 1, text)
                self.assertIn(str(path) + ':', text)
                self.assertIn(expected, text)
                self.assertNotIn('\\_', text)
                for other in {'config.toml', 'rules.toml', 'never-touch.toml', 'state.json'} - {filename}:
                    self.assertNotIn(str(store / other) + ':', text)

    def test_busy_lock_names_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shutil.copyfile(ROOT / "config/config.example.toml", store / "config.toml")
            token = triage.acquire(store)
            expiry = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(triage.read_lock(store)['created'] + triage.STALE_SECONDS + triage.TAKEOVER_MARGIN))
            result = subprocess.run([shutil.which('python3'), 'scripts/triage.py', 'lock'], cwd=ROOT,
                                    env=dict(os.environ, PIGEON_HOME=tmp), capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn(str(store / 'run.lock') + ': Run lock is busy until ' + expiry, result.stderr)
            triage.release(store, token)

    def test_default_private_store_refusal_names_actual_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp)
            env.pop('PIGEON_HOME', None)
            result = subprocess.run([shutil.which('python3'), 'scripts/triage.py', 'lock'], cwd=ROOT,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn(str(Path(tmp) / '.config/pigeon'), result.stderr)
            self.assertIn('Create it, or set PIGEON_HOME to an existing folder.', result.stderr)
            self.assertNotIn('\\_', result.stderr)

class GitEnvironmentTests(unittest.TestCase):
    def test_git_environment_cannot_hide_worktree(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            store = root / 'store'
            store.mkdir()
            for variables in ({'GIT_DIR': str(root / 'absent')}, {'GIT_WORK_TREE': str(root / 'elsewhere')}, {'GIT_CEILING_DIRECTORIES': str(root)}, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.worktree', 'GIT_CONFIG_VALUE_0': str(root / 'elsewhere')}):
                with self.subTest(variables=variables), patch.dict(os.environ, variables):
                    with self.assertRaisesRegex(ValueError, 'git-ignored'):
                        triage.private_store(store)


class QuietTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        self.config = dict(accounts={'cedar': {'quiet_kind': 'history'}, 'marble': {'quiet_kind': 'ids'}},
                           timezone='UTC', schedule=dict(weekdays=['08:00', '12:00'], weekends=['09:00'], always_full=['12:00']))
        self.previous = {'cedar': 3, 'marble': 1, '_positions': {'cedar': '100', 'marble': ['a', 'b']}}
        self.data = {'cedar': dict(outcome='ok', position='101', arrivals=[]), 'marble': dict(outcome='ok', ids=['a', 'b'])}
        self.obs = self.store / 'observation.json'
        self.now = datetime.fromisoformat('2026-11-11T08:10:00+00:00')

    def decide(self, scheduled=True):
        self.obs.write_text(json.dumps(self.data))
        return triage.quiet_check(self.store, self.config, self.previous, self.obs, scheduled, self.now)['decision']

    def test_gateway_ids_and_loud_invalid_fields(self):
        import contextlib, io, copy
        ident = 'AAMk+abc/def=='
        self.previous['_positions']['marble'] = [ident]
        self.data['marble']['ids'] = [ident]
        self.assertEqual(self.decide(), 'skip')
        self.data['cedar']['arrivals'] = [ident]
        self.assertEqual(self.decide(), 'full')
        self.data['cedar']['arrivals'] = []
        original = copy.deepcopy(self.data)
        for account, field, value in [('marble', 'ids', [x]) for x in
                ('bad id', 'bad\x01id', 'é', 'x' * 513)] + [
                ('cedar', 'arrivals', ['bad id']), ('cedar', 'position', 'bad'),
                ('cedar', 'outcome', 'expired')]:
            self.data = copy.deepcopy(original)
            self.data[account][field] = value
            (self.store / 'state.json').write_text(json.dumps(self.previous))
            before = (self.store / 'state.json').read_bytes()
            out = io.StringIO()
            with contextlib.redirect_stderr(out):
                self.assertEqual(self.decide(), 'full')
            self.assertIn(account, out.getvalue())
            self.assertIn(field, out.getvalue())
            self.assertEqual(before, (self.store / 'state.json').read_bytes())
        for raw, field in [(None, 'observation'), ('{', 'observation'),
                           (' ' * (triage.OBSERVATION_LIMIT + 1), 'observation'),
                           ('{}', 'accounts')]:
            self.obs.unlink(missing_ok=True)
            if raw is not None: self.obs.write_text(raw)
            out = io.StringIO()
            with contextlib.redirect_stderr(out):
                self.assertIsNone(triage.read_observation(self.obs, self.config, {}))
            self.assertIn('cedar', out.getvalue())
            self.assertIn(field, out.getvalue())
        self.obs.write_text(json.dumps(original))
        self.obs.chmod(0)
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertIsNone(triage.read_observation(self.obs, self.config, {}))
        self.assertIn('cedar', out.getvalue())
        self.assertIn('observation', out.getvalue())
        self.obs.chmod(0o600)

    def test_skip_history_and_ids_unchanged(self):
        self.assertEqual(self.decide(), 'skip')

    def test_skip_preserves_artifacts_and_counts(self):
        files = ['state.json', 'plan.json', 'report.md']
        for name in files:
            (self.store / name).write_bytes(b'original\x00bytes')
        self.assertEqual(self.decide(), 'skip')
        for name in files:
            self.assertEqual((self.store / name).read_bytes(), b'original\x00bytes')
        self.assertEqual((self.store / 'history.log').read_text().splitlines(), ['2026-11-11T08:10:00+00:00 skipped: no new mail'])
        self.assertFalse((self.store / 'run-message.md').exists())

    def test_no_schedule_full(self):
        del self.config['schedule']
        self.assertEqual(self.decide(), 'full')

    def test_always_full(self):
        self.config['schedule']['always_full'] = ['08:00']
        self.assertEqual(self.decide(), 'full')

    def test_on_demand_full(self):
        self.assertEqual(self.decide(False), 'full')

    def test_undelivered_full(self):
        (self.store / 'run-message.md').write_text('')
        self.assertEqual(self.decide(), 'full')

    def test_no_positions_full(self):
        del self.previous['_positions']
        self.assertEqual(self.decide(), 'full')

    def test_one_missing_position_full(self):
        del self.previous['_positions']['marble']
        self.assertEqual(self.decide(), 'full')

    def test_history_arrival_full(self):
        self.data['cedar']['arrivals'] = ['new']
        self.assertEqual(self.decide(), 'full')

    def test_ids_changed_full(self):
        self.data['marble']['ids'] = ['c', 'a']
        self.assertEqual(self.decide(), 'full')

    def test_ids_reordered_full(self):
        self.data['marble']['ids'].reverse()
        self.assertEqual(self.decide(), 'full')

    def test_missing_account_full(self):
        del self.data['cedar']
        self.assertEqual(self.decide(), 'full')

    def test_extra_account_full(self):
        self.data['extra'] = self.data['cedar']
        self.assertEqual(self.decide(), 'full')

    def test_failed_read_full(self):
        self.data['cedar']['outcome'] = 'error'
        self.assertEqual(self.decide(), 'full')

    def test_expired_full(self):
        self.data['cedar']['outcome'] = 'expired'
        self.assertEqual(self.decide(), 'full')

    def test_position_plain_digits(self):
        for value in ['+101', '１２３', '10\n', '', '1'*129, 101, None, [], {}]:
            with self.subTest(value=value):
                self.data['cedar']['position'] = value
                self.assertEqual(self.decide(), 'full')

    def test_decreasing_position_full(self):
        self.data['cedar']['position'] = '99'
        self.assertEqual(self.decide(), 'full')

    def test_missing_current_position_full(self):
        del self.data['cedar']['position']
        self.assertEqual(self.decide(), 'full')

    def test_missing_observation_full(self):
        self.assertEqual(triage.quiet_check(self.store, self.config, self.previous, self.obs, True, self.now)['decision'], 'full')

    def test_unreadable_observation_full(self):
        self.obs.write_text(json.dumps(self.data))
        self.obs.chmod(0)
        self.assertEqual(triage.quiet_check(self.store, self.config, self.previous, self.obs, True, self.now)['decision'], 'full')

    def test_long_observation_full(self):
        self.obs.write_text(' '*65537)
        self.assertEqual(triage.quiet_check(self.store, self.config, self.previous, self.obs, True, self.now)['decision'], 'full')

    def test_malformed_observation_full(self):
        for text in ['{', '[]', 'null', '"text"', '\udcff']:
            self.obs.write_bytes(text.encode('utf-8', errors='surrogatepass'))
            self.assertEqual(triage.quiet_check(self.store, self.config, self.previous, self.obs, True, self.now)['decision'], 'full')

    def test_observation_field_guards(self):
        import copy
        for account, key, values in [('cedar', 'arrivals', [None, 'x', [1], ['bad\n'], ['a']*1001]),
                                     ('marble', 'ids', [None, 'x', [1], ['a']*11, ['a','a'], ['bad!'], ['x'*513]])]:
            original = copy.deepcopy(self.data)
            for value in values:
                with self.subTest(key=key, value=str(value)[:40]):
                    self.data = copy.deepcopy(original)
                    self.data[account][key] = value
                    self.assertEqual(self.decide(), 'full')
            self.data = original
        self.data['cedar']['extra'] = 'untrusted'
        self.assertEqual(self.decide(), 'full')
        self.data['cedar'] = []
        self.assertEqual(self.decide(), 'full')

    def test_decision_deterministic_without_process_or_network(self):
        from unittest.mock import patch
        self.obs.write_text(json.dumps(self.data))
        triage.acquire(self.store)
        with patch.object(triage.subprocess, 'run', side_effect=AssertionError('subprocess')), patch('socket.socket', side_effect=AssertionError('network')):
            first = triage.quiet_check(self.store, self.config, self.previous, self.obs, True, self.now)
            second = triage.quiet_check(self.store, self.config, self.previous, self.obs, True, self.now)
        self.assertEqual(json.dumps(first), json.dumps(second))

    def test_repeat_writes_one_history_line(self):
        triage.acquire(self.store)
        self.assertEqual(self.decide(), 'skip')
        self.data['cedar']['arrivals'] = ['new']
        self.assertEqual(self.decide(), 'skip')
        self.assertEqual(len((self.store / 'history.log').read_text().splitlines()), 1)

    def test_slot_transitions_absolute_time(self):
        for zone, local, stamp, delay in [
                ('Europe/Berlin', '02:30', '2026-10-25T00:31:00+00:00', 1),
                ('Europe/Berlin', '02:30', '2026-10-25T01:31:00+00:00', 1),
                ('Europe/Berlin', '01:30', '2026-03-29T01:31:00+00:00', 61),
                ('UTC', '01:30', '2026-11-01T01:31:00+00:00', 1)]:
            with self.subTest(zone=zone, stamp=stamp):
                config = dict(timezone=zone, schedule=dict(weekdays=[local], weekends=[local]))
                now = datetime.fromisoformat(stamp)
                slot = triage.schedule_slot(config, now)
                self.assertEqual((now.timestamp() - slot.timestamp()) / 60, delay)

    def test_drive_age_does_not_choose_slot(self):
        from unittest.mock import patch
        import contextlib, io
        self.config['schedule']['always_full'] = ['08:00']
        (self.store / 'config.toml').write_text('[schedule]\nweekdays=["07:00","08:00"]\nweekends=["07:00","08:00"]\nalways_full=["08:00"]\n[accounts.cedar]\nquiet_kind="history"\n[accounts.marble]\nquiet_kind="ids"\n')
        (self.store / 'state.json').write_text(json.dumps(self.previous))
        self.obs.write_text(json.dumps(self.data))
        from mock_provider import MockDrive
        from datetime import timedelta
        with (self.store / 'config.toml').open('a') as out:
            out.write('[lock]\nkind="onedrive"\naccount="drive-example"\nfolder="locks-example"\n')
        drive = MockDrive((self.now - timedelta(minutes=40)).isoformat())
        call = triage.drive_lock(self.store, 'acquire', now=self.now)['call']
        lock_obs = self.store / 'lock-observation.json'
        lock_obs.write_text(json.dumps(drive.perform(call)))
        call = triage.drive_lock(self.store, 'acquire', observation=lock_obs, now=self.now)['call']
        lock_obs.write_text(json.dumps(drive.perform(call)))
        token = triage.drive_lock(self.store, 'acquire', observation=lock_obs, now=self.now)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat('2026-11-11T08:10:00+00:00')
        output = io.StringIO()
        with patch.object(triage, 'datetime', Clock), patch.object(triage.time, 'time', return_value=self.now.timestamp()), patch.dict(os.environ, PIGEON_HOME=str(self.store)), contextlib.redirect_stdout(output):
            triage.main(['quiet-check', '--token', token, '--scheduled', '--observation', str(self.obs)])
        self.assertEqual(json.loads(output.getvalue())['reason'], 'always-full slot')
        for name, text in [('rules.toml', 'rules=[]\n'), ('never-touch.toml', 'allow_empty=true\n'), ('guardrails.md', 'fixture policy\n'), ('inbox.json', '{"accounts":{"cedar":100,"marble":100},"messages":[]}')]:
            (self.store / name).write_text(text)
        output = io.StringIO()
        output.reconfigure = lambda **kwargs: None
        with patch.object(triage, 'datetime', Clock), patch.object(triage.time, 'time', return_value=self.now.timestamp()), patch.dict(os.environ, PIGEON_HOME=str(self.store)), contextlib.redirect_stdout(output):
            triage.main(['plan', '--token', token, '--scheduled', '--inbox', str(self.store / 'inbox.json'), '--report-out', str(self.store / 'report.md')])
        self.assertFalse((self.store / 'report.md').read_text().startswith('Late start:'))


    def test_nonexistent_slot_is_not_an_occurrence(self):
        config = dict(timezone='Europe/Berlin', schedule=dict(weekdays=['02:30'], weekends=['02:30']))
        now = datetime.fromisoformat('2026-03-29T01:31:00+00:00')
        slot = triage.schedule_slot(config, now)
        self.assertEqual(slot.date().isoformat(), '2026-03-28')

    def test_schedule_validation(self):
        import copy
        for key, values in [('weekdays', [None, '08:00', [], ['8:00'], ['24:00'], ['08:60'], ['08:00','08:00'], [1]]),
                            ('weekends', [[]]), ('always_full', [None, ['10:00']]), ('extra', [True])]:
            original = copy.deepcopy(self.config)
            for value in values:
                with self.subTest(key=key, value=value):
                    config = copy.deepcopy(original)
                    config['schedule'][key] = value
                    with self.assertRaisesRegex(ValueError, 'config.schedule.' + key):
                        triage.validate_inputs(dict(accounts={}, messages=[]), config, {})
        for value in [[], True, 'bad']:
            with self.assertRaisesRegex(ValueError, 'config.schedule'):
                triage.validate_inputs(dict(accounts={}, messages=[]), dict(accounts={'cedar': {}}, schedule=value), {})

    def test_weekday_weekend_previous_day_local(self):
        from datetime import datetime
        self.config['timezone'] = 'Europe/Berlin'
        for utc, expected in [('2026-11-14T07:30:00+00:00', '2026-11-13 12:00'),
                              ('2026-11-14T08:00:00+00:00', '2026-11-14 09:00'),
                              ('2026-11-15T23:30:00+00:00', '2026-11-15 09:00'),
                              ('2026-11-16T07:00:00+00:00', '2026-11-16 08:00')]:
            self.assertEqual(triage.schedule_slot(self.config, datetime.fromisoformat(utc)).strftime('%Y-%m-%d %H:%M'), expected)

    def test_legacy_state_and_no_schedule_plan_unchanged(self):
        config = dict(accounts={'cedar': {}})
        listing = dict(accounts={'cedar': 10}, messages=[])
        before = triage.build_plan(listing, [], [], config, {'cedar': 0})
        self.assertEqual(before, triage.build_plan(listing, [], [], config, {'cedar': 0, '_positions': {'cedar': []}}))
        triage.validate_inputs(listing, config, {'cedar': 0})

    def test_config_kind_and_reserved_state(self):
        for fields in [{'quiet_kind': 'typo'}, {'quiet_kind': 1}]:
            with self.assertRaisesRegex(ValueError, 'quiet_kind'):
                triage.validate_inputs(dict(accounts={}, messages=[]), dict(accounts={'cedar': fields}), {})
        with self.assertRaisesRegex(ValueError, '_positions'):
            triage.validate_inputs(dict(accounts={}, messages=[]), dict(accounts={'_positions': {}}), {})

    def test_late_report_boundary_and_on_demand(self):
        from datetime import datetime
        from unittest.mock import patch
        import contextlib
        import io
        LockTests().prepare(self.store)
        config = (self.store / 'config.toml').read_text()
        (self.store / 'config.toml').write_text(config + '\n[schedule]\nweekdays=["08:00"]\nweekends=["09:00"]\nalways_full=[]\n')
        (self.store / 'state.json').write_text(json.dumps({'cedar': 0, 'marble': 0, '_positions': {'cedar': '100', 'marble': []}}))
        token = triage.acquire(self.store)
        for clock, scheduled, late in [('2026-11-11T08:16:00+00:00', True, True),
                                       ('2026-11-11T08:15:00+00:00', True, False),
                                       ('2026-11-11T08:16:00+00:00', False, False)]:
            output = io.StringIO()
            output.reconfigure = lambda **kwargs: None
            with patch.dict(os.environ, PIGEON_HOME=str(self.store)), contextlib.redirect_stdout(output):
                triage.main(['plan', '--token', token, '--inbox', str(self.store / 'inbox.json'), '--report-out', str(self.store / 'report.md')] + (['--scheduled'] if scheduled else []), now=datetime.fromisoformat(clock))
            first = (self.store / 'report.md').read_text().splitlines()[0]
            self.assertEqual(first.startswith('Late start:'), late)
            if late:
                self.assertIn('08:00 UTC', first)
                self.assertIn('16 minutes', first)
        triage.release(self.store, token)

    def test_quiet_cli_requires_lock_and_emits_reads(self):
        from unittest.mock import patch
        import contextlib
        import io
        (self.store / 'config.toml').write_text('[accounts.cedar]\nquiet_kind="history"\n[accounts.marble]\nquiet_kind="ids"\n')
        execute_atomic = lambda name, data: (self.store / name).write_text(json.dumps(data))
        execute_atomic('state.json', self.previous)
        token = triage.acquire(self.store)
        with patch.dict(os.environ, PIGEON_HOME=str(self.store)):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                triage.main(['quiet-check', '--token', token])
            reads = json.loads(output.getvalue())['reads']
            self.assertEqual(reads['cedar']['since'], '100')
            self.assertEqual(reads['marble']['limit'], 10)
            with self.assertRaises(ValueError):
                triage.main(['quiet-check', '--token', 'wrong'])
        triage.release(self.store, token)

    def test_valid_oversized_json_full(self):
        self.data['cedar']['position'] = '101'
        raw = json.dumps(self.data)
        self.obs.write_text(raw + ' ' * (65537 - len(raw)))
        self.assertEqual(triage.quiet_check(self.store, self.config, self.previous, self.obs, True, self.now)['decision'], 'full')

    def test_saved_positions_validation(self):
        for saved in [[], {'extra': []}, {'cedar': 'abc'}, {'marble': ['bad!']}]:
            with self.subTest(saved=saved), self.assertRaises(ValueError):
                triage.validate_inputs(dict(accounts={}, messages=[]), self.config, {'_positions': saved})

    def test_schedule_refusal_names_file_and_key_on_one_line(self):
        LockTests().prepare(self.store)
        with (self.store / 'config.toml').open('a') as out:
            out.write('\n[schedule]\nweekdays=["25:00"]\nweekends=["09:00"]\nalways_full=[]\n')
        token = triage.acquire(self.store)
        result = LockTests().run_plan(self.store, token)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.decode().splitlines()), 1)
        self.assertIn(str(self.store / 'config.toml'), result.stderr.decode())
        self.assertIn('config.schedule.weekdays', result.stderr.decode())

    def test_new_store_files_protected_from_plan_outputs(self):
        for name in ['history.log', 'run-message.md', 'quiet-observation.json', 'positions-observation.json', 'quiet-check.json']:
            with self.subTest(name=name):
                LockTests().prepare(self.store)
                target = self.store / name
                target.write_bytes(b'preserve')
                token = triage.acquire(self.store)
                result = LockTests().run_plan(self.store, token, '--report-out', str(target))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(target.read_bytes(), b'preserve')

                triage.release(self.store, token)

    def test_missing_history_position_full(self):
        del self.previous['_positions']['cedar']
        self.assertEqual(self.decide(), 'full')

    def test_deeply_nested_observation_full(self):
        self.obs.write_text('[' * 2000 + ']' * 2000)
        self.assertEqual(triage.quiet_check(self.store, self.config, self.previous, self.obs, True, self.now)['decision'], 'full')

    def test_nonregular_observation_not_opened(self):
        from unittest.mock import patch
        os.mkfifo(self.obs)
        with patch.object(Path, 'open', side_effect=AssertionError('must not open a pipe')):
            self.assertEqual(triage.quiet_check(self.store, self.config, self.previous, self.obs, True, self.now)['decision'], 'full')


class DriveLockTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        from mock_provider import MockDrive
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        LockTests().prepare(self.store)
        self.setting = '\n[lock]\nkind="onedrive"\naccount="drive-example"\nfolder="locks-example"\n'
        with (self.store / 'config.toml').open('a') as out:
            out.write(self.setting)
        self.now = datetime.fromisoformat('2026-11-11T08:00:00+00:00')
        self.drive = MockDrive()
        from unittest.mock import patch
        clock = patch.object(triage.time, 'time', return_value=self.now.timestamp())
        clock.start()
        self.addCleanup(clock.stop)

    def start(self, operation='acquire', token=None, store=None):
        return triage.drive_lock(store or self.store, operation, token, now=self.now)

    def observe(self, data, operation='acquire', token=None, store=None):
        store = store or self.store
        path = store / 'lock-observation.json'
        path.write_text(json.dumps(data))
        return triage.drive_lock(store, operation, token, path, now=self.now)

    def acquire(self):
        read = self.observe(self.drive.perform(self.start()['call']))
        self.assertEqual(read['call']['operation'], 'read')
        return self.observe(self.drive.perform(read['call']))

    def check(self, token):
        triage.require_lock(self.store, token, now=self.now.timestamp())

    def refused_confirmation(self, **changes):
        created = self.drive.perform(self.start()['call'])
        read = self.observe(created)
        self.assertEqual(read['call']['operation'], 'read')
        self.assertFalse((self.store / 'run.lock').exists())
        data = self.drive.perform(read['call']) | changes
        with self.assertRaisesRegex(ValueError, 'no delete issued.*45 minutes'):
            self.observe(data)
        self.assertFalse((self.store / 'run.lock').exists())
        self.assertFalse(any(c['operation'] == 'delete' for c in self.drive.calls))
        with self.assertRaises(ValueError): self.start()
        with self.assertRaises(ValueError): self.observe(data)

    def test_drive_dry_commit_issues_no_read(self):
        from unittest.mock import patch
        token = self.acquire()
        path = self.store / 'plan.json'
        (self.store/'rules.toml').write_text('rules=[]\n')
        (self.store/'never-touch.toml').write_text('allow_empty=true\n')
        save_plan(self.store, token, path, dict(dry_run=True))
        with patch.object(triage, 'private_store', return_value=self.store), patch.object(triage, 'drive_lock', side_effect=AssertionError('drive call')):
            with self.assertRaisesRegex(ValueError, 'dry-run'):
                triage.main(['commit-state', '--token', token, '--plan', str(path)])
            with self.assertRaisesRegex(ValueError, 'dry-run'):
                triage.finish_run(self.store, token, path, now=self.now)

    def test_batch_acquisition_interleaved_exclusion(self):
        other = self.store / 'other'
        other.mkdir()
        shutil.copyfile(self.store / 'config.toml', other / 'config.toml')
        calls_a = triage.batch_lock(self.store, now=self.now)['calls']
        calls_b = triage.batch_lock(other, now=self.now)['calls']
        a = self.drive.perform(calls_a[0])
        b = self.drive.perform(calls_b[0])
        read_a = self.drive.perform(calls_a[1])
        read_b = self.drive.perform(calls_b[1])
        token = triage.batch_lock(self.store, dict(create=a, read=read_a), now=self.now)
        self.check(token)
        with self.assertRaisesRegex(ValueError, 'busy'):
            triage.batch_lock(other, dict(create=b, read=read_b), now=self.now)
        self.assertFalse((other / 'run.lock').exists())

    def test_finish_batches_uploads_then_conditional_release(self):
        token = self.acquire()
        (self.store / 'rules.toml').write_text('rules=[]\n')
        (self.store / 'never-touch.toml').write_text('allow_empty=true\n')
        listing = execute.read(self.store / 'inbox.json')
        plan = triage.build_plan(listing, [], [], triage.state_config(self.store), {})
        save_plan(self.store, token, self.store / 'plan.json', plan)
        execute.Execution(self.store, token, self.store / 'plan.json').step()
        first = triage.finish_run(self.store, token, self.store / 'plan.json', now=self.now)
        self.assertEqual(first['call']['operation'], 'read')
        second = triage.finish_run(self.store, token, self.store / 'plan.json', self.drive.perform(first['call']), now=self.now)
        self.assertEqual(second['calls'][-1]['operation'], 'delete')
        result = triage.finish_run(self.store, token, self.store / 'plan.json', dict(uploads={}, release=self.drive.perform(second['calls'][-1])), now=self.now)
        self.assertTrue(result['done'])
        self.assertIn('Triage', result['message'])
        self.assertFalse((self.store / 'run.lock').exists())

    def test_refresh_keeps_token_and_replaces_lease(self):
        from datetime import timedelta
        token = self.acquire()
        old = triage.read_lock(self.store)
        self.now += timedelta(minutes=40)
        self.drive.created = self.now.isoformat()
        result = self.start('refresh', token)
        with self.assertRaises(ValueError):
            self.check(token)
        for operation in ('delete', 'create', 'read'):
            self.assertEqual(result['call']['operation'], operation)
            result = self.observe(self.drive.perform(result['call']), 'refresh', token)
        self.assertEqual(result, token)
        self.assertGreater(triage.read_lock(self.store, now=self.now.timestamp())['created'], old['created'])
        self.assertEqual(triage.read_lock(self.store, now=self.now.timestamp())['started'], old['created'])
        self.check(token)

    def test_refresh_lost_condition_stops(self):
        token = self.acquire()
        result = self.start('refresh', token)
        self.drive.item['etag'] = 'other'
        with self.assertRaises(ValueError):
            self.observe(self.drive.perform(result['call']), 'refresh', token)
        with self.assertRaises(ValueError):
            self.check(token)

    def test_confirmation_deterministic_without_process_or_network(self):
        from unittest.mock import patch
        created = self.drive.perform(self.start()['call'])
        self.observe(created)
        path = self.store / 'lock-exchange.json'
        pending = path.read_bytes()
        observed = dict(status='ok', **self.drive.item)
        with patch.object(triage.subprocess, 'run', side_effect=AssertionError('process')), patch('socket.socket', side_effect=AssertionError('network')):
            first = self.observe(observed)
            held = (self.store / 'run.lock').read_bytes()
            (self.store / 'run.lock').unlink()
            path.write_bytes(pending)
            second = self.observe(observed)
        self.assertEqual(first, second)
        self.assertEqual((self.store / 'run.lock').read_bytes(), held)

    def test_drive_acquisition_trims_logs(self):
        for name in triage.LOG_NAMES:
            (self.store / name).write_text('old-line\n' * 40000 + 'newest\n')
        self.acquire()
        for name in triage.LOG_NAMES:
            raw = (self.store / name).read_bytes()
            self.assertLessEqual(len(raw), 262144)
            self.assertTrue(raw.endswith(b'newest\n'))

    def test_recover_abandoned_exchange(self):
        from datetime import timedelta
        self.start()
        with self.assertRaises(ValueError): self.observe({'status': 'error'})
        with self.assertRaises(ValueError): self.start()
        self.now += timedelta(hours=2)
        self.drive.created = self.now.isoformat()
        call = self.start()['call']
        self.assertEqual(call['operation'], 'create')
        self.assertEqual(call['conflict'], 'fail')
        with self.assertRaises(ValueError): self.start()
        read = self.observe(self.drive.perform(call))
        token = self.observe(self.drive.perform(read['call']))
        self.check(token)
        self.assertEqual((self.store / 'history.log').read_text().count('abandoned'), 1)

    def test_recovery_after_each_uncertain_phase(self):
        from datetime import timedelta
        for phase in ('create', 'confirm', 'read', 'verify', 'release'):
            with self.subTest(phase=phase):
                (self.store / 'run.lock').unlink(missing_ok=True)
                (self.store / 'lock-exchange.json').unlink(missing_ok=True)
                self.drive.item = None
                self.drive.created = self.now.isoformat()
                token = None
                operation = 'acquire'
                if phase in ('verify', 'release'):
                    token = self.acquire()
                    operation = 'commit' if phase == 'verify' else 'release'
                    self.start(operation, token)
                else:
                    call = self.start()['call']
                    if phase == 'confirm': self.observe(self.drive.perform(call))
                    if phase == 'read': self.observe({'status': 'conflict'})
                with self.assertRaises(ValueError): self.observe({'status': 'unknown'}, operation, token)
                if token:
                    self.assertTrue(triage.read_lock(self.store, now=self.now.timestamp())['lost'])
                with self.assertRaises(ValueError): self.start()
                self.now += timedelta(hours=2)
                pending = (self.store / 'lock-exchange.json').read_bytes()
                with self.assertRaises(ValueError): self.start('release', token or 'wrong')
                self.assertEqual((self.store / 'lock-exchange.json').read_bytes(), pending)
                fresh = self.start()['call']
                self.assertEqual(fresh['operation'], 'create')
                self.assertNotIn('if_match', fresh)

    def test_taking_clock_six_minutes_fast_cannot_remove_valid_holder(self):
        from datetime import timedelta
        token = self.acquire()
        other = self.store / 'fast'
        other.mkdir()
        shutil.copyfile(self.store / 'config.toml', other / 'config.toml')
        owner_clock = self.now + timedelta(minutes=40)
        self.now = owner_clock + timedelta(minutes=6)
        call = self.start(store=other)['call']
        read = self.observe(self.drive.perform(call), store=other)
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.observe(self.drive.perform(read['call']), store=other)
        triage.require_lock(self.store, token, now=owner_clock.timestamp())
        self.assertFalse(any(c['operation'] == 'delete' for c in self.drive.calls))
        # A later attempt starts a fresh conflict-fail create; the drive decides again.
        self.assertEqual(self.start(store=other)['call'], dict(account='drive-example', folder='locks-example',
                                                               name=triage.LOCK_NAME, operation='create', conflict='fail'))

    def test_busy_does_not_block_store_after_holder_releases(self):
        token = self.acquire()
        other = self.store / 'later'
        other.mkdir()
        shutil.copyfile(self.store / 'config.toml', other / 'config.toml')
        read = self.observe(self.drive.perform(self.start(store=other)['call']), store=other)
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.observe(self.drive.perform(read['call']), store=other)
        self.assertFalse((other / 'lock-exchange.json').exists())
        release = triage.drive_lock(self.store, 'release', token, now=self.now)['call']
        self.assertEqual(self.observe(self.drive.perform(release), 'release', token), 'released')
        read = self.observe(self.drive.perform(self.start(store=other)['call']), store=other)
        self.assertIsInstance(self.observe(self.drive.perform(read['call']), store=other), str)
        self.assertFalse((other / 'history.log').exists())

    def test_takeover_margin(self):
        from datetime import timedelta
        for minutes in (45, 46, 54, 55, 56):
            with self.subTest(minutes=minutes):
                (self.store / 'lock-exchange.json').unlink(missing_ok=True)
                self.start()
                self.observe({'status': 'conflict'})
                item = dict(status='ok', id='old', etag='v1', name=triage.LOCK_NAME,
                            created=(self.now - timedelta(minutes=minutes)).isoformat())
                if minutes <= 55:
                    with self.assertRaisesRegex(ValueError, 'busy'): self.observe(item)
                else:
                    self.assertEqual(self.observe(item)['call']['operation'], 'delete')

    def test_confirmation_failed(self): self.refused_confirmation(status='error')
    def test_confirmation_uncertain(self): self.refused_confirmation(status='unknown')
    def test_confirmation_not_found(self): self.refused_confirmation(status='not-found')
    def test_confirmation_different_id(self): self.refused_confirmation(id='different')
    def test_confirmation_different_time(self): self.refused_confirmation(created='2026-11-11T07:59:59+00:00')
    def test_confirmation_different_name(self): self.refused_confirmation(name='different')
    def test_confirmation_expired(self):
        from datetime import timedelta
        created = self.drive.perform(self.start()['call'])
        read = self.observe(created)
        self.now += timedelta(seconds=2701)
        with self.assertRaises(ValueError):
            self.observe(self.drive.perform(read['call']))
        self.assertFalse((self.store / 'run.lock').exists())
        self.assertFalse(any(c['operation'] == 'delete' for c in self.drive.calls))

    def test_drive_release_cleanup_failure_unlocks(self):
        from unittest.mock import patch
        for regular in (True, False):
            with self.subTest(regular=regular):
                created = self.drive.perform(self.start()['call'])
                read = self.observe(created)
                token = self.observe(self.drive.perform(read['call']))
                folder = self.store / triage.SIMULATION_DIR
                if regular:
                    folder.write_text('fixture')
                else:
                    folder.mkdir()
                    (folder / 'blocked').mkdir()
                    (folder / 'blocked' / 'fixture').write_text('fixture')
                    (folder / 'blocked').chmod(0o500)
                delete = self.start('release', token)['call']
                with patch('shutil.rmtree', side_effect=PermissionError('private detail')) if not regular else patch('shutil.rmtree', wraps=shutil.rmtree):
                    result = self.observe(self.drive.perform(delete), 'release', token)
                self.assertFalse((self.store / 'run.lock').exists())
                self.assertFalse((self.store / 'lock-exchange.json').exists())
                self.assertEqual(result, 'released' if regular else 'released; dry-run cleanup failed')
                if not regular:
                    self.assertTrue((folder / 'blocked' / 'fixture').exists())
                    (folder / 'blocked').chmod(0o700)
                triage.clean_simulation(self.store)
                self.assertFalse(folder.exists())

    def test_create_tag_never_delete_condition(self):
        created = self.drive.perform(self.start()['call'])
        read = self.observe(created)
        self.assertNotIn(created['etag'], (self.store / 'lock-exchange.json').read_text())
        token = self.observe(self.drive.perform(read['call']))
        held = triage.read_lock(self.store)
        self.assertNotEqual(held['etag'], created['etag'])
        self.assertEqual(self.drive.perform(dict(read['call'], operation='delete', if_match=created['etag'])),
                         {'status': 'precondition-failed'})
        delete = self.start('release', token)['call']
        self.assertNotEqual(delete['if_match'], created['etag'])
        self.assertEqual(self.observe(self.drive.perform(delete), 'release', token), 'released')
        self.assertEqual(delete['if_match'], held['etag'])

    def test_normal_full_run_and_release(self):
        import execute
        from unittest.mock import patch
        token = self.acquire()
        self.check(token)
        with patch.object(triage.time, 'time', return_value=self.now.timestamp()):
            import contextlib
            import io
            output = io.StringIO()
            output.reconfigure = lambda **kwargs: None
            with patch.dict(os.environ, PIGEON_HOME=str(self.store)), contextlib.redirect_stdout(output):
                triage.main(['plan', '--token', token, '--inbox', str(self.store / 'inbox.json'), '--plan-out', str(self.store / 'plan.json')])
            machine = execute.Execution(self.store, token, self.store / 'plan.json')
            from mock_provider import MockProvider
            while not machine.step().get('done'):
                machine.step(MockProvider([]))
            read = self.start('commit', token)
            self.assertEqual(read['call']['operation'], 'read')
            self.assertEqual(self.observe(self.drive.perform(read['call']), 'commit', token), 'verified')
            triage.commit_state(self.store, token, self.store / 'plan.json', self.store / 'results.json', lock_verified=True)
        call = self.start('release', token)['call']
        self.assertEqual(call['if_match'], self.drive.item['etag'])
        self.assertEqual(self.observe(self.drive.perform(call), 'release', token), 'released')
        self.assertIsNone(self.drive.item)

    def test_stale_takeover(self):
        self.drive.perform(self.start()['call'])
        (self.store / 'lock-exchange.json').unlink()
        self.drive.item['created'] = '2026-11-11T07:00:00+00:00'
        read = self.observe(self.drive.perform(self.start()['call']))
        delete = self.observe(self.drive.perform(read['call']))
        self.assertEqual(delete['call']['if_match'], self.drive.item['etag'])
        create = self.observe(self.drive.perform(delete['call']))
        read = self.observe(self.drive.perform(create['call']))
        token = self.observe(self.drive.perform(read['call']))
        self.check(token)

    def test_two_instances_stale_race_exactly_one_holder(self):
        from mock_provider import MockDrive
        self.drive = MockDrive('2026-11-11T07:00:00+00:00')
        self.drive.perform(self.start()['call'])
        (self.store / 'lock-exchange.json').unlink()
        other = self.store / 'second'
        other.mkdir()
        (other / 'config.toml').write_text(self.setting)
        reads = [self.observe(self.drive.perform(self.start(store=s)['call']), store=s) for s in (self.store, other)]
        deletes = [self.observe(self.drive.perform(r['call']), store=s) for s, r in zip((self.store, other), reads)]
        create = self.observe(self.drive.perform(deletes[0]['call']))
        self.drive.created = self.now.isoformat()
        read = self.observe(self.drive.perform(create['call']))
        token = self.observe(self.drive.perform(read['call']))
        with self.assertRaises(ValueError):
            self.observe(self.drive.perform(deletes[1]['call']), store=other)
        self.check(token)
        self.assertFalse((other / 'run.lock').exists())

    def invalid_created(self, **changes):
        call = self.start()['call']
        data = self.drive.perform(call)
        data.update(changes)
        with self.assertRaises(ValueError):
            self.observe(data)
        self.assertFalse((self.store / 'run.lock').exists())

    def test_unknown_status(self): self.invalid_created(status='surprise')
    def test_failed_call(self): self.invalid_created(status='error')
    def test_uncertain_call(self): self.invalid_created(status='unknown')
    def test_invalid_id(self): self.invalid_created(id='bad/target')
    def test_invalid_etag(self): self.invalid_created(etag='bad\ncondition')
    def test_invalid_created(self): self.invalid_created(created='yesterday')
    def test_future_created(self): self.invalid_created(created='2026-11-11T08:00:06+00:00')
    def test_wrong_name(self): self.invalid_created(name='another-lock')

    def invalid_file(self, content=None):
        self.start()
        path = self.store / 'lock-observation.json'
        if content is not None:
            path.write_bytes(content)
        with self.assertRaises(ValueError):
            triage.drive_lock(self.store, 'acquire', observation=path, now=self.now)
        self.assertFalse((self.store / 'run.lock').exists())

    def test_missing_observation(self): self.invalid_file()
    def test_long_observation(self):
        valid = dict(status='created', id='i', etag='v', name=triage.LOCK_NAME, created=self.now.isoformat())
        self.invalid_file(json.dumps(valid).encode() + b' ' * 4097)
    def test_malformed_observation(self): self.invalid_file(b'{')
    def test_nonobject_observation(self): self.invalid_file(b'[]')
    def test_observation_without_issued_call(self):
        with self.assertRaises(ValueError): self.observe({'status': 'deleted'})
    def test_delete_without_condition(self):
        self.start()
        path = self.store / 'lock-exchange.json'
        data = json.loads(path.read_text())
        data['phase'] = 'takeover'
        data['call'] = {'operation': 'delete'}
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError): self.observe({'status': 'deleted'})
    def test_busy_young(self):
        self.drive.perform(self.start()['call'])
        (self.store / 'lock-exchange.json').unlink()
        read = self.observe(self.drive.perform(self.start()['call']))
        with self.assertRaisesRegex(ValueError, 'busy'):
            self.observe(self.drive.perform(read['call']))
    def test_expired_holder(self):
        token = self.acquire()
        with self.assertRaises(ValueError):
            triage.require_lock(self.store, token, now=self.now.timestamp() + 2701)
    def test_fresh_read_differs_commit_refused(self):
        token = self.acquire()
        call = self.start('commit', token)['call']
        self.drive.item['etag'] = 'different'
        with self.assertRaises(ValueError): self.observe(self.drive.perform(call), 'commit', token)
        self.assertEqual((self.store / 'state.json').read_text(), '{}')
        # The ownership check names the loss, not a configuration change.
        with self.assertRaisesRegex(ValueError, 'ownership was lost') as caught:
            self.check(token)
        self.assertNotIn('backend', str(caught.exception))
    def test_commit_without_fresh_read(self):
        token = self.acquire()
        with self.assertRaisesRegex(ValueError, 'fresh'):
            triage.commit_state(self.store, token, self.store / 'plan.json')
    def test_release_lost(self):
        token = self.acquire()
        call = self.start('release', token)['call']
        self.drive.item['etag'] = 'different'
        with self.assertRaises(ValueError): self.observe(self.drive.perform(call), 'release', token)
        self.assertIsNotNone(self.drive.item)
    def test_release_not_found(self):
        token = self.acquire()
        call = self.start('release', token)['call']
        self.drive.item = None
        with self.assertRaises(ValueError): self.observe(self.drive.perform(call), 'release', token)
    def test_no_retry_or_second_takeover(self):
        self.start()
        with self.assertRaises(ValueError): self.start()
        self.observe({'status': 'conflict'})
        call = self.observe(dict(status='ok', id='old', etag='v1', name=triage.LOCK_NAME, created='2026-11-11T07:00:00+00:00'))
        self.assertIn('if_match', call['call'])
        self.observe({'status': 'deleted'})
        with self.assertRaises(ValueError): self.observe({'status': 'conflict'})
    def test_lock_config_strict(self):
        for kind in ['gdrive', 'google', 'other', '', 1, None, [], {}]:
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'cannot hold the lock'):
                triage.lock_setting({'lock': {'kind': kind}})
        for value in [None, [], {}, {'kind': 'local', 'folder': 'x'}, {'kind': 'onedrive'},
                      {'kind': 'onedrive', 'account': 'x', 'folder': '../x'},
                      {'kind': 'onedrive', 'account': 1, 'folder': 'x'},
                      {'kind': 'onedrive', 'account': 'x', 'folder': 'x', 'extra': 1}]:
            with self.subTest(value=value), self.assertRaises(ValueError): triage.lock_setting({'lock': value})
    def test_legacy_local_store(self):
        (self.store / 'config.toml').write_text('[accounts.cedar]\n')
        token = triage.acquire(self.store)
        triage.require_lock(self.store, token)
        self.assertEqual(triage.release(self.store, token), 'released')
    def test_local_record_under_drive_not_bypassed(self):
        (self.store / 'run.lock').write_text(json.dumps(dict(token='old', created=self.now.timestamp())))
        with self.assertRaises(ValueError): self.start()
    def test_deterministic_without_subprocess_network(self):
        from unittest.mock import patch
        self.start()
        data = json.loads((self.store / 'lock-exchange.json').read_text())
        with patch.object(triage.subprocess, 'run', side_effect=AssertionError('process')), patch('socket.socket', side_effect=AssertionError('network')):
            first = self.observe({'status': 'conflict'})
            (self.store / 'lock-exchange.json').write_text(json.dumps(data))
            second = self.observe({'status': 'conflict'})
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_observed_value_types_lengths_and_charset(self):
        for key, values in [('id', [None, 1, [], '', 'a'*513, 'bad\n']),
                            ('etag', [None, 1, [], '', 'v'*513, 'bad/etag']),
                            ('created', [None, 1, '', 'x'*41, '2026-11-11T08:00:00', '2026-11-11T08:00:00+01:00', '2026-11-11T09:00:00+01:00']),
                            ('name', [None, 1, [], 'wrong'])]:
            for value in values:
                with self.subTest(key=key, value=value):
                    (self.store / 'lock-exchange.json').unlink(missing_ok=True)
                    self.drive.item = None
                    self.invalid_created(**{key: value})

    def test_extra_and_missing_item_fields(self):
        for change in ('extra', 'missing'):
            (self.store / 'lock-exchange.json').unlink(missing_ok=True)
            self.drive.item = None
            data = self.drive.perform(self.start()['call'])
            if change == 'extra': data['extra'] = 'untrusted'
            else: del data['id']
            with self.assertRaises(ValueError): self.observe(data)

    def test_old_created_result_not_held(self):
        self.invalid_created(created='2026-11-11T07:00:00+00:00')

    def test_unreadable_and_nonregular_observation(self):
        self.start()
        path = self.store / 'obs'
        path.mkdir()
        with self.assertRaises(ValueError): triage.lock_observation(path)
        path.rmdir()
        path.write_text('{}')
        path.chmod(0)
        with self.assertRaises(ValueError): triage.lock_observation(path)

    def test_failure_invalidates_holder_and_steps(self):
        import execute
        from unittest.mock import patch
        import io
        import contextlib
        token = self.acquire()
        output = io.StringIO()
        output.reconfigure = lambda **kwargs: None
        with patch.dict(os.environ, PIGEON_HOME=str(self.store)), contextlib.redirect_stdout(output):
            triage.main(['plan', '--token', token, '--inbox', str(self.store / 'inbox.json'), '--plan-out', str(self.store / 'plan.json')])
        call = self.start('commit', token)['call']
        self.drive.item['id'] = 'another'
        with self.assertRaises(ValueError): self.observe(self.drive.perform(call), 'commit', token)
        with self.assertRaises(ValueError): self.check(token)
        before = (self.store / 'state.json').read_bytes()
        self.assertIn('stop', execute.Execution(self.store, token, self.store / 'plan.json').step())
        self.assertEqual((self.store / 'state.json').read_bytes(), before)

    def test_exchange_operation_token_and_setting_match(self):
        token = self.acquire()
        self.start('release', token)
        for operation, supplied in [('commit', token), ('release', 'wrong')]:
            with self.assertRaises(ValueError): self.observe({'status': 'deleted'}, operation, supplied)
        config = self.store / 'config.toml'
        config.write_text(config.read_text().replace('locks-example', 'other'))
        with self.assertRaises(ValueError): self.observe({'status': 'deleted'}, 'release', token)

    def test_create_held_timestamp_and_future_tolerance(self):
        token = self.acquire()
        held = triage.read_lock(self.store)
        self.assertEqual(held['id'], self.drive.item['id'])
        self.assertEqual(held['backend']['kind'], 'onedrive')
        self.assertEqual(held['created'], self.now.timestamp())
        with self.assertRaises(ValueError): self.check('wrong')
        with self.assertRaises(ValueError): triage.release(self.store, token)

    def test_lock_config_each_field(self):
        for key in ('account', 'folder'):
            for value in [None, 1, [], '', 'x'*513, 'bad/field', 'bad\n']:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    triage.lock_setting({'lock': dict(kind='onedrive', account='a', folder='f', **{}) | {key: value}})

    def test_nonregular_never_opened(self):
        from unittest.mock import patch
        path = self.store / 'pipe'
        os.mkfifo(path)
        with patch.object(Path, 'open', side_effect=AssertionError('must not open pipe')):
            with self.assertRaises(ValueError): triage.lock_observation(path)

    def test_expired_record_allows_new_exchange(self):
        from datetime import timedelta
        token = self.acquire()
        self.now += timedelta(seconds=2701)
        self.assertEqual(self.start()['call']['operation'], 'create')
        with self.assertRaises(ValueError): self.check(token)

    def test_read_mismatch_creation_time(self):
        token = self.acquire()
        call = self.start('commit', token)['call']
        data = self.drive.perform(call)
        data['created'] = '2026-11-11T07:59:59+00:00'
        with self.assertRaises(ValueError): self.observe(data, 'commit', token)

    def test_release_condition_matches_recorded_holder(self):
        token = self.acquire()
        call = self.start('release', token)['call']
        held = triage.read_lock(self.store)
        held['etag'] = 'changed'
        (self.store / 'run.lock').write_text(json.dumps(held))
        with self.assertRaises(ValueError): self.observe(self.drive.perform(call), 'release', token)

    def test_drive_exchange_requires_drive_setting(self):
        (self.store / 'config.toml').write_text('[lock]\nkind="local"\n')
        with self.assertRaises(ValueError): self.start()

    def test_backend_change_stops_holder(self):
        token = self.acquire()
        (self.store / 'config.toml').write_text('[lock]\nkind="local"\n')
        with self.assertRaises(ValueError): self.check(token)

    def test_lock_outputs_protect_exchange_and_observations(self):
        for name in ('lock-exchange.json', 'lock-observation.json', 'lock-commit-observation.json'):
            (self.store / 'config.toml').write_text('[accounts.cedar]\n')
            token = triage.acquire(self.store)
            target = self.store / name
            target.write_text('preserve')
            result = LockTests().run_plan(self.store, token, '--report-out', str(target))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(), 'preserve')

            triage.release(self.store, token)

    def test_invalid_recorded_backend_and_loss_flag(self):
        self.acquire()
        original = triage.read_lock(self.store)
        for change in ({'backend': {'kind': 'local'}}, {'lost': 'yes'}):
            (self.store / 'run.lock').write_text(json.dumps(original | change))
            with self.assertRaises(ValueError): triage.read_lock(self.store)

    def test_injected_clock_does_not_depend_on_wall_clock(self):
        from unittest.mock import patch
        token = self.acquire()
        with patch.object(triage.time, 'time', return_value=1):
            self.check(token)
            read = self.start('commit', token)
            self.assertEqual(self.observe(self.drive.perform(read['call']), 'commit', token), 'verified')

    def test_failed_read_with_well_formed_metadata(self):
        self.start()
        self.observe({'status': 'conflict'})
        with self.assertRaises(ValueError):
            self.observe(dict(status='error', id='i', etag='v', name=triage.LOCK_NAME, created='2026-11-11T07:00:00+00:00'))

    def test_failed_commit_read_with_well_formed_metadata(self):
        token = self.acquire()
        self.start('commit', token)
        with self.assertRaises(ValueError):
            self.observe(dict(status='unknown', **self.drive.item), 'commit', token)

    def test_created_only_after_issued_create(self):
        self.start()
        self.observe({'status': 'conflict'})
        with self.assertRaises(ValueError):
            self.observe(dict(status='created', id='i', etag='v', name=triage.LOCK_NAME, created=self.now.isoformat()))


class DriveStateTests(unittest.TestCase):
    setUp = DriveLockTests.setUp
    start = DriveLockTests.start
    observe = DriveLockTests.observe
    acquire = DriveLockTests.acquire

    def configured(self, kind='gdrive'):
        self.drive.files['state.json'] = (self.store / 'state.json').read_bytes()
        with (self.store / 'config.toml').open('a') as out:
            out.write('\n[state]\nkind="' + kind + '"\naccount="state-example"\nfolder="files-example"\n')
        self.token = self.acquire()
        return self.token

    def exchange(self, operation, observation=None, **kwargs):
        path = None
        if observation is not None:
            path = self.store / 'state-observation.json'
            path.write_text(json.dumps(observation))
        return triage.drive_state(self.store, operation, self.token, path, now=self.now, **kwargs)

    def fetch(self):
        issued = self.exchange('fetch')
        observed = {call['name']: self.drive.file_call(call)['status'] for call in issued['calls']}
        return self.exchange('fetch', observed)

    def send(self, failure=None):
        result = self.exchange('send')
        self.assertEqual(result['call']['operation'], 'read')
        obs = self.store / 'state-lock-observation.json'
        obs.write_text(json.dumps(self.drive.perform(result['call'])))
        result = self.exchange('send', lock_observation=obs)
        names = []
        while 'call' in result:
            call = result['call']
            names.append(call['name'])
            observation = failure if failure is not None else self.drive.file_call(call)
            result = self.exchange('send', observation)
        return names, result

    def test_approval_survives_empty_host_restart(self):
        self.approval_drive_cycle()

    def test_approval_cutoff_replay_empty_host_refused(self):
        self.approval_drive_cycle(replay='empty')

    def test_approval_cutoff_replay_same_host_refused(self):
        self.approval_drive_cycle(replay='same')

    def test_approval_cutoff_after_reading_before_upload_keeps_question_open(self):
        self.approval_drive_cycle(read_cutoff='after')

    def test_approval_cutoff_before_reading_keeps_question_open(self):
        self.approval_drive_cycle(read_cutoff=True)

    def test_stale_question_cleared_from_drive(self):
        self.approval_drive_cycle(stale=True)

    def test_approval_failed_upload_issues_nothing(self):
        self.approval_drive_cycle(failure=True)

    def test_closed_question_not_carried_to_drive(self):
        for closure in ('skip','expired','replaced'):
            for repeated in (False, True):
                with self.subTest(closure=closure, repeated=repeated):
                    self.setUp()
                    self.approval_drive_cycle(closure=closure, repeated=repeated)

    def test_documented_local_state_approval_sequence(self):
        import shlex
        from mock_provider import MockDrive, MockProvider
        section = (ROOT/'docs/quickstart.md').read_text().split('## Approval question\n')[1].split('\n## ')[0]
        local = section.split('### Without drive state\n')[1].split('### With drive state\n')[0]
        steps = [line for line in local.splitlines() if line.startswith('PIGEON_HOME=')]
        self.assertEqual(len(steps), 2)
        reading = next(line for line in section.splitlines() if line.startswith('PIGEON_HOME=') and '--approve RUN_ID --results' in line)
        for drive_lock in (False, True):
            with self.subTest(drive_lock=drive_lock), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp)
                LockTests().prepare(store)
                config = (store/'config.toml').read_text().replace('inbox_limit = 100', 'inbox_limit = 1')
                config = config.replace('# provider = "gmail"', 'provider = "gmail"')
                if drive_lock:
                    config += '\n[lock]\nkind="onedrive"\naccount="drive-example"\nfolder="locks-example"\n'
                (store/'config.toml').write_text(config)
                (store/'rules.toml').write_text('[[rules]]\nid="r"\naccounts=["cedar"]\nsubject="move"\naction="archive"\n')
                (store/'never-touch.toml').write_text('allow_empty=true\n')
                listing = dict(accounts={'cedar':4}, messages=[dict(account='cedar',id=str(i),sender='a@example.net',subject='move',folder='Inbox') for i in range(4)])
                execute.atomic(store/'inbox.json', listing)
                env = dict(os.environ, PIGEON_HOME=str(store))
                def command(script, *args):
                    run = subprocess.run([shutil.which('python3'), str(ROOT/'scripts'/script), *args], env=env, capture_output=True, text=True)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    return run.stdout
                drive = MockDrive()
                drive.created = datetime.now(timezone.utc).isoformat()
                def lock(operation, token=None):
                    args = ['lock' if operation == 'acquire' else operation] + (['--token', token] if token else [])
                    output = command('triage.py', *args)
                    while output.lstrip().startswith('{'):
                        call = json.loads(output)['call']
                        obs = store/'lock-observation.json'
                        obs.write_text(json.dumps(drive.perform(call)))
                        output = command('triage.py', *args, '--observation', str(obs))
                    return output.strip()
                token = lock('acquire')
                path = str(store/'plan.json')
                command('triage.py','plan','--token',token,'--inbox',str(store/'inbox.json'),'--plan-out',path)
                plan = execute.read(Path(path))
                command('execute.py','next','--token',token,'--plan',path)
                def commit_state():
                    args = ['commit-state','--token',token,'--plan',path]
                    output = command('triage.py', *args)
                    if drive_lock:
                        obs = store/'commit-observation.json'
                        obs.write_text(json.dumps(drive.perform(json.loads(output)['call'])))
                        command('triage.py', *args, '--lock-observation', str(obs))
                commit_state()
                lock('release',token)
                token = lock('acquire')
                (store/'reply.txt').write_text('proceed')
                answer = command('triage.py','reply','--token',token,'--inbox',str(store/'inbox.json'),'--reply',str(store/'reply.txt'),'--plan-out',path,'--question',plan['run_id'])
                issued = json.loads(command('execute.py', *shlex.split(answer)[4:]))
                self.assertTrue(issued['calls'][0]['path'].endswith('messages.list'))
                def documented(line):
                    words = shlex.split(line.replace('/absolute/private-store', str(store)).replace('TOKEN',token).replace('RUN_ID',plan['run_id']))
                    return json.loads(command(Path(words[2]).name, *words[3:]))
                execute.atomic(store/'approval-results.json', [dict(ok=True,value=dict(messages=[dict(id=m['id']) for m in listing['messages'][:2]], nextPageToken='next'))])
                issued = documented(reading)
                self.assertIn('round', issued)
                self.assertFalse(triage.question_closed(store, plan))
                execute.atomic(store/'approval-results.json', [dict(ok=True,value=dict(messages=[dict(id=m['id']) for m in listing['messages'][2:]]))])
                issued = documented(reading)
                self.assertEqual(len(issued['calls']),4)
                mailbox = MockProvider([m['id'] for m in listing['messages']])
                for call in issued['calls']:
                    self.assertEqual(mailbox.perform(call), 'ok')
                execute.atomic(store/'gateway-results.json', [dict(ok=True,value={}) for call in issued['calls']])
                # Execute the local branch's ordered commands from its own text.
                documented(steps[0])
                issued = documented(steps[1])
                self.assertIn('final_reads', issued)
                final = store/'final-inbox.json'
                execute.atomic(final, dict(accounts={'cedar':0},messages=[]))
                command('execute.py','final','--token',token,'--plan',path,'--inbox',str(final))
                self.assertEqual(mailbox.ids, [])
                self.assertEqual([r['status'] for r in execute.read(store/'results.json')['results']], ['executed'] * 4)
                commit_state()
                message = command('triage.py','message','--token',token,'--plan',path)
                self.assertIn('4 handled',message)
                self.assertIn('Owner approved',message)
                lock('release',token)
                self.assertFalse((store/'run.lock').exists())

    def test_documented_drive_approval_sequence(self):
        self.approval_drive_cycle(documented=True)

    def test_approval_send_without_lock_observation_refused(self):
        self.approval_drive_cycle(refuse_send=True)

    def approval_drive_cycle(self, replay=None, failure=False, closure=None, refuse_send=False, documented=False, repeated=False, read_cutoff=False, stale=False):
        from unittest.mock import patch
        import contextlib
        import io
        self.now = datetime.now(timezone.utc)
        self.drive.created = self.now.isoformat()
        clock = patch.object(triage.time, 'time', side_effect=lambda: datetime.now(timezone.utc).timestamp())
        clock.start()
        self.addCleanup(clock.stop)
        self.configured()
        self.fetch()
        config = (self.store/'config.toml').read_text()
        (self.store/'config.toml').write_text(config.replace('inbox_limit = 100', 'inbox_limit = 1'))
        (self.store/'rules.toml').write_text('[[rules]]\nid="r"\naccounts=["cedar"]\nsubject="move"\naction="archive"\n')
        (self.store/'never-touch.toml').write_text('allow_empty=true\n')
        listing = dict(accounts={'cedar':4}, messages=[dict(account='cedar',id=str(i),sender='a@example.net',subject='move',folder='Inbox') for i in range(4)])
        execute.atomic(self.store/'inbox.json', listing)
        def command(script, args):
            output = io.StringIO()
            output.reconfigure = lambda **kwargs: None
            with patch.dict(os.environ, PIGEON_HOME=str(self.store)), contextlib.redirect_stdout(output):
                if script is execute:
                    with patch.object(sys, 'argv', ['execute.py'] + args):
                        script.main()
                else:
                    script.main(args, now=self.now)
            return output.getvalue()
        def documented_command(index):
            import shlex
            section = (ROOT/'docs/quickstart.md').read_text().split('## Approval question\n')[1].split('\n## ')[0]
            branch = section.split('### With drive state\n')[1]
            lines = [line for line in branch.splitlines() if line.startswith('PIGEON_HOME=')]
            self.assertEqual(len(lines), 3, 'approval step must show the three drive-resume commands')
            line = lines[index].replace('/absolute/private-store', str(self.store)).replace('TOKEN', self.token)
            return command(execute if 'scripts/execute.py' in line else triage, shlex.split(line)[3:])
        path = self.store/'plan.json'
        command(triage, ['plan','--token',self.token,'--inbox',str(self.store/'inbox.json'),'--plan-out',str(path)])
        plan = execute.read(path)
        self.assertEqual(plan['approval_state'], 'awaiting approval')
        command(execute, ['next','--token',self.token,'--plan',str(path)])
        commit = ['commit-state','--token',self.token,'--plan',str(path)]
        call = json.loads(command(triage,commit))['call']
        observation = self.store/'commit-read.json'
        observation.write_text(json.dumps(self.drive.perform(call)))
        command(triage,commit + ['--lock-observation',str(observation)])
        self.send()
        call = self.start('release',self.token)['call']
        self.observe(self.drive.perform(call),'release',self.token)
        old = self.store
        self.store = old/'empty-host'
        self.store.mkdir()
        for name in ('config.toml','guardrails.md'):
            shutil.copyfile(old/name,self.store/name)
        self.token = self.acquire()
        self.fetch()
        path = self.store/'plan.json'
        self.assertTrue(path.exists(), 'awaiting plan must travel with state')
        self.assertEqual(execute.read(self.store/'inbox.json'),listing)
        reply = self.store/'reply.txt'
        reply.write_text('proceed')
        if closure:
            if closure == 'skip':
                reply.write_text('skip')
            elif closure == 'expired':
                history = self.store/'history.log'
                stamp = datetime.fromtimestamp(triage.time.time()-13*3600,timezone.utc).isoformat()
                history.write_text(stamp + ' approval asked: ' + plan['run_id'] + '\n')
            else:
                triage.approval_event(self.store,plan,'replaced')
            args = ['reply','--inbox',str(self.store/'inbox.json'),'--token',self.token,
                    '--reply',str(reply),'--plan-out',str(path),'--question',plan['run_id']]
            if closure == 'skip':
                command(triage,args)
            else:
                with self.assertRaises(ValueError):
                    command(triage,args)
            if repeated:
                # A repeated asked event cannot reopen a closed run, even with
                # the original awaiting plan restored in the working store.
                execute.atomic(path, plan)
                triage.approval_event(self.store, plan, 'asked')
            self.send()
            self.assertEqual(self.drive.files['plan.json'],b'')
            self.assertEqual(self.drive.files['inbox.json'],b'')
            return
        answer = command(triage,['reply','--inbox',str(self.store/'inbox.json'),'--token',self.token,'--reply',str(reply),'--plan-out',str(path),'--question',plan['run_id']])
        self.assertIn('--approve',answer)
        issued = command(execute,['next','--token',self.token,'--plan',str(path),'--approve',plan['run_id']])
        self.assertIn('approval_reads', json.loads(issued))
        self.assertFalse(triage.question_closed(self.store, plan))
        self.assertNotIn(b'approval approved:', self.drive.files['history.log'])
        self.assertTrue(self.drive.files['plan.json'])
        if read_cutoff is True:
            call = self.start('release',self.token)['call']
            self.observe(self.drive.perform(call),'release',self.token)
            old = self.store
            self.store = old/'after-reading-cutoff'
            self.store.mkdir()
            for name in ('config.toml','guardrails.md'):
                shutil.copyfile(old/name,self.store/name)
            self.token = self.acquire()
            self.fetch()
            path = self.store/'plan.json'
            self.assertFalse(triage.question_closed(self.store, plan))
            again = command(execute,['next','--token',self.token,'--plan',str(path),'--approve',plan['run_id']])
            self.assertIn('approval_reads', json.loads(again))
            self.assertFalse((self.store/'calls.jsonl').exists())
            return
        fresh = self.store/'approval-inbox.json'
        execute.atomic(fresh, dict(listing, messages=listing['messages'][1:]) if stale else listing)
        if documented:
            import shlex
            section = (ROOT/'docs/quickstart.md').read_text().split('## Approval question\n')[1].split('\n## ')[0]
            line = next(line for line in section.splitlines() if line.startswith('PIGEON_HOME=') and '--approve RUN_ID --inbox' in line)
            line = line.replace('/absolute/private-store', str(self.store)).replace('TOKEN',self.token).replace('RUN_ID',plan['run_id'])
            issued = command(execute, shlex.split(line)[3:])
        else:
            issued = command(execute,['next','--token',self.token,'--plan',str(path),'--approve',plan['run_id'],'--inbox',str(fresh)])
        if read_cutoff == 'after':
            # A crashed host cannot finish its pending lock read; expire the mock lease.
            self.drive.item = None
            old = self.store
            self.store = old/'after-reading-cutoff'
            self.store.mkdir()
            for name in ('config.toml','guardrails.md'):
                shutil.copyfile(old/name,self.store/name)
            self.token = self.acquire()
            self.fetch()
            path = self.store/'plan.json'
            self.assertFalse(triage.question_closed(self.store, plan))
            again = command(execute,['next','--token',self.token,'--plan',str(path),'--approve',plan['run_id']])
            self.assertIn('approval_reads', json.loads(again))
            self.assertFalse((self.store/'calls.jsonl').exists())
            return
        if stale:
            self.assertEqual(json.loads(issued)['status'], 'stale')
            self.assertFalse((self.store/'calls.jsonl').exists())
            commit = ['commit-state','--token',self.token,'--plan',str(path)]
            call = json.loads(command(triage,commit))['call']
            observation = self.store/'stale-commit.json'
            observation.write_text(json.dumps(self.drive.perform(call)))
            command(triage,commit + ['--lock-observation',str(observation)])
            self.assertIn('Inbox changed since the question', command(triage,['message','--token',self.token,'--plan',str(path)]))
            triage.approval_event(self.store,plan,'asked')
            self.send()
            self.assertEqual(self.drive.files['plan.json'], b'')
            self.assertEqual(self.drive.files['inbox.json'], b'')
            self.assertIn(b'approval stale:', self.drive.files['history.log'])
            return
        upload = json.loads(issued)
        if refuse_send:
            self.assertEqual(upload['call']['operation'], 'read')
            with self.assertRaisesRegex(ValueError, 'pending or uncertain call'):
                command(triage, ['send-state', '--token', self.token])
            self.assertFalse((self.store/'calls.jsonl').exists())
            return
        if not replay or 'call' in upload:
            self.assertIn('call', upload, 'approval must persist before issuing mail')
            self.assertFalse((self.store/'calls.jsonl').exists())
            with self.assertRaisesRegex(ValueError, 'upload not confirmed'):
                command(execute,['next','--token',self.token,'--plan',str(path)])
            # Approval carries the ordinary send-state lock read and upload protocol.
            if documented:
                observation = self.store/'approval-observation.json'
            observation.write_text(json.dumps(self.drive.perform(upload['call'])))
            issued = documented_command(0) if documented else command(triage, ['send-state','--token',self.token,'--lock-observation',str(observation)])
            if failure:
                with self.assertRaises(ValueError):
                    command(execute,['next','--token',self.token,'--plan',str(path)])
            while 'call' in json.loads(issued):
                call = json.loads(issued)['call']
                observation.write_text(json.dumps(dict(status='failed') if failure else self.drive.file_call(call)))
                issued = documented_command(1) if documented else command(triage,['send-state','--token',self.token,'--observation',str(observation)])
            if failure:
                with self.assertRaises(ValueError):
                    command(execute,['next','--token',self.token,'--plan',str(path)])
                self.assertFalse((self.store/'calls.jsonl').exists())
                return
            self.assertEqual(self.drive.files['plan.json'], b'')
            self.assertEqual(self.drive.files['inbox.json'], b'')
            self.assertIn(b'approval approved:', self.drive.files['history.log'])
            issued = documented_command(2) if documented else command(execute,['next','--token',self.token,'--plan',str(path)])
        self.assertIn('archive',issued)
        from mock_provider import MockProvider
        mailbox = MockProvider([m['id'] for m in listing['messages']])
        for action in plan['actions']:
            call = json.loads(issued)
            self.assertEqual(call['id'],action['id'])
            outcome = mailbox.perform(call)
            command(execute,['result','--token',self.token,'--plan',str(path),
                '--observation',json.dumps(dict(outcome=outcome,inbox_ids=mailbox.ids))])
            if replay:
                # Simulate a cut-off after the first confirmed mailbox write,
                # without send-state. Release only the mock lock for the restart.
                call = self.start('release',self.token)['call']
                self.observe(self.drive.perform(call),'release',self.token)
                old = self.store
                if replay == 'empty':
                    self.store = old/'restarted'
                    self.store.mkdir()
                    for name in ('config.toml','guardrails.md'):
                        shutil.copyfile(old/name,self.store/name)
                self.token = self.acquire()
                self.fetch()
                self.assertFalse((self.store/'plan.json').exists())
                reply = self.store/'reply.txt'
                reply.write_text('proceed')
                with self.assertRaises(ValueError):
                    command(triage,['reply','--token',self.token,'--inbox',str(self.store/'inbox.json'),
                        '--reply',str(reply),'--plan-out',str(self.store/'plan.json'),'--question',plan['run_id']])
                with self.assertRaises((ValueError, OSError)):
                    command(execute,['next','--token',self.token,'--plan',str(self.store/'plan.json'),'--approve',plan['run_id']])
                self.assertEqual(mailbox.ids, ['1','2','3'])
                return
            issued = command(execute,['next','--token',self.token,'--plan',str(path)])
        self.assertTrue(json.loads(issued)['done'])
        commit = ['commit-state','--token',self.token,'--plan',str(path)]
        call = json.loads(command(triage,commit))['call']
        observation = self.store/'commit-read.json'
        observation.write_text(json.dumps(self.drive.perform(call)))
        command(triage,commit + ['--lock-observation',str(observation)])
        message = command(triage,['message','--token',self.token,'--plan',str(path)])
        self.assertIn('4 handled',message)
        self.send()
        self.assertEqual(self.drive.files['plan.json'],b'')
        self.assertEqual(self.drive.files['inbox.json'],b'')
        call = self.start('release',self.token)['call']
        self.observe(self.drive.perform(call),'release',self.token)
        old = self.store
        self.store = old/'next-empty-host'
        self.store.mkdir()
        for name in ('config.toml','guardrails.md'):
            shutil.copyfile(old/name,self.store/name)
        self.token = self.acquire()
        self.fetch()
        self.assertFalse((self.store/'plan.json').exists())
        self.assertFalse((self.store/'inbox.json').exists())

    def test_empty_drive_preserves_local_records(self):
        self.configured()
        self.drive.files.clear()
        (self.store / 'numbers.json').write_text('{"next":8,"items":[]}')
        before = (self.store / 'numbers.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'drive store empty but local records exist'):
            self.fetch()
        self.assertEqual((self.store / 'numbers.json').read_bytes(), before)

    def test_finish_drive_state_batch(self):
        token = self.configured()
        self.fetch()
        (self.store / 'rules.toml').write_text('rules=[]\n')
        (self.store / 'never-touch.toml').write_text('allow_empty=true\n')
        shutil.copyfile(ROOT / 'tests/fixtures/inbox.json', self.store / 'inbox.json')
        listing = execute.read(self.store / 'inbox.json')
        plan = triage.build_plan(listing, [], [], triage.state_config(self.store), {})
        path = self.store / 'plan.json'
        save_plan(self.store, token, path, plan)
        execute.Execution(self.store, token, path).step()
        first = triage.finish_run(self.store, token, path, now=self.now)
        second = triage.finish_run(self.store, token, path, self.drive.perform(first['call']), now=self.now)
        uploads = second['calls'][:-1]
        self.assertGreater(len(uploads), 1)
        self.assertEqual(uploads[-1]['name'], 'state.json')
        outcomes = {c['name']: self.drive.file_call(c) for c in uploads}
        released = self.drive.perform(second['calls'][-1])
        done = triage.finish_run(self.store, token, path, dict(uploads=outcomes, release=released), now=self.now)
        self.assertTrue(done['done'])
        self.assertEqual(done['failed'], [])
        with self.assertRaises(ValueError):
            triage.finish_run(self.store, token, path, dict(uploads=outcomes, release=released), now=self.now)
        self.assertIsNone(self.drive.item)
        self.assertEqual(json.loads(self.drive.files['state.json'])['cedar'], sum(m['account'] == 'cedar' for m in listing['messages']))

    def test_finish_failed_upload_warns_and_releases(self):
        token = self.configured()
        self.fetch()
        first = triage.finish_run(self.store, token, now=self.now)
        second = triage.finish_run(self.store, token, observation=self.drive.perform(first['call']), now=self.now)
        uploads = second['calls'][:-1]
        self.assertTrue(uploads)
        outcomes = {c['name']: dict(status='failed') for c in uploads}
        released = self.drive.perform(second['calls'][-1])
        done = triage.finish_run(self.store, token, observation=dict(uploads=outcomes, release=released), now=self.now)
        self.assertEqual(set(done['failed']), set(outcomes))
        self.assertTrue(done['message'].startswith('Saved files on the drive are out of date:'))
        self.assertIsNone(self.drive.item)
        self.assertTrue((self.store / 'run-message.md').exists())

    def test_incoming_cleanup_refuses_links_and_subfolders(self):
        self.configured()
        incoming = self.store / 'state-incoming'
        incoming.mkdir()
        outside = self.store / 'keep'
        outside.write_text('retained')
        ordinary = incoming / 'old'
        ordinary.write_text('leftover')
        bad = incoming / 'bad'
        for kind in ('link', 'folder', 'pipe'):
            if kind == 'link': bad.symlink_to(outside)
            elif kind == 'folder': bad.mkdir()
            else: os.mkfifo(bad)
            with self.assertRaises(ValueError): self.exchange('fetch')
            self.assertEqual(outside.read_text(), 'retained')
            self.assertTrue(ordinary.exists())
            if kind == 'folder': bad.rmdir()
            else: bad.unlink()
        self.exchange('fetch')
        self.assertEqual(list(incoming.iterdir()), [])

    def test_fetch_preserves_abandoned_exchange_history(self):
        from datetime import timedelta
        with (self.store / 'config.toml').open('a') as out:
            out.write('[state]\nkind="gdrive"\naccount="state-example"\nfolder="files-example"\n')
        self.start()
        with self.assertRaises(ValueError): self.observe({'status': 'error'})
        self.now += timedelta(hours=2)
        self.drive.created = self.now.isoformat()
        self.token = self.acquire()
        extra = '2026-11-11T08:30:00+00:00 abandoned: earlier lock exchange\n'
        with (self.store / 'history.log').open('a') as out:
            out.write(extra + '2026-11-11T07:00:00+00:00 skipped: no new mail\n')
        self.drive.files['history.log'] = ('2026-11-11T08:00:00+00:00 skipped: no new mail\n' + extra).encode()
        self.fetch()
        history = (self.store / 'history.log').read_text()
        self.assertEqual(history.count('abandoned'), 2)
        self.assertIn('skipped: no new mail', history)
        self.assertNotIn('07:00:00', history)
        self.assertIn('history.log', self.send()[0])

    def test_fetch_discards_leftover_regular_files(self):
        self.configured()
        self.exchange('fetch')
        incoming = self.store / 'state-incoming'
        (incoming / 'numbers.json').write_text('invalid')
        with self.assertRaises(ValueError): self.exchange('fetch', {})
        self.assertFalse((self.store / 'numbers.json').exists())
        call = self.start('release', self.token)['call']
        self.observe(self.drive.perform(call), 'release', self.token)
        self.token = self.acquire()
        self.assertEqual(len(self.exchange('fetch')['calls']), len(triage.STATE_FILES))
        self.assertEqual(list(incoming.iterdir()), [])

    def test_all_log_tails_fetch_and_send(self):
        self.configured()
        records = {
            'history.log': '2026-11-11T08:00:00+00:00 skipped: no new mail',
            'calls.jsonl': json.dumps(dict(sequence=1, time=0, run='old', account='cedar', id='x', verb='trash', outcome='issued')),
            'decisions.jsonl': json.dumps(dict(run='old', time=self.now.isoformat(), parts=[])),
        }
        for name, line in records.items():
            self.drive.files[name] = ((line + '\n') * 10000).encode()
        self.fetch()
        for name, line in records.items():
            raw = (self.store / name).read_bytes()
            self.assertLessEqual(len(raw), 262144)
            self.assertEqual(raw, ((line + '\n') * (len(raw) // len(line + '\n'))).encode())
        self.assertTrue(set(records) <= set(self.send()[0]))
        call = self.start('release', self.token)['call']
        self.observe(self.drive.perform(call), 'release', self.token)
        self.token = self.acquire()
        self.assertEqual(self.fetch(), {'done': True})

    def test_logs_bounded_at_fetch_boundary(self):
        self.configured()
        line = b'2026-11-11T08:00:00+00:00 skipped: no new mail\n'
        for name in ('rules.toml', 'never-touch.toml'):
            self.drive.files[name] = (self.store / name).read_bytes()
        self.drive.files['history.log'] = line * 10000
        self.fetch()
        raw = (self.store / 'history.log').read_bytes()
        self.assertLessEqual(len(raw), 262144)
        self.assertTrue(raw.endswith(line))
        self.assertEqual(raw, line * (len(raw) // len(line)))
        self.assertEqual(self.send()[0], ['history.log'])

    def test_default_local_no_setting(self):
        self.assertEqual(triage.state_setting({'accounts': {'cedar': {}}}), {'kind': 'local'})
        self.token = self.acquire()
        self.assertEqual(self.exchange('fetch'), {'done': True})
        self.assertEqual(self.exchange('send'), {'done': True})

    def test_empty_folder_and_two_stores_reproduce(self):
        self.configured('onedrive')
        self.drive.files.clear()
        (self.store / 'state.json').unlink()
        self.assertEqual(self.fetch(), {'done': True})
        self.assertFalse((self.store / 'state.json').exists())
        self.assertTrue((self.store / 'rules.toml').exists())
        listing = {'accounts': {'cedar': 100}, 'messages': [dict(account='cedar', id='x', folder='Inbox', sender='helper@example.org', subject='Question')]}
        plan = triage.build_plan(listing, [], [], triage.state_config(self.store))
        triage.number_decisions(self.store, plan, listing)
        saved = {'cedar': 1, '_positions': {'cedar': '101'}}
        (self.store / 'state.json').write_text(json.dumps(saved))
        names, result = self.send()
        self.assertEqual(names[0], 'numbers.json')
        self.assertEqual(names[-1], 'state.json')
        self.assertEqual(result['failed'], [])
        call = self.start('release', self.token)['call']
        self.observe(self.drive.perform(call), 'release', self.token)
        other = self.store / 'other'
        other.mkdir()
        for name in ('config.toml', 'guardrails.md'):
            shutil.copyfile(self.store / name, other / name)
        self.store = other
        self.token = self.acquire()
        self.fetch()
        self.assertEqual(json.loads((other / 'state.json').read_text()), saved)
        again = triage.build_plan(listing, [], [], triage.state_config(other), saved)
        triage.number_decisions(other, again, listing)
        self.assertEqual(again['decisions'], plan['decisions'])
        self.assertEqual(self.send()[0], [])

    def test_gdrive_state_on_onedrive_lock(self):
        self.configured()
        result = self.exchange('fetch')
        self.assertTrue(all(c['kind'] == 'gdrive' for c in result['calls']))
        self.assertEqual(tuple(c['name'] for c in result['calls']), ('numbers.json', 'unsubscribe-secret', 'unsubscribe-failures.json', 'decisions.jsonl', 'calls.jsonl', 'history.log', 'run-message.md', 'rules.toml', 'never-touch.toml', 'tool-paths.json', 'plan.json', 'inbox.json', 'state.json'))

    def test_state_config_refusals(self):
        for value in (None, [], {}, {'kind': 'typo'}, {'kind': 'local', 'account': 'a'},
                      {'kind': 'gdrive'}, {'kind': 'gdrive', 'account': 'a', 'folder': '../x'},
                      {'kind': 'onedrive', 'account': 4, 'folder': 'f'},
                      {'kind': 'gdrive', 'account': 'a', 'folder': 'f', 'extra': 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                triage.state_setting({'state': value, 'lock': {'kind': 'onedrive', 'account': 'a', 'folder': 'f'}})
        with self.assertRaisesRegex(ValueError, 'requires'):
            triage.state_setting({'state': {'kind': 'gdrive', 'account': 'a', 'folder': 'f'}})

    def test_fetch_failure_uncertain_and_unexpected_names(self):
        self.configured()
        for change in ({'state.json': 'failed'}, {'state.json': 'unknown'}, {'../x': 'absent'}, {'state.json': []}):
            (self.store / 'state-exchange.json').unlink(missing_ok=True)
            self.exchange('fetch')
            observed = dict.fromkeys(triage.STATE_FILES, 'absent') | change
            before = (self.store / 'state.json').read_bytes()
            with self.assertRaises(ValueError): self.exchange('fetch', observed)
            self.assertEqual((self.store / 'state.json').read_bytes(), before)
            with self.assertRaises(ValueError): triage.require_fetched(self.store, self.token)

    def bad_download(self, filename, content=None, mode=None):
        self.exchange('fetch')
        path = self.store / 'state-incoming' / filename
        if mode == 'directory': path.mkdir()
        elif mode == 'symlink': path.symlink_to(self.store / 'state.json')
        elif mode == 'pipe': os.mkfifo(path)
        elif content is not None: path.write_bytes(content)
        if mode == 'unreadable': path.chmod(0)
        before = (self.store / 'state.json').read_bytes()
        observed = dict.fromkeys(triage.STATE_FILES, 'absent')
        observed[filename] = 'fetched'
        if mode == 'pipe':
            from unittest.mock import patch
            original_open = Path.open
            def checked_open(candidate, *args, **kwargs):
                if candidate == path:
                    raise AssertionError('must not open a pipe')
                return original_open(candidate, *args, **kwargs)
            with patch.object(Path, 'open', checked_open), self.assertRaises(ValueError):
                self.exchange('fetch', observed)
        else:
            with self.assertRaises((ValueError, OSError)):
                self.exchange('fetch', observed)
        self.assertEqual((self.store / 'state.json').read_bytes(), before)

    def test_bad_downloads_all_formats(self):
        self.configured()
        for filename, content in [('state.json', b'{"cedar":-1}'), ('numbers.json', b'{"next":0,"items":[]}'),
                                  ('rules.toml', b'rules=false'), ('never-touch.toml', b'allow_empty=false'),
                                  ('unsubscribe-secret', b'bad'), ('unsubscribe-failures.json', b'{"senders":["bad"]}'),
                                  ('decisions.jsonl', b'{}\n'), ('calls.jsonl', b'{}\n'),
                                  ('history.log', b'wrong\n'), ('run-message.md', b'\x00'),
                                  ('state.json', b'\xff'), ('state.json', b'{' ),
                                  ('run-message.md', b'x' * (triage.STATE_LIMIT + 1))]:
            with self.subTest(filename=filename, content=content[:40]):
                (self.store / 'state-exchange.json').unlink(missing_ok=True)
                incoming = self.store / 'state-incoming'
                if incoming.exists():
                    for p in incoming.iterdir(): p.unlink()
                self.bad_download(filename, content)

    def test_nonregular_unreadable_missing_and_extra_files(self):
        self.configured()
        for mode in ('directory', 'symlink', 'pipe', 'unreadable', 'missing'):
            with self.subTest(mode=mode):
                (self.store / 'state-exchange.json').unlink(missing_ok=True)
                incoming = self.store / 'state-incoming'
                if incoming.exists():
                    for p in incoming.iterdir():
                        if p.is_dir(): p.rmdir()
                        else: p.unlink()
                self.bad_download('state.json', b'{}' if mode == 'unreadable' else None, mode)
        (self.store / 'state-exchange.json').unlink()
        self.exchange('fetch')
        (self.store / 'state-incoming' / 'extra').write_text('x')
        with self.assertRaises(ValueError): self.exchange('fetch', dict.fromkeys(triage.STATE_FILES, 'absent'))

    def test_send_requires_fetch_and_fresh_matching_read(self):
        self.configured()
        with self.assertRaises(ValueError): self.exchange('send')
        self.fetch()
        obs = self.store / 'lock-read.json'
        obs.write_text(json.dumps(dict(status='ok', **self.drive.item)))
        with self.assertRaises(ValueError): self.exchange('send', lock_observation=obs)
        self.exchange('send')
        self.drive.item['etag'] = 'changed'
        obs.write_text(json.dumps(dict(status='ok', **self.drive.item)))
        with self.assertRaises(ValueError): self.exchange('send', lock_observation=obs)

    def test_size_mismatch_and_failed_upload_warn_without_retry(self):
        self.configured()
        self.fetch()
        (self.store / 'state.json').write_text('{"cedar":1}')
        opening = 'Late start: slot 2026-11-11 08:00 UTC, delay 20 minutes\n📬 Triage · example\nmarble: not listed this run\n'
        (self.store / 'run-message.md').write_text(opening)
        names, result = self.send({'status': 'done', 'size': -1})
        lines = (self.store / 'run-message.md').read_text().splitlines()
        self.assertEqual(lines, [result['warning'], *opening.splitlines()])
        self.assertEqual(names.count('state.json'), 1)
        self.assertIn('state.json', result['failed'])
        self.assertIn('out of date', (self.store / 'run-message.md').read_text().splitlines()[0])
        with self.assertRaises(ValueError): self.exchange('send')
        self.assertEqual(self.start('release', self.token)['call']['operation'], 'delete')

    def test_failed_uncertain_upload(self):
        self.configured()
        self.fetch()
        names, result = self.send({'status': 'failed'})
        self.assertEqual(result['failed'], names)
        self.assertTrue(result['warning'].startswith('Saved files'))
        with self.assertRaises(ValueError): self.exchange('send', {'status': 'done', 'size': 0})

    def test_skip_only_history_changed(self):
        self.configured()
        for name in ('rules.toml', 'never-touch.toml', 'state.json'):
            self.drive.files[name] = (self.store / name).read_bytes()
        self.fetch()
        (self.store / 'history.log').write_text('2026-11-11T08:00:00+00:00 skipped: no new mail\n')
        self.assertEqual(self.send()[0], ['history.log'])

    def test_stopped_and_refused_send_only_changed_files(self):
        self.configured()
        for name in ('rules.toml', 'never-touch.toml', 'state.json'):
            self.drive.files[name] = (self.store / name).read_bytes()
        self.fetch()
        self.assertEqual(self.send()[0], [])

    def test_lost_lock_sends_nothing(self):
        self.configured()
        self.fetch()
        held = triage.read_lock(self.store)
        held['lost'] = True
        (self.store / 'run.lock').write_text(json.dumps(held))
        before = len(self.drive.calls)
        with self.assertRaises(ValueError): self.exchange('send')
        self.assertEqual(len(self.drive.calls), before)

    def plan_command(self, *args):
        import io
        import contextlib
        from unittest.mock import patch
        output = io.StringIO()
        output.reconfigure = lambda **kwargs: None
        with patch.dict(os.environ, PIGEON_HOME=str(self.store)), contextlib.redirect_stdout(output):
            triage.main(list(args), now=self.now)
        return output.getvalue()

    def commit(self):
        call = self.start('commit', self.token)['call']
        self.observe(self.drive.perform(call), 'commit', self.token)
        triage.commit_state(self.store, self.token, self.store / 'plan.json', self.store / 'results.json', now=self.now, lock_verified=True)

    def test_reply_decisions_and_confirmed_rule_uploaded(self):
        import execute
        self.configured()
        self.fetch()
        listing = dict(accounts={'cedar': 100}, messages=[dict(account='cedar', id='x', folder='Inbox', sender='helper@example.org', subject='Question')])
        (self.store / 'inbox.json').write_text(json.dumps(listing))
        (self.store / 'numbers.json').write_text(json.dumps(dict(next=2, items=[dict(number=1, account='cedar', id='x')])))
        (self.store / 'state.json').write_text('{"cedar":1}')
        (self.store / 'reply.txt').write_text('1 trash')
        self.plan_command('reply', '--token', self.token, '--inbox', str(self.store / 'inbox.json'), '--reply', str(self.store / 'reply.txt'), '--plan-out', str(self.store / 'plan.json'))
        machine = execute.Execution(self.store, self.token, self.store / 'plan.json')
        self.assertEqual(machine.step()['action'], 'trash')
        self.assertEqual(machine.step(execute.AssistantProvider(dict(outcome='ok', inbox_ids=[])))['status'], 'executed')
        self.assertEqual(machine.step(), {'done': True})
        self.commit()
        (self.store / 'rules.toml').write_text('rules=[]\n')
        names, _ = self.send()
        self.assertIn('decisions.jsonl', names)
        self.assertIn('rules.toml', names)
        self.assertEqual(json.loads(self.drive.files['decisions.jsonl'])['parts'][0]['outcome'], 'executed')
        self.assertNotIn('config.toml', names)
        self.assertNotIn('guardrails.md', names)

    def test_deterministic_without_process_or_network(self):
        from unittest.mock import patch
        self.configured()
        self.fetch()
        (self.store / 'history.log').write_text('2026-11-11T08:00:00+00:00 skipped: no new mail\n')
        before = (self.store / 'state-exchange.json').read_bytes()
        with patch.object(triage.subprocess, 'run', side_effect=AssertionError('process')), patch('socket.socket', side_effect=AssertionError('network')):
            first = self.exchange('send')
            exchange = (self.store / 'lock-exchange.json').read_bytes()
            (self.store / 'lock-exchange.json').unlink()
            second = self.exchange('send')
        self.assertEqual(first, second)
        self.assertEqual((self.store / 'state-exchange.json').read_bytes(), before)
        self.assertEqual((self.store / 'lock-exchange.json').read_bytes(), exchange)

    def test_additional_format_guards(self):
        self.configured()
        config = triage.state_config(self.store)
        cases = [
            ('rules.toml', b'[[rules]]\nid="r"\naccounts=["unknown"]\nsender="helper@example.org"\naction="archive"'),
            ('decisions.jsonl', json.dumps(dict(run='r', time='2026-11-11T08:00:00', parts=[])).encode()),
            ('decisions.jsonl', json.dumps(dict(run='r', time=self.now.isoformat(), parts=[dict(number=0,account='cedar',id='x',verb='leave',target=None,outcome='answered')])).encode()),
            ('calls.jsonl', json.dumps(dict(sequence=0,time=0,run='r',account='cedar',id='x',verb='trash',outcome='issued')).encode()),
            ('history.log', b'2026-11-11T08:00:00+01:00 skipped: no new mail\n'),
            ('unsubscribe-failures.json', b'{"version":1,"senders":[]}'),
        ]
        for name, content in cases:
            with self.subTest(name=name), self.assertRaises(ValueError):
                triage.validate_state_file(name, content, config, self.store)

    def test_exchange_guards(self):
        self.configured()
        with self.assertRaises(ValueError): self.exchange('fetch', {})
        self.exchange('fetch')
        with self.assertRaises(ValueError): self.exchange('fetch')
        (self.store / 'state-exchange.json').unlink()
        (self.store / 'state-incoming' / 'unexpected').write_text('x')
        self.exchange('fetch')
        self.assertFalse((self.store / 'state-incoming' / 'unexpected').exists())
        (self.store / 'state.json').unlink(missing_ok=True)
        self.exchange('fetch', dict.fromkeys(triage.STATE_FILES, 'absent'))
        with self.assertRaises(ValueError): self.exchange('fetch', dict.fromkeys(triage.STATE_FILES, 'absent'))
        for value in ('[]', '{', '{}' + ' ' * 4097):
            path = self.store / 'state-observation.json'
            path.write_text(value)
            with self.assertRaises(ValueError): triage.state_observation(path)

    def test_planning_and_execution_before_fetch_refused(self):
        import execute
        self.configured()
        (self.store / 'plan.json').write_text('{"actions":[]}')
        with self.assertRaises(ValueError):
            execute.Execution(self.store, self.token, self.store / 'plan.json')
        with self.assertRaises(ValueError): self.plan_command('quiet-check', '--token', self.token)
        with self.assertRaises(ValueError):
            self.plan_command('plan', '--token', self.token, '--inbox', str(self.store / 'inbox.json'))

    def test_failed_batch_never_installs_valid_prefix(self):
        self.configured()
        self.exchange('fetch')
        incoming = self.store / 'state-incoming'
        (incoming / 'numbers.json').write_text('{"next":1,"items":[]}')
        (incoming / 'state.json').write_text('{"cedar":-1}')
        before = (self.store / 'state.json').read_bytes()
        observed = dict.fromkeys(triage.STATE_FILES, 'absent')
        observed.update({'numbers.json': 'fetched', 'state.json': 'fetched'})
        with self.assertRaises(ValueError): self.exchange('fetch', observed)
        self.assertFalse((self.store / 'numbers.json').exists())
        self.assertEqual((self.store / 'state.json').read_bytes(), before)

    def test_snapshot_and_cleared_message(self):
        self.configured()
        self.fetch()
        (self.store / 'run-message.md').write_text('Undelivered\n')
        self.assertIn('run-message.md', self.send()[0])
        (self.store / 'run-message.md').unlink()
        self.assertEqual(self.send()[0], ['run-message.md'])
        self.assertEqual(self.drive.files['run-message.md'], b'')
        self.assertFalse((self.store / 'run-message.md').exists())
        self.assertEqual(self.send()[0], [])
        call = self.start('release', self.token)['call']
        self.observe(self.drive.perform(call), 'release', self.token)
        self.token = self.acquire()
        self.fetch()
        self.assertFalse((self.store / 'run-message.md').exists())

    def test_snapshot_bytes_unchanged_during_transport(self):
        self.configured()
        self.fetch()
        (self.store / 'numbers.json').write_text('{"next":1,"items":[]}')
        call = self.exchange('send')['call']
        path = self.store / 'state-lock-observation.json'
        path.write_text(json.dumps(self.drive.perform(call)))
        upload = self.exchange('send', lock_observation=path)['call']
        self.assertEqual(upload['name'], 'numbers.json')
        snapshot = Path(upload['path']).read_bytes()
        (self.store / 'numbers.json').write_text('{"next":2,"items":[]}')
        self.assertEqual(Path(upload['path']).read_bytes(), snapshot)

    def test_incoming_and_outgoing_symlink_folders(self):
        self.configured()
        other = self.store / 'other'
        other.mkdir()
        incoming = self.store / 'state-incoming'
        incoming.symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError): self.exchange('fetch')
        incoming.unlink()
        self.fetch()
        (self.store / 'state-outgoing').symlink_to(other, target_is_directory=True)
        call = self.exchange('send')['call']
        path = self.store / 'state-lock-observation.json'
        path.write_text(json.dumps(self.drive.perform(call)))
        with self.assertRaises(ValueError): self.exchange('send', lock_observation=path)

    def test_fetch_matching_settings_and_token(self):
        self.configured()
        self.fetch()
        path = self.store / 'state-exchange.json'
        original = path.read_text()
        data = json.loads(original)
        for change in ({'token': 'wrong'}, {'setting': {'kind': 'gdrive', 'account': 'other', 'folder': 'files-example'}}):
            path.write_text(json.dumps(data | change))
            with self.assertRaises(ValueError): triage.require_fetched(self.store, self.token)
            with self.assertRaises(ValueError): self.exchange('fetch', {})
        path.write_text(original)

    def test_skip_uses_quiet_check_and_only_sends_history(self):
        self.configured()
        config = triage.state_config(self.store)
        config['schedule'] = dict(weekdays=['08:00'], weekends=['08:00'], always_full=[])
        previous = {'cedar': 1, 'marble': 0, '_positions': {'cedar': '100', 'marble': []}}
        self.drive.files['state.json'] = json.dumps(previous).encode()
        for name in ('rules.toml', 'never-touch.toml'):
            self.drive.files[name] = (self.store / name).read_bytes()
        self.fetch()
        obs = self.store / 'quiet-observation.json'
        obs.write_text('{"cedar":{"outcome":"ok","position":"100","arrivals":[]},"marble":{"outcome":"ok","ids":[]}}')
        self.assertEqual(triage.quiet_check(self.store, config, previous, obs, True, self.now)['decision'], 'skip')
        self.assertEqual(self.send()[0], ['history.log'])

    def test_stopped_plan_commits_and_uploads_observed_counts(self):
        import execute
        self.configured()
        for name in ('rules.toml', 'never-touch.toml'):
            self.drive.files[name] = (self.store / name).read_bytes()
        self.drive.files['state.json'] = b'{"cedar":3}'
        self.fetch()
        (self.store / 'inbox.json').write_text('{"accounts":{"cedar":100},"messages":[]}')
        self.plan_command('plan', '--token', self.token, '--inbox', str(self.store / 'inbox.json'), '--plan-out', str(self.store / 'plan.json'))
        self.assertTrue(json.loads((self.store / 'plan.json').read_text())['reasons'])
        self.assertEqual(execute.Execution(self.store, self.token, self.store / 'plan.json').step(), {'done': True})
        self.commit()
        names, _ = self.send()
        self.assertEqual(set(names), {'numbers.json', 'state.json'})
        self.assertEqual(json.loads(self.drive.files['state.json']), {'version': 1, 'cedar': 0})

    def test_lost_lock_fetch_no_download_issued(self):
        self.configured()
        held = triage.read_lock(self.store)
        held['lost'] = True
        (self.store / 'run.lock').write_text(json.dumps(held))
        with self.assertRaises(ValueError): self.exchange('fetch')

    def test_install_failure_rolls_back_entire_batch(self):
        import execute
        from unittest.mock import patch
        self.configured()
        self.exchange('fetch')
        incoming = self.store / 'state-incoming'
        (incoming / 'numbers.json').write_text('{"next":1,"items":[]}')
        (incoming / 'state.json').write_text('{"cedar":2}')
        before = (self.store / 'state.json').read_bytes()
        observed = dict.fromkeys(triage.STATE_FILES, 'absent')
        observed.update({'numbers.json':'fetched', 'state.json':'fetched'})
        atomic = execute.atomic_text
        failed = False
        def write(path, text):
            nonlocal failed
            if path.name == 'state.json' and not failed:
                failed = True
                raise OSError('injected publication failure')
            return atomic(path, text)
        with patch.object(execute, 'atomic_text', write), self.assertRaises(OSError):
            self.exchange('fetch', observed)
        self.assertFalse((self.store / 'numbers.json').exists())
        self.assertEqual((self.store / 'state.json').read_bytes(), before)

    def test_unknown_state_kind_with_complete_fields(self):
        for kind in ('typo', '', None, 3, [], {}):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                triage.state_setting(dict(state=dict(kind=kind, account='a', folder='f'), lock=dict(kind='onedrive', account='a', folder='f')))

    def test_pending_fetch_rejects_wrong_token_and_settings(self):
        self.configured()
        self.exchange('fetch')
        path = self.store / 'state-exchange.json'
        data = json.loads(path.read_text())
        for change in ({'token': 'wrong'}, {'setting': {'kind':'gdrive','account':'wrong','folder':'f'}}):
            path.write_text(json.dumps(data | change))
            with self.assertRaises(ValueError): self.exchange('fetch', dict.fromkeys(triage.STATE_FILES, 'absent'))

    def test_failure_memory_unknown_fields(self):
        with self.assertRaises(ValueError):
            triage.validate_unsubscribe_memory(dict(version=2, senders=[], extra=1))

    def test_decision_batch_run_and_target_guards(self):
        self.configured()
        config = triage.state_config(self.store)
        part = dict(number=1,account='cedar',id='x',verb='move',target='../x',outcome='executed')
        for batch in (dict(run='bad/run',time=self.now.isoformat(),parts=[]), dict(run='r',time=self.now.isoformat(),parts=[part])):
            with self.assertRaises(ValueError):
                triage.validate_state_file('decisions.jsonl', json.dumps(batch).encode(), config, self.store)

    def test_state_setting_checked_by_input_validator(self):
        with self.assertRaises(ValueError):
            triage.validate_inputs(dict(accounts={}, messages=[]), dict(accounts={'cedar':{}},state={'kind':'typo'}), {})

    def test_state_handle_types_lengths_and_characters(self):
        for key in ('account', 'folder'):
            for value in (None, 1, [], '', 'x'*513, '../x', 'bad\n', 'é'):
                setting = dict(kind='gdrive', account='a', folder='f') | {key:value}
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    triage.state_setting(dict(state=setting, lock=dict(kind='onedrive',account='a',folder='f')))

    def test_upload_observation_types_and_unknown_fields(self):
        for value in ({'status':'unknown'}, {'status':'done','size':True}, {'status':'done','size':'1'},
                      {'status':'done','size':None}, {'status':'done','size':1.0},
                      {'status':'done','size':0,'name':'../other'}, {}, []):
            case = DriveStateTests('test_default_local_no_setting')
            case.setUp()
            try:
                case.configured()
                case.fetch()
                names, result = case.send(value)
                self.assertEqual(result['failed'], names)
                self.assertTrue(all(n in triage.STATE_FILES for n in names))
                self.assertIn('out of date', (case.store/'run-message.md').read_text().splitlines()[0])
            finally:
                case.doCleanups()

    def test_history_requires_skip_record_suffix(self):
        self.configured()
        with self.assertRaises(ValueError):
            triage.validate_state_file('history.log', b'2026-11-11T08:00:00+00:00 something else\n', triage.state_config(self.store), self.store)

    def test_deep_fetch_observation_refuses_on_one_line(self):
        from datetime import datetime, timezone
        self.configured()
        self.exchange('fetch')
        # The child CLI uses its real UTC clock, independently of the fixture clock.
        held = triage.read_lock(self.store)
        held['created'] = held['drive_created'] = datetime.now(timezone.utc).timestamp()
        (self.store / 'run.lock').write_text(json.dumps(held))
        path = self.store / 'deep.json'
        path.write_text('[' * 2000 + ']' * 2000)
        # Python 3.14's iterative decoder accepts this nesting; reproduce the
        # parser limit older supported interpreters raise inside the real CLI.
        code = '''import json, runpy, sys
original = json.loads
def loads(raw, *args, **kwargs):
    if isinstance(raw, bytes) and raw.startswith(b'[' * 100):
        raise RecursionError('parser nesting limit')
    return original(raw, *args, **kwargs)
json.loads = loads
sys.argv = sys.argv[1:]
sys.path.insert(0, 'scripts')
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        result = subprocess.run([shutil.which('python3'), '-c', code, 'scripts/triage.py', 'fetch-state', '--token', self.token, '--observation', str(path)], cwd=ROOT, env=dict(os.environ, PIGEON_HOME=str(self.store)), capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertNotIn('Traceback', result.stderr)

    def test_state_observations_require_utf8(self):
        path = self.store / 'state-observation.json'
        path.write_bytes('{"status":"done","size":0}'.encode('utf-16'))
        with self.assertRaises(ValueError): triage.state_observation(path)

    def test_state_file_parser_requires_utf8(self):
        self.configured()
        with self.assertRaises(ValueError):
            triage.validate_state_file('run-message.md', b'hello\xff', triage.state_config(self.store), self.store)

    def test_exchange_validates_state_kind(self):
        self.configured()
        path = self.store / 'config.toml'
        path.write_text(path.read_text().replace('kind="gdrive"', 'kind="typo"'))
        with self.assertRaises(ValueError): self.exchange('fetch')

    def test_execution_reuses_failure_memory_validator(self):
        import execute
        machine = object.__new__(execute.Execution)
        machine.store = self.store
        (self.store/'unsubscribe-failures.json').write_text('{"senders":["bad"]}')
        with triage.lock_gate(self.store) as gate, self.assertRaises(ValueError):
            machine.unsubscribe(dict(account='cedar', id='m1', url=''), gate)

    def test_send_refuses_invalid_local_file_before_upload(self):
        self.configured()
        self.fetch()
        (self.store / 'rules.toml').write_text('rules=false\n')
        call = self.exchange('send')['call']
        observation = self.store / 'state-lock-observation.json'
        observation.write_text(json.dumps(self.drive.perform(call)))
        with self.assertRaises(ValueError):
            self.exchange('send', lock_observation=observation)

class RequiredConfigTests(unittest.TestCase):
    def test_local_takeover_margin_and_holder_expiry(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            shutil.copyfile(ROOT / 'config/config.example.toml', store / 'config.toml')
            with patch.object(triage.time, 'time', return_value=10000):
                token = triage.acquire(store)
            for minutes in (45, 46, 54, 55, 56):
                with patch.object(triage.time, 'time', return_value=10000 + minutes * 60):
                    if minutes <= 55:
                        with self.assertRaises(ValueError): triage.acquire(store)
                    else:
                        self.assertNotEqual(triage.acquire(store), token)
                    if minutes > 45:
                        with self.assertRaises(ValueError): triage.require_lock(store, token)

    def test_lock_requires_config_and_example_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            result = subprocess.run([shutil.which('python3'), 'scripts/triage.py', 'lock'], cwd=ROOT,
                                    env={'PATH': os.environ['PATH'], 'PIGEON_HOME': tmp}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('config.toml', result.stderr)
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertFalse((store / 'run.lock').exists())
            shutil.copyfile(ROOT / 'config/config.example.toml', store / 'config.toml')
            result = subprocess.run([shutil.which('python3'), 'scripts/triage.py', 'lock'], cwd=ROOT,
                                    env={'PATH': os.environ['PATH'], 'PIGEON_HOME': tmp}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class ParserRefusalTests(unittest.TestCase):
    def test_malformed_files_name_source_without_decoder_text(self):
        import execute
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            cases = [
                ('config.toml', '[', lambda p: triage.store_lock_setting(store)),
                ('state.json', '?', lambda p: execute.read(p)),
                ('numbers.json', '?', lambda p: triage.read_numbers(store)),
                ('state-observation.json', '?', triage.state_observation),
                ('lock-observation.json', '?', triage.lock_observation),
                ('rules.toml', '[', lambda p: triage.parse_rules(p.read_text())),
                ('never-touch.toml', '[', lambda p: triage.parse_rules(p.read_text(), protection=True)),
            ]
            for name, bad, read in cases:
                with self.subTest(name=name):
                    path = store / name
                    path.write_text(bad)
                    with self.assertRaises(ValueError) as caught:
                        read(path)
                    line = str(caught.exception)
                    self.assertIn(name, line)
                    self.assertIn('expected', line)
                    self.assertNotRegex(line, r'Expecting|line 1 column|Invalid initial|at line')

    def test_valid_parser_inputs_still_pass(self):
        import execute
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            (store / 'config.toml').write_text('[accounts.cedar]\n')
            self.assertEqual(triage.store_lock_setting(store), {'kind': 'local'})
            for name, value, read in [
                ('state.json', {'cedar': 0}, execute.read),
                ('numbers.json', {'next': 1, 'items': []}, lambda p: triage.read_numbers(store)),
                ('state-observation.json', {'status': 'failed'}, triage.state_observation),
                ('lock-observation.json', {'status': 'error'}, triage.lock_observation),
            ]:
                with self.subTest(name=name):
                    p = store / name
                    p.write_text(json.dumps(value))
                    self.assertEqual(read(p), value)
            self.assertEqual(triage.parse_rules('rules=[]'), [])
            self.assertEqual(triage.parse_rules('allow_empty=true', protection=True), [])

    def test_utf8_refusals_and_valid_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'rules.toml'
            p.write_bytes(b'\xff')
            for read in (triage.state_bytes, lambda p: triage.load_inputs([p])):
                with self.subTest(read=read):
                    with self.assertRaises(ValueError) as caught:
                        read(p)
                    self.assertIn(str(p), str(caught.exception))
                    self.assertIn('UTF-8', str(caught.exception))
            p.write_text('rules=[]\n')
            self.assertEqual(triage.state_bytes(p), b'rules=[]\n')
            self.assertEqual(triage.load_inputs([p]), ['rules=[]\n'])

    def test_history_read_description_matches_saved_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            (store / 'config.toml').write_text('[accounts.cedar]\nquiet_kind="history"\n')
            token = triage.acquire(store)
            for positions in ({}, {'_positions': {'cedar': '100'}}):
                (store / 'state.json').write_text(json.dumps(positions))
                result = subprocess.run(['env', '-i', 'PATH=' + os.environ['PATH'],
                    'PIGEON_HOME=' + str(store), 'python3', str(ROOT / 'scripts/triage.py'),
                    'quiet-check', '--token', token], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                read = json.loads(result.stdout)['reads']['cedar']
                if positions:
                    self.assertEqual(read['since'], '100')
                    self.assertIn('arrivals', read['read'])
                else:
                    self.assertIsNone(read['since'])
                    self.assertEqual(read['read'], 'current position only')

    def test_saved_log_timestamps_name_file(self):
        config = {'accounts': {'cedar': {}}}
        cases = {
            'history.log': '2026-99-11T08:00:00+00:00 skipped: no new mail\n',
            'decisions.jsonl': json.dumps(dict(run='run-example', time='invalid', parts=[])),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in cases.items():
                with self.subTest(name=name):
                    with self.assertRaises(ValueError) as caught:
                        triage.validate_state_file(name, text.encode(), config, Path(tmp))
                    self.assertIn(name, str(caught.exception))
                    self.assertNotIn('isoformat', str(caught.exception))
                    self.assertNotIn('month must', str(caught.exception))
            for name, text in {
                'history.log': '2026-11-11T08:00:00+00:00 skipped: no new mail\n',
                'decisions.jsonl': json.dumps(dict(run='run-example', time='2026-11-11T08:00:00+00:00', parts=[])),
            }.items():
                triage.validate_state_file(name, text.encode(), config, Path(tmp))

    def test_saved_json_utf8_names_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            path = store / 'numbers.json'
            path.write_bytes(b'\xff')
            with self.assertRaises(ValueError) as caught:
                triage.read_numbers(store)
            self.assertIn(str(path), str(caught.exception))
            self.assertNotIn('codec', str(caught.exception))

    def test_reply_utf8_refusal_names_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            LockTests().prepare(store)
            token = triage.acquire(store)
            for raw, refused in ((b'1 \xff', True), ('1 leave'.encode(), False)):
                with self.subTest(raw=raw):
                    (store / 'reply.txt').write_bytes(raw)
                    result = subprocess.run([shutil.which('python3'), 'scripts/triage.py', 'reply', '--token', token,
                                             '--inbox', str(store / 'inbox.json'), '--reply', str(store / 'reply.txt'),
                                             '--plan-out', str(store / 'plan.json'), '--report-out', str(store / 'report.md')],
                                            cwd=ROOT, env={'PATH': os.environ['PATH'], 'PIGEON_HOME': tmp}, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn('codec', result.stdout)
                    self.assertEqual('--reply: expected UTF-8 text' in result.stdout, refused)
                    self.assertEqual(json.loads((store / 'plan.json').read_text())['actions'], [])

    def test_state_phase_refusal_names_exchange(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            (store / 'config.toml').write_text('[lock]\nkind="onedrive"\naccount="drive-example"\nfolder="locks-example"\n[state]\nkind="gdrive"\naccount="drive-example"\nfolder="state-example"\n')
            setting = triage.state_setting(triage.state_config(store))
            for phase in ('fetch', 'upload', 'done'):
                (store / 'state-exchange.json').write_text(json.dumps(dict(token='test', phase=phase, setting=setting)))
                with self.assertRaises(ValueError) as caught:
                    triage.require_fetched(store, 'test')
                self.assertIn('state-exchange.json', str(caught.exception))
                self.assertIn('ready phase', str(caught.exception))
            (store / 'state-exchange.json').write_text(json.dumps(dict(token='test', phase='ready', setting=setting)))
            triage.require_fetched(store, 'test')

    def test_downloaded_json_refusals_name_each_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ('state.json', 'numbers.json', 'unsubscribe-failures.json', 'calls.jsonl', 'decisions.jsonl'):
                with self.subTest(name=name):
                    (folder / name).write_text('?')
                    with self.assertRaises(ValueError) as caught:
                        triage.validate_state_file(name, b'?', {'accounts': {'cedar': {}}}, folder)
                    self.assertIn(name, str(caught.exception))
                    self.assertNotRegex(str(caught.exception), r'Expecting|line 1 column')

    def test_saved_rule_file_refusals_name_file(self):
        config = {'accounts': {'cedar': {}}}
        cases = {
            'rules.toml': ('[[rules]]\nid="bad"\naccounts=["cedar"]\nsender="x@example.org"\naction="trash"\n', 'exclusions'),
            'never-touch.toml': ('[[rules]]\naccounts=["other"]\nsender="x@example.org"\n', 'unconfigured account'),
        }
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name, (text, reason) in cases.items():
                with self.subTest(name=name):
                    with self.assertRaises(ValueError) as caught:
                        triage.validate_state_file(name, text.encode(), config, folder)
                    self.assertIn(str(folder / name), str(caught.exception))
                    self.assertIn(reason, str(caught.exception))
