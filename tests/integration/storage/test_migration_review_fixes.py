from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiosqlite.context import Result

import agent_sdk.storage.migrations as migration_storage
from agent_sdk.storage.migrations import MigrationRunner
from agent_sdk.storage.sqlite import SQLiteStore, _normalized_sql


_MIGRATION_ROOT = (
    Path(__file__).parents[3] / "src" / "agent_sdk" / "storage" / "migrations"
)


def _create_legacy_database(path: Path, version: int) -> None:
    with sqlite3.connect(path) as connection:
        for current in range(1, version + 1):
            resource = next(_MIGRATION_ROOT.glob(f"{current:04d}_*.sql"))
            connection.executescript(resource.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (current, f"applied-{current}"),
            )


def _rewrite_schema_sql(path: Path, *, object_name: str, old: str, new: str) -> None:
    with sqlite3.connect(path) as connection:
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            """
            UPDATE sqlite_master SET sql = replace(sql, ?, ?)
            WHERE name = ? AND sql IS NOT NULL
            """,
            (old, new, object_name),
        )
        assert connection.total_changes == 1
        connection.execute("PRAGMA writable_schema=OFF")
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")


def _legacy_generation_signature(path: Path) -> tuple[object, ...]:
    if not path.exists():
        return ((), ())
    with sqlite3.connect(path) as connection:
        schema = tuple(
            connection.execute(
                """
                SELECT type, name, tbl_name, sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
        )
        tables = {row[1] for row in schema if row[0] == "table"}
        versions = (
            tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
            if "schema_migrations" in tables
            else ()
        )
    return (schema, versions)


def _expected_legacy_generation(tmp_path: Path, version: int) -> tuple[object, ...]:
    path = tmp_path / f"expected-v{version}.db"
    if version:
        _create_legacy_database(path, version)
    return _legacy_generation_signature(path)


async def _apply_in_subprocess(path: Path) -> None:
    code = "\n".join(
        (
            "import asyncio, sys",
            "from agent_sdk.storage.migrations import MigrationRunner",
            "async def main():",
            "    await (await MigrationRunner.open(sys.argv[1])).apply()",
            "asyncio.run(main())",
        )
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == 0, stderr.decode(errors="replace")


def _thread_loop(
    name: str,
    operation: Callable[[], Awaitable[None]],
    *,
    errors: list[BaseException],
    errors_lock: threading.Lock,
    loops: dict[str, asyncio.AbstractEventLoop],
    loops_lock: threading.Lock,
) -> None:
    loop = asyncio.new_event_loop()
    loop.set_debug(True)
    with loops_lock:
        loops[name] = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(operation())
    except BaseException as error:
        with errors_lock:
            errors.append(error)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except RuntimeError:
            pass
        loop.close()


def _stop_live_threads(
    threads: tuple[threading.Thread, ...],
    loops: dict[str, asyncio.AbstractEventLoop],
    loops_lock: threading.Lock,
) -> None:
    for thread in threads:
        if not thread.is_alive():
            continue
        with loops_lock:
            loop = loops.get(thread.name)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
    for thread in threads:
        thread.join(timeout=2)


def test_same_database_plan_apply_and_open_are_loop_neutral_across_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "threaded-same.db"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    counter_lock = threading.Lock()
    counters = {"active": 0, "maximum": 0}
    original_apply = MigrationRunner._apply_locked
    original_inspect = migration_storage._inspect_applied

    async def enter_critical() -> None:
        with counter_lock:
            counters["active"] += 1
            counters["maximum"] = max(counters["maximum"], counters["active"])

    async def leave_critical() -> None:
        with counter_lock:
            counters["active"] -= 1

    async def held_apply(
        runner: MigrationRunner,
        migrations: tuple[migration_storage.Migration, ...],
        *,
        keep_open: bool = False,
    ) -> Any:
        await enter_critical()
        try:
            first_entered.set()
            acquired = await asyncio.to_thread(release_first.wait, 5)
            assert acquired
            return await original_apply(
                runner,
                migrations,
                keep_open=keep_open,
            )
        finally:
            await leave_critical()

    async def observed_inspect(
        database_path: Path,
        migrations: tuple[migration_storage.Migration, ...],
    ) -> tuple[migration_storage.AppliedMigration, ...]:
        await enter_critical()
        try:
            return await original_inspect(database_path, migrations)
        finally:
            await leave_critical()

    monkeypatch.setattr(MigrationRunner, "_apply_locked", held_apply)
    monkeypatch.setattr(migration_storage, "_inspect_applied", observed_inspect)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    loops: dict[str, asyncio.AbstractEventLoop] = {}
    loops_lock = threading.Lock()

    async def apply() -> None:
        await (await MigrationRunner.open(path)).apply()

    async def inspect_and_open() -> None:
        second_started.set()
        runner = await MigrationRunner.open(path)
        await runner.plan()
        await runner.applied()
        store = await SQLiteStore.open(path)
        await store.close()

    first = threading.Thread(
        name="migration-first-loop",
        target=_thread_loop,
        args=("migration-first-loop", apply),
        kwargs={
            "errors": errors,
            "errors_lock": errors_lock,
            "loops": loops,
            "loops_lock": loops_lock,
        },
        daemon=True,
    )
    second = threading.Thread(
        name="migration-second-loop",
        target=_thread_loop,
        args=("migration-second-loop", inspect_and_open),
        kwargs={
            "errors": errors,
            "errors_lock": errors_lock,
            "loops": loops,
            "loops_lock": loops_lock,
        },
        daemon=True,
    )
    threads = (first, second)
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    assert second_started.wait(timeout=2)
    time.sleep(0.05)
    release_first.set()
    for thread in threads:
        thread.join(timeout=5)
    live_threads = [thread.name for thread in threads if thread.is_alive()]
    _stop_live_threads(threads, loops, loops_lock)

    assert live_threads == []
    assert errors == []
    assert counters["maximum"] == 1
    runner = asyncio.run(MigrationRunner.open(path))
    assert tuple(item.version for item in asyncio.run(runner.applied())) == (
        1,
        2,
        3,
        4,
    )


def test_distinct_databases_overlap_across_thread_event_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = (tmp_path / "threaded-first.db", tmp_path / "threaded-second.db")
    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    counters = {"active": 0, "maximum": 0}
    original_apply = MigrationRunner._apply_locked

    async def overlapping_apply(
        runner: MigrationRunner,
        migrations: tuple[migration_storage.Migration, ...],
        *,
        keep_open: bool = False,
    ) -> Any:
        with counter_lock:
            counters["active"] += 1
            counters["maximum"] = max(counters["maximum"], counters["active"])
        try:
            await asyncio.to_thread(barrier.wait, 3)
            return await original_apply(
                runner,
                migrations,
                keep_open=keep_open,
            )
        finally:
            with counter_lock:
                counters["active"] -= 1

    monkeypatch.setattr(MigrationRunner, "_apply_locked", overlapping_apply)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    loops: dict[str, asyncio.AbstractEventLoop] = {}
    loops_lock = threading.Lock()

    def operation(path: Path) -> Callable[[], Awaitable[None]]:
        async def apply() -> None:
            await (await MigrationRunner.open(path)).apply()

        return apply

    threads = tuple(
        threading.Thread(
            name=f"migration-distinct-{index}",
            target=_thread_loop,
            args=(f"migration-distinct-{index}", operation(path)),
            kwargs={
                "errors": errors,
                "errors_lock": errors_lock,
                "loops": loops,
                "loops_lock": loops_lock,
            },
            daemon=True,
        )
        for index, path in enumerate(paths)
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    live_threads = [thread.name for thread in threads if thread.is_alive()]
    _stop_live_threads(threads, loops, loops_lock)

    assert live_threads == []
    assert errors == []
    assert counters["maximum"] == 2


@pytest.mark.asyncio
async def test_cancelled_coordinator_waiter_does_not_leak_ownership() -> None:
    identity = "review-fix-cancelled-waiter"
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold() -> None:
        async with migration_storage._coordinator(identity):
            holder_entered.set()
            await release_holder.wait()

    async def wait() -> None:
        async with migration_storage._coordinator(identity):
            raise AssertionError("cancelled waiter entered the critical section")

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(holder_entered.wait(), timeout=1)
    waiter = asyncio.create_task(wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, timeout=0.5)

    release_holder.set()
    await asyncio.wait_for(holder, timeout=2)

    async with asyncio.timeout(2):
        async with migration_storage._coordinator(identity):
            pass


@pytest.mark.parametrize("operation", ["plan", "applied"])
@pytest.mark.asyncio
async def test_read_inspection_uses_one_wal_snapshot_across_all_schema_reads(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / f"snapshot-{operation}.db"
    _create_legacy_database(path, 3)
    keeper = sqlite3.connect(path)
    assert keeper.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    keeper.execute(
        "UPDATE schema_migrations SET applied_at = 'wal-applied-3' WHERE version = 3"
    )
    keeper.commit()
    wal_path = path.parent / f"{path.name}-wal"
    assert wal_path.exists() and wal_path.stat().st_size > 0
    first_generation_read = asyncio.Event()
    continue_inspection = asyncio.Event()
    original_columns = migration_storage._migration_columns
    paused = False

    async def pause_after_generation_read(
        connection: Any,
    ) -> tuple[str, ...]:
        nonlocal paused
        columns = await original_columns(connection)
        if not paused:
            paused = True
            first_generation_read.set()
            await continue_inspection.wait()
        return columns

    monkeypatch.setattr(
        migration_storage, "_migration_columns", pause_after_generation_read
    )
    runner = await MigrationRunner.open(path)
    task = asyncio.create_task(getattr(runner, operation)())
    try:
        await asyncio.wait_for(first_generation_read.wait(), timeout=2)
        await _apply_in_subprocess(path)
        continue_inspection.set()
        result = await asyncio.wait_for(task, timeout=3)
    finally:
        continue_inspection.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        keeper.close()

    if operation == "plan":
        assert tuple(item.version for item in result) in {(4,), ()}
    else:
        assert tuple(item.version for item in result) in {
            (1, 2, 3),
            (1, 2, 3, 4),
        }


@pytest.mark.parametrize("stage", ["table-names", "migration-columns"])
@pytest.mark.asyncio
async def test_cancelled_read_inspection_rolls_back_its_snapshot_transaction(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / f"cancel-inspection-{stage}.db"
    _create_legacy_database(path, 3)
    reached = asyncio.Event()
    release = asyncio.Event()
    connections: list[Any] = []
    rollback_calls = 0
    post_rollback_transactions: list[bool] = []
    original_open = migration_storage._readonly_connection
    original_rollback = migration_storage.aiosqlite.Connection.rollback

    async def capture_connection(database_path: Path) -> Any:
        connection = await original_open(database_path)
        connections.append(connection)
        return connection

    async def rollback(connection: Any) -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        await original_rollback(connection)
        post_rollback_transactions.append(connection.in_transaction)

    monkeypatch.setattr(migration_storage, "_readonly_connection", capture_connection)
    monkeypatch.setattr(migration_storage.aiosqlite.Connection, "rollback", rollback)
    if stage == "table-names":
        original_stage = migration_storage._table_names

        async def pause(connection: Any) -> Any:
            result = await original_stage(connection)
            reached.set()
            await release.wait()
            return result

        monkeypatch.setattr(migration_storage, "_table_names", pause)
    else:
        original_columns = migration_storage._migration_columns

        async def pause_columns(connection: Any) -> Any:
            result = await original_columns(connection)
            reached.set()
            await release.wait()
            return result

        monkeypatch.setattr(migration_storage, "_migration_columns", pause_columns)

    runner = await MigrationRunner.open(path)
    task = asyncio.create_task(runner.plan())
    await asyncio.wait_for(reached.wait(), timeout=2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert rollback_calls == 1
    assert post_rollback_transactions == [False]
    assert len(connections) == 1
    assert connections[0]._connection is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("'ready'", "'READY'"),
        ("'delete_pending'", "'delete_ pending'"),
    ],
    ids=["literal-case", "literal-whitespace"],
)
@pytest.mark.asyncio
async def test_schema_four_rejects_quoted_literal_semantic_changes(
    old: str,
    new: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"quoted-{old.strip(chr(39))}.db"
    runner = await MigrationRunner.open(path)
    await runner.apply()
    _rewrite_schema_sql(
        path,
        object_name="artifact_generations",
        old=old,
        new=new,
    )

    with pytest.raises(migration_storage.MigrationSchemaError, match="Artifact"):
        await runner.plan()


@pytest.mark.asyncio
async def test_v3_bootstrap_rejects_changed_quoted_literal_case(tmp_path: Path) -> None:
    path = tmp_path / "quoted-v3.db"
    _create_legacy_database(path, 3)
    _rewrite_schema_sql(
        path,
        object_name="external_operations",
        old="'model_call'",
        new="'MODEL_CALL'",
    )
    runner = await MigrationRunner.open(path)

    with pytest.raises(migration_storage.MigrationSchemaError, match="database schema"):
        await runner.apply()

    with sqlite3.connect(path) as connection:
        versions = tuple(
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
    assert versions == (1, 2, 3)


@pytest.mark.asyncio
async def test_unquoted_keyword_case_and_formatting_remain_compatible(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unquoted-formatting.db"
    runner = await MigrationRunner.open(path)
    await runner.apply()
    _rewrite_schema_sql(
        path,
        object_name="artifact_generations",
        old="CREATE TABLE artifact_generations",
        new="create  table\nartifact_generations",
    )

    assert await runner.plan() == ()


def test_sql_lexical_normalizer_preserves_quoted_tokens_and_fails_unclosed() -> None:
    assert _normalized_sql("SELECT a b") != _normalized_sql("SELECT ab")
    assert _normalized_sql("SELECT x1") != _normalized_sql("SELECT x 1")
    assert _normalized_sql("SELECT x >= 1") != _normalized_sql("SELECT x > = 1")
    assert _normalized_sql("SELECT 'ready'") != _normalized_sql("select 'READY'")
    assert _normalized_sql("SELECT 'a  b'") != _normalized_sql("SELECT 'a b'")
    assert _normalized_sql('SELECT "Name"') != _normalized_sql('SELECT "name"')
    assert _normalized_sql("SELECT `Name`") != _normalized_sql("SELECT `name`")
    assert _normalized_sql("SELECT [Name]") != _normalized_sql("SELECT [name]")
    assert _normalized_sql("SELECT 'it''s'") == _normalized_sql("select 'it''s'")
    for malformed in ("SELECT 'open", 'SELECT "open', "SELECT `open", "SELECT [open"):
        with pytest.raises(ValueError, match="malformed SQLite SQL"):
            _normalized_sql(malformed)


@pytest.mark.parametrize(
    ("stage", "expected_version"),
    [
        ("migration-1-statement-1-after", 0),
        ("migration-2-version-inserted", 1),
        ("migration-3-version-inserted", 2),
    ],
)
@pytest.mark.asyncio
async def test_empty_bootstrap_fault_rolls_back_only_the_current_migration(
    stage: str,
    expected_version: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / f"empty-fault-v{expected_version + 1}.db"

    async def fail_at(checkpoint: str) -> None:
        if checkpoint == stage:
            raise RuntimeError("injected legacy migration fault")

    monkeypatch.setattr(SQLiteStore, "_migration_checkpoint", staticmethod(fail_at))
    with pytest.raises(RuntimeError, match="injected legacy migration fault"):
        await (await MigrationRunner.open(path)).apply()

    assert _legacy_generation_signature(path) == _expected_legacy_generation(
        tmp_path, expected_version
    )


@pytest.mark.parametrize(
    ("existing_version", "stage"),
    [
        (1, "migration-2-version-inserted"),
        (2, "migration-3-version-inserted"),
    ],
)
@pytest.mark.asyncio
async def test_existing_legacy_fault_preserves_the_prior_exact_generation(
    existing_version: int,
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / f"existing-v{existing_version}-fault.db"
    _create_legacy_database(path, existing_version)
    before = _legacy_generation_signature(path)

    async def fail_at(checkpoint: str) -> None:
        if checkpoint == stage:
            raise RuntimeError("injected existing migration fault")

    monkeypatch.setattr(SQLiteStore, "_migration_checkpoint", staticmethod(fail_at))
    with pytest.raises(RuntimeError, match="injected existing migration fault"):
        await (await MigrationRunner.open(path)).apply()

    assert _legacy_generation_signature(path) == before


@pytest.mark.parametrize(
    ("stage", "expected_version"),
    [
        ("migration-1-statement-1-after", 0),
        ("migration-2-version-inserted", 1),
        ("migration-3-version-inserted", 2),
    ],
)
@pytest.mark.asyncio
async def test_empty_bootstrap_cancellation_preserves_prior_exact_generation(
    stage: str,
    expected_version: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / f"empty-cancel-v{expected_version + 1}.db"
    reached = asyncio.Event()
    release = asyncio.Event()

    async def pause_at(checkpoint: str) -> None:
        if checkpoint == stage:
            reached.set()
            await release.wait()

    monkeypatch.setattr(SQLiteStore, "_migration_checkpoint", staticmethod(pause_at))
    task = asyncio.create_task((await MigrationRunner.open(path)).apply())
    try:
        await asyncio.wait_for(reached.wait(), timeout=2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert _legacy_generation_signature(path) == _expected_legacy_generation(
        tmp_path, expected_version
    )


_MIGRATION_4_CHECKPOINTS = (
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


@pytest.mark.parametrize("stage", _MIGRATION_4_CHECKPOINTS)
@pytest.mark.asyncio
async def test_cancellation_at_every_migration_four_checkpoint_preserves_v3(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / f"cancel-{stage}.db"
    _create_legacy_database(path, 3)
    before = _legacy_generation_signature(path)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def pause_at(checkpoint: str) -> None:
        if checkpoint == stage:
            reached.set()
            await release.wait()

    monkeypatch.setattr(
        MigrationRunner, "_migration_checkpoint", staticmethod(pause_at)
    )
    task = asyncio.create_task((await MigrationRunner.open(path)).apply())
    try:
        await asyncio.wait_for(reached.wait(), timeout=2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert _legacy_generation_signature(path) == before


@pytest.mark.asyncio
async def test_begin_completed_before_cancellation_explicitly_rolls_back_and_unlocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "begin-cancel-race.db"
    reached = asyncio.Event()
    release = asyncio.Event()
    captured: list[Any] = []
    rollback_states: list[bool] = []
    original_execute = migration_storage.aiosqlite.Connection.execute
    original_rollback = migration_storage.aiosqlite.Connection.rollback
    paused = False

    def execute(connection: Any, sql: str, parameters: Any = None) -> Any:
        nonlocal paused
        result = original_execute(connection, sql, parameters)
        if paused or sql.strip().upper() != "BEGIN IMMEDIATE":
            return result
        paused = True

        async def complete_then_pause() -> Any:
            cursor = await result
            captured.append(connection)
            reached.set()
            await release.wait()
            return cursor

        return Result(complete_then_pause())

    async def rollback(connection: Any) -> None:
        await original_rollback(connection)
        if connection in captured:
            rollback_states.append(connection.in_transaction)

    monkeypatch.setattr(migration_storage.aiosqlite.Connection, "execute", execute)
    monkeypatch.setattr(migration_storage.aiosqlite.Connection, "rollback", rollback)
    task = asyncio.create_task((await MigrationRunner.open(path)).apply())
    await asyncio.wait_for(reached.wait(), timeout=2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert rollback_states == [False]
    runner = await MigrationRunner.open(path)
    await runner.apply()
    assert tuple(item.version for item in await runner.applied()) == (1, 2, 3, 4)


@pytest.mark.asyncio
async def test_commit_completed_before_cancellation_leaves_complete_v2_and_unlocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "commit-cancel-race.db"
    reached = asyncio.Event()
    release = asyncio.Event()
    original_commit = migration_storage.aiosqlite.Connection.commit
    commit_count = 0

    async def commit(connection: Any) -> None:
        nonlocal commit_count
        commit_count += 1
        await original_commit(connection)
        if commit_count == 3:
            reached.set()
            await release.wait()

    monkeypatch.setattr(migration_storage.aiosqlite.Connection, "commit", commit)
    task = asyncio.create_task((await MigrationRunner.open(path)).apply())
    await asyncio.wait_for(reached.wait(), timeout=2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert _legacy_generation_signature(path)[1] == (1, 2)
    store = await SQLiteStore.open(path)
    await store.close()
    assert tuple(
        item.version for item in await (await MigrationRunner.open(path)).applied()
    ) == (
        1,
        2,
        3,
        4,
    )


def _assert_sanitized_database_io_error(
    error: BaseException,
    *,
    secrets: tuple[str, ...],
) -> None:
    assert type(error).__name__ == "MigrationIOError"
    assert str(error) == "migration database I/O failed"
    for secret in secrets:
        assert secret not in str(error)


@pytest.mark.asyncio
async def test_database_path_resolution_failure_is_stable_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = tmp_path / "credential-token.db"
    os_message = "resolve denied for credential-token"
    original_resolve = Path.resolve

    def resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path == supplied:
            raise OSError(os_message)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(migration_storage.MigrationError) as captured:
        await MigrationRunner.open(supplied)

    _assert_sanitized_database_io_error(
        captured.value,
        secrets=(str(supplied), "credential-token", os_message),
    )


@pytest.mark.asyncio
async def test_parent_is_file_failure_is_stable_and_sanitized(tmp_path: Path) -> None:
    parent = tmp_path / "private-parent"
    parent.write_text("not a directory", encoding="utf-8")
    supplied = parent / "database.db"

    with pytest.raises(migration_storage.MigrationError) as captured:
        await (await MigrationRunner.open(supplied)).apply()

    _assert_sanitized_database_io_error(
        captured.value,
        secrets=(str(supplied), "private-parent", "File exists"),
    )


@pytest.mark.asyncio
async def test_denied_parent_failure_is_stable_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = tmp_path / "denied-parent" / "database.db"
    os_message = "permission denied at denied-parent"
    original_mkdir = Path.mkdir

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == supplied.parent:
            raise PermissionError(os_message)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(migration_storage.MigrationError) as captured:
        await (await MigrationRunner.open(supplied)).apply()

    _assert_sanitized_database_io_error(
        captured.value,
        secrets=(str(supplied), "denied-parent", os_message),
    )


@pytest.mark.asyncio
async def test_denied_database_connect_is_stable_for_store_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = tmp_path / "secret-connect.db"
    os_message = "access denied for secret-connect.db"

    def fail_connect(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise PermissionError(os_message)

    monkeypatch.setattr(migration_storage.aiosqlite, "connect", fail_connect)
    with pytest.raises(migration_storage.MigrationError) as captured:
        await SQLiteStore.open(supplied)

    _assert_sanitized_database_io_error(
        captured.value,
        secrets=(str(supplied), "secret-connect", os_message),
    )


@pytest.mark.asyncio
async def test_database_stat_failure_is_stable_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = tmp_path / "secret-stat.db"
    os_message = "stat blocked for secret-stat.db"
    runner = await MigrationRunner.open(supplied)
    original_exists = Path.exists

    def exists(path: Path) -> bool:
        if path == runner.path:
            raise OSError(os_message)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", exists)
    with pytest.raises(migration_storage.MigrationError) as captured:
        await runner.plan()

    _assert_sanitized_database_io_error(
        captured.value,
        secrets=(str(supplied), "secret-stat", os_message),
    )


@pytest.mark.parametrize("failure", ["configure", "wal"])
@pytest.mark.asyncio
async def test_configure_and_wal_failures_are_stable_and_sanitized(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = tmp_path / f"secret-{failure}.db"
    os_message = f"{failure} leaked secret credential"
    if failure == "configure":

        async def fail_configure(connection: Any) -> None:
            del connection
            raise RuntimeError(os_message)

        monkeypatch.setattr(SQLiteStore, "_configure_connection", fail_configure)
    else:
        original_execute = migration_storage.aiosqlite.Connection.execute

        def execute(connection: Any, sql: str, parameters: Any = None) -> Any:
            if sql.strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
                raise sqlite3.OperationalError(os_message)
            return original_execute(connection, sql, parameters)

        monkeypatch.setattr(migration_storage.aiosqlite.Connection, "execute", execute)

    with pytest.raises(migration_storage.MigrationError) as captured:
        await (await MigrationRunner.open(supplied)).apply()

    _assert_sanitized_database_io_error(
        captured.value,
        secrets=(str(supplied), f"secret-{failure}", os_message),
    )


@pytest.mark.asyncio
async def test_corrupt_sqlite_failure_is_stable_and_sanitized(tmp_path: Path) -> None:
    supplied = tmp_path / "secret-corrupt.db"
    supplied.write_bytes(b"not a SQLite database; credential=do-not-leak")

    with pytest.raises(migration_storage.MigrationError) as captured:
        await (await MigrationRunner.open(supplied)).applied()

    assert type(captured.value).__name__ == "MigrationSchemaError"
    assert str(captured.value) == "incompatible database schema"
    assert str(supplied) not in str(captured.value)
    assert "secret-corrupt" not in str(captured.value)
    assert "not a database" not in str(captured.value)


@pytest.mark.asyncio
async def test_packaged_resource_read_failure_has_separate_stable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = migration_storage.resources.files(migration_storage.__package__)
    os_message = "resource path and credential must not leak"

    class FailingResource:
        def read_bytes(self) -> bytes:
            raise OSError(os_message)

    class FailingRoot:
        def iterdir(self) -> Any:
            return root.iterdir()

        def joinpath(self, name: str) -> Any:
            if name == "0001_initial.sql":
                return FailingResource()
            return root.joinpath(name)

    monkeypatch.setattr(
        migration_storage.resources,
        "files",
        lambda package: FailingRoot(),
    )
    runner = await MigrationRunner.open(tmp_path / "resource.db")
    with pytest.raises(migration_storage.MigrationResourceError) as captured:
        await runner.plan()

    assert str(captured.value) == "packaged migration resource is unavailable"
    assert os_message not in str(captured.value)


@pytest.mark.asyncio
async def test_database_boundary_preserves_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancel_configure(connection: Any) -> None:
        del connection
        raise asyncio.CancelledError

    monkeypatch.setattr(SQLiteStore, "_configure_connection", cancel_configure)
    with pytest.raises(asyncio.CancelledError):
        await (await MigrationRunner.open(tmp_path / "cancel.db")).apply()
