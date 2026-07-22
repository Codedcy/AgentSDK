# M02-T002 Phase 5B2B Implementation Report

## Status

DONE. Phase 5B2B integrates confirmed Run outcomes with the existing explicit
Workflow recovery coordinator. The implementation commit is `489b60a`
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

## Independent review closure addendum

This addendum is authoritative over the earlier verification counts and scope
summary. Independent review found two blocking gaps. Both are closed in
implementation commit `15132ed` (`fix(recovery): close workflow projection
review gaps`) and are ready for re-review; this report does not self-approve the
independent review.

### I1: terminal projection certification TOCTOU

The first RED barrier paused the Workflow node commit after terminal Run
certification, then changed either the durable Run terminal data or Session
Workflow ownership without changing its version. Memory and SQLite both
projected the stale terminal result for completed and failed Runs:

```text
Run/Session x completed/failed x Memory/SQLite: 8 failed
```

A second RED proved the same gap for child parent evidence. Removing the parent
precondition made both Stores project the child despite same-version parent Run
corruption:

```text
child parent mutation x Memory/SQLite: 2 failed
```

Root cause: terminal certification and `complete_node`/`fail_node` were separate
durable boundaries. The node batch required only Session existence plus
Workflow/node versions, so it did not bind the projected value to the exact
certified Run, exact current Session ownership, or exact child parent.

The fix reloads and exactly compares the terminal Run after certification,
reloads and validates current Session ownership, revalidates child parent and
live capabilities, and carries exact Session, terminal Run, and optional parent
Run preconditions into the same atomic node batch. The batch also uses exact
Workflow and node data preconditions. A concurrent winning SDK is distinguished
from lost ownership by reloading the authoritative Workflow and retrying only
when it actually changed; this preserved two-SDK convergence.

```text
I1 Run/Session/parent barriers: 10 passed
```

No stale node result, Workflow terminal event, Session detach, callback, or
cursor advance survives a rejected projection.

### I2: confirmed Model ToolCall followed by later reconciliation

The public production path starts a Workflow, interrupts its first Model call,
confirms a Model result containing one ToolCall, executes that Tool normally
exactly once, interrupts the following Model call, and admits a second
reconciliation. Before the fix, both possible second decisions conflicted on
both Stores:

```text
second CONFIRM_NOT_EXECUTED/CONFIRM_COMPLETED x Memory/SQLite: 4 failed
```

Root cause was cumulative evidence normalization, not Workflow logic:

- stripping a prior Model `reconciliation.resolved` marker for current-attempt
  certification left non-contiguous normalized event sequences/cursors;
- terminal closed-world validation required a single reconciliation before it
  considered the exact normalized terminal history;
- `_effective_resolved_evidence` normalized confirmed Model ToolCalls but not a
  later confirmed terminal Model result; and
- terminal lifecycle validation could identify only one operator-confirmed
  Model operation.

The fix re-sequences the exact certification projection, validates terminal
closed-world history before applying the single-record shortcut, normalizes a
later confirmed terminal Model result, and passes the set of individually exact
confirmed Model operation IDs into the existing lifecycle FSM. This does not
introduce a new state machine or relax the Model, Tool, permission, event, or
terminal grammars.

Canonical CNE and CC paths now complete the Run and Workflow with the exact
durable Tool result, one Tool call, the expected zero-or-one final Provider
call, and stable replay of the first decision. A schema-valid corruption of the
prior confirmed Model outcome makes the second decision return the exact public
conflict with unchanged Workflow, node, Run, checkpoint, operations,
reconciliation records, cursor, and callbacks:

```text
canonical/corrupt x CNE/CC x Memory/SQLite: 8 passed
```

### Fresh post-review verification

All commands used the explicit `uv.exe` runtime with Python 3.13.

```text
new Workflow projection file:                         44 passed
reconciliation + Store focus:                        394 passed
Phase 5 Provider/Tool/RecoveryAPI focus:             772 passed
Workflow recovery/admission/ownership:               203 passed
Store/lease/Session/Workflow/observability neighbors: 688 passed
full suite after the final security tightening:     2027 passed in 135.87s

ruff check .
All checks passed!

mypy src
Success: no issues found in 75 source files

import/signature/schema smoke
103 unique root exports; public signatures unchanged; SQLite schema version 3

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The review closure changes only:

- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/workflow/executor.py`
- `src/agent_sdk/workflow/state.py`
- `tests/integration/workflow/test_workflow_reconciliation_projection.py`
- this report

There is still no public API, dependency, lockfile, migration, schema version,
root export, event contract, design, roadmap, progress ledger, or task-index
change. Phase 5C scope remains unchanged.

## Independent re-review closure addendum

Independent re-review confirmed the original I1 and I2 findings closed, then
found one new blocking regression introduced by `15132ed`. It is fixed in
implementation commit `2f2db60` (`fix(workflow): require session for node
transitions`) and is ready for another independent re-review. This report does
not self-approve that review.

### Universal Session existence on node transitions

`15132ed` replaced the former `_node_transition` precondition tuple while
adding exact terminal projection evidence. That accidentally removed the
universal `SnapshotPrecondition("session", session_id)`. Terminal recovery still
carried an exact Session precondition, but `start_node` and ordinary live
complete/fail transitions passed no related preconditions and could therefore
commit after their Session disappeared.

A public `recover_workflow` barrier now pauses the pending-node
`workflow.node.started` commit. The test deletes only the Session snapshot,
then releases the commit. Before the fix, both Stores wrote the node event and
left an orphaned running Workflow/node projection:

```text
Session-snapshot-only deletion x Memory/SQLite: 2 failed
```

The paired positive path uses the supported Store `delete_session` cascade at
the same barrier. Memory and SQLite already rejected the stale node commit and
left no Session, Workflow, node, or event residue:

```text
supported delete cascade x Memory/SQLite: 2 passed
```

The production correction restores the universal Session-exists precondition
to every `_node_transition`. Terminal projection still adds its exact Session,
certified Run, and optional parent Run preconditions in the same atomic batch;
the exact Workflow and node data preconditions are unchanged.

Fresh focused evidence after the one-line production fix:

```text
missing Session + supported cascade + terminal/parent CAS: 14 passed
new projection + Workflow admission files:               147 passed
Phase 5 Provider/Tool/RecoveryAPI core:                   772 passed
Workflow recovery/admission/ownership core:               207 passed
Store/lease/Session/Workflow/observability neighbors:     692 passed
full Python 3.13 suite:                                  2031 passed in 137.14s

ruff check .
All checks passed!

mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

This re-review closure changes only:

- `src/agent_sdk/workflow/state.py`
- `tests/integration/workflow/test_workflow_recovery_admission.py`
- this report

Phase 5C and all prior out-of-scope items remain untouched.
