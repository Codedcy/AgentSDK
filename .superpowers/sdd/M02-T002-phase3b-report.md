# M02-T002 Phase 3B Implementation Report

## Status

DONE. Live `RunEngine` execution now owns a fresh generation-fenced lease,
heartbeats that exact lease, and persists every live Run event/snapshot mutation
through the Phase 3A `StateStore.commit_run_progress` transaction. Exact durable
checkpoints and conservative model/Tool external-operation boundaries surround
all external I/O.

Phase 3B does not scan stale Runs, extend `RunStatus`, expose recovery admission,
resume checkpoints, register provider recovery adapters, or change Workflow
recovery/schema/public behavior.

Base commit: `ade5f55d832f9fb76907cfcafc29adeb77178600`.

## Delivered behavior

### Lease ownership and cancellation-safe progress

- Each `execute` loads and validates a CREATED Run, acquires a finite fresh
  `coord_*` lease, and only then persists `run.started` or invokes external I/O.
- A heartbeat renews only the owned lease. Renewal loss cancels the owner and is
  also checked at every emitter buffer/flush/commit boundary, so a provider that
  suppresses cancellation cannot publish a late result.
- Heartbeat and delta tasks are settled on all exits. Exact lease release is
  cancellation-safe best effort and cannot overwrite an already durable result.
- Each progress event, snapshot, operation, checkpoint, and batch is constructed
  once. Store ambiguity may replay the same batch object; caller cancellation
  shields and settles the in-progress commit before returning.
- Private progress conflict/storage errors distinguish fencing failures from
  external provider/Tool failures. The public `execute` boundary reconstructs a
  constant context-free SDK error after discarding request-bearing locals.
- Session deletion retains the existing stable `NOT_FOUND` behavior; generation
  takeover and other live fencing conflicts remain stable `CONFLICT` results.

### Checkpoints and external-operation boundaries

- The first fenced transaction atomically writes sequence-2 `run.started`, the
  RUNNING Run snapshot, and checkpoint v1/turn 0/`ready_for_model` with exact
  detached initial messages.
- Current execution descriptors are authoritative before lease acquisition. The
  model, params, messages, complete Tool capability descriptors/request schemas,
  and effective policy must match. Strict model validation protects the durable
  agent/capability/policy/descriptor hashes. Mismatch invokes the provider zero
  times and writes no checkpoint.
- Legacy CREATED internal/Workflow compatibility uses the exact detached
  `ModelRequest` messages and remains `legacy_unknown`.
- Every model turn first atomically records `step.started`,
  `model.call.started`, a started `ModelCallOperation`, and a
  `model_in_flight` checkpoint. Provider I/O starts only afterward.
- A model outcome atomically transitions that exact operation, records normalized
  finish/text/ordered Tool calls/per-turn usage, writes deterministic usage and
  completion events, and advances to a safe checkpoint. Checkpoint/Run usage is
  accumulated across turns.
- Provider/stream failure atomically records a bounded sanitized failed operation,
  failed Run, terminal checkpoint, and Session detach. Provider exception text and
  type are neither persisted nor retained by the public error.
- Invalid/missing/denied Tools perform no handler I/O and create no fake operation.
  Their normalized result advances directly to the next safe checkpoint.
- Authorized Tool execution atomically records `tool.call.started`, a started
  `ToolCallOperation`, and `tool_in_flight` before entering the handler. Success,
  normalized handler failure, and timeout atomically record the exact terminal
  operation, normalized result, messages/results, incremented turn, and
  `ready_for_model` checkpoint.
- Permission request/resolution atomically moves the Run and checkpoint through
  WAITING/`waiting` and RUNNING/`ready_for_tool` before handler entry.

### Terminal ownership

- Successful and provider-failed terminal commits atomically write the terminal
  Run event/snapshot, terminal checkpoint, Session detach event, and exact Session
  snapshot transition.
- Session CAS retry keeps stable Run event/checkpoint targets and rebuilds only
  the Session-specific event/snapshot. It retries only after confirming the lease
  and Run are still exact.
- Precommit failure/cancellation publishes none of the terminal targets.
  Cancellation before an external started boundary prevents the external call;
  cancellation after durable Tool start leaves the exact started operation and
  `tool_in_flight` checkpoint without inventing an outcome.

## Production files

- `src/agent_sdk/runtime/engine.py`
- `src/agent_sdk/tools/executor.py`

