# M02-T002 Phase 3C2 Implementation Report

## Status

COMPLETE. The first independent review's three Important findings and the second
re-review's one new Important finding are fixed. The final independent read-only
re-review approved Spec at C0/I0/M1 and Quality at C0/I0/M1.
Phase 3C2 exposes explicit Run recovery after one coordinated startup scan,
validates the exact registered execution capability before ownership or external
work, resumes only exact safe checkpoints, and atomically routes every unsafe or
unknown boundary to durable reconciliation. Provider authoritative-status and
same-operation-id resend adapters remain Phase 3D; Workflow recovery remains
Phase 4.

The original implementation was based on
`d8b4f99f06c2881a62a8f1f5db3765a16800fd8d`; this correction is based on the
reviewed Phase 3C2 commit `d5d14ab455982248c589aac2843d3e2b4511f164`.
The second correction is based on the first fix commit
`6876851b8e08a7ceef4d5b6f368eb8762335804b`.

## Post-review corrections

- Recovery-start now carries the exact checkpoint read by the engine as a
  read-only `RunProgressBatch` precondition. Memory and SQLite validate that
  checkpoint in the same lock/transaction as the Run/Session CAS, before any
  recovery-start target can apply. A two-backend barrier race proves a
  concurrent checkpoint rewrite rejects the whole start with zero provider
  calls and no `run.recovery.started` event.
- A lease-losing recovery coordinator now follows both durable Run state and the
  current unreleased Run lease. Missing/released/expired ownership is confirmed
  twice before returning stable retryable `recovery required`; an active newer
  generation remains followable. Owner cancellation, failed terminal commit,
  expiry, takeover/release, and follower SDK close all settle without duplicate
  external work or disturbing the owner. Memory, SQLite, and lazy SQLite have a
  strict detached `get_run_lease` parity test.
- A completed no-Tool Model operation at the checkpoint's current
  READY_FOR_MODEL turn is never resendable. Exact operation outcome, assistant
  checkpoint message/output/usage, and event-tail evidence route the terminal
  commit gap to bounded
  `model_call_completed_terminalization_unknown` reconciliation with the exact
  operation id; any mismatch fails closed to generic reconciliation. Memory and
  SQLite crash/close/reopen tests prove the provider remains at one call.
- READY_FOR_TOOL now requires exactly one COMPLETED current-turn Model operation,
  no future operation, and a complete ordered Model-turn chain. Every completed
  outcome must have the exact shape and one Tool call; the current outcome's
  text and call id/name/arguments must exactly reconstruct the final assistant
  message, accumulated Model usage must equal the checkpoint usage, and the
  started/completed event counts, completion payload, and interrupted tail must
  agree. Any FAILED, missing, duplicate, or mismatched relation enters one
  bounded generic reconciliation before Tool or provider work.

## Delivered behavior

### Public recovery and startup coordination

- `sdk.recovery` provides `scan()`, `recover_run(run_id)`, and
  `pending_requests(run_id)`.
- Construction in a running loop immediately schedules and lifecycle-tracks one
  scanner task. Synchronous construction defers that same scan until the first
  recovery operation. Recovery operations share and settle the startup task;
  only an explicit scan after startup has settled runs a later idempotent pass.
- Lazy SQLite construction remains nonblocking. Failed lazy open is reduced to a
  constant scan error, the failed open task is released during close, and
  repeated cancellation of a close waiter leaves the shared close coordinator
  able to finish.
- Startup scanning and recovery have no provider, Tool, MCP, permission,
  Workflow execution, or application callback dependency. A Workflow method
  trap test proves neither startup scan nor explicit Run recovery enters
  Workflow production.

### Capability and durable-evidence admission

- Recovery strictly loads the authoritative Run, owning Session, descriptor,
  checkpoint, all Run external operations, pending reconciliation requests, and
  captured Run events before automatic execution.
- Current compatibility and a valid hashed `ExecutionDescriptor` are mandatory.
  The exact registered Agent revision/content, model and params, complete ordered
  Tool capability descriptors (including schema, version, source, effects, and
  timeout), model-visible Tool schemas, initial messages protected by the
  descriptor hash, and effective Policy descriptor must match.
- Missing or mismatched capability returns the constant non-retryable
  `recovery capabilities unavailable` error with no lease, mutation, provider,
  or Tool call. Exact later registration can recover the unchanged Run.
- Memory, SQLite, and lazy SQLite implement the read-only, sorted, detached
  `list_external_operations(run_id)` query. It includes terminal operations and
  validates canonical record JSON, wrapper columns/keys, uniqueness, Run and
  Session ownership, and Session existence. No schema change was made.
- A current CREATED Run starts through the normal live entry only when version,
  sole matching `run.created` event, absent checkpoint, absent operation, absent
  pending request, and Session ownership prove it is pristine. Extra, missing,
  or changed events and prior checkpoint/operation evidence fail closed to one
  durable reconciliation request.

