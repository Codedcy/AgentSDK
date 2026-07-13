# M01-T003 SQLite Runtime Spine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist Events, Session snapshots, and Run snapshots in SQLite with one initial migration.

**Architecture:** SQLiteStore implements the M01-T002 contract using WAL and `BEGIN IMMEDIATE`. SessionService writes only events/snapshots and never touches SQL directly.

**Tech Stack:** aiosqlite, JSON, Pydantic, pytest temporary paths.

## Global Constraints

- Event append and snapshot update occur in the same transaction.
- SQLite creates parent directories but does not silently replace an incompatible database.
- Session delete removes its events and snapshots.

---

### Task 1: Add SQLiteStore and minimal Session/Run services

**Files:**
- Create: `src/agent_sdk/storage/migrations/0001_initial.sql`
- Create: `src/agent_sdk/storage/sqlite.py`
- Create: `src/agent_sdk/runtime/models.py`
- Create: `src/agent_sdk/runtime/commands.py`
- Create: `tests/integration/storage/test_sqlite_spine.py`

**Interfaces:**
- Produces: `SQLiteStore.open/close`, `SessionSnapshot`, `RunSnapshot`, `RunStatus`, `RuntimeCommands.create_session/start_run`.
- Consumes: `StateStore`, `CommitBatch`, `EventEnvelope`.

- [ ] **Step 1: Write the persistence/reopen test**

```python
@pytest.mark.asyncio
async def test_session_and_run_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = await SQLiteStore.open(path)
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[tmp_path])
    run = await commands.start_run(session.session_id, agent_revision="agent:1", user_input="hello")
    await store.close()
    reopened = await SQLiteStore.open(path)
    assert (await reopened.get_snapshot("session", session.session_id))["status"] == "active"
    assert (await reopened.get_snapshot("run", run.run_id))["status"] == "created"
    assert len(await reopened.read_events(after_cursor=0, session_id=session.session_id)) == 2
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/storage/test_sqlite_spine.py -v`

Expected: import failure for SQLiteStore.

- [ ] **Step 3: Add migration SQL**

```sql
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE events(cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL, run_id TEXT, sequence INTEGER NOT NULL, type TEXT NOT NULL, schema_version INTEGER NOT NULL, occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE snapshots(kind TEXT NOT NULL, entity_id TEXT NOT NULL, session_id TEXT NOT NULL, version INTEGER NOT NULL, data_json TEXT NOT NULL, PRIMARY KEY(kind, entity_id));
CREATE INDEX events_session_cursor ON events(session_id, cursor);
```

- [ ] **Step 4: Implement SQLite commit/read/delete**

Open one aiosqlite connection, enable foreign keys/WAL, run migrations, and serialize with canonical JSON (`sort_keys=True`, compact separators). In `commit`, execute `BEGIN IMMEDIATE`, insert every event, upsert snapshots only when incoming version is greater, then commit; rollback on any exception.

```python
async def commit(self, batch: CommitBatch) -> CommitResult:
    async with self._lock:
        await self._connection.execute("BEGIN IMMEDIATE")
        try:
            for event in batch.events:
                await self._insert_event(event)
            for snapshot in batch.snapshots:
                await self._upsert_newer_snapshot(snapshot)
            cursor = await self._last_cursor()
            await self._connection.commit()
            return CommitResult(last_cursor=cursor)
        except BaseException:
            await self._connection.rollback()
            raise
```

- [ ] **Step 5: Implement minimal runtime snapshots and commands**

```python
class RunStatus(StrEnum):
    CREATED = "created"; RUNNING = "running"; COMPLETED = "completed"; FAILED = "failed"

class SessionSnapshot(BaseModel):
    session_id: str; status: Literal["active"] = "active"; workspaces: tuple[str, ...]; version: int = 1

class RunSnapshot(BaseModel):
    run_id: str; session_id: str; agent_revision: str; status: RunStatus; user_input: str; version: int = 1
```

`RuntimeCommands` allocates ids, builds one event plus one snapshot per command, and commits through StateStore.

- [ ] **Step 6: Verify SQLite behavior and parity**

Run: `uv run pytest tests/contract/test_memory_store_contract.py tests/integration/storage/test_sqlite_spine.py -v`

Expected: all tests pass after running both stores.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/storage src/agent_sdk/runtime tests/integration/storage
git commit -m "feat: add sqlite runtime spine"
```
