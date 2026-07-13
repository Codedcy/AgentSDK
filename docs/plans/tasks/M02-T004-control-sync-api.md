# M02-T004 Cancellation, Pause/Resume, and Sync API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Run control commands and a safe synchronous convenience facade.

**Architecture:** Control commands persist desired state and signal active tasks; RunEngine observes cancellation/pause at safe boundaries. Sync methods execute the same async public APIs in a dedicated runner and reject nested same-thread loops.

**Tech Stack:** asyncio.Event/TaskGroup, threading, pytest-asyncio.

## Global Constraints

- Cancel is durable before signal delivery.
- Pause completes only at a safe boundary.
- Sync and async paths produce equivalent events/results.

---

### Task 1: Implement controls and sync facade

**Files:**
- Modify: `src/agent_sdk/runtime/commands.py`
- Modify: `src/agent_sdk/runtime/engine.py`
- Modify: `src/agent_sdk/runtime/handles.py`
- Modify: `src/agent_sdk/tools/executor.py`
- Modify: `src/agent_sdk/api.py`
- Create: `tests/integration/runtime/test_run_controls.py`
- Create: `tests/unit/test_sync_api.py`

**Interfaces:**
- Produces: `RunHandle.pause/resume/cancel`, `ToolContext.cancelled`, `AgentSDK.run_sync`, `RunStatus.PAUSED/CANCELLED`.
- Consumes: RuntimeCommands, lease and execution task registry.

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

- [ ] **Step 5: Implement sync wrapper**

```python
def run_sync(self, agent: AgentSpec, input: str) -> RunResult:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(self.run(agent, input))
    raise AgentSDKError(ErrorCode.INVALID_STATE, "run_sync cannot run inside an active event loop", retryable=False)
```

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/runtime/test_run_controls.py tests/unit/test_sync_api.py -v`

Expected: cancellation propagates, pause/resume is durable, sync returns equivalent result and rejects nested loop use.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/runtime src/agent_sdk/tools src/agent_sdk/api.py tests/integration/runtime tests/unit/test_sync_api.py
git commit -m "feat: add run controls and sync facade"
```
