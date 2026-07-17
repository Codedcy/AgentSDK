from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import Any, TypeVar, cast
from weakref import WeakValueDictionary

import aiosqlite


_T = TypeVar("_T")
_MIGRATION_2_TRANSFORM_ID = "session-ownership-v1-to-v2"
_NUMBERED_SQL = re.compile(r"^(?P<version>[0-9]{4})_[a-z0-9_]+\.sql$")
_RELEASE_MANIFEST = (
    (
        1,
        "0001_initial.sql",
        "bbba32d3480b1a2ce4d9e0443bcd118dbaad0f9e639622040922ba5fa2d796b3",
        (),
    ),
    (
        2,
        "0002_idempotency.sql",
        "1b3ad181c3ab0ab07b5d34cfe65297df77cbaee1df5582007e14420290ddcf2b",
        (_MIGRATION_2_TRANSFORM_ID,),
    ),
    (
        3,
        "0003_leases.sql",
        "63eaef03dcd1c10aabb6ce654374b8ae4d4bcc40477742a992ab2e26f933b7ee",
        (),
    ),
    (
        4,
        "0004_migration_checksums_and_artifacts.sql",
        "eae6246b32cb379b5f7245551d1caf519f68998b6f6e33b5fd07ee031bd4f935",
        (),
    ),
)
_COORDINATORS: WeakValueDictionary[str, _DatabaseCoordinator]
_COORDINATORS_LOCK = threading.RLock()
_ARTIFACT_TABLES = (
    "artifact_generations",
    "artifact_heads",
    "artifact_owners",
    "artifact_cleanup_jobs",
)
_ARTIFACT_INDEXES = (
    "artifact_generations_state_claim",
    "artifact_owners_session",
    "artifact_owners_generation",
    "artifact_cleanup_jobs_state_claim",
)


class MigrationError(ValueError):
    """Base class for stable migration failures."""


class MigrationResourceError(MigrationError):
    """The packaged migration release manifest is incomplete or untrusted."""


class MigrationIOError(MigrationError):
    """A database filesystem or SQLite open operation failed safely."""


class MigrationChecksumError(MigrationError):
    """Stored or packaged migration identity differs from the trusted release."""


class MigrationSchemaError(MigrationError):
    """The database schema or applied migration history is incompatible."""


class SchemaGenerationChangedError(RuntimeError):
    """A Store write was fenced because its opened schema generation is stale."""


def _is_corrupt_database_error(error: BaseException) -> bool:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if isinstance(code, int) and code & 0xFF in {
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
            }:
                return True
        current = (
            current.__cause__
            if current.__cause__ is not None
            else current.__context__
        )
    return False


def _database_boundary_error(error: BaseException) -> MigrationError:
    if _is_corrupt_database_error(error):
        return MigrationSchemaError("incompatible database schema")
    return MigrationIOError("migration database I/O failed")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    sql: str
    sql_bytes: bytes
    checksum: str
    identity_inputs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: int
    checksum: str
    applied_at: str


def _migration_checksum(sql_bytes: bytes, identity_inputs: tuple[str, ...]) -> str:
    identity = b"".join(b"\0" + value.encode("utf-8") for value in identity_inputs)
    return hashlib.sha256(sql_bytes + identity).hexdigest()


def _migration_resource_listing() -> tuple[Any, tuple[str, ...]]:
    try:
        root = resources.files(__package__)
        names: list[str] = []
        for child in root.iterdir():
            name = child.name
            if not isinstance(name, str):
                raise TypeError("packaged migration resource name is not text")
            names.append(name)
    except Exception as error:
        raise MigrationResourceError(
            "packaged migration resource is unavailable"
        ) from error
    return root, tuple(names)


def _read_migration_resource(root: Any, name: str) -> bytes:
    try:
        contents = root.joinpath(name).read_bytes()
        if not isinstance(contents, bytes):
            raise TypeError("packaged migration resource contents are not bytes")
    except Exception as error:
        raise MigrationResourceError(
            "packaged migration resource is unavailable"
        ) from error
    return contents


