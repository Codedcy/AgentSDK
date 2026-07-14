from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
import pytest
import aiosqlite

from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.leases import LeaseManager
from agent_sdk.storage.sqlite import SQLiteStore

from datetime import UTC, datetime, timedelta


_V3_TABLES = {
    "events",
    "external_operations",
    "idempotency_records",
    "leases",
    "reconciliation_requests",
    "run_checkpoints",
    "schema_migrations",
    "snapshots",
}
_V3_INDEXES = {
    "events_aggregate_sequence",
    "events_session_cursor",
    "external_operations_run_status",
    "external_operations_session",
    "idempotency_records_session",
    "leases_expires_at",
    "reconciliation_requests_operation",
    "reconciliation_requests_run_status",
    "reconciliation_requests_session",
    "run_checkpoints_operation",
    "run_checkpoints_phase",
    "run_checkpoints_session",
    "snapshots_session",
}


def _migration(name: str) -> str:
    return (
        Path(__file__).parents[3]
        / "src"
        / "agent_sdk"
        / "storage"
        / "migrations"
        / name
    ).read_text(encoding="utf-8")


def _create_v2_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(_migration("0001_initial.sql"))
        connection.execute("INSERT INTO schema_migrations VALUES (1, 'v1')")
        connection.executescript(_migration("0002_idempotency.sql"))
        connection.execute("INSERT INTO schema_migrations VALUES (2, 'v2')")


def _schema_state(path: Path) -> tuple[tuple[int, ...], set[str], set[str]]:
    with sqlite3.connect(path) as connection:
        versions = tuple(
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            )
        }
    return versions, tables, indexes


@pytest.mark.asyncio
async def test_empty_database_is_created_as_complete_v3(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"

    store = await SQLiteStore.open(path)
    await store.close()

    versions, tables, indexes = _schema_state(path)
    assert versions == (1, 2, 3)
    assert tables == _V3_TABLES
    assert indexes == _V3_INDEXES
    with sqlite3.connect(path) as connection:
        checkpoint_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(run_checkpoints)"
        ).fetchall()
        reconciliation_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(reconciliation_requests)"
        ).fetchall()
    assert [(row[2], row[3], row[4]) for row in checkpoint_foreign_keys] == [
        ("external_operations", "operation_id", "operation_id"),
        ("external_operations", "run_id", "run_id"),
        ("external_operations", "session_id", "session_id"),
    ]
    assert [(row[2], row[3], row[4]) for row in reconciliation_foreign_keys] == [
        ("external_operations", "operation_id", "operation_id"),
        ("external_operations", "run_id", "run_id"),
        ("external_operations", "session_id", "session_id"),
    ]