`ToolExecutor` gained optional private before-handler and completed-call hooks.
Existing callers without hooks retain the prior event and normalized-result
behavior.

Focused tests are in
`tests/integration/runtime/test_live_run_progress.py`. Existing Store wrappers and
fault-injection doubles in runtime, Tool, Workflow, evaluation, and child-run
regressions were minimally forwarded/retargeted to the new required StateStore
surface; no production behavior outside the two files above changed.

## Strict TDD evidence

All valid test commands used the worktree Python 3.13 virtual environment. The
initial bare `pytest.exe` invocation was blocked by local Application Control and
is not counted as behavior evidence.

1. Initial lease/checkpoint/model-start boundary
   - RED: `1 failed`; the provider observed no checkpoint.
   - GREEN: `1 passed`.
2. Current descriptor authority
   - Model RED: `1 failed` (`DID NOT RAISE`); GREEN: initial `2 passed`.
   - Full fields RED: `5 failed` for messages, params, request Tool schemas,
     capability, and policy; GREEN: `5 passed`.
3. Heartbeat
   - Renewal RED: constructor rejected deterministic clock injection; GREEN:
     `1 passed`.
   - Loss RED: owner remained pending; GREEN: `1 passed`.
   - Provider-suppressed cancellation RED: `1 failed` because the late result was
     accepted; GREEN heartbeat group: `3 passed` with no late delta/outcome/terminal.
4. Model outcome/failure
   - Atomic outcome RED: no operation transition; GREEN: `1 passed`.
   - Provider failure RED: original provider secret remained as exception cause;
     GREEN: `1 passed` with a terminal atomic batch and non-vacuous traceback-local
     sanitization.
   - Per-turn usage RED: second operation stored accumulated `6/3/9` instead of
     its `4/2/6`; GREEN: `2 passed`, while the checkpoint retained accumulated
     `6/3/9`.
5. Terminal ownership
   - Atomic success RED: terminal checkpoint was absent; GREEN: `1 passed`.
   - Session-CAS RED: concurrent Session change returned failed-to-persist; GREEN:
     targeted `1 passed` and one durable `run.completed`.
   - Precommit failure/cancellation: `2 passed`; no terminal target was partially
     published.
6. Tool/permission boundary
   - Authorized Tool hooks RED: no Tool operation batches; GREEN: `1 passed`.
   - Permission RED: WAITING checkpoint remained `ready_for_tool`; GREEN: `1 passed`.
   - Missing/invalid/denied safe path: `3 passed`, zero handler calls and no fake
     operation.
   - Normalized handler failure/timeout: `2 passed`, exact failed operation and
     safe checkpoint; handler task cancellation was settled.
7. Concurrency, replay, and cancellation
   - Ambiguous model-start RED: committed batch raised raw runtime error; GREEN:
     `1 passed`, the same batch object was submitted twice, provider called once,
     and one durable start event exists.
   - Simultaneous engines: `1 passed`; one lease winner/provider call/start event.
   - Commit-pending cancellation: `1 passed`; cancellation waited for the start
     batch, provider calls remained zero, and one exact in-flight operation remained.
   - Handler cancellation: `1 passed`; provider/handler calls were each one,
     Tool start was durable, and no Tool outcome was invented.
   - Generation takeover: model and Tool late outcomes both rejected; `2 passed`.
8. Progress error isolation and cleanup
   - Gap batch RED: `3 failed` for takeover mapped INTERNAL, Tool conflict
     terminalizing the Run, and missing Session-CAS retry.
   - GREEN: targeted `3 passed`; complete gap batch `9 passed`.
   - A Store rejecting ordinary `commit` proved live progress used only the fenced
     API. Success/provider-failure/handler-cancellation/timeout tests require no
     new tasks after exit.
   - Tool outcome storage conflict retained neither secret arguments nor results
     in cause/context/SDK traceback locals and left the exact in-flight boundary.

Fresh final Phase 3B focused result: `34 passed in 3.33s`.

## Final-code gates

- Phase 3B focused:
  `34 passed in 3.33s`.
- Phase 3A `test_run_progress.py`:
  `117 passed in 6.35s`.
- Phase 2 recovery models/records/SQLite validation:
  `136 passed in 7.06s`.
- Phase 1 + M02-T001 leases/migration/idempotency/descriptors/SQLite spine:
  `188 passed in 12.53s`.
