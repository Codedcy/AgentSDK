# M02-T004 Cancellation, Pause/Resume, and Sync API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Run/Workflow control commands, force Session close/delete, and a safe synchronous convenience facade.

**Architecture:** Control commands persist desired state before signaling active tasks; RunEngine and the M01 Workflow executor observe cancellation/pause at safe boundaries and use the M02-T001 lifecycle-final coordinator. A Store-backed cancellation coordinator can settle safe abandoned states without a local Python task and returns bounded recovery-required conflicts for unknown outcomes. Force delete requires explicit data-loss confirmation before durably abandoning such outcomes. Sync calls share one persistent owner event loop/thread for the SDK lifetime; they never create a new loop per call.

**Tech Stack:** asyncio.Event/TaskGroup, threading, pytest-asyncio.

## Global Constraints

- Cancel is durable before signal delivery.
- Pause completes only at a safe boundary.
- Sync and async paths produce equivalent events/results.
- `RunStatus.CANCELLED` is lifecycle-final and detaches from Session in the same commit; interrupted, reconciliation, paused, and waiting states do not detach.
- `close(force=True)` persistently requests cancellation for every owned
  Run/Workflow and returns only after closed, except unknown external outcomes
  return a bounded retryable recovery-required conflict with ownership intact.
- `delete(force=True, confirm_data_loss=True)` is the only force-delete form; omitting explicit confirmation fails before cancellation or deletion.
- Force deletion may abandon reconciliation evidence only after confirmation and records that decision before Session cleanup.
- `RunHandle.result()` and `WorkflowHandle.result()` raise a stable public
  `AgentSDKError(ErrorCode.CANCELLED, "... cancelled", retryable=False)` for
  cancelled terminal snapshots; cancellation is inspected through `get`, not a
  synthetic successful result.
- One SDK instance binds to one async execution domain. Repeated/concurrent sync
  calls share its persistent runner; a previously async-bound SDK rejects
  `run_sync`, and sync-owned resources are closed before the runner stops.

---

### Task 1: Implement controls and sync facade

**Files:**
- Modify: `src/agent_sdk/runtime/commands.py`
- Create: `src/agent_sdk/runtime/cancellation.py`
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/errors.py`
- Modify: `src/agent_sdk/runtime/engine.py`
- Modify: `src/agent_sdk/runtime/handles.py`
- Modify: `src/agent_sdk/runtime/session_lifecycle.py`
- Modify: `src/agent_sdk/tools/executor.py`
- Modify: `src/agent_sdk/workflow/executor.py`
- Modify: `src/agent_sdk/workflow/state.py`
- Modify: `src/agent_sdk/workflow/models.py`
- Modify: `src/agent_sdk/workflow/handles.py`
- Modify: `src/agent_sdk/api.py`
- Create: `src/agent_sdk/sync.py`
- Create: `tests/integration/runtime/test_run_controls.py`
- Create: `tests/integration/runtime/test_force_session_lifecycle.py`
- Create: `tests/integration/workflow/test_workflow_cancellation.py`
- Create: `tests/unit/test_sync_api.py`

**Interfaces:**
- Produces: `RunHandle.pause/resume/cancel`, durable Workflow cancellation, `SessionAPI.close(force=True)`, `SessionAPI.delete(force=True, confirm_data_loss=True)`, `ToolContext.cancelled`, persistent `AgentSDK.run_sync/close_sync`, `ErrorCode.CANCELLED`, `RunStatus.PAUSED/CANCELLED`, and `WorkflowRunStatus/WorkflowNodeStatus.CANCELLED`.
- Consumes: RuntimeCommands, lease and execution task registry, M02-T001 Session ownership/final-detach coordinator, M02-T002 reconciliation state.

- [ ] **Step 1: Write cancel/pause/sync equivalence tests**

```python
@pytest.mark.asyncio
async def test_cancel_reaches_running_tool(sdk, cancellable_tool) -> None:
    run = await sdk.fixtures.run_tool(cancellable_tool)
    await cancellable_tool.started.wait()
    await run.cancel()
    with pytest.raises(AgentSDKError) as raised:
        await run.result()
    assert raised.value.code is ErrorCode.CANCELLED
    assert (await sdk.runs.get(run.run_id)).status == "cancelled"
    assert cancellable_tool.saw_cancel is True

def test_sync_and_async_results_match(sync_fixture) -> None:
    assert sync_fixture.sdk.run_sync(sync_fixture.agent, "hello").output_text == "hello"

