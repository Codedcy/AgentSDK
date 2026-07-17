from __future__ import annotations

import asyncio
import ast
import gc
import shutil
import sqlite3
import sys
import weakref
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import agent_sdk.storage.migrations as migration_storage
import agent_sdk.storage.sqlite as sqlite_storage
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.leases import Lease
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
    ReconciliationAction,
    ReconciliationRequest,
    ReconciliationResolution,
    ReconciliationStatus,
    RunCheckpoint,
    RunCheckpointPhase,
)
from agent_sdk.storage.base import CommitBatch, RunProgressBatch, SnapshotWrite
from agent_sdk.storage.migrations import (
    MigrationChecksumError,
    MigrationRunner,
    MigrationSchemaError,
    SchemaGenerationChangedError,
)
from agent_sdk.storage.sqlite import SQLiteStore


_MIGRATION_ROOT = Path(__file__).parents[3] / "src" / "agent_sdk" / "storage" / "migrations"


def _create_legacy_database(path: Path, version: int) -> None:
    with sqlite3.connect(path) as connection:
        for current in range(1, version + 1):
            resource = next(_MIGRATION_ROOT.glob(f"{current:04d}_*.sql"))
            connection.executescript(resource.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (current, f"applied-{current}"),
            )


def _database_bytes(path: Path) -> tuple[bytes, bytes | None, bytes | None]:
    return (
        path.read_bytes(),
        (path.parent / f"{path.name}-wal").read_bytes()
        if (path.parent / f"{path.name}-wal").exists()
        else None,
        (path.parent / f"{path.name}-shm").read_bytes()
        if (path.parent / f"{path.name}-shm").exists()
        else None,
    )


