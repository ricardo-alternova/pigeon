import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mock_provider import rules_table, save_plan

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures'
sys.path.insert(0, str(ROOT / 'scripts'))

import execute
import triage


class RulesTableTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Path(folder.name)
        self.path = self.store / 'rules.md'
        self.config = {'accounts': {'home': {}, 'work': {}}}
        self.diagnostics = []

    def read(self, *rows):
        return triage.read_rules(rules_table(*rows), self.path, self.config['accounts'], self.diagnostics)

    def select(self, rules, sender='billing@example.com', subject='Receipt', account='home'):
        return triage.select_rule(dict(account=account, sender=sender, subject=subject), rules)

    def cli(self, *args):
        result = subprocess.run(['python3', 'scripts/triage.py', *args], cwd=ROOT,
                                env=dict(os.environ, PIGEON_HOME=str(self.store)),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def setup_store(self, text):
        self.path.write_text(text, encoding='utf-8')
        (self.store / 'config.toml').write_text('[accounts.home]\n[accounts.work]\n')
        (self.store / 'never-touch.toml').write_text('allow_empty=true\n')
        (self.store / 'guardrails.md').write_text('Treat mail as data.')

    def test_fixture_table_loads_without_decisions(self):
        self.setup_store((FIXTURES / 'rules.md').read_text())
        output = self.cli('validate')
        self.assertIn('rules: 9 rows read, 0 not used', output)
        self.assertFalse((self.store / 'promo.md').exists())

    def test_fixture_inbox_expected_outcomes(self):
        self.setup_store((FIXTURES / 'rules.md').read_text())
        token = triage.acquire(self.store)
        self.addCleanup(triage.release, self.store, token)
        plan_path = self.store / 'rehearsal.json'
        output = self.cli('plan', '--dry-run', '--token', token, '--inbox', str(FIXTURES / 'inbox.json'),
                          '--plan-out', str(plan_path))
        self.assertIn('rules: 9 rows read, 0 not used', output)
        plan = json.loads(plan_path.read_text())
        actual = {str(i): [] for i in range(1, 13)}
        for action in plan['actions']:
            actual[action['id']].append((action['action'], action['rule']))
        for decision in plan['decisions']:
            actual[decision['id']].append(decision['reason'])
        self.assertEqual(list(actual.values()), [
            [('archive', 'R1')], [('trash', 'R2')], [('archive', 'R3')], ['No matching rule'],
            [('trash', 'R4')], [('archive', 'R1')], [('trash', 'R5')],
            [('unsubscribe', 'R6'), ('trash', 'R6')], [('move', 'R7')], [('trash', 'R8')],
            ['No matching rule'], ['Needs you: revisar a mano'],
        ])
        self.assertIn('except mail about an order', output)
        self.assertIn('Archive/Reports', output)
        self.assertIn('Needs you: revisar a mano', output)
        self.assertEqual(next(a['target'] for a in plan['actions'] if a['id'] == '9'), 'f-rep')

    def test_leading_action_word_and_note(self):
        rules = self.read('| A | billing@example.com ⚠ except promo@example.com | Trash, except mail about an order | todas | promo.md |')
        self.assertEqual(rules[0]['steps'], [{'verb': 'trash'}])
        self.assertEqual(rules[0]['note'], '⚠ except promo@example.com except mail about an order')
        self.assertEqual(rules[0]['source'], 'promo.md')
        self.assertEqual(rules[0]['files'], [])
        self.assertIsNone(self.select(rules, sender='promo@example.com'))
        rules = self.read('| B | **(billing@example.com)** | BAJA http://example.com/policy.md, then notify | home | |')
        self.assertEqual(rules[0]['steps'], [{'verb': 'unsubscribe'}])
        self.assertEqual(rules[0]['files'], [])
        self.assertEqual(rules[0]['note'], 'then notify')

    def test_action_words_closed_set(self):
        self.assertEqual(triage.ACTION_WORDS, {
            'trash': 'trash', 'archive': 'archive', 'spam': 'spam', 'move': 'move',
            'mover': 'move', 'unsubscribe': 'unsubscribe', 'baja': 'unsubscribe',
        })
        message = dict(account='home', id='m', folder='Inbox', sender='billing@example.com', subject='Receipt')
        for verb in ('delete', 'borrar', 'eliminar', 'vaciar', 'send', 'draft', 'trash_all'):
            with self.subTest(verb=verb):
                rules = self.read(f'| A | billing@example.com | {verb} + trash | todas | |')
                plan = triage.build_plan(dict(accounts={'home': 1}, messages=[message]), rules, [], self.config)
                self.assertEqual(plan['actions'], [])
                self.assertEqual(plan['decisions'][0]['reason'], 'Needs you: ' + verb)

    def test_header_only_table_loads_empty(self):
        shipped = (ROOT / 'rules/rules.example.md').read_text()
        self.assertEqual(shipped, rules_table())
        self.assertEqual(self.read(), [])

    def test_bad_row_refused_with_row_number(self):
        cases = [
            ('| X | billing@example.com | trash | todas | | extra |', 'more than five cells'),
            ('| X | none | trash | todas | |', 'no sender, domain or subject'),
            ('| X | asunto: * | trash | todas | |', 'subject pattern has no literal text'),
            ('| X | *.com | trash | todas | |', 'wildcard covers a top-level domain'),
            ('| X | example.com | trash + archive | todas | |', 'more than one filing step'),
            ('| X | example.com | trash + baja | todas | |', 'kit step after filing'),
            ('| X | example.com | unsubscribe + baja | todas | |', 'more than one unsubscribe step'),
            ('| X | example.com | mover | todas | |', 'move without a folder'),
            ('| X | example.com | mover ../x | todas | |', 'invalid move folder'),
            ('| X | example.com | mover a/b/c/d/e/f/g/h/i | todas | |', 'invalid move folder'),
            ('| X | example.com | mover `A\tB` | todas | |', 'invalid move folder'),
            ('| X | example.com | trash | other | |', 'no configured account in scope'),
        ]
        for row, reason in cases:
            with self.subTest(reason=reason):
                self.diagnostics.clear()
                rules = self.read(row, '| Y | example.org | archive | home | |')
                self.assertEqual([r['id'] for r in rules], ['Y'])
                self.assertEqual(self.diagnostics, ['rules: 1 rows read, 1 not used',
                                                    'rules: table at line 1',
                                                    'Rule X (line 3) not used: ' + reason])

    def test_grammar_and_table_boundaries(self):
        text = '\n'.join((
            '# Rules', '| other | table |', '| --- | --- |', '| ignored | ignored |',
            'Id | PATRÓN (remitente) | Acción original | Alcance | Fuente',
            '--- | :---: | ---: | --- | ---',
            r'| E | `@example.org` y `asunto: a\|b` | mover `Archive/A + B` | home, unknown |',
            '| E | billing@example.com y second@example.com and example.net note | archive | todas |',
            '| | subject: a loose `tick | archive | home |',
            'End of table.',
            rules_table('| H | `subject: x|y` | archive | home | |'),
        ))
        rules = triage.read_rules(text, self.path, self.config['accounts'], self.diagnostics)
        self.assertEqual(self.diagnostics, ['rules: 4 rows read, 0 not used',
                                           'rules: table at line 5', 'rules: table at line 11',
                                           'Rule E (line 7): unknown account ignored: unknown'])
        match = self.select(rules, sender='first@example.org', subject='a|b')
        self.assertEqual(match['steps'], [{'verb': 'move', 'target': 'Archive/A + B'}])
        self.assertIsNone(self.select(rules, sender='first@example.org', subject='a'))
        self.assertIsNone(self.select(rules, sender='first@example.org', subject='a|b', account='work'))
        for sender in ('billing@example.com', 'second@example.com', 'first@example.net'):
            match = self.select(rules, sender=sender, account='work')
            self.assertEqual(match['id'], 'E (line 8)')
            self.assertEqual(match['note'], 'note')
        self.assertEqual(self.select(rules, sender='nobody@example.com', subject='a loose `tick')['id'], 'line 9')
        self.assertEqual(self.select(rules, sender='nobody@example.com', subject='x|y')['id'], 'H')

    def test_priority_and_exact_domains(self):
        rules = self.read('| S | subject: receipt | spam | todas | |',
                          '| W | *.example.com | archive | todas | |',
                          '| D | mail.example.com | trash | todas | |',
                          '| A | billing@' + 'mail.example.com | archive | todas | |',
                          '| B | billing@' + 'mail.example.com | trash | todas | |')
        for sender, expected in [('billing@' + 'mail.example.com', 'A'), ('sale@' + 'mail.example.com', 'W'),
                                 ('sale@example.com', 'S'), ('sale@' + 'notexample.com', 'S')]:
            self.assertEqual(self.select(rules, sender=sender)['id'], expected)
        self.assertIsNone(self.select(rules[2:3], sender='sale@' + 'sub.mail.example.com'))
        listing = dict(accounts={'home': 1}, messages=[
            dict(account='home', id='m', folder='Inbox', sender='billing@' + 'mail.example.com', subject='Receipt')])
        plan = triage.build_plan(listing, rules, [dict(domain='example.com')], self.config)
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['decisions'][0]['reason'], 'Protected by never-touch')

    def test_plain_subject_and_explicit_wildcard(self):
        plain, wildcard = self.read('| P | subject: receipt | archive | todas | |',
                                    '| W | asunto: Weekly * | trash | todas | |')
        self.assertIsNotNone(self.select([plain], subject='Your Receipt from Acme'))
        self.assertIsNotNone(self.select([wildcard], subject='Weekly update'))
        self.assertIsNone(self.select([wildcard], subject='Re: Weekly update'))

    def test_file_reference_blocks_only_when_required(self):
        message = dict(account='home', id='m', folder='Inbox', sender='billing@example.com', subject='Receipt')
        listing = dict(accounts={'home': 1}, messages=[message])
        for reference in ('[policy](manual.md)', '`manual.md`', 'manual.md'):
            with self.subTest(reference=reference):
                required = self.store / 'manual.md'
                required.unlink(missing_ok=True)
                rules = self.read(f'| A | billing@example.com | trash, follow {reference} | home | citation.md |')
                plan = triage.build_plan(listing, rules, [], self.config)
                self.assertEqual(plan['actions'], [])
                self.assertEqual(plan['decisions'][0]['reason'], 'Needs manual.md, which is not available')
                required.write_text('Example policy')
                plan = triage.build_plan(listing, rules, [], self.config)
                self.assertEqual([a['action'] for a in plan['actions']], ['trash'])

    def test_ordered_prerequisites(self):
        message = dict(account='home', id='m', folder='Inbox', sender='billing@example.com', subject='Receipt')
        listing = dict(accounts={'home': 1}, messages=[message])
        for action, reason in [
            ('baja + trash', 'Unsafe unsubscribe'),
            ('reportar el monto + trash', 'Needs you: reportar el monto'),
        ]:
            rules = self.read(f'| A | billing@example.com | {action} | home | |')
            plan = triage.build_plan(listing, rules, [], self.config)
            self.assertEqual(plan['actions'], [])
            self.assertEqual(plan['decisions'][0]['reason'], reason)

    def test_failed_unsubscribe_stops_filing(self):
        self.setup_store(rules_table('| A | billing@example.com | baja HTTPS + trash | home | |'))
        listing = dict(accounts={'home': 100}, messages=[
            dict(account='home', id='m', folder='Inbox', sender='billing@example.com',
                 subject='Receipt', unsubscribe_url='https://example.com/leave')])
        execute.atomic(self.store / 'inbox.json', listing)
        rules = triage.read_rules(self.path.read_text(), self.path, self.config['accounts'])
        plan = triage.build_plan(listing, rules, [], self.config)
        self.assertEqual([a['action'] for a in plan['actions']], ['unsubscribe', 'trash'])
        for status in ('failed', 'unsubscribe_not_executable', 'executed'):
            with self.subTest(status=status):
                token = triage.acquire(self.store)
                try:
                    path = self.store / 'plan.json'
                    save_plan(self.store, token, path, dict(plan, first_run=True))
                    machine = execute.Execution(self.store, token, path)
                    self.assertIn('unsubscribe', machine.step()['run'])
                    with patch.object(machine, 'unsubscribe', return_value=status):
                        self.assertEqual(machine.step(local=True), {'status': status})
                    following = machine.step()
                    if status == 'executed':
                        self.assertEqual(following['action'], 'trash')
                    else:
                        self.assertEqual(following, {'done': True})
                        self.assertEqual([(r['action'], r['status']) for r in machine.state['results']],
                                         [('unsubscribe', status), ('trash', 'unexecuted')])
                finally:
                    triage.release(self.store, token)

    def test_row_diagnostics_in_validate_and_plan(self):
        self.setup_store(rules_table('| X | no pattern | trash | todas | |',
                                     '| Y | subject: Receipt | archive | home other | |'))
        token = triage.acquire(self.store)
        self.addCleanup(triage.release, self.store, token)
        for args in [('validate',), ('plan', '--dry-run', '--token', token, '--inbox',
                                      str(FIXTURES / 'inbox.json'))]:
            output = self.cli(*args)
            self.assertIn('rules: 1 rows read, 1 not used', output)
            self.assertIn('Rule X (line 3) not used: no sender, domain or subject', output)
            self.assertIn('Rule Y (line 4): unknown account ignored: other', output)

    def test_row_cap_shared_by_alternative_tokens(self):
        rules = self.read('| A | billing@example.com or second@example.com | archive | todas | |')
        listing = dict(accounts={'home': 100}, messages=[
            dict(account='home', id=str(i), folder='Inbox', subject='Receipt',
                 sender=('billing' if i % 2 else 'second') + '@example.com') for i in range(11)])
        plan = triage.build_plan(listing, rules, [], self.config)
        self.assertEqual(len(plan['actions']), 11)
        self.assertEqual(plan['approval_thresholds'], [dict(kind='rule', rule='A', count=11, limit=10)])

    def test_rehearsal_checks_references_relative_to_the_table(self):
        self.setup_store(rules_table('| A | billing@example.com | archive, see manual.md | home | |'))
        (self.store / 'manual.md').write_text('Example instructions')
        token = triage.acquire(self.store)
        self.addCleanup(triage.release, self.store, token)
        path = self.store / 'rehearsal.json'
        self.cli('plan', '--dry-run', '--token', token, '--inbox', str(FIXTURES / 'inbox.json'),
                 '--plan-out', str(path))
        self.assertEqual(execute.simulate(self.store, path)['stages'][-1]['commit_preview']['counts'],
                         {'home': 9, 'work': 1})

    def test_mold_linear(self):
        start = time.monotonic()
        self.assertFalse(triage.subject_matches('a*' * 49 + 'b*', 'a' * 10000))
        self.assertLess(time.monotonic() - start, 1)

    def test_unrecognised_backticked_token_skips_row(self):
        rules = self.read('| A | `billing@example.com` `asunto Weekly` | trash | todas | |',
                          '| B | `billing@example.com` y `promo@example.com` | archive | todas | |')
        self.assertEqual(list(dict.fromkeys(r['id'] for r in rules)), ['B'])
        self.assertEqual(self.diagnostics, ['rules: 1 rows read, 1 not used', 'rules: table at line 1',
                                            'Rule A (line 3) not used: unrecognised backticked token'])
        self.assertEqual(rules[0]['note'], 'y')

    def test_url_link_is_not_a_file_reference(self):
        message = dict(account='home', id='m', folder='Inbox', sender='billing@example.com', subject='Receipt')
        rules = self.read('| A | billing@example.com | trash, see [aquí](https://example.com/leave) | home | |')
        self.assertEqual(rules[0]['files'], [])
        plan = triage.build_plan(dict(accounts={'home': 1}, messages=[message]), rules, [], self.config)
        self.assertEqual([a['action'] for a in plan['actions']], ['trash'])

    def test_file_reference_outside_rules_folder_skips_row(self):
        for reference in ('[policy](../../outside.md)', '[policy](/etc/policy.md)'):
            with self.subTest(reference=reference):
                self.diagnostics.clear()
                rules = self.read(f'| A | billing@example.com | trash, see {reference} | home | |',
                                  '| B | example.org | archive | home | |')
                self.assertEqual([r['id'] for r in rules], ['B'])
                self.assertEqual(self.diagnostics[2],
                                 'Rule A (line 3) not used: file reference outside the rules folder')

    def test_table_ends_at_first_non_row(self):
        text = '\n'.join((
            rules_table('| A | billing@example.com | trash | home | |'),
            'Notes: sender | subject | action | scope | source',
            '| B | sale@example.org | trash | home | |',
            '```',
            '| C | info@example.net | trash | home | |',
            '```',
            rules_table('| D | promo@example.com | archive | home | |'),
        ))
        rules = triage.read_rules(text, self.path, self.config['accounts'], self.diagnostics)
        self.assertEqual([r['id'] for r in rules], ['A', 'D'])
        self.assertEqual(self.diagnostics, ['rules: 2 rows read, 0 not used',
                                            'rules: table at line 1', 'rules: table at line 10'])

    def test_header_without_separator_is_reported(self):
        text = '\n'.join(('| ID | Patrón | Acción | Alcance | Fuente |',
                          '| A | billing@example.com | trash | home | |'))
        rules = triage.read_rules(text, self.path, self.config['accounts'], self.diagnostics)
        self.assertEqual(rules, [])
        self.assertEqual(self.diagnostics, ['rules: 0 rows read, 0 not used', 'rules: table at line 1',
                                            'rules: header at line 1 has no separator'])

    def test_rule_cases_use_the_plan_outcome(self):
        self.setup_store(rules_table('| A | billing@example.com | baja + trash | home | |',
                                     '| B | promo@example.com | revisar a mano | home | |'))
        (self.store / 'cases.toml').write_text(
            '[[cases]]\nname="two step"\naccount="home"\nsender="billing@example.com"\nsubject="Receipt"\nexpect="none"\n'
            '[[cases]]\nname="agent"\naccount="home"\nsender="promo@example.com"\nsubject="Sale"\nexpect="none"\n'
            '[[cases]]\nname="wrong"\naccount="home"\nsender="billing@example.com"\nsubject="Receipt"\nexpect="unsubscribe"\n')
        result = subprocess.run(['python3', 'scripts/triage.py', 'validate'], cwd=ROOT,
                                env=dict(os.environ, PIGEON_HOME=str(self.store)), capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('cases: 3, failed: 1', result.stdout)
        self.assertIn('wrong: expected unsubscribe, found none', result.stdout)
        config = triage.state_config(self.store)
        rules = triage.read_rules(self.path.read_text(), self.path, config['accounts'])
        for sender, subject, reason in (('billing@example.com', 'Receipt', 'Unsafe unsubscribe'),
                                        ('promo@example.com', 'Sale', 'Needs you: revisar a mano')):
            plan = triage.build_plan(dict(accounts={'home': 1}, messages=[
                dict(account='home', id='case', folder='Inbox', sender=sender, subject=subject)]), rules, [], config)
            self.assertEqual(plan['actions'], [])
            self.assertEqual(plan['decisions'][0]['reason'], reason)

    def test_reply_plan_carries_rule_diagnostics(self):
        self.setup_store(rules_table('| X | no pattern | trash | todas | |',
                                     '| A | billing@example.com | archive | home | |'))
        token = triage.acquire(self.store)
        self.addCleanup(triage.release, self.store, token)
        (self.store / 'reply.txt').write_text('1 archive')
        path = self.store / 'rehearsal.json'
        output = self.cli('reply', '--dry-run', '--token', token, '--inbox', str(FIXTURES / 'inbox.json'),
                          '--reply', str(self.store / 'reply.txt'), '--plan-out', str(path))
        self.assertIn('Rule X (line 3) not used: no sender, domain or subject', output)
        self.assertIn('Rule X (line 3) not used: no sender, domain or subject',
                      json.loads(path.read_text())['rule_diagnostics'])

    def test_protected_inputs_include_both_rules_files(self):
        self.setup_store(rules_table('| A | billing@example.com | archive | home | |'))
        (self.store / 'rules.toml').write_text('[[rules]]\n')
        token = triage.acquire(self.store)
        self.addCleanup(triage.release, self.store, token)
        for name in ('rules.md', 'rules.toml'):
            with self.subTest(name=name):
                result = subprocess.run(['python3', 'scripts/triage.py', 'plan', '--dry-run', '--token', token,
                                         '--inbox', str(FIXTURES / 'inbox.json'), '--plan-out', str(self.store / name)],
                                        cwd=ROOT, env=dict(os.environ, PIGEON_HOME=str(self.store)),
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('--plan-out: expected a path other than an input, state or lock file', result.stderr)
                self.assertTrue((self.store / name).exists())

    def test_legacy_rules_toml_is_reported(self):
        self.setup_store(rules_table('| A | billing@example.com | archive | home | |'))
        (self.store / 'rules.toml').write_text('[[rules]]\n')
        token = triage.acquire(self.store)
        self.addCleanup(triage.release, self.store, token)
        self.assertIn(triage.LEGACY_RULES, self.cli('validate'))
        self.assertIn(triage.LEGACY_RULES, self.cli('plan', '--dry-run', '--token', token,
                                                    '--inbox', str(FIXTURES / 'inbox.json')))
        (self.store / 'reply.txt').write_text('1 archive')
        self.assertIn(triage.LEGACY_RULES, self.cli('reply', '--dry-run', '--token', token,
                                                    '--inbox', str(FIXTURES / 'inbox.json'),
                                                    '--reply', str(self.store / 'reply.txt')))

    def test_brake_ignores_unsafe_unsubscribe(self):
        rules = self.read('| A | billing@example.com | baja + trash | todas | |')
        listing = dict(accounts={'home': 11}, messages=[
            dict(account='home', id=str(i), folder='Inbox', sender='billing@example.com', subject='Receipt')
            for i in range(11)])
        plan = triage.build_plan(listing, rules, [], self.config)
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['reasons'], [])
