# pigeon

pigeon is an MIT-licensed email triage kit run by a capable AI assistant through an MCP mail gateway such as [Executor](https://executor.sh). Executor is a hosted service that connects mail accounts to an AI assistant as callable tools. pigeon’s Python planner matches rules against an Inbox listing and produces an action plan and report without accessing mail.

It works on Inbox messages only and never sends. A plan is the proposed list of actions, produced without changing mail. Fixed limits stop the whole plan when exceeded. Read the policy in [guardrails](config/guardrails.example.md) before using a real account.

## Requirements

See the four [host requirements](docs/setup.md#host-requirements). The scripts need Python 3.11+, git and a POSIX system with Bash; no Python packages are needed.

## Quick start

Give your always-on hosted assistant this repository and one entry instruction: follow [AGENTS.md](AGENTS.md). It runs on its own schedule, posts the run message in its chat, and accepts your numbered replies there. Use the ordered [hosted setup](docs/setup.md#hosted-setup) to reach your first attended run using the [storage choice](docs/setup.md#storage-choice) for your host.

### Developer machine

A local folder is also supported. Clone https://github.com/ricardo-alternova/pigeon and enter the checkout. This Bash demo uses sample mail only and overwrites private/demo settings; it enables commit hooks. For real mail use [setup](docs/setup.md).

<!-- quick-start -->
```bash
(
set -eu
umask 077
git config core.hooksPath hooks
export PIGEON_HOME="$PWD/private/demo"
mkdir -p "$PIGEON_HOME"
cp config/config.example.toml "$PIGEON_HOME/config.toml"
cp config/guardrails.example.md "$PIGEON_HOME/guardrails.md"
cp rules/rules.example.toml "$PIGEON_HOME/rules.toml"
cp rules/never-touch.example.toml "$PIGEON_HOME/never-touch.toml"
token=$(python3 scripts/triage.py lock)
trap 'python3 scripts/triage.py release --token "$token" 2>/dev/null || true' EXIT
python3 scripts/triage.py plan --first-run --token "$token" --inbox tests/fixtures/inbox.json --plan-out "$PIGEON_HOME/plan.json" --report-out "$PIGEON_HOME/report.md"
cat "$PIGEON_HOME/report.md"
python3 scripts/triage.py release --token "$token"
trap - EXIT
)
```
<!-- /quick-start -->

The printed report should match tests/fixtures/expected-report.md; the final command also prints "released". For real mail, follow [setup](docs/setup.md), replace the fictional rules and never-touch entries, and ask your assistant to follow [AGENTS.md](AGENTS.md).

## Private data and contributions

Your real private store is `~/.config/pigeon/` (create it first) or another folder outside the checkout named by `PIGEON_HOME`. The quick start uses the ignored `private/demo` folder for sample data only. Keep account handles, app slugs, rules, listings, state, reports and logs there, never in public files.

The allowlist in [.gitignore](.gitignore) ignores everything except reviewed kit paths. Git can force-add ignored files, so the [commit hook](hooks/pre-commit) refuses unapproved paths and private-looking text. Hooks protect only after the quick start enables them. A public-fork push is public before CI runs. CI runs the same leak checker. These checks reduce leaks; they cannot identify every private value. Never force private content into an allowed file.

Before contributing, enable the hooks as above, use invented examples on example.com, example.org, example.net or .invalid, inspect the staged diff, and run `python3 scripts/leak_check.py` and `python3 scripts/leak_check.py --history`. For private deny-list checks, see [maintainer checks](docs/setup.md#maintainer-publication-checks). Do not attach real mail or reports to issues. Run `python3 -m unittest discover -s tests` and `bash tests/e2e.sh` before submitting.

## Known limits

While the assistant carries the calls, the scripts cannot physically stop a call they did not issue. They log every issued call and result; the report flags any unplanned call disclosed by the assistant. See the [lock policy](docs/setup.md#private-store) for the run deadline. User subject regular expressions can be slow. Sender addresses can be forged. See the [local unsubscribe policy](docs/setup.md#local-unsubscribe-client). See [setup](docs/setup.md) for unverified integrations.

See [CHANGELOG](CHANGELOG.md) for release history.

## License

[MIT](LICENSE).