def _packaged_migrations() -> tuple[Migration, ...]:
    root, names = _migration_resource_listing()
    numbered: dict[int, str] = {}
    for name in names:
        match = _NUMBERED_SQL.fullmatch(name)
        if match is None:
            if name.endswith(".sql"):
                raise MigrationResourceError("packaged migration resource name is malformed")
            continue
        version = int(match.group("version"))
        if version in numbered:
            raise MigrationResourceError("duplicate packaged migration version")
        numbered[version] = name
    expected_names = {version: name for version, name, _, _ in _RELEASE_MANIFEST}
    if numbered != expected_names:
        raise MigrationResourceError("packaged migration manifest is incompatible")

    migrations: list[Migration] = []
    for version, name, expected_checksum, manifest_identity_inputs in _RELEASE_MANIFEST:
        identity_inputs = (_MIGRATION_2_TRANSFORM_ID,) if version == 2 else manifest_identity_inputs
        sql_bytes = _read_migration_resource(root, name)
        checksum = _migration_checksum(sql_bytes, identity_inputs)
        if checksum != expected_checksum:
            raise MigrationChecksumError("packaged migration checksum changed")
        try:
            sql = sql_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MigrationResourceError("packaged migration is not UTF-8") from error
        migrations.append(
            Migration(
                version=version,
                sql=sql,
                sql_bytes=sql_bytes,
                checksum=checksum,
                identity_inputs=identity_inputs,
            )
        )
    return tuple(migrations)


class _DatabaseCoordinator:
    _POLL_SECONDS = 0.01

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def __aenter__(self) -> _DatabaseCoordinator:
        cancel_acquire = threading.Event()

        def acquire() -> bool:
            while not cancel_acquire.is_set():
                if self._lock.acquire(timeout=self._POLL_SECONDS):
                    return True
            return False

        acquire_task = asyncio.create_task(asyncio.to_thread(acquire))
        try:
            acquired = await asyncio.shield(acquire_task)
        except asyncio.CancelledError as cancellation:
            cancel_acquire.set()
            while not acquire_task.done():
                try:
                    await asyncio.shield(acquire_task)
                except asyncio.CancelledError:
                    continue
            if acquire_task.result():
                self._lock.release()
            raise cancellation
        if not acquired:  # pragma: no cover - only cancellation sets the stop event
            raise RuntimeError("database coordinator acquisition stopped")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self._lock.release()


_COORDINATORS = WeakValueDictionary()


def _coordinator(identity: str) -> _DatabaseCoordinator:
    with _COORDINATORS_LOCK:
        coordinator = _COORDINATORS.get(identity)
        if coordinator is None:
            coordinator = _DatabaseCoordinator()
            _COORDINATORS[identity] = coordinator
        return coordinator


async def _close_connection(connection: aiosqlite.Connection) -> None:
    from agent_sdk.storage.sqlite import SQLiteStore

    close = asyncio.create_task(connection.close())
    await SQLiteStore._await_cleanup(close)


async def _settle_awaitable(
    operation: Callable[[], Awaitable[_T]],
) -> tuple[_T, asyncio.CancelledError | None]:
    async def run() -> _T:
        return await operation()

    task = asyncio.create_task(run())
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
        except BaseException:
            break
    return task.result(), cancellation


async def _rollback_if_active(connection: aiosqlite.Connection) -> None:
    if not connection.in_transaction:
        return
    await _settle_awaitable(connection.rollback)


@asynccontextmanager
async def _migration_transaction(
    connection: aiosqlite.Connection,
    *,
    immediate: bool,
    message: str,
) -> AsyncIterator[None]:
    async def begin() -> None:
        if immediate:
            import agent_sdk.storage.sqlite as sqlite_storage

            async def begin_immediate() -> None:
                await connection.execute("BEGIN IMMEDIATE")

            await sqlite_storage._with_busy_retry(
                begin_immediate,
                deadline=monotonic() + sqlite_storage._OPEN_RETRY_SECONDS,
                message=message,
            )
        else:
            await connection.execute("BEGIN")

    try:
        _, begin_cancellation = await _settle_awaitable(begin)
    except BaseException:
        await _rollback_if_active(connection)
        raise
    if begin_cancellation is not None:
        await _rollback_if_active(connection)
        raise begin_cancellation

    try:
        yield
    except BaseException:
        await _rollback_if_active(connection)
        raise
    else:
        try:
            _, commit_cancellation = await _settle_awaitable(connection.commit)
        except BaseException:
            await _rollback_if_active(connection)
            raise
        if commit_cancellation is not None:
            await _rollback_if_active(connection)
            raise commit_cancellation


