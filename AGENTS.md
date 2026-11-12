# Run pigeon

For a routine run, read only [docs/quickstart.md](docs/quickstart.md). Open this document for refusals, owner replies and rule changes. Open [setup](docs/setup.md) for installation, formats and gateway recipes.

Follow [guardrails](config/guardrails.example.md).

## Run exits

Choose one exit when directed below. Once an exit is selected, make no further mailbox changes. A stopped plan (nonempty reasons, exit 0) uses Finish. An outside-change response with status=stopped uses Finish. An execution halt has done=true and `stop`; a refusal exits nonzero with Refused:. On final_reads complete the final reading before Finish.

| Exit | Commit | Send saved files | Release | Owner chat |
| --- | --- | --- | --- | --- |
| Finish | Step 6, confirmed results only; stopped plans save observed counts | Step 8 after fetch | Step 8 | Standard run message prepared in step 7, including reasons for a stopped plan |
| Quiet | No commit | Step 8 send-state uploads history.log when drive state is enabled | Step 8 release | Nothing in chat unless an upload or release warning occurs; no plan, report or run message is produced |
| Halt held | No commit or retry of a refused commit | Step 8, only legitimately changed files after fetch; skip a refused or failed send exchange | Step 8 | Standard stopped message from step 7 with refusal, confirmed outcomes and uncertain or remaining actions |
| Fetch refused | No commit | No send-state | Step 8 | Standard stopped message with the fetch refusal and no mailbox actions |
| No lock | No commit; no private-store writes or new acquisition this run | No send-state | No release | Prepare step 7 from available evidence and post it immediately in the chat; no later step delivers it. If chat delivery fails, surface that failure in this conversation |

Lock loss at any step selects No lock. Other refusals after acquisition use the named exit for that step. If ownership is uncertain, use `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py check --token TOKEN`: success selects the held exit; refusal selects No lock. A send-state refusal selects Halt held if ownership remains valid, otherwise No lock. A release refusal selects No lock and is reported in chat.

## Run steps

Follow [the routine sequence](docs/quickstart.md). For exceptions use these numbered references.

1. Resolve the [private store](docs/setup.md#private-store).
2. Complete lock and fetch using [Drive lock](docs/setup.md#drive-lock) and [Drive state](docs/setup.md#drive-state).
3. Use the [gateway recipes](docs/setup.md#gateway-recipes) and [listing format](docs/setup.md#inbox-listing). On quiet-check refusal use Halt held, or No lock on ownership failure.
4. On planning refusal keep its Refused: line and check ownership. Nonempty plan reasons with exit 0 are a stopped plan: use Finish.
5. Interpret [step-machine responses](docs/setup.md#step-machine-responses). If a gateway cannot perform the issued operation exactly, return error. For create-folder never create missing parents. For an observation refusal, re-read Inbox and resubmit a corrected observation once without repeating the action. On status=stopped use Finish. On other stop use No lock. On other refusals use Halt held if ownership is valid. For uncertain creation follow [folder help](docs/setup.md#run-message-format); for other uncertain calls ask the owner to check the Inbox. Disclose an unissued action while held:
   ```bash
   PIGEON_HOME=/absolute/private-store python3 scripts/execute.py observed --token TOKEN --account cedar --id item-a --verb extra
   ```
   If disclosure refuses, include it and the call in chat.
6. Commit only confirmed results; do not read positions after actions. Follow [state files](docs/setup.md#state-and-execution-files) on interruption.
7. For missing plans/results or lock loss, post the refusal and confirmed outcomes immediately without more store reads. Use the [pending delivery record](docs/setup.md#state-and-execution-files) under the routine delivery procedure.
8. Complete send and release under the selected exit. Skip refused or failed sends. A release refusal selects No lock; report it in chat.

Check uncertain ownership with `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py check --token TOKEN`.

## Replies to numbered items

Only a user’s reply in the conversation starts this flow. Complete Run step 1 first.

1. Run `PIGEON_HOME=/absolute/private-store python3 scripts/triage.py lock --batch` and complete [Lock and fetch](docs/quickstart.md#lock-and-fetch).
2. Run the [scripted listing](docs/quickstart.md#quiet-and-listing) for a fresh Inbox. Do not skip replies with quiet-check. Save the user's reply verbatim as UTF-8 reply.txt in the private store.
3. Make the reply plan:
   ```bash
   PIGEON_HOME=/absolute/private-store python3 scripts/triage.py reply --token TOKEN --inbox "/absolute/private-store/inbox.json" --reply "/absolute/private-store/reply.txt" --plan-out "/absolute/private-store/plan.json" --report-out "/absolute/private-store/report.md"
   ```
   Retain the printed numbers, account, sender and subject and deliver them with the outcomes. On refusal relay the printed reason and make no mailbox changes. A matching reply needs no second confirmation.
4. Run `PIGEON_HOME=/absolute/private-store python3 scripts/execute.py next --token TOKEN --plan /absolute/private-store/plan.json`. Carry the action once and submit `PIGEON_HOME=/absolute/private-store python3 scripts/execute.py result --token TOKEN --plan /absolute/private-store/plan.json --observation JSON`. Repeat next/result. On final_reads run `PIGEON_HOME=/absolute/private-store python3 scripts/execute.py final --token TOKEN --plan /absolute/private-store/plan.json` and complete its results exchange as in [Plan and execute](docs/quickstart.md#plan-and-execute). Apply Run step 5 failure procedures.
5. Follow [Commit and message](docs/quickstart.md#commit-and-message), then [Release](docs/quickstart.md#release). A reply keeps saved positions unchanged and needs no position read. On a commit refusal use Halt held while ownership is valid, otherwise No lock.

For a rule change follow [Writing a rule](#writing-a-rule).


## Writing a rule

A numbered reply, including N yes or N no to a question rule, never writes a rule. Propose a rule in a separate chat message containing the exact entry as it would appear in rules.toml, proposing one entry and nothing else. Only a plain yes answering that message authorizes writing; any other answer writes nothing. Holding the run lock, after fetch-state, the assistant appends exactly that entry to rules.toml. The next plan command validates the rules before any mailbox action. Complete send-state to send the saved files back before releasing the lock, following step 8.