def _logical_schema_state(path: Path) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(path)
    try:
        schema = tuple(
            connection.execute(
                """
                SELECT type, name, tbl_name, sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
        )
        migrations = tuple(
            connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        )
        return schema + (("migration_rows", migrations),)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_open_plan_and_applied_are_non_mutating_on_empty_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty.db"

    runner = await MigrationRunner.open(path)
    plan = await runner.plan()

    assert tuple(migration.version for migration in plan) == (1, 2, 3, 4)
    assert all(len(migration.checksum) == 64 for migration in plan)
    assert await runner.applied() == ()
    assert not path.exists()


@pytest.mark.asyncio
async def test_apply_empty_database_records_trusted_checksums_and_artifact_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "apply.db"
    runner = await MigrationRunner.open(path)
    expected = await runner.plan()

    await runner.apply()

    applied = await runner.applied()
    assert tuple(item.version for item in applied) == (1, 2, 3, 4)
    assert tuple(item.checksum for item in applied) == tuple(
        migration.checksum for migration in expected
    )
    assert await runner.plan() == ()
    with sqlite3.connect(path) as connection:
        columns = tuple(
            row[1] for row in connection.execute("PRAGMA table_info(schema_migrations)")
        )
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert columns == ("version", "checksum", "applied_at")
    assert {
        "artifact_generations",
        "artifact_heads",
        "artifact_owners",
        "artifact_cleanup_jobs",
    } <= tables


@pytest.mark.asyncio
async def test_artifact_generation_metadata_expresses_all_lifecycle_states(
    tmp_path: Path,
) -> None:
    path = tmp_path / "artifact-states.db"
    runner = await MigrationRunner.open(path)
    await runner.apply()
    rows = (
        ("publishing", "claim-publish", "2026-07-17T09:00:00+00:00"),
        ("ready", None, None),
        ("delete_pending", None, None),
        ("deleting", "claim-delete", "2026-07-17T09:00:00+00:00"),
    )
    with sqlite3.connect(path) as connection:
        for generation, (state, claim, expires_at) in enumerate(rows, start=1):
            connection.execute(
                """
                INSERT INTO artifact_generations(
                    digest, generation, physical_path, size, mime_type, state,
                    claim_token, claim_expires_at
                ) VALUES (?, ?, ?, 0, 'application/octet-stream', ?, ?, ?)
                """,
                (
                    f"{generation:064x}",
                    generation,
                    f"artifacts/{generation}",
                    state,
                    claim,
                    expires_at,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO artifact_generations(
                    digest, generation, physical_path, size, mime_type, state
                ) VALUES (?, 1, 'invalid/publishing', 0,
                          'application/octet-stream', 'publishing')
                """,
                ("f" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO artifact_generations(
                    digest, generation, physical_path, size, mime_type, state,
                    claim_token, claim_expires_at
                ) VALUES (?, 1, 'invalid/ready', 0,
                          'application/octet-stream', 'ready', 'claim', 'later')
                """,
                ("e" * 64,),
            )
        stored_states = tuple(
            row[0]
            for row in connection.execute(
                "SELECT state FROM artifact_generations ORDER BY generation"
            )
        )
    assert stored_states == tuple(row[0] for row in rows)


@pytest.mark.asyncio
async def test_changed_packaged_sql_is_rejected_before_database_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "changed-sql.db"
    original_read_bytes = Path.read_bytes

    def changed_read_bytes(resource: Path) -> bytes:
        contents = original_read_bytes(resource)
        if resource.name == "0003_leases.sql":
            return contents + b"\n"
        return contents

    monkeypatch.setattr(Path, "read_bytes", changed_read_bytes)
    runner = await MigrationRunner.open(path)

    with pytest.raises(MigrationChecksumError, match="packaged migration checksum"):
        await runner.plan()
    assert not path.exists()


@pytest.mark.asyncio
async def test_changed_v2_transform_identity_is_rejected_before_database_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "changed-transform.db"
    monkeypatch.setattr(
        migration_storage,
        "_MIGRATION_2_TRANSFORM_ID",
        "session-ownership-v1-to-v2-changed",
    )
    runner = await MigrationRunner.open(path)

    with pytest.raises(MigrationChecksumError, match="packaged migration checksum"):
        await runner.plan()
    assert not path.exists()


@pytest.mark.parametrize(
    "mutation",
    ["malformed", "duplicate", "future", "missing"],
)
@pytest.mark.asyncio
async def test_packaged_manifest_fails_closed_before_database_creation(
    mutation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_root = tmp_path / "resources"
    resource_root.mkdir()
    for resource in _MIGRATION_ROOT.glob("*.sql"):
        shutil.copy2(resource, resource_root / resource.name)
    if mutation == "malformed":
        (resource_root / "not_a_numbered_migration.sql").write_text("SELECT 1;", encoding="utf-8")
    elif mutation == "duplicate":
        shutil.copy2(
            resource_root / "0001_initial.sql",
            resource_root / "0001_duplicate.sql",
        )
    elif mutation == "future":
        shutil.copy2(
            resource_root / "0004_migration_checksums_and_artifacts.sql",
            resource_root / "0005_future.sql",
        )
    else:
        (resource_root / "0004_migration_checksums_and_artifacts.sql").unlink()
    monkeypatch.setattr(
        migration_storage.resources,
        "files",
        lambda package: resource_root,
    )
    path = tmp_path / "must-not-exist.db"
    runner = await MigrationRunner.open(path)

    with pytest.raises(migration_storage.MigrationResourceError, match="migration"):
        await runner.plan()
    assert not path.exists()


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.asyncio
async def test_plan_and_applied_are_read_only_on_every_legacy_schema(
    version: int,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"v{version}.db"
    _create_legacy_database(path, version)
    before = _database_bytes(path)
    runner = await MigrationRunner.open(path)

    applied = await runner.applied()
    pending = await runner.plan()

    assert tuple(item.version for item in applied) == tuple(range(1, version + 1))
    assert tuple(item.applied_at for item in applied) == tuple(
        f"applied-{current}" for current in range(1, version + 1)
    )
    assert tuple(item.version for item in pending) == tuple(range(version + 1, 5))
    assert _database_bytes(path) == before


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.asyncio
async def test_apply_upgrades_every_legacy_schema_and_preserves_historical_times(
    version: int,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"upgrade-v{version}.db"
    _create_legacy_database(path, version)
    runner = await MigrationRunner.open(path)

    await runner.apply()

    applied = await runner.applied()
    assert tuple(item.version for item in applied) == (1, 2, 3, 4)
    assert tuple(item.applied_at for item in applied[:version]) == tuple(
        f"applied-{current}" for current in range(1, version + 1)
    )


@pytest.mark.asyncio
async def test_same_database_concurrent_runners_converge_on_schema_four(
    tmp_path: Path,
) -> None:
    path = tmp_path / "same-database.db"
    _create_legacy_database(path, 3)
    first = await MigrationRunner.open(path)
    second = await MigrationRunner.open(path)

    await asyncio.wait_for(asyncio.gather(first.apply(), second.apply()), timeout=5)

    assert tuple(item.version for item in await first.applied()) == (1, 2, 3, 4)
    assert await second.plan() == ()


@pytest.mark.asyncio
async def test_same_database_concurrent_plan_apply_and_open_converge(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plan-apply-open.db"
    _create_legacy_database(path, 3)
    planning = await MigrationRunner.open(path)
    applying = await MigrationRunner.open(path)

    pending, _, store = await asyncio.wait_for(
        asyncio.gather(planning.plan(), applying.apply(), SQLiteStore.open(path)),
        timeout=5,
    )
    try:
        assert tuple(migration.version for migration in pending) in {(4,), ()}
        assert await planning.plan() == ()
        assert tuple(item.version for item in await applying.applied()) == (1, 2, 3, 4)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_distinct_database_coordinators_do_not_block_each_other(
    tmp_path: Path,
) -> None:
    first = await MigrationRunner.open(tmp_path / "first.db")
    second = await MigrationRunner.open(tmp_path / "second.db")
    first_lock = migration_storage._coordinator(first.database_identity)

    async with first_lock:
        await asyncio.wait_for(second.apply(), timeout=3)

    assert tuple(item.version for item in await second.applied()) == (1, 2, 3, 4)


def test_idle_database_coordinator_is_reclaimed() -> None:
    identity = "test-idle-coordinator"
    first = migration_storage._coordinator(identity)
    second = migration_storage._coordinator(identity)
    reference = weakref.ref(first)
    assert second is first

    del first, second
    gc.collect()

    assert reference() is None
    assert identity not in migration_storage._COORDINATORS


@pytest.mark.asyncio
async def test_waiting_database_coordinator_cannot_split_into_a_second_lock() -> None:
    identity = "test-waiting-coordinator"
    lock = migration_storage._coordinator(identity)
    holder_started = asyncio.Event()
    release_holder = asyncio.Event()
    waiter_started = asyncio.Event()

    async def hold_lock() -> None:
        async with lock:
            holder_started.set()
            await release_holder.wait()

    async def wait_for_lock() -> None:
        waiting_lock = migration_storage._coordinator(identity)
        waiter_started.set()
        async with waiting_lock:
            assert waiting_lock is lock

    holder = asyncio.create_task(hold_lock())
    await asyncio.wait_for(holder_started.wait(), timeout=1)
    waiter = asyncio.create_task(wait_for_lock())
    await asyncio.wait_for(waiter_started.wait(), timeout=1)
    assert migration_storage._coordinator(identity) is lock
    release_holder.set()
    await asyncio.wait_for(holder, timeout=1)
    await asyncio.wait_for(waiter, timeout=1)


async def _wait_for_path(path: Path) -> None:
    while not path.exists():
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_cross_process_apply_rediscovers_schema_after_writer_lock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cross-process.db"
    _create_legacy_database(path, 3)
    first_locked = tmp_path / "first-locked"
    second_attempted = tmp_path / "second-attempted"
    release = tmp_path / "release"
    first_code = "\n".join(
        (
            "import asyncio, sys",
            "from pathlib import Path",
            "from agent_sdk.storage.migrations import MigrationRunner",
            "async def main():",
            "    path, locked, release = map(Path, sys.argv[1:])",
            "    async def pause(stage):",
            "        if stage == 'migration-4-legacy-validated':",
            "            locked.touch()",
            "            while not release.exists():",
            "                await asyncio.sleep(0.01)",
            "    MigrationRunner._migration_checkpoint = staticmethod(pause)",
            "    await (await MigrationRunner.open(path)).apply()",
            "asyncio.run(main())",
        )
    )
    second_code = "\n".join(
        (
            "import asyncio, sys",
            "from pathlib import Path",
            "import aiosqlite",
            "from agent_sdk.storage.migrations import MigrationRunner",
            "attempted = Path(sys.argv[2])",
            "original_execute = aiosqlite.Connection.execute",
            "def traced_execute(self, sql, *args, **kwargs):",
            "    if sql == 'BEGIN IMMEDIATE':",
            "        attempted.touch()",
            "    return original_execute(self, sql, *args, **kwargs)",
            "aiosqlite.Connection.execute = traced_execute",
            "async def main():",
            "    await (await MigrationRunner.open(sys.argv[1])).apply()",
            "asyncio.run(main())",
        )
    )
    processes: list[asyncio.subprocess.Process] = []
    try:
        first_process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            first_code,
            str(path),
            str(first_locked),
            str(release),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        processes.append(first_process)
        await asyncio.wait_for(_wait_for_path(first_locked), timeout=5)
        second_process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            second_code,
            str(path),
            str(second_attempted),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        processes.append(second_process)
        await asyncio.wait_for(_wait_for_path(second_attempted), timeout=5)
        release.touch()
        results = await asyncio.wait_for(
            asyncio.gather(*(process.communicate() for process in processes)),
            timeout=10,
        )
        for process, (_, stderr) in zip(processes, results, strict=True):
            assert process.returncode == 0, stderr.decode(errors="replace")
    finally:
        release.touch(exist_ok=True)
        for process in processes:
            if process.returncode is None:
                process.terminate()
                await process.wait()

    runner = await MigrationRunner.open(path)
    assert tuple(item.version for item in await runner.applied()) == (1, 2, 3, 4)


@pytest.mark.asyncio
async def test_exact_schema_four_plan_is_byte_for_byte_read_only(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    runner = await MigrationRunner.open(path)
    await runner.apply()
    before = _database_bytes(path)

    assert await runner.plan() == ()
    assert tuple(item.version for item in await runner.applied()) == (1, 2, 3, 4)
    assert _database_bytes(path) == before


@pytest.mark.asyncio
async def test_changed_applied_checksum_is_rejected_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "changed-applied.db"
    runner = await MigrationRunner.open(path)
    await runner.apply()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 2",
            ("0" * 64,),
        )
        connection.commit()
    finally:
        connection.close()
    before = _database_bytes(path)

    with pytest.raises(MigrationChecksumError, match="applied migration checksum"):
        await runner.plan()
    with pytest.raises(MigrationChecksumError, match="applied migration checksum"):
        await runner.applied()
    assert _database_bytes(path) == before


@pytest.mark.asyncio
async def test_schema_four_rejects_changed_migration_table_sql_shape(
    tmp_path: Path,
) -> None:
    path = tmp_path / "migration-table-shape.db"
    runner = await MigrationRunner.open(path)
    await runner.apply()
    connection = sqlite3.connect(path)
    try:
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            """
            UPDATE sqlite_master
            SET sql = replace(sql, 'length(applied_at) > 0', 'length(applied_at) >= 0')
            WHERE type='table' AND name='schema_migrations'
            """
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationSchemaError, match="migration table"):
        await runner.plan()


@pytest.mark.asyncio
async def test_schema_four_rejects_untrusted_extra_index(tmp_path: Path) -> None:
    path = tmp_path / "extra-index.db"
    runner = await MigrationRunner.open(path)
    await runner.apply()
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE INDEX untrusted_extra ON artifact_heads(generation)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationSchemaError, match="database schema"):
        await runner.applied()


_MIGRATION_4_FAULT_STAGES = (
    "migration-4-legacy-validated",
    *(
        f"migration-4-statement-{index}-{side}"
        for index in range(1, 12)
        for side in ("before", "after")
    ),
    *(
        f"migration-4-copy-{version}-{side}"
        for version in range(1, 4)
        for side in ("before", "after")
    ),
    "migration-4-version-insert-before",
    "migration-4-version-insert-after",
    "migration-4-final-validation",
)


@pytest.mark.parametrize("stage", _MIGRATION_4_FAULT_STAGES)
@pytest.mark.asyncio
async def test_every_migration_four_fault_boundary_rolls_back_to_exact_v3(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / f"fault-{stage}.db"
    _create_legacy_database(path, 3)
    before = _logical_schema_state(path)
    runner = await MigrationRunner.open(path)

    async def fail_at(checkpoint: str) -> None:
        if checkpoint == stage:
            raise RuntimeError("injected migration-4 fault")

    monkeypatch.setattr(MigrationRunner, "_migration_checkpoint", staticmethod(fail_at))
    with pytest.raises(RuntimeError, match="injected migration-4 fault"):
        await runner.apply()

    assert _logical_schema_state(path) == before
    assert tuple(item.version for item in await runner.applied()) == (1, 2, 3)


@pytest.mark.asyncio
async def test_cancelling_migration_four_rolls_back_and_reopens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cancel-v4.db"
    _create_legacy_database(path, 3)
    before = _logical_schema_state(path)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def pause(checkpoint: str) -> None:
        if checkpoint == "migration-4-version-insert-after":
            reached.set()
            await release.wait()

    monkeypatch.setattr(MigrationRunner, "_migration_checkpoint", staticmethod(pause))
    runner = await MigrationRunner.open(path)
    task = asyncio.create_task(runner.apply())
    await asyncio.wait_for(reached.wait(), timeout=2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert _logical_schema_state(path) == before
    assert tuple(item.version for item in await runner.applied()) == (1, 2, 3)


def _insert_snapshot(
    connection: sqlite3.Connection,
    *,
    kind: str,
    entity_id: str,
    session_id: str,
    version: int,
    data_json: str,
) -> None:
    connection.execute(
        """
        INSERT INTO snapshots(kind, entity_id, session_id, version, data_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (kind, entity_id, session_id, version, data_json),
    )


def _seed_recovery_owner(
    connection: sqlite3.Connection,
    *,
    include_run: bool = True,
) -> None:
    session = SessionSnapshot(session_id="ses_1", workspaces=("workspace",))
    _insert_snapshot(
        connection,
        kind="session",
        entity_id=session.session_id,
        session_id=session.session_id,
        version=session.version,
        data_json=session.model_dump_json(),
    )
    if not include_run:
        return
    run = RunSnapshot(
        run_id="run_1",
        session_id=session.session_id,
        agent_revision="agent:1",
        status=RunStatus.CREATED,
        user_input="hello",
    )
    _insert_snapshot(
        connection,
        kind="run",
        entity_id=run.run_id,
        session_id=run.session_id,
        version=run.version,
        data_json=run.model_dump_json(),
    )


def _insert_operation(
    connection: sqlite3.Connection,
    operation: ModelCallOperation,
) -> None:
    connection.execute(
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
            operation.model_dump_json(),
        ),
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "operation-run",
        "checkpoint-operation",
        "reconciliation-operation",
        "reconciliation-event",
    ],
)
@pytest.mark.asyncio
async def test_recovery_relation_corruption_blocks_v3_to_v4_bootstrap(
    corruption: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"invalid-v3-{corruption}.db"
    _create_legacy_database(path, 3)
    with sqlite3.connect(path) as connection:
        _seed_recovery_owner(
            connection,
            include_run=corruption != "operation-run",
        )
        operation = ModelCallOperation(
            operation_id="op_1",
            session_id="ses_1",
            run_id="run_1",
            turn=0,
            request_fingerprint="sha256:model",
            lease_generation=1,
            status=ExternalOperationStatus.STARTED,
            provider_identity="provider:model",
        )
        if corruption in {"operation-run", "checkpoint-operation"}:
            _insert_operation(connection, operation)
        if corruption == "checkpoint-operation":
            checkpoint = RunCheckpoint(
                run_id="run_1",
                session_id="ses_1",
                checkpoint_version=1,
                turn=0,
                phase=RunCheckpointPhase.TOOL_IN_FLIGHT,
                operation_id=operation.operation_id,
                messages=({"role": "user", "content": "hello"},),
            )
            connection.execute(
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
                    checkpoint.model_dump_json(),
                ),
            )
        elif corruption.startswith("reconciliation-"):
            request = ReconciliationRequest(
                request_id="rec_1",
                session_id="ses_1",
                run_id="run_1",
                operation_id=("op_missing" if corruption == "reconciliation-operation" else None),
                reason="unknown outcome",
            )
            if corruption == "reconciliation-event":
                request = request.model_copy(
                    update={
                        "status": ReconciliationStatus.RESOLVED,
                        "resolution": ReconciliationResolution(
                            action=ReconciliationAction.TERMINATE,
                            actor={"type": "user"},
                            evidence={"reason": "test"},
                            decided_at=_NOW,
                            event_id="evt_missing",
                        ),
                    }
                )
            connection.execute(
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
                    request.model_dump_json(),
                ),
            )
    before = _business_row_counts(path)
    runner = await MigrationRunner.open(path)

    with pytest.raises(MigrationSchemaError, match="incompatible"):
        await runner.apply()

    assert _business_row_counts(path) == before
    with sqlite3.connect(path) as connection:
        versions = tuple(
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        )
    assert versions == (1, 2, 3)


