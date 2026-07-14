from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from enum import Enum
from importlib import resources
from pathlib import Path
from time import monotonic
from typing import Any, NamedTuple, cast

import aiosqlite

from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.leases import (
    Lease,
    LeaseHeldError,
    LeaseLostError,
    canonical_lease_timestamp,
)
from agent_sdk.runtime.models import RunSnapshot
from agent_sdk.runtime.reconciliation import (
    ExternalOperation,
    ExternalOperationStatus,
    ModelCallOperation,
    ReconciliationRequest,
    ReconciliationStatus,
    RecoveryStateConflictError,
    RunCheckpoint,
    RunCheckpointPhase,
    ToolCallOperation,
    _canonical_record_json,
    _checkpoint_from_json,
    _context_free_recovery_errors,
    _external_operation_from_json,
    _reconciliation_request_from_json,
    _valid_checkpoint_replay_shape,
)
from agent_sdk.storage.base import (
    canonical_snapshot_data,
    CommitBatch,
    CommitResult,
    EventPreconditionConflictError,
    EventPreconditionNotFoundError,
    SnapshotPreconditionError,
    SnapshotPrecondition,
    SnapshotWrite,
    StoredEvent,
)
from agent_sdk.storage.idempotency import (
    IdempotencyConflictError,
    IdempotencyRecord,
    IdempotencyReplay,
    IdempotencyReplayMissError,
    IdempotencyValidationError,
    canonical_result_json,
    detached_record,
    record_from_stored_json,
    record_from_write,
    validate_replay,
)
from agent_sdk.tools.models import thaw_json

_SCHEMA_VERSION = 3
_MIGRATION_2_TRANSFORM_ID = "session-ownership-v1-to-v2"
_OPEN_BUSY_TIMEOUT_MS = 50
_OPEN_RETRY_SECONDS = 2.0
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
    "idempotency_records": (
        ("scope", "TEXT", True, 1),
        ("key", "TEXT", True, 2),
        ("request_fingerprint", "TEXT", True, 0),
        ("session_id", "TEXT", True, 0),
        ("result_json", "TEXT", True, 0),
    ),
    "leases": (
        ("run_id", "TEXT", True, 1),
        ("owner", "TEXT", True, 0),
        ("generation", "INTEGER", True, 0),
        ("acquired_at", "TEXT", True, 0),
        ("renewed_at", "TEXT", True, 0),
        ("expires_at", "TEXT", True, 0),
        ("released", "INTEGER", True, 0),
    ),
    "external_operations": (
        ("operation_id", "TEXT", True, 1),
        ("operation_kind", "TEXT", True, 0),
        ("session_id", "TEXT", True, 0),
        ("run_id", "TEXT", True, 0),
        ("turn", "INTEGER", True, 0),
        ("request_fingerprint", "TEXT", True, 0),
        ("provider_identity", "TEXT", False, 0),
        ("tool_identity", "TEXT", False, 0),
        ("lease_generation", "INTEGER", True, 0),
        ("status", "TEXT", True, 0),
        ("data_json", "TEXT", True, 0),
    ),
    "run_checkpoints": (
        ("run_id", "TEXT", True, 1),
        ("session_id", "TEXT", True, 0),
        ("checkpoint_version", "INTEGER", True, 0),
        ("turn", "INTEGER", True, 0),
        ("phase", "TEXT", True, 0),
        ("operation_id", "TEXT", False, 0),
        ("data_json", "TEXT", True, 0),
    ),
    "reconciliation_requests": (
        ("request_id", "TEXT", True, 1),
        ("session_id", "TEXT", True, 0),
        ("run_id", "TEXT", True, 0),
        ("operation_id", "TEXT", False, 0),
        ("status", "TEXT", True, 0),
        ("data_json", "TEXT", True, 0),
    ),
}
_EXPECTED_INDEXES = {
    "events_session_cursor": (False, ("session_id", "cursor")),
    "events_aggregate_sequence": (True, (None, "sequence")),
    "snapshots_session": (False, ("session_id",)),
    "idempotency_records_session": (False, ("session_id",)),
    "leases_expires_at": (False, ("expires_at",)),
    "external_operations_session": (False, ("session_id",)),
    "external_operations_run_status": (False, ("run_id", "status")),
    "run_checkpoints_session": (False, ("session_id",)),
    "run_checkpoints_phase": (False, ("phase",)),
    "run_checkpoints_operation": (False, ("operation_id",)),
    "reconciliation_requests_session": (False, ("session_id",)),
    "reconciliation_requests_run_status": (False, ("run_id", "status")),
    "reconciliation_requests_operation": (False, ("operation_id",)),
}
_AGGREGATE_INDEX_SQL = (
    "create unique index events_aggregate_sequence "
    "on events(coalesce(run_id, session_id), sequence)"
)
_EXPECTED_TABLE_SQL = {
    "schema_migrations": """
        CREATE TABLE schema_migrations(
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """,
    "events": """
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
        )
    """,
    "snapshots": """
        CREATE TABLE snapshots(
            kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY(kind, entity_id)
        )
    """,
    "idempotency_records": """
        CREATE TABLE idempotency_records(
            scope TEXT NOT NULL,
            key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            session_id TEXT NOT NULL,
            result_json TEXT NOT NULL,
            PRIMARY KEY(scope, key)
        )
    """,
    "leases": """
        CREATE TABLE leases(
            run_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(run_id)) > 0),
            owner TEXT NOT NULL CHECK(length(trim(owner)) > 0),
            generation INTEGER NOT NULL CHECK(generation >= 1),
            acquired_at TEXT NOT NULL CHECK(length(acquired_at) > 0),
            renewed_at TEXT NOT NULL CHECK(length(renewed_at) > 0),
            expires_at TEXT NOT NULL CHECK(
                length(expires_at) > 0
                AND renewed_at >= acquired_at
                AND expires_at > renewed_at
            ),
            released INTEGER NOT NULL DEFAULT 0 CHECK(released IN (0, 1))
        )
    """,
    "external_operations": """
        CREATE TABLE external_operations(
            operation_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(operation_id)) > 0),
            operation_kind TEXT NOT NULL CHECK(operation_kind IN ('model_call', 'tool_call')),
            session_id TEXT NOT NULL CHECK(length(trim(session_id)) > 0),
            run_id TEXT NOT NULL CHECK(length(trim(run_id)) > 0),
            turn INTEGER NOT NULL CHECK(turn >= 0),
            request_fingerprint TEXT NOT NULL CHECK(length(trim(request_fingerprint)) > 0),
            provider_identity TEXT,
            tool_identity TEXT,
            lease_generation INTEGER NOT NULL CHECK(lease_generation >= 1),
            status TEXT NOT NULL CHECK(status IN ('started', 'completed', 'failed')),
            data_json TEXT NOT NULL CHECK(
                json_valid(data_json) AND json_type(data_json) = 'object'
            ),
            UNIQUE(run_id, turn, operation_kind, operation_id),
            UNIQUE(operation_id, run_id, session_id),
            CHECK(
                (operation_kind = 'model_call'
                    AND provider_identity IS NOT NULL
                    AND length(trim(provider_identity)) > 0
                    AND tool_identity IS NULL)
                OR
                (operation_kind = 'tool_call'
                    AND provider_identity IS NULL
                    AND tool_identity IS NOT NULL
                    AND length(trim(tool_identity)) > 0)
            ),
            CHECK(coalesce(
                json_type(data_json, '$.operation_id') = 'text'
                AND json_extract(data_json, '$.operation_id') = operation_id
                AND json_type(data_json, '$.operation_kind') = 'text'
                AND json_extract(data_json, '$.operation_kind') = operation_kind
                AND json_type(data_json, '$.session_id') = 'text'
                AND json_extract(data_json, '$.session_id') = session_id
                AND json_type(data_json, '$.run_id') = 'text'
                AND json_extract(data_json, '$.run_id') = run_id
                AND json_type(data_json, '$.turn') = 'integer'
                AND json_extract(data_json, '$.turn') = turn
                AND json_type(data_json, '$.request_fingerprint') = 'text'
                AND json_extract(data_json, '$.request_fingerprint') = request_fingerprint
                AND (
                    (provider_identity IS NULL
                        AND json_type(data_json, '$.provider_identity') = 'null')
                    OR
                    (provider_identity IS NOT NULL
                        AND json_type(data_json, '$.provider_identity') = 'text'
                        AND json_extract(data_json, '$.provider_identity') = provider_identity)
                )
                AND (
                    (tool_identity IS NULL
                        AND json_type(data_json, '$.tool_identity') = 'null')
                    OR
                    (tool_identity IS NOT NULL
                        AND json_type(data_json, '$.tool_identity') = 'text'
                        AND json_extract(data_json, '$.tool_identity') = tool_identity)
                )
                AND json_type(data_json, '$.lease_generation') = 'integer'
                AND json_extract(data_json, '$.lease_generation') = lease_generation
                AND json_type(data_json, '$.status') = 'text'
                AND json_extract(data_json, '$.status') = status,
                0
            ))
        )
    """,
    "run_checkpoints": """
        CREATE TABLE run_checkpoints(
            run_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(run_id)) > 0),
            session_id TEXT NOT NULL CHECK(length(trim(session_id)) > 0),
            checkpoint_version INTEGER NOT NULL CHECK(checkpoint_version >= 1),
            turn INTEGER NOT NULL CHECK(turn >= 0),
            phase TEXT NOT NULL CHECK(phase IN (
                'ready_for_model', 'model_in_flight', 'ready_for_tool',
                'tool_in_flight', 'waiting', 'terminal'
            )),
            operation_id TEXT,
            data_json TEXT NOT NULL CHECK(
                json_valid(data_json) AND json_type(data_json) = 'object'
            ),
            FOREIGN KEY(operation_id, run_id, session_id)
                REFERENCES external_operations(operation_id, run_id, session_id)
                ON DELETE RESTRICT,
            CHECK(
                (phase IN ('model_in_flight', 'tool_in_flight') AND operation_id IS NOT NULL)
                OR
                (phase NOT IN ('model_in_flight', 'tool_in_flight') AND operation_id IS NULL)
            ),
            CHECK(coalesce(
                json_type(data_json, '$.run_id') = 'text'
                AND json_extract(data_json, '$.run_id') = run_id
                AND json_type(data_json, '$.session_id') = 'text'
                AND json_extract(data_json, '$.session_id') = session_id
                AND json_type(data_json, '$.checkpoint_version') = 'integer'
                AND json_extract(data_json, '$.checkpoint_version') = checkpoint_version
                AND json_type(data_json, '$.turn') = 'integer'
                AND json_extract(data_json, '$.turn') = turn
                AND json_type(data_json, '$.phase') = 'text'
                AND json_extract(data_json, '$.phase') = phase
                AND (
                    (operation_id IS NULL
                        AND json_type(data_json, '$.operation_id') = 'null')
                    OR
                    (operation_id IS NOT NULL
                        AND json_type(data_json, '$.operation_id') = 'text'
                        AND json_extract(data_json, '$.operation_id') = operation_id)
                ),
                0
            ))
        )
    """,
    "reconciliation_requests": """
        CREATE TABLE reconciliation_requests(
            request_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(request_id)) > 0),
            session_id TEXT NOT NULL CHECK(length(trim(session_id)) > 0),
            run_id TEXT NOT NULL CHECK(length(trim(run_id)) > 0),
            operation_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending', 'resolved')),
            data_json TEXT NOT NULL CHECK(
                json_valid(data_json) AND json_type(data_json) = 'object'
            ),
            FOREIGN KEY(operation_id, run_id, session_id)
                REFERENCES external_operations(operation_id, run_id, session_id)
                ON DELETE RESTRICT,
            CHECK(coalesce(
                json_type(data_json, '$.request_id') = 'text'
                AND json_extract(data_json, '$.request_id') = request_id
                AND json_type(data_json, '$.session_id') = 'text'
                AND json_extract(data_json, '$.session_id') = session_id
                AND json_type(data_json, '$.run_id') = 'text'
                AND json_extract(data_json, '$.run_id') = run_id
                AND (
                    (operation_id IS NULL
                        AND json_type(data_json, '$.operation_id') = 'null')
                    OR
                    (operation_id IS NOT NULL
                        AND json_type(data_json, '$.operation_id') = 'text'
                        AND json_extract(data_json, '$.operation_id') = operation_id)
                )
                AND json_type(data_json, '$.status') = 'text'
                AND json_extract(data_json, '$.status') = status,
                0
            ))
        )
    """,
}
_EXPECTED_INDEX_SQL = {
    "events_session_cursor": (
        "CREATE INDEX events_session_cursor ON events(session_id, cursor)"
    ),
    "events_aggregate_sequence": _AGGREGATE_INDEX_SQL,
    "snapshots_session": "CREATE INDEX snapshots_session ON snapshots(session_id)",
    "idempotency_records_session": (
        "CREATE INDEX idempotency_records_session ON idempotency_records(session_id)"
    ),
    "leases_expires_at": "CREATE INDEX leases_expires_at ON leases(expires_at)",
    "external_operations_session": (
        "CREATE INDEX external_operations_session ON external_operations(session_id)"
    ),
    "external_operations_run_status": (
        "CREATE INDEX external_operations_run_status ON external_operations(run_id, status)"
    ),
    "run_checkpoints_session": (
        "CREATE INDEX run_checkpoints_session ON run_checkpoints(session_id)"
    ),
    "run_checkpoints_phase": "CREATE INDEX run_checkpoints_phase ON run_checkpoints(phase)",
    "run_checkpoints_operation": (
        "CREATE INDEX run_checkpoints_operation ON run_checkpoints(operation_id)"
    ),
    "reconciliation_requests_session": (
        "CREATE INDEX reconciliation_requests_session ON reconciliation_requests(session_id)"
    ),
    "reconciliation_requests_run_status": (
        "CREATE INDEX reconciliation_requests_run_status "
        "ON reconciliation_requests(run_id, status)"
    ),
    "reconciliation_requests_operation": (
        "CREATE INDEX reconciliation_requests_operation "
        "ON reconciliation_requests(operation_id)"
    ),
}


