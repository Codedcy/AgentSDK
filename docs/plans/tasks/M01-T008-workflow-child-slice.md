# M01-T008 Workflow and Child Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILLS: use `superpowers:test-driven-development` while implementing and `superpowers:verification-before-completion` before reporting completion. Execute this task in the current worktree; do not create another worktree.

**Goal:** Compile and durably execute a strict sequential Workflow containing a standard parent Agent Run and one context-isolated Child Run, then resume it from SQLite without repeating a completed node.

**Architecture:** YAML and Python `WorkflowDefinition` values compile into the same frozen canonical `WorkflowIR`. A persisted `WorkflowRunSnapshot` owns ordered `WorkflowNodeSnapshot` transitions. Every `agent` node is executed by the existing `RuntimeCommands`/`RunEngine`; a Child is a normal `RunSnapshot` with explicit parent/workflow relationship fields and a serialized `TaskEnvelope`, not a second lightweight agent loop. All Workflow and Child records are Session-owned so `delete_session` removes them.

**Tech Stack:** Pydantic v2 discriminated unions, `yaml.safe_load`, SHA-256 canonical JSON, asyncio, existing StateStore/RuntimeCommands/RunEngine, SQLite and pytest-asyncio.

## Global Constraints

- Only the M01 sequential `agent` node subset is implemented. Parallelism, waits, retries, dynamic generation/approval, budgets, permission intersection, messages and detach remain M04 work.
- The executor accepts only validated `WorkflowIR`; it never evaluates arbitrary Python, callbacks, template expressions or YAML tags.
- Child model input contains the immutable `TaskEnvelope`, selected explicit references and its own future history only. Parent output/history is not inherited implicitly.
- Agent nodes are standard Runs and therefore reuse the existing LiteLLM-only Agent Loop, Tools, permissions, events, usage and terminal semantics.
- Workflow, node and child state changes are persisted before exposure. Session existence is an atomic Store precondition so Session deletion cannot race with a later commit and resurrect data.
- Reopening a completed Workflow is read-only. Reopening after a completed node never calls that node's model again. A terminal underlying Run is reconciled into its node before deciding whether to execute.
- Public failures use stable sanitized `AgentSDKError` values; `asyncio.CancelledError` propagates and leaves durable state resumable.

---

### Task 1: Define strict Workflow, Child and result contracts

**Files:**
- Create: `src/agent_sdk/workflow/__init__.py`
- Create: `src/agent_sdk/workflow/models.py`
- Create: `src/agent_sdk/workflow/dsl.py`
- Create: `src/agent_sdk/workflow/compiler.py`
- Create: `src/agent_sdk/subagents/__init__.py`
- Create: `src/agent_sdk/subagents/models.py`
- Create: `tests/unit/workflow/test_workflow_compiler.py`

**Public interfaces:**
- `AgentNode`, `WorkflowEdge`, `WorkflowDefinition`, `WorkflowIR`
- `WorkflowRunStatus`, `WorkflowNodeStatus`, `WorkflowRunSnapshot`, `WorkflowNodeSnapshot`, `WorkflowResult`
- `WorkflowCompiler.compile`, `WorkflowCompiler.compile_yaml`
- `TaskEnvelope`, `ChildResult`

- [ ] **Step 1: Write compiler and immutability RED tests**

Cover Python/YAML parity, stable canonical bytes/hash independent of mapping insertion order, deep detachment, strict extra-field rejection, supported `api_version`/`kind`, and exact node order.

```python
definition = WorkflowDefinition.model_validate({
    "api_version": "agent-sdk/v1",
    "kind": "Workflow",
    "name": "parent-child",
    "nodes": [
        {"id": "plan", "kind": "agent", "agent_revision": "planner:1", "input": "plan"},
        {
            "id": "child", "kind": "agent", "agent_revision": "worker:1",
            "input": "verify", "run_as": "child",
            "success_criteria": ["return a verification result"],
            "evidence_refs": [],
        },
    ],
    "edges": [{"source": "plan", "target": "child"}],
})
assert WorkflowCompiler().compile(definition).canonical_json() == (
    WorkflowCompiler().compile_yaml(WORKFLOW_YAML).canonical_json()
)
```

- [ ] **Step 2: Write graph/YAML fail-closed RED tests**

Reject empty definitions, duplicate ids, missing edge endpoints, self/cyclic edges, disconnected nodes, more than one root, branching/joining graphs, a root Child (no parent Run), unsupported node kinds/conditions, extra YAML documents/tags/aliases and bounded-size/depth/item violations. Diagnostics must not include the complete untrusted YAML document.