async def _readonly_connection(path: Path) -> aiosqlite.Connection:
    wal_path = path.parent / f"{path.name}-wal"
    try:
        immutable = not wal_path.exists() or wal_path.stat().st_size == 0
    except OSError as error:
        raise MigrationIOError("migration database I/O failed") from error
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    try:
        connection = await aiosqlite.connect(f"{path.as_uri()}?{query}", uri=True)
    except (OSError, sqlite3.Error) as error:
        raise _database_boundary_error(error) from error
    try:
        await connection.execute("PRAGMA query_only=ON")
        await connection.execute("PRAGMA foreign_keys=ON")
    except (OSError, sqlite3.Error) as error:
        await _close_connection(connection)
        raise _database_boundary_error(error) from error
    except BaseException:
        await _close_connection(connection)
        raise
    return connection


async def _table_names(connection: aiosqlite.Connection) -> frozenset[str]:
    async with connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    ) as cursor:
        rows = await cursor.fetchall()
    return frozenset(cast(str, row[0]) for row in rows)


async def _migration_columns(connection: aiosqlite.Connection) -> tuple[str, ...]:
    async with connection.execute("PRAGMA table_info(schema_migrations)") as cursor:
        rows = await cursor.fetchall()
    return tuple(cast(str, row[1]) for row in rows)


async def _legacy_state(connection: aiosqlite.Connection) -> str | None:
    from agent_sdk.storage.sqlite import SQLiteStore

    columns = await _migration_columns(connection)
    if columns == ("version", "checksum", "applied_at"):
        return None
    if columns != ("version", "applied_at"):
        raise MigrationSchemaError("incompatible database migration table")
    try:
        return (await SQLiteStore._discover_schema_state(connection)).value
    except ValueError as error:
        raise MigrationSchemaError(str(error)) from error


async def _validate_legacy_state(connection: aiosqlite.Connection, state: str) -> None:
    import agent_sdk.storage.sqlite as sqlite_storage

    store = sqlite_storage.SQLiteStore
    expected_version = {"v1": 1, "v2": 2, "v3": 3}.get(state)
    if expected_version is None:
        raise MigrationSchemaError("incompatible database schema version")
    try:
        await store._validate_schema(connection, expected_version=expected_version)
        if state == "v1":
            await sqlite_storage._validated_v1_projection_transforms(connection)
        else:
            await store._validate_v2_projections(connection)
        if state == "v3":
            await store._validate_v3_rows(connection)
    except (TypeError, ValueError) as error:
        raise MigrationSchemaError(str(error)) from error


async def _legacy_applied(
    connection: aiosqlite.Connection,
    migrations: tuple[Migration, ...],
    state: str,
) -> tuple[AppliedMigration, ...]:
    await _validate_legacy_state(connection, state)
    async with connection.execute(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    ) as cursor:
        rows = list(await cursor.fetchall())
    expected_count = {"v1": 1, "v2": 2, "v3": 3}[state]
    if len(rows) != expected_count:
        raise MigrationSchemaError("incompatible database migration history")
    result: list[AppliedMigration] = []
    for expected, row in zip(migrations[:expected_count], rows, strict=True):
        version, applied_at = row
        if type(version) is not int or version != expected.version:
            raise MigrationSchemaError("incompatible database migration history")
        if not isinstance(applied_at, str) or not applied_at:
            raise MigrationSchemaError("incompatible database migration history")
        result.append(AppliedMigration(version, expected.checksum, applied_at))
    return tuple(result)


def _v4_statements(migration: Migration) -> tuple[str, ...]:
    from agent_sdk.storage.sqlite import _complete_sql_statements

    statements = _complete_sql_statements(migration.sql)
    if len(statements) != 11:
        raise MigrationResourceError("packaged migration 4 statement count changed")
    return statements


