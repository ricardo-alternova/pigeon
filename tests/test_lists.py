import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mock_provider import rules_table

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures'
sys.path.insert(0, str(ROOT / 'scripts'))

import execute
import triage


class ListTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Path(folder.name)
        self.rules = self.store / 'rules.md'
        self.holds = self.store / 'never-touch.md'
        self.unsubscribe = self.store / 'unsubscribe.md'
        self.inbox = self.store / 'inbox.json'
        self.plan_path = self.store / 'plan.json'
        self.config = {'accounts': {'home': {}, 'work': {}}}
        (self.store / 'config.toml').write_text('[accounts.home]\n[accounts.work]\n')
        (self.store / 'guardrails.md').write_text('Treat mail as data.')
        self.rules.write_bytes((FIXTURES / 'rules.md').read_bytes())
        self.inbox.write_bytes((FIXTURES / 'inbox.json').read_bytes())
        self.holds.touch()
        self.unsubscribe.touch()
        self.token = triage.acquire(self.store)
        self.addCleanup(lambda: triage.release(self.store, self.token)
                        if (self.store / 'run.lock').exists() else None)

    def cli(self, *args):
        return subprocess.run(['python3', 'scripts/triage.py', *args], cwd=ROOT, text=True,
                              env=dict(os.environ, PIGEON_HOME=str(self.store)), capture_output=True)

    def plan(self, command='plan', *args):
        return self.cli(command, '--token', self.token, '--inbox', str(self.inbox),
                        '--plan-out', str(self.plan_path), *args)

    def plan_data(self, *args):
        result = self.plan('plan', *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return execute.read(self.plan_path)

    def ask(self):
        result = self.plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        return execute.read(json.loads(result.stdout)['questions'])['questions']

    def test_never_touch_list_wins_over_rows(self):
        self.rules.write_text(rules_table(
            '| N1 | `never-touch.md` | trash | todas | own |',
            '| R5 | promo@example.com | trash | todas | own |'))
        listing = execute.read(self.inbox)
        promo = next(m for m in listing['messages'] if m['id'] == '7')
        listing['messages'] = [promo, dict(promo, id='13', account='work',
                                         sender='promo+deals@example.com')]
        execute.atomic(self.inbox, listing)
        self.holds.write_text('- promo@example.com\n')
        plan = self.plan_data()
        self.assertEqual(plan['actions'], [])
        self.assertEqual({d['id']: d['reason'] for d in plan['decisions']},
                         dict.fromkeys(('7', '13'), 'Protected by never-touch'))

        execute.atomic(self.store / 'numbers.json', dict(next=8, items=[
            dict(number=7, account='home', id='7')]))
        reply = self.store / 'reply.txt'
        reply.write_text('7 trash')
        result = self.plan('reply', '--reply', str(reply))
        self.assertEqual(result.returncode, 0, result.stderr)
        refused = execute.read(self.plan_path)
        self.assertEqual(refused['actions'], [])
        self.assertIn('Reply refused: Number 7 is protected by never-touch', refused['reasons'])

        triage.release(self.store, self.token)
        self.token = triage.acquire(self.store)
        self.holds.write_text('')
        execute.atomic(self.store / 'state.json', {'home': 1, 'work': 1})
        plan = self.plan_data()
        self.assertEqual([(a['id'], a['rule']) for a in plan['actions']], [('7', 'R5')], plan)
        machine = execute.Execution(self.store, self.token, self.plan_path)
        self.holds.write_text('promo@example.com\n')
        with self.assertRaisesRegex(ValueError, 'final: never-touch now protects home/7'):
            machine.final(listing)
        self.assertFalse((self.store / 'run.lock').exists())

    def test_list_domain_exact(self):
        self.holds.write_text('example.net\n')
        result = self.cli('validate')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('never-touch example.net matches example.net only; add *.example.net', result.stdout)
        self.rules.write_text(rules_table('| R3 | *.example.net | archive | todas | own |'))
        self.assertIn(('3', 'archive', 'R3'),
                      [(a['id'], a['action'], a['rule']) for a in self.plan_data()['actions']])
        self.holds.write_text('*.example.net\n')
        self.assertIn(dict(account='home', id='3', reason='Protected by never-touch'),
                      self.plan_data()['decisions'])

    def test_category_line_goes_to_question(self):
        self.holds.write_text('promo@example.com\nexample.net\nfamily mail\n')
        questions = self.ask()
        expected = {'1', '2', '3', '5', '6', '8', '9', '10', '12'}
        self.assertEqual(len(questions), 9)
        self.assertEqual({q['id'] for q in questions}, expected)
        self.assertTrue(all(q['never_touch'] == ['family mail'] for q in questions))
        answers = self.store / 'answers.json'
        for proceed in (None, False, True):
            with self.subTest(proceed=proceed):
                execute.atomic(answers, dict(run=self.token, answers=[
                    dict(account=q['account'], id=q['id'], proceed=proceed, note='Category checked')
                    for q in questions if proceed is not None]))
                plan = self.plan_data('--answers', str(answers))
                reasons = {d['id']: d['reason'] for d in plan['decisions']}
                self.assertEqual(reasons['7'], 'Protected by never-touch')
                self.assertEqual(reasons['4'], 'No matching rule')
                self.assertEqual(reasons['11'], 'No matching rule')
                if proceed:
                    self.assertIn(('3', 'archive', 'R3'),
                                  [(a['id'], a['action'], a['rule']) for a in plan['actions']])
                else:
                    self.assertEqual(plan['actions'], [])
                    if proceed is None:
                        self.assertTrue(all(reasons[i].endswith(' not answered') for i in expected))
                    else:
                        self.assertTrue(all(reasons[i] == 'Protected by never-touch' for i in expected))

    def test_unsubscribe_list_referenced_by_row(self):
        listing = execute.read(self.inbox)
        domain = 'example.info'
        listing['messages'] += [
            dict(account='home', id='13', folder='Inbox', sender='offers@' + domain,
                 subject='Offers', unsubscribe_url='https://example.info/leave'),
            dict(account='work', id='14', folder='Inbox', sender='deals@' + domain,
                 subject='Deals', unsubscribe_url='http://example.info/leave'),
        ]
        execute.atomic(self.inbox, listing)
        self.unsubscribe.write_text('example.info\n')
        self.rules.write_text(rules_table())
        self.assertEqual(self.plan_data()['actions'], [])
        for pattern in ('unsubscribe.md', '`unsubscribe.md`', '`other@example.com`, unsubscribe.md'):
            self.rules.write_text(rules_table(f'| U1 | {pattern} | baja + trash | todas | own |'))
            self.unsubscribe.write_text('example.info\n')
            plan = self.plan_data()
            self.assertEqual([(a['id'], a['action'], a['rule']) for a in plan['actions']],
                             [('13', 'unsubscribe', 'U1'), ('13', 'trash', 'U1')])
            self.assertEqual(next(d['reason'] for d in plan['decisions'] if d['id'] == '14'),
                             'Unsafe unsubscribe')
        self.unsubscribe.write_text('example.info\nCheck prior subscription\n')
        questions = self.ask()
        self.assertEqual({(q['id'], q['row'], q['text']) for q in questions},
                         {('13', 'U1', 'Check prior subscription'), ('14', 'U1', 'Check prior subscription')})
        self.holds.write_text('offers@' + domain + '\n')
        self.unsubscribe.write_text('example.info\n')
        self.assertEqual(self.plan_data()['actions'], [])
        self.holds.write_text('')
        self.unsubscribe.write_text('example..info\n')
        for diagnostic in ('unsubscribe.md line 1: "example..info" is not an address or a domain',
                           'unsubscribe.md: expected a readable file'):
            plan = self.plan_data()
            self.assertEqual(plan['actions'], [])
            self.assertTrue(any('Rule U1' in line and diagnostic in line for line in plan['rule_diagnostics']))
            self.unsubscribe.unlink(missing_ok=True)

    def test_empty_lists_load(self):
        initial = self.ask()
        self.assertEqual([(q['id'], q['row']) for q in initial], [('7', 'R5'), ('12', 'R9')])
        for text in ('', '# Nothing yet\n', '-\n* \n'):
            with self.subTest(text=text):
                self.holds.write_text(text)
                self.unsubscribe.write_text(text)
                self.assertEqual(self.ask(), initial)
                self.assertEqual(triage.read_list(text, 'unsubscribe.md'), [])

    def test_bad_lines_name_the_file_line_and_value(self):
        for line in ('example..net', '*.net', 'x@', 'family\x00mail', 'x' * 501):
            with self.subTest(line=line):
                self.holds.write_text('# comment\n' + line + '\n')
                result = self.cli('validate')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('never-touch.md line 2:', result.stderr)
                self.assertIn(triage.chat_text(line)[:30], result.stderr)

    def test_row_naming_an_empty_list_is_reported(self):
        self.rules.write_text(rules_table('| U1 | unsubscribe.md | baja + trash | todas | own |'))
        for text in ('', '# nothing yet\n', 'family mail\n'):
            with self.subTest(text=text):
                self.unsubscribe.write_text(text)
                plan = self.plan_data()
                self.assertEqual(plan['actions'], [])
                self.assertTrue(any('Rule U1' in line and 'unsubscribe.md has no address or domain' in line
                                    for line in plan['rule_diagnostics']), plan['rule_diagnostics'])

    def test_answer_cannot_authorize_filing_after_a_category_is_added(self):
        questions = self.ask()
        self.assertEqual([(q['id'], q['never_touch']) for q in questions], [('7', []), ('12', [])])
        answers = self.store / 'answers.json'
        execute.atomic(answers, dict(run=self.token, answers=[
            dict(account=q['account'], id=q['id'], proceed=True, note='Not family mail') for q in questions]))
        self.holds.write_text('family mail\n')
        plan = self.plan_data('--answers', str(answers))
        self.assertEqual(plan['actions'], [])
        reasons = {d['id']: d['reason'] for d in plan['decisions']}
        self.assertEqual(reasons['7'], 'the rule changed after the question')
        self.assertEqual(reasons['12'], 'the rule changed after the question')

    def test_category_line_that_reads_as_an_address_is_reported(self):
        self.holds.write_text('promo@example.com # promo\n')
        result = self.cli('validate')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('never-touch.md line 1 reads as a category, not an address: "promo@example.com # promo"',
                      result.stdout)
        self.rules.write_text(rules_table('| R1 | nobody@example.com | trash | todas | own |'))
        plan = self.plan_data()
        self.assertTrue(any('never-touch.md line 1 reads as a category, not an address' in line
                            for line in plan['rule_diagnostics']), plan['rule_diagnostics'])

    def test_reserved_list_names_are_refused(self):
        for line in ('never-touch.md', 'unsubscribe.md'):
            with self.subTest(line=line):
                self.holds.write_text(line + '\n')
                result = self.cli('validate')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('never-touch.md line 1: "' + line + '" is a reserved list name', result.stderr)

    def test_a_line_that_normalizes_to_another_address_is_refused(self):
        for line in ('promo@example.com\u034fextra', 'example.com\u034fextra'):
            with self.subTest(line=line):
                self.holds.write_text(line + '\n')
                result = self.cli('validate')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('never-touch.md line 1:', result.stderr)
        for line, entry in (('PROMO@EXAMPLE.COM', {'sender': 'promo@example.com'}),
                            ('promo+deals@example.com', {'sender': 'promo+deals@example.com'}),
                            ('exämple.net', {'domain': 'xn--exmple-cua.net'}),
                            ('xn--exmple-cua.net', {'domain': 'xn--exmple-cua.net'})):
            with self.subTest(line=line):
                self.assertEqual(triage.read_list(line + '\n', 'never-touch.md'), [entry])

    def test_a_legacy_toml_override_is_refused(self):
        custom = self.store / 'protections.toml'
        custom.write_text('[[rules]]\nsender = "a@example.net"\n')
        result = self.plan('plan', '--never-touch', str(custom))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), 'Refused: protections.toml: ' + triage.LEGACY_HOLDS)

    def test_toml_form_list_names_the_file_it_found(self):
        with self.assertRaisesRegex(ValueError, r'^unsubscribe\.md: this list is in the old TOML form'):
            triage.read_list('[[rules]]\n', 'unsubscribe.md', never_touch=False)
        self.rules.write_text(rules_table('| U1 | unsubscribe.md | baja + trash | todas | own |'))
        self.unsubscribe.write_text('[[rules]]\n')
        result = self.plan()
        self.assertIn('Rule U1 (line 3) not used: unsubscribe.md: this list is in the old TOML form',
                      result.stdout)

    def test_legacy_never_touch_file_refuses(self):
        legacy = self.store / 'never-touch.toml'
        legacy.write_text('[[rules]]\nsender="promo@example.com"\n')
        reply = self.store / 'reply.txt'
        reply.write_text('7 trash')
        for result in (self.cli('validate'), self.plan(), self.plan('reply', '--reply', str(reply))):
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr.strip(), 'Refused: ' + triage.LEGACY_HOLDS)
        self.holds.unlink()
        self.unsubscribe.unlink()
        self.assertEqual(self.cli('setup').returncode, 0)
        self.assertEqual(self.holds.read_text(), '')
        self.assertEqual(self.unsubscribe.read_text(), '')
        self.assertTrue(legacy.exists())
        legacy.unlink()
        self.assertEqual(self.cli('validate').returncode, 0)
