#!/usr/bin/env bash
# Offline fresh-clone proof. Scratch is removed on every exit.
set -eu
source_repo=$(git rev-parse --show-toplevel)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/pigeon-e2e.XXXXXX")
trap 'python3 -c "import shutil, sys; shutil.rmtree(sys.argv[1])" "$scratch"' EXIT
git clone --quiet --no-hardlinks "$source_repo" "$scratch/clone"
# Test the current candidate, including uncommitted instruction changes.
git -C "$source_repo" diff HEAD --binary > "$scratch/candidate.patch"
git -C "$scratch/clone" apply --allow-empty "$scratch/candidate.patch"
cd "$scratch/clone"
export PIGEON_HOME="$PWD/private/demo"
python3 - <<'PY' > "$scratch/quick-start.sh"
from pathlib import Path
text = Path('README.md').read_text()
block = text.split('<!-- quick-start -->\n```bash\n')[1].split('\n```\n<!-- /quick-start -->')[0]
print(block)
PY
env -i PATH="$PATH" bash "$scratch/quick-start.sh" > "$scratch/output"
cmp "$PIGEON_HOME/report.md" tests/fixtures/expected-report.md
test ! -e "$PIGEON_HOME/run.lock"
# Execute the published AGENTS command blocks in separate empty environments.
env -i PATH="$PATH" TMPDIR="$scratch" PIGEON_HOME="$PIGEON_HOME" python3 - <<'PYCLI'
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess

store = Path(os.environ['PIGEON_HOME'])
blocks = re.findall(r'```bash\n(.*?)\n\s*```', Path('AGENTS.md').read_text(), re.S)
assert len(blocks) == 15
def command(index, token='', observation=None, first=False):
    text = blocks[index + (2 if index else 0)].replace('/absolute/private-store', str(store)).replace('TOKEN', token)
    if first:
        text += ' --first-run'
    if observation is not None:
        text = text.replace('{"outcome":"ok","inbox_ids":["id-2","id-3"]}',
                            json.dumps(observation))
    return subprocess.run(['bash', '-eu', '-c', text], check=True, text=True,
                          capture_output=True,
                          env={'PATH': os.environ['PATH']})

# A private fixture uses every accepted rule key and action and listing key.
# Eleven body matches prove limit=12 overrides the default ten.
rules = """
[[rules]]
id="dated"
accounts=["cedar"]
sender="billing@example.org"
domain="example.org"
subject="Invoice"
body="paid"
has_attachment=true
exclusions=["retain"]
action="move"
target="Bills/{year}/{month}"
limit=12

[[rules]]
id="question"
accounts=["cedar"]
subject="Renewal"
action="report"
question="Keep this renewal?"

[[rules]]
id="trash"
accounts=["cedar"]
subject="Discard"
action="trash"
exclusions=[]

[[rules]]
id="archive"
accounts=["cedar"]
subject="Receipt"
action="archive"

[[rules]]
id="spam"
accounts=["cedar"]
subject="Offer"
action="spam"

[[rules]]
id="unsubscribe"
accounts=["cedar"]
subject="Newsletter"
action="unsubscribe"
after_unsubscribe="archive"
"""
(store/'rules.toml').write_text(rules)
messages = []
for i, subject in enumerate(['Invoice'] * 11 + ['Renewal', 'Discard', 'Receipt', 'Offer', 'Newsletter']):
    messages.append(dict(account='cedar', id='item-'+str(i), folder='Inbox',
                         sender='billing@example.org', sender_name='Billing', subject=subject,
                         has_attachment=True, spam=False, phishing=False, body='Paid',
                         date='2026-03-31T23:00:00-05:00',
                         unsubscribe_url='https://example.org/unsubscribe',
                         unsubscribe_one_click=True))
listing = dict(accounts={'cedar': 200, 'marble': 0}, messages=messages,
               folders={'cedar': [], 'marble': []})