@pytest.mark.parametrize("corruption", ["lease-row", "projection-row", "index-shape"])
@pytest.mark.asyncio
async def test_invalid_v3_schema_or_data_is_rejected_without_bootstrap(
    corruption: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"invalid-v3-{corruption}.db"
    _create_legacy_database(path, 3)
    connection = sqlite3.connect(path)
    try:
        if corruption == "lease-row":
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                """
                INSERT INTO leases(
                    run_id, owner, generation, acquired_at, renewed_at, expires_at,
                    released
                ) VALUES ('run_bad', 'worker', 1, '2026-01-01', '2026-01-01',
                          '2026-01-02', 2)
                """
            )
        elif corruption == "projection-row":
            connection.execute(
                """
                INSERT INTO snapshots(kind, entity_id, session_id, version, data_json)
                VALUES ('session', 'ses_bad', 'ses_bad', 1,
                        '{"session_id":"ses_other","workspaces":[],"version":1}')
                """
            )
        else:
            connection.execute("DROP INDEX leases_expires_at")
            connection.execute("CREATE INDEX leases_expires_at ON leases(owner)")
        connection.commit()
    finally:
        connection.close()
    before = _logical_schema_state(path)
    runner = await MigrationRunner.open(path)

    with pytest.raises(MigrationSchemaError, match="incompatible"):
        await runner.apply()
    assert _logical_schema_state(path) == before


