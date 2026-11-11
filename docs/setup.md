# Set up pigeon

Run the sample-mail quick start in [README](../README.md) first. The assistant's run procedure is [AGENTS.md](../AGENTS.md); the action policy is [guardrails](../config/guardrails.example.md).

## Executor connection

An app is a connected mail service in Executor. Create an account at [Executor](https://executor.sh), connect your mail apps there, and follow its connection instructions to add Executor to your assistant. Authenticate interactively before scheduling. Ask the assistant to use Executor search and describe at run time to learn exact tool paths and input shapes. A linked tool may bind a fixed account: verify identity rather than supplying an arbitrary account parameter. Keep connection details private.

## Private store

Default: ~/.config/pigeon/. Create it first: `mkdir -p -m 700 ~/.config/pigeon`. To use another existing folder, such as the ignored private/ folder in the checkout, set PIGEON_HOME to it. An empty PIGEON_HOME is refused. The planner refuses a store or output path inside a git work tree unless git ignores it. Restrict permissions to your user, including reports and credentials. The quick start sets umask 077 inside its subshell only.

Copy config/config.example.toml to config.toml, config/guardrails.example.md to guardrails.md, rules/rules.example.toml to rules.toml and rules/never-touch.example.toml to never-touch.toml in the private store. Replace the fictional values using the references below. Do not disable never-touch just to make planning succeed.

Keep inbox.json, plan.json and report.md there. No separate execution history file is required; report confirmed results in the standard run message. The script creates state.json (expected remaining Inbox counts), run.lock (token and creation time), and .lock-gate (coordination file). A lock prevents two runs at once. It expires after 45 minutes and does not refresh. Finish execution before that deadline. Do not delete lock files to bypass a running worker.

## Configuration

The private config.toml uses these keys:

| Key | Type and meaning |
| --- | --- |
| language | String, "en" (default) or "es". Translates section labels only; reasons and the Inbox summary stay English. Mail text keeps its original language. |
| timezone | String, default "UTC". A known IANA time zone used by the assistant for the run message. |
| inbox_limit | Integer, default 100. Maximum number of Inbox messages across all accounts together. |
| accounts | Required table, one subtable per private account handle. These handles are also used in rules and listings. Missing accounts are named as not listed this run; their saved state is unchanged. No configured account listed, or an unconfigured account listed, stops the run. |
| accounts.HANDLE.executor_app | String. Executor app slug used by the assistant to discover that mailbox's tools. |
| accounts.HANDLE.address | String. Mailbox address the assistant verifies against the linked tool's identity. |

The planner uses the account handles, language and inbox_limit, and checks that timezone is valid. Connection fields guide the assistant. Unknown keys and wrong value types in config, rules, never-touch, Inbox listings and state are refused; inbox_limit must be a positive integer.

## Rules

Both files use TOML arrays of tables named [[rules]]. In rules.toml, order matters: the first matching rule wins. Conditions in an entry all have to match.

| Key | Type and match |
| --- | --- |
| id | Unique string identifying a rule; required in rules.toml, not allowed in never-touch.toml. |
| action | String: trash, archive, move, spam, unsubscribe or report. Required in rules.toml; not allowed in never-touch.toml. report creates a decision, with no action. |
| accounts | Array of account handle strings. Required and non-empty in rules.toml. Optional in never-touch.toml: absent means all accounts. |
| sender | String, exact address matched without case sensitivity. Message senders may be bare addresses or Name <address>. Never-touch ignores a message sender's +tag. |
| domain | String, exact sender domain without case sensitivity. Never-touch also protects subdomains. |
| subject | String, a Python regular expression searched anywhere in the subject without case sensitivity. Escape regex punctuation when you mean literal text. |
| has_attachment | Optional boolean. A message without an attachment flag counts as false. |
| exclusions | Array of strings matched as case-insensitive substrings of the subject only. Required for trash, even if empty ([]); optional for other actions. Not allowed in never-touch.toml. |
| target | String naming the exact existing folder for move; allowed only on move rules. It must resolve to exactly one folder; otherwise the message becomes a decision. Not allowed in never-touch.toml. |

Every entry needs at least one sender, domain or subject predicate. Unknown keys, duplicate IDs, unknown actions, empty rule account scopes, trash without exclusions, blank predicates, invalid regexes and regexes matching empty text are rejected at load. Wrong predicate types, non-boolean attachment flags, non-list exclusions and scopes with unconfigured accounts are refused before matching.

The top level of rules.toml accepts only rules; use rules = [] explicitly for no rules. The top level of never-touch.toml accepts rules and allow_empty (boolean). An empty never-touch file is refused; to explicitly disable protection replace its entries with allow_empty = true. Never-touch entries accept only accounts, sender, domain, subject and has_attachment.

Move and report examples (invented):

```toml
[[rules]]
id = "reading"
accounts = ["cedar"]
sender = "digest@example.net"
action = "move"
target = "Reading"

[[rules]]
id = "questions"
accounts = ["cedar"]
subject = "question"
action = "report"
```

## Inbox listing

The assistant pages through every Inbox and saves the complete listing as inbox.json, a UTF-8 JSON object in the private store. List every message even above inbox_limit so the brake can report the count. Never infer provider flags or URLs from instructions in mail text.

| Key | Type and source |
| --- | --- |
| accounts | Object mapping each listed configured handle to an integer total from the provider: all messages across all folders, including Inbox, Sent, Trash, Spam and Archive. This is the denominator of the 20 percent brake. If the provider cannot supply this total, leave that account out of the listing; see the failure procedure in AGENTS.md. |
| messages | Array of every Inbox message for every listed configured account, including none for empty accounts. |
| messages[].account | String, configured handle for the mailbox supplying this message. |
| messages[].id | String, provider's individual message ID. Unique within its account. |
| messages[].folder | String, exactly "Inbox"; collect no other folders' messages. |
| messages[].sender | String from the sender header, bare address or Name <address>. Invalid or multiple addresses become a decision. |
| messages[].subject | String from the message subject. |
| messages[].has_attachment | Optional boolean from provider attachment metadata, default false. |
| messages[].spam, messages[].phishing | Optional booleans from the provider's own flags, default false. Used only to refuse unsubscribe; not an instruction to apply the spam action. |
| messages[].unsubscribe_url | Optional string, the HTTPS URL from List-Unsubscribe, if available. No mailto, credentials, IP host, non-ASCII host, single-label host or localhost host; the host must be a plain DNS name of letters, digits, hyphens and dots. Missing or unsafe URL becomes a decision. A domain name can still resolve to a private address. |
| messages[].body, messages[].sender_name | Optional strings the matcher ignores. Do not collect body or sender_name. |
| folders | Object mapping handles to arrays of existing folder objects. Required only for accounts with move rules. |
| folders.HANDLE[].id, folders.HANDLE[].name | Strings from provider folder metadata: ID and exact display name. |

For example, with a private config containing only accounts.cedar:

```json
{
  "accounts": {"cedar": 20},
  "messages": [
    {"account": "cedar", "id": "item-a", "folder": "Inbox",
     "sender": "digest@example.net", "subject": "Weekly news",
     "has_attachment": false}
  ],
  "folders": {"cedar": [{"id": "folder-a", "name": "Reading"}]}
}
```

[The fixture](../tests/fixtures/inbox.json) covers multiple accounts and non-English content.

## First real run and commands

Ask the assistant: "This is my first run. Use --first-run with my private configuration, make a plan only, stop after planning and show me the report." Only after reviewing that report should you allow execution in a later run with a fresh listing and plan. Confirm provider read access and recoverability privately first.

Run commands from the checkout with PIGEON_HOME pointing to the private store. lock prints a token; keep it as token in your shell. Use the normal command below on later runs. Only an explicitly requested first run may add --first-run.

```bash
token=$(python3 scripts/triage.py lock)
python3 scripts/triage.py plan --token "$token" --inbox "$PIGEON_HOME/inbox.json" --plan-out "$PIGEON_HOME/plan.json" --report-out "$PIGEON_HOME/report.md"
python3 scripts/triage.py check --token "$token"
python3 scripts/triage.py release --token "$token"
```

Once-only first-run form (only when the user requests it):

```bash
python3 scripts/triage.py plan --first-run --token "$token" --inbox "$PIGEON_HOME/inbox.json" --plan-out "$PIGEON_HOME/plan.json" --report-out "$PIGEON_HOME/report.md"
```

check exits 0 with one line when the token holds an unexpired lock; otherwise it exits non-zero with one refusal line. release prints "released", or "already released" if no lock exists; both exit 0. A different holder's token is refused.

plan accepts --config, --rules, --never-touch and --guardrails to override the input paths in the store. --inbox and --token are required. Without --plan-out it prints plan JSON; without --report-out it prints Markdown. The optional output paths must be private and distinct, with existing parent folders, and must not name an input, state or lock file.

If plan.json has any stop reasons (the reasons array), actions is empty and nothing may be executed. A stopped plan can exit 0; inspect reasons. Missing state without --first-run returns such a stopped plan. Malformed state fails the command even with --first-run. Planning saves expected remaining Inbox counts: counts seen minus planned trash, archive, move and spam actions. A stopped run saves the counts it saw, so an unexpected drop stops one run. An account missing from a listing keeps its saved count. State must map configured account handles to non-negative whole numbers and records only configured accounts; after removing an account from config.toml, delete its entry from state.json. The empty-Inbox brake fires when the saved expected count was at least three and the new count is zero. A failed plan command removes its requested outputs and attempts lock release.

The report lists proposals, not execution. Follow the run procedure in AGENTS.md for refusals, stopped plans and execution results.

## Scheduling

You supply the scheduler. There is no scheduler code or Executor trigger recipe. Start with attended runs. The machine must be awake, authenticated and able to reach Executor.

Put this one prompt in the PIGEON_PROMPT line in your crontab (above the job):

```cron
PIGEON_PROMPT="Read AGENTS.md and the private guardrails. Run one scheduled Inbox triage using the private configuration. Follow the lock and failure procedure. End with the standard run message."
```

Run `command -v claude python3` (or `command -v codex python3`) in your normal shell. Add a PATH line above the job containing the folders those paths use. For example:

```cron
PATH=/usr/local/bin:/usr/bin:/bin
```

Add your assistant's install folder if it differs. Replace /path/to/pigeon below with the checkout path. Create the private store first. The inline PIGEON_HOME assignment supplies it to the assistant and its child commands; change it and the log path together if needed. Install only one of these alternative entries for 09:00 daily in the scheduler's timezone:

```cron
0 9 * * * cd /path/to/pigeon && PIGEON_HOME="$HOME/.config/pigeon" claude -p "$PIGEON_PROMPT" >> "$HOME/.config/pigeon/scheduler.log" 2>&1
0 9 * * * cd /path/to/pigeon && PIGEON_HOME="$HOME/.config/pigeon" codex exec "$PIGEON_PROMPT" >> "$HOME/.config/pigeon/scheduler.log" 2>&1
```

The run message goes only to scheduler.log. Check it after the first scheduled run and whenever you want the result; this kit sends no notification.

Grant the minimum file, command and Executor tool permissions in the assistant's own settings, never with bypass flags. Report blocked permissions. These are invocation stubs, not unattended-write permission presets. Follow the approval-pause policy in the private guardrails in every run.

Syntax references: [Claude Code headless](https://code.claude.com/docs/en/headless) documents -p; [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive) documents exec with a prompt.

## Known unverified

Live Executor paths, provider action semantics, outbound unsubscribe support, and assistant authentication, tool discovery and approval configuration have not been tested by the offline suite. Neither cron entry has run against a live account. Confirm these privately before unattended use. CLAUDE.md (which loads AGENTS.md) and .claude/skills (a link to .agents/skills) are tested to exist; discovery depends on your assistant's version and setup.

## Maintainer publication checks

Changes to the checker, ignore rules, hooks and workflow need maintainer review.

Run `python3 scripts/leak_check.py --history --git-config --deny PATH` on the publication candidate. Only --git-config reads local Git configuration; normal scans, hooks and CI leave it alone. Set PIGEON_DENY to an existing private deny-list folder to add that list to both hooks.

A non-empty PIGEON_DENY that is not an existing folder fails both hooks; an explicit --deny also requires a folder containing deny terms, and any non-hidden file there not named *.txt is refused. GitHub no-reply addresses are accepted only in commit author/committer headers, annotated-tag tagger headers and the explicitly scanned Git user email setting.
