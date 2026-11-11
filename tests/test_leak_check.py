"""Offline guard checks using invented data and isolated Git repositories."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('leak_check', ROOT / 'scripts/leak_check.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class Guards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        for name in ('scripts/leak_check.py', 'hooks/pre-commit', 'hooks/pre-push', '.gitignore'):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, path)
        self.git('init', '-q', '--initial-branch=main')
        self.git('config', 'user.name', 'Example Contributor')
        self.git('config', 'user.email', 'author@example.com')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.hooksPath', 'hooks')
        self.git('add', '.')

    def run_command(self, *args):
        return subprocess.run(args, cwd=self.repo, env=self.env, capture_output=True, text=True)

    def git(self, *args):
        result = self.run_command('git', *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def scan(self, *args):
        return self.run_command('python3', 'scripts/leak_check.py', *args)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def candidates(self):
        return {
            'address': 'fictional' + '@' + 'private.example.org',
            'account-id': 'acc_' + 'abcdef12',
            'uuid': '-'.join(['12345678', 'abcd', '1234', 'abcd', '123456789abc']),
            'home-path': '/' + 'home/fictional/',
            'remote-url': 'ssh' + '://example.com/other.git',
            'ipv4': '.'.join(['192', '0', '2', '44']),
            'phone': '+1' + ' 202 ' + '555 ' + '0147',
        }

    def test_patterns_and_redaction(self):
        for rule, value in self.candidates().items():
            with self.subTest(rule=rule):
                self.write('README.md', value)
                self.git('add', 'README.md')
                for args in [(), ('--staged',)]:
                    result = self.scan(*args)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(rule, result.stdout)
                    self.assertNotIn(value, result.stdout + result.stderr)

    def test_instruction_import_address_exception(self):
        self.assertNotIn('address', checker.rules('@' + 'AGENTS.md'))
        for value in ('person' + '@' + 'private.example.org',
                      'person' + '@' + 'AGENTS.md', '"person"' + '@' + 'AGENTS.md',
                      'person(c)' + '@' + 'AGENTS.md', '<' + '@' + 'AGENTS.md'):
            with self.subTest(value=value):
                self.assertIn('address', checker.rules(value))

    def test_revision_guards(self):
        for value in ('@' + 'unittest.skipIf(False)', '@' + 'mock.patch.object(x)', 'https://api.example.org/users/octo', 'https://example.org/home/guide'):
            self.assertFalse(checker.rules(value))
        for value in ('https://example.org/view?p=/' + 'home/fictional/x', '{"u":"https://example.org","p":"/' + 'home/fictional"}',
                      'https://example.org,/' + 'home/fictional/x'):
            self.assertIn('home-path', checker.rules(value), value)
        url = 'https://example.org/team/kit.' + 'git'
        for value in ('"' + url + '"', '[kit](' + url + ')', "'" + url + "'"):
            self.assertIn('remote-url', checker.rules(value), value)
        self.assertFalse(checker.rules('https://example.org/team/.github/guide'))
        for scheme in ('http', 'https'):
            value = scheme + '://example.org/team/kit.git'
            self.write('README.md', value)
            self.git('add', '.')
            self.assertIn('remote-url', self.scan().stdout)
            self.assertIn('remote-url', self.scan('--staged').stdout)
            self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Remote probe')
            self.assertIn('remote-url', self.scan('--history').stdout)
        self.write('README.md', 'clean')
        self.git('add', '.')
        self.git('config', 'audit.fixture', self.candidates()['address'])
        self.assertEqual(self.scan().returncode, 0)
        self.assertIn('git-config', self.scan('--git-config').stdout)

    def test_public_commit_identities(self):
        self.env.update(GIT_AUTHOR_NAME='Example', GIT_AUTHOR_EMAIL='4242+example' + '@' + 'users.noreply.github.com',
                        GIT_COMMITTER_NAME='GitHub', GIT_COMMITTER_EMAIL='noreply' + '@' + 'github.com')
        self.git('commit', '-qm', 'Public contributor')
        self.assertEqual(self.scan('--history').returncode, 0)
        self.git('commit', '-q', '--allow-empty', '-m', 'Subject', '-m', 'author Example <' + self.env['GIT_AUTHOR_EMAIL'] + '>')
        self.assertIn('address', self.scan('--history').stdout)
        self.git('reset', '-q', '--hard', 'HEAD~1')
        self.git('config', 'user.email', self.env['GIT_AUTHOR_EMAIL'])
        self.assertEqual(self.scan('--git-config').returncode, 0, self.scan('--git-config').stdout)
        for key, value in [('user.email', self.candidates()['address']), ('audit.email', self.env['GIT_AUTHOR_EMAIL'])]:
            self.git('config', key, value)
            self.assertIn('address', self.scan('--git-config').stdout, key)
            self.git('config', 'user.email', self.env['GIT_AUTHOR_EMAIL'])
        for address in (self.env['GIT_AUTHOR_EMAIL'], self.env['GIT_COMMITTER_EMAIL']):
            self.assertIn('address', checker.rules(address))

    def test_hook_deny_environment(self):
        folder = Path(self.temp.name) / 'deny'
        folder.mkdir()
        (folder / 'names.txt').write_text('Fictional Nebula')
        self.env['PIGEON_DENY'] = str(folder)
        self.write('README.md', 'Fictional Nebula')
        self.git('add', '.')
        result = self.run_command('git', 'commit', '-qm', 'Deny probe')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('deny-term', result.stdout + result.stderr)
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Deny probe')
        head = self.git('rev-parse', 'HEAD').strip()
        result = subprocess.run(['sh', 'hooks/pre-push'], cwd=self.repo, env=self.env,
                                input='refs/heads/main ' + head + ' refs/heads/main ' + '0' * 40 + '\n', capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('deny-term', result.stdout)

    def test_allowed_forms(self):
        forms = ['a@example.com', 'b@example.org', 'c@example.net', 'd@demo.invalid',
                 'Python 3.11.8', 'v1.2.3', 'v1.2.3.4', '2026-11-10',
                 '12345', 'acc_short', 'home/example/', 'Users/example/', *checker.REMOTES]
        self.write('README.md', '\n'.join(forms))
        self.git('add', 'README.md')
        self.assertEqual(self.scan('--staged').returncode, 0)
        self.git('commit', '-qm', 'Public examples')
        self.assertEqual(self.scan('--history').returncode, 0)
        self.assertTrue(checker.rules(checker.IDENTITY))
        self.assertFalse(checker.rules(checker.IDENTITY, metadata=True))

    def test_outside_path_hook(self):
        self.write('private/probe.txt', 'invented')
        self.assertEqual(self.git('check-ignore', 'private/probe.txt').strip(), 'private/probe.txt')
        self.git('add', '-f', 'private/probe.txt')
        result = self.run_command('git', 'commit', '-qm', 'Probe ignored file')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('path-allowlist', result.stdout + result.stderr)

    def test_address_hook_and_index(self):
        value = self.candidates()['address']
        self.write('README.md', value)
        self.git('add', 'README.md')
        self.write('README.md', 'safe unstaged replacement')
        self.assertEqual(self.scan().returncode, 0)
        result = self.run_command('git', 'commit', '-qm', 'Probe staged content')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('address', result.stdout + result.stderr)
        self.assertNotIn(value, result.stdout + result.stderr)

    def test_symlinks(self):
        self.write('.agents/skills/email-triage/SKILL.md', 'Public skill')
        (self.repo / '.claude').mkdir()
        link = self.repo / '.claude/skills'
        link.symlink_to('../.agents/skills')
        self.git('add', '.')
        self.assertEqual(self.scan('--staged').returncode, 0)
        self.git('rm', '--cached', '.agents/skills/email-triage/SKILL.md')
        self.assertIn('symlink', self.scan('--staged').stdout)
        self.git('add', '.agents/skills/email-triage/SKILL.md')
        link.unlink()
        link.symlink_to('../docs')
        self.git('add', '.claude/skills')
        self.assertIn('symlink', self.scan('--staged').stdout)
        link.unlink()
        self.write('.claude/skills', 'ordinary file')
        (self.repo / 'README.md').symlink_to('LICENSE')
        self.git('add', '.')
        self.assertIn('symlink', self.scan('--staged').stdout)

    def test_deny(self):
        folder = Path(self.temp.name) / 'deny'
        folder.mkdir()
        (folder / 'names.txt').write_text('# comment\n\nFictional Nebula\nSample\n')
        (folder / 'allowed-public.txt').write_text('\ufeff# comment\r\nPublic sample\r\n')
        self.write('README.md', 'Public sample')
        self.git('add', 'README.md')
        self.assertEqual(self.scan('--deny', str(folder)).returncode, 0)
        self.write('README.md', 'sample')
        self.assertIn('deny-term', self.scan('--deny', str(folder)).stdout)
        value = self.candidates()['ipv4']
        (folder / 'allowed-public.txt').write_text('Public sample\n' + value + '\n')
        self.write('README.md', value)
        self.assertIn('ipv4', self.scan('--deny', str(folder)).stdout)
        self.write('README.md', 'prefix FICTIONAL NEBULA suffix')
        result = self.scan('--deny', str(folder))
        self.assertEqual(result.returncode, 1)
        self.assertIn('deny-term', result.stdout)
        self.assertNotIn('NEBULA', result.stdout)

    def test_history_and_config(self):
        value = self.candidates()['address']
        self.write('README.md', value)
        self.git('add', '.')
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Invented historical content')
        self.write('README.md', 'clean')
        self.git('add', '.')
        self.git('commit', '-qm', 'Clean tree')
        self.assertEqual(self.scan().returncode, 0)
        result = self.scan('--history')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(value, result.stdout + result.stderr)
        self.git('config', 'remote.invented.url', 'https' + '://example.org/elsewhere')
        self.assertNotIn('git-config', self.scan('--history').stdout)

    def test_filename_and_metadata_redaction(self):
        value = self.candidates()['address']
        self.write(value, 'invented')
        self.git('add', '-f', value)
        result = self.scan('--staged')
        self.assertEqual(result.returncode, 1)
        self.assertIn('[redacted-path]', result.stdout)
        self.assertNotIn(value, result.stdout + result.stderr)
        self.git('reset', '--', value)
        self.git('config', 'user.email', value)
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Metadata probe')
        result = self.scan('--history')
        self.assertEqual(result.returncode, 1)
        self.assertIn('git-history', result.stdout)
        self.assertNotIn(value, result.stdout + result.stderr)

    def test_sentence_final_values(self):
        for value in (self.candidates()['ipv4'], self.candidates()['phone'], '202-555-' + '0147'):
            self.assertTrue(checker.rules('Contact ' + value + '.'))

    def test_non_utf8(self):
        for encoding in ('utf-16', 'utf-8'):
            value = self.candidates()['address']
            (self.repo / 'README.md').write_bytes(value.encode(encoding))
            self.git('add', 'README.md')
            self.assertIn('address', self.scan('--staged').stdout)
        for data in (b'bad\xff', b'a\0b'):
            (self.repo / 'README.md').write_bytes(data)
            self.git('add', 'README.md')
            self.assertIn('non-utf8', self.scan('--staged').stdout)

    def test_fork_config(self):
        self.git('commit', '-qm', 'Initial')
        self.git('remote', 'add', 'origin', 'https' + '://example.org/contributor/pigeon.git')
        self.git('config', 'http.https://example.org/.extraheader', 'AUTHORIZATION: basic invented')
        self.assertEqual(self.scan('--history').returncode, 0)

    def test_tag_message_and_identity(self):
        self.git('commit', '-qm', 'Initial')
        for identity in (False, True):
            if identity:
                self.git('tag', '-d', 'probe')
            args = ['-c', 'user.email=' + self.candidates()['address']] if identity else []
            self.git(*args, 'tag', '-a', 'probe', '-m', 'Release' if identity else self.candidates()['address'])
            result = self.scan('--history')
            self.assertEqual(result.returncode, 1)
            self.assertIn('git-tag', result.stdout)
            self.assertNotIn(self.candidates()['address'], result.stdout)

    def test_mailmap_cannot_hide_identity(self):
        value = self.candidates()['address']
        self.git('-c', 'user.email=' + value, 'commit', '-qm', 'Initial')
        self.write('.mailmap', 'Example <author@example.com> <' + value + '>')
        self.git('config', 'log.mailmap', 'true')
        self.assertIn('address', self.scan('--history').stdout)

    def test_url_forms(self):
        for value in ('https' + '://example.com/guide', *[
                prefix + 'github.com' + suffix + 'ricardo-alternova/pigeon' + ending
                for prefix, suffix in [('https://', '/'), ('ssh' + '://git' + '@', '/'), ('git' + '@', ':')]
                for ending in ('', '.git', '/issues/1', '.', '.git.')]):
            self.assertFalse(checker.rules(value), value)
        for value in ('ssh' + '://example.org/private', 'git' + '@example.org:owner/repo',
                      'git' + '://example.org/repo', 'https' + '://token@example.org/repo',
                      'https' + '://user:pass@example.org/repo'):
            self.assertIn('remote-url', checker.rules(value))
        self.assertIn('remote-url', checker.rules('https' + '://token' + '@' + 'github.com/ricardo-alternova/pigeon'))

    def test_bare_integers(self):
        for value in ('1731283200', '1731283200000', '12345678901234567890'):
            self.assertFalse(checker.rules(value))

    def test_deny_bom_and_invalid_folders(self):
        folder = Path(self.temp.name) / 'deny'
        folder.mkdir()
        self.write('README.md', 'fictional nebula')
        self.git('add', 'README.md')
        for path in (folder / 'missing', self.repo / 'README.md', folder):
            result = self.scan('--deny', str(path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('deny folder', result.stderr)
        (folder / 'names.txt').write_bytes(b'\xef\xbb\xbfFictional Nebula\r\n')
        self.assertIn('deny-term', self.scan('--deny', str(folder)).stdout)
        (folder / 'places.text').write_text('other')
        for path in ('', str(folder)):
            result = self.scan('--deny', path)
            self.assertEqual(result.returncode, 2, path)
            self.assertIn('deny folder', result.stderr)

    def test_path_account_version_and_long_line(self):
        for value in ('/' + 'home/fictional', 'C:' + chr(92) + 'Users' + chr(92) + 'fictional'):
            self.assertIn('home-path', checker.rules(value))
        self.assertIn('account-id', checker.rules('user_acc_' + 'abcdef12'))
        self.assertFalse(checker.rules('version ' + '.'.join(['1', '2', '3', '4'])))
        self.assertIn('ipv4', checker.rules(self.candidates()['ipv4']))
        start = time.monotonic()
        self.assertFalse(checker.rules('a' * 80000))
        self.assertLess(time.monotonic() - start, 1)

    def test_push_ref_updates(self):
        self.git('commit', '-qm', 'Initial')
        base = self.git('rev-parse', 'HEAD').strip()
        bare = Path(self.temp.name) / 'remote.git'
        self.git('init', '--bare', '-q', str(bare))
        self.git('push', str(bare), 'HEAD:refs/heads/main')
        self.write('README.md', self.candidates()['address'])
        self.git('add', 'README.md')
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Probe')
        leak = self.git('rev-parse', 'HEAD').strip()
        self.git('reset', '--hard', base)
        result = self.run_command('git', 'push', str(bare), leak + ':refs/heads/probe')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('address', result.stdout + result.stderr)
        self.git('push', str(bare), 'HEAD:refs/heads/clean', 'HEAD:refs/heads/second')
        self.git('push', str(bare), ':refs/heads/clean')
        self.write('README.md', 'clean')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'Clean update')
        self.git('push', str(bare), 'HEAD:refs/heads/main')
        # An unrelated local ref must not poison a clean ref update.
        self.git('branch', 'unrelated', leak)
        head = self.git('rev-parse', 'HEAD').strip()
        updates = 'refs/heads/main ' + head + ' refs/heads/main ' + 'f' * 40 + '\n'
        result = subprocess.run(['sh', 'hooks/pre-push'], cwd=self.repo, env=self.env,
                                input=updates, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.git('tag', '-a', 'private-tag', '-m', self.candidates()['address'])
        self.assertNotEqual(self.run_command('git', 'push', str(bare), 'private-tag').returncode, 0)

    def test_wiring_and_allowlist_ignore(self):
        for name in ('hooks/pre-commit', 'hooks/pre-push', '.github/workflows/check.yml'):
            self.assertIn('scripts/leak_check.py', (ROOT / name).read_text())
        for name in checker.ALLOWLIST:
            self.assertNotEqual(self.run_command('git', 'check-ignore', '--no-index', name).returncode, 0, name)
        self.assertIn('--staged', (ROOT / 'hooks/pre-commit').read_text())
        self.assertIn('--history', (ROOT / 'hooks/pre-push').read_text())


if __name__ == '__main__':
    unittest.main()

class TypoGuards(unittest.TestCase):
    setUp = Guards.setUp
    run_command = Guards.run_command
    git = Guards.git
    scan = Guards.scan
    def test_missing_deny_hooks(self):
        for value in (str(self.repo / 'missing'), str(self.repo / '.gitignore')):
            self.env['PIGEON_DENY'] = value
            for hook in ('pre-commit', 'pre-push'):
                result = self.run_command('sh', 'hooks/' + hook)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('PIGEON_DENY expected an existing folder', result.stderr)

    def test_unknown_flags_and_combinations(self):
        for args in (('--hist',), ('--denny', 'missing'), ('--staged', '--history'),
                     ('--staged', '--push'), ('--push',)):
            self.assertNotEqual(self.scan(*args).returncode, 0, args)

    def test_public_tagger_header_only(self):
        self.git('commit', '-qm', 'Initial')
        address = '4242+example' + '@' + 'users.noreply.github.com'
        self.git('-c', 'user.email=' + address, 'tag', '-a', 'public', '-m', 'Release')
        self.assertEqual(self.scan('--history').returncode, 0)
        self.git('tag', '-d', 'public')
        for text in (address, 'tagger Example <' + address + '>'):
            self.git('tag', '-a', 'probe', '-m', text)
            self.assertIn('address', self.scan('--history').stdout)
            self.git('tag', '-d', 'probe')
