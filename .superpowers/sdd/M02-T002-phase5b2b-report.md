# M02-T002 Phase 5B2B Implementation Report

## Status

DONE. Phase 5B2B integrates confirmed Run outcomes with the existing explicit
Workflow recovery coordinator. The implementation commit is `3bacf27`
(`feat(recovery): certify workflow terminal projection`). It is ready for the
required independent Spec and Quality review; this report does not claim that
review approval.

Phase 5C subprocess fault/E2E release work, M02-T003, M02-T004, `TERMINATE`,
Workflow scheduler leases, and any new Workflow state machine remain outside
this slice.

## Delivered behavior

- Added production-path Memory and SQLite coverage beginning at public
  `workflows.start`, creating real Model or Tool in-flight work, expiring the
  real Run lease, invoking public `scan`, reopening, entering reconciliation
  through public `recover_workflow`, resolving through public `resolve`, and
  projecting only after a later explicit public `recover_workflow`.
- Confirmed terminal Model text is projected exactly once to the running node
  and Workflow, including exact output and usage, with zero Provider or Tool
  callback for the already-terminal child. Exact decision replay remains
  read-only after Workflow projection.
- Confirmed Provider failure is projected through the existing exact
  `WorkflowFailure` path, including one node failure, one Workflow failure,
  Session detach/close, zero external callback, and stable decision replay.
- Confirmed Tool success and normalized Tool failure remain authoritative
  durable Tool results. Explicit Workflow recovery resumes the existing Run
  recovery coordinator, sends the exact durable Tool message to one next
  Provider turn, never repeats the Tool side effect, and projects the eventual
  terminal Run rather than treating the Tool status as Workflow failure.
- A confirmed first node in a two-node Workflow projects exactly, starts one
  child Run with the exact parent, node, task envelope, descriptor, and input,
  and returns the exact final output and summed usage. Public execution-tree
  queries show the exact root/child relation; Tool cases show the exact durable
  `ToolResult` in the terminal Run tree.
- A later second reconciliation is supported: after a confirmed Tool result, a
  following Model attempt can become unknown, receive an explicit
  `CONFIRM_NOT_EXECUTED` decision, resume, complete the Workflow, and still
  replay the original confirmed Tool decision without writes or callbacks.
- Deterministic two-SDK Memory/SQLite races cover both a confirmed terminal
  child at node projection and a confirmed Tool child at Run lease admission.
  They converge on one Provider call where needed, zero repeated Tool calls,
  and one node/Workflow terminal projection.
- Post-commit cancellation after `workflow.node.completed` and after the
  Workflow terminal/Session-detach batch reopens and converges without a
  duplicate event or external call.
- A closing Session stays closing after the terminal Run detaches while the
  Workflow remains active, closes only after Workflow detach, rejects public
  delete while owned, and deletes all Session, Run, Workflow, node, checkpoint,
  operation, reconciliation, event, and idempotency state after completion.
- Confirmed terminal capability drift fails before Workflow mutation or
  external work. Existing Phase 4 mismatch/corruption matrices remain green for
  child/node/parent/task/descriptor, Session ownership, event ordering,
  projection ambiguity, SDK close, and cancellation.

## Architectural correction

The only production defect exposed by the new tests was that explicit
Workflow recovery accepted a syntactically valid terminal `RunSnapshot` and
projected it without authenticating the checkpoint, operations,
reconciliations, and complete certified Run history.

The correction does not add a reconciliation-specific Workflow state machine:

- `RunRecoveryService._certify_terminal_run_for_workflow` is a read-only
  internal boundary. It loads the same `_RecoveryEvidence`, verifies terminal
  Session ownership, requires a terminal checkpoint and no started operation,
  and reuses the existing closed reconciliation grammar, exact confirmed Model
  replay certifier, exact confirmed Tool replay certifier, and ordinary
  terminal Provider/lifecycle FSM.
- `WorkflowExecutor` calls that certifier only from explicit Workflow recovery
  after exact Workflow admission and before node mutation. After the await, it
  rechecks the exact selected Run/session/Workflow/node/agent/parent/task/
  descriptor relation before the existing node CAS.
- `RecoveryAPI.resolve` remains Run-only and unchanged. It still writes no
  Workflow or node snapshot and invokes no Workflow, Provider, Tool, MCP,
  permission, hook, or application callback.
- No public method, root export, event meaning, SQLite table, migration, or
  schema version changed.