async def _validate_v4_schema(
    connection: aiosqlite.Connection,
    migrations: tuple[Migration, ...],
) -> tuple[AppliedMigration, ...]:
    import agent_sdk.storage.sqlite as sqlite_storage

    expected_tables = frozenset(sqlite_storage._EXPECTED_TABLE_INFO) | frozenset(_ARTIFACT_TABLES)
    if await _table_names(connection) != expected_tables:
        raise MigrationSchemaError("incompatible database schema")
    if await _migration_columns(connection) != (
        "version",
        "checksum",
        "applied_at",
    ):
        raise MigrationSchemaError("incompatible database migration table")

    statements = _v4_statements(migrations[3])
    async with connection.execute("PRAGMA table_info(schema_migrations)") as cursor:
        migration_info_rows = await cursor.fetchall()
    migration_info = tuple(
        (
            cast(str, row[1]),
            cast(str, row[2]).upper(),
            bool(row[3]),
            cast(int, row[5]),
        )
        for row in migration_info_rows
    )
    if migration_info != (
        ("version", "INTEGER", False, 1),
        ("checksum", "TEXT", True, 0),
        ("applied_at", "TEXT", True, 0),
    ):
        raise MigrationSchemaError("incompatible database migration table")
    expected_migration_sql = (
        statements[0].replace("schema_migrations_next", '"schema_migrations"', 1).rstrip(";")
    )
    async with connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ) as cursor:
        migration_sql_row = await cursor.fetchone()
    if migration_sql_row is None or not sqlite_storage._sql_shapes_equal(
        cast(str, migration_sql_row[0]), expected_migration_sql
    ):
        raise MigrationSchemaError("incompatible database migration table")

    expected_table_sql = {
        name: statement for name, statement in zip(_ARTIFACT_TABLES, statements[1:5], strict=True)
    }
    for table_name, expected_sql in expected_table_sql.items():
        async with connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or not sqlite_storage._sql_shapes_equal(
            cast(str, row[0]), expected_sql.rstrip(";")
        ):
            raise MigrationSchemaError("incompatible Artifact database schema")

    expected_index_sql = {
        name: statement for name, statement in zip(_ARTIFACT_INDEXES, statements[5:9], strict=True)
    }
    for index_name, expected_sql in expected_index_sql.items():
        async with connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or not sqlite_storage._sql_shapes_equal(
            cast(str, row[0]), expected_sql.rstrip(";")
        ):
            raise MigrationSchemaError("incompatible Artifact database schema")

    for table_name, expected_info in sqlite_storage._EXPECTED_TABLE_INFO.items():
        if table_name == "schema_migrations":
            continue
        async with connection.execute(f"PRAGMA table_info({table_name})") as cursor:
            rows = await cursor.fetchall()
        actual_info = tuple(
            (
                cast(str, row[1]),
                cast(str, row[2]).upper(),
                bool(row[3]),
                cast(int, row[5]),
            )
            for row in rows
        )
        if actual_info != expected_info:
            raise MigrationSchemaError("incompatible database schema")
        async with connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or not sqlite_storage._sql_shapes_equal(
            cast(str, row[0]), sqlite_storage._EXPECTED_TABLE_SQL[table_name]
        ):
            raise MigrationSchemaError("incompatible database schema")

    for index_name, expected_sql in sqlite_storage._EXPECTED_INDEX_SQL.items():
        async with connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or not sqlite_storage._sql_shapes_equal(
            cast(str, row[0]), expected_sql
        ):
            raise MigrationSchemaError("incompatible database schema")

    expected_index_names = frozenset(sqlite_storage._EXPECTED_INDEX_SQL) | frozenset(
        _ARTIFACT_INDEXES
    )
    async with connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'
        """
    ) as cursor:
        index_rows = await cursor.fetchall()
    if frozenset(cast(str, row[0]) for row in index_rows) != expected_index_names:
        raise MigrationSchemaError("incompatible database schema")

    await sqlite_storage.SQLiteStore._validate_v2_projections(connection)
    await sqlite_storage.SQLiteStore._validate_v3_rows(connection)
    async with connection.execute(
        "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"
    ) as cursor:
        rows = list(await cursor.fetchall())
    if len(rows) != len(migrations):
        raise MigrationSchemaError("incompatible database migration history")
    applied: list[AppliedMigration] = []
    for expected, row in zip(migrations, rows, strict=True):
        version, checksum, applied_at = row
        if type(version) is not int or version != expected.version:
            raise MigrationSchemaError("incompatible database migration history")
        if not isinstance(checksum, str) or checksum != expected.checksum:
            raise MigrationChecksumError("applied migration checksum changed")
        if not isinstance(applied_at, str) or not applied_at:
            raise MigrationSchemaError("incompatible database migration history")
        applied.append(AppliedMigration(version, checksum, applied_at))
    return tuple(applied)


async def _inspect_applied(
    path: Path, migrations: tuple[Migration, ...]
) -> tuple[AppliedMigration, ...]:
    try:
        missing_or_empty = not path.exists() or path.stat().st_size == 0
    except OSError as error:
        raise MigrationIOError("migration database I/O failed") from error
    if missing_or_empty:
        return ()
    connection = await _readonly_connection(path)
    try:
        return await _inspect_connection_applied(connection, migrations)
    except sqlite3.Error as error:
        raise _database_boundary_error(error) from error
    finally:
        await _close_connection(connection)


async def _inspect_connection_applied(
    connection: aiosqlite.Connection,
    migrations: tuple[Migration, ...],
) -> tuple[AppliedMigration, ...]:
    if connection.in_transaction:
        raise RuntimeError("managed migration inspection requires no transaction")
    async with _migration_transaction(
        connection,
        immediate=False,
        message="SQLite migration inspection conflict",
    ):
        return await _inspect_connection_applied_in_current_transaction(
            connection, migrations
        )


async def _inspect_connection_applied_in_current_transaction(
    connection: aiosqlite.Connection,
    migrations: tuple[Migration, ...],
) -> tuple[AppliedMigration, ...]:
    if not connection.in_transaction:
        raise RuntimeError(
            "transaction-local migration inspection requires a transaction"
        )
    tables = await _table_names(connection)
    if not tables:
        return ()
    if "schema_migrations" not in tables:
        raise MigrationSchemaError("incompatible database schema")
    state = await _legacy_state(connection)
    if state is not None:
        return await _legacy_applied(connection, migrations, state)
    return await _validate_v4_schema(connection, migrations)


async def _schema_generation(
    connection: aiosqlite.Connection,
    migrations: tuple[Migration, ...] | None = None,
) -> tuple[tuple[int, str], ...]:
    trusted = _packaged_migrations() if migrations is None else migrations
    columns = await _migration_columns(connection)
    if columns == ("version", "applied_at"):
        async with connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ) as cursor:
            rows = await cursor.fetchall()
        versions = tuple(row[0] for row in rows)
        if versions not in {(1,), (1, 2), (1, 2, 3)} or any(
            type(version) is not int for version in versions
        ):
            raise MigrationSchemaError("incompatible database migration history")
        return tuple((version, trusted[version - 1].checksum) for version in versions)
    if columns != ("version", "checksum", "applied_at"):
        raise MigrationSchemaError("incompatible database migration table")
    async with connection.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ) as cursor:
        rows = await cursor.fetchall()
    generation: list[tuple[int, str]] = []
    for row in rows:
        version, checksum = row
        if (
            type(version) is not int
            or not isinstance(checksum, str)
            or version < 1
            or version > len(trusted)
        ):
            raise MigrationSchemaError("incompatible database migration history")
        generation.append((version, checksum))
    return tuple(generation)


async def _apply_v4(
    connection: aiosqlite.Connection,
    migrations: tuple[Migration, ...],
    checkpoint: Any,
) -> None:
    async with _migration_transaction(
        connection,
        immediate=True,
        message="SQLite migration apply conflict",
    ):
        state = await _legacy_state(connection)
        if state is None:
            await _validate_v4_schema(connection, migrations)
            return
        if state != "v3":
            raise MigrationSchemaError("migration 4 requires exact schema version 3")
        await _validate_legacy_state(connection, state)
        await checkpoint("migration-4-legacy-validated")
        async with connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ) as cursor:
            historical_rows = list(await cursor.fetchall())
        if len(historical_rows) != 3:
            raise MigrationSchemaError("incompatible database migration history")

        statements = _v4_statements(migrations[3])
        for index, statement in enumerate(statements, start=1):
            await checkpoint(f"migration-4-statement-{index}-before")
            await connection.execute(statement)
            await checkpoint(f"migration-4-statement-{index}-after")
            if index == 1:
                for expected, row in zip(migrations[:3], historical_rows, strict=True):
                    version, applied_at = row
                    if type(version) is not int or version != expected.version:
                        raise MigrationSchemaError("incompatible database migration history")
                    if not isinstance(applied_at, str) or not applied_at:
                        raise MigrationSchemaError("incompatible database migration history")
                    await checkpoint(f"migration-4-copy-{expected.version}-before")
                    await connection.execute(
                        """
                        INSERT INTO schema_migrations_next(version, checksum, applied_at)
                        VALUES (?, ?, ?)
                        """,
                        (expected.version, expected.checksum, applied_at),
                    )
                    await checkpoint(f"migration-4-copy-{expected.version}-after")

        await checkpoint("migration-4-version-insert-before")
        await connection.execute(
            """
            INSERT INTO schema_migrations(version, checksum, applied_at)
            VALUES (?, ?, ?)
            """,
            (4, migrations[3].checksum, datetime.now(UTC).isoformat()),
        )
        await checkpoint("migration-4-version-insert-after")
        await _validate_v4_schema(connection, migrations)
        await checkpoint("migration-4-final-validation")


@dataclass(frozen=True, slots=True)
class MigrationRunner:
    path: Path
    database_identity: str

    @classmethod
    async def open(cls, path: str | Path) -> MigrationRunner:
        try:
            database_path = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise MigrationIOError("migration database I/O failed") from error
        identity = os.path.normcase(str(database_path))
        return cls(path=database_path, database_identity=identity)

    async def plan(self) -> tuple[Migration, ...]:
        migrations = _packaged_migrations()
        async with _coordinator(self.database_identity):
            applied = await _inspect_applied(self.path, migrations)
        return migrations[len(applied) :]

    async def applied(self) -> tuple[AppliedMigration, ...]:
        migrations = _packaged_migrations()
        async with _coordinator(self.database_identity):
            return await _inspect_applied(self.path, migrations)

    async def apply(self) -> None:
        migrations = _packaged_migrations()
        async with _coordinator(self.database_identity):
            await self._apply_locked(migrations)

    async def _apply_locked(
        self,
        migrations: tuple[Migration, ...],
        *,
        keep_open: bool = False,
    ) -> aiosqlite.Connection | None:
        import agent_sdk.storage.sqlite as sqlite_storage

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(self.path)
        except (OSError, sqlite3.Error) as error:
            raise _database_boundary_error(error) from error
        try:
            try:
                await sqlite_storage.SQLiteStore._configure_connection(connection)
            except (
                OSError,
                sqlite3.Error,
                sqlite_storage._SQLiteBusyExhaustedError,
                sqlite_storage._SQLiteConfigurationError,
            ) as error:
                raise _database_boundary_error(error) from error
            current = await _inspect_connection_applied(connection, migrations)
            if len(current) < 3:
                await sqlite_storage.SQLiteStore._migrate(connection, migrations)
            current = await _inspect_connection_applied(connection, migrations)
            if len(current) == 3:
                await _apply_v4(
                    connection,
                    migrations,
                    self._migration_checkpoint,
                )
            elif len(current) != 4:
                raise MigrationSchemaError("incompatible database migration history")
        except sqlite_storage._SQLiteBusyExhaustedError as error:
            await _close_connection(connection)
            raise MigrationIOError("migration database I/O failed") from error
        except sqlite3.Error as error:
            await _close_connection(connection)
            raise _database_boundary_error(error) from error
        except BaseException:
            await _close_connection(connection)
            raise
        if keep_open:
            return connection
        await _close_connection(connection)
        return None

    @staticmethod
    async def _migration_checkpoint(stage: str) -> None:
        del stage


__all__ = [
    "AppliedMigration",
    "Migration",
    "MigrationChecksumError",
    "MigrationError",
    "MigrationIOError",
    "MigrationResourceError",
    "MigrationRunner",
    "MigrationSchemaError",
    "SchemaGenerationChangedError",
]
