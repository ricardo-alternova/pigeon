import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import execute
import triage
from mock_provider import MockProvider


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        self.token = triage.acquire(self.store)
        (self.store / 'config.toml').write_text('[accounts.cedar]\n')
        (self.store / 'rules.toml').write_text('[[rules]]\nid="r"\naccounts=["cedar"]\nsubject="move"\naction="move"\ntarget="Reading"\n[[rules]]\nid="t"\naccounts=["cedar"]\nsubject="trash"\naction="trash"\nexclusions=[]\n')
        (self.store / 'never-touch.toml').write_text('allow_empty=true\n')
        self.listing = dict(accounts={'cedar': 100}, messages=[dict(account='cedar', id=str(i), folder='Inbox', sender='a@example.net', subject='move' if i < 3 else 'trash') for i in range(4)], folders={'cedar': [dict(id='reading', name='Reading')]})
        execute.atomic(self.store / 'inbox.json', self.listing)
        self.plan = triage.build_plan(self.listing, triage.parse_rules((self.store / 'rules.toml').read_text()), [], {'accounts': {'cedar': {}}})
        self.plan['first_run'] = True
        self.path = self.store / 'plan.json'
        execute.atomic(self.path, self.plan)

    def machine(self):
        return execute.Execution(self.store, self.token, self.path)

    def test_pending_and_crash_results(self):
        self.machine().step()
        with self.assertRaisesRegex(ValueError, 'result before'):
            self.machine().step()
        self.machine().step(MockProvider(['0']))
        self.machine().step()
        self.assertEqual(len(execute.read(self.store / 'results.json')['results']), 1)
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 3})

    def test_verification_retained_id_no_decrement(self):
        for _ in range(3):
            call = self.machine().step()
            self.machine().step(MockProvider([call['id']]))
        self.machine().step()
        self.assertEqual(self.machine().step(MockProvider(['3'], remove=False)), {'status': 'unexecuted'})
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 1})

    def test_two_failures_other_verb_continues(self):
        for _ in range(2):
            self.assertEqual(self.machine().step()['action'], 'move')
            self.machine().step(MockProvider([], outcome='error'))
        self.assertEqual(self.machine().step()['action'], 'trash')
        self.assertEqual(execute.read(self.store / 'results.json')['results'][2]['status'], 'skipped')

    def test_tampering_refused_without_provider_calls(self):
        for key, value in [('action', 'send'), ('id', 'absent'), ('account', 'marble'), ('target', 'other')]:
            plan = json.loads(json.dumps(self.plan))
            plan['actions'][0][key] = value
            execute.atomic(self.path, plan)
            provider = MockProvider([])
            with self.assertRaises(ValueError):
                self.machine().step()
                self.machine().step(provider)
            self.assertEqual(provider.calls, [])

    def test_lock_lost_pending_outcome_unknown(self):
        self.machine().step()
        triage.release(self.store, self.token)
        output = self.machine().step()
        self.assertTrue(output['done'])
        self.assertEqual(len(output['remaining']), 4)
        self.assertEqual(output['remaining'][0]['status'], 'issued_unknown')
        self.assertTrue(all(a['status'] == 'unexecuted' for a in output['remaining'][1:]))
        self.assertIn(triage.safe_text('issued_unknown'), (self.store / 'execution-report.md').read_text())

    def test_stopped(self):
        self.plan['reasons'] = ['Stopped']
        execute.atomic(self.path, self.plan)
        self.assertEqual(self.machine().step(), {'done': True})

    def test_observed_report_escaped(self):
        self.machine().step()
        env = dict(os.environ, PIGEON_HOME=str(self.store))
        subprocess.run(['python3', str(Path(execute.__file__)), 'observed', '--token', self.token, '--account', 'cedar', '--id', '<unsafe>\n', '--verb', 'extra'], env=env, check=True, capture_output=True)
        report = (self.store / 'execution-report.md').read_text()
        self.assertIn('**Unplanned calls**', report)
        self.assertIn('extra', report)
        self.assertNotIn('<unsafe>', report)
        self.assertEqual([json.loads(s)['sequence'] for s in (self.store / 'calls.jsonl').read_text().splitlines()], [1, 2])

    def test_commit_refuses_unrecorded_results_and_edited_counts(self):
        fake = self.store / 'fake-results.json'
        execute.atomic(fake, {'results': [dict(account='cedar', id='0', action='move', status='executed')]})
        with self.assertRaises(ValueError):
            triage.commit_state(self.store, self.token, self.path, fake)
        self.plan['counts']['cedar'] = 999
        execute.atomic(self.path, self.plan)
        with self.assertRaises(ValueError):
            triage.commit_state(self.store, self.token, self.path)

    def test_missing_state_cannot_bypass_first_run(self):
        self.plan.pop('first_run')
        execute.atomic(self.path, self.plan)
        self.assertFalse((self.store / 'state.json').exists())
        with self.assertRaises(ValueError):
            self.machine().step()

    def test_summary_before_step_ignores_previous_run_results(self):
        execute.atomic(self.store / 'results.json', {'results': [dict(account='cedar', id='old', action='trash', status='executed')]})
        execute.render_summary(self.store, self.token)
        report = (self.store / 'execution-report.md').read_text()
        self.assertNotIn('trash: 1', report)
        self.assertIn('No execution step', report)

    def test_pending_commit_does_not_decrement_or_write_after_lock_loss(self):
        self.machine().step()
        machine = self.machine()
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 4})
        before = {p: p.read_bytes() for p in self.store.iterdir() if p.is_file() and p.name != 'run.lock'}
        held = triage.read_lock(self.store)
        held['created'] -= 2701
        execute.atomic(self.store / 'run.lock', held)
        result = machine.step(MockProvider([]))
        self.assertEqual(result['remaining'][0]['status'], 'issued_unknown')
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_skipped_result_logged(self):
        self.test_two_failures_other_verb_continues()
        entries = [json.loads(line) for line in (self.store / 'calls.jsonl').read_text().splitlines()]
        self.assertTrue(any(e['id'] == '2' and e['outcome'] == 'skipped' for e in entries))

    def test_unplanned_calls_filtered_by_token(self):
        execute.append_log(self.store, 'other-run', dict(account='cedar', id='foreign-marker', action='extra'), 'unplanned')
        execute.append_log(self.store, self.token, dict(account='cedar', id='current-marker', action='extra'), 'unplanned')
        execute.render_summary(self.store, self.token)
        report = (self.store / 'execution-report.md').read_text()
        self.assertIn('current-marker', report)
        self.assertNotIn('foreign-marker', report)

    def test_impossible_observation_refused(self):
        self.machine().step()
        for ids in ([], ['foreign'], ['1', '1', '2', '3']):
            with self.assertRaisesRegex(ValueError, 'observation.inbox_ids'):
                self.machine().step(execute.AssistantProvider(dict(outcome='ok', inbox_ids=ids)))
        self.assertEqual(self.machine().step(execute.AssistantProvider(dict(outcome='ok', inbox_ids=['1', '2', '3']))), {'status': 'executed'})

    def test_new_lock_removes_stale_execution_report(self):
        (self.store / 'execution-report.md').write_text('previous run')
        triage.release(self.store, self.token)
        self.token = triage.acquire(self.store)
        self.assertFalse((self.store / 'execution-report.md').exists())

    def test_malformed_observation_json_is_named_and_correctable(self):
        self.machine().step()
        def result(observation):
            return subprocess.run(['python3', str(Path(execute.__file__)), 'result', '--token', self.token,
                                   '--plan', str(self.path), '--observation', observation],
                                  env=dict(os.environ, PIGEON_HOME=str(self.store)), capture_output=True, text=True)
        refused = result('{outcome:ok}')
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue(refused.stderr.startswith('Refused: observation:'), refused.stderr)
        accepted = result(json.dumps(dict(outcome='ok', inbox_ids=['1', '2', '3'])))
        self.assertEqual(json.loads(accepted.stdout), {'status': 'executed'})

    def test_observed_refuses_malformed_checkpoint_before_logging(self):
        machine = self.machine()
        machine.step()
        state = execute.read(machine.path)
        state['typo'] = True
        execute.atomic(machine.path, state)
        before = (self.store / 'calls.jsonl').read_bytes()
        result = subprocess.run(['python3', str(Path(execute.__file__)), 'observed',
                                 '--token', self.token, '--account', 'cedar',
                                 '--id', '0', '--verb', 'extra'],
                                env=dict(os.environ, PIGEON_HOME=str(self.store)),
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('typo', result.stderr)
        self.assertEqual((self.store / 'calls.jsonl').read_bytes(), before)
