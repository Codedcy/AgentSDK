from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from agent_sdk.api import _LazySQLiteStore
from agent_sdk.errors import AgentSDKError, ErrorCode, SessionBusyError
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.leases import Lease, LeaseManager
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.models import (
    RunSnapshot,
    RunStatus,
    SessionSnapshot,
    SessionStatus,
    TokenUsage,
)
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
    RecoveryStateConflictError,
    RunCheckpoint,
    RunCheckpointPhase,
)
from agent_sdk.storage.base import CommitBatch, RunProgressBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


NOW = datetime(2026, 7, 15, 10, tzinfo=UTC)


@pytest_asyncio.fixture(params=("memory", "sqlite", "lazy_sqlite"))
async def scanner_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield InMemoryStore()
        return
    store: Any
    if request.param == "sqlite":
        store = await SQLiteStore.open(tmp_path / "scanner.db")
    else:
        store = _LazySQLiteStore(tmp_path / "lazy-scanner.db")
    try:
        yield store
    finally:
        await store.close()


def _run(status: RunStatus) -> RunSnapshot:
    return RunSnapshot(
        run_id="run_1",
        session_id="ses_1",
        agent_revision="agent:1",
        status=status,
        user_input="hello",
        version=2 if status is RunStatus.RUNNING else 3,
    )


def _snapshot_write(snapshot: RunSnapshot | SessionSnapshot) -> SnapshotWrite:
    if isinstance(snapshot, RunSnapshot):
        return SnapshotWrite(
            "run",
            snapshot.run_id,
            snapshot.session_id,
            snapshot.version,
            snapshot.model_dump(mode="json"),
        )
    return SnapshotWrite(
        "session",
        snapshot.session_id,
        snapshot.session_id,
        snapshot.version,
        snapshot.model_dump(mode="json"),
    )


async def _seed_abandoned(
    store: Any,
    status: RunStatus,
) -> tuple[RunSnapshot, SessionSnapshot, Lease, RunCheckpoint, ModelCallOperation]:
    run = _run(status)
    session = SessionSnapshot(
        session_id=run.session_id,
        status=SessionStatus.CLOSING,
        workspaces=("private-workspace",),
        active_run_ids=(run.run_id,),
    )
    tail = EventEnvelope(
        event_id="evt_existing_tail",
        type="run.progressed",
        session_id=run.session_id,
        run_id=run.run_id,
        sequence=7,
        payload={"status": run.status.value},
        occurred_at=NOW - timedelta(minutes=2),
    )
    await store.commit(
        CommitBatch(
            events=(tail,),
            snapshots=(_snapshot_write(session), _snapshot_write(run)),
        )
    )
    lease = await store.acquire_lease(
        run_id=run.run_id,
        owner="expired-owner",
        now=NOW - timedelta(minutes=2),
        expires_at=NOW - timedelta(minutes=1),
    )
    operation = ModelCallOperation(
        operation_id="op_unknown",
        session_id=run.session_id,
        run_id=run.run_id,
        turn=0,
        request_fingerprint="sha256:model",
        lease_generation=lease.generation,
        status=ExternalOperationStatus.STARTED,
        provider_identity="provider:model",
    )
    checkpoint = RunCheckpoint(
        run_id=run.run_id,
        session_id=run.session_id,
        checkpoint_version=1,
        turn=0,
        phase=RunCheckpointPhase.READY_FOR_MODEL,
        messages=({"role": "user", "content": "hello"},),
    )
    await store.create_external_operation(operation, lease=lease, now=lease.acquired_at)
    await store.put_run_checkpoint(
        checkpoint,
        expected=None,
        lease=lease,
        now=lease.acquired_at,
    )
    return run, session, lease, checkpoint, operation


