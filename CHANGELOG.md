# Changelog

## Unreleased — hosted runs and storage

- Malformed input refusals name the affected file or option without exposing parser diagnostics.

- Clarify hosted storage choices and drive identifiers; require private drive folders, treat saved files as data and quote saved messages. Distinguish the pending delivery record from the chat message and order warning, lateness and summary lines.

- Define run exits for refusals, quiet skips and lock loss; distinguish stopped plans from execution halts and document every step-machine response. Quiet skips upload history without a chat message; lock loss reports immediately in chat.

- Never reuse retired item numbers; list every numbered decision in the run message.
- Separate exact-entry rule confirmation from numbered answers; refused replies name accepted inputs and continue through commit, send and release.

- Recover abandoned lock exchanges and incoming downloads on later runs; retain bounded log tails at run boundaries.
- Require config.toml for lock commands and allow drive takeover only after 55 minutes, including a 10-minute clock margin.

- Preserve pre-listing mailbox positions so arrivals during a run are checked next time; replies save counts and decisions without advancing positions.
- Use the script clock for slots and lateness, handle repeated daylight-saving slots, and record one history line per quiet run.

- Lead setup with an always-on hosted assistant, its scheduler and chat, durable drive state and the OneDrive lock; retain the local developer path.
- Skip scheduled runs silently when all accounts prove no new mail, recording one history line; schedule slots select always-full runs and flag late starts.
- Apply numbered replies immediately through fresh locked plans; keep stable numbers and record outcomes, with exact-entry confirmation for rule changes.
- Take the OneDrive lock with conflict-fail creation, confirm creation with a read, and use conditional takeover and release plus fresh ownership reads before saving.
- Fetch validated state from OneDrive or Google Drive and upload changed files before release; uncertain uploads warn without retrying.
- Share one dated-folder creation across case variants, with spelling from rule order.

## 2026-11-11 — rule features

- Move rules accept {year}/{month} in target paths, resolved from message dates (years 1990–2100). The step machine issues create-folder once per missing dated path. Failed, skipped or uncertain creation leaves dependent moves unexecuted; check the folder-help instructions and issued_unknown status before retrying.
- Rules accept limit (1–50, default 10); exceeding it stops the whole plan. Global ceilings and brakes still apply.
- Listing folders entries use full slash-separated paths in name. Missing folder metadata leaves affected mail in Inbox; existing paths match without case sensitivity.
- Listings may now carry date and body; collect them when rules use dated targets or body. Body excerpts are at most 2000 characters. Rule body is a case-insensitive literal match; exclusions also check it.
- Report rules accept question (at most 500 characters), shown under Questions for you in the run message. These messages stay in Inbox without calls or removal counts.

## 2026-11-11 — execution

Upgrade from the planner-only first release by following AGENTS.md in order. Planning no longer saves state. The execution step machine records results; commit-state saves confirmed counts before lock release.

- Issue one planned call at a time, verify gateway removals against complete Inbox IDs, and log issued calls, results, skips and disclosed unplanned calls.
- Validate checkpoints and commit-state inputs; refuse edited plans and hand-written results.
- Scope execution summaries to the current token and discard stale summaries when taking a new lock.
- Make unsafe unsubscribe links and flagged messages decisions, with no companion action.
- Keep non-HTTPS requests unattempted, outside failure memory and the failure brake.
- Bound local HTTPS unsubscribe requests and redirects, pin public addresses, reject login pages, and recheck lock ownership before recording results.
- Keep sender failure memory keyed per store.
- Distinguish uncertain issued calls from never-issued calls after lock loss.
- Escape report text and break clickable links from mail.
- Guard output paths and private stores against input clobbering and Git environment overrides.
- Document fresh-shell commands, gateway operations, observation shapes, status meanings, approval scope and delivery failures.