(store/'inbox.json').write_text(json.dumps(listing))
# Remembered sender prevents all network contact while exercising the exact
# local command returned by next. Transport behavior has its own offline suite.
key = 'ab' * 32
(store/'unsubscribe-secret').write_text(key)
digest = hmac.new(bytes.fromhex(key), b'cedar\0billing@example.org', hashlib.sha256).hexdigest()
(store/'unsubscribe-failures.json').write_text(json.dumps(dict(version=2, senders=[digest])))
token = command(0).stdout.strip()
command(1, token, first=True)
plan = json.loads((store/'plan.json').read_text())
assert not plan['reasons'], plan
# Preserve the edited-plan refusal check from the original CLI walkthrough.
original = (store/'plan.json').read_text()
plan['actions'][0]['action'] = 'send'
(store/'plan.json').write_text(json.dumps(plan))
try:
    command(2, token)
except subprocess.CalledProcessError:
    pass
else:
    raise AssertionError('edited plan accepted')
(store/'plan.json').write_text(original)
plan = json.loads(original)
assert len([a for a in plan['actions'] if a['action'] == 'create-folder']) == 1
assert len([a for a in plan['actions'] if a['action'] == 'move']) == 11
assert plan['decisions'][0]['question'] == 'Keep this renewal?'
ids = [m['id'] for m in messages]
verbs = []
while True:
    call = json.loads(command(2, token).stdout)
    if call.get('done'):
        assert 'stop' not in call
        break
    if 'run' in call:
        result = subprocess.run(call['run'], check=True, text=True, capture_output=True,
                                env={'PATH': os.environ['PATH']})
        assert json.loads(result.stdout)['status'] == 'unsubscribe_not_attempted'
        verbs.append('unsubscribe')
        continue
    verbs.append(call['action'])
    observation = dict(outcome='ok', inbox_ids=ids.copy())
    if call['action'] == 'create-folder':
        observation['folder_id'] = 'created-bills'
    else:
        if call['action'] == 'move':
            assert call['target'] == 'created-bills'
        ids.remove(call['id'])
        observation['inbox_ids'] = ids.copy()
    assert json.loads(command(3, token, observation).stdout)['status'] == 'executed'
assert set(verbs) == {'create-folder', 'move', 'trash', 'archive', 'spam', 'unsubscribe'}
command(4, token)
assert 'extra' in (store/'execution-report.md').read_text()
command(5, token)
assert json.loads((store/'state.json').read_text()) == {'cedar': 1, 'marble': 0}
command(6, token)
assert not (store/'run.lock').exists()

# Full scheduled run -> quiet skip -> changed observation -> full, using the
# current documented commands in fresh shells, with a fake report channel.
(store/'config.toml').write_text('timezone="UTC"\n[schedule]\nweekdays=["00:00"]\nweekends=["00:00"]\nalways_full=[]\n[accounts.cedar]\nquiet_kind="history"\n[accounts.marble]\nquiet_kind="ids"\n')
(store/'rules.toml').write_text('rules=[]\n')
(store/'never-touch.toml').write_text('allow_empty=true\n')
observation = {'cedar': dict(outcome='ok', position='100', arrivals=[]),
               'marble': dict(outcome='ok', ids=[])}
listing = dict(accounts={'cedar': 10, 'marble': 10}, messages=[])
def documented(index, token='', on_demand=False):
    text = blocks[index].replace('/absolute/private-store', str(store)).replace('TOKEN', token)
    if on_demand:
        text = text.replace(' --scheduled', '')
    return subprocess.run(['bash', '-eu', '-c', text], check=True, text=True,
                          capture_output=True, env={'PATH': os.environ['PATH']}).stdout
for run in (1, 2, 3):
    token = documented(0).strip()
    reads = json.loads(documented(1, token))['reads']
    assert reads['marble']['limit'] == 10
    if run == 3:
        observation['cedar'] = dict(outcome='ok', position='101', arrivals=['new'])
        listing['messages'] = [dict(account='cedar', id='new', folder='Inbox',
                                    sender='digest@example.net', subject='News')]
    (store/'quiet-observation.json').write_text(json.dumps(observation))
    before = {name: (store/name).read_bytes() for name in ('state.json', 'plan.json', 'report.md')}
    decision = json.loads(documented(2, token))['decision']
    assert decision == ('skip' if run == 2 else 'full')
    if decision == 'skip':
        assert all((store/name).read_bytes() == value for name, value in before.items())
        assert not (store/'run-message.md').exists()
    else:
        (store/'inbox.json').write_text(json.dumps(listing))
        documented(3, token)
        assert json.loads(documented(4, token)) == {'done': True}
        documented(7, token)
        assert json.loads((store/'state.json').read_text())['_positions']['cedar'] == observation['cedar']['position']
        assert (store/'report.md').read_text()
    documented(8, token)
    assert not (store/'run.lock').exists()