### Ownership, deduplication, and durable following

- A per-SDK lock and identity-checked `run_id -> Task[RunResult]` registry make
  twenty local callers share the exact coordinator task. Identity-safe callbacks
  remove success, failure, and cancelled coordinators; tests require the tasks to
  become garbage-collectable.
- RunEngine creates the fresh coordinator identity and owns the execution lease.
  A cross-SDK loser follows yielded durable Run and current-lease state and never
  invokes a duplicate provider or Tool. Terminal states return normally;
  reconciliation-owned, ownerless, released, or expired nonterminal states
  return recovery-required instead of spinning, while an active takeover remains
  followable.
- Cross-SDK owner/follower cancellation and SDK close are isolated: cancelling
  or closing the follower neither cancels nor duplicates the owner operation.
- COMPLETED/FAILED Runs return detached durable handles without capability,
  lease, or external work. WAITING_RECONCILIATION returns detached state after
  strict canonical pending-request validation.

### Safe checkpoint resume

- `RunEngine.resume` is a distinct recovery entry. It rejects non-INTERRUPTED,
  foreign, missing, stale, in-flight, waiting, or malformed checkpoints and
  unresolved operations before external work. READY_FOR_TOOL content validation
  also runs inside the engine before lease acquisition.
- Under a fresh lease, the first exact transaction writes the adjacent
  `run.recovery.started` event and INTERRUPTED-to-RUNNING snapshot while leaving
  checkpoint contents unchanged. The checkpoint is also an exact same-transaction
  precondition, preventing a stale engine read from racing a concurrent rewrite.
- READY_FOR_MODEL restores the checkpoint's exact detached messages, accumulated
  output, cumulative usage, ordered Tool results, and turn. It creates the next
  model operation only after the recovery-start transaction.
- READY_FOR_TOOL reconstructs exactly one final assistant Tool call, verifies the
  currently registered full Tool capability, and reuses the normal permission,
  Tool-start, Tool-outcome, checkpoint, heartbeat, cancellation, and fencing
  path. The assistant call is not appended twice. Permission ask/deny is durable
  and observable; denial invokes no handler and produces the normal denied Tool
  result before the next model turn.
- SQLite close/reopen proves exact READY_FOR_MODEL recovery. Same-SDK and
  cross-SDK tests prove one provider/Tool side effect for pristine,
  READY_FOR_MODEL, and READY_FOR_TOOL recovery.

### Conservative reconciliation

- Legacy, non-pristine CREATED, missing checkpoint, model/tool in-flight,
  permission-wait-lost, and malformed checkpoint-operation relationships never
  retry an external call. They use bounded reasons and metadata only.
- A completed no-Tool Model operation paired with the exact READY_FOR_MODEL
  terminalization-gap evidence is reconciled with its operation id; incomplete
  or mismatched relationships also reconcile and can never repeat the provider.
- Admission acquires a fresh lease and submits one stable
  `RunProgressBatch` containing the exact Run/Session preconditions, one pending
  request, one `reconciliation.requested` event, and the
  WAITING_RECONCILIATION snapshot. Exact ambiguous replay is idempotent.
- Precommit failure is all-or-none; ambiguous commit reuses the same batch;
  cross-SDK admission creates one request/event; generation takeover and Session
  deletion reject every partial target; commit cancellation and late failing
  release are settled through repeated cancellation with no background task.
- Recovery-start has matching precommit, ambiguous replay, and cancellation
  evidence. A recovery-start committed immediately before owner cancellation is
  discoverable by the scanner, becomes INTERRUPTED, and safely resumes without a
  duplicate external call.
- Pending request validation rejects multiple, foreign, resolved-only, missing,
  and request/status-disagreement states with a constant cause/context-free
  error. Secret-bearing descriptors and request evidence are absent from every
  SDK traceback local asserted by the tests. Reconciliation-owned Runs remain
  Session-owned, closing stays busy, and normal deletion is rejected.

## Strict TDD evidence

The work used deterministic events, fake timestamps, and injected Store
boundaries; no wall-clock sleeps were introduced in the new recovery tests.

- Baseline before changes: `1180 passed in 39.25s`.
- Startup scheduling first timed out because no scan was created in a running
  loop; synchronous construction then failed because `sdk.recovery` was absent.
  The final startup/open/close/cancel group is included in the 56-test focused
  result.
- Same-SDK dedup was deliberately removed after the first implementation and
  produced twenty distinct task identities; restoring the identity-safe registry
  produced one coordinator/provider call and collectable success/failure/cancel
  tasks.
- The all-operation query initially failed with `AttributeError` on Memory,
  SQLite, and lazy SQLite. The final storage/recovery-record gate is included in
  the 136-test Phase 2 result.
- Non-pristine CREATED initially attempted normal execution. The conservative
  admission path then produced one request/event/snapshot transaction.
