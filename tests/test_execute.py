import json
from datetime import datetime, timezone
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

    def test_long_logs_keep_current_run_until_next_acquisition(self):
        line = json.dumps(dict(sequence=1, time=0, run='old', account='cedar',
                               id='old', verb='trash', outcome='issued')) + '\n'
        (self.store / 'calls.jsonl').write_text(line * 3000)
        for name in ('history.log', 'decisions.jsonl'):
            (self.store / name).write_text(('old-' + name + '\n') * 20000)
        execute.append_log(self.store, self.token, dict(account='cedar', id='extra', action='extra'), 'unplanned')
        while not self.machine().step().get('done'):
            self.machine().step(MockProvider([]))
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertIn('extra', (self.store / 'execution-report.md').read_text())
        self.assertGreater((self.store / 'calls.jsonl').stat().st_size, 262144)
        triage.release(self.store, self.token)
        triage.acquire(self.store)
        for name in triage.LOG_NAMES:
            raw = (self.store / name).read_bytes()
            self.assertLessEqual(len(raw), 262144)
            self.assertTrue(raw.endswith(b'\n'))
        calls = [json.loads(line) for line in (self.store / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls[-1]['run'], self.token)
        self.assertEqual(calls[-1]['outcome'], 'executed')

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
        self.assertTrue(refused.stderr.startswith('Refused: --observation:'), refused.stderr)
        self.assertNotRegex(refused.stderr, r'Expecting|line 1 column')
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


    def test_question_has_no_call_or_state_decrement(self):
        (self.store / 'rules.toml').write_text(
            '[[rules]]\nid="q"\naccounts=["cedar"]\nsubject="move|trash"\n'
            'action="report"\nquestion="Keep this?"\n')
        rules = triage.parse_rules((self.store / 'rules.toml').read_text())
        self.plan = triage.build_plan(self.listing, rules, [], {'accounts': {'cedar': {}}})
        self.plan['first_run'] = True
        execute.atomic(self.path, self.plan)
        self.assertEqual(self.machine().step(), {'done': True})
        self.assertFalse((self.store / 'calls.jsonl').exists())
        self.assertEqual(execute.read(self.store / 'results.json'), {'results': []})
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 4})

    def dated(self, *, existing=False, count=2, limit=None):
        text = ('[[rules]]\nid="dated"\naccounts=["cedar"]\nsubject="move"\n'
                'action="move"\ntarget="Bills/{year}/{month}"\n')
        if limit is not None:
            text += 'limit=' + str(limit) + '\n'
        (self.store / 'rules.toml').write_text(text)
        self.listing['messages'] = [dict(account='cedar', id=str(i), folder='Inbox',
                                        sender='billing@example.org', subject='move',
                                        date='2026-03-31T23:00:00-05:00') for i in range(count)]
        self.listing['folders'] = {'cedar': [dict(id='present', name='Bills/2026/03')] if existing else []}
        execute.atomic(self.store / 'inbox.json', self.listing)
        self.plan = triage.build_plan(self.listing, triage.parse_rules(text), [], {'accounts': {'cedar': {}}})
        self.plan['first_run'] = True
        execute.atomic(self.path, self.plan)

    def test_dated_create_once_returned_id_and_commit(self):
        self.dated()
        self.assertEqual([a['action'] for a in self.plan['actions']], ['create-folder', 'move', 'move'])
        call = self.machine().step()
        self.assertEqual(call['target_path'], 'Bills/2026/03')
        self.machine().step(execute.AssistantProvider(dict(outcome='ok', folder_id='new-folder', inbox_ids=['0', '1'])))
        for i in range(2):
            self.assertEqual(self.machine().step()['target'], 'new-folder')
            self.machine().step(execute.AssistantProvider(dict(outcome='ok', inbox_ids=['1'] if i == 0 else [])))
        self.assertEqual(self.machine().step(), {'done': True})
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 0})

    def test_dated_rule_paths_share_creation(self):
        for second, expected in [('bILLS/{year}/{month}', 1), ('Other/{year}/{month}', 2)]:
            with self.subTest(second=second):
                self.dated()
                rules = triage.parse_rules((self.store / 'rules.toml').read_text())
                rules[0]['subject'] = '^first$'
                rules.append(rules[0] | {'id': 'second', 'subject': '^second$', 'target': second})
                # Reverse message order to prove spelling follows file order.
                for message, subject in zip(self.listing['messages'], ('second', 'first')):
                    message['subject'] = subject
                plan = triage.build_plan(self.listing, rules, [], {'accounts': {'cedar': {}}})
                creates = [a for a in plan['actions'] if a['action'] == 'create-folder']
                self.assertEqual(len(creates), expected)
                if expected == 1:
                    self.assertEqual({a['target_path'] for a in plan['actions']}, {'Bills/2026/03'})

    def test_dated_existing_avoids_creation(self):
        self.dated(existing=True)
        self.assertEqual(self.machine().step()['target'], 'present')
        self.assertTrue(all(a['action'] == 'move' for a in self.plan['actions']))

    def test_dated_bad_dates_stay(self):
        for value in (None, 'bad', '2026-02-30'):
            self.dated()
            for m in self.listing['messages']:
                if value is None:
                    m.pop('date')
                else:
                    m['date'] = value
            plan = triage.build_plan(self.listing, triage.parse_rules((self.store / 'rules.toml').read_text()), [], {'accounts': {'cedar': {}}})
            self.assertEqual(plan['actions'], [])
            self.assertTrue(all('date' in d['reason'] for d in plan['decisions']))

    def test_creation_failure_keeps_dependents(self):
        self.dated()
        self.machine().step()
        self.machine().step(execute.AssistantProvider(dict(outcome='error', inbox_ids=['0', '1'])))
        self.assertEqual(self.machine().step(), {'done': True})
        self.assertEqual([r['status'] for r in self.machine().state['results']], ['failed', 'unexecuted', 'unexecuted'])
        self.assertIn('Bills/2026/03', (self.store / 'execution-report.md').read_text())
        summary = (self.store / 'execution-report.md').read_text()
        incomplete = summary.split('**Actions not completed**')[1].split('**Folders')[0]
        self.assertIn('failed', incomplete)
        self.assertEqual(incomplete.count('unexecuted'), 2)
        self.assertIn('create-folder', incomplete)
        self.assertEqual(incomplete.count('move'), 2)
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 2})

    def test_creation_unknown_keeps_dependents(self):
        self.dated()
        self.machine().step()
        self.machine().step(execute.AssistantProvider(dict(outcome='unknown', inbox_ids=['0', '1'])))
        self.assertEqual(self.machine().step(), {'done': True})
        self.assertEqual([r['status'] for r in self.machine().state['results']], ['issued_unknown', 'unexecuted', 'unexecuted'])
        self.assertIn('create it if absent', (self.store / 'execution-report.md').read_text())

    def test_creation_observation_is_strict(self):
        self.dated()
        self.machine().step()
        for obs in (dict(outcome='ok', inbox_ids=['0', '1']),
                    dict(outcome='ok', folder_id='', inbox_ids=['0', '1']),
                    dict(outcome='error', folder_id='x', inbox_ids=['0', '1']),
                    dict(outcome='ok', folder_id='x', inbox_ids=['1'])):
            with self.assertRaises(ValueError):
                self.machine().step(execute.AssistantProvider(obs))

    def test_folder_paths_refuse_hostile_and_unknown_placeholders(self):
        self.dated()
        rule = triage.parse_rules((self.store / 'rules.toml').read_text())[0]
        for target in ('Bills/{day}', 'a/../b', '/a', 'a//b', 'a/./b', 'a\nb', 'a\\b', 'x' * 256, '/'.join(['a'] * 9)):
            with self.assertRaisesRegex(ValueError, 'target'):
                triage.validate_rules([rule | {'target': target}])

    def test_folder_path_report_escaped(self):
        self.dated()
        rules = triage.parse_rules((self.store / 'rules.toml').read_text())
        rules[0]['target'] = 'Mail.Box/<Bills>_[x]`/{year}/{month}'
        plan = triage.build_plan(self.listing, rules, [], {'accounts': {'cedar': {}}})
        report = triage.render_report(plan, self.listing)
        path = 'Mail.Box/<Bills>_[x]`/2026/03'
        self.assertIn(triage.code_path(path), report)
        self.assertNotIn('[dot]', report)
        (self.store / 'rules.toml').write_text((self.store / 'rules.toml').read_text().replace('Bills/{year}/{month}', rules[0]['target']))
        self.plan = plan | {'first_run': True}
        execute.atomic(self.path, self.plan)
        self.machine().step()
        self.machine().step(execute.AssistantProvider(dict(outcome='error', inbox_ids=['0', '1'])))
        summary = (self.store / 'execution-report.md').read_text()
        self.assertIn('cedar: Create this folder: ' + triage.code_path(path), summary)
        self.assertNotIn('[dot]', summary)
        self.assertEqual(triage.code_path(path), '`` ' + path + ' ``')

    def test_rule_limit_and_global_brake(self):
        self.dated(existing=True, count=11)
        self.assertEqual(self.plan['actions'], [])
        self.dated(existing=True, count=11, limit=12)
        self.assertEqual(len(self.plan['actions']), 11)
        self.listing['accounts']['cedar'] = 20
        plan = triage.build_plan(self.listing, triage.parse_rules((self.store / 'rules.toml').read_text()), [], {'accounts': {'cedar': {}}})
        self.assertEqual(plan['actions'], [])
        self.assertTrue(plan['reasons'])

    def test_rule_limit_invalid(self):
        self.dated()
        rule = triage.parse_rules((self.store / 'rules.toml').read_text())[0]
        for limit in (51, 0, -1, True, '12', 1.5):
            with self.assertRaisesRegex(ValueError, 'limit'):
                triage.validate_rules([rule | {'limit': limit}])

    def test_create_two_failure_brake_and_lock(self):
        self.dated(count=3)
        for i, message in enumerate(self.listing['messages']):
            message['date'] = '2026-0' + str(i + 1) + '-01'
        execute.atomic(self.store / 'inbox.json', self.listing)
        self.plan = triage.build_plan(self.listing, triage.parse_rules((self.store / 'rules.toml').read_text()), [], {'accounts': {'cedar': {}}})
        self.plan['first_run'] = True
        execute.atomic(self.path, self.plan)
        for _ in range(2):
            self.assertEqual(self.machine().step()['action'], 'create-folder')
            self.machine().step(execute.AssistantProvider(dict(outcome='error', inbox_ids=['0', '1', '2'])))
        self.assertEqual(self.machine().step(), {'done': True})
        self.assertEqual([r['status'] for r in self.machine().state['results']],
                         ['failed', 'unexecuted', 'failed', 'unexecuted', 'skipped', 'unexecuted'])

    def test_create_pending_lock_loss_no_move(self):
        self.dated()
        self.machine().step()
        triage.release(self.store, self.token)
        result = self.machine().step()
        self.assertIn('stop', result)
        self.assertEqual(result['remaining'][0]['status'], 'issued_unknown')
        self.assertTrue(all(a['status'] == 'unexecuted' for a in result['remaining'][1:]))

    def rebuild(self):
        execute.atomic(self.store / 'inbox.json', self.listing)
        self.plan = triage.build_plan(self.listing, triage.parse_rules((self.store / 'rules.toml').read_text()), [], {'accounts': {'cedar': {}}})
        self.plan['first_run'] = True
        execute.atomic(self.path, self.plan)
        return self.plan

    def test_missing_folder_metadata_never_creates(self):
        for folders in (None, {}):
            self.dated()
            if folders is None:
                self.listing.pop('folders')
            else:
                self.listing['folders'] = folders
            plan = self.rebuild()
            self.assertEqual(plan['actions'], [])
            self.assertTrue(all('lacked folder information' in d['reason'] for d in plan['decisions']))
            self.assertIn('lacked folder information', triage.render_report(plan, self.listing))

    def test_leaf_folder_metadata_refused(self):
        self.dated()
        self.listing['folders']['cedar'] = [dict(id='leaf', name='03')]
        with self.assertRaisesRegex(ValueError, r'Inbox.folders.cedar.*full path'):
            self.rebuild()

    def test_listed_full_path_wins_over_same_leaf(self):
        self.dated(existing=True)
        self.listing['folders']['cedar'].append(dict(id='top', name='03'))
        plan = self.rebuild()
        self.assertTrue(all(a['target'] == 'present' for a in plan['actions']))

    def test_folder_comparison_casefold(self):
        self.dated(existing=True)
        self.listing['folders']['cedar'][0]['name'] = 'bILLS/2026/03'
        plan = self.rebuild()
        self.assertEqual([a['action'] for a in plan['actions']], ['move', 'move'])
        self.assertTrue(all(a['target'] == 'present' for a in plan['actions']))

    def test_year_range(self):
        for year in (1, 1989, 2101, 9999):
            self.dated()
            for m in self.listing['messages']:
                m['date'] = f'{year:04d}-01-01'
            plan = self.rebuild()
            self.assertEqual(plan['actions'], [])
            self.assertTrue(all('date' in d['reason'] for d in plan['decisions']))
        for year in (1990, 2100):
            for m in self.listing['messages']:
                m['date'] = f'{year}-01-01'
            self.assertEqual(len(self.rebuild()['actions']), 3)

    def test_unicode_path_separators_refused(self):
        for char in ('\u2028', '\u2029', '\u2215', '\u2044', '\uff0f', '\u29f8', '\uff3c', '\ufe68', '\u2571', '\u27cb', '\u29f5', '\u2572', '\u27cd'):
            with self.subTest(char=char), self.assertRaisesRegex(ValueError, 'target'):
                triage.folder_path('Bills/' + char + '2026')

    def test_creation_does_not_consume_rule_limit(self):
        self.dated(count=2, limit=2)
        self.assertEqual(self.plan['reasons'], [])
        self.assertEqual(len(self.plan['actions']), 3)

    def test_creation_not_handled_mail(self):
        self.dated()
        self.machine().step()
        self.machine().step(execute.AssistantProvider(dict(outcome='ok', folder_id='new', inbox_ids=['0', '1'])))
        report = (self.store / 'execution-report.md').read_text()
        self.assertNotIn('create-folder: 1', report)

    def test_move_observation_unknown_or_folder_id_refused(self):
        self.dated(existing=True)
        self.machine().step()
        for extra in (dict(outcome='unknown'), dict(outcome='ok', folder_id='new')):
            with self.assertRaisesRegex(ValueError, 'create-folder only'):
                self.machine().step(execute.AssistantProvider(dict(inbox_ids=['1'], **extra)))
        self.assertEqual(self.machine().state['results'], [])

    def test_created_id_observation_and_checkpoint(self):
        self.dated()
        self.listing['folders'] = {'cedar': [dict(id='known', name='Trash')]}
        self.rebuild()
        machine = self.machine()
        machine.step()
        pending_state = execute.read(machine.path)
        for value in (None, '', 'known', 'x' * 513, 'a\nb', 'a\u200db', 'a\u2028b'):
            with self.subTest(value=value):
                obs = dict(outcome='ok', inbox_ids=['0', '1'])
                if value is not None:
                    obs['folder_id'] = value
                with self.assertRaisesRegex(ValueError, 'folder_id'):
                    self.machine().step(execute.AssistantProvider(obs))
                state = json.loads(json.dumps(pending_state))
                state['pending'] = None
                entry = {k: machine.plan['actions'][0][k] for k in ('account', 'id', 'action')}
                entry['status'] = 'executed'
                if value is not None:
                    entry['folder_id'] = value
                state['results'] = [entry]
                execute.atomic(machine.path, state)
                with self.assertRaisesRegex(ValueError, 'folder_id'):
                    self.machine()
                execute.atomic(machine.path, pending_state)
        self.machine().step(execute.AssistantProvider(dict(outcome='ok', folder_id='new', inbox_ids=['0', '1'])))
        self.machine().step()
        self.machine().step(execute.AssistantProvider(dict(outcome='ok', inbox_ids=['1'])))
        state = execute.read(machine.path)
        state['results'][1]['folder_id'] = 'unexpected'
        execute.atomic(machine.path, state)
        with self.assertRaisesRegex(ValueError, 'folder_id'):
            self.machine()

    def test_created_ids_unique_across_accounts_and_creations(self):
        self.dated(count=2)
        self.listing['folders']['other'] = [dict(id='other-known', name='Trash')]
        # The config must declare the other account even when it is not listed.
        (self.store / 'config.toml').write_text('[accounts.cedar]\n[accounts.other]\n')
        self.listing['messages'][1]['date'] = '2026-04-01'
        execute.atomic(self.store / 'inbox.json', self.listing)
        self.plan = triage.build_plan(self.listing, triage.parse_rules((self.store / 'rules.toml').read_text()), [], {'accounts': {'cedar': {}, 'other': {}}})
        self.plan['first_run'] = True
        execute.atomic(self.path, self.plan)
        self.machine().step()
        with self.assertRaisesRegex(ValueError, 'folder_id'):
            self.machine().step(execute.AssistantProvider(dict(outcome='ok', folder_id='other-known', inbox_ids=['0', '1'])))
        self.machine().step(execute.AssistantProvider(dict(outcome='ok', folder_id='new', inbox_ids=['0', '1'])))
        self.machine().step()
        self.machine().step(execute.AssistantProvider(dict(outcome='ok', inbox_ids=['1'])))
        self.machine().step()
        with self.assertRaisesRegex(ValueError, 'folder_id'):
            self.machine().step(execute.AssistantProvider(dict(outcome='ok', folder_id='new', inbox_ids=['1'])))
        machine = self.machine()
        machine.state['pending'] = None
        machine.state['results'].append(dict(account='cedar', id='1', action='create-folder', status='executed', folder_id='new'))
        execute.atomic(machine.path, machine.state)
        with self.assertRaisesRegex(ValueError, 'folder_id'):
            self.machine()

    def test_commit_positions_only_from_pre_listing_observation(self):
        observation = self.store / 'positions-observation.json'
        execute.atomic(observation, {'cedar': dict(outcome='ok', ids=['fresh'])})
        execute.atomic(self.store / 'quiet-observation.json', {'cedar': dict(outcome='ok', ids=['stale'])})
        triage.quiet_check(self.store, dict(accounts={'cedar': {}}), {}, self.store / 'quiet-observation.json', False, datetime.now(timezone.utc))
        for _ in range(4):
            call = self.machine().step()
            self.machine().step(MockProvider([call['id']]))
        self.machine().step()
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 0, '_positions': {'cedar': ['stale']}})

    def test_pre_listing_positions_see_later_arrivals_both_kinds(self):
        import tomllib
        for kind in ('history', 'ids'):
            for arrived in (False, True):
                with self.subTest(kind=kind, arrived=arrived), tempfile.TemporaryDirectory() as folder:
                    store = Path(folder)
                    token = triage.acquire(store)
                    (store / 'config.toml').write_text('[schedule]\nweekdays=["00:00"]\nweekends=["00:00"]\nalways_full=[]\n[accounts.cedar]\nquiet_kind="'+kind+'"\n')
                    (store / 'rules.toml').write_text('rules=[]\n')
                    (store / 'never-touch.toml').write_text('allow_empty=true\n')
                    config = tomllib.loads((store / 'config.toml').read_text())
                    before = dict(outcome='ok', position='100', arrivals=[]) if kind == 'history' else dict(outcome='ok', ids=[])
                    after = dict(outcome='ok', position='101', arrivals=['late']) if kind == 'history' else dict(outcome='ok', ids=['late'])
                    obs = store / 'quiet-observation.json'
                    execute.atomic(obs, {'cedar': before})
                    now = datetime.fromisoformat('2026-11-11T00:01:00+00:00')
                    triage.quiet_check(store, config, {}, obs, True, now)
                    listing = dict(accounts={'cedar': 100}, messages=[])
                    execute.atomic(store / 'inbox.json', listing)
                    plan = triage.build_plan(listing, [], [], config, {})
                    plan['first_run'] = True
                    path = store / 'plan.json'
                    execute.atomic(path, plan)
                    execute.Execution(store, token, path).step()
                    # Overwriting the gateway file cannot advance the saved snapshot.
                    execute.atomic(obs, {'cedar': after if arrived else before})
                    triage.commit_state(store, token, path, store / 'results.json')
                    previous = execute.read(store / 'state.json')
                    self.assertEqual(previous['_positions']['cedar'], '100' if kind == 'history' else [])
                    triage.release(store, token)
                    triage.acquire(store)
                    answer = triage.quiet_check(store, config, previous, obs, True, now)
                    self.assertEqual(answer['decision'], 'full' if arrived else 'skip')

    def test_previous_run_snapshot_cannot_seed_positions(self):
        obs = self.store / 'quiet-observation.json'
        execute.atomic(obs, {'cedar': dict(outcome='ok', ids=[])})
        triage.quiet_check(self.store, dict(accounts={'cedar': {}}), {}, obs, False, datetime.now(timezone.utc))
        triage.release(self.store, self.token)
        self.token = triage.acquire(self.store)
        for _ in range(4):
            call = self.machine().step()
            self.machine().step(MockProvider([call['id']]))
        self.machine().step()
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertNotIn('_positions', execute.read(self.store / 'state.json'))

    def test_corrupted_snapshot_positions_are_refused(self):
        obs = self.store / 'quiet-observation.json'
        execute.atomic(obs, {'cedar': dict(outcome='ok', ids=[])})
        triage.quiet_check(self.store, dict(accounts={'cedar': {}}), {}, obs, False, datetime.now(timezone.utc))
        cache = self.store / 'quiet-check.json'
        execute.atomic(cache, execute.read(cache) | {'positions': {'cedar': ['bad/id']}})
        for _ in range(4):
            call = self.machine().step()
            self.machine().step(MockProvider([call['id']]))
        self.machine().step()
        with self.assertRaisesRegex(ValueError, '_positions'):
            triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')

    def test_commit_missing_observation_removes_positions_keeps_counts(self):
        execute.atomic(self.store / 'state.json', {'cedar': 4, '_positions': {'cedar': ['old']}})
        triage.commit_state(self.store, self.token, self.path)
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 4})

    def test_commit_invalid_observation_removes_positions_keeps_counts(self):
        execute.atomic(self.store / 'state.json', {'cedar': 4, '_positions': {'cedar': ['old']}})
        observation = self.store / 'positions-observation.json'
        observation.write_text('broken')
        triage.quiet_check(self.store, dict(accounts={'cedar': {}}), {}, observation, False, datetime.now(timezone.utc))
        triage.commit_state(self.store, self.token, self.path)
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 4})

    def test_incomplete_execution_cannot_save_positions(self):
        observation = self.store / 'positions-observation.json'
        execute.atomic(observation, {'cedar': dict(outcome='ok', ids=['fresh'])})
        triage.quiet_check(self.store, dict(accounts={'cedar': {}}), {}, observation, False, datetime.now(timezone.utc))
        self.machine().step()
        triage.commit_state(self.store, self.token, self.path)
        self.assertNotIn('_positions', execute.read(self.store / 'state.json'))

    def test_stopped_run_cannot_save_positions(self):
        observation = self.store / 'positions-observation.json'
        execute.atomic(observation, {'cedar': dict(outcome='ok', ids=['fresh'])})
        triage.quiet_check(self.store, dict(accounts={'cedar': {}}), {}, observation, False, datetime.now(timezone.utc))
        (self.store / 'config.toml').write_text('inbox_limit=1\n[accounts.cedar]\n')
        self.plan = triage.build_plan(self.listing, triage.parse_rules((self.store / 'rules.toml').read_text()), [], dict(inbox_limit=1, accounts={'cedar': {}}))
        self.plan['first_run'] = True
        execute.atomic(self.path, self.plan)
        self.machine().step()
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertNotIn('_positions', execute.read(self.store / 'state.json'))

    def test_history_position_saved_after_full_execution(self):
        (self.store / 'config.toml').write_text('[accounts.cedar]\nquiet_kind="history"\n')
        observation = self.store / 'positions-observation.json'
        execute.atomic(observation, {'cedar': dict(outcome='ok', position='200', arrivals=['arrived'])})
        triage.quiet_check(self.store, dict(accounts={'cedar': {'quiet_kind': 'history'}}), {}, observation, False, datetime.now(timezone.utc))
        for _ in range(4):
            call = self.machine().step()
            self.machine().step(MockProvider([call['id']]))
        self.machine().step()
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 0, '_positions': {'cedar': '200'}})

    def test_missing_account_cannot_seed_positions(self):
        (self.store / 'config.toml').write_text('[accounts.cedar]\n[accounts.marble]\n')
        for _ in range(4):
            call = self.machine().step()
            self.machine().step(MockProvider([call['id']]))
        self.machine().step()
        observation = self.store / 'positions-observation.json'
        execute.atomic(observation, {'cedar': dict(outcome='ok', ids=[]), 'marble': dict(outcome='ok', ids=[])})
        triage.quiet_check(self.store, dict(accounts={'cedar': {}, 'marble': {}}), {}, observation, False, datetime.now(timezone.utc))
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertNotIn('_positions', execute.read(self.store / 'state.json'))

    def test_no_checkpoint_cannot_seed_positions(self):
        (self.store / 'rules.toml').write_text('rules=[]\n')
        self.plan = triage.build_plan(self.listing, [], [], dict(accounts={'cedar': {}}))
        self.plan['first_run'] = True
        execute.atomic(self.path, self.plan)
        observation = self.store / 'positions-observation.json'
        execute.atomic(observation, {'cedar': dict(outcome='ok', ids=['fresh'])})
        triage.quiet_check(self.store, dict(accounts={'cedar': {}}), {}, observation, False, datetime.now(timezone.utc))
        triage.commit_state(self.store, self.token, self.path)
        self.assertNotIn('_positions', execute.read(self.store / 'state.json'))

    def test_partial_results_cannot_seed_positions(self):
        call = self.machine().step()
        self.machine().step(MockProvider([call['id']]))
        observation = self.store / 'positions-observation.json'
        execute.atomic(observation, {'cedar': dict(outcome='ok', ids=['fresh'])})
        triage.quiet_check(self.store, dict(accounts={'cedar': {}}), {}, observation, False, datetime.now(timezone.utc))
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertNotIn('_positions', execute.read(self.store / 'state.json'))


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        self.config = {'accounts': {'cedar': {}}}
        self.listing = dict(accounts={'cedar': 100}, messages=[dict(account='cedar', id=str(i), folder='Inbox', sender='a@example.net', subject='Question '+str(i), date='2026-11-11T00:00:00+00:00') for i in range(3)], folders={'cedar': [dict(id='billing', name='Billing')]})
        for name, text in [('config.toml', '[accounts.cedar]\n'), ('rules.toml', 'rules=[]\n'), ('never-touch.toml', 'allow_empty=true\n'), ('guardrails.md', 'fixture policy\n')]:
            (self.store/name).write_text(text)
        execute.atomic(self.store/'state.json', {'cedar': 3})
        execute.atomic(self.store/'inbox.json', self.listing)
        self.token = triage.acquire(self.store)
        self.path = self.store/'plan.json'
        self.number()

    def number(self):
        plan = triage.build_plan(self.listing, [], [], self.config, {'cedar': 3})
        triage.number_decisions(self.store, plan, self.listing)
        return plan

    def reply(self, text, scheduled=False):
        import contextlib, io
        from unittest.mock import patch
        (self.store/'reply.txt').write_text(text)
        output = io.StringIO()
        output.reconfigure = lambda **kw: None
        with patch.dict(os.environ, PIGEON_HOME=str(self.store)), contextlib.redirect_stdout(output):
            triage.main(['reply', '--token', self.token, '--inbox', str(self.store/'inbox.json'), '--reply', str(self.store/'reply.txt'), '--plan-out', str(self.path), '--report-out', str(self.store/'report.md')] + (['--scheduled'] if scheduled else []))
        self.output = output.getvalue()
        return execute.read(self.path)

    def test_reply_cannot_advance_quiet_position(self):
        obs = self.store / 'quiet-observation.json'
        execute.atomic(obs, {'cedar': dict(outcome='ok', ids=['0','1','2','new'])})
        triage.quiet_check(self.store, self.config, {}, obs, False, datetime.now(timezone.utc))
        self.reply('1 leave')
        execute.Execution(self.store, self.token, self.path).step()
        triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        self.assertEqual(execute.read(self.store / 'state.json'), {'cedar': 3})
        self.assertEqual(execute.read(self.store / 'decisions.jsonl')['parts'][0]['outcome'], 'answered')
        self.assertTrue(execute.read(self.store / 'numbers.json')['items'])

    def test_reply_keeps_counterfactual_quiet_answer(self):
        for arrived in (False, True):
            with self.subTest(arrived=arrived):
                self.setUp()
                self.config['schedule'] = dict(weekdays=['00:00'], weekends=['00:00'], always_full=[])
                (self.store / 'config.toml').write_text('[schedule]\nweekdays=["00:00"]\nweekends=["00:00"]\nalways_full=[]\n[accounts.cedar]\n')
                previous = {'cedar': 3, '_positions': {'cedar': ['0','1','2']}}
                execute.atomic(self.store / 'state.json', previous)
                obs = self.store / 'quiet-observation.json'
                execute.atomic(obs, {'cedar': dict(outcome='ok', ids=['new','0','1','2'] if arrived else ['0','1','2'])})
                now = datetime.fromisoformat('2026-11-11T00:01:00+00:00')
                with tempfile.TemporaryDirectory() as folder:
                    expected = triage.quiet_check(Path(folder), self.config, previous, obs, True, now)
                self.reply('1 leave')
                execute.Execution(self.store, self.token, self.path).step()
                triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
                saved = execute.read(self.store / 'state.json')
                self.assertEqual(saved, previous)
                triage.release(self.store, self.token)
                self.token = triage.acquire(self.store)
                self.assertEqual(triage.quiet_check(self.store, self.config, saved, obs, True, now), expected)

    def refused(self, text, scheduled=False):
        triage.release(self.store, self.token)
        self.token = triage.acquire(self.store)
        plan = self.reply(text, scheduled)
        self.assertEqual(plan['actions'], [])
        self.assertTrue(plan['reasons'])
        self.assertEqual(len(self.output.splitlines()), 1)
        self.assertNotIn('Question:', self.output)
        for word in ('Type', 'trash', 'archive', 'spam', 'move', 'leave', 'yes', 'no', 'next', 'commit-state', 'send-state', 'release'):
            self.assertIn(word, self.output)
        self.assertFalse((self.store/'decisions.jsonl').exists())
        self.assertEqual(execute.Execution(self.store, self.token, self.path).step(), {'done': True})
        triage.commit_state(self.store, self.token, self.path, self.store/'results.json')
        self.assertFalse((self.store/'decisions.jsonl').exists())

    def test_every_saved_number_is_in_report(self):
        cases = [([], []), ([], [dict(sender='a@example.net')]),
                 ([dict(id='q', accounts=['cedar'], subject='Question', action='report', question='Keep it?')], [])]
        for rules, holds in cases:
            plan = triage.build_plan(self.listing, rules, holds, self.config, {'cedar': 3})
            triage.number_decisions(self.store, plan, self.listing)
            report = triage.render_report(plan, self.listing)
            if holds:
                self.assertEqual(triage.read_numbers(self.store)['items'], [])
                self.assertTrue(all('number' not in d for d in plan['decisions']))
                self.assertNotIn('1. ', report)
            for item in triage.read_numbers(self.store)['items']:
                self.assertIn(str(item['number']) + '. ', report)

    def test_automatic_item_retires_unlisted_number(self):
        rules = [dict(id='auto', accounts=['cedar'], sender='a@example.net', action='archive')]
        plan = triage.build_plan(self.listing, rules, [], self.config, {'cedar': 3})
        triage.number_decisions(self.store, plan, self.listing)
        self.assertEqual(triage.read_numbers(self.store)['items'], [])
        self.assertEqual(self.reply('1 trash')['actions'], [])
        self.assertIn('Unknown number', self.output)

    def test_answers_never_write_rules(self):
        before = (self.store/'rules.toml').read_bytes()
        for answer in ('yes', 'no'):
            triage.release(self.store, self.token)
            self.token = triage.acquire(self.store)
            self.reply('1 ' + answer)
            execute.Execution(self.store,self.token,self.path).step()
            triage.commit_state(self.store,self.token,self.path,self.store/'results.json')
            self.assertEqual((self.store/'rules.toml').read_bytes(), before)

    def test_no_reply_and_legacy_store(self):
        (self.store/'numbers.json').unlink()
        plan = self.number()
        self.assertEqual([x['number'] for x in plan['decisions']], [1,2,3])
        self.assertEqual(plan['actions'], [])
        self.assertIn('1. cedar', triage.render_report(plan, self.listing))

    def test_two_actions_only_and_batch_outcomes(self):
        from datetime import datetime
        plan = self.reply('1 trash, 3 move to Billing')
        self.assertEqual([(x['id'],x['action']) for x in plan['actions']], [('0','trash'),('2','move')])
        self.assertEqual(len(self.output.splitlines()), 2)
        for ident in ('0','2'):
            m = execute.Execution(self.store, self.token, self.path)
            self.assertEqual(m.step()['id'], ident)
            m.step(MockProvider([ident]))
        execute.Execution(self.store, self.token, self.path).step()
        now = datetime.fromisoformat('2026-11-11T12:00:00+00:00')
        triage.commit_state(self.store,self.token,self.path,self.store/'results.json',now=now)
        batches = [json.loads(x) for x in (self.store/'decisions.jsonl').read_text().splitlines()]
        self.assertEqual([x['outcome'] for x in batches[0]['parts']], ['executed','executed'])
        self.assertEqual(batches[0]['time'], now.isoformat())
        self.assertNotIn('subject', str(batches))
        self.assertEqual(execute.read(self.store/'state.json'), {'cedar': 1})

    def test_all_reply_forms_remain_valid(self):
        for verb in ('trash', 'archive', 'spam', 'move to Billing', 'leave', 'yes', 'no'):
            with self.subTest(verb=verb):
                triage.release(self.store, self.token)
                self.token = triage.acquire(self.store)
                plan = self.reply('1 ' + verb)
                self.assertEqual(plan['reasons'], [])
                self.assertEqual(len(plan['actions']), int(verb not in {'leave', 'yes', 'no'}))

    def test_mixed_case_spaces(self):
        self.assertEqual(self.reply('  1   TrAsH ; 3  MoVe   TO   Billing  ')['actions'][1]['target'], 'billing')

    def test_folder_spaces_nested_non_english(self):
        self.listing['folders']['cedar'][0]['name'] = 'Árvíztűrő mappa/日本語'
        execute.atomic(self.store/'inbox.json', self.listing)
        self.assertEqual(self.reply('3 move to Árvíztűrő mappa/日本語')['actions'][0]['target'], 'billing')

    def test_newer_report_stability_no_reuse(self):
        self.listing['messages'].pop(1)
        self.listing['messages'].append(dict(self.listing['messages'][0], id='new'))
        execute.atomic(self.store/'inbox.json', self.listing)
        self.assertEqual([x['number'] for x in self.number()['decisions']], [1,3,4])
        self.assertEqual(self.reply('3 trash')['actions'][0]['id'], '2')

    def test_non_actions_no_calls_keep_numbers(self):
        self.reply('1 leave, 2 YES, 3 no')
        self.assertEqual(execute.Execution(self.store,self.token,self.path).step(), {'done':True})
        triage.commit_state(self.store,self.token,self.path,self.store/'results.json')
        parts = json.loads((self.store/'decisions.jsonl').read_text())['parts']
        self.assertEqual([x['outcome'] for x in parts], ['answered']*3)
        self.assertFalse((self.store/'calls.jsonl').exists())
        self.assertEqual([x['number'] for x in self.number()['decisions']], [1,2,3])

    def test_empty(self): self.refused('')
    def test_unparsed_part(self): self.refused('trash 1')
    def test_unknown_verb(self):
        for verb in ['unsubscribe','send','reply','forward','delete','draft']:
            with self.subTest(verb=verb): self.refused('1 '+verb)
    def test_unknown_number(self): self.refused('99 trash')
    def test_duplicate_number(self): self.refused('1 trash, 1 archive')
    def test_stale_message(self):
        self.listing['messages'].pop(0)
        execute.atomic(self.store/'inbox.json',self.listing)
        self.refused('1 trash')
    def test_other_account(self):
        self.listing['messages'][0]['account']='marble'
        self.listing['accounts']['marble']=100
        self.config['accounts']['marble']={}
        (self.store/'config.toml').write_text('[accounts.cedar]\n[accounts.marble]\n')
        execute.atomic(self.store/'inbox.json',self.listing)
        self.refused('1 trash')
    def test_never_touch(self):
        (self.store/'never-touch.toml').write_text('[[rules]]\nsender="a@example.net"\n')
        self.refused('1 trash')
    def test_missing_folder(self): self.refused('1 move to Absent')
    def test_ambiguous_folder(self):
        self.listing['folders']['cedar'].append(dict(id='other',name='BILLING'))
        execute.atomic(self.store/'inbox.json',self.listing)
        self.refused('1 move to Billing')
    def test_ceiling(self):
        self.listing['messages']=[dict(self.listing['messages'][0],id=str(i)) for i in range(11)]
        self.number()
        execute.atomic(self.store/'inbox.json',self.listing)
        self.refused(','.join(str(i)+' trash' for i in range(1,12)))
    def test_brake(self):
        self.listing['accounts']['cedar']=3
        execute.atomic(self.store/'inbox.json',self.listing)
        self.refused('1 trash')
    def test_inbox_brake(self):
        (self.store/'config.toml').write_text('inbox_limit=1\n[accounts.cedar]\n')
        self.refused('1 trash')
    def test_scheduled(self): self.refused('1 trash',True)
    def test_hostile_inputs(self):
        for text in ['1,'*1000, '1 trash\x00', '1'*400+' trash', '1 trash,' , '1 trash'+' '*2000]:
            with self.subTest(text=text[:20]): self.refused(text)
    def test_deterministic_in_process(self):
        from unittest.mock import patch
        with patch.object(triage.subprocess,'run',side_effect=AssertionError('subprocess')), patch.object(triage.time,'time',side_effect=AssertionError('clock')):
            parts=triage.parse_reply('1 trash, 3 move to Billing')
            one=triage.reply_plan(self.store,self.listing,[],self.config,{'cedar':3},parts)
            two=triage.reply_plan(self.store,self.listing,[],self.config,{'cedar':3},parts)
        self.assertEqual(json.dumps(one),json.dumps(two))
    def test_tamper_reply(self):
        plan=self.reply('1 trash')
        plan['reply'][0]['verb']='archive'
        execute.atomic(self.path,plan)
        with self.assertRaises(ValueError): execute.Execution(self.store,self.token,self.path).step()
    def test_protected_files(self):
        import contextlib, io
        from unittest.mock import patch
        for name in ['numbers.json','reply.json','decisions.jsonl','reply.txt']:
            path=self.store/name
            original=path.read_bytes() if path.exists() else b'preserve'
            path.write_bytes(original)
            with patch.dict(os.environ,PIGEON_HOME=str(self.store)), self.assertRaises(ValueError):
                triage.main(['plan','--token',self.token,'--inbox',str(self.store/'inbox.json'),'--report-out',str(path)])
            self.assertEqual(path.read_bytes(),original)
            self.token=triage.acquire(self.store)

    def test_number_store_validation(self):
        original=triage.read_numbers(self.store)
        for changed in [dict(next=0,items=[]),dict(next=True,items=[]),dict(next=4,items='bad'),dict(next=4,items=[dict(number=4,account='cedar',id='0')]),dict(next=4,items=[dict(number=1,account=3,id='0')]),dict(next=4,items=[dict(number=1,account='cedar',id='0'),dict(number=1,account='cedar',id='1')]),dict(next=4,items=[dict(number=1,account='cedar',id='0'),dict(number=2,account='cedar',id='0')])]:
            execute.atomic(self.store/'numbers.json',changed)
            with self.assertRaises(ValueError): triage.read_numbers(self.store)
        execute.atomic(self.store/'numbers.json',original)

    def test_counter_survives_empty_store(self):
        self.listing['messages']=[]
        self.number()
        self.listing['messages']=[dict(account='cedar',id='fresh',folder='Inbox',sender='a@example.net',subject='fresh')]
        self.assertEqual(self.number()['decisions'][0]['number'],4)
        execute.atomic(self.store/'inbox.json', self.listing)
        self.refused('1 trash')

    def test_missing_account_preserves_numbers(self):
        listing=dict(accounts={'marble':10},messages=[])
        plan=triage.build_plan(listing,[],[],dict(accounts={'cedar':{},'marble':{}}))
        triage.number_decisions(self.store,plan,listing)
        self.assertEqual(len(triage.read_numbers(self.store)['items']),3)

    def test_long_decision_log_current_reply_is_not_trimmed(self):
        line = json.dumps(dict(run='old', time='2026-11-11T08:00:00+00:00', parts=[])) + '\n'
        (self.store / 'decisions.jsonl').write_text(line * 10000)
        self.reply('1 leave')
        execute.Execution(self.store, self.token, self.path).step()
        for _ in range(2):
            triage.commit_state(self.store, self.token, self.path, self.store / 'results.json')
        batches = [json.loads(line) for line in (self.store / 'decisions.jsonl').read_text().splitlines()]
        self.assertEqual(len(batches), 10001)
        self.assertEqual(batches[-1]['run'], self.token)
        self.assertEqual(batches[-1]['parts'][0]['outcome'], 'answered')

    def test_commit_no_duplicate_batch(self):
        self.reply('1 leave')
        execute.Execution(self.store,self.token,self.path).step()
        for _ in range(2): triage.commit_state(self.store,self.token,self.path,self.store/'results.json')
        self.assertEqual(len((self.store/'decisions.jsonl').read_text().splitlines()),1)

    def test_wrong_saved_token(self):
        self.reply('1 trash')
        saved=execute.read(self.store/'reply.json')
        saved['token']='other'
        execute.atomic(self.store/'reply.json',saved)
        with self.assertRaises(ValueError): execute.Execution(self.store,self.token,self.path).step()

    def test_action_target_revalidation(self):
        plan=self.reply('1 move to Billing')
        plan['actions'][0]['target']='arbitrary'
        execute.atomic(self.path,plan)
        with self.assertRaises(ValueError): execute.Execution(self.store,self.token,self.path).step()

    def test_reply_decisions_keep_saved_numbers(self):
        plan=self.reply('1 trash, 3 archive')
        self.assertEqual(plan['decisions'][0]['number'],2)
        self.assertIn('2. cedar', (self.store/'report.md').read_text())

    def test_global_ceiling(self):
        self.listing['accounts']['cedar']=1000
        self.listing['messages']=[dict(self.listing['messages'][0],id=str(i)) for i in range(51)]
        self.number()
        execute.atomic(self.store/'inbox.json',self.listing)
        self.refused(','.join(str(i)+' trash' for i in range(1,52)))

    def test_reply_requires_lock(self):
        triage.release(self.store,self.token)
        with self.assertRaises(ValueError): self.reply('1 trash')

    def test_invalid_sender(self):
        self.listing['messages'][0]['sender']='invalid'
        execute.atomic(self.store/'inbox.json',self.listing)
        self.refused('1 trash')

    def test_folder_path_guard(self): self.refused('1 move to ../Billing')

    def test_reply_escaping(self):
        self.listing['messages'][0]['subject']='[pay](https://example.org)\n*now*'
        execute.atomic(self.store/'inbox.json',self.listing)
        self.reply('1 trash')
        self.assertNotIn('https://',self.output)
        self.assertEqual(len(self.output.splitlines()),1)
        self.assertIn('\\[control\\]',self.output)

    def test_parser_refuses_before_resolution(self):
        for text in ['1 send','1 unsubscribe','1 move to ../Billing','1'*400+' trash','1\ttrash','1 trash\t','0 trash, 0 archive', '1 trash,' , '1 trash'+' '*2000]:
            with self.subTest(text=text[:30]), self.assertRaises(ValueError): triage.parse_reply(text)

    def test_never_touch_non_action(self):
        (self.store/'never-touch.toml').write_text('[[rules]]\nsender="a@example.net"\n')
        self.refused('1 leave')

    def test_custom_reply_input_protected(self):
        from unittest.mock import patch
        custom=self.store/'custom.txt'
        custom.write_text('1 trash')
        with patch.dict(os.environ,PIGEON_HOME=str(self.store)), self.assertRaises(ValueError):
            triage.main(['reply','--token',self.token,'--inbox',str(self.store/'inbox.json'),'--reply',str(custom),'--report-out',str(custom)])
        self.assertEqual(custom.read_text(),'1 trash')

    def test_symlink_store_output_protected(self):
        from unittest.mock import patch
        source=self.store/'numbers.json'
        contents=source.read_bytes()
        target=self.store/'actual.json'
        source.rename(target)
        source.symlink_to(target)
        with patch.dict(os.environ,PIGEON_HOME=str(self.store)), self.assertRaises(ValueError):
            triage.main(['plan','--token',self.token,'--inbox',str(self.store/'inbox.json'),'--report-out',str(target)])
        self.assertEqual(target.read_bytes(),contents)

    def test_pending_log_outcomes(self):
        self.reply('1 trash, 3 archive')
        execute.Execution(self.store,self.token,self.path).step()
        triage.commit_state(self.store,self.token,self.path,self.store/'results.json')
        parts=json.loads((self.store/'decisions.jsonl').read_text())['parts']
        self.assertEqual([p['outcome'] for p in parts],['issued_unknown','unexecuted'])

    def test_number_store_unknown_keys(self):
        for data in [dict(next=1,items=[],extra=True),dict(next=2,items=[dict(number=1,account='cedar',id='0',extra=True)])]:
            execute.atomic(self.store/'numbers.json',data)
            with self.assertRaises(ValueError): triage.read_numbers(self.store)

    def test_non_action_output_empty(self):
        self.reply('1 yes')
        self.assertEqual(self.output,'')

    def test_cli_plan_bytes_identical(self):
        self.reply('1 trash, 3 move to Billing')
        original=self.path.read_bytes()
        self.reply('1 trash, 3 move to Billing')
        self.assertEqual(self.path.read_bytes(),original)
