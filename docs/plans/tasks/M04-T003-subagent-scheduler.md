# M04-T003 Subagent Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run child agents as observable, budgeted Child Runs with isolated context and constrained capabilities.

**Architecture:** A parent creates an immutable `TaskEnvelope`; the scheduler derives a child configuration by intersecting permissions and budgets, queues it fairly, and returns a structured `ChildResult`. Parent-child coordination uses durable messages and state—not shared hidden prompts.

**Tech Stack:** asyncio, SQLite, Pydantic v2, pytest-asyncio.

## Global Constraints

- A child can only lose permissions relative to its parent unless the application explicitly authorizes an escalation flow.
- Depth, total children, active children, token, cost, time, and tool-call limits are enforced before and during execution.
- Child progress is visible through the same event/projection APIs as root Runs.
- Parent cancellation cascades unless the child was explicitly detached by policy.

---

### Task 1: Implement task envelope, derived context, and result contract

**Files:**
- Modify: `src/agent_sdk/subagents/models.py`
- Modify: `src/agent_sdk/subagents/service.py`
- Modify: `src/agent_sdk/context/planner.py`
- Create: `tests/integration/subagents/test_context_isolation.py`

- [ ] **Step 1: Write failing isolation tests**

```python
@pytest.mark.asyncio
async def test_child_receives_envelope_not_parent_private_context(subagents, parent_run) -> None:
    child = await subagents.spawn(parent_run, TaskEnvelope(objective="inspect tests", evidence_refs=[]))
    captured = await child.debug_context_manifest()
    assert "parent-private-thought" not in captured.text
    assert captured.task_envelope.objective == "inspect tests"

@pytest.mark.asyncio
async def test_child_result_is_structured(subagents, parent_run) -> None:
    result = await (await subagents.spawn(parent_run, envelope())).result()
    assert result.model_dump().keys() >= {"status", "summary", "outputs", "evidence_refs", "usage"}
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/subagents/test_context_isolation.py -v`

Expected: complete envelope/context/result contracts are missing.

- [ ] **Step 3: Implement immutable contracts and derived views**

```python
class TaskEnvelope(BaseModel, frozen=True):
    objective: str
    instructions: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    expected_output_schema: dict[str, Any] | None = None
    evidence_refs: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] | None = None
    workspace_scopes: tuple[str, ...] = ()
    budget: RunBudget = RunBudget()

class ChildResult(BaseModel, frozen=True):
    run_id: str
    status: RunStatus
    summary: str
    outputs: dict[str, Any]
    evidence_refs: tuple[str, ...]
    usage: UsageSummary
    failure: FailureRecord | None = None
```

Build the child Context View from the envelope, selected shared evidence, applicable system/profile layers, and the child's own history. Store envelope/result artifacts in the Session evidence tree.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/integration/subagents/test_context_isolation.py -v`

Expected: isolation, evidence access, prompt provenance, structured output, and raw-ledger ownership pass.

```powershell
git add src/agent_sdk/subagents src/agent_sdk/context/planner.py tests/integration/subagents/test_context_isolation.py
git commit -m "feat: isolate subagent context and results"
```

---

### Task 2: Enforce permission/budget limits and fair scheduling

**Files:**
- Modify: `src/agent_sdk/subagents/scheduler.py`
- Modify: `src/agent_sdk/subagents/limits.py`
- Modify: `src/agent_sdk/permissions/policy.py`
- Create: `tests/integration/subagents/test_limits.py`
- Create: `tests/property/test_permission_intersection.py`

- [ ] **Step 1: Write failing bounds tests**

```python
@given(parent=policies(), requested=policies())
def test_child_policy_never_exceeds_parent(parent, requested) -> None:
    child = intersect_policy(parent, requested)
    assert capabilities(child) <= capabilities(parent)

@pytest.mark.asyncio
async def test_scheduler_enforces_depth_and_concurrency(subagents, concurrency_probe) -> None:
    results = await subagents.run_tree(depth=5, width=10, limits=SubagentLimits(max_depth=2, max_active=3))
    assert concurrency_probe.maximum <= 3
    assert results.denied_depth_count > 0
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/subagents/test_limits.py tests/property/test_permission_intersection.py -v`

