# pigeon

pigeon never sends anything without the owner's approval. Sending is off by default. With allow_send=true, "N send" ("N enviar") sends an unchanged draft it created. "N forward HANDLE" ("N reenviar HANDLE") forwards one message to another configured account's address, with no added text, through the mailbox forward-draft operation. Both read the draft back before sending; at most five sends per reply. Only a numbered send or forward approval can authorize sending. It never composes mail or takes draft text or forward recipients from mail; replies use the mailbox reply operation. It never deletes permanently; the calendar stays read-only.

pigeon is an MIT-licensed email triage kit. An AI assistant carries calls through an MCP mail gateway such as [Executor](https://executor.sh). The Python planner matches Inbox messages against a private Markdown rules table; the execution step machine issues actions in batches per account.

## Quick start

Give your hosted assistant this repository and the instruction: "Follow AGENTS.md for one scheduled Inbox triage and send the standard run message." Use [the routine quick start](docs/quickstart.md). Complete [hosted setup](docs/hosted-setup.md#hosted-setup), including the [host requirements](docs/hosted-setup.md#host-requirements) and [storage choice](docs/hosted-setup.md#storage-choice).

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
printf '[accounts.home]\n[accounts.work]\n' > "$PIGEON_HOME/config.toml"
touch "$PIGEON_HOME/never-touch.md" "$PIGEON_HOME/unsubscribe.md"
cp tests/fixtures/rules.md "$PIGEON_HOME/rules.md"
python3 scripts/triage.py validate
token=$(python3 scripts/triage.py lock)
trap 'python3 scripts/triage.py release --token "$token" 2>/dev/null || true' EXIT
python3 scripts/triage.py plan --token "$token" --inbox tests/fixtures/inbox.json --plan-out "$PIGEON_HOME/plan.json" --report-out "$PIGEON_HOME/report.md"
printf '{"run":"%s","answers":[{"account":"home","id":"7","proceed":true,"note":"is an order confirmation"},{"account":"home","id":"12","proceed":false,"note":"needs a human"}]}\n' "$token" > "$PIGEON_HOME/answers.json"
python3 scripts/triage.py plan --token "$token" --inbox tests/fixtures/inbox.json --plan-out "$PIGEON_HOME/plan.json" --report-out "$PIGEON_HOME/report.md" --answers "$PIGEON_HOME/answers.json"
cat "$PIGEON_HOME/report.md"
python3 scripts/triage.py release --token "$token"
trap - EXIT
)
```
<!-- /quick-start -->

Compare the plain-text report with tests/fixtures/expected-report.md. The demo prints addresses unchanged and uses plain section titles. The final command prints "released". For real mail, follow [hosted setup](docs/hosted-setup.md).

## Contributions

Enable hooks with `git config core.hooksPath hooks`. Use invented examples on example.com, example.org, example.net or .invalid. Inspect the staged diff. Run `python3 -m unittest discover -s tests` and `bash tests/e2e.sh`. Follow [maintainer checks](docs/setup.md#maintainer-publication-checks) for leak scans. Do not attach real mail or reports to issues.

See [CHANGELOG](CHANGELOG.md) and the [MIT license](LICENSE).
