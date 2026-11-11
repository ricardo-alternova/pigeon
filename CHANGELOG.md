# Changelog

## Unreleased — rule features

- Move rules accept {year}/{month} in target paths, resolved from message dates (years 1990–2100). The step machine issues create-folder once per missing dated path. Failed, skipped or uncertain creation leaves dependent moves unexecuted; check the new folder-help instructions and issued_unknown status before retrying.
- Rules accept limit (1–50, default 10); exceeding it stops the whole plan. Global ceilings and brakes still apply.
- Listing folders entries use full slash-separated paths in name. Missing folder metadata leaves affected mail in Inbox; existing paths match without case sensitivity.
- Listings may now carry date and body; collect them when rules use dated targets or body. Body excerpts are at most 2000 characters. Rule body is a case-insensitive literal match; exclusions also check it.
- Report rules accept question (at most 500 characters), shown under Questions for you in the run message. These messages stay in Inbox without calls or removal counts.

## Unreleased — M0 execution

Upgrade from the planner-only release of 2026-11-10 by following AGENTS.md in order. Planning no longer saves state. The execution step machine records results; commit-state saves confirmed counts before lock release.

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
