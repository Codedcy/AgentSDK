# M01-T008 Workflow and Child Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute a validated sequential Workflow containing one parent Agent node and one isolated Child Run.

**Architecture:** YAML and Python objects compile into the same minimal WorkflowIR. WorkflowExecutor persists node transitions; SubagentService creates a normal Run from a TaskEnvelope and injects only explicit context references.

**Tech Stack:** Pydantic discriminated unions, PyYAML, RuntimeCommands/RunEngine.

## Global Constraints

- Arbitrary Python/eval is forbidden in DSL.
- Child Context contains TaskEnvelope and explicit refs only.
- Completed NodeRuns are not repeated after reopen.

---

### Task 1: Add sequential IR, executor, and Child Run

**Files:**
- Create: `src/agent_sdk/workflow/models.py`
- Create: `src/agent_sdk/workflow/dsl.py`
- Create: `src/agent_sdk/workflow/compiler.py`
- Create: `src/agent_sdk/workflow/executor.py`
- Create: `src/agent_sdk/subagents/models.py`
- Create: `src/agent_sdk/subagents/service.py`
- Create: `tests/integration/workflow/test_workflow_child_slice.py`

**Interfaces:**
- Produces: `WorkflowDefinition`, `WorkflowIR`, `AgentNode`, `WorkflowCompiler.compile`, `WorkflowExecutor.start`, `TaskEnvelope`, `ChildResult`, `SubagentService.spawn/await_result`.
- Consumes: `AgentSDK`, `StateStore`, `ContextPlanner`, `PolicyEngine`.

- [ ] **Step 1: Write the YAML/Child isolation test**

```python
@pytest.mark.asyncio
async def test_sequential_workflow_runs_isolated_child(sdk: AgentSDK) -> None:
    definition = WorkflowDefinition.model_validate({
        "api_version": "agent-sdk/v1", "name": "parent-child",
        "nodes": [
            {"id": "plan", "kind": "agent", "agent_revision": "planner:1", "input": "plan"},
            {"id": "child", "kind": "agent", "agent_revision": "worker:1", "input": "execute", "run_as": "child"},
        ],
        "edges": [{"source": "plan", "target": "child"}],
    })
    run = await sdk.workflows.start(definition)
    result = await run.result()
    assert result.node_statuses == {"plan": "completed", "child": "completed"}
    child_prompt = await sdk.debug.prompt_manifest(result.child_run_ids[0])
    assert "unrelated-parent-message" not in child_prompt.rendered_preview
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/workflow/test_workflow_child_slice.py -v`

Expected: missing workflow/subagents modules.

- [ ] **Step 3: Implement typed nodes and compiler**

```python
class AgentNode(BaseModel):
    kind: Literal["agent"]; id: str; agent_revision: str; input: str
    run_as: Literal["workflow", "child"] = "workflow"
class WorkflowEdge(BaseModel):
    source: str; target: str; condition: str | None = None
Node = Annotated[AgentNode, Field(discriminator="kind")]
```

Compiler rejects duplicate/missing dependencies and cycles, topologically sorts nodes, and hashes canonical JSON.

- [ ] **Step 4: Implement persistent sequential executor**

Persist `workflow.started`, `workflow.node.started/completed`, and `workflow.completed`. On restart, read NodeRun snapshots and skip completed nodes. For ChildNode call SubagentService.

```python
async def execute(self, workflow_run_id: str, ir: WorkflowIR) -> WorkflowResult:
    completed = await self._state.completed_node_ids(workflow_run_id)
    for node in ir.topological_order():
        if node.id in completed:
            continue
        await self._state.start_node(workflow_run_id, node.id)
        result = await self._subagents.spawn(node.task_envelope()) if node.run_as == "child" else await self._agents.run(node)
        await self._state.complete_node(workflow_run_id, node.id, result)
    return await self._state.complete_workflow(workflow_run_id)
```

- [ ] **Step 5: Implement TaskEnvelope and isolated spawn**

```python
class TaskEnvelope(BaseModel):
    objective: str; success_criteria: tuple[str, ...]; evidence_refs: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = (); workspace_scopes: tuple[str, ...] = ()
class ChildResult(BaseModel):
    run_id: str; status: str; summary: str
    outputs: dict[str, Any] = Field(default_factory=dict); evidence_refs: tuple[str, ...] = ()
```

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/workflow/test_workflow_child_slice.py -v`

Expected: node order is stable, Child completes, and only explicit context appears in its Manifest.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/workflow src/agent_sdk/subagents tests/integration/workflow
git commit -m "feat: add workflow and child slice"
```
