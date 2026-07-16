# M02-T002 Phase 5B2A Implementation Report

## Status

DONE. Phase 5B2A implements strict `CONFIRM_COMPLETED` resolution for
operation-linked Tool reconciliation requests on Memory and SQLite. The
implementation commit is `2178fab` (`feat(recovery): confirm tool
reconciliation outcomes`).

This slice is intentionally limited to confirmed Tool outcomes. Workflow
snapshot projection remains Phase 5B2B. Phase 5C fault-injection/E2E,
M02-T003, M02-T004, and `TERMINATE` remain untouched.

## Implemented scope

- Accepts evidence only as the exact object `{"tool_result": ...}` and
  reconstructs a strict detached `ToolResult` without Pydantic coercion.
- Enforces exact Tool-result fields and raw types, valid statuses, 16 KiB
  content/value bounds, a 512-byte error bound, finite JSON values, string
  object keys, and a JSON round trip. Tuples, non-finite numbers, arbitrary
  objects, unknown fields, wrong call IDs, and wrong Tool names fail closed.
- Reuses the Phase 5A closed lifecycle, reconciliation-operation, capability,
  and Tool-call certifier chain before admitting an operator decision.
- Projects one exact atomic batch: resolved audit record, terminal Tool
  operation, `READY_FOR_MODEL` checkpoint at the next turn, interrupted Run,
  then `reconciliation.resolved`, `tool.call.completed`, and `step.completed`.
- Maps only `succeeded` to a completed Tool operation; denied, timed out,
  invalid arguments, and normalized execution failures remain canonical
  failed Tool outcomes while still becoming authoritative durable results.
- Extends Memory and SQLite old-generation admission only for the exact legal
  confirmed-Tool batch. Ordinary transition and generation rules remain
  unchanged.
- Authenticates exact public replay both immediately and after legal later
  history: normal terminal completion, a later Tool turn, or a unique later
  pending Model reconciliation.
- Preserves stable replay after post-commit ambiguity and across two SDK
  instances, without re-running the Tool or invoking Provider, MCP,
  permission, hook, or application callbacks.
- Preserves Session closing/deletion behavior, explicit recovery, SDK close,
  cancellation atomicity, capability-drift rejection, and zero mutation on
  every invalid/conflicting request.
- Rejects missing, duplicated, moved, or malformed Tool lifecycle and atomic
  resolution evidence on both Stores.

The certification-only effective history removes reconciliation control
markers, retains the authoritative Tool completion and step completion, and
places the original interruption at its certified historical position. The
durable event history is never rewritten.

## Changed files

- `src/agent_sdk/runtime/reconciliation.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/storage/memory.py`
- `src/agent_sdk/storage/sqlite.py`
- `tests/integration/runtime/test_reconciliation_resolution.py`
- `.superpowers/sdd/M02-T002-phase5b2a-report.md`

There is no dependency, lockfile, migration, SQLite schema-version, public
export-count, Workflow snapshot, roadmap, or progress-ledger change.

## RED/GREEN evidence

All commands used
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` with
Python 3.13.

### Canonical Tool projection

The production-path Memory/SQLite matrix for succeeded and normalized
non-JSON failure results was written first.

```text
RED:  4 failed in 5.94s; public resolve returned the unsupported-action error
GREEN: the initial matrix passed; the expanded 8-shape/2-Store matrix passed
       16 cases
```

The expanded matrix covers scalar, object, list, and null success values plus
normalized non-JSON failure, denied, timed-out, and invalid-arguments results.
Every case proves exact recovery, no repeated Tool call, later terminal
completion, and stable replay.

### Strict evidence and zero mutation

The raw evidence matrix covers extra/missing fields, coercible strings,
tuples, non-string keys, non-finite values, arbitrary objects, oversized
payloads, mismatched call identity, and mismatched Tool identity.

```text
RED:  wrong call/name evidence reached durable-state conflict instead of the
      constant non-retryable invalid-state decision
GREEN: 2 Store cases passed the complete invalid-evidence matrix with zero
       durable mutation
```

Boundary and detachment tests additionally prove exact 16 KiB admission,
deep detachment from caller-owned values, and conflict on changed replay.

### Atomicity, replay, and closed history

Dedicated Memory/SQLite matrices pass for post-commit ambiguity, two-SDK
convergence, cancellation, SDK close, Session closing/deletion, later pending
Model reconciliation, later Tool turns, capability drift, and Store-level
malformed old-generation batches.

The missing/duplicate/moved Tool lifecycle matrix passes 10 cases. It
authenticates the complete historical Tool lifecycle rather than only the
confirmed suffix.

## Fresh verification evidence

### Focused reconciliation and Store gate

```text
pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
334 passed in 22.02s
```

### Reconciliation, Provider, Tool-recovery, and RecoveryAPI gate

```text
pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
712 passed in 96.47s
```

### Store/lease/Session and Workflow neighbors

```text
pytest -q \
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
543 passed in 18.59s
```

### Full Python 3.13 suite

```text
pytest -q
1967 passed in 131.02s; zero skipped, zero failed
```

### Static, compatibility, diff, and scope gates

```text
ruff check .
All checks passed!

mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The fresh compatibility smoke passed with 103 unique `agent_sdk.__all__`
exports, exact unchanged `RecoveryAPI.resolve` and
`ReconciliationService.resolve` signatures, and SQLite schema version 3. The
pre-report scope check contained exactly the five implementation/test files
listed above.

## Risks and handoff

No known Phase 5B2A implementation or verification risk remains. The
confirmed-Tool exception is narrow and must stay bound to the exact resolution
record, exact Tool operation, exact lifecycle suffix, and the existing closed
historical certifier chain.

Phase 5B2B must separately design and test Workflow snapshot projection. This
implementation must not be treated as admitting Workflow evidence or changing
Workflow recovery semantics. The branch and worktree are preserved for
independent review. No merge or push was performed.
