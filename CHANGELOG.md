# Changelog

## 0.1.0 - 2026-07-21

- Added SQLite Sessions, recoverable Runs, configurable/default prompts, automatic
  Context L0-L4, Skills, and durable Prompt Manifests.
- Added application, built-in workspace/command, MCP, and Child-control Tools with
  allow/ask/deny authorization.
- Added validated, explicitly started Workflows with conditions and bounded loops.
- Added live subscriptions, normalized historical Trace, deterministic per-Run
  attribution, evaluation, success rate, and Tool failure metrics.
- Added explicit interrupted-work reconciliation with application-selected
  retry or abort; abort terminally fails the Run without replay. Session deletion keeps
  application workspace files.
- Expanded the installed reference so deterministic `--smoke` proves automatic
  L0-L4, condition and two-iteration loop control, two-way Child communication
  with result consumption, live/historical Trace, evaluation/attribution, safe
  reopen without replay, and history deletion without deleting workspace data.

This entry describes the v0.1 contract; packaging/version finalization is handled
by the release-candidate task.