assert len((store/'history.log').read_text().splitlines()) == 1

# Clear outstanding fixture numbers, then demonstrate three numbered items and
# both reply branches with the documented commands, each in a fresh shell.
token = documented(0).strip()
listing['messages'] = []
(store/'inbox.json').write_text(json.dumps(listing))
documented(3, token)
documented(4, token)
documented(7, token)
documented(8, token)
listing['messages'] = [dict(account='cedar', id='reply-'+str(i), folder='Inbox', sender='helper@example.org', subject='Question '+str(i)) for i in range(3)]
listing['accounts']['cedar'] = 100
listing['folders'] = {'cedar': [dict(id='billing', name='Billing')]}
token = documented(0).strip()
assert 'reads' in json.loads(documented(1, token, on_demand=True))
(store/'quiet-observation.json').write_text(json.dumps(observation))
assert json.loads(documented(2, token, on_demand=True))['decision'] == 'full'
(store/'inbox.json').write_text(json.dumps(listing))
documented(3, token, on_demand=True)
plan = json.loads((store/'plan.json').read_text())
numbers = [d['number'] for d in plan['decisions']]
assert numbers == list(range(numbers[0], numbers[0] + 3))
assert all(str(n)+'. ' in (store/'report.md').read_text() for n in numbers)
documented(4, token)
documented(7, token)
documented(8, token)
# Fake fresh gateway listing is identical. No real gateway is contacted.
token = documented(9).strip()
(store/'inbox.json').write_text(json.dumps(listing))
(store/'reply.txt').write_text(str(numbers[0]) + ' trash, ' + str(numbers[2]) + ' move to Billing')
printed = documented(10, token)
assert len(printed.splitlines()) == 2
ids = ['reply-0','reply-1','reply-2']
for ident, verb in [('reply-0','trash'),('reply-2','move')]:
    call = json.loads(documented(11, token))
    assert (call['id'],call['action']) == (ident,verb)
    ids.remove(ident)
    command(3,token,dict(outcome='ok',inbox_ids=ids.copy()))
assert json.loads(documented(11, token)) == {'done':True}
documented(13, token)
assert json.loads((store/'state.json').read_text())['cedar'] == 1
batch = json.loads((store/'decisions.jsonl').read_text())
assert [e['number'] for e in batch['parts']] == [numbers[0],numbers[2]]
assert [e['outcome'] for e in batch['parts']] == ['executed','executed']
documented(14, token)
listing['messages'] = [m for m in listing['messages'] if m['id'] == 'reply-1']
token = documented(9).strip()
(store/'inbox.json').write_text(json.dumps(listing))
(store/'reply.txt').write_text('99 trash')
refusal = documented(10, token)
assert refusal.startswith('Reply refused:') and 'commit-state' in refusal and 'release' in refusal
assert json.loads((store/'plan.json').read_text())['actions'] == []
assert json.loads(documented(11, token)) == {'done':True}
documented(13, token)
assert len((store/'decisions.jsonl').read_text().splitlines()) == 1
documented(14, token)
assert not (store/'run.lock').exists()
print('PASS: three saved numbers; reply acted on the first and third saved numbers only, logged outcomes; unknown number made zero calls')
PYCLI
WALK_STORE="$scratch/drive-walk" python3 - <<'PYDRIVE'
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timedelta, timezone

