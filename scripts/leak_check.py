#!/usr/bin/env python3
"""Fail closed on unreviewed paths and private-looking values. Python 3.11+."""
# Known limits: zero-width and full-width characters, URL-encoded values, and
# values split across lines need the deny-list scan and publication audit.
# Commit messages are first checked at push time.
import argparse
import ipaddress
from pathlib import Path
import re
import subprocess
import sys

ALLOWLIST = set('''CHANGELOG.md README.md LICENSE AGENTS.md CLAUDE.md .gitignore
.agents/skills/email-triage/SKILL.md .claude/skills docs/setup.md
config/config.example.toml config/guardrails.example.md
rules/rules.example.toml rules/never-touch.example.toml
scripts/unsubscribe.py tests/test_unsubscribe.py scripts/triage.py scripts/execute.py tests/mock_provider.py tests/test_execute.py scripts/leak_check.py hooks/pre-commit hooks/pre-push
.github/workflows/check.yml tests/test_leak_check.py tests/test_matcher.py
tests/test_lock.py tests/test_kit_text.py tests/e2e.sh tests/fixtures/inbox.json
tests/fixtures/expected-report.md'''.split())
IDENTITY = '72031190+ricardo-alternova' + '@' + 'users.noreply.github.com'
REMOTES = {'git' + '@' + 'github.com:ricardo-alternova/pigeon.git',
           'https://github.com/ricardo-alternova/pigeon'}
PATTERNS = {
    'account-id': r'(?<![0-9a-z])acc_[0-9a-f]{8,}',
    'uuid': r'\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b',
    'home-path': r'(?:/(?:home|Users)/[^/\s]+|[a-z]:\\Users\\[^\\\s]+)',
    'phone': r'(?<![\w.])(?:\+\d[\d ()-]{7,}\d|\(?\d{3}\)?[ -]\d{3}[ -]\d{4})(?!\w|\.\d)',
}


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL)


