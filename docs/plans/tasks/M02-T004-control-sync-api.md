# M02-T004 Cancellation, Pause/Resume, and Sync API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Run/Workflow control commands, force Session close/delete, and a safe synchronous convenience facade.

**Architecture:** Control commands persist desired state before signaling active tasks; RunEngine and the M01 Workflow executor observe cancellation/pause at safe boundaries and use the M02-T001 lifecycle-final coordinator. Force Session close is an orchestration command over owned Run/Workflow cancellation; force delete requires explicit data-loss confirmation, waits for lifecycle-final ownership release, then uses normal resumable deletion. Sync methods execute the same async public APIs in a dedicated runner and reject nested same-thread loops.

**Tech Stack:** asyncio.Event/TaskGroup, threading, pytest-asyncio.

## Global Constraints

- Cancel is durable before signal delivery.
- Pause completes only at a safe boundary.
- Sync and async paths produce equivalent events/results.
- `RunStatus.CANCELLED` is lifecycle-final and detaches from Session in the same commit; interrupted, reconciliation, paused, and waiting states do not detach.
- `close(force=True)` persistently cancels every owned Run/Workflow and returns only after the Session is closed.
- `delete(force=True, confirm_data_loss=True)` is the only force-delete form; omitting explicit confirmation fails before cancellation or deletion.
- Force deletion may abandon reconciliation evidence only after confirmation and records that decision before Session cleanup.

---

### Task 1: Implement controls and sync facade

**Files:**
- Modify: `src/agent_sdk/runtime/commands.py`
- Modify: `src/agent_sdk/runtime/engine.py`
- Modify: `src/agent_sdk/runtime/handles.py`
- Modify: `src/agent_sdk/runtime/session_lifecycle.py`
- Modify: `src/agent_sdk/tools/executor.py`
- Modify: `src/agent_sdk/workflow/executor.py`
- Modify: `src/agent_sdk/workflow/state.py`
- Modify: `src/agent_sdk/api.py`
- Create: `tests/integration/runtime/test_run_controls.py`
- Create: `tests/integration/runtime/test_force_session_lifecycle.py`
- Create: `tests/integration/workflow/test_workflow_cancellation.py`
- Create: `tests/unit/test_sync_api.py`

**Interfaces:**
- Produces: `RunHandle.pause/resume/cancel`, durable Workflow cancellation, `SessionAPI.close(force=True)`, `SessionAPI.delete(force=True, confirm_data_loss=True)`, `ToolContext.cancelled`, `AgentSDK.run_sync`, `RunStatus.PAUSED/CANCELLED`.
- Consumes: RuntimeCommands, lease and execution task registry, M02-T001 Session ownership/final-detach coordinator, M02-T002 reconciliation state.

- [ ] **Step 1: Write cancel/pause/sync equivalence tests**

```python
@pytest.mark.asyncio
async def test_cancel_reaches_running_tool(sdk, cancellable_tool) -> None:
    run = await sdk.fixtures.run_tool(cancellable_tool)
    await cancellable_tool.started.wait()
    await run.cancel()
    assert (await run.result()).status == "cancelled"
    assert cancellable_tool.saw_cancel is True

def test_sync_and_async_results_match(sync_fixture) -> None:
    assert sync_fixture.sdk.run_sync(sync_fixture.agent, "hello").output_text == "hello"

@pytest.mark.asyncio
async def test_force_close_cancels_owned_run_and_workflow(force_fixture) -> None:
    session, run, workflow = await force_fixture.start_owned_work()
    closed = await force_fixture.sdk.sessions.close(session.session_id, force=True)
    assert closed.status == "closed"
    assert (await force_fixture.sdk.runs.get(run.run_id)).status == "cancelled"
    assert (await force_fixture.sdk.workflows.get(workflow.workflow_run_id)).status == "cancelled"

@pytest.mark.asyncio
async def test_force_delete_requires_confirmation_before_side_effect(force_fixture) -> None:
    session, run = await force_fixture.start_reconciliation_owned_work()
    with pytest.raises(AgentSDKError, match="data-loss confirmation"):
        await force_fixture.sdk.sessions.delete(session.session_id, force=True)
    assert (await force_fixture.sdk.runs.get(run.run_id)).status == "waiting_reconciliation"
    await force_fixture.sdk.sessions.delete(
        session.session_id, force=True, confirm_data_loss=True
    )
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/runtime/test_run_controls.py tests/unit/test_sync_api.py -v`

Expected: control/sync methods missing.

- [ ] **Step 3: Persist control commands and signal tasks**

Cancel/pause commands append events and update snapshot desired state. ActiveEngineRegistry maps run id to cancellation/pause events. Tools receive a read-only cancellation check; bash gets graceful terminate then configurable kill timeout.

```python
async def cancel(self, run_id: str) -> None:
    await self._commands.set_desired_state(run_id, DesiredRunState.CANCELLED)
    if signal := self._active.cancel_signal(run_id):
        signal.set()

async def pause(self, run_id: str) -> None:
    await self._commands.set_desired_state(run_id, DesiredRunState.PAUSED)
    if signal := self._active.pause_signal(run_id):
        signal.set()
```

- [ ] **Step 4: Implement safe-boundary pause/resume**

Engine checks pause after model/tool terminal commits, releases lease, and waits in paused state. Resume queues a new execution acquisition; no Python stack is required for durable resume.

```python
async def _safe_boundary(self, run_id: str, lease: Lease) -> bool:
    desired = await self._commands.desired_state(run_id)
    if desired is DesiredRunState.PAUSED:
        await self._commands.mark_paused(run_id)
        await self._leases.release(lease)
        return False
    if desired is DesiredRunState.CANCELLED:
        raise asyncio.CancelledError
    return True
```

- [ ] **Step 5: Implement force Session orchestration**

`sessions.close(force=True)` first commits `session.closing`, then writes durable
cancel requests for every `active_run_id` and `active_workflow_run_id`, signals
local tasks, and waits through Store-backed status observation. Each Run or
Workflow cancellation commits its own `cancelled` outcome plus Session detach;
the final detach writes `session.closed`. Repeating force close attaches to the
same desired states and never emits duplicate cancel outcomes.

`sessions.delete(force=True, confirm_data_loss=False)` returns `CONFLICT` before
any mutation. With confirmation, persist a data-loss/abandoned-reconciliation
decision, perform force close, then call the same normal `closed -> deleting ->
removed` path from M02-T001. Cancellation/failure between phases is resumable by
repeating the command; workspace files remain untouched.

- [ ] **Step 6: Implement sync wrapper**

```python
def run_sync(self, agent: AgentSpec, input: str) -> RunResult:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(self.run(agent, input))
    raise AgentSDKError(ErrorCode.INVALID_STATE, "run_sync cannot run inside an active event loop", retryable=False)
```

- [ ] **Step 7: Verify**

Run: `uv run pytest tests/integration/runtime/test_run_controls.py tests/integration/runtime/test_force_session_lifecycle.py tests/integration/workflow/test_workflow_cancellation.py tests/unit/test_sync_api.py -v`

Expected: cancellation propagates, pause/resume is durable, force close/delete
settles every ownership/reconciliation path with explicit confirmation, and sync
returns equivalent results while rejecting nested loop use.

- [ ] **Step 8: Commit**

```powershell
git add src/agent_sdk/runtime src/agent_sdk/tools src/agent_sdk/workflow src/agent_sdk/api.py tests/integration/runtime tests/integration/workflow tests/unit/test_sync_api.py
git commit -m "feat: add run controls and sync facade"
```
