from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_sdk.runtime.leases import Lease, LeaseHeldError, LeaseLostError, LeaseManager
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.storage import sqlite as sqlite_storage


def test_lease_is_strict_frozen_and_requires_utc_ordered_timestamps() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    lease = Lease(
        run_id="run_1",
        owner="coordinator_1",
        generation=1,
        acquired_at=now,
        renewed_at=now,
        expires_at=now + timedelta(seconds=1),
    )

    with pytest.raises(ValidationError):
        Lease.model_validate({**lease.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        Lease.model_validate({**lease.model_dump(), "run_id": " "})
    with pytest.raises(ValidationError):
        Lease.model_validate({**lease.model_dump(), "generation": 0})
    with pytest.raises(ValidationError):
        Lease.model_validate(
            {**lease.model_dump(), "acquired_at": now.replace(tzinfo=None)}
        )
    with pytest.raises(ValidationError):
        Lease.model_validate({**lease.model_dump(), "expires_at": now})
    with pytest.raises(ValidationError):
        lease.owner = "changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_memory_acquire_rejects_unexpired_lease_even_for_same_owner() -> None:
    manager = LeaseManager(InMemoryStore(), ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)

    first = await manager.acquire("run_1", "coordinator_1", now=now)

    with pytest.raises(LeaseHeldError):
        await manager.acquire("run_1", first.owner, now=now + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_memory_expired_acquire_increments_generation() -> None:
    manager = LeaseManager(InMemoryStore(), ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)

    first = await manager.acquire("run_1", "coordinator_1", now=now)
    second = await manager.acquire(
        "run_1", "coordinator_2", now=first.expires_at
    )

    assert first.generation == 1
    assert second.generation == 2
    assert second.owner == "coordinator_2"


@pytest.mark.asyncio
async def test_memory_stale_generation_cannot_assert_renew_or_release() -> None:
    manager = LeaseManager(InMemoryStore(), ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    first = await manager.acquire("run_1", "coordinator_1", now=now)
    current = await manager.acquire("run_1", "coordinator_2", now=first.expires_at)

    with pytest.raises(LeaseLostError):
        await manager.assert_current(first, now=current.acquired_at)
    with pytest.raises(LeaseLostError):
        await manager.renew(first, now=current.acquired_at)
    with pytest.raises(LeaseLostError):
        await manager.release(first)


@pytest.mark.asyncio
async def test_memory_renew_preserves_generation_and_returns_detached_copy() -> None:
    manager = LeaseManager(InMemoryStore(), ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    acquired = await manager.acquire("run_1", "coordinator_1", now=now)

    renewed = await manager.renew(acquired, now=now + timedelta(seconds=5))

    assert renewed is not acquired
    assert renewed.generation == acquired.generation
    assert renewed.acquired_at == acquired.acquired_at
    assert renewed.renewed_at == now + timedelta(seconds=5)
    assert renewed.expires_at == now + timedelta(seconds=35)
    await manager.assert_current(renewed, now=renewed.renewed_at)


@pytest.mark.asyncio
async def test_memory_release_reacquire_preserves_generation_high_water() -> None:
    manager = LeaseManager(InMemoryStore(), ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    first = await manager.acquire("run_1", "coordinator_1", now=now)

    await manager.release(first)
    second = await manager.acquire("run_1", "coordinator_2", now=now)

    assert second.generation == first.generation + 1
    with pytest.raises(LeaseLostError):
        await manager.assert_current(first, now=now)


async def _assert_reverse_renew_order_is_fenced(manager: LeaseManager) -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    acquired = await manager.acquire("run_renew", "coordinator_1", now=now)
    barrier = asyncio.Event()
    ready = 0
    ready_lock = asyncio.Lock()
    newer_done = asyncio.Event()

    async def rendezvous() -> None:
        nonlocal ready
        async with ready_lock:
            ready += 1
            if ready == 2:
                barrier.set()
        await asyncio.wait_for(barrier.wait(), timeout=1)

    async def newer() -> Lease:
        await rendezvous()
        try:
            return await manager.renew(acquired, now=now + timedelta(seconds=10))
        finally:
            newer_done.set()

    async def older() -> BaseException | Lease:
        await rendezvous()
        await asyncio.wait_for(newer_done.wait(), timeout=1)
        try:
            return await manager.renew(acquired, now=now + timedelta(seconds=5))
        except BaseException as error:
            return error

    renewed, stale = await asyncio.wait_for(
        asyncio.gather(newer(), older()), timeout=3
    )
    assert isinstance(renewed, Lease)
    assert isinstance(stale, LeaseLostError)
    assert renewed.expires_at == now + timedelta(seconds=40)
    with pytest.raises(LeaseHeldError):
        await manager.acquire(
            "run_renew", "coordinator_2", now=now + timedelta(seconds=36)
        )


@pytest.mark.asyncio
async def test_memory_reverse_order_renew_keeps_maximum_expiry() -> None:
    await _assert_reverse_renew_order_is_fenced(
        LeaseManager(InMemoryStore(), ttl=timedelta(seconds=30))
    )


@pytest.mark.asyncio
async def test_memory_concurrent_acquire_has_one_winner() -> None:
    manager = LeaseManager(InMemoryStore(), ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    ready = 0
    ready_lock = asyncio.Lock()
    barrier = asyncio.Event()

    async def acquire(index: int) -> object:
        nonlocal ready
        async with ready_lock:
            ready += 1
            if ready == 32:
                barrier.set()
        await asyncio.wait_for(barrier.wait(), timeout=1)
        try:
            return await manager.acquire("run_1", f"coordinator_{index}", now=now)
        except LeaseHeldError as error:
            return error

    results = await asyncio.wait_for(
        asyncio.gather(*(acquire(index) for index in range(32))), timeout=3
    )

    assert sum(not isinstance(result, LeaseHeldError) for result in results) == 1
    assert sum(isinstance(result, LeaseHeldError) for result in results) == 31


@pytest.mark.asyncio
async def test_sqlite_acquire_rejects_unexpired_lease_even_for_same_owner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "leases.db"
    store = await SQLiteStore.open(path)
    manager = LeaseManager(store, ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    try:
        first = await manager.acquire("run_1", "coordinator_1", now=now)
        with pytest.raises(LeaseHeldError):
            await manager.acquire("run_1", first.owner, now=now + timedelta(seconds=1))
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_expiry_stale_operations_and_renewal(tmp_path: Path) -> None:
    path = tmp_path / "leases.db"
    store = await SQLiteStore.open(path)
    manager = LeaseManager(store, ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    try:
        first = await manager.acquire("run_1", "coordinator_1", now=now)
        second = await manager.acquire("run_1", "coordinator_2", now=first.expires_at)
        renewed = await manager.renew(second, now=second.renewed_at + timedelta(seconds=1))
        assert second.generation == 2
        assert renewed.generation == second.generation
        with pytest.raises(LeaseLostError):
            await manager.assert_current(first, now=renewed.renewed_at)
        with pytest.raises(LeaseLostError):
            await manager.renew(first, now=renewed.renewed_at)
        with pytest.raises(LeaseLostError):
            await manager.release(first)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_release_reacquire_preserves_durable_generation_high_water(
    tmp_path: Path,
) -> None:
    path = tmp_path / "release-reacquire.db"
    now = datetime(2026, 7, 14, tzinfo=UTC)
    store = await SQLiteStore.open(path)
    first_manager = LeaseManager(store, ttl=timedelta(seconds=30))
    first = await first_manager.acquire("run_1", "coordinator_1", now=now)
    await first_manager.release(first)
    await store.close()

    reopened = await SQLiteStore.open(path)
    second_manager = LeaseManager(reopened, ttl=timedelta(seconds=30))
    try:
        second = await second_manager.acquire("run_1", "coordinator_2", now=now)
        assert second.generation == first.generation + 1
        with pytest.raises(LeaseLostError):
            await second_manager.assert_current(first, now=now)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_sqlite_reverse_order_renew_keeps_maximum_expiry(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "reverse-renew.db")
    try:
        await _assert_reverse_renew_order_is_fenced(
            LeaseManager(store, ttl=timedelta(seconds=30))
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_concurrent_acquire_across_connections_has_one_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "leases.db"
    bootstrap = await SQLiteStore.open(path)
    await bootstrap.close()
    stores = await asyncio.gather(*(SQLiteStore.open(path) for _ in range(32)))
    managers = [LeaseManager(store, ttl=timedelta(seconds=30)) for store in stores]
    now = datetime(2026, 7, 14, tzinfo=UTC)
    ready = 0
    ready_lock = asyncio.Lock()
    barrier = asyncio.Event()

    async def acquire(index: int) -> object:
        nonlocal ready
        async with ready_lock:
            ready += 1
            if ready == 32:
                barrier.set()
        await asyncio.wait_for(barrier.wait(), timeout=3)
        try:
            return await managers[index].acquire(
                "run_1", f"coordinator_{index}", now=now
            )
        except LeaseHeldError as error:
            return error

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(acquire(index) for index in range(32))), timeout=10
        )
        assert sum(not isinstance(result, LeaseHeldError) for result in results) == 1
        assert sum(isinstance(result, LeaseHeldError) for result in results) == 31
    finally:
        await asyncio.gather(*(store.close() for store in stores))


@pytest.mark.parametrize("operation", ["acquire", "renew", "release", "assert"])
@pytest.mark.asyncio
async def test_sqlite_cancel_after_begin_rolls_back_lease_transaction(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / f"cancel-{operation}.db")
    manager = LeaseManager(store, ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    current = await manager.acquire("run_1", "coordinator_1", now=now)
    execute = store._connection.execute

    def execute_with_cancel(sql: str, *args: object, **kwargs: object) -> object:
        if sql != "BEGIN IMMEDIATE":
            return execute(sql, *args, **kwargs)

        async def begin_then_cancel() -> None:
            await execute(sql, *args, **kwargs)
            raise asyncio.CancelledError

        return begin_then_cancel()

    try:
        with monkeypatch.context() as cancelled:
            cancelled.setattr(store._connection, "execute", execute_with_cancel)
            with pytest.raises(asyncio.CancelledError):
                if operation == "acquire":
                    await manager.acquire("run_2", "coordinator_2", now=now)
                elif operation == "renew":
                    await manager.renew(current, now=now + timedelta(seconds=1))
                elif operation == "release":
                    await manager.release(current)
                else:
                    await manager.assert_current(current, now=now)

        if operation == "acquire":
            await manager.acquire("run_2", "coordinator_2", now=now)
        else:
            await manager.assert_current(current, now=now)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_cancel_racing_lease_commit_observes_durable_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "cancel-commit.db")
    manager = LeaseManager(store, ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    commit = store._connection.commit
    committed = asyncio.Event()
    release = asyncio.Event()
    task: asyncio.Task[Lease] | None = None

    async def commit_then_wait() -> None:
        await commit()
        committed.set()
        await release.wait()

    try:
        with monkeypatch.context() as race:
            race.setattr(store._connection, "commit", commit_then_wait)
            task = asyncio.create_task(
                manager.acquire("run_1", "coordinator_1", now=now)
            )
            await asyncio.wait_for(committed.wait(), timeout=1)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        with pytest.raises(LeaseHeldError):
            await manager.acquire("run_1", "coordinator_2", now=now)
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_lease_begin_retries_transient_busy_and_bounds_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "busy.db")
    manager = LeaseManager(store, ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    execute = store._connection.execute
    attempts = 0

    def transient_busy(sql: str, *args: object, **kwargs: object) -> object:
        nonlocal attempts
        if sql == "BEGIN IMMEDIATE" and attempts < 2:
            attempts += 1

            async def busy() -> None:
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error

            return busy()
        return execute(sql, *args, **kwargs)

    try:
        with monkeypatch.context() as transient:
            transient.setattr(store._connection, "execute", transient_busy)
            await manager.acquire("run_1", "coordinator_1", now=now)
        assert attempts == 2

        def always_busy(sql: str, *args: object, **kwargs: object) -> object:
            if sql == "BEGIN IMMEDIATE":

                async def busy() -> None:
                    error = sqlite3.OperationalError("database is locked")
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error

                return busy()
            return execute(sql, *args, **kwargs)

        with monkeypatch.context() as exhausted:
            exhausted.setattr(store._connection, "execute", always_busy)
            exhausted.setattr(sqlite_storage, "_OPEN_RETRY_SECONDS", 0.0)
            with pytest.raises(RuntimeError, match="lease acquisition conflict"):
                await manager.acquire("run_2", "coordinator_2", now=now)
    finally:
        await store.close()