@pytest.mark.asyncio
async def test_exact_v2_database_upgrades_to_complete_v3(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    _create_v2_database(path)

    store = await SQLiteStore.open(path)
    await store.close()

    versions, tables, indexes = _schema_state(path)
    assert versions == (1, 2, 3)
    assert tables == _V3_TABLES
    assert indexes == _V3_INDEXES


@pytest.mark.asyncio
async def test_two_concurrent_v2_opens_migrate_once(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-v2.db"
    _create_v2_database(path)

    first, second = await asyncio.wait_for(
        asyncio.gather(SQLiteStore.open(path), SQLiteStore.open(path)), timeout=3
    )
    try:
        assert _schema_state(path) == ((1, 2, 3), _V3_TABLES, _V3_INDEXES)
    finally:
        await asyncio.gather(first.close(), second.close())


@pytest.mark.asyncio
async def test_exact_v3_open_validates_without_replaying_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    path = tmp_path / "v3.db"
    initial = await SQLiteStore.open(path)
    await initial.close()
    checkpoints: list[str] = []

    async def record(stage: str) -> None:
        checkpoints.append(stage)

    monkeypatch.setattr(SQLiteStore, "_migration_checkpoint", staticmethod(record))
    reopened = await SQLiteStore.open(path)
    await reopened.close()

    assert "migration-schema-discovered-v3" in checkpoints
    assert not any(stage.startswith("migration-3-statement-") for stage in checkpoints)
    assert "migration-3-version-inserted" not in checkpoints


@pytest.mark.parametrize(
    "stage",
    [
        *(f"migration-3-statement-{index}" for index in range(1, 14)),
        "migration-3-version-inserted",
        "migration-3-final-validation",
    ],
)
@pytest.mark.asyncio
async def test_v3_migration_fault_rolls_back_to_exact_v2_and_reopens(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / f"fault-{stage}.db"
    _create_v2_database(path)
    before = _schema_state(path)

    async def fail_at(checkpoint: str) -> None:
        if checkpoint == stage:
            raise RuntimeError("injected migration-3 fault")

    with monkeypatch.context() as fault:
        fault.setattr(SQLiteStore, "_migration_checkpoint", staticmethod(fail_at))
        with pytest.raises(RuntimeError, match="injected migration-3 fault"):
            await SQLiteStore.open(path)

    assert _schema_state(path) == before
    reopened = await SQLiteStore.open(path)
    await reopened.close()
    assert _schema_state(path) == ((1, 2, 3), _V3_TABLES, _V3_INDEXES)


@pytest.mark.parametrize(
    "mutation",
    [
        "empty_provider_identity",
        "non_object_operation_json",
        "non_object_checkpoint_json",
        "non_object_reconciliation_json",
    ],
)
@pytest.mark.asyncio
async def test_v3_tables_enforce_identity_and_json_object_constraints(
    mutation: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"constraint-{mutation}.db"
    store = await SQLiteStore.open(path)
    await store.close()
    with sqlite3.connect(path) as connection, pytest.raises(sqlite3.IntegrityError):
        if mutation in {"empty_provider_identity", "non_object_operation_json"}:
            connection.execute(
                """
                INSERT INTO external_operations VALUES (
                    'op_1', 'model_call', 'ses_1', 'run_1', 0, 'fingerprint',
                    ?, NULL, 1, 'started', ?
                )
                """,
                (
                    "" if mutation == "empty_provider_identity" else "provider:model",
                    "[]" if mutation == "non_object_operation_json" else "{}",
                ),
            )
        elif mutation == "non_object_checkpoint_json":
            connection.execute(
                """
                INSERT INTO run_checkpoints VALUES (
                    'run_1', 'ses_1', 1, 0, 'ready_for_model', NULL, '[]'
                )
                """
            )
        else:
            connection.execute(
                """
                INSERT INTO reconciliation_requests VALUES (
                    'rec_1', 'ses_1', 'run_1', NULL, 'pending', '[]'
                )
                """
            )


@pytest.mark.asyncio
async def test_v3_validator_rejects_missing_nullable_json_identity_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-null-identity.db"
    store = await SQLiteStore.open(path)
    await store.close()
    data = {
        "run_id": "run_1",
        "session_id": "ses_1",
        "checkpoint_version": 1,
        "turn": 0,
        "phase": "ready_for_model",
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO run_checkpoints VALUES (
                'run_1', 'ses_1', 1, 0, 'ready_for_model', NULL, ?
            )
            """,
            (json.dumps(data, sort_keys=True, separators=(",", ":")),),
        )

    with pytest.raises(ValueError, match="row identity"):
        await SQLiteStore.open(path)


@pytest.mark.asyncio
async def test_delete_session_cleans_all_v3_runtime_rows(tmp_path: Path) -> None:
    path = tmp_path / "delete.db"
    store = await SQLiteStore.open(path)
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[tmp_path])
    run = await commands.start_run(
        session.session_id, agent_revision="agent:1", user_input="hello"
    )
    manager = LeaseManager(store, ttl=timedelta(seconds=30))
    lease = await manager.acquire(
        run.run_id,
        "coordinator_1",
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )
    operation = {
        "operation_id": "op_1",
        "operation_kind": "model_call",
        "session_id": session.session_id,
        "run_id": run.run_id,
        "turn": 0,
        "request_fingerprint": "fingerprint",
        "provider_identity": "provider:model",
        "tool_identity": None,
        "lease_generation": lease.generation,
        "status": "started",
    }
    checkpoint = {
        "run_id": run.run_id,
        "session_id": session.session_id,
        "checkpoint_version": 1,
        "turn": 0,
        "phase": "model_in_flight",
        "operation_id": "op_1",
    }
    reconciliation = {
        "request_id": "rec_1",
        "session_id": session.session_id,
        "run_id": run.run_id,
        "operation_id": "op_1",
        "status": "pending",
    }
    await store._connection.execute(
        """
        INSERT INTO external_operations VALUES (
            'op_1', 'model_call', ?, ?, 0, 'fingerprint',
            'provider:model', NULL, ?, 'started', ?
        )
        """,
        (
            session.session_id,
            run.run_id,
            lease.generation,
            json.dumps(operation, sort_keys=True, separators=(",", ":")),
        ),
    )
    await store._connection.execute(
        "INSERT INTO run_checkpoints VALUES (?, ?, 1, 0, 'model_in_flight', 'op_1', ?)",
        (
            run.run_id,
            session.session_id,
            json.dumps(checkpoint, sort_keys=True, separators=(",", ":")),
        ),
    )
    await store._connection.execute(
        "INSERT INTO reconciliation_requests VALUES ('rec_1', ?, ?, 'op_1', 'pending', ?)",
        (
            session.session_id,
            run.run_id,
            json.dumps(reconciliation, sort_keys=True, separators=(",", ":")),
        ),
    )
    await store._connection.commit()
    try:
        await store.delete_session(session.session_id)
        for table in (
            "leases",
            "external_operations",
            "run_checkpoints",
            "reconciliation_requests",
        ):
            async with store._connection.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                assert await cursor.fetchone() == (0,)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancel_racing_v3_migration_commit_observes_complete_v3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cancel-v3-commit.db"
    _create_v2_database(path)
    commit = aiosqlite.Connection.commit
    committed = asyncio.Event()
    release = asyncio.Event()

    async def commit_then_wait(connection: aiosqlite.Connection) -> None:
        await commit(connection)
        committed.set()
        await release.wait()

    task: asyncio.Task[SQLiteStore] | None = None
    with monkeypatch.context() as race:
        race.setattr(aiosqlite.Connection, "commit", commit_then_wait)
        task = asyncio.create_task(SQLiteStore.open(path))
        await asyncio.wait_for(committed.wait(), timeout=2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    reopened = await SQLiteStore.open(path)
    await reopened.close()
    assert _schema_state(path) == ((1, 2, 3), _V3_TABLES, _V3_INDEXES)


@pytest.mark.parametrize("child_table", ["run_checkpoints", "reconciliation_requests"])
@pytest.mark.asyncio
async def test_v3_foreign_keys_reject_cross_owner_operation_reference(
    child_table: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"cross-owner-{child_table}.db"
    store = await SQLiteStore.open(path)
    operation = {
        "operation_id": "op_1",
        "operation_kind": "model_call",
        "session_id": "ses_1",
        "run_id": "run_1",
        "turn": 0,
        "request_fingerprint": "fingerprint",
        "provider_identity": "provider:model",
        "tool_identity": None,
        "lease_generation": 1,
        "status": "started",
    }
    try:
        await store._connection.execute(
            """
            INSERT INTO external_operations VALUES (
                'op_1', 'model_call', 'ses_1', 'run_1', 0, 'fingerprint',
                'provider:model', NULL, 1, 'started', ?
            )
            """,
            (json.dumps(operation, sort_keys=True, separators=(",", ":")),),
        )
        with pytest.raises(sqlite3.IntegrityError):
            if child_table == "run_checkpoints":
                child = {
                    "run_id": "run_2",
                    "session_id": "ses_2",
                    "checkpoint_version": 1,
                    "turn": 0,
                    "phase": "model_in_flight",
                    "operation_id": "op_1",
                }
                await store._connection.execute(
                    """
                    INSERT INTO run_checkpoints VALUES (
                        'run_2', 'ses_2', 1, 0, 'model_in_flight', 'op_1', ?
                    )
                    """,
                    (json.dumps(child, sort_keys=True, separators=(",", ":")),),
                )
            else:
                child = {
                    "request_id": "rec_1",
                    "session_id": "ses_2",
                    "run_id": "run_2",
                    "operation_id": "op_1",
                    "status": "pending",
                }
                await store._connection.execute(
                    """
                    INSERT INTO reconciliation_requests VALUES (
                        'rec_1', 'ses_2', 'run_2', 'op_1', 'pending', ?
                    )
                    """,
                    (json.dumps(child, sort_keys=True, separators=(",", ":")),),
                )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_exact_v3_open_rejects_orphaned_foreign_key_row(tmp_path: Path) -> None:
    path = tmp_path / "orphan.db"
    store = await SQLiteStore.open(path)
    await store.close()
    child = {
        "run_id": "run_1",
        "session_id": "ses_1",
        "checkpoint_version": 1,
        "turn": 0,
        "phase": "model_in_flight",
        "operation_id": "op_missing",
    }
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO run_checkpoints VALUES (
                'run_1', 'ses_1', 1, 0, 'model_in_flight', 'op_missing', ?
            )
            """,
            (json.dumps(child, sort_keys=True, separators=(",", ":")),),
        )

    with pytest.raises(ValueError, match="foreign key"):
        await SQLiteStore.open(path)
