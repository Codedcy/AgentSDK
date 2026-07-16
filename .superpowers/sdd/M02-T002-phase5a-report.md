# M02-T002 Phase 5A Implementation Report

## Status

DONE. Phase 5A implements strict reconciliation resolution admission and the
explicit `CONFIRM_NOT_EXECUTED` and `RETRY` transitions. The work remains on
`feature/agent-sdk-implementation`, based on `5d5600b`, for independent review.
No Phase 5B, M02-T003, M02-T004, `CONFIRM_COMPLETED`, `TERMINATE`, or subprocess
release-gate behavior was implemented.

This report is included in the Phase 5A commit; the handoff records the final
commit hash.

## Implemented scope

- Added the exact `RecoveryAPI.resolve(request_id, action, *, actor, evidence)`
  and `ReconciliationService.resolve(...)` contracts. Both return detached
  `ReconciliationRequest` values.
- Exported `ReconciliationAction`, `ReconciliationRequest`,
  `ReconciliationResolution`, and `ReconciliationService` from `agent_sdk`.
- Added strict request/action/actor/evidence, lifecycle, capability, Session
  ownership, Run/checkpoint/operation, full event-envelope, and exact pending
  request admission.
- Added exact same-decision replay, constant changed-decision conflicts, and
  bounded two-SDK convergence/conflict behavior.
- Added one-lease, one-`RunProgressBatch` resolution of the request/event,
  terminalization of the old operation, rewind to a safe checkpoint,
  transition of the Run to `INTERRUPTED`, and retention of the Run by its
  Session.
- Extended Memory and SQLite validation for the exact old-generation
  resolution batch without relaxing ordinary operation, checkpoint, event, or
  reconciliation transitions.
- Extended the closed recovery grammar so resolved attempts are authenticated
  and removed from the effective history before a later explicit recovery
  creates a new operation at the same logical turn.
- Added lifecycle, cancellation, SDK-close, Session close/delete, lease loss or
  expiry, CAS, corruption, capability drift, post-commit ambiguity, Memory,
  SQLite, and two-SDK coverage.
- `CONFIRM_COMPLETED` and `TERMINATE` remain constant `INVALID_STATE` with no
  durable mutation.

## Audit conclusions

### Lifecycle and atomicity

The admitted resolution projection has no impossible intermediate snapshot:

- authoritative Run: `WAITING_RECONCILIATION` to `INTERRUPTED`, version `+1`;
- current checkpoint: matching in-flight phase to `READY_FOR_MODEL` or
  `READY_FOR_TOOL`, checkpoint version `+1`, `operation_id=None`;
- old operation: exact `STARTED` record to `FAILED` with the exact
  reconciliation request/action outcome while retaining its original lease
  generation;
- reconciliation request: exact `PENDING` record to `RESOLVED` with the paired
  resolution event;
- Session: unchanged and still owns the non-final Run;
- event history: one `reconciliation.resolved` immediately after the exact
  paired `reconciliation.requested` event.

Memory publishes copied targets only after every check succeeds. SQLite applies
the same targets inside one immediate transaction and rolls back on every
conflict/fault. Cancellation, SDK close, lease loss/expiry, and CAS tests prove
the durable outcome is either the entire decision or no decision.

### No external callbacks during resolution

`RecoveryAPI.resolve` performs lifecycle admission and the startup scanner,
then delegates to reconciliation admission. The resolution path uses the Store,
lease manager, immutable execution descriptors, and local Agent/Tool/policy
registries only. It does not call the Provider, Tool handler, MCP integration,
permission bridge, or Workflow coordinator. Public tests install forbidden
Provider/Tool callbacks and prove their call counts remain zero throughout
resolution; the external attempt occurs only after a later explicit
`recover_run`.

### Secret retention

The shared public assertion inspects the public error text, cause/context,
formatted traceback, and every retained SDK traceback frame's locals. Fresh
characterization coverage now explicitly includes:

- capability drift: `fake/capability-drift-secret`;
- post-commit partial Store failure: `partial-resolution-store-secret`;
- existing actor, evidence, replay, corrupt request/operation/event payload,
  and lazy Store wrapper secret paths.

Both requested assertions were characterization-GREEN; no production
sanitization change was required.

