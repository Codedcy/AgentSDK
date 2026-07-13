from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, cast

import aiosqlite

from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import CommitBatch, CommitResult, SnapshotWrite, StoredEvent

_SCHEMA_VERSION = 1
_EXPECTED_TABLE_INFO: dict[str, tuple[tuple[str, str, bool, int], ...]] = {
    "schema_migrations": (
        ("version", "INTEGER", False, 1),
        ("applied_at", "TEXT", True, 0),
    ),
    "events": (
        ("cursor", "INTEGER", False, 1),
        ("event_id", "TEXT", True, 0),
        ("session_id", "TEXT", True, 0),
        ("run_id", "TEXT", False, 0),
        ("sequence", "INTEGER", True, 0),
        ("type", "TEXT", True, 0),
        ("schema_version", "INTEGER", True, 0),
        ("occurred_at", "TEXT", True, 0),
        ("payload_json", "TEXT", True, 0),
    ),
    "snapshots": (
        ("kind", "TEXT", True, 1),
        ("entity_id", "TEXT", True, 2),
        ("session_id", "TEXT", True, 0),
        ("version", "INTEGER", True, 0),
        ("data_json", "TEXT", True, 0),
    ),
}
_EXPECTED_INDEXES = {
    "events_session_cursor": (False, ("session_id", "cursor")),
    "events_aggregate_sequence": (True, (None, "sequence")),
    "snapshots_session": (False, ("session_id",)),
}
_AGGREGATE_INDEX_SQL = (
    "create unique index events_aggregate_sequence "
    "on events(coalesce(run_id, session_id), sequence)"
)


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_object(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("stored JSON must be an object")
    return cast(dict[str, Any], decoded)


def _normalized_sql(value: str) -> str:
    return "".join(value.casefold().split())


class SQLiteStore:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(cls, path: str | Path) -> SQLiteStore:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(database_path)
        try:
            await cls._migrate(connection)
            await cls._configure_connection(connection)
        except BaseException:
            await connection.close()
            raise
        return cls(connection)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            await self._connection.close()
            self._closed = True

    async def commit(self, batch: CommitBatch) -> CommitResult:
        async with self._lock:
            self._ensure_open()
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                for event in batch.events:
                    await self._insert_event(event)
                for snapshot in batch.snapshots:
                    await self._upsert_newer_snapshot(snapshot)
                cursor = await self._last_cursor()
                await self._connection.commit()
                return CommitResult(last_cursor=cursor)
            except BaseException:
                await self._rollback()
                raise

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
    ) -> list[StoredEvent]:
        async with self._lock:
            self._ensure_open()
            if session_id is None:
                query = """
                    SELECT cursor, event_id, schema_version, type, session_id, run_id,
                           sequence, payload_json, occurred_at
                    FROM events
                    WHERE cursor > ?
                    ORDER BY cursor
                """
                parameters: tuple[object, ...] = (after_cursor,)
            else:
                query = """
                    SELECT cursor, event_id, schema_version, type, session_id, run_id,
                           sequence, payload_json, occurred_at
                    FROM events
                    WHERE cursor > ? AND session_id = ?
                    ORDER BY cursor
                """
                parameters = (after_cursor, session_id)
            async with self._connection.execute(query, parameters) as cursor:
                rows = await cursor.fetchall()
            return [self._stored_event(row) for row in rows]

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        async with self._lock:
            self._ensure_open()
            async with self._connection.execute(
                "SELECT data_json FROM snapshots WHERE kind = ? AND entity_id = ?",
                (kind, entity_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            return _json_object(cast(str, row[0]))

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            self._ensure_open()
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                await self._connection.execute(
                    "DELETE FROM events WHERE session_id = ?",
                    (session_id,),
                )
                await self._connection.execute(
                    "DELETE FROM snapshots WHERE session_id = ?",
                    (session_id,),
                )
                await self._connection.commit()
            except BaseException:
                await self._rollback()
                raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLiteStore is closed")

    async def _rollback(self) -> None:
        rollback = asyncio.create_task(self._connection.rollback())
        while not rollback.done():
            try:
                await asyncio.shield(rollback)
            except asyncio.CancelledError:
                continue
        rollback.result()

    async def _insert_event(self, event: EventEnvelope) -> None:
        async with self._connection.execute(
            "SELECT 1 FROM events WHERE event_id = ?",
            (event.event_id,),
        ) as cursor:
            duplicate = await cursor.fetchone()
        if duplicate is not None:
            raise ValueError("event id must be unique")

        if event.run_id is None:
            sequence_query = """
                SELECT MAX(sequence) FROM events
                WHERE run_id IS NULL AND session_id = ?
            """
            aggregate_id = event.session_id
        else:
            sequence_query = "SELECT MAX(sequence) FROM events WHERE run_id = ?"
            aggregate_id = event.run_id
        async with self._connection.execute(sequence_query, (aggregate_id,)) as cursor:
            row = await cursor.fetchone()
        previous_sequence = None if row is None else cast(int | None, row[0])
        if previous_sequence is not None and event.sequence <= previous_sequence:
            raise ValueError("event sequence must be strictly increasing")

        try:
            await self._connection.execute(
                """
                INSERT INTO events(
                    event_id, session_id, run_id, sequence, type, schema_version,
                    occurred_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.session_id,
                    event.run_id,
                    event.sequence,
                    event.type,
                    event.schema_version,
                    event.occurred_at.isoformat(),
                    _canonical_json(event.payload),
                ),
            )
        except sqlite3.IntegrityError as error:
            message = str(error)
            if "event_id" in message:
                raise ValueError("event id must be unique") from error
            if "events_aggregate_sequence" in message:
                raise ValueError("event sequence must be strictly increasing") from error
            raise

    async def _upsert_newer_snapshot(self, snapshot: SnapshotWrite) -> None:
        async with self._connection.execute(
            "SELECT version FROM snapshots WHERE kind = ? AND entity_id = ?",
            (snapshot.kind, snapshot.entity_id),
        ) as cursor:
            row = await cursor.fetchone()
        previous_version = None if row is None else cast(int, row[0])
        if previous_version is not None and snapshot.version <= previous_version:
            raise ValueError("snapshot version must be strictly increasing")
        await self._connection.execute(
            """
            INSERT INTO snapshots(kind, entity_id, session_id, version, data_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(kind, entity_id) DO UPDATE SET
                session_id = excluded.session_id,
                version = excluded.version,
                data_json = excluded.data_json
            """,
            (
                snapshot.kind,
                snapshot.entity_id,
                snapshot.session_id,
                snapshot.version,
                _canonical_json(snapshot.data),
            ),
        )

    async def _last_cursor(self) -> int:
        async with self._connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'events'"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return 0
        return cast(int, row[0])

    @staticmethod
    def _stored_event(row: sqlite3.Row) -> StoredEvent:
        event = EventEnvelope.model_validate(
            {
                "event_id": row[1],
                "schema_version": row[2],
                "type": row[3],
                "session_id": row[4],
                "run_id": row[5],
                "sequence": row[6],
                "payload": _json_object(cast(str, row[7])),
                "occurred_at": row[8],
            }
        )
        return StoredEvent(cursor=cast(int, row[0]), event=event)

    @classmethod
    async def _migrate(cls, connection: aiosqlite.Connection) -> None:
        async with connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ) as cursor:
            rows = await cursor.fetchall()
        table_names = {cast(str, row[0]) for row in rows}

        if "schema_migrations" not in table_names:
            if table_names:
                raise ValueError("incompatible database schema")
            migration = resources.files("agent_sdk.storage").joinpath(
                "migrations",
                "0001_initial.sql",
            )
            await connection.executescript(migration.read_text(encoding="utf-8"))
            await connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )
            await connection.commit()
        else:
            async with connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ) as cursor:
                version_rows = await cursor.fetchall()
            versions = tuple(cast(int, row[0]) for row in version_rows)
            if versions != (_SCHEMA_VERSION,):
                raise ValueError("incompatible database schema version")

        await cls._validate_schema(connection)

    @staticmethod
    async def _configure_connection(connection: aiosqlite.Connection) -> None:
        try:
            await connection.execute("PRAGMA foreign_keys=ON")
            async with connection.execute("PRAGMA foreign_keys") as cursor:
                foreign_keys = await cursor.fetchone()
        except sqlite3.Error as error:
            raise RuntimeError("failed to enable SQLite foreign_keys") from error
        if foreign_keys != (1,):
            raise RuntimeError("failed to enable SQLite foreign_keys")

        try:
            async with connection.execute("PRAGMA journal_mode=WAL") as cursor:
                journal_mode = await cursor.fetchone()
        except sqlite3.Error as error:
            raise RuntimeError("failed to enable SQLite journal_mode=WAL") from error
        if journal_mode is None or cast(str, journal_mode[0]).lower() != "wal":
            raise RuntimeError("failed to enable SQLite journal_mode=WAL")

    @classmethod
    async def _validate_schema(cls, connection: aiosqlite.Connection) -> None:
        for table_name, expected_info in _EXPECTED_TABLE_INFO.items():
            async with connection.execute(f"PRAGMA table_info({table_name})") as cursor:
                rows = await cursor.fetchall()
            table_info = tuple(
                (
                    cast(str, row[1]),
                    cast(str, row[2]).upper(),
                    bool(row[3]),
                    cast(int, row[5]),
                )
                for row in rows
            )
            if table_info != expected_info:
                raise ValueError("incompatible database schema")

        async with connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ) as cursor:
            events_row = await cursor.fetchone()
        if events_row is None or "AUTOINCREMENT" not in cast(str, events_row[0]).upper():
            raise ValueError("incompatible database schema")

        indexes: dict[str, tuple[bool, tuple[str | None, ...]]] = {}
        for table_name in ("events", "snapshots"):
            async with connection.execute(f"PRAGMA index_list({table_name})") as cursor:
                index_rows = await cursor.fetchall()
            for index_row in index_rows:
                index_name = cast(str, index_row[1])
                if index_name not in _EXPECTED_INDEXES:
                    continue
                async with connection.execute(f"PRAGMA index_info({index_name})") as cursor:
                    column_rows = await cursor.fetchall()
                indexes[index_name] = (
                    bool(index_row[2]),
                    tuple(cast(str | None, column_row[2]) for column_row in column_rows),
                )
        if indexes != _EXPECTED_INDEXES:
            raise ValueError("incompatible database schema")

        async with connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("events_aggregate_sequence",),
        ) as cursor:
            aggregate_index = await cursor.fetchone()
        if aggregate_index is None or _normalized_sql(cast(str, aggregate_index[0])) != (
            _normalized_sql(_AGGREGATE_INDEX_SQL)
        ):
            raise ValueError("incompatible database schema")

        async with connection.execute(
            """
            SELECT 1
            FROM pragma_index_list('events') AS index_list
            WHERE index_list."unique" = 1
              AND (
                  SELECT group_concat(name, ',')
                  FROM pragma_index_info(index_list.name)
              ) = 'event_id'
            """
        ) as cursor:
            event_id_unique = await cursor.fetchone()
        if event_id_unique is None:
            raise ValueError("incompatible database schema")
