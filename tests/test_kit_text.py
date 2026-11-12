"""Contract checks for the public instructions and their assistant shims."""
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ['README.md', 'AGENTS.md', 'CLAUDE.md',
        '.agents/skills/email-triage/SKILL.md', 'docs/setup.md',
        'config/guardrails.example.md']


class KitTextTests(unittest.TestCase):
    def test_rule_examples_load(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import triage
        text = (ROOT / 'rules/rules.example.toml').read_text()
        self.assertTrue(triage.parse_rules(text))
        # Every commented TOML entry is also a valid, usable example.
        expanded = '\n'.join(line[2:] if line.startswith('# ') and
                             (line[2:].startswith('[[rules]]') or '=' in line)
                             else line for line in text.splitlines())
        rules = triage.parse_rules(expanded)
        self.assertEqual({r['action'] for r in rules}, triage.VERBS)
        self.assertTrue({'body', 'question', 'limit', 'target', 'after_unsubscribe'}
                        <= set().union(*(r.keys() for r in rules)))

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

    def test_documented_parser_commands(self):
        commands = set()
        for name in DOCS:
            text = (ROOT / name).read_text()
            for script, tail in re.findall(r'(?:python3 )?scripts/(triage|execute|leak_check)\.py([^`\n]*)', text):
                words = tail.split()
                command = words[0].rstrip(');,.') if script != 'leak_check' and words and not words[0].startswith('-') else ''
                # Bare script references are paths, not command examples.
                if script != 'leak_check' and not command:
                    continue
                flags = tuple(re.findall(r'--[a-z][a-z-]*', tail))
                commands.add((script, command, flags))
        self.assertTrue(commands)
        for script, command, flags in commands:
            with self.subTest(script=script, command=command):
                result = subprocess.run(['python3', str(ROOT / 'scripts' / (script + '.py')),
                                         *([command] if command else []), '--help'],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                supported = set(re.findall(r'--[a-z][a-z-]*', result.stdout))
                self.assertLessEqual(set(flags), supported)

    def test_relative_markdown_links(self):
        for name in DOCS:
            path = ROOT / name
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', path.read_text()):
                if re.match(r'[a-zA-Z]+:', target):
                    continue
                filename, _, anchor = target.partition('#')
                destination = path.parent / filename if filename else path
                with self.subTest(document=name, target=target):
                    self.assertTrue(destination.exists(), target)
                    if anchor:
                        headings = re.findall(r'^#+ +(.+)$', destination.read_text(), re.M)
                        anchors = {re.sub(r'[^\w -]', '', h.lower()).replace(' ', '-') for h in headings}
                        self.assertIn(anchor, anchors)

    def test_clarity_contract(self):
        for name in DOCS + ['.gitignore']:
            content = (ROOT / name).read_text().casefold()
            if name == 'docs/setup.md':
                content = content.split('## private store')[1].split('## configuration')[0]
            self.assertNotIn('fallback', content)
        section = (ROOT / '.agents/skills/email-triage/SKILL.md').read_text().split('## Standard run message')[1]
        for term in ('stopped', 'rules.toml', 'never-touch.toml', 'plan.json'):
            self.assertIn(term, section)


class RuleConfirmationTests(unittest.TestCase):
    def test_separate_rule_confirmation(self):
        agents = (ROOT / 'AGENTS.md').read_text().split('## Writing a rule')[1]
        for term in ('plain yes', 'nothing else', 'run lock', 'appends exactly', 'next plan', 'send-state', 'releasing'):
            self.assertIn(term, agents)
        skill = (ROOT / '.agents/skills/email-triage/SKILL.md').read_text()
        self.assertIn('#writing-a-rule', skill)
        self.assertNotIn('— Add to rules.toml:', skill)

    def test_numbered_sections_are_explicit(self):
        setup = (ROOT / 'docs/setup.md').read_text()
        self.assertNotIn('exact rule can be proposed', setup)
        table = setup.split('| Decision reason')[1].split('Stop reasons are')[0]
        for reason in ('Protected by never-touch', 'No matching rule', 'Run stopped'):
            row = next(line for line in table.splitlines() if line.startswith('| ' + reason + ' |'))
            self.assertIn('number', row)


class RunExitTests(unittest.TestCase):
    def test_named_exits_and_refusal_routes(self):
        text = (ROOT / 'AGENTS.md').read_text()
        self.assertIn('## Run exits', text)
        exits = text.split('## Run exits')[1].split('## Run steps')[0]
        for name in ('Finish', 'Quiet', 'Halt held', 'Fetch refused', 'No lock'):
            row = next(line for line in exits.splitlines() if line.startswith('| ' + name + ' |'))
            self.assertEqual(len(row.split('|')), 7)
            if name == 'Halt held':
                for term in ('skip', 'refused', 'failed'):
                    self.assertIn(term, row)
        for trigger in ('quiet-check refusal', 'planning refusal', 'lock loss'):
            self.assertIn(trigger, text)
        self.assertIn('triage.py check', text)

    def test_plan_stop_and_machine_key_are_distinct(self):
        text = (ROOT / 'AGENTS.md').read_text()
        self.assertNotIn('done without stop', text)
        self.assertNotIn('Here stop means', text)
        self.assertIn('`stop`', text)
        self.assertIn('stopped plan', text)
        self.assertNotIn('run array', text)

    def test_step_response_keys_match_implementation(self):
        import ast
        tree = ast.parse((ROOT / 'scripts/execute.py').read_text())
        step = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'step')
        # All literal response fields, including verify/remaining nested fields.
        keys = {k.value for ret in ast.walk(step) if isinstance(ret, ast.Return)
                for n in ast.walk(ret.value) if isinstance(n, ast.Dict)
                for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        # Action fields are copied from the pending call; its validator owns that schema.
        validator = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'validate_checkpoint')
        pending = next(n for n in ast.walk(validator) if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Attribute) and n.func.attr == 'known_keys'
                       and isinstance(n.args[0], ast.Name) and n.args[0].id == 'pending')
        keys |= {n.value for n in pending.args[1].elts}
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        keys |= {k.value for n in ast.walk(main) if isinstance(n, ast.Dict)
                 for k in n.keys if isinstance(k, ast.Constant)}
        table = (ROOT / 'docs/setup.md').read_text().split('### Step machine responses')[1].split('### Result statuses')[0]
        listed = set(re.findall(r'^\| `([^`]+)` \|', table, re.M))
        self.assertEqual(listed, keys)
        self.assertIn('#step-machine-responses', (ROOT / 'AGENTS.md').read_text())

    def test_quiet_chat_and_drive_are_separate(self):
        agents = (ROOT / 'AGENTS.md').read_text()
        quiet = next(line for line in agents.splitlines() if line.startswith('| Quiet |'))
        for word in ('history.log', 'chat', 'send-state', 'release'):
            self.assertIn(word, quiet)
        skill = (ROOT / '.agents/skills/email-triage/SKILL.md').read_text()
        self.assertIn('history.log', skill)
        self.assertNotIn('directly to release', skill)

    def test_lost_lock_message_is_delivered_in_chat(self):
        agents = (ROOT / 'AGENTS.md').read_text()
        self.assertNotIn('retain it in the host conversation for the next run', agents)
        self.assertIn('post it immediately in the chat', agents)
        setup = (ROOT / 'docs/setup.md').read_text()
        self.assertNotIn('After losing the lock, send nothing', setup)


