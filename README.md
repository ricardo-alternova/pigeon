# pigeon

pigeon is an MIT-licensed email triage kit. An AI assistant carries calls through an MCP mail gateway such as [Executor](https://executor.sh). The Python planner matches Inbox messages against private rules; the execution step machine issues one action at a time.

## Quick start

Give your hosted assistant this repository and the instruction: "Follow AGENTS.md for one scheduled Inbox triage and send the standard run message." Use [the routine quick start](docs/quickstart.md). Complete [hosted setup](docs/setup.md#hosted-setup), including the [host requirements](docs/setup.md#host-requirements) and [storage choice](docs/setup.md#storage-choice).

### Developer machine

Clone https://github.com/ricardo-alternova/pigeon and enter the checkout. Run this Bash demo with Python 3.11+ and git. It uses sample mail, creates private/demo settings and enables commit hooks.

<!-- quick-start -->
```bash
(
set -eu
umask 077
git config core.hooksPath hooks
export PIGEON_HOME="$PWD/private/demo"
python3 scripts/triage.py setup
python3 scripts/triage.py validate
token=$(python3 scripts/triage.py lock)
trap 'python3 scripts/triage.py release --token "$token" 2>/dev/null || true' EXIT
python3 scripts/triage.py plan --token "$token" --inbox tests/fixtures/inbox.json --plan-out "$PIGEON_HOME/plan.json" --report-out "$PIGEON_HOME/report.md"
cat "$PIGEON_HOME/report.md"
python3 scripts/triage.py release --token "$token"
trap - EXIT
)
```
<!-- /quick-start -->

Compare the report with tests/fixtures/expected-report.md. The final command prints "released". For real mail, follow [setup](docs/setup.md).

## Contributions

Enable hooks with `git config core.hooksPath hooks`. Use invented examples on example.com, example.org, example.net or .invalid. Inspect the staged diff. Run `python3 -m unittest discover -s tests` and `bash tests/e2e.sh`. Follow [maintainer checks](docs/setup.md#maintainer-publication-checks) for leak scans. Do not attach real mail or reports to issues.

See [CHANGELOG](CHANGELOG.md) and the [MIT license](LICENSE).