- [ ] **Step 3: Implement frozen models and the sequential compiler**

Use `ConfigDict(frozen=True, extra="forbid")`, tuples and recursively detached JSON-like values. Normalize the single chain into deterministic topological order, sort canonical keys, and compute `definition_hash` from canonical IR content excluding the hash field itself. `WorkflowIR` records a schema version and exposes canonical JSON bytes/text without mutable aliases.

`TaskEnvelope` for this slice contains at least `objective`, `success_criteria`, `instructions`, `evidence_refs`, `allowed_tools` and `workspace_scopes`. `ChildResult` contains `run_id`, terminal status, summary/output, evidence refs and usage. Do not place parent prompt/history in either model.

- [ ] **Step 4: Verify Task 1**

```powershell
uv run --python 3.13 pytest tests/unit/workflow/test_workflow_compiler.py -v
```

Expected: Python/YAML inputs produce the same frozen IR/hash; every invalid or unsafe input fails before execution.

---

### Task 2: Extend normal Runs for explicit Workflow/Child relationships

**Files:**
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/runtime/commands.py`
- Modify: `src/agent_sdk/runtime/engine.py`
- Create: `src/agent_sdk/runtime/agents.py`
- Create: `src/agent_sdk/subagents/service.py`
- Create: `tests/integration/subagents/test_child_run_slice.py`

**Interfaces:**
- Extend `RunSnapshot` with optional `parent_run_id`, `workflow_run_id`, `workflow_node_id` and `task_envelope` relationship data.
- Extend `RuntimeCommands.start_run` with internal keyword-only relationship inputs while preserving all existing calls.
- `SubagentService.spawn` and `SubagentService.await_result`.

- [ ] **Step 1: Write normal-Run and isolation RED tests**

Create a parent Run, then spawn a Child with an explicit envelope. Capture the child `ModelRequest` and assert:

- the Child has its own `run.created` through terminal Run events and normal usage;
- `parent_run_id`, `workflow_run_id` and `workflow_node_id` survive snapshot validation and SQLite reopen;
- the rendered TaskEnvelope/objective is present;
- a marker returned in the parent output and unrelated parent messages are absent;
- explicit evidence-ref identifiers remain present and ordered;
- cancellation propagates without fabricating a completed Child result.

- [ ] **Step 2: Make Run creation Session-safe**

`RuntimeCommands.start_run` and every `_RunEmitter` event/snapshot commit must require the owning Session snapshot as an atomic `SnapshotPrecondition`. A missing/deleted Session maps to stable NOT_FOUND, and a concurrent `delete_session` cannot leave or later recreate an orphan Run snapshot/event. Preserve existing Run behavior and tests.

- [ ] **Step 3: Implement SubagentService using the existing engine**

Resolve the requested immutable `AgentSpec`, create a normal related Run, render only the TaskEnvelope plus explicitly resolved references into the child request, schedule the existing `RunEngine.execute`, and translate the terminal `RunResult` into a frozen `ChildResult`. Do not copy the parent's `user_input`, output text, hidden messages, activated Skills or Tool results.

M01 only carries `allowed_tools` and `workspace_scopes` as explicit narrowing metadata; complete permission/workspace/budget intersection is implemented in M04. Do not label these fields as an enforcement boundary in this slice.

- [ ] **Step 4: Verify Task 2**

```powershell
uv run --python 3.13 pytest tests/integration/subagents/test_child_run_slice.py tests/integration/runtime -v
```

Expected: Child is a related standard Run, context isolation is observable at the actual LiteLLM request seam, reopen preserves relations, and Session deletion/races leave no Child data.

---

### Task 3: Implement durable sequential execution and recovery

**Files:**
- Create: `src/agent_sdk/workflow/state.py`
- Create: `src/agent_sdk/workflow/executor.py`
- Create: `src/agent_sdk/workflow/handles.py`
- Create: `tests/integration/workflow/test_workflow_child_slice.py`
- Create: `tests/integration/workflow/test_workflow_recovery.py`

**Interfaces:**
- `WorkflowExecutor.start`, `WorkflowExecutor.resume`, `WorkflowExecutor.get`
- `WorkflowHandle.result`, `WorkflowHandle.events`

- [ ] **Step 1: Write sequential execution RED test**

Register `planner:1` and `worker:1`, create a Session and start the two-node IR. Assert stable order, both node statuses completed, two normal Run ids, exactly one Child id, Child parent id equals the plan node Run id, final output/usage are retained, and Workflow lifecycle events are durable before `result()` returns.

The Workflow event aggregate uses its `workflow_run_id` in the current `EventEnvelope.run_id` slot so it has an independent monotonic sequence. Node snapshots use deterministic entity ids under that Workflow and all snapshots carry the owning `session_id`.

- [ ] **Step 2: Write crash/reopen and delete RED tests**

Use a Store wrapper that commits the first `workflow.node.completed` transaction and then raises `CancelledError`. Reopen the same SQLite file with fresh services and agent registrations, call `resume(workflow_run_id)`, and prove the first model call count remains one while the pending Child completes. Also verify:

- `resume` on an already completed Workflow performs zero model calls;
- a node whose related Run is already terminal is reconciled instead of re-executed;
- an IR/hash mismatch is rejected;
- Session deletion removes Workflow, node, parent and Child snapshots/events;
- a delete/transition race cannot resurrect Workflow or node state.

- [ ] **Step 3: Implement atomic Workflow state transitions**

Persist `workflow.started`, `workflow.node.started`, `workflow.node.completed`/`failed`, and `workflow.completed`/`failed` with the matching snapshots in Store transactions. Every transition checks both Session existence and expected Workflow snapshot version. Only expose a new snapshot/result after commit succeeds.

Before invoking a node, persist its selected Run id. On resume:

1. Load and validate the stored canonical IR/hash.
2. Return immediately if Workflow is terminal.
3. Skip completed nodes.
4. If a started node references a terminal Run, project that terminal result into the node.
5. If it references a still-created Run, execute that existing Run; if the Run had already crossed into a nonterminal side-effecting state, fail closed with a resumable/interrupted diagnostic instead of replaying it in M01.
6. Otherwise create the next normal Run and continue the chain.

M01 supports one active executor owner; cross-process leases and ambiguous in-flight external-side-effect reconciliation remain M02/M04 work and must not be simulated with an in-memory lock.

- [ ] **Step 4: Implement handles and failure semantics**

`WorkflowHandle.result()` shields the execution task from caller cancellation, validates the persisted terminal snapshot, and never reports completion early. `events(cursor)` filters the Workflow aggregate in global cursor order and drains the terminal event. Ordinary internal/provider failures are sanitized into durable node/workflow failure state; cancellation is propagated and leaves the last committed state resumable.

- [ ] **Step 5: Verify Task 3**

```powershell
uv run --python 3.13 pytest tests/integration/workflow/test_workflow_child_slice.py tests/integration/workflow/test_workflow_recovery.py -v
```

Expected: stable sequential execution, isolated Child, durable transitions, SQLite recovery without completed-node replay, and complete Session cleanup.

---

### Task 4: Wire the public SDK façade and exports

**Files:**
- Modify: `src/agent_sdk/api.py`
- Modify: `src/agent_sdk/__init__.py`
- Modify: `src/agent_sdk/workflow/__init__.py`
- Modify: `src/agent_sdk/subagents/__init__.py`
- Modify: `tests/integration/workflow/test_workflow_child_slice.py`

**Interfaces:**
- `sdk.agents.define(AgentSpec(...))` for immutable in-process revision resolution in M01.
- `sdk.workflows.start(session_id, definition_or_yaml)`, `resume(workflow_run_id)`, `get(workflow_run_id)`.
- Public package exports for the contracts named in Tasks 1-3.

- [ ] **Step 1: Write façade RED tests**

Use only public imports and `AgentSDK.for_test`. Duplicate `name:revision` registration conflicts; unknown revisions fail before node execution; SDK close waits for Workflow/Child tasks as well as ordinary Runs; a new start is rejected once SDK closing begins.

- [ ] **Step 2: Add thin delegation APIs**

Keep domain logic in compiler/executor/subagent services. `AgentRegistry` may be in-memory for M01 because the immutable canonical Workflow snapshot retains revision references and the application re-registers the same revisions after reopen; revision persistence/hot reload is later Runtime hardening. Track all Workflow-created Run tasks in the existing SDK lifecycle so close cannot abandon them.

- [ ] **Step 3: Run the task gate**

```powershell
uv run --python 3.13 pytest tests/unit/workflow tests/integration/workflow tests/integration/subagents -v
uv run --python 3.13 pytest -q
uv run --python 3.13 ruff check src tests
uv run --python 3.13 mypy src
git diff --check
```

Expected: all existing and new tests pass; no ignored/xfailed acceptance path; public models are immutable; SQLite reopen and Session deletion behavior are proven.

- [ ] **Step 4: Commit**

```powershell
git add docs/plans/tasks/M01-T008-workflow-child-slice.md src/agent_sdk tests
git commit -m "feat: add durable workflow and child slice"
```