## TDD evidence

### Initial production-path characterization

The first tests covered confirmed Model text/failure, confirmed Tool success/
normalized failure, resolve-without-Workflow-mutation, stable replay, and
multi-node projection across Memory and SQLite. Existing production behavior
already satisfied the legal paths:

```text
pytest -q tests/integration/workflow/test_workflow_reconciliation_projection.py
10 passed in 5.52s
```

No production edit was made for those characterization-green cases.

### Corrupt terminal Run RED

The adversarial test first creates a real confirmed terminal Run, then changes
only its durable `output_text`, leaving its certified checkpoint, operation,
resolution record, and events unchanged. Workflow recovery must fail before
node or Workflow mutation and before external work.

```text
pytest -q \
  tests/integration/workflow/test_workflow_reconciliation_projection.py::test_workflow_recovery_rejects_corrupt_confirmed_terminal_run
2 failed in 3.63s
Memory and SQLite both returned a completed Workflow instead of raising.
```

Root cause: `WorkflowExecutor._drive_recovery` bypassed Run recovery for a
terminal selected child and called `_run_result(run)` directly. The terminal
snapshot identity fields matched the node, so forged output was projected.

### Terminal certification GREEN

After wiring the existing Run certifiers into explicit Workflow recovery:

```text
corruption + confirmed Model text/failure + multi-node:
8 passed in 4.15s

complete new file after all adversarial and deletion-cleanup additions:
26 passed in 6.19s
```

The corrupt Run now fails with zero Workflow/node mutation, unchanged cursor,
and zero Provider calls on both Stores. All legal confirmed Model, Tool,
multi-resolution, two-SDK, ambiguity, closing/delete, capability, and query
paths remain green.

The only later test corrections were test-contract fixes, not production
defects: Store recovery-record list methods intentionally conflict after their
owning Run is deleted, and `read_events` returns an empty list rather than an
empty tuple. Raw backend checks now prove the records are deleted.

## Fresh verification evidence

All commands used
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` with
Python 3.13.

### New Workflow integration plus reconciliation Store focus

```text
pytest -q \
  tests/integration/workflow/test_workflow_reconciliation_projection.py \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
376 passed in 25.29s
```

### Phase 5A/5B1/5B2A Provider, Tool, and RecoveryAPI focus

```text
pytest -q \
  tests/integration/workflow/test_workflow_reconciliation_projection.py \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
754 passed in 100.48s
```

### Workflow recovery/admission/ownership

```text
pytest -q \
  tests/integration/workflow/test_workflow_reconciliation_projection.py \
  tests/integration/workflow/test_workflow_recovery_admission.py \
  tests/integration/workflow/test_workflow_recovery.py \
  tests/integration/workflow/test_workflow_session_ownership.py
185 passed in 11.55s
```

The complete pre-existing Workflow admission matrix independently remained
**99 passed**.

### Store, lease, Session, Workflow, and observability neighbors

The required reconciliation models/records, SQLite recovery validation,
scanner, live progress, lease, Session lifecycle/ownership, Memory and
idempotency contracts, Session E2E, all Workflow recovery/ownership files, and
public/internal observability query files passed together:

```text
620 passed in 22.07s
```

### Full Python 3.13 suite

```text
pytest -q
2009 passed in 136.22s; zero failed, zero skipped
```

### Static, public-contract, schema, diff, and scope gates

```text
ruff check .
All checks passed!

mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The fresh import/signature/schema smoke passed with 103 unique root exports,
the exact unchanged public `RecoveryAPI.resolve`,
`ReconciliationService.resolve`, and `RecoveryAPI.recover_workflow`
signatures, and SQLite schema version 3.

Implementation scope is exactly:

- `src/agent_sdk/api.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/workflow/executor.py`
- `tests/integration/workflow/test_workflow_reconciliation_projection.py`
- this forced-included report

There is no dependency, lockfile, migration, schema-version, root-export,
event-contract, design, roadmap, progress-ledger, or task-index change.

## Remaining Phase 5C scope

Phase 5C still owns the subprocess hard-exit cases after Provider acceptance,
after Tool side effect, and after safe Tool outcome commit; the final E2E
release matrix; Python 3.12 plus 3.13 release gates; wheel/sdist and clean-wheel
imports; reference CLI help; final M02-T002 report; whole-task independent
review; and progress-ledger transition. This slice does not claim or implement
any of those items.