def rules(text, terms=(), metadata=False, allowed=()):
    found = {name for name, pattern in PATTERNS.items()
             if re.search(pattern, re.sub(r'https?://[^\s<>"\'`,;?#()\[\]{}|\\]+', '', text) if name == 'home-path' else text, re.I)}
    original = text
    # SCP syntax contains @ but is a repository locator, not an email address.
    public = r'(?:git' r'@github\.com:|(?:(?:https?|git)' r'://|ssh' r'://(?:git' r'@)?)github\.com/)ricardo-alternova/pigeon(?:\.git)?(?=$|[/\s\x22\x27<>]|[.,;:!?](?=$|\s))[^\s\x22\x27<>]*'
    text = re.sub(public, '', text)
    for match in re.finditer(r'@([\w-]+(?:\.[\w-]+)+)', text):
        # Dotted decorators and instruction imports have no local part.
        if match.start() == 0 or text[match.start() - 1].isspace():
            continue
        domain = match[1].lower().rstrip('.')
        if metadata and re.search(r'(?:^|[<\s])' + r'(?:noreply@github\.com|[0-9]+\+[\w-]+@users\.noreply\.github\.com)' + r'$', text[:match.end()]):
            continue
        if domain not in {'example.com', 'example.org', 'example.net'} and not domain.endswith('.invalid'):
            found.add('address')
    if re.search(r'(?:git' r'://|ssh' r'://|git' r'@)[^\s\x22\x27<>]+|https?://[^/\s\x22\x27<>]*@|https?://[^\s\x22\x27<>]+\.git(?![\w-])', text):
        found.add('remote-url')
    for match in re.finditer(r'(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?!\w|\.\d)', text):
        if re.search(r'\bversion\s+$', text[:match.start()], re.I):
            continue
        try:
            ipaddress.IPv4Address(match[0])
            found.add('ipv4')
        except ipaddress.AddressValueError:
            pass
    masked = original.casefold()
    for value in sorted(allowed, key=len, reverse=True):
        masked = masked.replace(value.casefold(), '')
    if any(term in masked for term in terms):
        found.add('deny-term')
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--git-config', action='store_true', help='publication-only scan of local Git config')
    parser.add_argument('--staged', action='store_true')
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--deny')
    parser.add_argument('--push', action='store_true', help='read pre-push ref lines from stdin')
    args = parser.parse_args()
    if args.staged and (args.history or args.push):
        parser.error('--staged expected without --history or --push')
    if args.push and not args.history:
        parser.error('--push expected with --history')
    terms = []
    allowed = []
    if args.deny is not None:
        deny = Path(args.deny)
        if not args.deny or not deny.is_dir():
            parser.error('deny folder must exist and be a folder')
        if any(not p.name.endswith('.txt') and not p.name.startswith('.') for p in deny.iterdir()):
            parser.error('deny folder expected only <category>.txt files')
        for path in sorted(deny.glob('*.txt')):
            if path.name == 'allowed-public.txt':
                allowed.extend(line.strip() for line in path.read_text(encoding='utf-8-sig').splitlines()
                               if line.strip() and not line.lstrip().startswith('#'))
            else:
                terms.extend(line.strip().casefold() for line in path.read_text(encoding='utf-8-sig').splitlines()
                             if line.strip() and not line.lstrip().startswith('#'))
        if not terms:
            parser.error('deny folder contains zero terms')
    failed = False

    def scan(label, data, metadata=False):
        nonlocal failed
        try:
            text = data.decode('utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8')
            if '\0' in text:
                raise ValueError('NUL in text')
        except (UnicodeError, ValueError):
            print(f'{label}:1: non-utf8')
            failed = True
            return
        for number, line in enumerate(text.splitlines(), 1):
            for rule in sorted(rules(line, terms, metadata, allowed)):
                print(f'{label}:{number}: {rule}')
                failed = True

    def tree(revision=None):
        nonlocal failed
        if revision:
            records = git('ls-tree', '-rz', revision).split(b'\0')
        else:
            records = git('ls-files', '--stage', '-z').split(b'\0')
        entries = []
        for record in filter(None, records):
            header, raw = record.split(b'\t', 1)
            fields = header.split()
            mode, oid = fields[0], fields[2] if revision else fields[1]
            name = raw.decode('utf-8', errors='strict')
            label = '[redacted-path]' if rules(name, terms, allowed=allowed) else name
            scan(label, raw)
            if name not in ALLOWLIST:
                print(f'{label}:1: path-allowlist')
                failed = True
                continue
            if not revision and not args.staged:
                path = Path(name)
                if any(parent.is_symlink() for parent in path.parents):
                    print(f'{label}:1: symlink-parent')
                    failed = True
                    continue
                if path.is_symlink():
                    import os
                    data = os.readlink(path).encode()
                    mode = b'120000'
                else:
                    data = path.read_bytes()
                    mode = b'100644'
            else:
                data = git('cat-file', 'blob', oid.decode())
            entries.append((name, mode, data, label))
        names = {name for name, _, _, _ in entries}
        for name, mode, data, label in entries:
            if name == '.claude/skills' and mode != b'120000':
                print(f'{label}:1: symlink-required')
                failed = True
            if mode == b'120000':
                if (name != '.claude/skills' or data != b'../.agents/skills'
                        or '.agents/skills/email-triage/SKILL.md' not in names):
                    print(f'{label}:1: symlink')
                    failed = True
            scan(label, data)

    if args.git_config:
        section = b''
        config = Path(git('rev-parse', '--git-path', 'config').decode().strip()).read_bytes()
        for number, line in enumerate(config.splitlines(), 1):
            if header := re.match(rb'\s*\[\s*([\w.-]+)', line):
                section = header[1].lower()
            # Ruling 21: only [user] email may carry a public commit identity.
            scan(f'git-config:{number}', line, section == b'user' and re.match(rb'\s*email\s*=', line, re.I) is not None)
    revisions = set()
    tags = set()
    if args.push:
        for line in sys.stdin:
            local_ref, local, remote_ref, remote = line.split()
            if set(local) == {'0'}:  # Deleted ref.
                continue
            oid = local
            while git('cat-file', '-t', oid).strip() == b'tag':
                tags.add(oid)
                oid = git('cat-file', 'tag', oid).splitlines()[0].split()[1].decode()
            if git('cat-file', '-t', oid).strip() != b'commit':
                raise ValueError('pushed object is not a commit')
            selection = [oid]
            if set(remote) != {'0'}:
                try:
                    git('cat-file', '-e', remote + '^{commit}')
                    selection.append('^' + remote)
                except subprocess.CalledProcessError:
                    pass  # Unknown remote tip: scan everything reachable locally.
            revisions.update(git('rev-list', *selection).decode().splitlines())
    else:
        tree()
        if args.history:
            revisions.update(git('rev-list', '--all').decode().splitlines())
            for record in git('for-each-ref', 'refs/tags', '--format=%(objecttype) %(objectname)').decode().splitlines():
                kind, oid = record.split()
                while kind == 'tag':
                    tags.add(oid)
                    oid = git('cat-file', 'tag', oid).splitlines()[0].split()[1].decode()
                    kind = git('cat-file', '-t', oid).decode().strip()
    for revision in sorted(revisions):
        tree(revision)
        # Raw objects cannot be rewritten by mailmap or diff/textconv settings.
        header = True  # Identities are accepted in headers, never in the message.
        for number, line in enumerate(git('cat-file', 'commit', revision).splitlines(), 1):
            header = header and bool(line)
            scan(f'git-history-{revision}:{number}', line,
                 header and line.startswith((b'author ', b'committer ')))
    for oid in sorted(tags):
        header = True
        for number, line in enumerate(git('cat-file', 'tag', oid).splitlines(), 1):
            header = header and bool(line)
            scan(f'git-tag-{oid}:{number}', line, header and line.startswith(b'tagger '))
    return int(failed)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError):
        sys.exit('leak-check: unable to complete scan (details redacted)')