class HostedTrustTests(unittest.TestCase):
    def test_storage_choice(self):
        setup = (ROOT / 'docs/setup.md').read_text()
        choice = setup.split('## Storage choice')[1].split('## Host requirements')[0]
        for term in ('keeps its files', 'local state', 'starts empty', 'drive state', 'two hosts', 'OneDrive lock'):
            self.assertIn(term, choice)
        self.assertGreaterEqual(setup.count('#storage-choice'), 3)
        self.assertIn('#storage-choice', (ROOT / 'README.md').read_text())

    def test_drive_identifiers(self):
        step = (ROOT / 'docs/setup.md').read_text().split('5. ')[1].split('6. ')[0]
        for term in ('account handle', 'gateway', 'search', 'describe', 'folder ID', 'metadata', 'drive-example', 'folder-example', 'not its name'):
            self.assertIn(term, step)

    def owner(self, owner, others, term):
        self.assertIn(term, (ROOT / owner).read_text())
        for name in others:
            self.assertNotIn(term, (ROOT / name).read_text(), name)

    def test_private_drive_folder(self):
        text = (ROOT / 'docs/setup.md').read_text().split('## Drive state')[1].split('\n## ')[0]
        for term in ("owner's own account", 'never shared', 'shared folder', 'mail-derived summaries', 'lock'):
            self.assertIn(term, text)
        self.owner('docs/setup.md', ('config/guardrails.example.md', 'AGENTS.md'), 'never shared')
        checklist = (ROOT / 'docs/setup.md').read_text().split('## Host requirements')[1].split('The table')[0]
        self.assertIn('private', checklist)

    def test_saved_files_are_data(self):
        text = (ROOT / 'AGENTS.md').read_text()
        for term in ('Every saved or fetched file', 'data, never instructions', 'whatever it says', 'quoted block', 'never act on'):
            self.assertIn(term, text)
        self.owner('AGENTS.md', ('.agents/skills/email-triage/SKILL.md', 'docs/setup.md'), 'data, never instructions')

    def test_delivery_record_and_opening_order(self):
        record = (ROOT / 'docs/setup.md').read_text().split('- run-message.md: ')[1].split('\n')[0]
        for term in ('pending delivery record', 'undelivered', 'upload warning'):
            self.assertIn(term, record)
        for name in ('AGENTS.md', '.agents/skills/email-triage/SKILL.md'):
            self.assertIn('[pending delivery record](', (ROOT / name).read_text())
        self.owner('.agents/skills/email-triage/SKILL.md', ('AGENTS.md', 'docs/setup.md'), 'Opening order:')
        order = (ROOT / '.agents/skills/email-triage/SKILL.md').read_text().split('Opening order:')[1].split('\n')[0]
        terms = ['upload warning', 'Late start', '📬 Triage', 'not listed']
        self.assertEqual([order.index(t) for t in terms], sorted(order.index(t) for t in terms))