@pytest.mark.asyncio
async def test_sqlite_store_open_applies_schema_four_before_returning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store-open.db"

    store = await SQLiteStore.open(path)
    try:
        runner = await MigrationRunner.open(path)
        assert tuple(item.version for item in await runner.applied()) == (1, 2, 3, 4)
    finally:
        await store.close()


_BUSINESS_MUTATIONS = (
    "commit",
    "commit_run_progress",
    "create_external_operation",
    "transition_external_operation",
    "put_run_checkpoint",
    "create_reconciliation_request",
    "resolve_reconciliation_request",
    "acquire_lease",
    "renew_lease",
    "release_lease",
    "assert_current_lease",
    "delete_session",
)


def test_every_sqlite_business_mutation_uses_shared_generation_fence() -> None:
    source_path = Path(sqlite_storage.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    store_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SQLiteStore"
    )
    methods = {
        node.name: node
        for node in store_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    bypasses: list[str] = []
    for name in _BUSINESS_MUTATIONS:
        method = methods[name]
        fenced = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_begin_immediate"
            for node in ast.walk(method)
        )
        if not fenced:
            bypasses.append(name)
    assert bypasses == []


_NOW = datetime(2026, 7, 17, 8, tzinfo=UTC)


def _stale_mutation(
    store: SQLiteStore,
    name: str,
) -> Callable[[], Awaitable[object]]:
    lease = Lease(
        run_id="run_1",
        owner="worker_1",
        generation=1,
        acquired_at=_NOW,
        renewed_at=_NOW,
        expires_at=_NOW + timedelta(minutes=1),
    )
    operation = ModelCallOperation(
        operation_id="op_1",
        session_id="ses_1",
        run_id="run_1",
        turn=0,
        request_fingerprint="sha256:model",
        lease_generation=1,
        status=ExternalOperationStatus.STARTED,
        provider_identity="provider:model",
    )
    checkpoint = RunCheckpoint(
        run_id="run_1",
        session_id="ses_1",
        checkpoint_version=1,
        turn=0,
        phase=RunCheckpointPhase.READY_FOR_MODEL,
        messages=({"role": "user", "content": "hello"},),
    )
    request = ReconciliationRequest(
        request_id="rec_1",
        session_id="ses_1",
        run_id="run_1",
        operation_id=None,
        reason="test",
    )
    event = EventEnvelope(
        event_id="evt_1",
        type="run.test",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={},
        occurred_at=_NOW,
    )
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "commit": lambda: store.commit(CommitBatch(events=())),
        "commit_run_progress": lambda: store.commit_run_progress(
            RunProgressBatch(lease=lease, now=_NOW, events=(event,))
        ),
        "create_external_operation": lambda: store.create_external_operation(
            operation, lease=lease, now=_NOW
        ),
        "transition_external_operation": lambda: store.transition_external_operation(
            expected=operation,
            updated=operation,
            lease=lease,
            now=_NOW,
        ),
        "put_run_checkpoint": lambda: store.put_run_checkpoint(
            checkpoint,
            expected=None,
            lease=lease,
            now=_NOW,
        ),
        "create_reconciliation_request": lambda: store.create_reconciliation_request(request),
        "resolve_reconciliation_request": lambda: store.resolve_reconciliation_request(
            expected=request,
            resolved=request,
            event=event,
        ),
        "acquire_lease": lambda: store.acquire_lease(
            run_id="run_1",
            owner="worker_1",
            now=_NOW,
            expires_at=_NOW + timedelta(minutes=1),
        ),
        "renew_lease": lambda: store.renew_lease(
            lease,
            now=_NOW + timedelta(seconds=1),
            expires_at=_NOW + timedelta(minutes=2),
        ),
        "release_lease": lambda: store.release_lease(lease),
        "assert_current_lease": lambda: store.assert_current_lease(lease, now=_NOW),
        "delete_session": lambda: store.delete_session("ses_1"),
    }
    return calls[name]


