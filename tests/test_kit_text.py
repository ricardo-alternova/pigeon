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
        'config/guardrails.example.md', 'docs/quickstart.md']


class KitTextTests(unittest.TestCase):
    def test_document_heading_links_and_see_references_resolve(self):
        from urllib.parse import unquote
        documents = [ROOT / n for n in DOCS]
        def headings(path):
            return [re.sub(r'[^\w -]', '', h.lower()).replace(' ', '-')
                    for h in re.findall(r'^#+ (.+)$', path.read_text(), re.M)]
        all_headings = {h for path in documents for h in headings(path)}
        for path in documents:
            text = path.read_text()
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
                if '://' in target or target.startswith('mailto:'): continue
                name, _, anchor = target.partition('#')
                destination = (path.parent / name).resolve() if name else path
                self.assertTrue(destination.exists(), (path, target))
                if anchor: self.assertIn(unquote(anchor), headings(destination), (path, target))
            for section in re.findall(r'(?i)see\s+`([^`]+)`', text):
                anchor = re.sub(r'[^\w -]', '', section.lower()).replace(' ', '-')
                self.assertIn(anchor, all_headings, (path, section))

    def test_quickstart_commands_and_flags_exist(self):
        import argparse
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT / 'scripts'))
        import triage, execute
        parsers = {}
        class Captured(Exception): pass
        def capture(parser, *args, **kwargs):
            parsers[current] = parser
            raise Captured()
        for current, module in [('triage', triage), ('execute', execute)]:
            with patch.object(argparse.ArgumentParser, 'parse_args', capture):
                with self.assertRaises(Captured): module.main()
        text = (ROOT / 'docs/quickstart.md').read_text()
        all_options = {o for parser in parsers.values() for action in parser._actions if isinstance(action, argparse._SubParsersAction) for choice in action.choices.values() for a in choice._actions for o in a.option_strings}
        for flag in re.findall(r'--[a-z][a-z-]*', text): self.assertIn(flag, all_options)
        for script, command, suffix in re.findall(r'(triage|execute)\.py\s+([a-z][a-z-]*)([^`\n]*)', text):
            choices = next(a.choices for a in parsers[script]._actions if isinstance(a, argparse._SubParsersAction))
            self.assertIn(command, choices)
            options = {o for a in choices[command]._actions for o in a.option_strings}
            for flag in re.findall(r'--[a-z][a-z-]*', suffix): self.assertIn(flag, options, (script, command, flag))

    def test_routine_quickstart_size_and_entry_points(self):
        path = ROOT / 'docs/quickstart.md'
        self.assertLessEqual(len(path.read_bytes()), 6000)
        for name in ('README.md', 'AGENTS.md', '.agents/skills/email-triage/SKILL.md'):
            self.assertIn('docs/quickstart.md', (ROOT / name).read_text())
        text = path.read_text()
        for term in ('fetch-state', 'lock', 'quiet-check', 'plan', 'next', 'result',
                     'commit-state', 'message', 'send-state', 'release'):
            self.assertIn('py ' + term, text)
        self.assertIn('Stop on any refusal', text)
        for term in ('First installation', 'refusal', 'Owner replies', 'Rule change', 'Unsubscribe', 'Gateway recipes'):
            self.assertIn(term, text)

    def test_gmail_received_datetime_plans_dated_move(self):
        from datetime import datetime, timezone
        sys.path.insert(0, str(ROOT / 'scripts'))
        import triage
        date = datetime.fromtimestamp(1772323200000 / 1000, timezone.utc).isoformat()
        listing = dict(accounts={'cedar': 100}, messages=[dict(account='cedar',
            id='item-a', folder='Inbox', sender='billing@example.org', subject='Invoice', date=date)],
            folders={'cedar': [dict(id='folder-a', name='Bills/2026/03')]})
        rule = dict(id='dated', accounts=['cedar'], subject='Invoice', action='move', target='Bills/{year}/{month}')
        plan = triage.build_plan(listing, [rule], [], {'accounts': {'cedar': {}}})
        self.assertEqual(plan['reasons'], [])
        self.assertEqual([(a['action'], a['target']) for a in plan['actions']], [('move', 'folder-a')])

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
        text = (ROOT / 'AGENTS.md').read_text() + (ROOT / 'docs/quickstart.md').read_text()
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
        sys.path.insert(0, str(ROOT / 'scripts'))
        import triage
        setup = (ROOT / 'docs/setup.md').read_text()
        rows = re.findall(r'^\| (inbox|planned|rules|handled|notes) \| (.*?) \| (.*?) \|$', setup, re.M)
        self.assertEqual({k: en for k, en, es in rows}, triage.SECTION_LABELS['en'])
        self.assertEqual({k: es for k, en, es in rows}, triage.SECTION_LABELS['es'])
        for name in ('docs/quickstart.md',):
            text = (ROOT / name).read_text()
            self.assertIn('triage.py message --token TOKEN', text)
            self.assertIn('Post its output unchanged.', text)
        self.assertNotIn('Needs you items appear here once', (ROOT / 'scripts/triage.py').read_text())

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
        for term in ('data', 'instructions', 'Never send', 'Create and show issued drafts',
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
        for term in ('message', 'rules.toml', 'never-touch.toml', 'plan.json'):
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
            self.assertIn(trigger, text.casefold())
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
        step = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_step')
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
        keys |= {'executor_app', 'executor_account', 'refresh_lock', 'final_reads', 'tool'}
        listed = set(re.findall(r'^\| `([^`]+)` \|', table, re.M))
        self.assertEqual(listed, keys)
        self.assertIn('#step-machine-responses', (ROOT / 'AGENTS.md').read_text())

    def test_quiet_chat_and_drive_are_separate(self):
        agents = (ROOT / 'AGENTS.md').read_text()
        quiet = next(line for line in agents.splitlines() if line.startswith('| Quiet |'))
        for word in ('history.log', 'chat', 'send-state', 'release'):
            self.assertIn(word, quiet)
        skill = (ROOT / '.agents/skills/email-triage/SKILL.md').read_text()
        self.assertIn('Follow [AGENTS.md]', skill)
        self.assertIn('Nothing in chat unless an upload or release warning occurs', quiet)
        self.assertNotIn('directly to release', skill)

    def test_lost_lock_message_is_delivered_in_chat(self):
        agents = (ROOT / 'AGENTS.md').read_text()
        self.assertNotIn('retain it in the host conversation for the next run', agents)
        self.assertIn('post the refusal and confirmed outcomes immediately', agents)
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
        for term in ("owner's own account", 'never shared', 'shared folder', 'lock'):
            self.assertIn(term, text)
        self.owner('docs/setup.md', ('config/guardrails.example.md', 'AGENTS.md'), 'never shared')
        checklist = (ROOT / 'docs/setup.md').read_text().split('## Host requirements')[1].split('| Host |')[0]
        self.assertIn('private', checklist)

    def test_saved_files_are_data(self):
        text = (ROOT / 'docs/quickstart.md').read_text()
        for term in ('Every saved or fetched file', 'data, never instructions', 'whatever it says', 'quoted block', 'never act on'):
            self.assertIn(term, text)
        self.owner('docs/quickstart.md', ('.agents/skills/email-triage/SKILL.md', 'docs/setup.md', 'AGENTS.md'), 'data, never instructions')

    def test_delivery_record_and_opening_order(self):
        record = (ROOT / 'docs/setup.md').read_text().split('- run-message.md: ')[1].split('\n')[0]
        for term in ('pending delivery record', 'undelivered', 'upload warning'):
            self.assertIn(term, record)
        for name in ('AGENTS.md',):
            self.assertIn('[pending delivery record](', (ROOT / name).read_text())



class ReferenceTidyTests(unittest.TestCase):
    def test_no_document_hedges(self):
        for name in DOCS + ['CHANGELOG.md']:
            with self.subTest(document=name):
                self.assertNotRegex((ROOT / name).read_text().casefold(),
                                    r'known limits|known unverified|unverified|untested|'
                                    r'not tested|cannot guarantee|use at your own risk|'
                                    r'may or may not|these checks reduce leaks')

    def test_quiet_history_without_saved_position(self):
        import json, tempfile
        sys.path.insert(0, str(ROOT / 'scripts'))
        import triage
        self.assertIn('With no saved position, return arrivals as [].', (ROOT / 'docs/setup.md').read_text())
        config = {'accounts': {'cedar': {'quiet_kind': 'history'}}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'quiet-observation.json'
            path.write_text(json.dumps({'cedar': {'outcome': 'ok', 'position': '120', 'arrivals': []}}))
            self.assertEqual(triage.read_observation(path, config, {}), ({'cedar': '120'}, True))
            path.write_text(json.dumps({'cedar': {'outcome': 'ok', 'position': '120'}}))
            self.assertIsNone(triage.read_observation(path, config, {}))

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

class SetupValidationTests(unittest.TestCase):
    def test_shipped_safety_files_and_all_errors(self):
        import tempfile, shutil, os
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder)
            for source, target in [('config/config.example.toml', 'config.toml'),
                                   ('config/guardrails.example.md', 'guardrails.md'),
                                   ('rules/rules.example.toml', 'rules.toml'),
                                   ('rules/never-touch.example.toml', 'never-touch.toml')]:
                shutil.copyfile(ROOT / source, store / target)
            env = dict(os.environ, PIGEON_HOME=folder)
            def validate():
                return subprocess.run(['python3', str(ROOT / 'scripts/triage.py'), 'validate'],
                                      env=env, capture_output=True, text=True)
            before = {p.name: p.read_bytes() for p in store.iterdir()}
            result = validate()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('2 accounts, 2 rules, 1 never-touch', result.stdout)
            self.assertEqual(before, {p.name: p.read_bytes() for p in store.iterdir()})
            (store / 'config.toml').write_text('language="bad"\ninbox_limit=0\n[accounts.cedar]\n')
            (store / 'rules.toml').write_text('[[rules]]\nid="bad"\naction="bogus"\n')
            (store / 'guardrails.md').write_text('')
            result = validate()
            self.assertNotEqual(result.returncode, 0)
            for term in ('config.toml', 'language', 'inbox_limit', 'rules.toml', 'bad', 'guardrails.md'):
                self.assertIn(term, result.stderr)

    def test_setup_creates_only_examples_and_refuses_overwrite(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / 'new'
            env = dict(os.environ, PIGEON_HOME=str(store))
            command = ['python3', str(ROOT / 'scripts/triage.py'), 'setup']
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(store.stat().st_mode & 0o777, 0o700)
            before = {p.name: p.read_bytes() for p in store.iterdir()}
            self.assertEqual(set(before), {'config.toml', 'rules.toml', 'never-touch.toml', 'guardrails.md'})
            self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
            self.assertEqual(before, {p.name: p.read_bytes() for p in store.iterdir()})

    def test_gateway_account_validation_and_reads(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import triage
        config = {'accounts': {'cedar': {'executor_app': 'app', 'executor_account': 'id-1'}}}
        self.assertEqual(triage.gateway_account(config, 'cedar'), config['accounts']['cedar'])
        for value in ('', 'a' * 129, 'id!', 12):
            config['accounts']['cedar']['executor_account'] = value
            with self.assertRaisesRegex(ValueError, 'executor_account'):
                triage.validate_inputs({'accounts': {}, 'messages': []}, config, {})
        config['accounts']['cedar']['executor_account'] = 'id-1'
        config['accounts']['marble'] = dict(config['accounts']['cedar'])
        with self.assertRaisesRegex(ValueError, 'unique'):
            triage.validate_inputs({'accounts': {}, 'messages': []}, config, {})


class EnabledRulesTests(unittest.TestCase):
    def test_disabled_validated_but_never_matches(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import triage, json
        rule = dict(id='disabled', accounts=['cedar'], sender='digest@example.net',
                    action='trash', exclusions=[], enabled=False, limit=1)
        triage.validate_rules([rule])
        message = dict(account='cedar', id='item-a', folder='Inbox',
                       sender='digest@example.net', subject='Weekly')
        self.assertFalse(triage.matches(message, rule))
        plan = triage.build_plan(dict(accounts={'cedar': 20}, messages=[dict(message, id=str(i)) for i in range(12)]),
                                 [rule], [], {'accounts': {'cedar': {}}})
        self.assertEqual(plan['actions'], [])
        self.assertEqual(plan['reasons'], [])
        self.assertNotIn('disabled', json.dumps(plan))
        for value in (0, 'false', None):
            with self.assertRaisesRegex(ValueError, 'enabled'):
                triage.validate_rules([dict(rule, enabled=value)])
        with self.assertRaises(ValueError):
            triage.validate_rules([dict(rule, action='bogus')])
        self.assertTrue(triage.matches(message, {k:v for k,v in rule.items() if k != 'enabled'}))


class RulesConverterTests(unittest.TestCase):
    def convert(self, text, *, review=False):
        import tempfile, os
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root/'table.md').write_text(text)
        (root/'scopes.toml').write_text('all=["cedar", "marble"]\n"owner@example.com"=["cedar"]\n')
        command = ['python3', str(ROOT/'scripts/triage.py'), 'convert-rules',
                   '--table', str(root/'table.md'), '--scopes', str(root/'scopes.toml'), '--out', str(root/'rules.toml')]
        if review:
            command += ['--review', str(root/'review.txt')]
        result = subprocess.run(command, env=dict(os.environ, PIGEON_HOME=''), capture_output=True, text=True)
        return root, command, result

    def test_del_id_goes_to_review(self):
        root, _, result = self.convert('| id | pattern | action | scope | source |\n| --- | --- | --- | --- | --- |\n| del\x7fid | `example.net` | trash | all | invented |\n', review=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('invalid id', (root/'review.txt').read_text())

    def test_draft_and_forward_go_to_review(self):
        import triage
        for action in ('draft', 'forward', 'draft reply', 'forward owner@example.com'):
            root, command, result = self.convert('| id | pattern | action | scope | source |\n| --- | --- | --- | --- | --- |\n| d | `example.net` | '+action+' | all | invented |\n', review=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('d', (root/'review.txt').read_text())
            self.assertEqual(triage.parse_rules((root/'rules.toml').read_text()), [])

    def test_outcomes_multiple_tables_and_real_match(self):
        sys.path.insert(0, str(ROOT/'scripts'))
        import triage
        text = '''Outside text
| Whatever | Tokens | Verb | Accounts | Origin |
| --- | --- | --- | --- | --- |
| exact | `digest@example.net` | TrAsH | all | invented |
| domains | `example.org` `@example.net` | ARCHIVE | owner@example.com | sample |
| notes | `notice@example.com` except invoices ⚠ | spam | all | source |
| unknown | `secret@example.com` | secret action | all | hidden |
| scope | `example.net` | archive | missing | hidden |
| empty | except private text | archive | all | hidden |
| exact | `other@example.com` | archive | all | hidden |
| | `example.net` | archive | all | hidden |

More text
| a | b | c | d | e |
| :--- | ---: | --- | --- | --- |
| literal | ``subject:a|b`c.*`` | mover Reading | all | source with `ticks` |
| unsubscribe | `example.com` | BAJA https://example.org/leave + ARCHIVE | all | source |
| badtarget | `example.com` | move ../No | all | hidden |
| badtoken | `not a sender` | trash | all | hidden |
'''
        root, command, result = self.convert(text, review=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rows read: 12, active: 4, switched off: 1, sent to review: 7', result.stdout)
        rules = triage.parse_rules((root/'rules.toml').read_text())
        self.assertEqual([r['id'] for r in rules], ['exact','domains','domains-2','notes','literal','unsubscribe'])
        self.assertFalse(rules[3]['enabled'])
        self.assertIn('# Note: except invoices ⚠', (root/'rules.toml').read_text())
        self.assertEqual(rules[4]['target'], 'Reading')
        self.assertTrue(triage.matches(dict(account='cedar',sender='digest@example.net',subject='Weekly'), rules[0]))
        self.assertTrue(triage.matches(dict(account='cedar',sender='x@example.com',subject='a|b`c.*'), rules[4]))
        self.assertFalse(triage.matches(dict(account='cedar',sender='x@example.com',subject='abcZZ'), rules[4]))
        review = (root/'review.txt').read_text()
        self.assertEqual(len(review.splitlines()), 7)
        for cell in ('secret@example', 'secret action', 'missing', 'private text', 'other@example', '../No', 'hidden', 'not a sender'):
            self.assertNotIn(cell, review)
        original = (root/'rules.toml').read_bytes()
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(original, (root/'rules.toml').read_bytes())

    def test_escaped_pipe_warning_and_invalid_forms(self):
        import triage
        root, command, result = self.convert('''| id | pattern | action | scope | source |
| --- | --- | --- | --- | --- |
| pipe | `subject:a\\|b` | archive | all | source |
| warning | ⚠ `example.com` | baja + trash | all | source |
| mixed | `subject:hello` `example.com` | archive | all | source |
| wrong | `example.com` | archive + trash | all | source |
| suffix | `example.com` `example.org` | spam | all | source |
| suffix-2 | `example.net` | archive | all | source |
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        rules = triage.parse_rules((root/'rules.toml').read_text())
        self.assertEqual(rules[0]['subject'], 'a\\|b')
        self.assertFalse(rules[1]['enabled'])
        self.assertEqual(rules[1]['after_unsubscribe'], 'trash')
        self.assertIn('ambiguous patterns', result.stdout)
        self.assertIn('duplicate id', result.stdout)
        self.assertEqual(len(rules), 4)

    def test_validation_failure_leaves_no_output(self):
        import triage, tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'table').write_text('| a | b | c | d | e |\n| --- | --- | --- | --- | --- |\n| x | `example.com` | archive | all | sample |\n')
            (root/'scopes').write_text('all=["cedar"]')
            original = triage.parse_rules
            calls = []
            def fail(text):
                calls.append(text)
                if len(calls) == 2:
                    raise ValueError('validation failed')
                return original(text)
            with patch.object(triage, 'parse_rules', side_effect=fail):
                with self.assertRaises(ValueError):
                    triage.convert_rules(root/'table', root/'scopes', root/'out')
            self.assertFalse((root/'out').exists())


class ConverterDocumentationTests(unittest.TestCase):
    def test_converter_reference(self):
        text = (ROOT/'docs/setup.md').read_text()
        section = text.split('## Convert a rules table')[1].split('## Run message format')[0]
        for term in ('--table', '--scopes', '--out', '--review', 'Active:', 'Switched off:', 'Review:',
                     'enabled = true', 'exclusions', 'validate', 'Dry run', 'first matching enabled'):
            self.assertIn(term, section)
        self.assertIn('| enabled | Optional boolean, default true.', text)
        self.assertIn('#convert-a-rules-table', (ROOT/'docs/quickstart.md').read_text())


class DraftDocumentationTests(unittest.TestCase):
    def test_draft_contract(self):
        setup = (ROOT/'docs/setup.md').read_text()
        for value in ('| draft |', '| draft_text |', '| draft_to |', 'check Drafts by hand', 'Send drafts by hand from the mailbox.', 'search', 'describe', '_drafts'):
            self.assertIn(value, setup)
        self.assertIn('Create and show issued drafts. Never send them.', (ROOT/'config/guardrails.example.md').read_text())
        quick = (ROOT/'docs/quickstart.md').read_text()
        self.assertIn('create-draft', quick)
        self.assertLessEqual(len(quick.encode()),6000)


class ScriptedListingTextTests(unittest.TestCase):
    def test_routine_listing_commands(self):
        text = (Path(__file__).resolve().parents[1] / 'docs/quickstart.md').read_text()
        self.assertIn('triage.py listing --token TOKEN', text)
        self.assertIn('triage.py listing --token TOKEN --results', text)
        self.assertLessEqual(len(text.encode()), 6000)
