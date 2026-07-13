# M02-T002 Leases, Interruption, and Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent concurrent Run advancement and safely recover work interrupted around external side effects.

**Architecture:** Generation-based leases fence writers. Tool calls persist started/outcome boundaries; stale leases mark Runs interrupted, and unknown non-idempotent calls create durable reconciliation requests.

**Tech Stack:** SQLite timestamps/generations, asyncio heartbeat, fake clock, pytest subprocess fixtures.

## Global Constraints

- Only the current lease generation may commit Run progress.
- A non-idempotent started call without a terminal outcome never auto-retries.
- Reconciliation decisions are immutable audit events.

---

### Task 1: Add leases and reconciliation

**Files:**
- Create: `src/agent_sdk/runtime/leases.py`
- Create: `src/agent_sdk/runtime/reconciliation.py`
- Modify: `src/agent_sdk/runtime/engine.py`
- Modify: `src/agent_sdk/storage/base.py`
- Modify: `src/agent_sdk/storage/sqlite.py`
- Create: `src/agent_sdk/storage/migrations/0003_leases.sql`
- Create: `tests/integration/runtime/test_leases.py`
- Create: `tests/e2e/test_unknown_tool_outcome.py`

**Interfaces:**
- Produces: `Lease`, `LeaseManager.acquire/renew/release`, `ReconciliationRequest`, `ReconciliationService.resolve`, `RunStatus.INTERRUPTED`.
- Consumes: StateStore, RunEngine, Tool idempotency metadata.

- [ ] **Step 1: Write lease fencing and unknown-outcome tests**

```python
@pytest.mark.asyncio
async def test_stale_generation_cannot_commit(lease_manager) -> None:
    first = await lease_manager.acquire("run_1", "worker_a")
    await lease_manager.expire_for_test(first)
    second = await lease_manager.acquire("run_1", "worker_b")
    assert second.generation > first.generation
    with pytest.raises(LeaseLostError):
        await lease_manager.assert_current(first)

@pytest.mark.asyncio
async def test_unknown_non_idempotent_call_waits_for_reconciliation(crash_fixture) -> None:
    run_id = await crash_fixture.crash_after_tool_started(idempotency="non_idempotent")
    recovered = await crash_fixture.reopen(run_id)
    assert recovered.status == "waiting_reconciliation"
    assert crash_fixture.side_effect_count == 1
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/runtime/test_leases.py tests/e2e/test_unknown_tool_outcome.py -v`

Expected: Lease/Reconciliation types missing.

- [ ] **Step 3: Implement lease storage and heartbeat**

Lease table fields: run_id PK, owner, generation, acquired_at, renewed_at, expires_at. Acquire uses `BEGIN IMMEDIATE`, rejects unexpired foreign owner, and increments generation after expiry. Engine includes generation in progress commits.

```python
async def acquire(self, run_id: str, owner: str, now: datetime) -> Lease:
    async with self._store.immediate_transaction() as transaction:
        current = await transaction.get_lease(run_id)
        if current and current.expires_at > now and current.owner != owner:
            raise LeaseHeld(run_id)
        generation = 1 if current is None else current.generation + 1
        return await transaction.put_lease(run_id, owner, generation, now + self._ttl)
```

- [ ] **Step 4: Implement recovery scan**

At SDK open, find nonterminal Runs with stale leases. Mark interrupted; inspect started ToolCalls without outcomes. Retry only `idempotent`/`safe_retry`; otherwise create reconciliation request and waiting state.

```python
async def reconcile_stale_runs(self) -> None:
    for run in await self._store.list_stale_nonterminal_runs(self._clock.now()):
        await self._commands.mark_interrupted(run.id)
        for call in await self._store.unresolved_tool_calls(run.id):
            if call.idempotent or call.safe_retry:
                await self._commands.queue_tool_retry(call.id)
            else:
                await self._commands.request_reconciliation(call.id)
```

- [ ] **Step 5: Implement resolution actions**

```python
class ReconciliationAction(StrEnum):
    CONFIRM_COMPLETED = "confirm_completed"
    CONFIRM_NOT_EXECUTED = "confirm_not_executed"
    RETRY = "retry"
    TERMINATE = "terminate"
```

Require evidence/actor metadata; RETRY remains forbidden unless user explicitly selects it.

- [ ] **Step 6: Verify crash boundary**

Run: `uv run pytest tests/integration/runtime/test_leases.py tests/e2e/test_unknown_tool_outcome.py -v`

Expected: fencing works; side effect count stays one until explicit resolution.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/runtime src/agent_sdk/storage tests/integration/runtime tests/e2e/test_unknown_tool_outcome.py
git commit -m "feat: add run leases and reconciliation"
```
