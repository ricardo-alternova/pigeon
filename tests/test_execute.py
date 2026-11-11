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
