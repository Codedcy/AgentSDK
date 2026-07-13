# M01-T002 Event and Storage Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define versioned events and atomic event/snapshot commit contracts with an in-memory reference store.

**Architecture:** Runtime writes `CommitBatch`; stores assign global cursors and keep per-Run sequence ordering. InMemoryStore becomes the executable contract oracle for SQLite.

**Tech Stack:** Pydantic v2, asyncio.Lock, typing Protocol, pytest-asyncio.

## Global Constraints

- Event payloads have explicit schema versions.
- A batch either appends all events and snapshots or changes nothing.
- Cursor delivery is at least once; event ids enable deduplication.

---

### Task 1: Add contracts and InMemoryStore

**Files:**
- Create: `src/agent_sdk/events/models.py`
- Create: `src/agent_sdk/storage/base.py`
- Create: `src/agent_sdk/storage/memory.py`
- Create: `tests/contract/test_memory_store_contract.py`

**Interfaces:**
- Produces: `EventEnvelope`, `SnapshotWrite`, `CommitBatch`, `CommitResult`, `StateStore.commit`, `StateStore.read_events`, `StateStore.get_snapshot`, `StateStore.delete_session`.
- Consumes: `new_id` from M01-T001.

- [ ] **Step 1: Write the atomicity and cursor tests**

```python
import pytest
from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore

@pytest.mark.asyncio
async def test_commit_assigns_cursor_and_snapshot_atomically() -> None:
    store = InMemoryStore()
    event = EventEnvelope.new(type="run.created", session_id="ses_1", run_id="run_1", sequence=1, payload={})
    result = await store.commit(CommitBatch(events=(event,), snapshots=(SnapshotWrite("run", "run_1", 1, {"status": "created"}),)))
    assert result.last_cursor == 1
    assert (await store.get_snapshot("run", "run_1"))["status"] == "created"
    assert [item.cursor for item in await store.read_events(after_cursor=0)] == [1]

@pytest.mark.asyncio
async def test_delete_session_removes_events_and_snapshots() -> None:
    store = InMemoryStore()
    event = EventEnvelope.new(type="session.created", session_id="ses_1", run_id=None, sequence=1, payload={})
    await store.commit(CommitBatch(events=(event,), snapshots=(SnapshotWrite("session", "ses_1", 1, {"session_id": "ses_1"}),)))
    await store.delete_session("ses_1")
    assert await store.read_events(after_cursor=0) == []
    assert await store.get_snapshot("session", "ses_1") is None
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/contract/test_memory_store_contract.py -v`

Expected: import failure for `agent_sdk.events.models`.

- [ ] **Step 3: Implement immutable event and commit models**

```python
class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_id: str
    schema_version: int = 1
    type: str
    session_id: str
    run_id: str | None
    sequence: int
    payload: dict[str, Any]
    occurred_at: datetime
    @classmethod
    def new(cls, **values: Any) -> "EventEnvelope":
        return cls(event_id=new_id("evt"), occurred_at=datetime.now(UTC), **values)

class SnapshotWrite(NamedTuple):
    kind: str; entity_id: str; version: int; data: dict[str, Any]
class CommitBatch(NamedTuple):
    events: tuple[EventEnvelope, ...]; snapshots: tuple[SnapshotWrite, ...] = ()
class CommitResult(NamedTuple):
    last_cursor: int
```

- [ ] **Step 4: Implement the Protocol and in-memory transaction**

```python
class StateStore(Protocol):
    async def commit(self, batch: CommitBatch) -> CommitResult: ...
    async def read_events(self, *, after_cursor: int, session_id: str | None = None) -> list[StoredEvent]: ...
    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None: ...
    async def delete_session(self, session_id: str) -> None: ...
```

Use one `asyncio.Lock`; validate duplicate event ids and monotonically increasing Run sequence before mutating copied dictionaries, then swap the copies into live state.

- [ ] **Step 5: Run contract and quality checks**

Run: `uv run pytest tests/contract/test_memory_store_contract.py -v && uv run mypy src && uv run ruff check src tests`

Expected: two tests pass; mypy/Ruff exit 0.

- [ ] **Step 6: Commit**

```powershell
git add src/agent_sdk/events src/agent_sdk/storage tests/contract/test_memory_store_contract.py
git commit -m "feat: add event and storage contracts"
```