class _SchemaState(Enum):
    EMPTY = "empty"
    V1 = "v1"
    V2 = "v2"
    V3 = "v3"


def _canonical_json(value: dict[str, Any]) -> str:
    return canonical_snapshot_data(value)


def _json_object(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("stored JSON must be an object")
    return cast(dict[str, Any], decoded)


def _normalized_sql(value: str) -> str:
    return "".join(value.casefold().split())


def _complete_sql_statements(script: str) -> tuple[str, ...]:
    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise ValueError("incomplete packaged SQLite migration")
    return tuple(statements)


def _is_busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    message = str(error).casefold()
    return "database is locked" in message or "database table is locked" in message


def _lease_from_row(row: sqlite3.Row | tuple[Any, ...]) -> Lease:
    try:
        return Lease.model_validate(
            {
                "run_id": row[0],
                "owner": row[1],
                "generation": row[2],
                "acquired_at": row[3],
                "renewed_at": row[4],
                "expires_at": row[5],
            }
        )
    except ValueError as error:
        raise ValueError("incompatible lease row") from error


def _lease_values(lease: Lease) -> tuple[str, str, int, str, str, str]:
    return (
        lease.run_id,
        lease.owner,
        lease.generation,
        canonical_lease_timestamp(lease.acquired_at),
        canonical_lease_timestamp(lease.renewed_at),
        canonical_lease_timestamp(lease.expires_at),
    )


def _lease_identity_matches(current: Lease | None, expected: Lease) -> bool:
    return (
        current is not None
        and current.owner == expected.owner
        and current.generation == expected.generation
    )


def _valid_operation_transition(
    expected: ExternalOperation, updated: ExternalOperation
) -> bool:
    if (
        expected.status is not ExternalOperationStatus.STARTED
        or updated.status is ExternalOperationStatus.STARTED
        or type(expected) is not type(updated)
    ):
        return False
    immutable_fields = (
        "operation_id",
        "operation_kind",
        "session_id",
        "run_id",
        "turn",
        "request_fingerprint",
        "lease_generation",
        "provider_identity",
        "tool_identity",
        "recovery_metadata",
    )
    return all(
        getattr(expected, field) == getattr(updated, field)
        for field in immutable_fields
    )


def _valid_reconciliation_resolution(
    expected: ReconciliationRequest,
    resolved: ReconciliationRequest,
    event: EventEnvelope,
) -> bool:
    resolution = resolved.resolution
    if (
        expected.status is not ReconciliationStatus.PENDING
        or resolved.status is not ReconciliationStatus.RESOLVED
        or resolution is None
        or resolved.request_id != expected.request_id
        or resolved.session_id != expected.session_id
        or resolved.run_id != expected.run_id
        or resolved.operation_id != expected.operation_id
        or resolved.reason != expected.reason
        or resolved.details != expected.details
        or event.event_id != resolution.event_id
        or event.occurred_at != resolution.decided_at
        or event.type != "reconciliation.resolved"
        or event.session_id != resolved.session_id
        or event.run_id != resolved.run_id
    ):
        return False
    expected_payload = {
        "request_id": resolved.request_id,
        "operation_id": resolved.operation_id,
        "action": resolution.action.value,
        "actor": thaw_json(resolution.actor),
        "evidence": thaw_json(resolution.evidence),
    }
    return event.payload == expected_payload


class _StoredLease(NamedTuple):
    lease: Lease
    released: bool


async def _with_busy_retry(
    operation: Callable[[], Awaitable[Any]],
    *,
    deadline: float,
    message: str,
) -> Any:
    while True:
        try:
            return await operation()
        except sqlite3.OperationalError as error:
            if not _is_busy(error) or monotonic() >= deadline:
                if _is_busy(error):
                    raise RuntimeError(message) from error
                raise
            await asyncio.sleep(0)


async def _execute_script_statements(
    connection: aiosqlite.Connection,
    script: str,
    *,
    after_statement: Callable[[int], Awaitable[None]] | None = None,
) -> None:
    for index, statement in enumerate(_complete_sql_statements(script), start=1):
        await connection.execute(statement)
        if after_statement is not None:
            await after_statement(index)


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
            await cls._configure_connection(connection)
            await cls._migrate(connection)
        except BaseException:
            close_task = asyncio.create_task(connection.close())
            await cls._await_cleanup(close_task)
            raise
        return cls(connection)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            close = asyncio.create_task(self._connection.close())
            try:
                await self._await_cleanup(close)
            finally:
                if close.done() and not close.cancelled() and close.exception() is None:
                    self._closed = True

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if batch.replay_preconditions and batch.idempotency is None:
            raise IdempotencyValidationError(
                "replay preconditions require an idempotency request"
            )
        request = batch.idempotency
        incoming: IdempotencyRecord | None = None
        if isinstance(request, IdempotencyReplay):
            validate_replay(request)
        elif request is not None:
            incoming = record_from_write(request)
        async with self._lock:
            self._ensure_open()
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                if request is not None:
                    existing = await self._read_idempotency(request.scope, request.key)
                    if existing is not None:
                        await self._check_snapshot_preconditions(
                            batch.replay_preconditions
                        )
                        if existing.request_fingerprint != request.request_fingerprint:
                            raise IdempotencyConflictError(
                                "idempotency key was reused"
                            )
                        await self._rollback()
                        return CommitResult(
                            await self._last_cursor(),
                            applied=False,
                            idempotency=detached_record(existing),
                        )
                    if isinstance(request, IdempotencyReplay):
                        raise IdempotencyReplayMissError(
                            "idempotency replay record no longer exists"
                        )
                await self._check_event_preconditions(batch)
                await self._check_snapshot_preconditions(batch.preconditions)
                for event in batch.events:
                    await self._insert_event(event)
                for snapshot in batch.snapshots:
                    await self._upsert_newer_snapshot(snapshot)
                if isinstance(incoming, IdempotencyRecord):
                    await self._insert_idempotency(incoming)
                cursor = await self._last_cursor()
                await self._connection.commit()
                return CommitResult(last_cursor=cursor, idempotency=incoming)
            except BaseException:
                await self._rollback()
                raise

    async def _check_snapshot_preconditions(
        self, preconditions: tuple[SnapshotPrecondition, ...]
    ) -> None:
        for precondition in preconditions:
            async with self._connection.execute(
                """
                SELECT version, session_id, data_json
                FROM snapshots WHERE kind = ? AND entity_id = ?
                """,
                (precondition.kind, precondition.entity_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise SnapshotPreconditionError("snapshot precondition failed")
            version = cast(int, row[0])
            if (
                precondition.version is not None
                and version != precondition.version
            ) or (
                precondition.session_id is not None
                and cast(str, row[1]) != precondition.session_id
            ) or (
                precondition.data is not None
                and cast(str, row[2]) != _canonical_json(precondition.data)
            ):
                raise SnapshotPreconditionError("snapshot precondition failed")

    async def _check_event_preconditions(self, batch: CommitBatch) -> None:
        for precondition in batch.event_preconditions:
            async with self._connection.execute(
                """
                SELECT cursor, session_id, run_id, type, sequence
                FROM events WHERE event_id = ?
                """,
                (precondition.event_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise EventPreconditionNotFoundError(
                    "event precondition failed"
                )
            if (
                cast(int, row[0]) != precondition.cursor
                or cast(str, row[1]) != precondition.session_id
                or cast(str | None, row[2]) != precondition.run_id
                or cast(str, row[3]) != precondition.type
                or cast(int, row[4]) != precondition.sequence
            ):
                raise EventPreconditionConflictError(
                    "event precondition failed"
                )

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        if up_to_cursor is not None and up_to_cursor < after_cursor:
            raise ValueError("event cursor window is inverted")
        if limit is not None and limit <= 0:
            raise ValueError("event read limit must be positive")
        async with self._lock:
            self._ensure_open()
            predicates = ["cursor > ?"]
            parameters: list[object] = [after_cursor]
            if session_id is not None:
                predicates.append("session_id = ?")
                parameters.append(session_id)
            if up_to_cursor is not None:
                predicates.append("cursor <= ?")
                parameters.append(up_to_cursor)
            query = f"""
                SELECT cursor, event_id, schema_version, type, session_id, run_id,
                       sequence, payload_json, occurred_at
                FROM events
                WHERE {" AND ".join(predicates)}
                ORDER BY cursor
            """
            if limit is not None:
                query += " LIMIT ?"
                parameters.append(limit)
            async with self._connection.execute(query, tuple(parameters)) as cursor:
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

    async def get_idempotency(self, scope: str, key: str) -> IdempotencyRecord | None:
        async with self._lock:
            self._ensure_open()
            record = await self._read_idempotency(scope, key)
            return None if record is None else detached_record(record)

    @_context_free_recovery_errors
    async def create_external_operation(
        self, operation: ExternalOperation, *, lease: Lease, now: datetime
    ) -> ExternalOperation:
        serialized = _canonical_record_json(operation)
        async with self._lock:
            self._ensure_open()
            try:
                await self._begin_immediate("SQLite recovery operation conflict")
                await self._check_recovery_run_session(
                    operation.run_id, operation.session_id
                )
                await self._check_recovery_lease(
                    lease,
                    now=now,
                    run_id=operation.run_id,
                    lease_generation=operation.lease_generation,
                )
                if operation.status is not ExternalOperationStatus.STARTED:
                    raise RecoveryStateConflictError
                existing = await self._read_external_operation(
                    operation.operation_id
                )
                if existing is not None:
                    if _canonical_record_json(existing) != serialized:
                        raise RecoveryStateConflictError
                    await self._commit_transaction()
                    return _external_operation_from_json(serialized)
                await self._connection.execute(
                    """
                    INSERT INTO external_operations(
                        operation_id, operation_kind, session_id, run_id, turn,
                        request_fingerprint, provider_identity, tool_identity,
                        lease_generation, status, data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation.operation_id,
                        operation.operation_kind.value,
                        operation.session_id,
                        operation.run_id,
                        operation.turn,
                        operation.request_fingerprint,
                        operation.provider_identity,
                        operation.tool_identity,
                        operation.lease_generation,
                        operation.status.value,
                        serialized,
                    ),
                )
                await self._commit_transaction()
                return _external_operation_from_json(serialized)
            except sqlite3.IntegrityError:
                await self._rollback()
                raise RecoveryStateConflictError from None
            except BaseException:
                await self._rollback()
                raise

    async def get_external_operation(
        self, operation_id: str
    ) -> ExternalOperation | None:
        async with self._lock:
            self._ensure_open()
            operation = await self._read_external_operation(operation_id)
            if operation is None:
                return None
            return _external_operation_from_json(_canonical_record_json(operation))

    async def list_unresolved_external_operations(
        self, run_id: str
    ) -> tuple[ExternalOperation, ...]:
        async with self._lock:
            self._ensure_open()
            async with self._connection.execute(
                """
                SELECT data_json FROM external_operations
                WHERE run_id = ? AND status = 'started'
                ORDER BY turn, operation_kind, operation_id
                """,
                (run_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(
                _external_operation_from_json(cast(str, row[0])) for row in rows
            )

    @_context_free_recovery_errors
    async def transition_external_operation(
        self,
        *,
        expected: ExternalOperation,
        updated: ExternalOperation,
        lease: Lease,
        now: datetime,
    ) -> ExternalOperation:
        expected_json = _canonical_record_json(expected)
        updated_json = _canonical_record_json(updated)
        async with self._lock:
            self._ensure_open()
            try:
                await self._begin_immediate("SQLite recovery operation conflict")
                await self._check_recovery_run_session(
                    expected.run_id, expected.session_id
                )
                await self._check_recovery_lease(
                    lease,
                    now=now,
                    run_id=expected.run_id,
                    lease_generation=expected.lease_generation,
                )
                if not _valid_operation_transition(expected, updated):
                    raise RecoveryStateConflictError
                existing = await self._read_external_operation(
                    expected.operation_id
                )
                if existing is None:
                    raise RecoveryStateConflictError
                existing_json = _canonical_record_json(existing)
                if existing_json == updated_json:
                    await self._commit_transaction()
                    return _external_operation_from_json(updated_json)
                if existing_json != expected_json:
                    raise RecoveryStateConflictError
                result = await self._connection.execute(
                    """
                    UPDATE external_operations SET status = ?, data_json = ?
                    WHERE operation_id = ? AND status = 'started' AND data_json = ?
                    """,
                    (
                        updated.status.value,
                        updated_json,
                        expected.operation_id,
                        expected_json,
                    ),
                )
                if result.rowcount != 1:
                    raise RecoveryStateConflictError
                await self._commit_transaction()
                return _external_operation_from_json(updated_json)
            except sqlite3.IntegrityError:
                await self._rollback()
                raise RecoveryStateConflictError from None
            except BaseException:
                await self._rollback()
                raise

    @_context_free_recovery_errors
    async def put_run_checkpoint(
        self,
        checkpoint: RunCheckpoint,
        *,
        expected: RunCheckpoint | None,
        lease: Lease,
        now: datetime,
    ) -> RunCheckpoint:
        checkpoint_json = _canonical_record_json(checkpoint)
        expected_json = (
            None if expected is None else _canonical_record_json(expected)
        )
        async with self._lock:
            self._ensure_open()
            try:
                await self._begin_immediate("SQLite recovery checkpoint conflict")
                await self._check_recovery_run_session(
                    checkpoint.run_id, checkpoint.session_id
                )
                await self._check_recovery_lease(
                    lease,
                    now=now,
                    run_id=checkpoint.run_id,
                    lease_generation=lease.generation,
                )
                existing = await self._read_run_checkpoint(checkpoint.run_id)
                existing_json = (
                    None
                    if existing is None
                    else _canonical_record_json(existing)
                )
                if existing_json == checkpoint_json:
                    if not _valid_checkpoint_replay_shape(checkpoint, expected):
                        raise RecoveryStateConflictError
                    await self._check_checkpoint_operation(checkpoint, lease)
                    await self._commit_transaction()
                    return _checkpoint_from_json(checkpoint_json)
                if expected is None:
                    if existing is not None or checkpoint.checkpoint_version != 1:
                        raise RecoveryStateConflictError
                    await self._check_checkpoint_operation(checkpoint, lease)
                    await self._connection.execute(
                        """
                        INSERT INTO run_checkpoints(
                            run_id, session_id, checkpoint_version, turn, phase,
                            operation_id, data_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            checkpoint.run_id,
                            checkpoint.session_id,
                            checkpoint.checkpoint_version,
                            checkpoint.turn,
                            checkpoint.phase.value,
                            checkpoint.operation_id,
                            checkpoint_json,
                        ),
                    )
                else:
                    if (
                        existing_json != expected_json
                        or checkpoint.run_id != expected.run_id
                        or checkpoint.session_id != expected.session_id
                        or checkpoint.checkpoint_version
                        != expected.checkpoint_version + 1
                    ):
                        raise RecoveryStateConflictError
                    await self._check_checkpoint_operation(checkpoint, lease)
                    result = await self._connection.execute(
                        """
                        UPDATE run_checkpoints SET
                            checkpoint_version = ?, turn = ?, phase = ?,
                            operation_id = ?, data_json = ?
                        WHERE run_id = ? AND data_json = ?
                        """,
                        (
                            checkpoint.checkpoint_version,
                            checkpoint.turn,
                            checkpoint.phase.value,
                            checkpoint.operation_id,
                            checkpoint_json,
                            checkpoint.run_id,
                            expected_json,
                        ),
                    )
                    if result.rowcount != 1:
                        raise RecoveryStateConflictError
                await self._commit_transaction()
                return _checkpoint_from_json(checkpoint_json)
            except sqlite3.IntegrityError:
                await self._rollback()
                raise RecoveryStateConflictError from None
            except BaseException:
                await self._rollback()
                raise

    async def get_run_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        async with self._lock:
            self._ensure_open()
            checkpoint = await self._read_run_checkpoint(run_id)
            if checkpoint is None:
                return None
            return _checkpoint_from_json(_canonical_record_json(checkpoint))

    @_context_free_recovery_errors
    async def create_reconciliation_request(
        self, request: ReconciliationRequest
    ) -> ReconciliationRequest:
        serialized = _canonical_record_json(request)
        async with self._lock:
            self._ensure_open()
            try:
                await self._begin_immediate("SQLite reconciliation conflict")
                await self._check_recovery_run_session(
                    request.run_id, request.session_id
                )
                if request.status is not ReconciliationStatus.PENDING:
                    raise RecoveryStateConflictError
                if request.operation_id is not None:
                    operation = await self._read_external_operation(
                        request.operation_id
                    )
                    if operation is None or (
                        operation.run_id != request.run_id
                        or operation.session_id != request.session_id
                    ):
                        raise RecoveryStateConflictError
                existing = await self._read_reconciliation_request(
                    request.request_id
                )
                if existing is not None:
                    if _canonical_record_json(existing) != serialized:
                        raise RecoveryStateConflictError
                    await self._commit_transaction()
                    return _reconciliation_request_from_json(serialized)
                await self._connection.execute(
                    """
                    INSERT INTO reconciliation_requests(
                        request_id, session_id, run_id, operation_id, status, data_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.request_id,
                        request.session_id,
                        request.run_id,
                        request.operation_id,
                        request.status.value,
                        serialized,
                    ),
                )
                await self._commit_transaction()
                return _reconciliation_request_from_json(serialized)
            except sqlite3.IntegrityError:
                await self._rollback()
                raise RecoveryStateConflictError from None
            except BaseException:
                await self._rollback()
                raise

    async def get_reconciliation_request(
        self, request_id: str
    ) -> ReconciliationRequest | None:
        async with self._lock:
            self._ensure_open()
            request = await self._read_reconciliation_request(request_id)
            if request is None:
                return None
            return _reconciliation_request_from_json(_canonical_record_json(request))

    async def list_pending_reconciliation_requests(
        self, run_id: str
    ) -> tuple[ReconciliationRequest, ...]:
        async with self._lock:
            self._ensure_open()
            async with self._connection.execute(
                """
                SELECT data_json FROM reconciliation_requests
                WHERE run_id = ? AND status = 'pending'
                ORDER BY request_id
                """,
                (run_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(
                _reconciliation_request_from_json(cast(str, row[0]))
                for row in rows
            )

    @_context_free_recovery_errors
    async def resolve_reconciliation_request(
        self,
        *,
        expected: ReconciliationRequest,
        resolved: ReconciliationRequest,
        event: EventEnvelope,
    ) -> ReconciliationRequest:
        expected_json = _canonical_record_json(expected)
        resolved_json = _canonical_record_json(resolved)
        async with self._lock:
            self._ensure_open()
            try:
                await self._begin_immediate("SQLite reconciliation conflict")
                await self._check_recovery_run_session(
                    expected.run_id, expected.session_id
                )
                if not _valid_reconciliation_resolution(expected, resolved, event):
                    raise RecoveryStateConflictError
                current = await self._read_reconciliation_request(
                    expected.request_id
                )
                if current is None:
                    raise RecoveryStateConflictError
                current_json = _canonical_record_json(current)
                if current_json == resolved_json:
                    stored_event = await self._read_event_by_id(event.event_id)
                    if stored_event != event:
                        raise RecoveryStateConflictError
                    await self._commit_transaction()
                    return _reconciliation_request_from_json(resolved_json)
                if current_json != expected_json:
                    raise RecoveryStateConflictError
                if event.sequence <= 0:
                    raise RecoveryStateConflictError
                try:
                    await self._insert_event(event)
                except ValueError:
                    raise RecoveryStateConflictError from None
                result = await self._connection.execute(
                    """
                    UPDATE reconciliation_requests SET status = ?, data_json = ?
                    WHERE request_id = ? AND status = 'pending' AND data_json = ?
                    """,
                    (
                        resolved.status.value,
                        resolved_json,
                        expected.request_id,
                        expected_json,
                    ),
                )
                if result.rowcount != 1:
                    raise RecoveryStateConflictError
                await self._commit_transaction()
                return _reconciliation_request_from_json(resolved_json)
            except sqlite3.IntegrityError:
                await self._rollback()
                raise RecoveryStateConflictError from None
            except BaseException:
                await self._rollback()
                raise

    async def latest_cursor(self) -> int:
        async with self._lock:
            self._ensure_open()
            return await self._last_cursor()

    async def acquire_lease(
        self, *, run_id: str, owner: str, now: datetime, expires_at: datetime
    ) -> Lease:
        proposed = Lease(
            run_id=run_id,
            owner=owner,
            generation=1,
            acquired_at=now,
            renewed_at=now,
            expires_at=expires_at,
        )
        async with self._lock:
            self._ensure_open()
            try:
                await self._begin_immediate("SQLite lease acquisition conflict")
                current = await self._read_lease(proposed.run_id)
                if (
                    current is not None
                    and not current.released
                    and current.lease.expires_at > proposed.acquired_at
                ):
                    raise LeaseHeldError
                generation = 1 if current is None else current.lease.generation + 1
                acquired = proposed.model_copy(update={"generation": generation})
                await self._connection.execute(
                    """
                    INSERT INTO leases(
                        run_id, owner, generation, acquired_at, renewed_at, expires_at,
                        released
                    ) VALUES (?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(run_id) DO UPDATE SET
                        owner = excluded.owner,
                        generation = excluded.generation,
                        acquired_at = excluded.acquired_at,
                        renewed_at = excluded.renewed_at,
                        expires_at = excluded.expires_at,
                        released = 0
                    """,
                    _lease_values(acquired),
                )
                await self._commit_transaction()
                return acquired.model_copy()
            except BaseException:
                await self._rollback()
                raise

    async def renew_lease(
        self, lease: Lease, *, now: datetime, expires_at: datetime
    ) -> Lease:
        async with self._lock:
            self._ensure_open()
            try:
                await self._begin_immediate("SQLite lease renewal conflict")
                current = await self._read_lease(lease.run_id)
                if (
                    current is None
                    or current.released
                    or current.lease.owner != lease.owner
                    or current.lease.generation != lease.generation
                    or current.lease.expires_at <= now
                    or now < current.lease.renewed_at
                    or expires_at < current.lease.expires_at
                ):
                    raise LeaseLostError
                renewed = Lease(
                    run_id=current.lease.run_id,
                    owner=current.lease.owner,
                    generation=current.lease.generation,
                    acquired_at=current.lease.acquired_at,
                    renewed_at=now,
                    expires_at=expires_at,
                )
                result = await self._connection.execute(
                    """
                    UPDATE leases SET renewed_at = ?, expires_at = ?
                    WHERE run_id = ? AND owner = ? AND generation = ?
                    """,
                    (
                        canonical_lease_timestamp(renewed.renewed_at),
                        canonical_lease_timestamp(renewed.expires_at),
                        renewed.run_id,
                        renewed.owner,
                        renewed.generation,
                    ),
                )
                if result.rowcount != 1:
                    raise LeaseLostError
                await self._commit_transaction()
                return renewed.model_copy()
            except BaseException:
                await self._rollback()
                raise

    async def release_lease(self, lease: Lease) -> None:
        async with self._lock:
            self._ensure_open()
            try:
                await self._begin_immediate("SQLite lease release conflict")
                current = await self._read_lease(lease.run_id)
                if (
                    current is None
                    or current.released
                    or not _lease_identity_matches(current.lease, lease)
                ):
                    raise LeaseLostError
                result = await self._connection.execute(
                    """
                    UPDATE leases SET released = 1
                    WHERE run_id = ? AND owner = ? AND generation = ? AND released = 0
                    """,
                    (lease.run_id, lease.owner, lease.generation),
                )
                if result.rowcount != 1:
                    raise LeaseLostError
                await self._commit_transaction()
            except BaseException:
                await self._rollback()
                raise

    async def assert_current_lease(self, lease: Lease, *, now: datetime) -> None:
        async with self._lock:
            self._ensure_open()
            try:
                await self._begin_immediate("SQLite lease assertion conflict")
                current = await self._read_lease(lease.run_id)
                if (
                    current is None
                    or current.released
                    or current.lease.owner != lease.owner
                    or current.lease.generation != lease.generation
                    or current.lease.expires_at <= now
                ):
                    raise LeaseLostError
                await self._commit_transaction()
            except BaseException:
                await self._rollback()
                raise

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            self._ensure_open()
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                await self._connection.execute(
                    "DELETE FROM reconciliation_requests WHERE session_id = ?",
                    (session_id,),
                )
                await self._connection.execute(
                    "DELETE FROM run_checkpoints WHERE session_id = ?",
                    (session_id,),
                )
                await self._connection.execute(
                    "DELETE FROM external_operations WHERE session_id = ?",
                    (session_id,),
                )
                await self._connection.execute(
                    """
                    DELETE FROM leases WHERE run_id IN (
                        SELECT entity_id FROM snapshots
                        WHERE kind = 'run' AND session_id = ?
                    )
                    """,
                    (session_id,),
                )
                await self._connection.execute(
                    "DELETE FROM events WHERE session_id = ?",
                    (session_id,),
                )
                await self._connection.execute(
                    "DELETE FROM snapshots WHERE session_id = ?",
                    (session_id,),
                )
                await self._connection.execute(
                    "DELETE FROM idempotency_records WHERE session_id = ?",
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
        await self._await_cleanup(rollback)

    async def _commit_transaction(self) -> None:
        commit = asyncio.create_task(self._connection.commit())
        await self._await_cleanup(commit)

    async def _begin_immediate(self, message: str) -> None:
        async def begin() -> None:
            await self._connection.execute("BEGIN IMMEDIATE")

        await _with_busy_retry(
            begin,
            deadline=monotonic() + _OPEN_RETRY_SECONDS,
            message=message,
        )

    @staticmethod
    async def _await_cleanup(cleanup: asyncio.Task[None]) -> None:
        cancelled: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                if cancelled is None:
                    cancelled = error
            except BaseException:
                break
        cleanup.result()
        if cancelled is not None:
            raise cancelled

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

    async def _read_idempotency(
        self, scope: str, key: str
    ) -> IdempotencyRecord | None:
        async with self._connection.execute(
            """
            SELECT scope, key, request_fingerprint, session_id, result_json
            FROM idempotency_records WHERE scope = ? AND key = ?
            """,
            (scope, key),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return record_from_stored_json(
            scope=row[0],
            key=row[1],
            request_fingerprint=row[2],
            session_id=row[3],
            result_json=row[4],
        )

    async def _read_lease(self, run_id: str) -> _StoredLease | None:
        async with self._connection.execute(
            """
            SELECT run_id, owner, generation, acquired_at, renewed_at, expires_at,
                   released
            FROM leases WHERE run_id = ?
            """,
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _StoredLease(_lease_from_row(row), bool(row[6]))

    async def _check_recovery_lease(
        self,
        lease: Lease,
        *,
        now: datetime,
        run_id: str,
        lease_generation: int,
    ) -> None:
        current = await self._read_lease(run_id)
        if (
            current is None
            or current.released
            or current.lease.owner != lease.owner
            or current.lease.generation != lease.generation
            or current.lease.expires_at <= now
            or lease.run_id != run_id
            or lease_generation != lease.generation
        ):
            raise RecoveryStateConflictError

    async def _check_recovery_run_session(
        self, run_id: str, session_id: str
    ) -> None:
        async with self._connection.execute(
            """
            SELECT session_id, version, data_json FROM snapshots
            WHERE kind = 'run' AND entity_id = ?
            """,
            (run_id,),
        ) as cursor:
            snapshot_row = await cursor.fetchone()
        if snapshot_row is None or cast(str, snapshot_row[0]) != session_id:
            raise RecoveryStateConflictError
        try:
            snapshot_data = _strict_json_object(cast(str, snapshot_row[2]))
            run = RunSnapshot.model_validate(snapshot_data)
        except (TypeError, ValueError):
            raise RecoveryStateConflictError from None
        if (
            run.run_id != run_id
            or run.session_id != session_id
            or run.version != cast(int, snapshot_row[1])
        ):
            raise RecoveryStateConflictError
        async with self._connection.execute(
            """
            SELECT session_id FROM external_operations WHERE run_id = ?
            UNION ALL
            SELECT session_id FROM run_checkpoints WHERE run_id = ?
            UNION ALL
            SELECT session_id FROM reconciliation_requests WHERE run_id = ?
            """,
            (run_id, run_id, run_id),
        ) as cursor:
            rows = await cursor.fetchall()
        if any(cast(str, row[0]) != session_id for row in rows):
            raise RecoveryStateConflictError

    async def _read_external_operation(
        self, operation_id: str
    ) -> ExternalOperation | None:
        async with self._connection.execute(
            "SELECT data_json FROM external_operations WHERE operation_id = ?",
            (operation_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _external_operation_from_json(cast(str, row[0]))

    async def _read_run_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        async with self._connection.execute(
            "SELECT data_json FROM run_checkpoints WHERE run_id = ?",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _checkpoint_from_json(cast(str, row[0]))

    async def _read_reconciliation_request(
        self, request_id: str
    ) -> ReconciliationRequest | None:
        async with self._connection.execute(
            "SELECT data_json FROM reconciliation_requests WHERE request_id = ?",
            (request_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _reconciliation_request_from_json(cast(str, row[0]))

    async def _read_event_by_id(self, event_id: str) -> EventEnvelope | None:
        async with self._connection.execute(
            """
            SELECT cursor, event_id, schema_version, type, session_id, run_id,
                   sequence, payload_json, occurred_at
            FROM events WHERE event_id = ?
            """,
            (event_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._stored_event(row).event

    async def _check_checkpoint_operation(
        self, checkpoint: RunCheckpoint, lease: Lease
    ) -> None:
        if checkpoint.operation_id is None:
            return
        operation = await self._read_external_operation(checkpoint.operation_id)
        if operation is None:
            raise RecoveryStateConflictError
        expected_type: type[ModelCallOperation] | type[ToolCallOperation]
        if checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT:
            expected_type = ModelCallOperation
        elif checkpoint.phase is RunCheckpointPhase.TOOL_IN_FLIGHT:
            expected_type = ToolCallOperation
        else:
            raise RecoveryStateConflictError
        if (
            not isinstance(operation, expected_type)
            or operation.run_id != checkpoint.run_id
            or operation.session_id != checkpoint.session_id
            or operation.lease_generation != lease.generation
        ):
            raise RecoveryStateConflictError

    async def _insert_idempotency(self, record: IdempotencyRecord) -> None:
        await self._connection.execute(
            """
            INSERT INTO idempotency_records(
                scope, key, request_fingerprint, session_id, result_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.scope,
                record.key,
                record.request_fingerprint,
                record.session_id,
                canonical_result_json(record),
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
        deadline = monotonic() + _OPEN_RETRY_SECONDS

        async def begin() -> None:
            await connection.execute("BEGIN IMMEDIATE")

        await cls._migration_checkpoint("migration-lock-requested")
        await _with_busy_retry(
            begin,
            deadline=deadline,
            message="SQLite open conflict",
        )
        try:
            state = await cls._discover_schema_state(connection)
            await cls._migration_checkpoint(
                f"migration-schema-discovered-{state.value}"
            )
            empty_database = state is _SchemaState.EMPTY
            if empty_database:
                migration_one = resources.files("agent_sdk.storage").joinpath(
                    "migrations", "0001_initial.sql"
                )
                await _execute_script_statements(
                    connection, migration_one.read_text(encoding="utf-8")
                )
                await connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, datetime.now(UTC).isoformat()),
                )
                state = _SchemaState.V1

            if state is _SchemaState.V1:
                await cls._validate_schema(connection, expected_version=1)
                migration_two = resources.files("agent_sdk.storage").joinpath(
                    "migrations", "0002_idempotency.sql"
                )

                async def after_migration_statement(index: int) -> None:
                    await cls._migration_checkpoint(f"migration-2-statement-{index}")

                await _execute_script_statements(
                    connection,
                    migration_two.read_text(encoding="utf-8"),
                    after_statement=after_migration_statement,
                )
                if not empty_database:
                    await cls._validate_and_backfill_v1_projections(connection)
                await connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, datetime.now(UTC).isoformat()),
                )
                await cls._migration_checkpoint("migration-2-version-inserted")
                state = _SchemaState.V2

            if state is _SchemaState.V2:
                await cls._validate_schema(connection, expected_version=2)
                await cls._validate_v2_projections(connection)
                await cls._migration_checkpoint("migration-2-final-validation")
                migration_three = resources.files("agent_sdk.storage").joinpath(
                    "migrations", "0003_leases.sql"
                )

                async def after_migration_three_statement(index: int) -> None:
                    await cls._migration_checkpoint(f"migration-3-statement-{index}")

                await _execute_script_statements(
                    connection,
                    migration_three.read_text(encoding="utf-8"),
                    after_statement=after_migration_three_statement,
                )
                await connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, datetime.now(UTC).isoformat()),
                )
                await cls._migration_checkpoint("migration-3-version-inserted")
                state = _SchemaState.V3

            await cls._validate_schema(connection, expected_version=3)
            await cls._validate_v2_projections(connection)
            await cls._validate_v3_rows(connection)
            await cls._migration_checkpoint("migration-3-final-validation")
            commit_task = asyncio.create_task(connection.commit())
            await cls._await_cleanup(commit_task)
        except BaseException:
            rollback_task = asyncio.create_task(connection.rollback())
            await cls._await_cleanup(rollback_task)
            raise

    @staticmethod
    async def _migration_checkpoint(stage: str) -> None:
        del stage

    @staticmethod
    async def _configure_connection(connection: aiosqlite.Connection) -> None:
        try:
            await connection.execute(f"PRAGMA busy_timeout={_OPEN_BUSY_TIMEOUT_MS}")
        except sqlite3.Error as error:
            raise RuntimeError("failed to configure SQLite busy_timeout") from error

        try:
            await connection.execute("PRAGMA foreign_keys=ON")
            async with connection.execute("PRAGMA foreign_keys") as cursor:
                foreign_keys = await cursor.fetchone()
        except sqlite3.Error as error:
            raise RuntimeError("failed to enable SQLite foreign_keys") from error
        if foreign_keys != (1,):
            raise RuntimeError("failed to enable SQLite foreign_keys")

        async def enable_wal() -> tuple[Any, ...] | None:
            async with connection.execute("PRAGMA journal_mode=WAL") as cursor:
                return cast(tuple[Any, ...] | None, await cursor.fetchone())

        try:
            journal_mode = await _with_busy_retry(
                enable_wal,
                deadline=monotonic() + _OPEN_RETRY_SECONDS,
                message="SQLite journal_mode open conflict",
            )
        except sqlite3.Error as error:
            raise RuntimeError("failed to enable SQLite journal_mode=WAL") from error
        if journal_mode is None or cast(str, journal_mode[0]).lower() != "wal":
            raise RuntimeError("failed to enable SQLite journal_mode=WAL")

    @classmethod
    async def _discover_schema_state(
        cls, connection: aiosqlite.Connection
    ) -> _SchemaState:
        try:
            async with connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ) as cursor:
                rows = await cursor.fetchall()
            table_names = {cast(str, row[0]) for row in rows}
            if not table_names:
                return _SchemaState.EMPTY
            v1_tables = {"schema_migrations", "events", "snapshots"}
            v2_tables = {*v1_tables, "idempotency_records"}
            v3_tables = set(_EXPECTED_TABLE_INFO)
            frozen_table_names = frozenset(table_names)
            if frozen_table_names not in {
                frozenset(v1_tables),
                frozenset(v2_tables),
                frozenset(v3_tables),
            }:
                raise ValueError("incompatible database schema")
            async with connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ) as cursor:
                version_rows = await cursor.fetchall()
            versions = tuple(row[0] for row in version_rows)
            if table_names == v1_tables and versions == (1,):
                return _SchemaState.V1
            if table_names == v2_tables and versions == (1, 2):
                return _SchemaState.V2
            if table_names == v3_tables and versions == (1, 2, 3):
                return _SchemaState.V3
            raise ValueError("incompatible database schema version")
        except sqlite3.Error as error:
            raise ValueError("incompatible database schema") from error

    @classmethod
    async def _validate_schema(
        cls,
        connection: aiosqlite.Connection,
        *,
        expected_version: int,
    ) -> None:
        table_names: tuple[str, ...]
        if expected_version == 1:
            table_names = ("schema_migrations", "events", "snapshots")
        elif expected_version == 2:
            table_names = (
                "schema_migrations",
                "events",
                "snapshots",
                "idempotency_records",
            )
        else:
            table_names = tuple(_EXPECTED_TABLE_INFO)
        for table_name in table_names:
            expected_info = _EXPECTED_TABLE_INFO[table_name]
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
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ) as cursor:
                table_sql = await cursor.fetchone()
            if table_sql is None or _normalized_sql(cast(str, table_sql[0])) != (
                _normalized_sql(_EXPECTED_TABLE_SQL[table_name])
            ):
                raise ValueError("incompatible database schema")

        async with connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ) as cursor:
            events_row = await cursor.fetchone()
        if events_row is None or "AUTOINCREMENT" not in cast(str, events_row[0]).upper():
            raise ValueError("incompatible database schema")

        expected_indexes = {
            name: value
            for name, value in _EXPECTED_INDEXES.items()
            if (
                expected_version == 3
                or (
                    expected_version == 2
                    and name
                    in {
                        "events_session_cursor",
                        "events_aggregate_sequence",
                        "snapshots_session",
                        "idempotency_records_session",
                    }
                )
                or (
                    expected_version == 1
                    and name
                    in {
                        "events_session_cursor",
                        "events_aggregate_sequence",
                        "snapshots_session",
                    }
                )
            )
        }
        indexes: dict[str, tuple[bool, tuple[str | None, ...]]] = {}
        indexed_tables = {
            1: ("events", "snapshots"),
            2: ("events", "snapshots", "idempotency_records"),
            3: (
                "events",
                "snapshots",
                "idempotency_records",
                "leases",
                "external_operations",
                "run_checkpoints",
                "reconciliation_requests",
            ),
        }[expected_version]
        for table_name in indexed_tables:
            async with connection.execute(f"PRAGMA index_list({table_name})") as cursor:
                index_rows = await cursor.fetchall()
            for index_row in index_rows:
                index_name = cast(str, index_row[1])
                if index_name.startswith("sqlite_autoindex_"):
                    continue
                if index_name not in expected_indexes:
                    raise ValueError("incompatible database schema")
                async with connection.execute(f"PRAGMA index_info({index_name})") as cursor:
                    column_rows = await cursor.fetchall()
                indexes[index_name] = (
                    bool(index_row[2]),
                    tuple(cast(str | None, column_row[2]) for column_row in column_rows),
                )
        if indexes != expected_indexes:
            raise ValueError("incompatible database schema")

        for index_name in expected_indexes:
            async with connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ) as cursor:
                index_sql = await cursor.fetchone()
            if index_sql is None or _normalized_sql(cast(str, index_sql[0])) != (
                _normalized_sql(_EXPECTED_INDEX_SQL[index_name])
            ):
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

        async with connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ) as cursor:
            versions = tuple(row[0] for row in await cursor.fetchall())
        expected_versions = tuple(range(1, expected_version + 1))
        if versions != expected_versions:
            raise ValueError("incompatible database schema version")

    @classmethod
    async def _validate_and_backfill_v1_projections(
        cls, connection: aiosqlite.Connection
    ) -> None:
        transformed = await _validated_v1_projection_transforms(connection)
        for kind, entity_id, data in transformed:
            result = await connection.execute(
                "UPDATE snapshots SET data_json = ? WHERE kind = ? AND entity_id = ?",
                (_canonical_json(data), kind, entity_id),
            )
            if result.rowcount != 1:
                raise ValueError("incompatible version-1 projection")
            await cls._migration_checkpoint(f"migration-2-backfill-{kind}-{entity_id}")

    @staticmethod
    async def _validate_v2_projections(connection: aiosqlite.Connection) -> None:
        await _validate_current_projection_rows(connection)

    @staticmethod
    async def _validate_v3_rows(connection: aiosqlite.Connection) -> None:
        async with connection.execute(
            """
            SELECT run_id, owner, generation, acquired_at, renewed_at, expires_at,
                   released
            FROM leases
            """
        ) as cursor:
            lease_rows = await cursor.fetchall()
        for row in lease_rows:
            try:
                lease = Lease.model_validate(
                    {
                        "run_id": row[0],
                        "owner": row[1],
                        "generation": row[2],
                        "acquired_at": row[3],
                        "renewed_at": row[4],
                        "expires_at": row[5],
                    }
                )
                if row[6] not in (0, 1):
                    raise ValueError("lease released state is invalid")
                canonical_timestamps = (
                    canonical_lease_timestamp(lease.acquired_at),
                    canonical_lease_timestamp(lease.renewed_at),
                    canonical_lease_timestamp(lease.expires_at),
                )
                if tuple(row[3:6]) != canonical_timestamps:
                    raise ValueError("lease timestamps are not canonical UTC")
            except ValueError as error:
                raise ValueError("incompatible lease row") from error

        await _validate_json_identity_rows(
            connection,
            table="external_operations",
            columns=(
                "operation_id",
                "operation_kind",
                "session_id",
                "run_id",
                "turn",
                "request_fingerprint",
                "provider_identity",
                "tool_identity",
                "lease_generation",
                "status",
            ),
        )
        await _validate_json_identity_rows(
            connection,
            table="run_checkpoints",
            columns=(
                "run_id",
                "session_id",
                "checkpoint_version",
                "turn",
                "phase",
                "operation_id",
            ),
        )
        await _validate_json_identity_rows(
            connection,
            table="reconciliation_requests",
            columns=("request_id", "session_id", "run_id", "operation_id", "status"),
        )
        async with connection.execute("PRAGMA foreign_key_check") as cursor:
            foreign_key_errors = await cursor.fetchall()
        if foreign_key_errors:
            raise ValueError("incompatible v3 foreign key rows")

        async with connection.execute(
            "SELECT entity_id, session_id FROM snapshots WHERE kind = 'run'"
        ) as cursor:
            run_snapshot_rows = await cursor.fetchall()
        authoritative_run_sessions = {
            cast(str, entity_id): cast(str, session_id)
            for entity_id, session_id in run_snapshot_rows
        }

        async with connection.execute(
            "SELECT operation_id, data_json FROM external_operations"
        ) as cursor:
            operation_rows = await cursor.fetchall()
        operations: dict[str, ExternalOperation] = {}
        recovery_run_sessions: dict[str, str] = {}
        for operation_id, data_json in operation_rows:
            try:
                operation = _external_operation_from_json(cast(str, data_json))
            except (TypeError, ValueError):
                raise ValueError("incompatible external operation row") from None
            if operation.operation_id != operation_id:
                raise ValueError("incompatible external operation row")
            if (
                authoritative_run_sessions.get(operation.run_id)
                != operation.session_id
            ):
                raise ValueError("incompatible recovery run snapshot ownership")
            owner_session = recovery_run_sessions.setdefault(
                operation.run_id, operation.session_id
            )
            if owner_session != operation.session_id:
                raise ValueError("incompatible recovery run ownership")
            operations[operation.operation_id] = operation

        async with connection.execute(
            "SELECT run_id, data_json FROM run_checkpoints"
        ) as cursor:
            checkpoint_rows = await cursor.fetchall()
        for run_id, data_json in checkpoint_rows:
            try:
                checkpoint = _checkpoint_from_json(cast(str, data_json))
            except (TypeError, ValueError):
                raise ValueError("incompatible run checkpoint row") from None
            if checkpoint.run_id != run_id:
                raise ValueError("incompatible run checkpoint row")
            if (
                authoritative_run_sessions.get(checkpoint.run_id)
                != checkpoint.session_id
            ):
                raise ValueError("incompatible recovery run snapshot ownership")
            owner_session = recovery_run_sessions.setdefault(
                checkpoint.run_id, checkpoint.session_id
            )
            if owner_session != checkpoint.session_id:
                raise ValueError("incompatible recovery run ownership")
            if checkpoint.operation_id is not None:
                checkpoint_operation = operations.get(checkpoint.operation_id)
                if checkpoint_operation is None or (
                    checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
                    and not isinstance(checkpoint_operation, ModelCallOperation)
                ) or (
                    checkpoint.phase is RunCheckpointPhase.TOOL_IN_FLIGHT
                    and not isinstance(checkpoint_operation, ToolCallOperation)
                ):
                    raise ValueError("incompatible checkpoint operation row")

        async with connection.execute(
            "SELECT request_id, data_json FROM reconciliation_requests"
        ) as cursor:
            reconciliation_rows = await cursor.fetchall()
        for request_id, data_json in reconciliation_rows:
            try:
                request = _reconciliation_request_from_json(cast(str, data_json))
            except (TypeError, ValueError):
                raise ValueError("incompatible reconciliation request row") from None
            if request.request_id != request_id:
                raise ValueError("incompatible reconciliation request row")
            if (
                authoritative_run_sessions.get(request.run_id)
                != request.session_id
            ):
                raise ValueError("incompatible recovery run snapshot ownership")
            owner_session = recovery_run_sessions.setdefault(
                request.run_id, request.session_id
            )
            if owner_session != request.session_id:
                raise ValueError("incompatible recovery run ownership")
            if request.status is ReconciliationStatus.RESOLVED:
                assert request.resolution is not None
                async with connection.execute(
                    """
                    SELECT cursor, event_id, schema_version, type, session_id, run_id,
                           sequence, payload_json, occurred_at
                    FROM events WHERE event_id = ?
                    """,
                    (request.resolution.event_id,),
                ) as cursor:
                    event_row = await cursor.fetchone()
                if event_row is None:
                    raise ValueError("incompatible reconciliation audit event")
                try:
                    event = SQLiteStore._stored_event(event_row).event
                    pending = request.model_copy(
                        update={
                            "status": ReconciliationStatus.PENDING,
                            "resolution": None,
                        }
                    )
                except (TypeError, ValueError):
                    raise ValueError("incompatible reconciliation audit event") from None
                if not _valid_reconciliation_resolution(pending, request, event):
                    raise ValueError("incompatible reconciliation audit event")