@pytest.mark.parametrize(
    "status",
    (RunStatus.RUNNING, RunStatus.WAITING_PERMISSION),
)
@pytest.mark.asyncio
async def test_scanner_interrupts_abandoned_run_once_without_changing_owned_state(
    scanner_store: Any,
    status: RunStatus,
) -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    scanner_type = recovery.RecoveryScanner
    run, session, expired, checkpoint, operation = await _seed_abandoned(
        scanner_store,
        status,
    )
    scanner = scanner_type(
        scanner_store,
        lease_manager=LeaseManager(scanner_store, ttl=timedelta(seconds=30)),
        _clock=lambda: NOW,
    )

    await scanner.scan()
    await scanner.scan()

    durable_run_data = await scanner_store.get_snapshot("run", run.run_id)
    assert durable_run_data is not None
    durable_run = RunSnapshot.model_validate(durable_run_data)
    assert durable_run == run.model_copy(
        update={"status": RunStatus.INTERRUPTED, "version": run.version + 1}
    )
    events = [
        stored.event
        for stored in await scanner_store.read_events(after_cursor=0)
        if stored.event.run_id == run.run_id
    ]
    assert [(event.type, event.sequence, event.payload) for event in events] == [
        ("run.progressed", 7, {"status": status.value}),
        ("run.interrupted", 8, {"status": "interrupted"}),
    ]
    assert await scanner_store.get_run_checkpoint(run.run_id) == checkpoint
    assert await scanner_store.get_external_operation(operation.operation_id) == operation
    assert await scanner_store.get_snapshot("session", session.session_id) == (
        session.model_dump(mode="json")
    )
    assert durable_run.run_id in session.active_run_ids
    fresh = await scanner_store.acquire_lease(
        run_id=run.run_id,
        owner="after-scan",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    assert fresh.generation == expired.generation + 2


@pytest.mark.asyncio
async def test_scanner_never_interrupts_a_run_with_an_active_lease(
    scanner_store: Any,
) -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    run, _, expired, _, _ = await _seed_abandoned(
        scanner_store,
        RunStatus.RUNNING,
    )
    active = await scanner_store.acquire_lease(
        run_id=run.run_id,
        owner="new-live-owner",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    scanner = recovery.RecoveryScanner(scanner_store, _clock=lambda: NOW)

    await scanner.scan()

    assert await scanner_store.get_snapshot("run", run.run_id) == run.model_dump(
        mode="json"
    )
    assert await scanner_store.latest_run_event_sequence(run.run_id) == 7
    await scanner_store.assert_current_lease(active, now=NOW)
    assert active.generation == expired.generation + 1


@pytest.mark.asyncio
async def test_simultaneous_scanners_append_one_interruption_event() -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    store = InMemoryStore()
    run, _, _, _, _ = await _seed_abandoned(store, RunStatus.RUNNING)
    first = recovery.RecoveryScanner(store, _clock=lambda: NOW)
    second = recovery.RecoveryScanner(store, _clock=lambda: NOW)

    await asyncio.gather(first.scan(), second.scan())

    durable = await store.get_snapshot("run", run.run_id)
    assert durable is not None
    assert RunSnapshot.model_validate(durable).status is RunStatus.INTERRUPTED
    interruption_events = [
        stored.event
        for stored in await store.read_events(after_cursor=0)
        if stored.event.type == "run.interrupted"
    ]
    assert len(interruption_events) == 1
    assert interruption_events[0].sequence == 8


@pytest.mark.asyncio
async def test_expired_writer_is_fenced_after_scanner_claims_new_generation(
    scanner_store: Any,
) -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    run, _, expired, _, _ = await _seed_abandoned(
        scanner_store,
        RunStatus.RUNNING,
    )
    scanner = recovery.RecoveryScanner(scanner_store, _clock=lambda: NOW)
    await scanner.scan()
    stale_event = EventEnvelope(
        event_id="evt_stale_writer",
        type="run.progressed",
        session_id=run.session_id,
        run_id=run.run_id,
        sequence=9,
        payload={"source": "stale"},
        occurred_at=NOW,
    )

    with pytest.raises(RecoveryStateConflictError):
        await scanner_store.commit_run_progress(
            RunProgressBatch(
                lease=expired,
                now=NOW,
                events=(stale_event,),
            )
        )

    assert await scanner_store.latest_run_event_sequence(run.run_id) == 8


@pytest.mark.asyncio
async def test_scanner_has_no_external_capability_access() -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    delegate = InMemoryStore()
    await _seed_abandoned(delegate, RunStatus.RUNNING)

    class ExternalTrapStore:
        def __getattr__(self, name: str) -> Any:
            if name in {
                "models",
                "tools",
                "mcp",
                "workflow",
                "permission_bridge",
                "application_callback",
            }:
                raise AssertionError(f"external capability accessed: {name}")
            return getattr(delegate, name)

    scanner = recovery.RecoveryScanner(ExternalTrapStore(), _clock=lambda: NOW)

    await scanner.scan()

    assert await delegate.latest_run_event_sequence("run_1") == 8


class _PausingScannerAcquireStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.pause_scanner = False
        self.acquire_reached = asyncio.Event()
        self.allow_acquire = asyncio.Event()

    async def acquire_lease(
        self,
        *,
        run_id: str,
        owner: str,
        now: datetime,
        expires_at: datetime,
    ) -> Lease:
        if self.pause_scanner and owner.startswith("coord_"):
            self.acquire_reached.set()
            await self.allow_acquire.wait()
        return await super().acquire_lease(
            run_id=run_id,
            owner=owner,
            now=now,
            expires_at=expires_at,
        )


@pytest.mark.parametrize("race", ("terminal", "delete", "new_live_owner"))
@pytest.mark.asyncio
async def test_scan_races_resolve_without_partial_interruption(race: str) -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    store = _PausingScannerAcquireStore()
    run, session, _, _, _ = await _seed_abandoned(store, RunStatus.RUNNING)
    store.pause_scanner = True
    scanner = recovery.RecoveryScanner(store, _clock=lambda: NOW)
    task = asyncio.create_task(scanner.scan())
    await asyncio.wait_for(store.acquire_reached.wait(), timeout=2)

    active: Lease | None = None
    if race == "terminal":
        completed = run.model_copy(
            update={
                "status": RunStatus.COMPLETED,
                "version": run.version + 1,
                "output_text": "done",
                "usage": TokenUsage(),
            }
        )
        closed = session.model_copy(
            update={
                "status": SessionStatus.CLOSED,
                "active_run_ids": (),
                "version": session.version + 1,
            }
        )
        await store.commit(
            CommitBatch(
                events=(),
                snapshots=(_snapshot_write(completed), _snapshot_write(closed)),
            )
        )
    elif race == "delete":
        await store.delete_session(session.session_id)
    else:
        active = await store.acquire_lease(
            run_id=run.run_id,
            owner="live-race-winner",
            now=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )
    store.allow_acquire.set()
    await asyncio.wait_for(task, timeout=2)

    assert not any(
        stored.event.type == "run.interrupted"
        for stored in await store.read_events(after_cursor=0)
    )
    if race == "terminal":
        durable = await store.get_snapshot("run", run.run_id)
        assert durable is not None
        assert RunSnapshot.model_validate(durable).status is RunStatus.COMPLETED
    elif race == "delete":
        assert await store.get_snapshot("run", run.run_id) is None
        assert await store.get_snapshot("session", session.session_id) is None
    else:
        assert active is not None
        await store.assert_current_lease(active, now=NOW)


class _BlockingFailingReleaseStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.block_release = False
        self.release_calls = 0
        self.release_reached = asyncio.Event()
        self.allow_release = asyncio.Event()

    async def release_lease(self, lease: Lease) -> None:
        if not self.block_release:
            await super().release_lease(lease)
            return
        self.release_calls += 1
        self.release_reached.set()
        await self.allow_release.wait()
        await super().release_lease(lease)
        raise RuntimeError("late release secret")


@pytest.mark.asyncio
async def test_scanner_double_cancel_settles_one_late_failing_release() -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    store = _BlockingFailingReleaseStore()
    run, _, expired, _, _ = await _seed_abandoned(store, RunStatus.RUNNING)
    store.block_release = True
    scanner = recovery.RecoveryScanner(store, _clock=lambda: NOW)
    loop = asyncio.get_running_loop()
    late_errors: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: late_errors.append(context))
    try:
        task = asyncio.create_task(scanner.scan())
        await asyncio.wait_for(store.release_reached.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        store.allow_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        loop.set_exception_handler(previous_handler)

    assert store.release_calls == 1
    assert late_errors == []
    assert not any(
        pending is not asyncio.current_task()
        and "release" in repr(pending.get_coro()).casefold()
        for pending in asyncio.all_tasks()
    )
    fresh = await store.acquire_lease(
        run_id=run.run_id,
        owner="after-cancel",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    assert fresh.generation == expired.generation + 2


@pytest.mark.asyncio
async def test_interrupted_run_keeps_closing_session_busy() -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    store = InMemoryStore()
    run, session, _, _, _ = await _seed_abandoned(store, RunStatus.RUNNING)
    scanner = recovery.RecoveryScanner(store, _clock=lambda: NOW)
    await scanner.scan()
    commands = RuntimeCommands(store)

    closed = await commands.close_session(session.session_id)

    assert closed.status is SessionStatus.CLOSING
    assert closed.active_run_ids == (run.run_id,)
    with pytest.raises(SessionBusyError):
        await commands.delete_session(session.session_id)


@pytest.mark.asyncio
async def test_scanner_uses_sequence_one_when_run_has_no_event_tail() -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    store = InMemoryStore()
    run = _run(RunStatus.RUNNING)
    session = SessionSnapshot(
        session_id=run.session_id,
        workspaces=("workspace",),
        active_run_ids=(run.run_id,),
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(_snapshot_write(session), _snapshot_write(run)),
        )
    )
    scanner = recovery.RecoveryScanner(store, _clock=lambda: NOW)

    await scanner.scan()

    events = await store.read_events(after_cursor=0)
    assert [(stored.event.type, stored.event.sequence) for stored in events] == [
        ("run.interrupted", 1)
    ]


def _sdk_traceback_locals(error: BaseException) -> tuple[dict[str, Any], ...]:
    frames: list[dict[str, Any]] = []
    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in filename:
            frames.append(dict(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return tuple(frames)


@pytest.mark.parametrize("corruption", ("malformed_tail", "session_ownership"))
@pytest.mark.asyncio
async def test_scanner_corrupt_state_fails_closed_without_retaining_secret(
    corruption: str,
) -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    store = InMemoryStore()
    run, _, _, _, _ = await _seed_abandoned(store, RunStatus.RUNNING)
    secret = f"scanner-secret-{corruption}-8a1f"
    async with store._lock:
        if corruption == "malformed_tail":
            stored = store._events[0]
            event = stored.event.model_copy(
                update={"session_id": secret, "sequence": 0}
            )
            store._events[0] = stored._replace(event=event)
        else:
            write = store._snapshots[("session", "ses_1")]
            data = dict(write.data)
            data["workspaces"] = [secret]
            data["active_run_ids"] = []
            store._snapshots[("session", "ses_1")] = write._replace(data=data)
    scanner = recovery.RecoveryScanner(store, _clock=lambda: NOW)

    with pytest.raises(AgentSDKError) as caught:
        await scanner.scan()

    assert caught.value.code is ErrorCode.CONFLICT
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    sdk_locals = _sdk_traceback_locals(caught.value)
    assert sdk_locals
    assert all(secret not in repr(frame) for frame in sdk_locals)
    assert not any(
        stored.event.type == "run.interrupted"
        for stored in await store.read_events(after_cursor=0)
    )
    if corruption == "malformed_tail":
        fresh = await store.acquire_lease(
            run_id=run.run_id,
            owner="after-corrupt-scan",
            now=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )
        assert fresh.generation == 3


class _AmbiguousScannerCommitStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.progress_batches: list[RunProgressBatch] = []

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        self.progress_batches.append(batch)
        result = await super().commit_run_progress(batch)
        if len(self.progress_batches) == 1:
            raise RuntimeError("ambiguous scanner commit")
        return result


@pytest.mark.asyncio
async def test_scanner_replays_identical_batch_after_ambiguous_commit() -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    store = _AmbiguousScannerCommitStore()
    await _seed_abandoned(store, RunStatus.RUNNING)
    scanner = recovery.RecoveryScanner(store, _clock=lambda: NOW)

    await scanner.scan()

    assert len(store.progress_batches) == 2
    assert store.progress_batches[0] is store.progress_batches[1]
    assert len(
        [
            stored
            for stored in await store.read_events(after_cursor=0)
            if stored.event.type == "run.interrupted"
        ]
    ) == 1


class _CancellationSuppressingCommitStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.commit_reached = asyncio.Event()
        self.allow_commit = asyncio.Event()
        self.suppressed_cancellations = 0
        self.progress_batches: list[RunProgressBatch] = []

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        self.progress_batches.append(batch)
        self.commit_reached.set()
        while not self.allow_commit.is_set():
            try:
                await self.allow_commit.wait()
            except asyncio.CancelledError:
                self.suppressed_cancellations += 1
        return await super().commit_run_progress(batch)


@pytest.mark.asyncio
async def test_scanner_propagates_double_cancel_after_settling_suppressed_commit() -> None:
    recovery = importlib.import_module("agent_sdk.runtime.recovery")
    store = _CancellationSuppressingCommitStore()
    run, _, expired, _, _ = await _seed_abandoned(store, RunStatus.RUNNING)
    scanner = recovery.RecoveryScanner(store, _clock=lambda: NOW)
    task = asyncio.create_task(scanner.scan())
    await asyncio.wait_for(store.commit_reached.wait(), timeout=2)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    store.allow_commit.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert store.suppressed_cancellations == 0
    assert len(store.progress_batches) == 1
    assert await store.latest_run_event_sequence(run.run_id) == 8
    fresh = await store.acquire_lease(
        run_id=run.run_id,
        owner="after-commit-cancel",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    assert fresh.generation == expired.generation + 2