def test_repeated_and_concurrent_sync_calls_share_one_runner(sync_fixture) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(sync_fixture.run_once, range(24)))
    assert len(results) == 24
    assert sync_fixture.runner_thread_ids == {sync_fixture.sdk.sync_thread_id}
    sync_fixture.sdk.close_sync()
    with pytest.raises(AgentSDKError, match="closed"):
        sync_fixture.sdk.run_sync(sync_fixture.agent, "late")

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

- [ ] **Step 3: Define cancelled state and persist control commands**

Add `ErrorCode.CANCELLED`, `RunStatus.CANCELLED`,
`WorkflowRunStatus.CANCELLED`, and `WorkflowNodeStatus.CANCELLED`. A cancelled
Run snapshot is lifecycle-final, contains sanitized `RunFailure(code="cancelled")`,
partial output/usage and completed Tool results, and has no resumable desired
state. `RunResult` remains the successful-completion type and gains no `status`.
Run handles reconstruct completed results, raise the durable failure for failed
Runs, and raise public `CANCELLED` for cancelled Runs.

A cancelled Workflow has no output/usage, carries a cancelled failure, and its
nodes are a legal completed prefix followed by either one cancelled formerly
running node and pending suffix, or an all-pending suffix when cancelled before
node dispatch. A terminal `WorkflowResult` is returned only for completed
Workflows; failed/cancelled handles raise their respective stable errors.
Override/reuse validated copy constructors and add invariant tests for all new
states, including terminal Session detach in the same commit.

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
local tasks, and drives a Store-backed cancellation coordinator with a bounded,
configurable deadline:

- a local active task observes the request at its next safe boundary;
- `created`, safely paused, permission/input waiting, or interrupted work with
  no unresolved external operation is claimed under exact snapshot/lease CAS
  and directly committed `cancelled` plus Session detach;
- an expired local task is taken over only after lease fencing and the M02-T002
  checkpoint/external-operation scan;
- `waiting_reconciliation` or any unknown model/Tool outcome is never silently
  abandoned by force close. Return `SessionBusyError`/retryable `CONFLICT`
  (`recovery required`) within the deadline, leaving the Session `closing` and
  ownership/evidence intact.

Each safe Run or Workflow cancellation commits its own `cancelled` outcome plus
Session detach; the final detach writes `session.closed`. Repeating force close
resumes/attaches to the same desired states and never emits duplicate cancel
outcomes. No path polls forever.

`sessions.delete(force=True, confirm_data_loss=False)` returns `CONFLICT` before
any mutation. With confirmation, the Store-backed coordinator may settle
unknown/reconciliation work: first persist actor/reason/evidence and
`reconciliation.abandoned`/data-loss decisions, then in the same exact-CAS
transaction commit the Run/Workflow cancelled terminal snapshot and Session
detach. Only after all ownership is released does it call the same normal
`closed -> deleting -> removed` path from M02-T001. If a crash occurs before
cleanup, abandonment evidence remains durable and retry resumes; normal Session
cleanup ultimately removes Session-owned evidence by the documented retention
contract. Cancellation/failure between every phase is resumable by repeating
the command; workspace files remain untouched.

Test local-running, abandoned-created, paused, permission-waiting, interrupted
safe-checkpoint, active/expired lease, waiting-reconciliation, and unknown
model/Tool outcome cases. Force close must either complete or return bounded
recovery-required; confirmed force delete must record abandonment before
terminal detach, survive a crash/retry between phases, and then remove the
Session.

- [ ] **Step 6: Implement sync wrapper**

```python
def run_sync(self, agent: AgentSpec, input: str) -> RunResult:
    runner = self._execution_domain.bind_sync_or_raise()
    return runner.call(lambda: self.run(agent, input))

def close_sync(self) -> None:
    self._sync_runner.close(lambda: self._close_on_owner_loop())
```

`_SyncRunner` starts one hidden thread and one event loop lazily, submits
coroutine factories with `asyncio.run_coroutine_threadsafe`, propagates results
and sanitized exceptions, and serializes start/stop with a thread lock. It
keeps the loop alive across calls so `_LazySQLiteStore._open_task`, asyncio
locks/events, active tasks, leases, and MCP connections stay on one owner loop.
`close_sync` first schedules/awaits normal SDK async close on that loop, then
stops the loop and joins the thread; it is idempotent and rejects calls after
close. The sync runner thread cannot call `run_sync` recursively.

The shared execution-domain guard records the first async loop that uses an SDK.
If already bound to a user async loop, `run_sync` returns `INVALID_STATE`; if
bound to the sync runner, direct public async use from another loop returns
`INVALID_STATE` rather than touching cross-loop resources. Add repeated calls,
24 calls from concurrent threads, provider/store exceptions, `close_sync`
during/after work, double close, call-after-close, async-then-sync rejection,
and recursive runner-thread rejection tests. Never use per-call `asyncio.run`.

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