root = Path.cwd()
base = Path(os.environ['WALK_STORE'])
base.mkdir(parents=True, exist_ok=True)
text = (root / 'AGENTS.md').read_text()
blocks = re.findall(r'```bash\n(.*?)\n\s*```', text, re.S)
inline = re.findall(r'`(PIGEON_HOME=/absolute/private-store python3 scripts/triage.py (?:lock|release) [^`]+)`', text)
now = datetime.now(timezone.utc)
def prepare(label):
    store = base / label
    store.mkdir()
    (store / 'config.toml').write_text('[lock]\nkind="onedrive"\naccount="drive-example"\nfolder="locks-example"\n[accounts.cedar]\n')
    (store / 'rules.toml').write_text('rules=[]\n')
    (store / 'never-touch.toml').write_text('allow_empty=true\n')
    (store / 'guardrails.md').write_text((root / 'config/guardrails.example.md').read_text())
    (store / 'inbox.json').write_text('{"accounts":{"cedar":10},"messages":[]}')
    (store / 'state.json').write_text('{"cedar":0}')
    return store

def run(store, command, token='', fail=False):
    command = command.replace('/absolute/private-store', str(store)).replace('TOKEN', token).replace(' --scheduled', '')
    result = subprocess.run(['env', '-i', 'PATH=' + os.environ['PATH'], 'bash', '-eu', '-c', command], text=True, capture_output=True)
    assert (result.returncode != 0) == fail, (command, result.stdout, result.stderr)
    return result.stdout.strip()

def observed(store, value, command, token='', fail=False, name='lock-observation.json'):
    (store / name).write_text(json.dumps(value))
    return run(store, command, token, fail)

def item(status='created', stamp=None, tag='v1'):
    return dict(status=status, id='item-example', etag=tag, name='pigeon-run-lock', created=(stamp or now).isoformat())

store = prepare('full')
assert json.loads(run(store, blocks[0]))['call']['conflict'] == 'fail'
assert json.loads(observed(store, item(tag='create-v1'), inline[0]))['call']['operation'] == 'read'
token = observed(store, item('ok'), inline[0])
# Both quiet-check phases, full listing, plan, execution and commit.
assert 'reads' in json.loads(run(store, blocks[1], token))
(store / 'quiet-observation.json').write_text('{"cedar":{"outcome":"ok","ids":[]}}')
assert json.loads(run(store, blocks[2], token))['decision'] == 'full'
run(store, blocks[3], token)
assert json.loads(run(store, blocks[4], token)) == {'done': True}
assert json.loads(run(store, blocks[7], token))['call']['operation'] == 'read'
observed(store, item('ok'), blocks[7] + ' --lock-observation /absolute/private-store/lock-commit-observation.json', token, name='lock-commit-observation.json')
assert json.loads((store / 'state.json').read_text())['cedar'] == 0
assert json.loads(run(store, blocks[8], token))['call']['if_match'] == 'v1'
assert observed(store, {'status': 'deleted'}, inline[1], token) == 'released'
assert not (store / 'run.lock').exists()

store = prepare('busy')
run(store, blocks[0])
assert json.loads(observed(store, {'status': 'conflict'}, inline[0]))['call']['operation'] == 'read'
observed(store, item('ok'), inline[0], fail=True)
assert not (store / 'run.lock').exists()

store = prepare('stale')
run(store, blocks[0])
observed(store, {'status': 'conflict'}, inline[0])
assert json.loads(observed(store, item('ok', now - timedelta(hours=1)), inline[0]))['call']['if_match'] == 'v1'
assert json.loads(observed(store, {'status': 'deleted'}, inline[0]))['call']['conflict'] == 'fail'
assert json.loads(observed(store, item(tag='create-v2'), inline[0]))['call']['operation'] == 'read'
token = observed(store, item('ok', tag='v2'), inline[0])
assert json.loads(run(store, blocks[8], token))['call']['if_match'] == 'v2'
assert observed(store, {'status': 'deleted'}, inline[1], token) == 'released'
store = prepare('mismatch')
run(store, blocks[0])
assert json.loads(observed(store, item(tag='create-v1'), inline[0]))['call']['operation'] == 'read'
observed(store, item('ok') | {'id':'different-item'}, inline[0], fail=True)
assert not (store / 'run.lock').exists()
assert json.loads((store / 'lock-exchange.json').read_text())['call']['operation'] == 'read'
print('PASS: fresh-shell drive create/read, verify, release with read tag; busy and mismatched-id refusals without delete; stale conditional takeover; handwritten observations only')
PYDRIVE
WALK_STORE="$scratch/state-walk" python3 - <<'PYSTATE'
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone

