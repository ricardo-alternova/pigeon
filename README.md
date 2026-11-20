# pigeon

pigeon is a Markdown kit for triaging Gmail and Outlook with an AI agent. Your agent reads your rules, handles matching mail through Executor, and gives you a numbered report for messages that need a decision.

## Start with your Inbox

1. Connect the Gmail or Outlook accounts you want to triage to [Executor](https://executor.sh). Add its Model Context Protocol (MCP) server to an agent that can read and write files in your checkout.
2. Clone this repository and open the folder in that agent.
3. Give the agent this prompt:

   > Start triaging my email with this repo.

The agent verifies your accounts and asks which to include, which language to use, and when you want a full report in your time zone. You can also opt into a read-only calendar summary.

**Your first run starts with an empty rules table.** The agent leaves the messages in your Inbox and lists them in the report. Reply with an item number and an action. For example, `3 archive` archives item 3 once. Replying `5 trash and rule` moves item 5 to Trash and adds a rule for future runs.

## Bring your own rules

First-run setup copies the starter files into `local/`, a folder Git ignores. Keep your accounts, rules and never-touch list there.

If you already have a rules table, copy it to `local/rules.md` along with any files its rows reference. Each row needs a pattern, an action and an account scope. Use your own column names, language, notes and exceptions. If the agent cannot interpret an action or read a referenced rule, it leaves the message in the Inbox and flags it under **Needs you**.

## Schedule a run

Set a routine in your agent’s host that opens the same checkout and gives the agent this prompt:

> Follow AGENTS.md for one triage run.

Set your weekday and weekend full-report times in `local/accounts.md`. Other scheduled runs stay quiet when nothing has changed and no error needs reporting.

## Kit reference

| File | What it covers |
| --- | --- |
| [AGENTS.md](AGENTS.md) | The triage workflow, reports and numbered replies |
| [guardrails.md](guardrails.md) | Never-touch protection, action limits and send approvals |
| [executor-verbs.md](executor-verbs.md) | Executor calls for Gmail, Outlook and calendars |
| [templates/](templates/) | Starter files for each user’s private working folder |

[MIT license](LICENSE)
