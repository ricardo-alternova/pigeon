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
assert len(blocks) == 7
def command(index, token='', observation=None, first=False):
    text = blocks[index].replace('/absolute/private-store', str(store)).replace('TOKEN', token)
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
PYCLI
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
echo 'PASS: fresh-clone quick start, fresh-shell AGENTS commands, rule features and hook refusal'