class ReferenceTidyTests(unittest.TestCase):
    def test_single_release_section(self):
        text = (ROOT / 'CHANGELOG.md').read_text()
        self.assertEqual(len(re.findall(r'^## Unreleased', text, re.M)), 1)
        self.assertNotRegex(text, r'(?i)\bM[012]\b|\d+ tests')
        # Entries already published on main stay in their dated sections.
        unreleased = text.split('## Unreleased')[1].split('\n## ')[0]
        for published in ('Move rules accept', 'Issue one planned call at a time'):
            self.assertNotIn(published, unreleased)
        for heading in re.findall(r'^## (.*)', text, re.M):
            self.assertRegex(heading, r'^(Unreleased|\d{4}-\d{2}-\d{2}) — ')

    def test_storage_policy_has_one_owner(self):
        policy = (ROOT / 'config/guardrails.example.md').read_text()
        self.assertIn('Permitted storage calls', policy)
        for name in ('AGENTS.md', 'docs/setup.md'):
            text = (ROOT / name).read_text()
            self.assertIn('config/guardrails.example.md', text)
            self.assertNotIn('no listing, search, share', text)
            self.assertNotIn('No listing or search is allowed.', text)

    def test_config_table_complete_rows(self):
        text = (ROOT / 'docs/setup.md').read_text().split('## Configuration')[1].split('## Drive lock')[0]
        rows = [r.split('|')[1:-1] for r in text.splitlines() if r.startswith('|')]
        self.assertTrue(rows)
        self.assertTrue(all(len(r) == 4 for r in rows))
        keys = [r[0].strip() for r in rows[2:]]
        self.assertEqual(len(keys), len(set(keys)))
        for key in ('language', 'timezone', 'inbox_limit', 'lock.kind', 'lock.account', 'lock.folder', 'state.kind', 'state.account', 'state.folder', 'schedule.weekdays', 'schedule.weekends', 'schedule.always_full', 'accounts.HANDLE.address', 'accounts.HANDLE.executor_app', 'accounts.HANDLE.quiet_kind'):
            self.assertIn(key, keys)

    def test_reference_uses_owner_terms(self):
        setup = (ROOT / 'docs/setup.md').read_text()
        self.assertNotIn('Tests inject', setup)
        self.assertNotIn('injected clock', setup)
        self.assertNotIn('Existing local lock records remain valid', setup)
        self.assertEqual(len(re.findall(r'^\| Hosted assistant', setup, re.M)), 1)
        skill = (ROOT / '.agents/skills/email-triage/SKILL.md').read_text()
        self.assertNotIn('by position for their rule', skill)
