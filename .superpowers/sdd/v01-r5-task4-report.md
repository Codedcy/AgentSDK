# Agent SDK v0.1 R5 Task 4 Report

## Status

Implemented from baseline `eece098` without changing `pyproject.toml` version,
tagging, building release artifacts, or publishing. R5 Task 5 remains pending.

## RED / GREEN evidence

- RED: `tests/e2e/test_v01_reference_example.py` failed because
  `examples/v01_reference.py` did not exist (`1 failed in 0.10s`).
- Intermediate RED: the first real smoke returned Context
  `L0,L1,L2,L2,L2`; the public Context API correctly fell back because one Run
  did not leave older unprotected evidence for L3 summarization. A second real
  Run supplied durable history; L3 created a capsule and L4 rebased it.
- GREEN: subprocess smoke passed (`1 passed in 4.50s`).
- RED: the documentation contract failed on the missing `CHANGELOG.md` and
  guides (`1 failed in 0.09s`). GREEN: `tests/docs` passed (`4 passed`).
- RED: the crash fixture initially emitted no seed; after adding a real public
  Run/Tool process, reopen correctly stayed `running` until its 30-second lease
  expired. The next RED proved `ReconciliationAction.TERMINATE` is explicitly
  unsupported in v0.1. GREEN uses the supported explicit `RETRY` action with
  duplicate-side-effect risk acknowledgement (`1 passed in 37.85s`).
- GREEN: the ordered single public acceptance passed after public-only SQLite,
  query, Context, Skill, Prompt Manifest, Trace, evaluation, analytics,
  attribution, recovery, and deletion evidence replaced direct Store reads
  (`1 passed in 41.30s`).
- GREEN final checkpoint: release + subprocess + docs produced
  `6 passed in 45.23s`; Ruff passed; strict mypy passed for the reference example
  with `MYPYPATH=src`; `git diff --check` was clean.

## Review-rejection fixes

The follow-up review rejected three acceptance-test gaps. The repair changes only
tests, their fixture, and this report; production code remains unchanged.

- RED: requiring the real demo Skill and inspecting the real provider request
  exposed both the missing baseline Skill root and the missing failure cleanup.
  The old baseline did not exit before the 24-second outer timeout after failure
  because its live monitor, MCP process, and SDK were not protected by cleanup.
- C0: the Context acceptance no longer imports `ContextPlanner`, accepts a
  `monkeypatch` fixture, or replaces `_estimate_messages`. Six real differently
  sized inputs share a public `ContextRuntimeConfig(model_window=1000, ...)` and
  exercise the normal local LiteLLM token counter to recommend and apply L0-L4.
- I2: every actual baseline model request is inspected for the packaged general
  profile, application prompt, and activated demo Skill instructions. Public
  `query_events` projects each actual `prompt.manifest.created` payload into the
  public `PromptManifest`; its ID and Context View are correlated with the real
  `model.call.started` event, and layer/whole hashes are checked against the
  actual provider request. The standalone `PromptComposer.compose` false positive
  was removed.
- M1: the baseline now uses nested `try/finally` cleanup to cancel and await its
  live monitor, then close the MCP manager and SDK even after assertion failure.
  The reopened SDK is independently protected by `try/finally`.
- GREEN: the focused thirteen-step acceptance passed (`1 passed in 41.55s`). The
  final Task4 aggregate passed (`6 passed in 44.36s`); Ruff passed; strict mypy
  passed for the reference example and changed fixture (`2 source files`); and
  `git diff --check c0fc2db` was clean.

## Actual smoke JSON

```json
{"attribution_method":"deterministic_event_evidence_v1","child_status":"completed","context_levels":["L0","L1","L2","L3","L4"],"evaluation_verdict":"pass","run_status":"completed","trace_stage_count":1,"workflow_status":"completed"}
```

Every value is derived from a real public result. `trace_stage_count` counts the
Run-kind stage in the normalized timeline; it is not a constant. `--smoke` uses
`AgentSDK.for_test` with a deterministic no-network provider. Normal mode creates
`AgentSDK(AgentSDKConfig(database_path=...))` and uses application-environment
LiteLLM credentials.

## Thirteen-step acceptance evidence

`test_v01_release_public_acceptance_thirteen_steps` is the sole pytest test in
`tests/e2e/test_v01_release.py`. Its ordered helpers prove:

1. SQLite Session and retained workspace;
2. custom application system prompt;
3. automatic per-model-call Context;
4. applied L0-L4 with original source evidence retained;
5. application, built-in, and real stdio MCP Tools through authorization;
6. activated Skill plus public Prompt Manifest;
7. generated condition and bounded-loop Workflow candidate;
8. validation without execution followed by explicit start;
9. Agent-driven spawn/message/list/wait and consumed Child result;
10. live subscription and normalized historical Run/Child Trace;
11. evaluation, success rate, Tool failure rate, and per-Run attribution;
12. completed safe-boundary reopen without replay plus a real crashed unknown
    in-flight Tool that becomes interrupted and requires explicit resolution;
13. Session history deletion while workspace files remain.

## Files

- Added `examples/v01_reference.py`, `tests/e2e/test_v01_reference_example.py`,
  `CHANGELOG.md`, and three `docs/guides/v01-*.md` guides.
- Extended `tests/e2e/test_v01_release.py` and the focused public crash/provider
  fixture in `tests/fixtures/v01_runtime.py`.
- Extended the docs contract and updated README, release ledger, and SDD progress.

## Retained concerns and boundaries

- No exactly-once guarantee exists for external effects. v0.1 recovery is one SDK
  instance in one process; multi-process/distributed recovery is deferred.
- `ReconciliationAction.TERMINATE` remains unsupported. Safe abort in v0.1 means
  leaving the request pending/interrupted and closing without replay. This is
  documented rather than hidden or expanded into a Task4 runtime change.
- The known pre-R4 authoritative Context recovery debt remains unchanged.
- The pre-existing interrupted mid-loop Workflow recovery path produced
  `related terminal run recovery evidence changed after certification`; Task4
  proves the required completed safe boundary and does not repair that debt.
- During focused debugging, normalized Trace for a completed Workflow root after
  reopen did not return; Run and Child normalized Trace paths are acceptance-green.
  This production investigation is outside Task4 and is recorded for hardening.
- Existing prompt/timeout debts were not broadened or repaired. Normal provider
  mode was not invoked because Task4 has no authority or credentials for network
  calls; the no-network smoke and constructor contract are verified.
- Deterministic attribution is correlation, not causality. Aggregate Tool
  usefulness and multidimensional failure analysis remain deferred.
