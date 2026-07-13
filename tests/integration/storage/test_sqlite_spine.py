import sqlite3
from pathlib import Path

import pytest

from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.runtime.commands import RuntimeCommands


@pytest.mark.asyncio
async def test_session_and_run_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = await SQLiteStore.open(path)
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[tmp_path])
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="hello",
    )
    await store.close()

    reopened = await SQLiteStore.open(path)
    assert (await reopened.get_snapshot("session", session.session_id))["status"] == "active"
    assert (await reopened.get_snapshot("run", run.run_id))["status"] == "created"
    assert len(await reopened.read_events(after_cursor=0, session_id=session.session_id)) == 2
    await reopened.close()


@pytest.mark.asyncio
async def test_sqlite_stale_snapshot_rolls_back_event_and_cursor(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    created = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={},
    )
    await store.commit(
        CommitBatch(
            events=(created,),
            snapshots=(
                SnapshotWrite("run", "run_1", "ses_1", 1, {"status": "created"}),
            ),
        )
    )
    stale = EventEnvelope.new(
        type="run.failed",
        session_id="ses_1",
        run_id="run_1",
        sequence=2,
        payload={},
    )

    with pytest.raises(ValueError, match="snapshot version"):
        await store.commit(
            CommitBatch(
                events=(stale,),
                snapshots=(
                    SnapshotWrite("run", "run_1", "ses_1", 1, {"status": "failed"}),
                ),
            )
        )

    assert [item.cursor for item in await store.read_events(after_cursor=0)] == [1]
    await store.close()


@pytest.mark.asyncio
async def test_sqlite_delete_leaves_global_cursor_hole(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="session.created",
                    session_id="ses_1",
                    run_id=None,
                    sequence=1,
                    payload={},
                ),
            )
        )
    )
    await store.delete_session("ses_1")
    result = await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="session.created",
                    session_id="ses_2",
                    run_id=None,
                    sequence=1,
                    payload={},
                ),
            )
        )
    )

    assert result.last_cursor == 2
    await store.close()


@pytest.mark.asyncio
async def test_sqlite_rejects_incompatible_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1, 'existing');
            CREATE TABLE events(
                cursor INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT,
                sequence INTEGER NOT NULL,
                type TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE snapshots(
                kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                data_json TEXT NOT NULL
            );
            """
        )

    with pytest.raises(ValueError, match="incompatible database schema"):
        await SQLiteStore.open(path)

    with sqlite3.connect(path) as connection:
        migration = connection.execute("SELECT version, applied_at FROM schema_migrations").fetchone()
        events_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
    assert migration == (1, "existing")
    assert events_sql is not None
    assert "AUTOINCREMENT" not in events_sql[0]
