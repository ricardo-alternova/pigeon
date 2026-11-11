# Changelog

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