def _business_row_counts(path: Path) -> tuple[tuple[str, int], ...]:
    connection = sqlite3.connect(path)
    try:
        tables = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table'
                  AND name NOT LIKE 'sqlite_%'
                  AND name != 'schema_migrations'
                ORDER BY name
                """
            )
        )
        return tuple(
            (table, connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        )
    finally:
        connection.close()


@pytest.mark.parametrize("mutation", _BUSINESS_MUTATIONS)
@pytest.mark.asyncio
async def test_stale_v3_generation_fails_every_mutation_before_data_change(
    mutation: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"stale-{mutation}.db"
    _create_legacy_database(path, 3)
    stale = await SQLiteStore._open_existing(path)
    try:
        runner = await MigrationRunner.open(path)
        await runner.apply()
        before = _business_row_counts(path)

        with pytest.raises(SchemaGenerationChangedError, match="schema generation"):
            await _stale_mutation(stale, mutation)()
        assert _business_row_counts(path) == before
        assert not stale._connection.in_transaction
        writer = sqlite3.connect(path, timeout=0)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
        finally:
            writer.close()
    finally:
        await stale.close()


@pytest.mark.asyncio
async def test_fresh_store_can_write_while_fenced_stale_store_remains_open(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh-writer.db"
    _create_legacy_database(path, 3)
    stale = await SQLiteStore._open_existing(path)
    runner = await MigrationRunner.open(path)
    await runner.apply()
    with pytest.raises(SchemaGenerationChangedError, match="schema generation"):
        await stale.commit(CommitBatch(events=()))
    assert not stale._connection.in_transaction
    store = await SQLiteStore.open(path)
    session = SessionSnapshot(session_id="ses_fresh", workspaces=("workspace",))
    try:
        await store.commit(
            CommitBatch(
                events=(
                    EventEnvelope(
                        event_id="evt_fresh",
                        type="session.created",
                        session_id=session.session_id,
                        run_id=None,
                        sequence=1,
                        payload=session.model_dump(mode="json"),
                        occurred_at=_NOW,
                    ),
                ),
                snapshots=(
                    SnapshotWrite(
                        kind="session",
                        entity_id=session.session_id,
                        session_id=session.session_id,
                        version=session.version,
                        data=session.model_dump(mode="json"),
                    ),
                ),
            )
        )
        assert await store.get_snapshot("session", session.session_id) is not None
    finally:
        await store.close()
        await stale.close()
