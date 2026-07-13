import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import aiosqlite
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
        initial_journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
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
        final_journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        migration = connection.execute("SELECT version, applied_at FROM schema_migrations").fetchone()
        events_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
    assert migration == (1, "existing")
    assert final_journal_mode == initial_journal_mode
    assert events_sql is not None
    assert "AUTOINCREMENT" not in events_sql[0]


@pytest.mark.asyncio
async def test_open_enables_and_verifies_sqlite_pragmas(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    async with store._connection.execute("PRAGMA journal_mode") as cursor:
        journal_mode = await cursor.fetchone()
    async with store._connection.execute("PRAGMA foreign_keys") as cursor:
        foreign_keys = await cursor.fetchone()

    assert journal_mode == ("wal",)
    assert foreign_keys == (1,)
    await store.close()


@pytest.mark.asyncio
async def test_open_rejects_and_closes_when_wal_cannot_be_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = await aiosqlite.connect(":memory:")

    async def connect_to_memory(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        return connection

    monkeypatch.setattr(aiosqlite, "connect", connect_to_memory)

    with pytest.raises(RuntimeError, match="journal_mode"):
        await SQLiteStore.open(tmp_path / "state.db")

    with pytest.raises(ValueError, match="no active connection"):
        await connection.execute("SELECT 1")


@pytest.mark.asyncio
async def test_open_rejects_and_closes_when_foreign_keys_cannot_be_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "state.db"
    connection = await aiosqlite.connect(database_path)

    def ignore_foreign_keys(
        action: int,
        arg1: str | None,
        arg2: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del database_name, trigger_name
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "foreign_keys" and arg2 == "ON":
            return sqlite3.SQLITE_IGNORE
        return sqlite3.SQLITE_OK

    await connection.set_authorizer(ignore_foreign_keys)

    async def connect_with_authorizer(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        return connection

    monkeypatch.setattr(aiosqlite, "connect", connect_with_authorizer)

    with pytest.raises(RuntimeError, match="foreign_keys"):
        await SQLiteStore.open(database_path)

    with pytest.raises(ValueError, match="no active connection"):
        await connection.execute("SELECT 1")


@pytest.mark.parametrize("pragma", ["foreign_keys", "journal_mode"])
@pytest.mark.asyncio
async def test_open_converts_pragma_setter_errors_stably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pragma: str,
) -> None:
    database_path = tmp_path / "state.db"
    connection = await aiosqlite.connect(database_path)

    def deny_pragma_setter(
        action: int,
        arg1: str | None,
        arg2: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del database_name, trigger_name
        if action == sqlite3.SQLITE_PRAGMA and arg1 == pragma and arg2 is not None:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    await connection.set_authorizer(deny_pragma_setter)

    async def connect_with_authorizer(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        return connection

    monkeypatch.setattr(aiosqlite, "connect", connect_with_authorizer)

    with pytest.raises(RuntimeError, match=pragma):
        await SQLiteStore.open(database_path)

    with pytest.raises(ValueError, match="no active connection"):
        await connection.execute("SELECT 1")


def _cancel_after_begin(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = store._connection.execute

    def execute_with_cancel(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql != "BEGIN IMMEDIATE":
            return execute(sql, *args, **kwargs)

        async def begin_then_cancel() -> None:
            await execute(sql, *args, **kwargs)
            raise asyncio.CancelledError

        return begin_then_cancel()

    monkeypatch.setattr(store._connection, "execute", execute_with_cancel)


def _block_rollback(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, asyncio.Event]:
    rollback = store._connection.rollback
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_rollback() -> None:
        started.set()
        await release.wait()
        await rollback()

    monkeypatch.setattr(store._connection, "rollback", blocked_rollback)
    return started, release


@pytest.mark.asyncio
async def test_cancelled_commit_rolls_back_open_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    event = EventEnvelope.new(
        type="session.created",
        session_id="ses_1",
        run_id=None,
        sequence=1,
        payload={},
    )
    try:
        with monkeypatch.context() as cancelled:
            _cancel_after_begin(store, cancelled)
            with pytest.raises(asyncio.CancelledError):
                await store.commit(CommitBatch(events=(event,)))

        result = await store.commit(CommitBatch(events=(event,)))
        assert result.last_cursor == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancelled_delete_rolls_back_open_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    event = EventEnvelope.new(
        type="session.created",
        session_id="ses_1",
        run_id=None,
        sequence=1,
        payload={},
    )
    await store.commit(CommitBatch(events=(event,)))
    try:
        with monkeypatch.context() as cancelled:
            _cancel_after_begin(store, cancelled)
            with pytest.raises(asyncio.CancelledError):
                await store.delete_session("ses_1")

        await store.delete_session("ses_1")
        assert await store.read_events(after_cursor=0) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_commit_propagates_cancellation_received_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    rejected = EventEnvelope.new(
        type="run.failed",
        session_id="ses_1",
        run_id="run_1",
        sequence=2,
        payload={},
    )
    task: asyncio.Task[object] | None = None
    release: asyncio.Event | None = None
    try:
        with monkeypatch.context() as race:
            rollback_started, release = _block_rollback(store, race)
            task = asyncio.create_task(
                store.commit(
                    CommitBatch(
                        events=(rejected,),
                        snapshots=(
                            SnapshotWrite(
                                "run",
                                "run_1",
                                "ses_1",
                                1,
                                {"status": "failed"},
                            ),
                        ),
                    )
                )
            )
            await asyncio.wait_for(rollback_started.wait(), timeout=1)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        result = await store.commit(CommitBatch(events=(rejected,)))
        assert result.last_cursor == 2
        snapshot = await store.get_snapshot("run", "run_1")
        assert snapshot is not None
        assert snapshot["status"] == "created"
    finally:
        if release is not None:
            release.set()
        if task is not None and not task.done():
            task.cancel()
            with suppress(BaseException):
                await task
        await store.close()


@pytest.mark.asyncio
async def test_delete_propagates_cancellation_received_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    first = EventEnvelope.new(
        type="session.created",
        session_id="ses_1",
        run_id=None,
        sequence=1,
        payload={},
    )
    await store.commit(CommitBatch(events=(first,)))
    task: asyncio.Task[object] | None = None
    release: asyncio.Event | None = None
    try:
        with monkeypatch.context() as race:
            execute = store._connection.execute

            def fail_snapshot_delete(sql: str, *args: Any, **kwargs: Any) -> Any:
                if "DELETE FROM snapshots" not in sql:
                    return execute(sql, *args, **kwargs)

                async def fail() -> None:
                    raise ValueError("original failure")

                return fail()

            race.setattr(store._connection, "execute", fail_snapshot_delete)
            rollback_started, release = _block_rollback(store, race)
            task = asyncio.create_task(store.delete_session("ses_1"))
            await asyncio.wait_for(rollback_started.wait(), timeout=1)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        second = EventEnvelope.new(
            type="session.created",
            session_id="ses_2",
            run_id=None,
            sequence=1,
            payload={},
        )
        result = await store.commit(CommitBatch(events=(second,)))
        assert result.last_cursor == 2
        assert [item.event.session_id for item in await store.read_events(after_cursor=0)] == [
            "ses_1",
            "ses_2",
        ]
    finally:
        if release is not None:
            release.set()
        if task is not None and not task.done():
            task.cancel()
            with suppress(BaseException):
                await task
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_rejects_wrong_aggregate_index_expression(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1, 'existing');
            CREATE TABLE events(
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
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
                data_json TEXT NOT NULL,
                PRIMARY KEY(kind, entity_id)
            );
            CREATE INDEX events_session_cursor ON events(session_id, cursor);
            CREATE UNIQUE INDEX events_aggregate_sequence
                ON events(COALESCE(session_id, run_id), sequence);
            CREATE INDEX snapshots_session ON snapshots(session_id);
            """
        )

    opened: SQLiteStore | None = None
    try:
        with pytest.raises(ValueError, match="incompatible database schema"):
            opened = await SQLiteStore.open(path)
    finally:
        if opened is not None:
            await opened.close()


@pytest.mark.asyncio
async def test_closed_store_rejects_public_operations_stably(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    await store.close()

    operations: tuple[Callable[[], Awaitable[object]], ...] = (
        lambda: store.commit(CommitBatch(events=())),
        lambda: store.read_events(after_cursor=0),
        lambda: store.get_snapshot("session", "ses_1"),
        lambda: store.delete_session("ses_1"),
    )
    for operation in operations:
        with pytest.raises(RuntimeError, match="SQLiteStore is closed"):
            await operation()


@pytest.mark.asyncio
async def test_cancelled_close_keeps_store_stably_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    close_finished = asyncio.Event()
    release = asyncio.Event()
    task: asyncio.Task[None] | None = None
    try:
        with monkeypatch.context() as race:
            close = store._connection.close

            async def close_then_block() -> None:
                await close()
                close_finished.set()
                await release.wait()

            race.setattr(store._connection, "close", close_then_block)
            task = asyncio.create_task(store.close())
            await asyncio.wait_for(close_finished.wait(), timeout=1)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        with pytest.raises(RuntimeError, match="SQLiteStore is closed"):
            await store.read_events(after_cursor=0)
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            with suppress(BaseException):
                await task
        await store.close()
