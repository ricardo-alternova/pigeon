# Set up pigeon

## Developer machine

Run the [sample-mail demo](../README.md#developer-machine), then follow [storage choice](hosted-setup.md#storage-choice), private store, gateway and attended setup before scheduling.

## Calendar input

Configure optional [calendars.NAME] tables with required executor_app, optional executor_account (the mailbox handle checks apply), and optional boolean wins. Set wins = true on at most one calendar. Set optional [calendar] keys: days (integer 1–14, default 3), work_start/work_end and quiet_start/quiet_end (paired increasing HH:MM local times). Use timezone for all boundaries and displayed times.

After a full quiet check, use the gateway's event-listing tool for each configured calendar, found by search and describe. Expand recurring events to single occurrences. Limit the listing to today through days local dates, plus every pending invitation regardless of date. Save calendar.json in the private store and add --calendar /absolute/private-store/calendar.json to plan. Verify the configured executor_app and optional executor_account for each read.

Use a UTF-8 JSON object keyed by configured calendar names. Supply an array of events or {"failed": true} for each name; omit an unread calendar to show it as not read. Give every event id, start, end, all_day (boolean), response (pending, accepted, tentative, declined or organizer), cancelled (boolean), and optional string subject. Use message-style IDs and unique IDs within each calendar. Use ISO 8601 offset times for timed events and YYYY-MM-DD dates for all-day events; set end after start. Refuse unknown fields.

```json
{"oak":[{"id":"event-a","start":"2026-11-12T12:00:00+00:00","end":"2026-11-12T13:00:00+00:00","all_day":false,"response":"pending","cancelled":false,"subject":"Planning"}],"elm":{"failed":true}}
```

Read Calendar after the Inbox list in the report and final message, including dry runs. List pending invitations with working-hours and quiet-window marks; check a midnight-crossing event against working hours by its start. List each overlapping pair in start, calendar-name and ID order, including pairs within one calendar. Exclude cancelled, declined and all-day events from overlaps; ignore nonpending events outside the window. Treat touching endpoints as no overlap. Mark the winning calendar as the one that stays. Show unread calendars with “overlaps against it were not checked”.

Keep calendar items without reply numbers. Answer invitations in the calendar itself. Keep calendar data out of state. Skip calendar reads and the Calendar section on a quiet run.

## Rules

Both files use TOML arrays of tables named [[rules]]. In rules.toml, order matters; see the precedence policy in guardrails. Conditions in an entry all have to match.

| Key | Type and match |
| --- | --- |
| id | Unique required rule string; forbidden in never-touch. |
| enabled | Optional boolean, default true. Set false to load and validate the rule while excluding it from matching, limits and reports. rules.toml only. |
| action | String: trash, archive, move, spam, unsubscribe, draft or report. Required in rules.toml; not allowed in never-touch.toml. report creates a decision, with no action. |
| draft | Required on action draft: reply or forward. Use the mailbox's reply operation to address a reply draft. |
| draft_text | Required on action draft: the owner's full body, 1–4000 characters. Copy it unchanged; take no text from mail. |
| draft_to | Required only for forward: 1–5 bare addresses from the rule. Refuse it on reply; take no recipients from mail. |
| accounts | Nonempty account-handle string array required in rules; optional in never-touch (absent means all). |
| sender | Case-insensitive exact address; accept address or Name <address>. Ignore +tag for never-touch. |
| domain | Case-insensitive exact domain; protect subdomains in never-touch. |
| subject | String, a Python regular expression searched anywhere in the subject without case sensitivity. Escape regex punctuation when you mean literal text. |
| body | Optional non-blank literal substring in the plain-text excerpt, matched with Python re.IGNORECASE without Unicode normalization. Regex punctuation is literal. rules.toml only; absent excerpts do not match. |
| question | Optional non-blank string, at most 500 characters, on report rules only. Leaves the message in Inbox and shows this question with the message under Still in Inbox, without a mailbox call or removal count. Place a settling rule before the question rule. Propose edits through [Writing a rule](../AGENTS.md#writing-a-rule). |
| has_attachment | Optional boolean; absent message flag means false. |
| exclusions | Case-insensitive substring array for subject and body (when used). Require on trash, even []; optional elsewhere, forbidden in never-touch. |
| after_unsubscribe | Optional trash/archive on unsubscribe only. Plan separately after safe unsubscribe even on failure; turn unsafe requests and companions into decisions. |
| limit | Optional integer, 1 to 50 (hard cap), default 10. Counts actions across all accounts of the rule, including unsubscribes and companion removals, excluding folder creation. Exceeding it stops the whole plan with zero actions; the stop reason names the rule id. Report rules create no actions and consume no limit; question rules also consume no twenty-percent brake count. |
| target | Move-only exact folder path. Require a unique existing fixed path; resolve dated {year}/{month} from message date to four/two digits. Leave missing/invalid dates or years outside 1990–2100 as decisions. Require 1–255 characters and at most eight segments; refuse empty, dot, dot-dot, control, format, surrogate, line/paragraph separator, backslash and unknown placeholders. Refuse slash look-alikes U+2044, U+2215, U+2571, U+2572, U+27CB, U+27CD, U+29F5, U+29F8, U+FE68, U+FF0F, U+FF3C. Compare paths with casefold(), without normalization. Leave missing folder information as a decision; refuse leaf-only metadata for nested destinations. Plan one create-folder per missing dated account/path before dependent moves, comparing with casefold() and retaining the first matching rule's spelling. Forbid in never-touch.toml. |

Keep target, after_unsubscribe and question off draft rules. Keep messages in Inbox after creating drafts. Stop above ten drafts per run; apply the per-rule limit and twenty-percent brake. Count no draft toward removals. After sending by hand, file the original with a numbered reply.

Require a sender, domain, subject or body predicate on each rules.toml entry; require sender, domain or subject on never-touch. Refuse invalid regexes and regexes matching empty text.


The top level of rules.toml accepts only rules; use rules = [] explicitly for no rules. The top level of never-touch.toml accepts rules and allow_empty (boolean). An empty never-touch file is refused; to explicitly disable protection replace its entries with allow_empty = true. Never-touch entries accept only accounts, sender, domain, subject and has_attachment.

See the [rule examples](../rules/rules.example.toml), including commented-out examples for every feature.

## Convert a rules table

Convert a UTF-8 Markdown file with five-column pipe tables. Put id, pattern, action, scope and source in that order; choose any header words. Separate each header from its rows with a Markdown separator. Keep prose outside the tables.

```bash
python3 scripts/triage.py convert-rules --table /absolute/table.md --scopes /absolute/scopes.toml --out /absolute/converted.toml --review /absolute/review.txt
```

Choose new output paths. Omit --review to print the review list on stdout. Read the counts as rows read, active, switched off and sent to review. Run this file conversion without a lock or private store.

Write scopes.toml as scope names mapped to nonempty arrays of account handles:

```toml
all = ["cedar", "marble"]
"owner@example.com" = ["cedar"]
```

Separate scope names in a table cell with commas. Put each match token in backticks: `digest@example.net` for an exact sender, `example.org` or `@example.org` for an exact domain, or `subject:Weekly [news]` for literal subject text. Use double backticks around a token containing a backtick; escape a cell pipe as \| or put it inside the code span. Use several sender/domain tokens to produce separate rules with IDs ID, ID-2, ID-3. Send combinations containing a subject token and another token to review.

Use trash, archive, spam, move PATH, mover PATH, unsubscribe, baja or baja HTTPS://example.org/leave as actions, ignoring case around the action words. Preserve move path spelling. Join unsubscribe or baja with + trash or + archive for a companion removal. Keep the source cell as a comment.

Review the three outcomes:

- Active: every cell is understood and the pattern contains only tokens and whitespace.
- Switched off: every cell is understood and the pattern also contains notes or warning marks. Read the note comment above each enabled = false rule. Translate exceptions into exclusions or narrower predicates, then set enabled = true.
- Review: a cell is unknown, the pattern has no token, or an ID is invalid or duplicated. Read each row ID, table line number and fixed reason; correct the original row and convert to a new output path.

Keep converted rules in table order. Apply never-touch protection first, then the first matching enabled rule in file order, under the kit's action limits. Check scopes against config.toml with validate.

Install the converted file as rules.toml in the private store. Run `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py validate`. Take the lock, copy a fixture or fresh Inbox listing to inbox.json in the private store and follow [Dry run](#dry-run) before using the rules.

## Rule cases

Write optional cases.toml in the private store after editing or converting rules. Use [[cases]] entries with a unique nonempty name, account, sender, subject and expect. Add has_attachment, body and expect_rule when needed. Use an action name, none for no matching rule, or never-touch as expect; use a rule ID as expect_rule. Run `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py validate` to check never-touch precedence and the first enabled matching rule. Read the failing case names, expected and found actions/rules, and totals. Correct failures before running mail.

Copy these cases for the example rules:

```toml
[[cases]]
name = "weekly digest"
account = "cedar"
sender = "digest@example.net"
subject = "Weekly news"
expect = "trash"
expect_rule = "digest"

[[cases]]
name = "receipt attachment"
account = "cedar"
sender = "shop@example.org"
subject = "Receipt"
has_attachment = true
expect = "archive"
expect_rule = "receipts"

[[cases]]
name = "owner question"
account = "marble"
sender = "helper@example.com"
subject = "Question"
expect = "none"
```

Keep cases.toml local; supply it from the host's lasting storage. Setup leaves it absent. Validate reads cases without calls or writes.

## Saved tool paths

Follow [Unknown tool path](quickstart.md#unknown-tool-path) for discovery, replacement and path restrictions. Manage entries under the held lock after fetch:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py tool-show
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py tool-drop --token TOKEN --account cedar --kind move
```

Use kinds archive, attachments, create-draft, create-folder, folders, history, ids, labels, message, message-read, move, profile, report, spam, trash and unsubscribe. Fetch and send tool-paths.json with drive state.

## Run message format

Follow [Commit and message](quickstart.md#commit-and-message). Keep plan.json, inbox.json, config.toml, numbers.json, calls.jsonl and the checkpoint. Use committed results, including partial results on a halt; never substitute gateway results. Keep these labels:

| Key | English | Spanish |
| --- | --- | --- |
| inbox | Still in Inbox (by account) | Todavía en Recibidos (por cuenta) |
| planned | Planned actions | Acciones previstas |
| rules | New rules | Reglas nuevas |
| handled | Handled automatically | Gestionado automáticamente |
| notes | Run notes | Notas de ejecución |
| drafts | Drafts | Drafts |
| calendar | Calendar | Calendar |

Show each message once. Group handled messages by account and list action, sender and subject; show every message with a planned action, including failures. Group the Inbox list by account without repeating its name on item lines. Leave unmatched mail untagged; tag protection, refused rules, unconfirmed actions and stopped runs. In dry runs put the planned action inline, including the move destination. Use the saved numbers and print reply examples only when numbers exist. Combine empty accounts on one line without headings. Put account problems directly after the first line, once per account with status and “retried once” only after a retry. Use accounts.HANDLE.name or HANDLE throughout the message and report. Show draft kind, recipients and full text above Inbox; tag successful drafts and unsubscribes on their numbered Inbox lines; handled lists only messages that left Inbox, joining their actions on one line. The first-line handled count counts messages. Tag partial success with done and failed actions; leave it unnumbered. If the message left Inbox, keep it handled and name any failed or unattempted action after the action that ran. Put the stop reason directly after account problems; put unplanned calls and folder help in Run notes. Check uncertain folder creation by hand before creating it. Use config.language and config.timezone. For new_plus_count, show protected/new items and count older items per account. Keep quiet runs silent.

## Decision and stop reasons

Read decisions and reasons in plan.json. Keep decisions in Still in Inbox once, with the saved number and optional question; never number protected messages. Treat report rules, missing/ambiguous move targets, invalid dates, missing folder information, invalid senders, unsafe unsubscribes and unmatched mail as decisions. Follow [Brake refusal](quickstart.md#brake-refusal) for stopped plans and [Run exits](../AGENTS.md#run-exits) for command refusals.

Stop for missing state with prior records, unconfigured accounts, no configured account listed, combined Inbox count above inbox_limit, or an unexpectedly empty account whose saved expected count was at least three. Stop above 50 destructive actions, ten unsubscribes or a rule's limit. Follow the quick start for the per-account twenty-percent brake.

## Scripted listing

Set accounts.HANDLE.provider to gmail or outlook with executor_app and executor_account. Use history for Gmail quiet_kind and ids for Outlook; omission selects the provider default. Follow [Quiet and listing](quickstart.md#quiet-and-listing) and [Account skipped and retry](quickstart.md#account-skipped-and-retry).

Resume a pending listing batch after interruption. Keep listing-exchange.json, final-listing-exchange.json and quiet-gateway.json local and script-owned.

For missing provider, executor_app or executor_account, build the whole listing by hand using the recipes. For body-rule accounts, supply --manual-part PATH on every listing invocation with exactly those accounts and their excerpts; exclude scripted accounts. Read the required names in the missing-part refusal. Let listing merge the parts into inbox.json.

Set every provider for scripted quiet reads; use --observation PATH with [Quiet observations](quickstart.md#quiet-observations) for mixed configurations.

Follow [Plan and execute](quickstart.md#plan-and-execute) for final reads. Carry follow-up batches for arrivals until done: Gmail metadata/attachments or Outlook newest-message pages retaining new IDs. For accounts without providers, supply a complete final-inbox.json through final --inbox PATH.

## First real run and commands

A store without state.json is new only when numbers.json, history.log, calls.jsonl and decisions.jsonl are all absent. Run the first plan without a flag. If any of those records exists without state.json, stop for missing state.

Use `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py check --token TOKEN` to inspect ownership; require a held, unexpired token. Follow [Release](quickstart.md#release) for conditional drive release; local release accepts already-released holders, refuses another token.

Use plan --config, --rules, --never-touch and --guardrails for overrides. Without --plan-out read JSON; without --report-out read plain text. Choose private, distinct output paths with existing parents, separate from inputs/state/lock. Copy custom inputs to standard private names before planning; keep them, the listing and plan fixed during execution.

## Dry run

Follow [Dry run](quickstart.md#dry-run).

## Numbered replies

Follow [Numbered reply](quickstart.md#numbered-reply). Leave numbering to the planner; never reset its counter. A number stays bound to its account and message until retired. Keep numbers for unlisted accounts. Stores without numbers.json plan normally.

Keep reply.txt UTF-8, verbatim and at most 2000 characters. Use ASCII numbers of at most nine digits. Verbs ignore case and surrounding spaces; retain move-path spelling and compare with Unicode casefold without normalization. Allow spaces, nested paths and non-English letters, but no reply separators inside targets. leave, yes and no keep the number and make no call.

Refuse the whole reply for empty, malformed, oversized or control-bearing text, unsupported verbs, unknown/repeated numbers, messages absent from their fresh Inbox, never-touch matches, invalid senders or missing/ambiguous targets. Execute no partial reply. Keep normal listing validation, brakes, global ceilings and the default ten-action limit. Omit --scheduled; it gives zero actions. Use plan's safety/input/output options when needed.

## State and execution files

Keep every file in the private store. Keep the original inbox.json and plan inputs unchanged. Use final-inbox.json only for the final complete reading. Keep script-owned files unedited: numbers.json, reply.json, decisions.jsonl, state.json, results.json, calls.jsonl, execution checkpoints, finish-exchange.json, quiet-check.json, unsubscribe-failures.json and unsubscribe-secret. Keep the last two private. Keep final-inbox.json and finish-exchange.json local.

- numbers.json: reply counter and account/message bindings.
- reply.txt: verbatim user text, never a call template.
- plan.json: proposals and stop reasons; dry_run=true marks rehearsal.
- report.md: plain-text proposals, refreshed at commit from confirmed results. Leave New rules empty; write rules only through the owner-confirmation procedure.
- state.json: version 1 counts and reserved _positions and _drafts; remove an account's saved entry after removing it from config. Preserve uncertain drafts (null draft_id); check Drafts before any manual action. Keep the original's reply number; retire its draft record after it leaves Inbox.
- results.json: script-owned confirmed/failed/uncertain outcomes; omitted entries did not execute. Use this run's own file for commit-state, never hand-written results or another --results path.
- execution-report.md: handled counts, unfinished actions, folder help and disclosed unplanned calls. Follow AGENTS.md for delivery.
- history.log: quiet skips and abandoned lock exchanges.
- run-message.md: pending delivery record, containing an undelivered chat message or an upload warning; follow [Commit and message](quickstart.md#commit-and-message).
- quiet-observation.json: pre-listing [observations](quickstart.md#quiet-observations).
- run.lock and .lock-gate: local coordination; never remove them to bypass ownership.

Commit only under the held lock with this run's plan and checkpoint. Planning writes no state; preserve previous state until commit, even after completed mail actions. Refuse malformed state. Follow [Commit and message](quickstart.md#commit-and-message) and [Gateway results](quickstart.md#gateway-results).

For a draft without a provider, search and describe its creation tool. Carry only issued draft_text and draft_to; use the mailbox reply operation for reply. Return {"outcome":"ok","draft_id":"draft-a"}; use error for failure or unknown for uncertainty. Require a draft_id of 1–512 ASCII letters, digits or _ - = + / for executed. Otherwise retain uncertain evidence and tell the owner "check Drafts by hand". Never retry an uncertain draft in this or a later run. Send drafts by hand from the mailbox.

For accounts without providers, return outcome with in_inbox boolean or complete inbox_ids, read after the action. Count removal only for ok and in_inbox=false; keep ok/true unexecuted, error/true failed, error/false issued_unknown. Use whole Inbox IDs for create-folder. Use optional observation.account for another listed account; confirm a result with its own account's reading. Refuse duplicate IDs and wrong types.

Confirm create-folder with its returned folder_id and complete folder listing before dependent moves. Require an ID of 1–512 characters, nonblank, without Unicode Cc/Cf/Cs/Zl/Zp, distinct from listed and earlier created IDs, uniquely matching a planned creation.

Follow [Outside change stop](quickstart.md#outside-change-stop). Finish final_reads before commit and include arrivals in the final Inbox message. Report account and missing count. Never reissue an unreported batch.

### Step machine responses

Read one JSON object on success or a nonzero one-line Refused: on stderr; follow [Run exits](../AGENTS.md#run-exits). Carry issued actions once, verify and submit results. Execute a run argument list once, never as shell source; ask next afterward. Ask next after a status receipt. Include disclosed calls in the message. Finish done=true, including stopped plans; complete final_reads first when requested. On lock loss follow No lock. Never execute remaining entries.

| Key | Read or act |
| --- | --- |
| `path` | Gateway tool path; pass the call unchanged. |
| `input` | Gateway accountId and provider input. |
| `tool` | Use the saved gateway path. |
| `account` | Configured mailbox handle. |
| `id` | Message ID; create-folder's first dependent message is bookkeeping, never a gateway input. |
| `action` | Issued verb. |
| `rule` | Bookkeeping, never a gateway input. |
| `target` | Move destination ID. |
| `target_path` | Exact folder path; create only for create-folder. |
| `url` | Uncertain/unissued unsubscribe evidence; follow [guardrails](../config/guardrails.example.md). |
| `executor_account` | Configured mailbox ID. |
| `executor_app` | Configured app. |
| `verify` | Carry the subsequent read. |
| `read` | Post-action membership or complete Inbox IDs. |
| `run` | Execute the local unsubscribe argument list. |
| `status` | Read the status table; remaining means issued_unknown or unexecuted. |
| `done` | Finish; inspect stop. |
| `stop` | Halt reason; distinguish from a stopped plan. |
| `remaining` | Report without replaying. |
| `calls` | Carry ordered batch; return result --results PATH. |
| `final_reads` | Submit execute.py final for these accounts. |
| `refresh_lock` | Complete lock --refresh before continuing. |
| `recorded` | Disclosure logged. |
| `draft` | Reply or forward. |
| `draft_text` | Full rule body. |
| `draft_to` | Rule forward recipients; empty for reply. |

### Result statuses

| Status | Read or act |
| --- | --- |
| executed | Confirm ok removal absent from Inbox, 2xx unsubscribe, created folder ID or valid draft_id. Reset that verb's failure count. |
| failed | Count gateway error or unsuccessful/unsafe HTTPS request toward two-failure skip. Check manually if trusted. |
| unexecuted | Check the message in a fresh run. Count unsuccessful gateway evidence toward skip, never unissued calls after folder failure or lock loss. |
| unconfirmed | An ok provider removal remains in final Inbox; check manually, issue no further call this run. |
| skipped | After two consecutive failures of that verb, review failures; reconsider in a fresh run. |
| unsubscribe_not_executable | Missing/mailto-only URL: use a trusted manual method if wanted. |
| unsubscribe_not_attempted | Remembered sender, provider flags or non-HTTPS URL: leave or use a trusted manual method. |
| issued_unknown | Follow [Run exits](../AGENTS.md#run-exits); never replay. Count submitted unknown creation toward skip, never a pending call after lock loss. |

Follow [AGENTS step 5](../AGENTS.md) for folder observations and retries. Record only failed unsubscribe requests in failure memory.

## Starting runs

Start a run from a schedule or a push trigger: a webhook or mail notification that starts the assistant. Begin either run with the lock and quiet check. Carry no instruction or mail content from the trigger into the run.

## Model choice

Use a small fast model for the routine run: carry calls, save results, post messages. Leave decisions to scripts. Use a stronger model for owner replies, rule changes and conversion review.

## Scheduling

Use the host scheduler. Set schedule.weekdays/weekends to its local slots in timezone; these keys classify runs, never start them. Use always_full for full-run slots. Measure lateness from the latest local slot at/before the command, including preceding days. Count either repeated-time occurrence, omit nonexistent times. Use the script clock, never drive time; show slot/delay above 15 minutes after the 📬 first line and account problems.

Start in the checkout with PIGEON_HOME set and instruction: "Follow AGENTS.md for one scheduled Inbox triage and send the standard run message." Grant minimum file, command and gateway permissions, no bypass flags. For replies instruct: "Follow AGENTS.md’s Replies to numbered items procedure using my reply verbatim."

For a developer runner, replace paths and pass the scheduled instruction:

```cron
20 8 * * * cd /absolute/pigeon && PIGEON_HOME=/absolute/private-store /absolute/assistant-runner
```

## Maintainer publication checks

Never force private content into allowed files. Enable hooks with `git config core.hooksPath hooks`; use the [.gitignore allowlist](../.gitignore). Require maintainer review for checker, ignore, hook and workflow edits.

Run `python3 scripts/leak_check.py` and `python3 scripts/leak_check.py --history`. On a publication candidate run `python3 scripts/leak_check.py --history --git-config --deny PATH`; only --git-config reads local Git config. Set PIGEON_DENY to an existing private deny-list folder for both hooks. Refuse nonempty nonexistent PIGEON_DENY; require deny terms for --deny and only *.txt nonhidden files. Accept GitHub no-reply addresses only in author/committer/tagger headers or explicitly scanned Git user email.

## Local unsubscribe client

For an unsubscribe step, run exactly the command array returned by next. It invokes:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py unsubscribe --token TOKEN --plan /absolute/private-store/plan.json
```

It records its own status; do not submit a gateway observation. See the listing and rule tables for one-click and companion fields.

The client allows HTTPS on port 443 only, checks every DNS answer for public addresses, pins a checked address and verifies the host certificate. It follows at most three redirects, rechecking the scheme and addresses at each hop. It refuses credentials in URLs, authentication challenges, password forms and oversized headers; never handle login prompts yourself. It sends no cookies or credentials.

The planner makes unsafe unsubscribe links and provider-flagged spam or phishing decisions for the user; it plans neither the request nor its companion action. Reports show safe planned unsubscribe destinations as hosts; mail values remain plain text on their item lines. Do not write unsubscribe rules for senders you suspect of spam or phishing; use spam instead. See the status table for local refusals and remembered failures.

The network request releases the store gate. Ownership and the unchanged checkpoint are checked before saving its outcome or failure memory. Lost ownership leaves the issued call uncertain.

## Storage validation

Limit lock observations to 4096 bytes; IDs to 1–512 ASCII letters, digits or _ - . : !; tags to 1–512 ASCII letters, digits or _ - ! . , : { } or double quotes. Use UTC timestamps of at most 40 characters and at most five seconds future skew.

Require fetched files to be readable regular nonsymlink files, at most 4194304 bytes, valid UTF-8 and valid format. Refuse missing/unexpected files before installation. Start absent script-owned files empty. Keep state-incoming free of links/subfolders; the next fetch removes leftover regular files.

Keep calls.jsonl, history.log and decisions.jsonl to the script-owned 262144-byte newest-whole-line tails; never trim during execution, reporting, reply commit or send.

## Quiet observation validation

For history use a 1–128 ASCII-digit position and up to 1000 Inbox arrival IDs. For ids use at most ten unique newest-first IDs, each 1–512 ASCII letters, digits or _ - = + /. Run full for unknown fields, missing/extra accounts, wrong types, files over 65536 bytes, unreadable/absent files or decreasing positions. Prove quiet only with empty arrivals or unchanged ordered IDs for every account.