base = Path(os.environ['WALK_STORE'])
base.mkdir()
drive = base / 'drive'
drive.mkdir()
text = Path('AGENTS.md').read_text()
blocks = re.findall(r'```bash\n(.*?)\n\s*```', text, re.S)
inline = re.findall(r'`(PIGEON_HOME=/absolute/private-store python3 scripts/triage.py [^`]+)`', text)
def find(command, suffix=''):
    return next(c for c in inline if 'triage.py ' + command + ' --token TOKEN' in c and c.endswith(suffix or '--token TOKEN'))
def run(store, command, token='', scheduled=False):
    command = command.replace('/absolute/private-store', str(store)).replace('TOKEN', token)
    if not scheduled: command = command.replace(' --scheduled', '')
    result = subprocess.run(['env', '-i', 'PATH=' + os.environ['PATH'], 'bash', '-eu', '-c', command], text=True, capture_output=True)
    assert result.returncode == 0, (command, result.stdout, result.stderr)
    return result.stdout.strip()
def observed(store, command, value, token='', name='state-observation.json'):
    (store/name).write_text(json.dumps(value))
    return run(store, command, token)
created = datetime.now(timezone.utc).isoformat()
item = dict(id='item-example', etag='v1', name='pigeon-run-lock', created=created)
lock_resume = next(c for c in inline if 'triage.py lock --observation' in c)
release_resume = next(c for c in inline if 'triage.py release --token TOKEN --observation' in c)
previous = None
for iteration in (1, 2, 3, 4):
    store = base / ('store-' + str(iteration))
    store.mkdir()
    (store/'config.toml').write_text('[schedule]\nweekdays=["00:00"]\nweekends=["00:00"]\nalways_full=[]\n[lock]\nkind="onedrive"\naccount="drive-example"\nfolder="locks-example"\n[state]\nkind="gdrive"\naccount="state-example"\nfolder="files-example"\n[accounts.cedar]\nquiet_kind="history"\n')
    (store/'guardrails.md').write_text(Path('config/guardrails.example.md').read_text())
    if iteration == 1:
        (store/'rules.toml').write_text('rules=[]\n')
        (store/'never-touch.toml').write_text('allow_empty=true\n')
    print('run', iteration, 'create pigeon-run-lock conflict=fail -> guardrails.example.md:9 lock exchange')
    assert json.loads(run(store, blocks[0]))['call']['operation'] == 'create'
    assert json.loads(observed(store, lock_resume, dict(status='created', **(item | {'etag':'create-v1'})), name='lock-observation.json'))['call']['operation'] == 'read'
    token = observed(store, lock_resume, dict(status='ok', **item), name='lock-observation.json')
    downloads = json.loads(run(store, find('fetch-state'), token))['calls']
    outcomes = {}
    for call in downloads:
        print('run', iteration, 'download', call['name'], '-> guardrails.example.md:9 named state download')
        source = drive / call['name']
        outcomes[call['name']] = 'fetched' if source.exists() else 'absent'
        if source.exists(): Path(call['path']).write_bytes(source.read_bytes())
    observed(store, find('fetch-state', 'state-observation.json'), outcomes, token)
    print('run', iteration, 'identity, Inbox listing and history position reads -> guardrails.example.md:7 read-only mail calls')
    assert 'reads' in json.loads(run(store, blocks[1], token))
    quiet = {'cedar': dict(outcome='ok', position='100', arrivals=[])}
    (store/'quiet-observation.json').write_text(json.dumps(quiet))
    decision = json.loads(run(store, blocks[2], token, scheduled=iteration == 4))
    if iteration == 4:
        assert decision['decision'] == 'skip'
        assert (store/'history.log').read_text().count('skipped: no new mail') == 1
        assert not (store/'plan.json').exists()
    else:
        assert decision['decision'] == 'full'
        (store/'inbox.json').write_text(json.dumps(dict(accounts={'cedar':100}, messages=[dict(account='cedar',id='item-a',folder='Inbox',sender='helper@example.org',subject='Question')])))
        if iteration == 2:
            (store/'reply.txt').write_text('1 yes')
            run(store, blocks[10], token)
        else:
            run(store, blocks[3] + (' --first-run' if iteration == 1 else ''), token)
        plan = json.loads((store/'plan.json').read_text())
        assert json.loads(run(store, blocks[4], token)) == {'done':True}
        print('run', iteration, 'commit read pigeon-run-lock -> guardrails.example.md:9 lock exchange')
        assert json.loads(run(store, blocks[7], token))['call']['operation'] == 'read'
        observed(store, blocks[7] + ' --lock-observation /absolute/private-store/lock-commit-observation.json', dict(status='ok', **item), token, 'lock-commit-observation.json')
        current = {n:json.loads((store/n).read_text()) for n in ('numbers.json','state.json')}
        if previous is not None: assert current == previous
        previous = current
        if iteration == 2:
            assert json.loads((store/'decisions.jsonl').read_text())['parts'][0]['outcome'] == 'answered'
    if iteration == 3: (store/'run-message.md').write_text('Delivery failed\n')
    print('run', iteration, 'send read pigeon-run-lock -> guardrails.example.md:9 lock exchange')
    assert json.loads(run(store, find('send-state'), token))['call']['operation'] == 'read'
    result = json.loads(observed(store, find('send-state', 'state-lock-observation.json'), dict(status='ok', **item), token, 'state-lock-observation.json'))
    uploaded = []
    while 'call' in result:
        call = result['call']
        uploaded.append(call['name'])
        print('run', iteration, 'upload', call['name'], '-> guardrails.example.md:9 named state upload')
        raw = Path(call['path']).read_bytes()
        if iteration == 3: outcome = dict(status='failed')
        else:
            (drive/call['name']).write_bytes(raw)
            outcome = dict(status='done',size=len(raw))
        result = json.loads(observed(store, find('send-state', 'upload-observation.json'), outcome, token, 'upload-observation.json'))
    if iteration == 1: assert uploaded[0] == 'numbers.json' and uploaded[-1] == 'state.json'
    if iteration == 2: assert set(uploaded) <= {'state.json', 'decisions.jsonl'}
    if iteration == 4: assert uploaded == ['history.log']
    if iteration == 3:
        assert uploaded == ['run-message.md']
        assert (store/'run-message.md').read_text().splitlines()[0] == result['warning']
    print('run', iteration, 'delete pigeon-run-lock if_match=v1 -> guardrails.example.md:9 conditional lock delete')
    assert json.loads(run(store, blocks[8], token))['call']['operation'] == 'delete'
    assert observed(store, release_resume, dict(status='deleted'), token, 'lock-observation.json') == 'released'
