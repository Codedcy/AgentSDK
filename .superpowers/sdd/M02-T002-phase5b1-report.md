# M02-T002 Phase 5B1 Implementation Report

## Status

DONE. Phase 5B1 implements strict `CONFIRM_COMPLETED` resolution for
operation-linked Model reconciliation requests on Memory and SQLite. The
implementation commit is `8d9d671` (`feat(recovery): confirm model
reconciliation outcomes`).

The work is intentionally limited to confirmed Model outcomes. Confirmed Tool
outcome projection and Workflow projection remain Phase 5B2. Phase 5C fault
injection/E2E, M02-T003, M02-T004, and `TERMINATE` are not implemented here.

## Implemented scope

- Admits evidence with exactly one `provider_result` key and reconstructs the
  existing strict `ProviderRecoveryResult`; only `completed` and `failed`
  dispositions are accepted.
- Rejects coercion, extra fields, non-finite/non-object Tool arguments,
  unbounded strings, multiple Tool calls, and non-public Provider errors with
  the constant decision error and zero mutation.
- Resolves normal unknown Model outcomes in one `RunProgressBatch`, including
  the request, audit event, operation, checkpoint, Run, Session ownership,
  lifecycle events, output, messages, and token usage.
- Projects completed text outcomes to a terminal completed Run and completed
  Tool-call outcomes to `READY_FOR_TOOL` plus an interrupted, Session-owned
  Run. Tool execution occurs only after explicit recovery.
- Projects failed Provider outcomes to the canonical failed operation,
  checkpoint, Run, lifecycle, public error, and Session detach/close state.
- Closes the certified completed-model terminalization gap without rewriting
  the durable completed operation or duplicating its assistant message, usage,
  Model-completed event, or step-completed event.
- Extends resolved-history validation and exact public replay for completed
  text, completed Tool-call, failed, and terminalization-gap decisions.
- Extends the Memory and SQLite old-generation exception only for exact legal
  confirmed-Model batches. Ordinary transitions remain generation-exact.
- Adds an exact operation precondition for terminalization-only batches and
  validates its signed-integer fields and byte-identical durable relation.
- Preserves post-commit convergence, two-SDK convergence, lease/CAS atomicity,
  Session closing behavior, closed-SDK behavior, and callback-free resolution.

No LiteLLM, Provider recovery adapter, Tool, MCP, permission, hook, Workflow,
or application callback is invoked while resolving the decision.

## Changed files

- `src/agent_sdk/runtime/provider_recovery.py`
- `src/agent_sdk/runtime/reconciliation.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/storage/base.py`
- `src/agent_sdk/storage/memory.py`
- `src/agent_sdk/storage/sqlite.py`
- `tests/integration/runtime/test_reconciliation_resolution.py`

There is no dependency, lockfile, migration, SQLite schema-version, public
export-count, roadmap, or progress-ledger change.

## RED/GREEN evidence

All commands used the explicit executable
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` and Python
3.13.

### Canonical Model projections

The Memory/SQLite matrix for completed text, completed Tool call, and Provider
failure was written first.

RED:

```text
pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_confirm_completed_model_projects_exact_durable_outcome
6 failed; every case returned "reconciliation action is not supported"
```

GREEN:

```text
pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_confirm_completed_model_projects_exact_durable_outcome
6 passed
```

### Completed-model terminalization gap

The gap matrix first failed at the certified recovery-state relation. After
adding terminalization-only admission with an exact operation precondition:

```text
RED:   2 failed with recovery state conflict
GREEN: 2 passed
```

The passing assertions prove that the operation remains byte-identical and
that Model usage/completion, assistant message, and step completion are not
emitted twice.

### Replay and subsequent explicit Tool recovery

Exact confirmed replay initially failed against the closed-history grammar.
The Tool-call branch then exposed a second RED where explicit recovery still
returned `recovery required`. Resolved-history certification was extended
through the existing lifecycle and ready-for-tool certifiers.

```text
Exact replay GREEN:                    6 passed
Tool-call explicit recovery RED:       2 failed with "recovery required"
Tool-call explicit recovery GREEN:     2 passed
```

### Strict nested usage evidence

The invalid-evidence matrix initially found that nested `TokenUsage` accepted
a string token count through Pydantic coercion.

```text
RED:   194 passed, 2 failed; coerced usage was admitted
GREEN: 40 passed in the targeted invalid-evidence and Provider-model gate
```

The existing recovery model now validates the nested usage mapping before
construction and accepts only real nonnegative integers.

## Fresh verification evidence

### Focused reconciliation and Store gate

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
214 passed in 13.59s
```

### Reconciliation, Provider, Tool-recovery, and RecoveryAPI gate

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
592 passed in 90.83s
```

### Phase 2 Store/lease/Session and Phase 4 Workflow neighbors

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
543 passed in 18.46s
```

### Full Python 3.13 suite

```text
uv.exe run --python 3.13 pytest -q
1847 passed in 121.03s; zero skipped, zero failed
```

### Static, diff, import, signature, schema, and scope gates

```text
uv.exe run --python 3.13 ruff check src tests
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The import/signature/schema smoke passed with 103 unique `agent_sdk.__all__`
exports, exact unchanged `RecoveryAPI.resolve` and
`ReconciliationService.resolve` signatures, and SQLite schema version 3. The
pre-report scope check contained exactly the seven files listed above.

## Risks and handoff

No known Phase 5B1 implementation or verification risk remains. The large
recovery projection is guarded by exact lifecycle, capability, operation,
checkpoint, Run, Session, event-envelope, request, current-lease, and Store
preconditions on both backends, plus replay and corruption matrices.

Phase 5B2 must separately design and test confirmed Tool outcomes and Workflow
projection; this implementation must not be treated as admitting either. The
branch and worktree are preserved for independent review. No merge or push was
performed.
