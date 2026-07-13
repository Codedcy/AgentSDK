# M05-T001 Event Projections and Subscriptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide complete, rebuildable monitoring projections and cursor-resumable event subscriptions for Runs, workflows, child agents, and usage.

**Architecture:** Versioned immutable events are the source of truth. Idempotent projectors consume the global cursor into query tables; subscriptions use at-least-once delivery and tolerate cursor gaps caused by Session deletion.

**Tech Stack:** SQLite, asyncio, Pydantic v2, pytest-asyncio, Hypothesis.

## Global Constraints

- Every event has schema version, global cursor, aggregate sequence, timestamps, correlation/causation ids, and ownership ids.
- Upcasters are pure and do not rewrite historical rows.
- Projection rebuild produces the same observable state as incremental processing.
- Cursor gaps are legal; cursor reuse is forbidden.

---

### Task 1: Complete event envelope, upcasters, and projectors

**Files:**
- Modify: `src/agent_sdk/events/models.py`
- Modify: `src/agent_sdk/events/upcast.py`
- Modify: `src/agent_sdk/observability/projections.py`
- Modify: `src/agent_sdk/storage/sqlite.py`
- Create: `tests/unit/observability/test_upcasters.py`
- Create: `tests/integration/observability/test_projection_rebuild.py`

- [ ] **Step 1: Write failing envelope/rebuild tests**

```python
def test_upcaster_chain_preserves_event_identity() -> None:
    upgraded = upcasters.upcast(event_v1("tool.completed"))
    assert upgraded.schema_version == CURRENT_EVENT_VERSION
    assert upgraded.event_id == "event-1"
    assert upgraded.cursor == 7

@pytest.mark.asyncio
async def test_rebuild_matches_incremental_projection(observability_fixture) -> None:
    incremental = await observability_fixture.snapshot()
    await observability_fixture.drop_and_rebuild_projections()
    assert await observability_fixture.snapshot() == incremental
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/observability/test_upcasters.py tests/integration/observability/test_projection_rebuild.py -v`

Expected: complete envelope/upcasters/projectors are missing.

- [ ] **Step 3: Implement versioned event envelope and registry**

```python
class EventEnvelope(BaseModel, frozen=True):
    event_id: str
    type: str
    schema_version: int
    cursor: int
    aggregate_id: str
    sequence: int
    occurred_at: datetime
    recorded_at: datetime
    session_id: str
    run_id: str | None = None
    correlation_id: str
    causation_id: str | None = None
    payload: dict[str, Any]
```

Register explicit event type/version decoders. Upcast one version per pure function and fail with a typed compatibility error when no chain exists.

- [ ] **Step 4: Implement core projections**

Build Run snapshot/tree, timeline, tool calls, workflow nodes/attempts, child progress, token/cost usage, permissions/waits, and evaluation summaries. Each projector transaction applies only cursors greater than its checkpoint and records the new checkpoint atomically.

```python
async def project_batch(self, projector: Projector, limit: int = 500) -> int:
    async with self._store.immediate_transaction() as transaction:
        checkpoint = await transaction.projector_checkpoint(projector.name)
        events = await transaction.events_after(checkpoint, limit=limit)
        for event in events:
            await projector.apply(transaction, self._upcasters.upcast(event))
        next_cursor = events[-1].cursor if events else checkpoint
        await transaction.set_projector_checkpoint(projector.name, next_cursor)
        return next_cursor
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/unit/observability/test_upcasters.py tests/integration/observability/test_projection_rebuild.py -v`

Expected: incremental/rebuild parity, duplicate delivery, version fixtures, and partial-batch rollback pass.

```powershell
git add src/agent_sdk/observability src/agent_sdk/storage/sqlite.py tests/unit/observability tests/integration/observability/test_projection_rebuild.py
git commit -m "feat: complete event projections"
```

---

### Task 2: Implement query snapshots and resumable subscriptions

**Files:**
- Modify: `src/agent_sdk/observability/queries.py`
- Modify: `src/agent_sdk/observability/subscriptions.py`
- Modify: `src/agent_sdk/api.py`
- Create: `tests/integration/observability/test_subscriptions.py`
- Create: `tests/property/test_cursor_delivery.py`

- [ ] **Step 1: Write failing cursor tests**

```python
@pytest.mark.asyncio
async def test_subscription_resumes_after_last_acknowledged_cursor(events) -> None:
    first = events.subscribe(after_cursor=0, buffer_size=2)
    received = [await anext(first), await anext(first)]
    resumed = events.subscribe(after_cursor=received[-1].cursor, buffer_size=2)
    assert (await anext(resumed)).cursor > received[-1].cursor

@pytest.mark.asyncio
async def test_deleted_session_creates_gap_not_cursor_reuse(events, deleted_session) -> None:
    before = await events.high_watermark()
    await deleted_session.delete()
    await events.append(new_session_event())
    assert await events.high_watermark() > before
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/observability/test_subscriptions.py tests/property/test_cursor_delivery.py -v`

Expected: complete resumable delivery/backpressure behavior is missing.

- [ ] **Step 3: Implement snapshot queries and at-least-once subscription**

```python
async def subscribe(self, *, after_cursor: int, buffer_size: int = 256) -> AsyncIterator[EventEnvelope]:
    cursor = after_cursor
    while not self._closed:
        batch = await self._store.events_after(cursor, limit=buffer_size)
        if not batch:
            await self._notifier.wait_after(cursor)
            continue
        for event in batch:
            yield event
            cursor = event.cursor
```

Expose `get_run_snapshot`, `get_run_tree`, `get_timeline`, `get_workflow_state`, and `get_usage`. Bound subscriber buffers; configurable policy blocks the producer notification path or disconnects slow subscribers with a resumable cursor—never drops silently.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/integration/observability/test_subscriptions.py tests/property/test_cursor_delivery.py -v`

Expected: resume, duplicates, gaps, concurrent producers, slow consumers, close, and restart pass.

```powershell
git add src/agent_sdk/observability src/agent_sdk/api.py tests/integration/observability/test_subscriptions.py tests/property/test_cursor_delivery.py
git commit -m "feat: add resumable monitoring queries"
```