## RED/GREEN evidence

### Starting baseline

```text
C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe run --python 3.13 pytest tests/integration/runtime/test_reconciliation_resolution.py -q
73 passed in 8.51s
```

### Requested secret-retention characterization

Added the capability-drift and post-commit partial Store assertions before any
production change. Existing public-boundary sanitation already satisfied them:

```text
C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe run --python 3.13 pytest tests/integration/runtime/test_reconciliation_resolution.py -q -k "capability_drift or post_commit_partial_resolution"
13 passed, 60 deselected in 4.00s
```

### Exact paired-event replay gap

The audit found one genuine missing Store admission check: once every output
target was already exact after a post-commit ambiguity, a forged
`reconciliation.requested` event precondition could be ignored. A Memory/SQLite
regression test was added first.

RED:

```text
C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe run --python 3.13 pytest tests/integration/storage/test_run_progress_reconciliation.py::test_retry_resolution_exact_replay_requires_paired_requested_event -q
2 failed in 3.21s
Memory and SQLite both failed with: DID NOT RAISE RecoveryStateConflictError
```

Minimal production fix: before both fresh application and exact replay, Memory
and SQLite now authenticate the requested event precondition and its exact
request/operation/reason payload. Exact output replay still ignores a released
or expired lease, as required.

GREEN:

```text
C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe run --python 3.13 pytest tests/integration/storage/test_run_progress_reconciliation.py::test_retry_resolution_exact_replay_requires_paired_requested_event -q
2 passed in 2.94s
```

## Fresh verification evidence

All commands used the explicit executable
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe`.

### Phase 5A plus Phase 3 Provider/Tool and RecoveryAPI neighbors

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
509 passed in 85.11s
```

### Phase 2 Store, live/lease/Session/idempotency, and Phase 4 Workflow neighbors

```text
uv.exe run --python 3.13 pytest -q \
  tests/unit/runtime/test_reconciliation_models.py \
  tests/integration/storage/test_recovery_records.py \
  tests/integration/storage/test_sqlite_recovery_validation.py \
  tests/integration/runtime/test_recovery_scanner.py \
  tests/integration/runtime/test_live_run_progress.py \
  tests/integration/runtime/test_leases.py \
  tests/integration/runtime/test_session_lifecycle.py \
  tests/integration/runtime/test_run_session_ownership.py \
  tests/contract/test_memory_store_contract.py \
  tests/contract/test_idempotency_store_contract.py \
  tests/e2e/test_session_lifecycle_idempotency.py \
  tests/integration/workflow/test_workflow_recovery.py \
  tests/integration/workflow/test_workflow_recovery_admission.py \
  tests/integration/workflow/test_workflow_session_ownership.py
543 passed in 17.98s
```

### Full Python 3.13

```text
uv.exe run --python 3.13 pytest -q
1764 passed in 121.60s; zero skipped, zero failed
```

### Final focused Phase 5A public/storage gate

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
131 passed in 8.38s
```

### Static, diff, import, signature, scope, and schema gates

```text
uv.exe run --python 3.13 ruff check src tests
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files

git diff --check
exit 0 (only Windows LF-to-CRLF informational warnings)
```

The import/signature/schema smoke passed with 103 unique root exports, exact
`RecoveryAPI.resolve` and `ReconciliationService.resolve` parameter kinds and
return contracts, the existing `RecoveryAPI.recover_workflow` signature
retained, and SQLite `_SCHEMA_VERSION == 3`.

The scope check passed with exactly these implementation files before adding
this ignored report:

- `src/agent_sdk/__init__.py`
- `src/agent_sdk/api.py`
- `src/agent_sdk/runtime/reconciliation.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/storage/memory.py`
- `src/agent_sdk/storage/sqlite.py`
- `tests/integration/runtime/test_reconciliation_resolution.py`
- `tests/integration/storage/test_run_progress_reconciliation.py`

There is no dependency, lockfile, docs, roadmap, progress-ledger, migration, or
schema-version change.

## Concerns and handoff

No implementation or verification concerns remain. Phase 5A is ready for the
independent review required by the phase plan. The branch and worktree are
preserved; no merge, push, Phase 5B, M02-T003, or M02-T004 action was taken.
