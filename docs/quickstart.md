# Routine run

Replace /absolute/private-store with PIGEON_HOME and TOKEN with the lock token. Read private config.toml and guardrails.md.

Stop on any refusal; follow [Run exits](../AGENTS.md#run-exits). Never retry an uncertain call.

## Gateway results

Save the gateway response unchanged as gateway-results.json: a JSON array with one entry per call, in call order. Each entry holds boolean ok and value on success or error on failure:

```json
[{"ok":true,"value":{"messages":[]}},{"ok":false,"error":{"kind":"CloudFizzle","message":"HTTP 503"}}]
```

Use this file for listing, quiet-check, action result and final --results. For message and commit-state, --results names the script-owned results.json instead; never pass gateway results there.

## Lock and fetch

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py lock --batch
```

A local lock prints the token. A OneDrive lock prints calls; carry them once in order. Use [Drive lock](hosted-setup.md#drive-lock) outcomes. Save batch {"create":OUTCOME,"read":OUTCOME} in lock-observation.json; for a single call save its outcome. Resume until a token:

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

Omit --scheduled on demand. Without a [schedule] table, every run is full. A provider account has accounts.HANDLE.provider set.

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py quiet-check --token TOKEN --scheduled
```

Pass calls unchanged; save gateway-results.json and resume with --results PATH until skip/full. Use [manual observations](#quiet-observations) without a provider.

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py quiet-check --token TOKEN --scheduled --results /absolute/private-store/gateway-results.json
```

On skip go to Release without commit, message or chat. On full run `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py listing --token TOKEN`. Carry calls unchanged; save gateway-results.json. Resume with `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py listing --token TOKEN --results /absolute/private-store/gateway-results.json` until done writes inbox.json. On account failure follow Account skipped and retry below.



## Plan and execute

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py plan --token TOKEN --scheduled --inbox /absolute/private-store/inbox.json --plan-out /absolute/private-store/plan.json --report-out /absolute/private-store/report.md
```


Keep inputs fixed. A stopped plan has no actions; continue through Commit and message. On refresh_lock follow Lock refresh below.

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py next --token TOKEN --plan /absolute/private-store/plan.json
```

For a provider account, pass the calls array unchanged once; for calls without path and input, build each from the [action recipes](hosted-setup.md#gateway-recipes). Save its gateway results unchanged, then report them:

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/execute.py result --token TOKEN --plan /absolute/private-store/plan.json --results /absolute/private-store/gateway-results.json
```

Repeat next after reporting each batch. Batches hold at most 50 calls per account. For create-draft create once; never send. A draft needs a returned id.

For create-folder, return its results first, read the complete folder listing requested by verify, then confirm the returned ID and full path:

`PIGEON_HOME=/absolute/private-store python3 scripts/execute.py result --token TOKEN --plan /absolute/private-store/plan.json --observation '{"outcome":"ok","folder_id":"folder-example","folders":[{"id":"folder-example","name":"Reading/2026"}]}'`

Issue dependent moves only after this confirmation. Use saved tool fields; for a missing or unknown path follow Unknown tool path below. If next returns run, execute that argument list once.

Without a provider, carry the single call and its verify reading. Trash goes to Trash, archive leaves Inbox, move uses target, spam goes to Spam/Junk. Return --observation with outcome and in_inbox or complete inbox_ids; for a folder add folder_id. For a draft return outcome and draft_id.

On final_reads run `PIGEON_HOME=/absolute/private-store python3 scripts/execute.py final --token TOKEN --plan /absolute/private-store/plan.json`; carry its ids-only calls and resume with --results /absolute/private-store/gateway-results.json until done. Read each account's whole Inbox once, preserving inbox.json. For manual reads run `PIGEON_HOME=/absolute/private-store python3 scripts/execute.py final --token TOKEN --plan /absolute/private-store/plan.json --inbox /absolute/private-store/final-inbox.json`.

Count a removal only after an ok result and absence in this final reading. Keep a present message unconfirmed. After an interruption, run next; never replay an unreported batch. Finish its final reading and keep its outcomes unknown.

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

Follow [Drive state](hosted-setup.md#drive-state); warnings first.

```bash
PIGEON_HOME=/absolute/private-store python3 scripts/triage.py release --token TOKEN
```

Resume drive release with --observation PATH using [Drive lock](hosted-setup.md#drive-lock).

## Account skipped and retry

Read counts and failed in listing output. The command retries a failing account once; carry that batch like the first. After its second failure, continue the healthy accounts. If a final reading fails twice, commit-state saves healthy accounts, keeps failed actions not confirmed and clears that account’s position for a full listing next run. The retry batch reads a failed new arrival again. Read provider error (STATUS) as a gateway failure (status only), and pigeon error (WHAT) as the fixed script reason. Never copy provider error messages. Keep skipped accounts out of actions; keep their state, positions and numbers. Compute limits and the brake on handled accounts only.

Pass --skip ACCOUNT once for each account to skip; later --results calls keep it:

`PIGEON_HOME=/absolute/private-store python3 scripts/triage.py listing --token TOKEN --skip ACCOUNT`

Resume with the listing --results command above until done, then plan, execute, commit and message even when every account failed. If planning refuses, render the failure with `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py message --token TOKEN --plan /absolute/private-store/plan.json`; post it while holding the lock, then send-state and release.

## Brake refusal

Read the rule, account, match count, all-folder total and allowed count in the stop reason. Up to 10 matches per rule/account pass the twenty-percent brake; above 10 the allowance is max(10, floor(total / 5)). Keep the stopped plan unchanged and issue no actions. Run next, Commit and message, then Release. Correct the private rules or listing before a fresh run; never bypass a brake.

## Outside change stop

When next or result reports stopped or final_reads after a message leaves Inbox outside this run, issue no further actions. Complete final using the command above, then Commit and message and Release. Keep confirmed outcomes; never replay uncertain calls. Start the next run with a fresh listing.

## Lock refresh

On refresh_lock, complete `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py lock --refresh --token TOKEN` before continuing. For a drive lock, carry each returned call once and resume with `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py lock --refresh --token TOKEN --observation /absolute/private-store/lock-observation.json`. Stop on expired token, failed observation or competing acquisition. Never delete a lock to bypass its holder.

## Unknown tool path

Use an issued tool field without searching. If absent, search and describe the account's tool for that operation. If a listing tool is unknown, let listing retry and skip that account with provider error (CODE); continue the healthy accounts. For an unknown action tool, search once and save the replacement under the held lock:

`PIGEON_HOME=/absolute/private-store python3 scripts/triage.py tool-save --token TOKEN --account ACCOUNT --kind move --tool 'tools["cedar-mail"].gmail.users.messages.modify'`

Replace ACCOUNT, kind and tool with the configured account, issued operation and discovered path. Continue the issued operation only when it was not executed; never repeat an uncertain action. Keep paths under the account's executor_app, at most 200 characters. Use the provider's allowed tool for that kind; never use a segment containing send or delete, or reply/forward outside create-draft. Keep tool-paths.json script-owned.

## Numbered reply

Acquire a new lock and fetch and validate, then get a fresh listing as above, without quiet-check or a position read. Omit --scheduled. Keep saved positions unchanged. Save the user's text verbatim in reply.txt. Accept number plus trash, archive, spam, move to an existing folder path, leave, yes or no; separate parts with commas, semicolons or line breaks. Never create a folder for a reply.

`PIGEON_HOME=/absolute/private-store python3 scripts/triage.py reply --token TOKEN --inbox /absolute/private-store/inbox.json --reply /absolute/private-store/reply.txt --plan-out /absolute/private-store/plan.json --report-out /absolute/private-store/report.md`

Continue next, result, final, Commit and message, Release. Retain the printed numbers, account, sender and subject with the outcomes. On refusal execute nothing; post the refusal and ask for a supported reply. Never reuse a retired number or act on an unlisted account. Keep rule edits separate and follow [Writing a rule](../AGENTS.md#writing-a-rule).

## Dry run

Acquire and fetch as above. Add --dry-run to quiet-check; save responses in /absolute/private-store/.dry-run (create it with mode 700). Pass --progress RESPONSE_FILE with --results RESULTS_FILE until decision and positions print. On skip end the rehearsal and release; on full list as above. The quiet check saves nothing.

Add --dry-run to plan; omit --report-out: a dry run writes no report. After planning, resave the dry quiet decision in .dry-run for --positions. Render with message --token TOKEN --plan PLAN as above. To simulate actions, run `PIGEON_HOME=/absolute/private-store python3 scripts/execute.py dry-run --plan /absolute/private-store/plan.json`; add --fail N to fail the Nth planned action, counting from 1; add --positions PATH for the dry quiet decision. Read simulated_calls (SIMULATION_ONLY), simulated_results and commit_preview counts/positions. Pass none of this output to the gateway or a real run. Deliver/release; skip commit-state/send-state. Replies do nothing.

## Quiet observations

Submit --observation PATH for accounts without a provider, using UTF-8 JSON with every configured account:

```json
{"cedar":{"outcome":"ok","position":"120","arrivals":[]},"marble":{"outcome":"ok","ids":["item-b","item-a"]}}
```

Read history position/Inbox arrivals or ten newest ordered IDs; use error/expired on failed/expired reads. With no saved position, return arrivals as []. Skip only if every account is quiet; otherwise list changed accounts and those without usable positions. Save positions only through commit-state for completely listed, confirmed accounts. Preserve skipped/reply positions. On skip keep state/plan/report; the script writes the history line; write nothing yourself.

## Reference

First installation, config, drive and Gateway recipes: [Hosted setup](hosted-setup.md). For rules, drafts, calendar and Unsubscribe use [Advanced setup](setup.md); for tables use [Converter](setup.md#convert-a-rules-table).
