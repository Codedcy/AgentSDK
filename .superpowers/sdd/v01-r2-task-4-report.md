# v0.1 R2 Task 4 Implementation Report

## Scope

Implemented only R2 Task 4, “Execute and Recover Conditions/Loops”:

- routed schema-v2 control programs through the pure `next_action` reducer;
- kept schema-v1 and promoted linear-v2 execution on the existing sequential
  driver and preserved their public event shape;
- added public compile-only `sdk.workflows.compile(...)`;
- executed true/false conditions, bounded loops, durable loop-limit failures,
  unselected pending nodes, reducer-selected final output, and executed-only
  usage;
- reused the existing Agent Run creation, Agent Loop, child ownership,
  capability descriptor, recovery certification, unknown-outcome, and
  reconciliation paths;
- added public condition/loop timeline acceptance and restart coverage.

No parallelism, foreach, arbitrary retry, compensation, approval node,
subworkflow, R2 checkpoint, or R3 work was added.

## Minimal Loop/Restart Architecture Extension

The Task 3 snapshot shape had one durable `WorkflowNodeSnapshot` per static
Agent id. That shape could represent either:

1. “this static Agent completed, never dispatch it again”, or
2. “the loop revisited this static Agent, dispatch it again”,

but not both. Resetting the node to pending would also lose the crash boundary
between a completed side effect and program-counter advancement.

Task 4 therefore adds the smallest durable generation protocol needed by the
planned loop semantics:

- `WorkflowNodeSnapshot.execution_count` is the persisted logical execution
  index;
- `WorkflowControlState.node_execution_counts` is the deeply frozen mapping of
  execution indexes already consumed by the reducer;
- `RunSnapshot.workflow_node_execution` binds the selected parent/child Run to
  the exact logical execution;
- schema-v2 Run idempotency keys include the same execution index;
- a completed node with `execution_count == consumed + 1` is consumed and
  advances the program;
- a node with `execution_count == consumed` is eligible for the next loop
  execution;
- starting the next execution atomically increments the node execution index
  and selects a new Run id;
- prior iteration usage moves to `accumulated_usage`; the current Run keeps its
  own usage so existing child-parent certification remains exact;
- final Workflow usage sums prior and current usage, and the durable
  `last_output_node_id` advances on the last consumed logical execution.

Both counters are strict non-negative integers. Boolean counters, negative
counters, mutation aliases, unknown ids, future consumption, and inconsistent
snapshot/version relationships are rejected. The mappings remain deeply
immutable after construction and SQLite round trips.

Schema-v1 nodes retain `execution_count=0`, omit all new default fields, retain
the old version rules and Run binding, and emit the old event payloads. A
permanent integration regression executes a real schema-v1 IR and checks the
unchanged four-event timeline.

## TDD Evidence

Initial public control-flow RED:

```text
FFF                                                                      [100%]

AttributeError: 'WorkflowAPI' object has no attribute 'compile'
AssertionError: selected branch ran for a false condition
Failed: DID NOT RAISE for bounded loop exhaustion

3 failed in 3.74s
```

After the minimal schema-v2 driver and durable execution-generation protocol:

```text
...                                                                      [100%]
3 passed in 3.18s
```

The final focused matrix, including schema-v1 compatibility:

```text
....                                                                     [100%]
4 passed in 3.57s
```

## Durable Recovery Evidence

`tests/integration/workflow/test_control_recovery.py` uses real temporary SQLite
databases. Its commit-after-success cancellation store only injects the process
boundary; all recovery decisions use reopened SQLite snapshots, events, Runs,
control counts, execution indexes, and recovery evidence.

The matrix proves:

- restart after a persisted condition selection;
- restart after the second loop-iteration decision, when the first body
  execution is already durably completed and consumed;
- restart after an Agent Run/node completed but before reducer PC advancement;
- the selected Agent and each loop logical execution invoke the model exactly
  once;
- a child Run with an unknown external result remains `interrupted`, the
  Workflow remains running, and explicit Workflow recovery enters the existing
  bounded reconciliation path without replay;
- the child Run carries `workflow_node_execution=1`.

Fresh result:

```text
....                                                                     [100%]
4 passed in 5.16s
```

## Public Acceptance

The v0.1 E2E acceptance uses one shared `InMemoryStore` and a fresh SDK object
to exercise the public compile/start/reopen/timeline surface without repeating
the SQLite migration test layer:

- generated YAML is compiled but does not create a Workflow snapshot;
- explicit start selects one condition branch;
- one loop body executes twice;
- the second SDK resumes after a persisted boundary without replay;
- the public timeline includes condition selection, two loop iterations, loop
  exit, and Workflow completion.

Real SQLite durability is covered separately by the integration matrix above;
the E2E test itself does not claim SQLite coverage.

Fresh result:

```text
.                                                                        [100%]
1 passed in 3.35s
```

## Verification

Complete planned Workflow behavior matrix:

```text
380 passed in 48.36s
```

from:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/workflow tests/integration/workflow tests/e2e/test_v01_release.py -q
```

Changed durable runtime model unit tests:

```text
120 passed in 3.37s
```

Subagent and observability compatibility:

```text
78 passed in 4.70s
```

Static verification:

```text
All checks passed!
Success: no issues found in 16 source files
git diff --check: clean
```

`uv` is unavailable on `PATH`; all plan commands used the repository virtual
environment.

## Files Changed

- `src/agent_sdk/api.py`
- `src/agent_sdk/observability/queries.py`
- `src/agent_sdk/runtime/commands.py`
- `src/agent_sdk/runtime/models.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/subagents/service.py`
- `src/agent_sdk/workflow/executor.py`
- `src/agent_sdk/workflow/models.py`
- `src/agent_sdk/workflow/program.py`
- `src/agent_sdk/workflow/state.py`
- `tests/e2e/test_v01_release.py`
- `tests/integration/workflow/test_control_execution.py`
- `tests/integration/workflow/test_control_recovery.py`
- `tests/integration/workflow/test_control_state.py`
- `tests/integration/workflow/test_workflow_recovery_admission.py`

The recovery-admission helper change only updates its manually seeded selected
Run to carry the new schema-v2 execution index and indexed idempotency key.

## Concerns

None within Task 4. R2 Task 5 and later release slices remain unstarted.
