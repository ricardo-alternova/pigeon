#!/usr/bin/env bash
# Offline fresh-clone proof. Scratch is removed on every exit.
set -eu
source_repo=$(git rev-parse --show-toplevel)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/pigeon-e2e.XXXXXX")
trap 'python3 -c "import shutil, sys; shutil.rmtree(sys.argv[1])" "$scratch"' EXIT
git clone --quiet --no-hardlinks "$source_repo" "$scratch/clone"
cd "$scratch/clone"
export PIGEON_HOME="$PWD/private/demo"
python3 - <<'PY' > "$scratch/quick-start.sh"
from pathlib import Path
text = Path('README.md').read_text()
block = text.split('<!-- quick-start -->\n```bash\n')[1].split('\n```\n<!-- /quick-start -->')[0]
print(block)
PY
bash "$scratch/quick-start.sh" > "$scratch/output"
cmp "$PIGEON_HOME/report.md" tests/fixtures/expected-report.md
test ! -e "$PIGEON_HOME/run.lock"
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
echo 'PASS: fresh-clone quick start, report, release and hook refusal'
