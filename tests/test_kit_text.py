"""Contract checks for the public instructions and their assistant shims."""
from pathlib import Path
import re
import shlex
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ['README.md', 'AGENTS.md', 'CLAUDE.md',
        '.agents/skills/email-triage/SKILL.md', 'docs/setup.md',
        'config/guardrails.example.md']


class KitTextTests(unittest.TestCase):
    def test_execution_commands(self):
        text = (ROOT / 'AGENTS.md').read_text()
        for command in ('triage.py lock', 'triage.py plan', 'execute.py next',
                        'execute.py result', 'execute.py observed',
                        'triage.py commit-state', 'triage.py release'):
            self.assertIn(command, text)
        self.assertIn('execute.py unsubscribe', (ROOT / 'docs/setup.md').read_text())
        for obsolete in ('Execute only actions in the current plan through Executor',
                         'state is written at plan time',
                         'assistant fetches unsubscribe links'):
            for name in DOCS:
                self.assertNotIn(obsolete, (ROOT / name).read_text())

    def test_no_contradicting_permissions(self):
        forbidden = ('decline every pause', 'decline a pause', 'decline the pause',
                     'decline approval', 'decline paused', 'leave approvals to the user',
                     'do not answer it yourself', 'the user answers the pause',
                     'approve every call', 'approve any call', 'approve all calls',
                     'approve anything', 'approve all approvals', 'always allow',
                     'change saved approval settings', 'save approval settings',
                     'may change executor', 'can change executor',
                     'may send', 'can send', 'send is allowed')
        for name in DOCS + ['config/config.example.toml',
                            'rules/rules.example.toml', 'rules/never-touch.example.toml',
                            '.gitignore']:
            text = (ROOT / name).read_text().casefold()
            for phrase in forbidden:
                with self.subTest(name=name, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_standard_run_message(self):
        text = (ROOT / '.agents/skills/email-triage/SKILL.md').read_text()
        section = text.split('## Standard run message')[1]
        headings = ['🔴 Needs you', '✅ Handled automatically',
                    '📥 Still in Inbox (by account)', '❓ Questions for you', '⚙️ Run notes']
        positions = [section.index(h) for h in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('📬 Triage · <weekday, date, time, time zone> · <N need you | nothing needs you> · <M handled>', section)
        examples = re.findall(r'```text\n(.*?)```', section, re.S)
        self.assertEqual(len(examples), 2)
        for example in examples:
            self.assertTrue(example.startswith('📬 Triage · '))
            for domain in re.findall(r'@[A-Za-z0-9.-]+', example):
                self.assertIn(domain, ('@example.com', '@example.org', '@example.net'))
        self.assertEqual([examples[0].index(h) for h in headings],
                         sorted(examples[0].index(h) for h in headings))
        self.assertNotIn('🔴', examples[1])

    def test_si6_no_send_setting(self):
        config = tomllib.loads((ROOT / 'config/config.example.toml').read_text())
        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)
        self.assertFalse(any('send' in key.casefold() for key in keys(config)))

    def test_data_policy(self):
        policy = (ROOT / 'config/guardrails.example.md').read_text()
        for term in ('data', 'instructions', 'Never send', 'Never reply',
                     'Never permanently delete', 'step machine'):
            self.assertIn(term, policy)

    def test_assistant_shims(self):
        self.assertEqual((ROOT / 'CLAUDE.md').read_text(), '@' + 'AGENTS.md\n')
        link = ROOT / '.claude/skills'
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), Path('../.agents/skills'))
        self.assertEqual((link / 'email-triage/SKILL.md').resolve(),
                         ROOT / '.agents/skills/email-triage/SKILL.md')
        self.assertTrue((link / 'email-triage/SKILL.md').is_file())

    def test_quick_start_file_references(self):
        text = (ROOT / 'README.md').read_text()
        self.assertEqual(text.count('<!-- quick-start -->'), 1)
        block = text.split('<!-- quick-start -->\n```bash\n')[1].split(
            '\n```\n<!-- /quick-start -->')[0]
        references = set()
        for line in block.splitlines():
            for token in shlex.split(line):
                if token.startswith(('scripts/', 'config/', 'rules/', 'tests/')):
                    references.add(token)
        self.assertTrue(references)
        for name in references | {'hooks'}:
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).exists(), name)

    def test_relative_markdown_links(self):
        for name in DOCS:
            path = ROOT / name
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', path.read_text()):
                if re.match(r'[a-zA-Z]+:', target) or target.startswith('#'):
                    continue
                target = target.split('#')[0]
                with self.subTest(document=name, target=target):
                    self.assertTrue((path.parent / target).exists(), target)

    def test_clarity_contract(self):
        for name in DOCS + ['.gitignore']:
            self.assertNotIn('fallback', (ROOT / name).read_text().casefold())
        section = (ROOT / '.agents/skills/email-triage/SKILL.md').read_text().split('## Standard run message')[1]
        for term in ('stopped', 'rules.toml', 'never-touch.toml', 'plan.json'):
            self.assertIn(term, section)