- Existing text loop, permissioned Tool, Workflow recovery/session ownership, and
  child-run regressions:
  `137 passed in 6.73s`.
- Full Python 3.13 pytest:
  `1060 passed, 1 skipped in 32.01s`.
  The skip is the pre-existing environment guard in
  `test_prompt_slice.py`: `uv executable is unavailable`; a focused reason check
  returned `53 passed, 1 skipped in 3.59s`.
- `ruff check src tests`:
  `All checks passed!`.
- `mypy src/agent_sdk`:
  `Success: no issues found in 72 source files`.
- `git diff --check`: exit 0; only Windows LF-to-CRLF informational warnings.

The first full run found four stale regression test doubles: one did not delegate
the lease/progress Store surface and three still injected terminal faults through
ordinary `commit`. Their focused rerun was `4 passed`; the fresh full rerun above
is the final result.

## Scope and schema audit

- Production diff is exactly `runtime/engine.py` and `tools/executor.py`.
- Diff is empty for storage, SQLite schema/migrations, API, Run/status models,
  reconciliation records/actions, Workflow production, roadmap, milestone, and
  task-index paths.
- No stale scan, recovery command/admission, checkpoint resume, provider recovery
  adapter, reconciliation decision, or public Workflow behavior was added.
- The schema remains exact v3 and `StateStore.commit_run_progress` semantics are
  unchanged from Phase 3A.

## Self-review

- Every external call has a durable before boundary and only a normalized known
  outcome can close it. Lease loss/storage conflict cannot be mistaken for an
  external failure and cannot publish a guessed terminal state.
- All checkpoint transitions are adjacent full-record CAS updates. Exact batch
  replay cannot duplicate an event id, external call, operation, or checkpoint.
- Current descriptors fail closed; legacy compatibility is deliberately live-only
  and remains non-recoverable for Phase 3C.
- Public error reconstruction has nonempty SDK traceback coverage while retaining
  no model messages/params, provider payloads, Tool arguments/results, or original
  cause/context.
- No known Critical, Important, or in-scope correctness concern remains.

## Review-finding fix (2026-07-15)

The Phase 3B review identified two Important cleanup races and one defensive
model-stream invariant gap. All three were reproduced before production changes:

- Buffered delta/heartbeat-loss RED: `1 failed`; `execute` returned with one live
  `_RunEmitter._flush_after_delay` task. `_RunEmitter.close()` now runs an owned,
  repeated-cancel-safe cleanup that first detaches/cancels the timer and discards
  buffered deltas without committing when the lease is already lost.
- Double-cancel/release RED: `1 failed`; `execute` was already done before the
  blocking release settled, and asyncio reported an unretrieved late release
  exception. Cleanup now creates exactly one release task, waits through repeated
  owner cancellation, consumes its late failure, and preserves the original
  cancellation or lease-loss error.
- Missing-`ModelCompleted` RED: `1 failed`; the terminal batch contained only
  `run.failed` and Session detach while the started model operation remained
  unresolved. The defensive path now uses `emitter.fail_model`, atomically CASing
  that operation to FAILED with `model.call.failed`, `step.failed`, `run.failed`,
  terminal checkpoint, and Session detach.

Targeted GREEN was `3 passed in 3.18s`. A strengthened lease-loss plus blocking
failing release plus second-cancel combination passed independently (`1 passed in
3.15s`) and proves one release invocation, stable public `CONFLICT`, no release
task after return, and consumed late release failure.

Fresh final-code gates after these fixes:

- Phase 3B focused: `38 passed in 4.55s`.
- Phase 3A Run-progress transaction: `117 passed in 6.96s`.
- Phase 2 recovery models/records/SQLite validation: `136 passed in 7.30s`.
- Phase 1 + M02-T001 regressions: `188 passed in 14.35s`.
- Existing runtime/Tool/Workflow/subagent regressions: `137 passed in 6.95s`.
- Full Python 3.13 pytest: `1065 passed in 38.15s`.
- Ruff: `All checks passed!`.
- Mypy: `Success: no issues found in 72 source files`.
- `git diff --check`: exit 0; only Windows LF-to-CRLF informational warnings.
- Forbidden-scope diff (storage, API, Run/status models, reconciliation,
  Workflow, docs): zero lines. Schema remains exact v3.