class _SnapshotRow(NamedTuple):
    kind: str
    entity_id: str
    session_id: str
    version: int
    data_json: str


class _EventRow(NamedTuple):
    cursor: int
    event_id: str
    session_id: str
    run_id: str | None
    sequence: int
    type: str
    schema_version: int
    occurred_at: str
    payload_json: str


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _strict_json_object(value: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("stored JSON contains a duplicate key")
            result[key] = item
        return result

    decoded = json.loads(
        value,
        object_pairs_hook=object_pairs,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(decoded, dict):
        raise ValueError("stored JSON must be an object")
    return cast(dict[str, Any], decoded)


async def _validate_json_identity_rows(
    connection: aiosqlite.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
) -> None:
    query = f"SELECT {', '.join(columns)}, data_json FROM {table}"
    async with connection.execute(query) as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        try:
            data = _strict_json_object(cast(str, row[-1]))
        except (TypeError, ValueError) as error:
            raise ValueError(f"incompatible {table} JSON") from error
        if any(
            column not in data
            or type(data[column]) is not type(row[index])
            or data[column] != row[index]
            for index, column in enumerate(columns)
        ):
            raise ValueError(f"incompatible {table} row identity")


async def _snapshot_rows(connection: aiosqlite.Connection) -> tuple[_SnapshotRow, ...]:
    async with connection.execute(
        """
        SELECT kind, entity_id, session_id, version, data_json
        FROM snapshots ORDER BY kind, entity_id
        """
    ) as cursor:
        rows = await cursor.fetchall()
    result: list[_SnapshotRow] = []
    for row in rows:
        if (
            not isinstance(row[0], str)
            or not isinstance(row[1], str)
            or not isinstance(row[2], str)
            or not isinstance(row[3], int)
            or not isinstance(row[4], str)
            or row[3] <= 0
        ):
            raise ValueError("incompatible projection row")
        result.append(_SnapshotRow(*row))
    return tuple(result)


async def _event_rows(connection: aiosqlite.Connection) -> tuple[_EventRow, ...]:
    async with connection.execute(
        """
        SELECT cursor, event_id, session_id, run_id, sequence, type,
               schema_version, occurred_at, payload_json
        FROM events ORDER BY cursor
        """
    ) as cursor:
        rows = await cursor.fetchall()
    result: list[_EventRow] = []
    for row in rows:
        if (
            not isinstance(row[0], int)
            or not isinstance(row[1], str)
            or not isinstance(row[2], str)
            or (row[3] is not None and not isinstance(row[3], str))
            or not isinstance(row[4], int)
            or not isinstance(row[5], str)
            or not isinstance(row[6], int)
            or not isinstance(row[7], str)
            or not isinstance(row[8], str)
            or row[0] <= 0
            or row[4] <= 0
        ):
            raise ValueError("incompatible event row")
        result.append(_EventRow(*row))
    return tuple(result)


_V1_SESSION_FIELDS = {"session_id", "status", "workspaces", "version"}
_V1_RUN_FIELDS = {
    "run_id",
    "session_id",
    "agent_revision",
    "status",
    "user_input",
    "version",
    "output_text",
    "usage",
    "parent_run_id",
    "workflow_run_id",
    "workflow_node_id",
    "task_envelope",
    "error",
}
_V1_WORKFLOW_FIELDS = {
    "workflow_run_id",
    "session_id",
    "status",
    "workflow",
    "nodes",
    "version",
    "output_text",
    "usage",
    "error",
}


async def _validated_v1_projection_transforms(
    connection: aiosqlite.Connection,
) -> tuple[tuple[str, str, dict[str, Any]], ...]:
    from agent_sdk.context.models import ContextCapsule, ContextView
    from agent_sdk.evaluation.models import EvaluationResult
    from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot, SessionStatus
    from agent_sdk.workflow.models import (
        WorkflowNodeSnapshot,
        WorkflowRunSnapshot,
        WorkflowRunStatus,
    )

    try:
        rows = await _snapshot_rows(connection)
        decoded = {row: _strict_json_object(row.data_json) for row in rows}
        session_rows = {row.entity_id: row for row in rows if row.kind == "session"}
        sessions: dict[str, SessionSnapshot] = {}
        for session_id, row in session_rows.items():
            data = decoded[row]
            if set(data) != _V1_SESSION_FIELDS or data.get("status") != "active":
                raise ValueError("incompatible version-1 session projection")
            session = SessionSnapshot.model_validate(data)
            if (
                session.status is not SessionStatus.ACTIVE
                or session.session_id != session_id
                or row.session_id != session_id
                or row.version != session.version
            ):
                raise ValueError("incompatible version-1 session identity")
            sessions[session_id] = session

        runs: dict[str, RunSnapshot] = {}
        workflows: dict[str, WorkflowRunSnapshot] = {}
        nodes: dict[str, WorkflowNodeSnapshot] = {}
        capsules: dict[str, tuple[str, ContextCapsule]] = {}
        views: dict[str, ContextView] = {}
        evaluations: dict[str, EvaluationResult] = {}
        transformed: dict[tuple[str, str], dict[str, Any]] = {}

        for row in rows:
            if row.session_id not in sessions:
                raise ValueError("incompatible version-1 orphan projection")
            data = decoded[row]
            if row.kind == "session":
                continue
            if row.kind == "run":
                if set(data) != _V1_RUN_FIELDS:
                    raise ValueError("incompatible version-1 run projection")
                current = {
                    **data,
                    "execution_compatibility": "legacy_unknown",
                    "execution_descriptor": None,
                    "tool_results": [],
                }
                run = RunSnapshot.model_validate(current)
                if (
                    run.run_id != row.entity_id
                    or run.session_id != row.session_id
                    or run.version != row.version
                ):
                    raise ValueError("incompatible version-1 run identity")
                runs[run.run_id] = run
                transformed[(row.kind, row.entity_id)] = current
            elif row.kind == "workflow":
                if set(data) != _V1_WORKFLOW_FIELDS:
                    raise ValueError("incompatible version-1 workflow projection")
                current = {
                    **data,
                    "execution_compatibility": "legacy_unknown",
                    "execution_descriptor": None,
                }
                workflow = WorkflowRunSnapshot.model_validate(current)
                if (
                    workflow.workflow_run_id != row.entity_id
                    or workflow.session_id != row.session_id
                    or workflow.version != row.version
                ):
                    raise ValueError("incompatible version-1 workflow identity")
                workflows[workflow.workflow_run_id] = workflow
                transformed[(row.kind, row.entity_id)] = current
            elif row.kind == "workflow_node":
                node = WorkflowNodeSnapshot.model_validate(data)
                if (
                    node.entity_id != row.entity_id
                    or node.session_id != row.session_id
                    or node.version != row.version
                ):
                    raise ValueError("incompatible version-1 workflow node identity")
                nodes[node.entity_id] = node
            elif row.kind == "context_capsule":
                if set(data) != {"session_id", "capsule"} or data["session_id"] != row.session_id:
                    raise ValueError("incompatible version-1 context capsule")
                capsule = ContextCapsule.model_validate(data["capsule"])
                if row.version != 1:
                    raise ValueError("incompatible version-1 context capsule version")
                capsules[row.entity_id] = (row.session_id, capsule)
            elif row.kind == "context_view":
                view = ContextView.model_validate(data)
                if (
                    view.view_id != row.entity_id
                    or view.session_id != row.session_id
                    or row.version != 1
                ):
                    raise ValueError("incompatible version-1 context view identity")
                views[view.view_id] = view
            elif row.kind == "evaluation":
                evaluation = EvaluationResult.model_validate(data)
                if (
                    evaluation.evaluation_id != row.entity_id
                    or evaluation.session_id != row.session_id
                    or evaluation.record_version != row.version
                ):
                    raise ValueError("incompatible version-1 evaluation identity")
                evaluations[evaluation.evaluation_id] = evaluation
            else:
                raise ValueError("incompatible version-1 snapshot kind")

        for node in nodes.values():
            owner_workflow = workflows.get(node.workflow_run_id)
            if owner_workflow is None or owner_workflow.session_id != node.session_id:
                raise ValueError("incompatible version-1 workflow node owner")
            nested = next(
                (item for item in owner_workflow.nodes if item.entity_id == node.entity_id),
                None,
            )
            if nested != node:
                raise ValueError("incompatible version-1 workflow node projection")
        for workflow in workflows.values():
            for nested in workflow.nodes:
                if nodes.get(nested.entity_id) != nested:
                    raise ValueError("incompatible version-1 workflow node projection")

        for view in views.values():
            if view.capsule_id is not None:
                capsule_ref = capsules.get(view.capsule_id)
                if capsule_ref is None or capsule_ref[0] != view.session_id:
                    raise ValueError("incompatible version-1 context reference")
        for evaluation in evaluations.values():
            subject_run = runs.get(evaluation.subject_run_id)
            if subject_run is None or subject_run.session_id != evaluation.session_id:
                raise ValueError("incompatible version-1 evaluation subject")

        events = await _event_rows(connection)
        await _validate_v1_events(
            events=events,
            sessions=sessions,
            runs=runs,
            workflows=workflows,
            nodes=nodes,
            capsules=capsules,
            views=views,
            evaluations=evaluations,
        )

        active_runs: dict[str, list[str]] = {session_id: [] for session_id in sessions}
        active_workflows: dict[str, list[str]] = {
            session_id: [] for session_id in sessions
        }
        for run in runs.values():
            if run.status in {
                RunStatus.CREATED,
                RunStatus.RUNNING,
                RunStatus.WAITING_PERMISSION,
            }:
                active_runs[run.session_id].append(run.run_id)
        for workflow in workflows.values():
            if workflow.status is WorkflowRunStatus.RUNNING:
                active_workflows[workflow.session_id].append(workflow.workflow_run_id)
        for session_id, session in sessions.items():
            data = decoded[session_rows[session_id]]
            transformed[("session", session_id)] = {
                **data,
                "active_run_ids": sorted(active_runs[session_id]),
                "active_workflow_run_ids": sorted(active_workflows[session_id]),
            }

        return tuple(
            (kind, entity_id, transformed[(kind, entity_id)])
            for kind, entity_id in sorted(transformed)
        )
    except ValueError as error:
        if str(error).startswith("incompatible version-1"):
            raise
        raise ValueError("incompatible version-1 projection") from error
    except Exception as error:
        raise ValueError("incompatible version-1 projection") from error


async def _validate_v1_events(
    *,
    events: tuple[_EventRow, ...],
    sessions: Mapping[str, Any],
    runs: Mapping[str, Any],
    workflows: Mapping[str, Any],
    nodes: Mapping[str, Any],
    capsules: Mapping[str, tuple[str, Any]],
    views: Mapping[str, Any],
    evaluations: Mapping[str, Any],
) -> None:
    from agent_sdk.context.models import ContextBudget
    from agent_sdk.events.models import EventEnvelope
    from agent_sdk.runtime.models import RunSnapshot, RunStatus
    from agent_sdk.workflow.models import WorkflowNodeStatus, WorkflowRunStatus

    payloads: dict[str, dict[str, Any]] = {}
    event_ids: set[str] = set()
    events_by_id: dict[str, _EventRow] = {}
    by_run: dict[str, list[_EventRow]] = {}
    for row in events:
        payload = _strict_json_object(row.payload_json)
        EventEnvelope.model_validate(
            {
                "event_id": row.event_id,
                "schema_version": row.schema_version,
                "type": row.type,
                "session_id": row.session_id,
                "run_id": row.run_id,
                "sequence": row.sequence,
                "payload": payload,
                "occurred_at": row.occurred_at,
            }
        )
        if row.event_id in event_ids or row.session_id not in sessions:
            raise ValueError("incompatible version-1 event owner")
        event_ids.add(row.event_id)
        events_by_id[row.event_id] = row
        payloads[row.event_id] = payload
        if row.run_id is not None:
            by_run.setdefault(row.run_id, []).append(row)

    session_created: dict[str, list[_EventRow]] = {}
    for row in events:
        if row.type == "session.created":
            if row.run_id is not None or row.sequence != 1:
                raise ValueError("incompatible version-1 session event")
            payload = payloads[row.event_id]
            if (
                set(payload) != _V1_SESSION_FIELDS
                or payload.get("session_id") != row.session_id
                or payload.get("status") != "active"
            ):
                raise ValueError("incompatible version-1 session event payload")
            session_created.setdefault(row.session_id, []).append(row)
    if any(len(session_created.get(session_id, ())) != 1 for session_id in sessions):
        raise ValueError("incompatible version-1 session facts")

    for run_id, run in runs.items():
        aggregate = sorted(by_run.get(run_id, ()), key=lambda item: item.sequence)
        created = [item for item in aggregate if item.type == "run.created"]
        terminals = [
            item for item in aggregate if item.type in {"run.completed", "run.failed"}
        ]
        if len(created) != 1 or created[0].sequence != 1:
            raise ValueError("incompatible version-1 run start fact")
        created_payload = payloads[created[0].event_id]
        if set(created_payload) != _V1_RUN_FIELDS:
            raise ValueError("incompatible version-1 run start payload")
        migrated_payload = {
            **created_payload,
            "execution_compatibility": "legacy_unknown",
            "execution_descriptor": None,
            "tool_results": [],
        }
        created_run = RunSnapshot.model_validate(migrated_payload)
        if (
            created_run.status is not RunStatus.CREATED
            or created_run.run_id != run_id
            or created_run.session_id != run.session_id
            or created[0].session_id != run.session_id
        ):
            raise ValueError("incompatible version-1 run start payload")
        if run.status is RunStatus.COMPLETED:
            expected_terminal = "run.completed"
        elif run.status is RunStatus.FAILED:
            expected_terminal = "run.failed"
        else:
            expected_terminal = None
        if expected_terminal is None:
            if terminals:
                raise ValueError("incompatible version-1 run terminal fact")
        elif (
            len(terminals) != 1
            or terminals[0].type != expected_terminal
            or terminals[0].sequence != run.version
            or terminals[0].session_id != run.session_id
        ):
            raise ValueError("incompatible version-1 run terminal fact")

    for row in events:
        if row.type == "run.created" and (row.run_id is None or row.run_id not in runs):
            raise ValueError("incompatible version-1 orphan run event")

    for workflow_id, workflow in workflows.items():
        aggregate = sorted(by_run.get(workflow_id, ()), key=lambda item: item.sequence)
        started = [item for item in aggregate if item.type == "workflow.started"]
        terminals = [
            item
            for item in aggregate
            if item.type in {"workflow.completed", "workflow.failed"}
        ]
        if (
            len(started) != 1
            or started[0].sequence != 1
            or started[0].session_id != workflow.session_id
        ):
            raise ValueError("incompatible version-1 workflow start fact")
        start_payload = payloads[started[0].event_id]
        if set(start_payload) != {"definition_hash", "name"} or (
            start_payload.get("definition_hash") != workflow.workflow.definition_hash
            or start_payload.get("name") != workflow.workflow.name
        ):
            raise ValueError("incompatible version-1 workflow start payload")

        state: dict[str, dict[str, object]] = {
            node.node_id: {
                "status": WorkflowNodeStatus.PENDING,
                "version": 1,
                "run_id": None,
            }
            for node in workflow.nodes
        }
        aggregate_version = 1
        for event in aggregate:
            if event.type not in {
                "workflow.node.started",
                "workflow.node.completed",
                "workflow.node.failed",
            }:
                continue
            payload = payloads[event.event_id]
            node_id = payload.get("node_id")
            if not isinstance(node_id, str) or node_id not in state:
                raise ValueError("incompatible version-1 workflow node event")
            current = state[node_id]
            aggregate_version += 1
            if event.sequence != aggregate_version:
                raise ValueError("incompatible version-1 workflow event sequence")
            if event.type == "workflow.node.started":
                node_run_id = payload.get("run_id")
                if (
                    current["status"] is not WorkflowNodeStatus.PENDING
                    or not isinstance(node_run_id, str)
                ):
                    raise ValueError("incompatible version-1 workflow node event")
                current.update(
                    status=WorkflowNodeStatus.RUNNING,
                    version=2,
                    run_id=node_run_id,
                )
            else:
                if (
                    current["status"] is not WorkflowNodeStatus.RUNNING
                    or payload.get("run_id") != current["run_id"]
                ):
                    raise ValueError("incompatible version-1 workflow node event")
                current.update(
                    status=(
                        WorkflowNodeStatus.COMPLETED
                        if event.type == "workflow.node.completed"
                        else WorkflowNodeStatus.FAILED
                    ),
                    version=3,
                )
        for node in workflow.nodes:
            reduced = state[node.node_id]
            if (
                node.status is not reduced["status"]
                or node.version != reduced["version"]
                or node.run_id != reduced["run_id"]
                or nodes.get(node.entity_id) != node
            ):
                raise ValueError("incompatible version-1 workflow node facts")
        if workflow.status is WorkflowRunStatus.COMPLETED:
            expected_terminal = "workflow.completed"
        elif workflow.status is WorkflowRunStatus.FAILED:
            expected_terminal = "workflow.failed"
        else:
            expected_terminal = None
        if expected_terminal is None:
            if terminals or aggregate_version != workflow.version:
                raise ValueError("incompatible version-1 workflow terminal fact")
        else:
            aggregate_version += 1
            if (
                len(terminals) != 1
                or terminals[0].type != expected_terminal
                or terminals[0].sequence != aggregate_version
                or terminals[0].sequence != workflow.version
            ):
                raise ValueError("incompatible version-1 workflow terminal fact")

    for row in events:
        if row.type == "workflow.started" and (
            row.run_id is None or row.run_id not in workflows
        ):
            raise ValueError("incompatible version-1 orphan workflow event")

    view_events: dict[str, _EventRow] = {}
    compaction_events_by_view: dict[str, _EventRow] = {}
    capsule_events: dict[str, _EventRow] = {}
    evaluation_events: dict[str, _EventRow] = {}
    for row in events:
        payload = payloads[row.event_id]
        if row.type == "context.view.created":
            view_id = payload.get("view_id")
            if not isinstance(view_id, str) or view_id in view_events:
                raise ValueError("incompatible version-1 context event")
            view_events[view_id] = row
        elif row.type == "context.compaction.completed":
            required = {"view_id", "capsule_id", "level", "model", "budget", "usage"}
            view_id = payload.get("view_id")
            capsule_id = payload.get("capsule_id")
            model = payload.get("model")
            usage = payload.get("usage")
            if (
                set(payload) != required
                or not isinstance(view_id, str)
                or not isinstance(capsule_id, str)
                or not isinstance(model, str)
                or not model
                or not isinstance(usage, dict)
                or set(usage)
                != {"prompt_tokens", "completion_tokens", "total_tokens"}
                or any(
                    value is not None
                    and (type(value) is not int or value < 0)
                    for value in usage.values()
                )
                or view_id in compaction_events_by_view
                or capsule_id in capsule_events
            ):
                raise ValueError("incompatible version-1 context event")
            view = views.get(view_id)
            capsule = capsules.get(capsule_id)
            try:
                budget = ContextBudget.model_validate(payload.get("budget"))
            except ValueError as error:
                raise ValueError("incompatible version-1 context event") from error
            if (
                view is None
                or capsule is None
                or row.session_id != view.session_id
                or row.session_id != capsule[0]
                or row.run_id != view_id
                or row.sequence != 1
                or view.capsule_id != capsule_id
                or payload.get("level") != view.applied_level.value
                or view.budget is None
                or budget != view.budget
            ):
                raise ValueError("incompatible version-1 context event")
            compaction_events_by_view[view_id] = row
            capsule_events[capsule_id] = row
        elif row.type == "evaluation.completed":
            evaluation_id = payload.get("evaluation_id")
            if not isinstance(evaluation_id, str) or evaluation_id in evaluation_events:
                raise ValueError("incompatible version-1 evaluation event")
            evaluation_events[evaluation_id] = row
    for view_id, event in view_events.items():
        view = views.get(view_id)
        payload = payloads[event.event_id]
        if (
            view is None
            or event.session_id != view.session_id
            or event.run_id != view_id
            or payload.get("capsule_id") != view.capsule_id
        ):
            raise ValueError("incompatible version-1 context event")
    for capsule_id, event in capsule_events.items():
        capsule = capsules.get(capsule_id)
        if capsule is None or event.session_id != capsule[0]:
            raise ValueError("incompatible version-1 context capsule event")
    for evaluation_id, event in evaluation_events.items():
        evaluation = evaluations.get(evaluation_id)
        if (
            evaluation is None
            or event.session_id != evaluation.session_id
            or event.run_id != evaluation_id
            or payloads[event.event_id] != evaluation.model_dump(mode="json")
        ):
            raise ValueError("incompatible version-1 evaluation event")
    for _, (session_id, capsule) in capsules.items():
        if any(
            source_id not in events_by_id
            or events_by_id[source_id].session_id != session_id
            for source_id in capsule.source_event_ids
        ):
            raise ValueError("incompatible version-1 context source reference")
    for view in views.values():
        if any(
            reference not in events_by_id
            or events_by_id[reference].session_id != view.session_id
            for reference in view.message_refs
        ):
            raise ValueError("incompatible version-1 context message reference")
    for evaluation in evaluations.values():
        if any(
            evidence_id not in events_by_id
            or events_by_id[evidence_id].session_id != evaluation.session_id
            for evidence_id in evaluation.evidence_event_ids
        ):
            raise ValueError("incompatible version-1 evaluation evidence")
    for view_id, view in views.items():
        view_event = view_events.get(view_id)
        if (
            view_event is None
            or view_event.session_id != view.session_id
            or view_event.run_id != view_id
        ):
            raise ValueError("incompatible version-1 context facts")
        if view.capsule_id is not None and view_id not in compaction_events_by_view:
            raise ValueError("incompatible version-1 context capsule facts")
    for capsule_id, (session_id, _) in capsules.items():
        if capsule_id not in capsule_events:
            raise ValueError("incompatible version-1 context capsule facts")
        if capsule_events[capsule_id].session_id != session_id:
            raise ValueError("incompatible version-1 context capsule owner")
    for evaluation_id, evaluation in evaluations.items():
        evaluation_event = evaluation_events.get(evaluation_id)
        if (
            evaluation_event is None
            or evaluation_event.session_id != evaluation.session_id
            or evaluation_event.run_id != evaluation_id
        ):
            raise ValueError("incompatible version-1 evaluation facts")


async def _validate_current_projection_rows(connection: aiosqlite.Connection) -> None:
    from agent_sdk.context.models import ContextCapsule, ContextView
    from agent_sdk.evaluation.models import EvaluationResult
    from agent_sdk.runtime.models import RunSnapshot, SessionSnapshot
    from agent_sdk.workflow.models import WorkflowNodeSnapshot, WorkflowRunSnapshot

    try:
        rows = await _snapshot_rows(connection)
        decoded = {row: _strict_json_object(row.data_json) for row in rows}
        sessions: dict[str, SessionSnapshot] = {}
        runs: dict[str, RunSnapshot] = {}
        workflows: dict[str, WorkflowRunSnapshot] = {}
        nodes: dict[str, WorkflowNodeSnapshot] = {}
        capsules: dict[str, tuple[str, ContextCapsule]] = {}
        views: dict[str, ContextView] = {}
        evaluations: dict[str, EvaluationResult] = {}
        for row in rows:
            if row.kind != "session":
                continue
            session = SessionSnapshot.model_validate(decoded[row])
            if (
                session.session_id != row.entity_id
                or row.session_id != session.session_id
                or row.version != session.version
            ):
                raise ValueError("current session identity is invalid")
            sessions[session.session_id] = session
        for row in rows:
            if row.session_id not in sessions:
                raise ValueError("current projection owner is missing")
            data = decoded[row]
            if row.kind == "session":
                continue
            if row.kind == "run":
                run_value = RunSnapshot.model_validate(data)
                if (
                    run_value.run_id != row.entity_id
                    or run_value.session_id != row.session_id
                    or run_value.version != row.version
                ):
                    raise ValueError("current run identity is invalid")
                runs[run_value.run_id] = run_value
            elif row.kind == "workflow":
                workflow_value = WorkflowRunSnapshot.model_validate(data)
                if (
                    workflow_value.workflow_run_id != row.entity_id
                    or workflow_value.session_id != row.session_id
                    or workflow_value.version != row.version
                ):
                    raise ValueError("current workflow identity is invalid")
                workflows[workflow_value.workflow_run_id] = workflow_value
            elif row.kind == "workflow_node":
                node_value = WorkflowNodeSnapshot.model_validate(data)
                if (
                    node_value.entity_id != row.entity_id
                    or node_value.session_id != row.session_id
                    or node_value.version != row.version
                ):
                    raise ValueError("current workflow node identity is invalid")
                nodes[node_value.entity_id] = node_value
            elif row.kind == "context_capsule":
                if set(data) != {"session_id", "capsule"} or data["session_id"] != row.session_id:
                    raise ValueError("current context capsule is invalid")
                capsule_value = ContextCapsule.model_validate(data["capsule"])
                if row.version != 1:
                    raise ValueError("current context capsule version is invalid")
                capsules[row.entity_id] = (row.session_id, capsule_value)
            elif row.kind == "context_view":
                view_value = ContextView.model_validate(data)
                if (
                    view_value.view_id != row.entity_id
                    or view_value.session_id != row.session_id
                    or row.version != 1
                ):
                    raise ValueError("current context view identity is invalid")
                views[view_value.view_id] = view_value
            elif row.kind == "evaluation":
                evaluation_value = EvaluationResult.model_validate(data)
                if (
                    evaluation_value.evaluation_id != row.entity_id
                    or evaluation_value.session_id != row.session_id
                    or evaluation_value.record_version != row.version
                ):
                    raise ValueError("current evaluation identity is invalid")
                evaluations[evaluation_value.evaluation_id] = evaluation_value
            else:
                raise ValueError("current snapshot kind is invalid")
        for workflow in workflows.values():
            for node in workflow.nodes:
                if nodes.get(node.entity_id) != node:
                    raise ValueError("current workflow node projection is invalid")
        for node in nodes.values():
            owner_workflow = workflows.get(node.workflow_run_id)
            if owner_workflow is None or owner_workflow.session_id != node.session_id:
                raise ValueError("current workflow node owner is invalid")
        for view in views.values():
            if view.capsule_id is not None:
                capsule = capsules.get(view.capsule_id)
                if capsule is None or capsule[0] != view.session_id:
                    raise ValueError("current context reference is invalid")
        for evaluation in evaluations.values():
            run = runs.get(evaluation.subject_run_id)
            if run is None or run.session_id != evaluation.session_id:
                raise ValueError("current evaluation subject is invalid")
        async with connection.execute(
            """
            SELECT scope, key, request_fingerprint, session_id, result_json
            FROM idempotency_records ORDER BY scope, key
            """
        ) as cursor:
            records = await cursor.fetchall()
        for record_row in records:
            record = record_from_stored_json(
                scope=record_row[0],
                key=record_row[1],
                request_fingerprint=record_row[2],
                session_id=record_row[3],
                result_json=record_row[4],
            )
            if record.session_id not in sessions:
                raise ValueError("current idempotency owner is invalid")
    except Exception as error:
        raise ValueError("incompatible current projections") from error
