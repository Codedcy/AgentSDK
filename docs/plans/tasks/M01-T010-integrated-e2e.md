# M01-T010 Integrated Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the entire thin slice works through public APIs, process restart, and Session deletion.

**Architecture:** A scripted LiteLLM/MCP fixture drives a deterministic scenario. The reference CLI is a thin event consumer that resolves permission/workflow requests; the E2E test reopens the same SQLite file between phases.

**Tech Stack:** Typer optional extra, pytest subprocess/reopen fixtures, installed `agent_sdk` package.

## Global Constraints

- The example imports public APIs only.
- The test verifies data before and after reopening SQLite.
- Session deletion leaves no events, snapshots, evaluations, or analytics contribution.

---

### Task 1: Build the vertical scenario and reference CLI

**Files:**
- Create: `examples/reference_cli/__init__.py`
- Create: `examples/reference_cli/main.py`
- Create: `tests/e2e/test_vertical_slice.py`
- Create: `tests/fixtures/mcp_server.py`
- Create: `tests/fixtures/skills/coding-demo/SKILL.md`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Produces: `python -m examples.reference_cli.main`, one end-to-end acceptance test.
- Consumes: all M01 public APIs.

- [ ] **Step 1: Write the failing acceptance test**

```python
@pytest.mark.asyncio
async def test_complete_vertical_slice_survives_restart_and_delete(tmp_path: Path) -> None:
    db = tmp_path / "sdk.db"
    phase1 = await run_scenario_until_wait(db)
    assert phase1.run.status == "waiting_permission"
    await phase1.sdk.close()
    phase2 = await reopen_and_finish(db, phase1.ids)
    assert phase2.result.status == "completed"
    assert phase2.result.child_results
    assert phase2.context_capsules == 1
    assert phase2.evaluations[0].verdict == "pass"
    assert phase2.analytics.success_rate == 1.0
    await phase2.sdk.sessions.close(phase2.session_id, force=True)
    await phase2.sdk.sessions.delete(phase2.session_id)
    assert await phase2.sdk.events.query(session_id=phase2.session_id) == []
    assert await phase2.sdk.analytics.session_contribution(phase2.session_id) is None
```

- [ ] **Step 2: Run and inspect the first real integration failures**

Run: `uv run pytest tests/e2e/test_vertical_slice.py -v`

Expected: fails because scenario helpers/remaining public façade wiring are absent; no xfail is allowed.

- [ ] **Step 3: Implement deterministic fixtures and scenario helpers**

Script model turns: request permissioned Tool, consume its result, generate Workflow, spawn Child, return final answer. MCP fixture exposes `echo`; Skill fixture supplies one instruction/reference; context budget forces one Capsule.

```python
def scripted_vertical_slice() -> ScriptedLiteLLM:
    return ScriptedLiteLLM([
        ModelTurn.tool_call("write", {"path": "result.txt", "content": "hello"}),
        ModelTurn.workflow(coding_workflow_document()),
        ModelTurn.child(objective="verify result.txt"),
        ModelTurn.text("completed"),
    ])
```

- [ ] **Step 4: Implement the CLI event loop**

```python
async for event in run.events():
    if event.type == "permission.requested":
        await sdk.permissions.resolve(event.payload["request_id"], PermissionDecision.allow_once())
    elif event.type == "workflow.proposed":
        await sdk.workflows.approve(event.payload["workflow_id"])
```

Print status, ToolCalls, Child progress, usage, Evaluation, and analytics without reading private objects.

- [ ] **Step 5: Close public façade gaps and make E2E pass**

Add only missing delegation methods to AgentSDK APIs; do not duplicate domain logic in examples. Ensure reopen reconstructs pending permission/workflow state from snapshots/events.

- [ ] **Step 6: Run the M01 gate**

Run: `uv run ruff check . && uv run mypy src && uv run pytest tests/unit tests/integration tests/e2e/test_vertical_slice.py -v`

Expected: all checks pass; deletion assertions confirm no local Session facts remain.

- [ ] **Step 7: Commit**

```powershell
git add examples tests/e2e tests/fixtures pyproject.toml uv.lock README.md src/agent_sdk
git commit -m "feat: complete agent sdk vertical slice"
```
