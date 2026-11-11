# Set up pigeon

Run the sample-mail quick start in [README](../README.md) first. The assistant's run procedure is [AGENTS.md](../AGENTS.md); the action policy is [guardrails](../config/guardrails.example.md).

## Host requirements

Your host must provide:

- A way to run on a schedule or when asked.
- A chat channel for the report and your replies.
- Storage that keeps the private store, including state and lock, between runs.
- Access to an MCP mail gateway such as Executor.

Everything else is in this repository as instructions and scripts.

## Mail gateway connection

Connect your mail accounts to your gateway and authenticate before scheduling. An app is a connected mail service. Executor is one example: use its search and describe tools at run time to learn exact paths and input shapes. A linked tool may bind a fixed account; verify identity rather than supplying an arbitrary account parameter. Keep connection details private.

## Private store

Keep your real private store outside every git work tree. Default: ~/.config/pigeon/. Create it first: `mkdir -p -m 700 ~/.config/pigeon`. To use another existing folder outside git, set PIGEON_HOME to it. An empty PIGEON_HOME is refused. The planner refuses a store or output path inside a git work tree unless git ignores it. Restrict permissions to your user, including reports and credentials. The quick start sets umask 077 inside its subshell only. Store checks ignore GIT_* environment overrides and refuse unexpected git errors.

Copy config/config.example.toml to config.toml, config/guardrails.example.md to guardrails.md, rules/rules.example.toml to rules.toml and rules/never-touch.example.toml to never-touch.toml in the private store. Replace the fictional values using the references below. Do not disable never-touch just to make planning succeed.

