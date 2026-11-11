# pigeon

pigeon is an MIT-licensed email triage kit driven by Claude Code, Codex or another AI assistant through [Executor](https://executor.sh). Executor is a hosted service that connects mail accounts to an AI assistant as callable tools. pigeon’s Python planner matches rules against an Inbox listing and produces an action plan and report without accessing mail.

It works on Inbox messages only and never sends. A plan is the proposed list of actions, produced without changing mail. Fixed limits stop the whole plan when exceeded. Read the policy in [guardrails](config/guardrails.example.md) before using a real account.

## Requirements

An Executor account with your mail apps connected, an AI assistant with Executor access, Python 3.11+, git, and a POSIX system with Bash. No Python packages are needed.

## Quick start

Clone https://github.com/ricardo-alternova/pigeon and enter that folder, or use your existing checkout. Run the following in Bash. This runs on sample mail in private/demo and overwrites its sample settings. No mail is touched. A private store is the folder holding your local settings and reports.

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

The private store is `~/.config/pigeon/` (create it first) or an existing folder named by `PIGEON_HOME`, such as the ignored `private/` folder. Keep account handles, app slugs, rules, listings, state, reports and logs there, never in public files.

The allowlist in [.gitignore](.gitignore) ignores everything except reviewed kit paths. Git can force-add ignored files, so the [commit hook](hooks/pre-commit) refuses unapproved paths and private-looking text. Hooks protect only after the quick start enables them. A public-fork push is public before CI runs. CI runs the same leak checker. These checks reduce leaks; they cannot identify every private value. Never force private content into an allowed file.

Before contributing, enable the hooks as above, use invented examples on example.com, example.org, example.net or .invalid, inspect the staged diff, and run `python3 scripts/leak_check.py` and `python3 scripts/leak_check.py --history`. If you maintain a private deny-list outside git, add `--deny PATH` to each scan and set `PIGEON_DENY` to its folder for hooks. Do not attach real mail or reports to issues. Run `python3 -m unittest discover -s tests` and `bash tests/e2e.sh` before submitting.

## Known limits

The tested matcher covers deterministic rules. The assistant still executes the plan through Executor. Prompt injection risk is reduced, not removed. A run must finish within 45 minutes; the lock does not refresh. User subject regular expressions can be slow. Sender addresses can be forged. Unsubscribe is planned only from user-written rules, and its link is shown in the report. See [setup](docs/setup.md) for unverified integrations.

## License

[MIT](LICENSE).
