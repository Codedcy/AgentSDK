# v0.1 R2 Task 3 Implementation Report

## Scope

Implemented only R2 Task 3, “Persist and Reduce Workflow Control State”:

- added the deeply immutable, JSON-compatible `WorkflowControlState`;
- added the pure deterministic `next_action` reducer and its four action types;
- added schema-v2 control-state snapshot validation and revision accounting;
- preserved schema-v1 `control=None`, sequential-prefix, version, and serialized
  payload behavior;
- added atomic `WorkflowState.advance_control` persistence with an exact Workflow
  snapshot precondition and no node snapshot rewrites;
- initialized schema-v2 Workflow snapshots with revision-1 control state.

No `WorkflowExecutor`, handle, public API, v2 driving loop, acceptance scenario,
or R2 Task 4 behavior was changed.

The existing compiler snapshot fixture received only the required
`WorkflowControlState()` value because Task 2 promotes legacy definitions to
schema-v2 and Task 3 now correctly requires control for every schema-v2
snapshot.

## Reducer Matrix

The pure reducer tests cover:

- condition true and false selection;
- pending Agent dispatch;
- completed Agent skip, proving it is never dispatched again;
- parsed JSON node output and `{"text": ...}` fallback;
- jump target advancement;
- loop iteration increment, loop exit, and durable loop-limit failure;
- complete with the last completed Agent output;
- complete without an Agent using canonical Workflow inputs;
- repeated calls returning equal actions without mutating inputs.

Reducer events are `workflow.condition.selected`,
`workflow.node.output.recorded`, `workflow.control.jumped`,
`workflow.loop.iteration`, and `workflow.loop.exited`.

## State and Compatibility

`WorkflowControlState` validates non-negative program counters and loop
iterations, positive revisions, bounded non-empty ids, branch literals, finite
JSON outputs, and strict integer counters (booleans are rejected). All mappings
and nested JSON values are defensively frozen. `model_copy` revalidates updates.

Schema-v2 snapshots require control and validate:

- the program counter is inside the instruction program;
- branch ids, loop ids, loop limits, and output node ids;
- outputs belong only to completed nodes;
- control revision contributes `revision - 1` to Workflow snapshot version;
- a selected branch may leave the other branch pending while a later node is
  completed.

Schema-v1 snapshots reject control, retain the prior sequential-prefix rule and
version calculation, and omit `control` from serialized payloads.

`advance_control` atomically writes one Workflow snapshot plus one event, uses
the exact current Workflow snapshot as a precondition, leaves every node
snapshot unchanged, and reports a retryable conflict so the caller can reload
and reduce the newly persisted state.

## TDD Evidence

Initial RED:

```text
ERROR tests/unit/workflow/test_program.py
ERROR tests/integration/workflow/test_control_state.py
ImportError: cannot import name 'WorkflowControlState'
2 errors in 2.98s
```

Counter-hardening RED:

```text
FAILED test_control_state_is_deeply_immutable_and_json_bounded
Failed: DID NOT RAISE ValidationError
```

Conflict-semantics RED:

```text
FAILED test_schema_v2_create_and_control_advance_are_atomic
assert conflict.value.retryable is True
```

During full regression, strict Pydantic model mode rejected frozen
`mappingproxy` control payloads from the idempotency store. The root cause was
confirmed by inspecting the stored payload boundary. Strictness was narrowed
to the actual integer fields, preserving both boolean rejection and frozen
payload round trips.

## Fresh Verification

Focused Task 3 tests:

```text
16 passed in 2.79s
```

All Workflow unit and integration tests:

```text
367 passed in 37.83s
```

Static verification:

```text
Success: no issues found in 3 source files
All checks passed!
```

Commands used:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/workflow/test_program.py tests/integration/workflow/test_control_state.py -q
.\.venv\Scripts\python.exe -m pytest tests/unit/workflow tests/integration/workflow -q
.\.venv\Scripts\python.exe -m mypy --strict src/agent_sdk/workflow/program.py src/agent_sdk/workflow/state.py src/agent_sdk/workflow/models.py
.\.venv\Scripts\python.exe -m ruff check src/agent_sdk/workflow tests/unit/workflow tests/integration/workflow
git diff --check
```

`git diff --check` completed successfully; only line-ending normalization
warnings were emitted.

## Independent Review Fix

The independent review's one Important and one Minor finding were corrected
without entering R2 Task 4.

### Durable last-output attribution

`WorkflowControlState` now has the minimal durable field
`last_output_node_id: str | None = None`. The default is omitted from serialized
payloads, so pre-field schema-v2 control payloads and all schema-v1 snapshots
remain loadable. Control revision and Workflow version formulas are unchanged.

The reducer sets the marker only when it first merges a completed Agent's
output. Revisiting an already-recorded completed Agent advances control without
changing the marker. `complete` uses the marker rather than flattened static
node order. Schema-v2 snapshots reject a non-null marker unless it identifies
an output already recorded for a completed node.

The regression program has static Agent order `(a, b)` and durable execution
merge order `b -> a`. It verifies:

- the first merge records `b`;
- the second merge records `a`;
- revisiting completed `b` does not replace `a`;
- canonical sorted JSON round trips preserve `a`;
- a real SQLite close/reopen round trip preserves `a`;
- completion returns `A-last`, not static-order `B-first`;
- a corrupted marker is rejected.

The SQLite regression also exposed that reducer output payloads contain frozen
nested mappings. `advance_control` now thaws its typed JSON event payload before
the atomic write, allowing the same reducer action to persist on SQLite as on
the in-memory store.

### Expression failure regression

A permanent reducer test now asserts that a missing expression value returns an
event-free `FailWorkflow` with the exact durable failure:

```text
code: workflow_expression_error
message: workflow expression value is missing
retryable: false
```

### Review-fix TDD and verification

The last-output test first failed because `WorkflowControlState` had no
`last_output_node_id`. After the minimal implementation, the focused review
matrix passed:

```text
4 passed in 2.97s
```

Fresh complete Task 3 focused tests:

```text
19 passed in 3.28s
```

Fresh complete Workflow regression:

```text
370 passed in 39.44s
```

Static verification remained clean:

```text
Success: no issues found in 3 source files
All checks passed!
```
