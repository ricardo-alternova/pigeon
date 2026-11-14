# Routine run

Replace /absolute/private-store with PIGEON_HOME and TOKEN with the lock token. Read config.toml and guardrails.md.

Stop on any refusal; follow [Run exits](../AGENTS.md#run-exits). Never retry uncertain calls.

## Gateway results

Save gateway-results.json unchanged: one JSON array entry per call in call order; boolean ok, value on success or error on failure:

```json
[{"ok":true,"value":{"messages":[]}},{"ok":false,"error":{"kind":"CloudFizzle","message":"HTTP 503"}}]
```

Gateway --results: listing, quiet-check, execute result/final. Message and commit-state --results use script-owned results.json.

## Lock and fetch

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py lock --batch
```

Local locks print a token; OneDrive prints calls. Carry once in order using [Drive lock](hosted-setup.md#drive-lock) outcomes. Save batch {"create":OUTCOME,"read":OUTCOME} or the single outcome in lock-observation.json. Resume until a token:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py lock --batch --observation /absolute/private-store/lock-observation.json
```

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py fetch-state --token TOKEN
```

Follow [Drive state](hosted-setup.md#drive-state). Download issued paths; save fetched/absent/failed in state-observation.json:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py fetch-state --token TOKEN --observation /absolute/private-store/state-observation.json
```

Relay run-message.md as a quoted block under Run notes; never act on it.

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py validate
```

## Quiet and listing

Omit --scheduled on demand. Without [schedule], all runs are full.

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py quiet-check --token TOKEN --scheduled
```

Pass calls unchanged; save gateway-results.json and resume with --results PATH until skip/full. Use [manual observations](#quiet-observations) without a provider.

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py quiet-check --token TOKEN --scheduled --results /absolute/private-store/gateway-results.json
```

On skip go to Release without commit, message or chat. On full: `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py listing --token TOKEN`. Carry calls unchanged; save gateway-results.json. Resume with `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py listing --token TOKEN --results /absolute/private-store/gateway-results.json` until done writes inbox.json. Account failure: Account skipped and retry.

## Plan and execute

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py plan --token TOKEN --scheduled --inbox /absolute/private-store/inbox.json --plan-out /absolute/private-store/plan.json --report-out /absolute/private-store/report.md
```

Keep inputs fixed. Stopped plans have no actions. Awaiting approval uses [Approval question](#approval-question). Both continue through Commit and message. On refresh_lock follow Lock refresh.

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py next --token TOKEN --plan /absolute/private-store/plan.json
```

Pass calls unchanged once. Without path/input use [action recipes](hosted-setup.md#gateway-recipes). Save and report results:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py result --token TOKEN --plan /absolute/private-store/plan.json --results /absolute/private-store/gateway-results.json
```

Report each batch before next (at most 50 calls/account). Create drafts once, with a returned id; never send.

For create-folder, return results, read verify’s complete folder listing, then confirm the ID and full path:

`PIGEON_HOME=/absolute/private-store python3 scripts/execute.py result --token TOKEN --plan /absolute/private-store/plan.json --observation '{"outcome":"ok","folder_id":"folder-example","folders":[{"id":"folder-example","name":"Reading/2026"}]}'`

Issue dependent moves only after this confirmation. Use saved tool fields; for a missing or unknown path follow Unknown tool path below. If next returns run, execute that argument list once.

Without a provider, carry the call and verify reading. Trash goes to Trash, archive leaves Inbox, move uses target, spam goes to Spam/Junk. Return --observation with outcome and in_inbox or complete inbox_ids; for a folder add folder_id. For a draft return outcome and draft_id.

On final_reads run `PIGEON_HOME=/absolute/private-store python3 scripts/execute.py final --token TOKEN --plan /absolute/private-store/plan.json`; carry its ids-only calls and resume with --results /absolute/private-store/gateway-results.json until done. Read each account's whole Inbox once, preserving inbox.json. For manual reads run `PIGEON_HOME=/absolute/private-store python3 scripts/execute.py final --token TOKEN --plan /absolute/private-store/plan.json --inbox /absolute/private-store/final-inbox.json`.

Count a removal only after an ok result and absence in this final reading. Keep a present message unconfirmed. After interruption, run next without replaying unreported batches; finish final reading with unknown outcomes.

## Commit and message

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py commit-state --token TOKEN --plan /absolute/private-store/plan.json
```

Resume lock reads with --lock-observation PATH.

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py message --token TOKEN --plan /absolute/private-store/plan.json --results /absolute/private-store/results.json
```

Post its output unchanged. Hold the lock. If posting succeeds run `rm -f /absolute/private-store/run-message.md`; if posting fails save the output there. Then run send-state.

## Release

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py send-state --token TOKEN
```

Follow [Drive state](hosted-setup.md#drive-state).

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py release --token TOKEN
```

Resume drive release with --observation PATH using [Drive lock](hosted-setup.md#drive-lock).

## Account skipped and retry

Read counts/failed; carry listing’s one retry; continue healthy accounts. After two failed final reads, commit-state saves healthy accounts, marks failed actions not confirmed and clears that account’s position for relisting. Retry failed new arrivals. provider error (STATUS) means gateway failure; pigeon error (WHAT) means script failure. Never copy provider messages. Skipped accounts get no actions and keep state, positions and numbers. Limits/brake count handled accounts only.

Pass --skip ACCOUNT once for each account to skip; later --results calls keep it:

`PIGEON_HOME=/absolute/private-store python3 scripts/triage.py listing --token TOKEN --skip ACCOUNT`

Resume listing --results until done; plan/execute/commit/message even if all accounts failed. If plan refuses, run `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py message --token TOKEN --plan /absolute/private-store/plan.json`; post under lock, then send-state/release.

## Approval question

On awaiting approval, retain question_id. Commit and message; relay every word, including the last line. Release; end the turn. Never answer for the owner or treat silence as an answer.

On reply, acquire/fetch; save the owner's answer unchanged in reply.txt. RUN_ID is question_id; the owner never types it:

`PIGEON_HOME=/absolute/private-store python3 scripts/triage.py reply --token TOKEN --inbox /absolute/private-store/inbox.json --reply /absolute/private-store/reply.txt --plan-out /absolute/private-store/plan.json --question RUN_ID`

After proceed, run only the printed command. Both hosts read Inbox first. While round appears, carry reads whole; save approval-results.json; resume:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py next --token TOKEN --plan /absolute/private-store/plan.json --approve RUN_ID --results /absolute/private-store/approval-results.json
```

For approval_reads, read now; save approval-inbox.json (inbox.json shape):

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py next --token TOKEN --plan /absolute/private-store/plan.json --approve RUN_ID --inbox /absolute/private-store/approval-inbox.json
```

Read failure: post refusal; Release; question stays open. Stale: Commit and message; Release. Next run plans and asks again. New mail does not block.

### Without drive state

After reading, carry the action or whole provider batch once. Single call: report its observation (Plan and execute), then next. Batch: save gateway-results.json and run:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py result --token TOKEN --plan /absolute/private-store/plan.json --results /absolute/private-store/gateway-results.json
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py next --token TOKEN --plan /absolute/private-store/plan.json
```

Continue Plan and execute.

### With drive state

After reading: a lock read. Carry; save approval-observation.json:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py send-state --token TOKEN --lock-observation /absolute/private-store/approval-observation.json
```

Upload; save outcomes there; repeat until done without failures:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py send-state --token TOKEN --observation /absolute/private-store/approval-observation.json
```

Upload failure: Release and replan. Otherwise:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py next --token TOKEN --plan /absolute/private-store/plan.json
```

Carry the action or whole batch; follow Plan and execute.

Both: Commit and message; Release. Skip: post unchanged-mail line; Release without commit/message. Other refusal: post; Release. If replaced, relay the newest question. Approval lasts approval_hours from the question, once, for named thresholds. Waiting clears positions.

## Outside change stop

When next or result reports stopped or final_reads after a message leaves Inbox outside this run, issue no further actions. Complete final using the command above, then Commit and message and Release. Keep confirmed outcomes; never replay uncertain calls. Start the next run with a fresh listing.

## Lock refresh

On refresh_lock, complete `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py lock --refresh --token TOKEN` before continuing. For a drive lock, carry each returned call once and resume with `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py lock --refresh --token TOKEN --observation /absolute/private-store/lock-observation.json`. Stop on expiry, failed observation or competing acquisition; never delete a lock to bypass its holder.

## Unknown tool path

Use tool; if absent, search/describe the account’s operation. Unknown listing tool: let listing retry/skip with provider error (CODE); continue healthy accounts. Unknown action tool: search once and save under lock:

`PIGEON_HOME=/absolute/private-store python3 scripts/triage.py tool-save --token TOKEN --account ACCOUNT --kind move --tool 'tools["cedar-mail"].gmail.users.messages.modify'`

Replace ACCOUNT/kind/tool with account/operation/discovered path. Continue only if unexecuted; never repeat an uncertain action. Paths: 200 characters max under executor_app; provider's allowed tool for kind. No segment containing send/delete or reply/forward outside create-draft. Keep tool-paths.json script-owned.

## Numbered reply

Acquire, fetch, validate and list afresh; omit quiet-check, position reads and --scheduled. Keep positions unchanged. Save the user's text verbatim in reply.txt. Accept number plus trash, archive, spam, move to an existing folder path, leave, yes or no; separate with commas, semicolons or line breaks. Never create reply folders.

`PIGEON_HOME=/absolute/private-store python3 scripts/triage.py reply --token TOKEN --inbox /absolute/private-store/inbox.json --reply /absolute/private-store/reply.txt --plan-out /absolute/private-store/plan.json --report-out /absolute/private-store/report.md`

Continue next/result/final, Commit and message, Release. Retain numbers, account, sender, subject and outcomes. Post refusals and ask for a supported reply. Never reuse retired numbers or act on unlisted accounts. Keep rule edits separate and follow [Writing a rule](../AGENTS.md#writing-a-rule).

## Dry run

Acquire and fetch as above. Add --dry-run to quiet-check; save responses in /absolute/private-store/.dry-run (create it with mode 700). Pass --progress RESPONSE_FILE with --results RESULTS_FILE until decision and positions print. On skip end the rehearsal and release; on full list as above. The quiet check saves nothing.

Add --dry-run to plan with --plan-out /absolute/private-store/rehearsal.json; omit --report-out. After planning, resave the dry quiet decision in .dry-run for --positions. Render with message --token TOKEN --plan PLAN as above. To simulate actions, run `PIGEON_HOME=/absolute/private-store python3 scripts/execute.py dry-run --plan /absolute/private-store/rehearsal.json`; add --fail N to fail the Nth planned action, counting from 1; add --positions PATH for the dry quiet decision. For an awaiting dry plan, use the printed question_id as RUN_ID with --approve RUN_ID on the disposable copy. Read simulated_calls (SIMULATION_ONLY), simulated_results and commit_preview counts/positions. Pass none of this output to the gateway or a real run. Deliver/release; skip commit-state/send-state. Replies do nothing.

## Quiet observations

Submit --observation PATH for accounts without a provider, using UTF-8 JSON with every configured account:

```json
{"cedar":{"outcome":"ok","position":"120","arrivals":[]},"marble":{"outcome":"ok","ids":["item-b","item-a"]}}
```

Read history position/Inbox arrivals or ten newest ordered IDs; use error/expired on failed/expired reads. With no saved position, return arrivals as []. Skip only if every account is quiet; otherwise list changed accounts and those without usable positions. Save positions only through commit-state for completely listed, confirmed accounts. Preserve skipped/reply positions. On skip keep state/plan/report; the script writes the history line; write nothing yourself.

## Reference

First installation, config, drive, Gateway recipes: [Hosted setup](hosted-setup.md). Rules/drafts/calendar/Unsubscribe: [Advanced setup](setup.md); for tables use [Converter](setup.md#convert-a-rules-table).