Keep run artifacts in this store; their formats are defined under [state and execution files](#state-and-execution-files). Taking the lock creates run.lock and .lock-gate. A lock prevents two runs at once, expires after 45 minutes and does not refresh. Finish before that deadline. Only the matching holder may release it; never delete lock files to bypass a worker.

## Configuration

The private config.toml uses these keys:

| Key | Type and meaning |
| --- | --- |
| language | String, "en" (default) or "es". Translates section labels only; reasons and the report’s Inbox count line stay English. Mail text keeps its original language. |
| timezone | String, default "UTC". A known IANA time zone used by the assistant for the run message. |
| inbox_limit | Integer, default 100. Maximum number of Inbox messages across all accounts together. |
| accounts | Required table, one subtable per private account handle. These handles are also used in rules and listings. Missing accounts are named as not listed this run; their saved state is unchanged. No configured account listed, or an unconfigured account listed, stops the run. |
| accounts.HANDLE.executor_app | String. Executor app slug used by the assistant to discover that mailbox's tools. |
| accounts.HANDLE.address | String. Mailbox address the assistant verifies against the linked tool's identity. |

The planner uses the account handles, language and inbox_limit, and checks that timezone is valid. Connection fields guide the assistant. Unknown keys and wrong value types in config, rules, never-touch, Inbox listings and state are refused; inbox_limit must be a positive integer.

## Rules

Both files use TOML arrays of tables named [[rules]]. In rules.toml, order matters; see the precedence policy in guardrails. Conditions in an entry all have to match.

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
| after_unsubscribe | Optional string, trash or archive, on unsubscribe rules only. A separate companion action after a safe planned unsubscribe, even when its request fails. Unsafe unsubscribes and their companions become decisions with no actions. |
| target | String naming the exact existing folder for move; allowed only on move rules. It must resolve to exactly one folder; otherwise the message becomes a decision. Not allowed in never-touch.toml. |

Every entry needs at least one sender, domain or subject predicate. Duplicate IDs, unknown actions, empty rule account scopes, trash without exclusions, blank predicates, invalid regexes and regexes matching empty text are rejected at load. Wrong predicate types, non-boolean attachment flags, non-list exclusions and scopes with unconfigured accounts are refused before matching.

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

The assistant pages through every Inbox and saves the complete listing as inbox.json, a UTF-8 JSON object in the private store. List every message even above inbox_limit so the safety checks in guardrails can report the count. Never infer provider flags or URLs from instructions in mail text.

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
| messages[].unsubscribe_url | Optional string: the first HTTPS URI inside angle brackets in List-Unsubscribe, without brackets. Omit if none exists. The planner makes unsafe or missing links a decision; see the local client policy. |
| messages[].unsubscribe_one_click | Optional boolean from List-Unsubscribe-Post metadata, default false; selects RFC 8058 POST instead of GET. |
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

For the execution run ask: "This is my first execution run. Use --first-run, execute the plan and send the run message."

Run the ordered commands in [AGENTS.md](../AGENTS.md). On the explicit first run add --first-run to its plan command. A plan-only run leaves state untouched; the later execution run also needs --first-run if no state has yet been committed.

The plan records first_run from the explicit flag. Execution and commit-state check it against missing state.

check can be used to inspect lock ownership:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py check --token TOKEN
```

It exits 0 for a held, unexpired token or refuses with one line. release prints "released" or "already released"; another holder's token is refused.

plan accepts --config, --rules, --never-touch and --guardrails overrides for plan-only runs. Without --plan-out it prints JSON; without --report-out it prints Markdown. Output paths must be private, distinct, have existing parents, and differ from inputs, state and lock. Execution uses the standard private input names, so put any custom inputs there before planning. Keep those inputs, the original listing and the plan unchanged during execution.

## State and execution files

All files below live in the private store:

- inbox.json: the JSON listing defined above.
- plan.json: JSON actions, decisions, reasons, counts, forecast expected_counts (never saved as state) and not_listed accounts. A stopped plan has no actions; it may still exit 0.
- report.md: escaped Markdown proposals from the planner, never proof of execution. New rules is an empty placeholder; the planner does not create rules.
- state.json: a JSON object mapping configured account handles to non-negative integer remaining Inbox counts. Only commit-state saves it, atomically, from observed counts minus confirmed executed trash/archive/move/spam results. Missing accounts retain their saved counts. Stopped plans save observed counts without deductions, so an empty-Inbox brake need not repeat forever. After removing a configured account, remove its saved entry.
- results.json: an object with only a results array, for example {"results":[{"account":"cedar","id":"item-a","action":"trash","status":"executed"}]}. Each entry has exactly those four string keys and uniquely matches a planned action. See the status table below. Omitted entries did not execute. Only executed removals reduce state counts. The step machine atomically replaces this file after recording results; do not edit it.
- calls.jsonl: append-only JSON objects with sequence, time (Unix seconds), run (lock token), account, id, verb and outcome (issued, a result status, or unplanned). Unplanned calls appear only when disclosed with observed.
- execution-report.md: escaped Markdown handled counts from results and disclosed unplanned calls for the current token. Use it as directed by AGENTS.md.
- execution-<SHA256 of token>.json: the step machine’s checkpoint for this plan and run; do not edit it.
- unsubscribe-failures.json and unsubscribe-secret: keyed sender failure memory and its private key; keep both private and do not edit them.
- run-message.md: a message saved after delivery failed, handled by AGENTS.md.
- run.lock: JSON token and created timestamp; .lock-gate: persistent local coordination file.

Planning does not save state. If a run stops before commit, previous state remains unchanged, even if mail actions completed; the execution files retain evidence. Do not assume a proposed removal happened or replay uncertain writes. Start a later run with a fresh listing and token. Without state and without --first-run, planning stops. Finalizing that stopped plan saves the observed counts; the following run proceeds without --first-run. Ask: "Follow AGENTS.md for one execution run and send the run message." Malformed state is refused even with --first-run. Commit-state revalidates the plan and accepts supplied results only when they match the checkpoint for this token and plan. Hand-written results are refused. Failed planning removes its requested outputs and attempts lock release.

The step machine requires one result before another next. Lost lock returns stop and remaining actions without writing the private store. Observations with duplicate IDs or missing known Inbox IDs not yet removed are refused.

### Result statuses

| Status | Meaning | Counts toward brake | What the user does |
| --- | --- | --- | --- |
| executed | A gateway removal answered ok and the ID left the Inbox; an unsubscribe site answered 2xx. | No; resets that verb’s failure count. | Nothing. Unsubscribe mail may continue for a while. |
| failed | Gateway error, or an attempted HTTPS unsubscribe refused by safety checks or unsuccessful. | Yes. | Check the message or unsubscribe manually if trusted. |
| unexecuted | Gateway answered ok but the ID stayed in Inbox, or a later call was never issued after lock loss. | Yes for the gateway result; no for never-issued calls. | Check the message; a fresh run reconsiders it. |
| skipped | Never issued after two consecutive unsuccessful results of that verb. | No. | Review the preceding failures; a fresh run reconsiders it. |
| unsubscribe_not_executable | Missing or mailto-only URL; no request sent. | No. | Use a trusted manual unsubscribe method if wanted. |
| unsubscribe_not_attempted | Remembered sender, flagged message or non-HTTPS URL prevented contact. | No. | Leave it or use a trusted manual method. |
| issued_unknown | Issued call with no confirmed outcome, shown in remaining actions or the pending summary. | No. | Follow the uncertain-call instructions in AGENTS.md. |

Only failed unsubscribe requests enter failure memory. Never-attempted outcomes do not update the unsubscribe brake. Only executed removals reduce saved counts.


## Scheduling

You supply the scheduler and report channel. Configure the host to start in the checkout with PIGEON_HOME set and this instruction: "Follow AGENTS.md for one scheduled Inbox triage and send the standard run message." Start with attended runs, confirm report delivery and replies, then enable your schedule. Grant minimum file, command and gateway permissions without bypass flags.

No host integration is required by the scripts.

## Known unverified

Live gateway paths, provider recovery semantics, authentication, approval configuration and scheduled report delivery have not been tested by the offline suite. Confirm these privately before unattended use.

## Maintainer publication checks

Changes to the checker, ignore rules, hooks and workflow need maintainer review.

Run `python3 scripts/leak_check.py --history --git-config --deny PATH` on the publication candidate. Only --git-config reads local Git configuration; normal scans, hooks and CI leave it alone. Set PIGEON_DENY to an existing private deny-list folder to add that list to both hooks.

A non-empty PIGEON_DENY that is not an existing folder fails both hooks; an explicit --deny also requires a folder containing deny terms, and any non-hidden file there not named *.txt is refused. GitHub no-reply addresses are accepted only in commit author/committer headers, annotated-tag tagger headers and the explicitly scanned Git user email setting.

## Local unsubscribe client

scripts/unsubscribe.py is a library with no command-line interface; --help on that file does nothing. For an unsubscribe step, run exactly the command array returned by next. It invokes:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py unsubscribe --token TOKEN --plan /absolute/private-store/plan.json
```

It records its own status; do not submit a gateway observation. See the listing and rule tables for one-click and companion fields.

The client allows HTTPS on port 443 only, checks every DNS answer for public addresses, pins a checked address and verifies the host certificate. It follows at most three redirects, rechecking the scheme and addresses at each hop. It refuses credentials in URLs, authentication challenges, password forms and oversized headers; never handle login prompts yourself. It sends no cookies or credentials. Socket timeouts are five seconds. A single 20-second deadline covers DNS, connect, TLS, headers, body and redirects. Response headers are capped at 16 KiB. It reads at most 16 KiB plus one overflow byte and judges successful responses from the status and capped sample. POST changes to GET after 301/302/303; 307/308 preserve POST.

The planner makes unsafe unsubscribe links and provider-flagged spam or phishing decisions for the user; it plans neither the request nor its companion action. Reports show safe planned links as escaped hosts, never clickable links. The client independently checks every issued request. Do not write unsubscribe rules for senders you suspect of spam or phishing; use spam instead. The assistant runs each issued command as given. See the status table for local refusals and remembered failures.

The network request releases the store gate. Ownership and the unchanged checkpoint are checked before saving its outcome or failure memory. Lost ownership leaves the issued call uncertain.
