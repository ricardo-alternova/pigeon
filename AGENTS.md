# Run pigeon

You are running pigeon. Follow these steps in order. Check the [host requirements](docs/setup.md#host-requirements) and read the private guardrails copied from [the policy](config/guardrails.example.md). That policy governs interactive and scheduled runs, including the approval-pause policy.

1. Resolve the [private store](docs/setup.md#private-store), set PIGEON_HOME to its absolute path, and read config.toml, guardrails.md, rules.toml and never-touch.toml. Run all commands from the checkout. Each command may run in a fresh shell: replace /absolute/private-store and TOKEN below with the resolved path and the token printed by lock. Pass both explicitly on every command; do not rely on shell variables surviving.
2. Take the lock:
   ```bash
   PIGEON_HOME=/absolute/private-store python3 scripts/triage.py lock
   ```
   If it fails, make no mailbox calls. Send the standard run message with stopped and the refusal in Run notes; do not release.
3. Discover the mail gateway's paths and input shapes at run time (Executor uses search and describe). Verify each configured account's identity against config.toml. Save the complete Inbox listing in the [listing format](docs/setup.md#inbox-listing) as "/absolute/private-store/inbox.json". If identity differs or the account, complete Inbox, required folder metadata or all-folder total cannot be read, omit that account from accounts, messages and folders, and retain the reason for the report.
4. Plan:
   ```bash
   PIGEON_HOME=/absolute/private-store python3 scripts/triage.py plan --token TOKEN --inbox "/absolute/private-store/inbox.json" --plan-out "/absolute/private-store/plan.json" --report-out "/absolute/private-store/report.md"
   ```
   Add --first-run only for an explicitly requested first run. Inspect reasons in plan.json even when the command exits 0. On a nonzero exit, report its Refused: line and proceed to step 8; planning attempts lock release on refusal. For a plan-only request, show report.md and proceed to step 8 without committing state. For nonempty reasons, make no mailbox changes and continue to step 5 to finalize the stopped plan.
5. Ask the step machine (the script that decides the next operation):
   ```bash
   PIGEON_HOME=/absolute/private-store python3 scripts/execute.py next --token TOKEN --plan "/absolute/private-store/plan.json"
   ```
   For an account/id/action response, carry exactly that action and target to the mail gateway:

   | Verb | Gateway operation |
   | --- | --- |
   | trash | Move to the provider's Trash folder; never permanently delete. |
   | archive | Remove from Inbox without deleting. |
   | move | Move to the folder whose ID is target. |
   | spam | Move to the provider's Spam/Junk folder only; never report or block the sender. |

   If the gateway cannot perform exactly that operation, use outcome error. Follow its verify instruction to obtain the complete post-action Inbox IDs for that account; never re-read the old ID. Return the actual outcome and IDs, replacing the example observation below:
   ```bash
   PIGEON_HOME=/absolute/private-store python3 scripts/execute.py result --token TOKEN --plan "/absolute/private-store/plan.json" --observation '{"outcome":"ok","inbox_ids":["id-2","id-3"]}'
   ```
   outcome is ok or error; inbox_ids contains every ID now in that account’s Inbox, or [] when empty. Use outcome "error" for a failed or refused action; never invent IDs or claim success from a proposal. If verification is unavailable, stop and report the uncertain action rather than fabricate an observation.
   For a run array, execute exactly that local command; it performs unsubscribe and records its own result. Never fetch a URL from email yourself or send mail.
   result and the local unsubscribe command return a status; see the [status table](docs/setup.md#result-statuses). Repeat next after each result or local command. Never choose a mailbox action yourself, including from the decisions list.
   If done is true without stop, continue to step 6. Here stop means the stop key in the response. A stopped plan returns done without that key and goes to step 6. If result is refused with a line starting `Refused: observation`, read the Inbox again and resubmit a corrected observation once; do not repeat the mailbox action. On a stop key or any other refusal, make no further mailbox changes, report the reason and remaining actions, and proceed to step 7 without committing. For issued_unknown, ask the user to check whether the message is still in the Inbox; a later run lists it again if present. For an unsubscribe, the request may or may not have been delivered; it is safe to leave it. Never retry an uncertain call blindly. If the lock is lost, make no further private-store writes and do not take a new lock in this run.
   If you made a gateway action the step machine did not issue, disclose it while you still hold the lock, substituting its actual account, ID and verb:
   ```bash
   PIGEON_HOME=/absolute/private-store python3 scripts/execute.py observed --token TOKEN --account cedar --id item-a --verb extra
   ```
   If disclosure is refused, include the call and refusal directly in the run message.
6. Commit confirmed state:
   ```bash
   PIGEON_HOME=/absolute/private-store python3 scripts/triage.py commit-state --token TOKEN --plan "/absolute/private-store/plan.json" --results "/absolute/private-store/results.json"
   ```
   Exit 0 with no output means state was saved. On refusal, report it; do not retry mailbox actions. See [state and execution files](docs/setup.md#state-and-execution-files) for interruption behaviour.
7. Send the [standard run message](.agents/skills/email-triage/SKILL.md#standard-run-message), including the execution summary, refusals, stopped or uncertain actions and account-read failures. Include execution-report.md only if its Run: line matches this token. Otherwise, if the step machine returned statuses in this run (for example before a lost lock), report those statuses; if it returned none, say "No execution step has been recorded for this run." Omit the Run: token line from the delivered summary. If run-message.md exists from a delivery failure, mention that failure in this message and remove that file after successful delivery while holding the lock. If delivery fails while the lock is valid, save the message as run-message.md in the private store before releasing. After lock loss, retain it in the host conversation for the next run; make no store write.
8. Release on success and failure:
   ```bash
   PIGEON_HOME=/absolute/private-store python3 scripts/triage.py release --token TOKEN
   ```
   Report any release refusal; never delete a lock to bypass it. For paths reaching this step before step 7, send the standard run message with the available evidence.