Expected: intersections, budget checks, and fair queues are incomplete.

- [ ] **Step 3: Implement limits and weighted fair queue**

```python
async def spawn(self, parent: RunSnapshot, envelope: TaskEnvelope) -> ChildHandle:
    self._limits.check_spawn(parent, envelope)
    config = ChildRunConfig(
        permission_policy=intersect_policy(parent.permission_policy, envelope.requested_policy()),
        budget=parent.remaining_budget.intersect(envelope.budget),
        depth=parent.depth + 1,
    )
    child = await self._runs.create_child(parent, envelope, config)
    await self._queue.put(FairQueueItem(session_id=parent.session_id, child_run_id=child.id))
    return ChildHandle(child.id, self)
```

Round-robin across Sessions, FIFO within equal priority, reserve slots for root progress, and stop dispatch when any durable budget boundary is reached.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/integration/subagents/test_limits.py tests/property/test_permission_intersection.py -v`

Expected: permission monotonicity, all budgets, depth/count/concurrency, fairness, and queue cancellation pass.

```powershell
git add src/agent_sdk/subagents src/agent_sdk/permissions/policy.py tests/integration/subagents tests/property/test_permission_intersection.py
git commit -m "feat: enforce subagent scheduling limits"
```

---

### Task 3: Add progress, messages, waits, cancellation, and detach

**Files:**
- Modify: `src/agent_sdk/subagents/handles.py`
- Modify: `src/agent_sdk/subagents/messages.py`
- Modify: `src/agent_sdk/subagents/service.py`
- Create: `tests/integration/subagents/test_coordination.py`

- [ ] **Step 1: Write failing coordination tests**

```python
@pytest.mark.asyncio
async def test_parent_can_watch_and_message_child(subagents, child_fixture) -> None:
    child = await child_fixture.start()
    updates = child.updates(after_cursor=0)
    await child.send({"kind": "clarification", "text": "focus on sqlite"})
    assert (await anext(updates)).child_run_id == child.id

@pytest.mark.asyncio
async def test_wait_cycle_is_rejected(subagents) -> None:
    parent, child = await subagents.parent_child_pair()
    await parent.wait_for(child.id)
    with pytest.raises(WaitCycleError):
        await child.wait_for(parent.id)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/subagents/test_coordination.py -v`

Expected: durable coordination APIs are incomplete.

- [ ] **Step 3: Implement messages and acyclic waits**

Persist typed messages with sender/recipient/sequence/idempotency key. Maintain a wait-for projection and reject any new edge that reaches its source. Expose child tree/status/usage/latest stage and cursor-resumable updates.

```python
async def add_wait(self, waiter_id: str, target_id: str) -> None:
    graph = await self._state.wait_graph()
    if graph.reachable(target_id, waiter_id):
        raise WaitCycleError(waiter_id, target_id)
    await self._state.persist_wait_edge(waiter_id, target_id)

async def send(self, message: ChildMessage) -> None:
    await self._state.append_message_once(message.idempotency_key, message)
```

- [ ] **Step 4: Implement cancellation and policy-controlled detach**

Cancellation cascades breadth-first and records desired state before signalling. Detach requires an explicit policy allowance, transfers budget ownership to the Session, and leaves a durable relationship visible to monitoring.

```python
async def cancel_tree(self, root_run_id: str) -> None:
    queue = deque([root_run_id])
    while queue:
        run_id = queue.popleft()
        await self._commands.set_desired_state(run_id, DesiredRunState.CANCELLED)
        queue.extend(await self._state.attached_child_ids(run_id))

async def detach(self, child_run_id: str) -> None:
    self._policy.require_detach_allowed(child_run_id)
    await self._state.transfer_budget_to_session(child_run_id)
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/integration/subagents/test_coordination.py -v`

Expected: progress, messages, waits, cycle rejection, cascade, detach, restart, and Session deletion cleanup pass.

```powershell
git add src/agent_sdk/subagents tests/integration/subagents/test_coordination.py
git commit -m "feat: add subagent coordination controls"
```
