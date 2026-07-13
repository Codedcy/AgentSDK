# M04-T002 Durable Workflow Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute every Workflow IR node durably with retries, waits, parallel joins, quality gates, and restart recovery.

**Architecture:** The workflow executor is a persisted state machine. A generation-fenced Workflow scheduler lease gives one worker authority to advance nodes; each node attempt is claimed, started, and completed through events plus a projection transaction. A node's Run independently retains the M02 Run lease before external side effects, which use operation ids and explicit unknown-outcome handling.

**Tech Stack:** asyncio TaskGroup, SQLite, pytest-asyncio, fault injection.

## Global Constraints

- Node state is reconstructable without the previous Python stack.
- Retry policy distinguishes retryable failure, permanent failure, unknown outcome, and denied permission.
- Approval/input waits release leases and resume from durable records.
- Parallel/foreach scheduling is bounded and join semantics are deterministic.
- Only the current Workflow scheduler lease generation may claim or project a
  node attempt. Losing/stale workers fail before node dispatch, while every
  agent node also requires the current M02 Run lease before external effects.

---

### Task 1: Implement durable node attempts and failure routing

**Files:**
- Modify: `src/agent_sdk/workflow/executor.py`
- Modify: `src/agent_sdk/workflow/state.py`
- Modify: `src/agent_sdk/workflow/events.py`
- Modify: `src/agent_sdk/storage/sqlite.py`
- Create: `tests/integration/workflow/test_node_lifecycle.py`
- Create: `tests/integration/workflow/test_workflow_leases.py`

- [ ] **Step 1: Write failing lifecycle and retry tests**

```python
@pytest.mark.asyncio
async def test_retry_creates_distinct_attempts(workflow_runner, flaky_node) -> None:
    result = await workflow_runner.run(workflow_with(flaky_node, max_attempts=3))
    assert result.status == "completed"
    assert result.node("flaky").attempts == 3

@pytest.mark.asyncio
async def test_permanent_failure_uses_failure_edge(workflow_runner) -> None:
    result = await workflow_runner.run(failing_workflow(on_failure="recover"))
    assert result.node("recover").status == "completed"

@pytest.mark.asyncio
async def test_stale_workflow_lease_cannot_dispatch_node(workflow_workers) -> None:
    stale, current = await workflow_workers.rotate_lease("wfr_1")
    await current.advance_once("wfr_1")
    with pytest.raises(WorkflowLeaseLostError):
        await stale.advance_once("wfr_1")
    assert workflow_workers.external_effect_count == 1
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/workflow/test_node_lifecycle.py tests/integration/workflow/test_workflow_leases.py -v`

Expected: durable attempts/retry/failure routing are incomplete.

- [ ] **Step 3: Implement state transitions with operation ids**

```python
async def execute_node(self, run: WorkflowRun, node: Node) -> NodeResult:
    attempt = await self._state.claim_attempt(run.id, node.id)
    await self._events.append(node_started(run, node, attempt))
    try:
        result = await self._dispatch(node, operation_id=attempt.operation_id)
    except AgentSDKError as error:
        return await self._handle_failure(run, node, attempt, error)
    await self._state.complete_attempt(attempt, result)
    await self._events.append(node_completed(run, node, attempt, result))
    return result
```

`claim_attempt` verifies the current Workflow lease generation in the same
transaction as the node-attempt projection. Compute exponential backoff plus
bounded jitter from persisted attempt number; persist next-attempt time before
releasing the worker.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/integration/workflow/test_node_lifecycle.py tests/integration/workflow/test_workflow_leases.py -v`

Expected: all node handlers, timeout, retry/backoff, failure edges, and quality-gate failures pass.

```powershell
git add src/agent_sdk/workflow src/agent_sdk/storage/sqlite.py tests/integration/workflow/test_node_lifecycle.py
git commit -m "feat: add durable workflow node lifecycle"
```

---

### Task 2: Implement bounded parallelism, foreach, waits, and restart

**Files:**
- Modify: `src/agent_sdk/workflow/scheduler.py`
- Modify: `src/agent_sdk/workflow/executor.py`
- Modify: `src/agent_sdk/runtime/reconciliation.py`
- Create: `tests/integration/workflow/test_parallel_waits.py`
- Create: `tests/faults/test_workflow_restart.py`

- [ ] **Step 1: Write failing concurrency and crash tests**

```python
@pytest.mark.asyncio
async def test_parallel_never_exceeds_limit(workflow_runner, concurrency_probe) -> None:
    await workflow_runner.run(parallel_workflow(20), max_parallel_nodes=3)
    assert concurrency_probe.maximum <= 3

@pytest.mark.asyncio
async def test_restart_resumes_approval_without_replaying_completed_tool(crash_fixture) -> None:
    run_id = await crash_fixture.stop_at_approval_after_tool()
    result = await crash_fixture.restart_and_approve(run_id)
    assert result.status == "completed"
    assert crash_fixture.tool_call_count == 1
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/workflow/test_parallel_waits.py tests/faults/test_workflow_restart.py -v`

Expected: bounded branches, durable waits, and reconciliation are incomplete.

- [ ] **Step 3: Implement ready-queue scheduling and deterministic joins**

```python
async def run_ready_nodes(self, run_id: str) -> None:
    semaphore = asyncio.Semaphore(self._limits.max_parallel_nodes)
    ready = await self._state.list_ready(run_id)
    async with asyncio.TaskGroup() as group:
        for node in sorted(ready, key=lambda item: (item.priority, item.id)):
            group.create_task(self._run_with_slot(semaphore, run_id, node))
```

Foreach persists the expanded item key/index and caps fan-out. Joins support `all`, `all_successful`, and `any`; output combination is stable by branch id/item index.

- [ ] **Step 4: Implement durable approval/input waits and reconciliation**

Wait nodes persist prompt/schema/deadline and expose response commands with idempotency keys. On startup, reconcile started attempts: completed operations are projected, safe idempotent operations may retry, and unsafe ambiguous operations become `unknown_outcome` for explicit resolution.

```python
async def enter_wait(self, run_id: str, node: ApprovalNode | InputNode) -> None:
    await self._state.persist_wait(run_id, node.id, schema=node.response_schema, deadline=node.deadline)
    await self._leases.release_for_run(run_id)

async def respond(self, wait_id: str, value: Any, *, idempotency_key: str) -> None:
    await self._commands.resolve_wait(wait_id, value, idempotency_key=idempotency_key)
    await self._scheduler.enqueue_wait_owner(wait_id)
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/integration/workflow/test_parallel_waits.py tests/faults/test_workflow_restart.py -v`

Expected: concurrency, joins, foreach, waits, duplicate responses, process restart, and no-double-effect assertions pass.

```powershell
git add src/agent_sdk/workflow src/agent_sdk/runtime/reconciliation.py tests/integration/workflow tests/faults/test_workflow_restart.py
git commit -m "feat: complete durable workflow scheduling"
```