print('PASS: fresh-shell drive state; two empty working stores preserve numbers/counts/positions; numbered reply answered; scheduled quiet skip uploads only history; failed upload warns first, no retry, conditional release')
print('Gateway-to-guardrail: create/read/conditional delete pigeon-run-lock -> guardrails line 9 lock exchange; ten named downloads and changed uploads -> guardrails line 9 state exchange; identity/Inbox/history reads -> guardrails line 7 read-only mail calls. No gateway was contacted.')
PYSTATE

# Identity is fictional; the hook must refuse the private path, not bad content.
git config user.name 'Kit Test'
git config user.email 'kit@example.com'
git config commit.gpgsign false
mkdir -p private/probe
printf 'fixture only\n' > private/probe/rules.toml
git check-ignore -q private/probe/rules.toml
git add -f private/probe/rules.toml
before=$(git rev-parse HEAD)
if git commit -m 'Test private path refusal' > "$scratch/refusal" 2>&1; then
    echo 'FAIL: private file commit succeeded' >&2
    exit 1
fi
test "$(git rev-parse HEAD)" = "$before"
# Assert the expected gate caused refusal, rather than an unrelated git failure.
python3 - "$scratch/refusal" <<'PY'
from pathlib import Path
import sys
assert 'path-allowlist' in Path(sys.argv[1]).read_text()
PY
echo 'PASS: fresh-clone quick start, fresh-shell AGENTS commands, rule features, quiet scheduled runs and hook refusal'