- READY_FOR_MODEL initially had no resume entry. The first implementation exposed
  a frozen `mappingproxy` deepcopy failure after recovery-start; detached JSON
  reconstruction fixed it. READY_FOR_TOOL initially called the provider before
  the pending Tool; extracting and reusing the live Tool path fixed ordering and
  duplicate-message behavior.
- Cross-SDK READY recovery initially surfaced a generic execution failure. A
  diagnostic run identified that RunEngine's sanitizer had erased the private
  `LeaseHeldError` control signal; preserving only that constant subclass made
  the loser follow durable state. A later lease-takeover fault test exposed an
  infinite CREATED follower; the follower now waits through CREATED/INTERRUPTED
  only for a real lease-held race.
- Review I1 RED: both Memory and SQLite allowed a concurrent checkpoint rewrite
  after the engine read and still moved the Run to RUNNING. GREEN: exact
  checkpoint CAS rejects both races, `2 passed in 3.38s`.
- Review I2 RED: owner cancel and failed terminal commit, lease expiry and
  takeover/release all exhausted deterministic bounded yields into INTERNAL;
  follower close remained pending. GREEN: the five-case matrix is `5 passed`.
- Review I3 RED: Memory and SQLite reopen both resumed a completed no-Tool Model
  operation and called the provider twice (`2 failed`, `DID NOT RAISE`). GREEN:
  both produce one reconciliation request with the original operation id and
  keep provider calls at one, `2 passed`.
- Second-review I1 RED: all 24 Memory/SQLite reopen cases for FAILED, missing,
  duplicate, outcome text/usage/call identity, checkpoint assistant/usage, and
  completion event/tail mismatches executed Tool/provider work and completed
  instead of raising. GREEN: all `24 passed in 4.81s`, with zero Tool/provider
  calls and exactly one bounded reconciliation request/event/status per case.
  Existing real allow, permission-deny, and cross-SDK READY_FOR_TOOL paths remain
  green (`3 passed in 3.33s`).
- The final Phase 3C2 focused result is `89 passed in 67.02s`; the current-lease
  Memory/SQLite/lazy parity addition remains `3 passed`.

## Fresh final-code gates

All commands used
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` with
Python 3.13.

- Phase 3C2 focused (`test_recovery_api.py`):
  `89 passed in 67.02s`.
- Phase 3C1 focused (`test_recovery_scanner.py`, `test_abandoned_runs.py`,
  `test_run_progress_reconciliation.py`):
  `115 passed in 6.69s`.
- Phase 3B live progress:
  `38 passed in 3.40s`.
- Phase 3A Run-progress transaction:
  `117 passed in 6.64s`.
- Phase 2 recovery models/records/SQLite validation:
  `139 passed in 7.46s`.
- Phase 1 + M02-T001 regressions:
  `188 passed in 14.71s`.
- Session/Run/Tool/MCP/Workflow recovery/child compatibility regressions:
  `237 passed in 10.36s`.
- Full Python 3.13 pytest after the last test change:
  `1272 passed in 103.48s`.
- Public package import check: `RecoveryAPI` imports from `agent_sdk` and appears
  in `agent_sdk.__all__`.
- Ruff: `All checks passed!`.
- Mypy: `Success: no issues found in 73 source files`.
- `git diff --check`: exit 0; only Windows LF-to-CRLF informational warnings.
- Explicit forbidden-scope and schema audit: exit 0; no diff under migrations,
  provider gateway, Workflow production, roadmap, milestones, or task index;
  SQLite `_SCHEMA_VERSION` remains exactly 3.

## Scope and handoff concerns

Production changes are limited to the brief's permitted files:

- `src/agent_sdk/__init__.py` (public `RecoveryAPI` export only)
- `src/agent_sdk/api.py`
- `src/agent_sdk/runtime/engine.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/storage/base.py`
- `src/agent_sdk/storage/memory.py`
- `src/agent_sdk/storage/sqlite.py`

Focused tests are in `tests/integration/runtime/test_recovery_api.py`, with the
all-operation and current-lease parity assertions in
`tests/integration/storage/test_recovery_records.py` and one startup-scan
compatibility adjustment in `tests/integration/runtime/test_text_agent_loop.py`.

The durable follower intentionally polls strict Run and current-lease queries
with an injected cooperative yield rather than adding a new notification surface
in this phase. Provider status queries/resend certification, reconciliation
resolution actions, Tool retry metadata, Workflow recovery, and any
schema/migration change remain explicitly out of scope for Phase 3C2.

The first independent review returned C0/I3/M0; the second re-review closed those
findings and returned C0/I1/M0 for the separate READY_FOR_TOOL relation gap. The
two fixes address all four findings with the RED/GREEN evidence and final gates
above. The final independent re-review approved Spec at C0/I0/M1 and Quality at
C0/I0/M1. Its Minor notes that multi-turn READY_FOR_TOOL currently validates
aggregate Model started/completed counts and the final completed payload, not
every historical turn's event ordering and completed payload; that hardening is
recorded for whole-branch triage.
