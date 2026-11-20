# pigeon

pigeon is an email triage kit for our team. It is plain Markdown that any AI agent can follow: Claude Code, Codex, Cursor, or any agent with MCP.

pigeon is opinionated: all mail and calendar access goes through **Executor** (executor.sh) and its MCP tools (`search`, `describe`, `execute`, `resume`). There is no code to install and no engine to run. The agent reads the instructions, reads your rules table, looks at your Inbox through Executor, does the safe actions your rules cover, and sends you a short numbered report for everything else.

## Before you start

- An Executor account with your Gmail or Outlook mailboxes connected (and Google or Outlook calendars, if you want a calendar summary).
- The Executor MCP server added to your agent.

## Start

1. Clone this repo.
2. Open it in your agent.
3. Say: **"Start triaging my email with this repo."**

## What happens on the first run

1. The agent copies `templates/` into `local/`. `local/` is gitignored, so your accounts, rules, and history never go into the repo.
2. It lists the mail and calendar accounts connected in your Executor and asks which ones to triage. If one is missing, it walks you through connecting it in Executor. It saves each account's Executor `accountId` in `local/accounts.md`.
3. It asks for your timezone, the times you want a full report, your report language (default: the language you write in), and whether to include a calendar summary.
4. It sweeps your Inbox. With zero rules it acts on nothing: every message goes into the report, numbered.
5. You reply to the report (`3 archive`, `5 trash and rule`). Each `and rule` reply adds a row to `local/rules.md`, so the agent handles more on its own every run.

## Running it on a schedule

pigeon has no scheduler of its own. Schedule runs in whatever runs your agent (its routines or automations feature, cron, a CI job) with this prompt:

> Follow AGENTS.md for one triage run.

Runs at your full-report times always send a report. Other scheduled runs stay quiet when no new mail arrived and nothing failed.

## Already have a rules table?

Copy it to `local/rules.md`. The agent reads the table directly, so your own column names, notes, exceptions, and action words in another language work as written. Each row needs a pattern, an action, and a scope. A row whose action the agent cannot map, or whose full-rule file is missing from `local/`, goes to "Needs you" instead of acting. Copy any files your rows point to into `local/` too.

## Files

| File | Purpose |
|---|---|
| `AGENTS.md` | What the agent does on every run |
| `guardrails.md` | Hard safety limits. They override everything else |
| `executor-verbs.md` | The Executor calls for Gmail, Outlook, and calendars |
| `templates/` | Empty starter files, copied into `local/` on the first run |
| `local/` | Your files (gitignored) |
