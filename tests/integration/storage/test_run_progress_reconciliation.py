from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from agent_sdk.api import _LazySQLiteStore
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
    RecoveryStateConflictError,
)
from agent_sdk.storage import base as storage_base
from agent_sdk.storage.base import (
    CommitBatch,
    ExternalOperationWrite,
    RunProgressBatch,
    SnapshotPrecondition,
    SnapshotWrite,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


NOW = datetime(2026, 7, 15, 9, tzinfo=UTC)


@pytest_asyncio.fixture(params=("memory", "sqlite"))
async def progress_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield InMemoryStore()
        return
    store = await SQLiteStore.open(tmp_path / "reconciliation-progress.db")
    try:
        yield store
    finally:
        await store.close()


def _run(**updates: object) -> RunSnapshot:
    values: dict[str, object] = {
        "run_id": "run_1",
        "session_id": "ses_1",
        "agent_revision": "agent:1",
        "status": RunStatus.RUNNING,
        "user_input": "hello",
        "version": 2,
    }
    values.update(updates)
    return RunSnapshot.model_validate(values)


def _run_write(run: RunSnapshot) -> SnapshotWrite:
    return SnapshotWrite(
        "run",
        run.run_id,
        run.session_id,
        run.version,
        run.model_dump(mode="json"),
    )


def _request(**updates: object) -> ReconciliationRequest:
    values: dict[str, object] = {
        "request_id": "rec_1",
        "session_id": "ses_1",
        "run_id": "run_1",
        "reason": "unknown outcome",
        "details": {"source": "scanner"},
    }
    values.update(updates)
    return ReconciliationRequest.model_validate(values)


def _operation(
    run: RunSnapshot,
    lease: Lease,
    *,
    operation_id: str = "op_unknown",
) -> ModelCallOperation:
    return ModelCallOperation(
        operation_id=operation_id,
        session_id=run.session_id,
        run_id=run.run_id,
        turn=0,
        request_fingerprint="sha256:model",
        lease_generation=lease.generation,
        status=ExternalOperationStatus.STARTED,
        provider_identity="provider:model",
    )


def _event(run: RunSnapshot, request: ReconciliationRequest) -> EventEnvelope:
    return EventEnvelope(
        event_id="evt_waiting_reconciliation",
        type="run.waiting_reconciliation",
        session_id=run.session_id,
        run_id=run.run_id,
        sequence=1,
        payload={"request_id": request.request_id},
        occurred_at=NOW,
    )


def _resolved(request: ReconciliationRequest) -> ReconciliationRequest:
    return request.model_copy(
        update={
            "status": ReconciliationStatus.RESOLVED,
            "resolution": ReconciliationResolution(
                action=ReconciliationAction.TERMINATE,
                actor={"type": "operator", "id": "user_1"},
                evidence={"reason": "provider result unavailable"},
                decided_at=NOW,
                event_id="evt_reconciliation_resolved",
            ),
        }
    )


def _resolution_event(resolved: ReconciliationRequest) -> EventEnvelope:
    assert resolved.resolution is not None
    resolution = resolved.resolution
    return EventEnvelope(
        event_id=resolution.event_id,
        type="reconciliation.resolved",
        session_id=resolved.session_id,
        run_id=resolved.run_id,
        sequence=1,
        payload={
            "request_id": resolved.request_id,
            "operation_id": resolved.operation_id,
            "action": resolution.action.value,
            "actor": {"type": "operator", "id": "user_1"},
            "evidence": {"reason": "provider result unavailable"},
        },
        occurred_at=resolution.decided_at,
    )


async def _seed(store: Any) -> tuple[RunSnapshot, Lease]:
    run = _run()
    session = SessionSnapshot(
        session_id=run.session_id,
        workspaces=("workspace",),
        active_run_ids=(run.run_id,),
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "session",
                    session.session_id,
                    session.session_id,
                    session.version,
                    session.model_dump(mode="json"),
                ),
                _run_write(run),
            ),
        )
    )
    lease = await store.acquire_lease(
        run_id=run.run_id,
        owner="scanner_1",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    return run, lease


def _create_batch(
    run: RunSnapshot,
    lease: Lease,
    request: ReconciliationRequest,
) -> tuple[RunProgressBatch, RunSnapshot, EventEnvelope]:
    updated = run.model_copy(
        update={"status": RunStatus.WAITING_RECONCILIATION, "version": 3}
    )
    event = _event(updated, request)
    return (
        RunProgressBatch(
            lease=lease,
            now=NOW,
            events=(event,),
            snapshots=(_run_write(updated),),
            preconditions=(
                SnapshotPrecondition(
                    "run",
                    run.run_id,
                    run.version,
                    run.session_id,
                    run.model_dump(mode="json"),
                ),
            ),
            reconciliation=storage_base.ReconciliationRequestWrite(
                expected=None,
                updated=request,
            ),
        ),
        updated,
        event,
    )


@pytest.mark.asyncio
async def test_run_progress_atomically_creates_reconciliation_request(
    progress_store: Any,
) -> None:
    assert hasattr(storage_base, "ReconciliationRequestWrite")
    run, lease = await _seed(progress_store)
    request = _request()
    batch, updated, event = _create_batch(run, lease, request)

    result = await progress_store.commit_run_progress(batch)

    assert result == storage_base.CommitResult(last_cursor=1, applied=True)
    assert await progress_store.get_reconciliation_request(request.request_id) == request
    assert await progress_store.get_snapshot("run", run.run_id) == updated.model_dump(
        mode="json"
    )
    assert [
        stored.event for stored in await progress_store.read_events(after_cursor=0)
    ] == [event]


@pytest.mark.asyncio
async def test_run_progress_atomically_resolves_reconciliation_request(
    progress_store: Any,
) -> None:
    run, lease = await _seed(progress_store)
    request = _request()
    await progress_store.create_reconciliation_request(request)
    resolved = _resolved(request)
    interrupted = run.model_copy(
        update={"status": RunStatus.INTERRUPTED, "version": 3}
    )
    event = _resolution_event(resolved)
    batch = RunProgressBatch(
        lease=lease,
        now=NOW,
        events=(event,),
        snapshots=(_run_write(interrupted),),
        preconditions=(
            SnapshotPrecondition(
                "run",
                run.run_id,
                run.version,
                run.session_id,
                run.model_dump(mode="json"),
            ),
        ),
        reconciliation=storage_base.ReconciliationRequestWrite(
            expected=request,
            updated=resolved,
        ),
    )

    result = await progress_store.commit_run_progress(batch)

    assert result == storage_base.CommitResult(last_cursor=1, applied=True)
    assert await progress_store.get_reconciliation_request(request.request_id) == resolved
    assert await progress_store.get_snapshot("run", run.run_id) == (
        interrupted.model_dump(mode="json")
    )
    assert [
        stored.event for stored in await progress_store.read_events(after_cursor=0)
    ] == [event]


@pytest.mark.asyncio
async def test_reconciliation_target_exact_replay_ignores_released_lease(
    progress_store: Any,
) -> None:
    run, lease = await _seed(progress_store)
    request = _request()
    batch, updated, event = _create_batch(run, lease, request)
    applied = await progress_store.commit_run_progress(batch)
    await progress_store.release_lease(lease)

    replay = await progress_store.commit_run_progress(batch)

    assert applied == storage_base.CommitResult(last_cursor=1, applied=True)
    assert replay == storage_base.CommitResult(last_cursor=1, applied=False)
    assert await progress_store.get_reconciliation_request(request.request_id) == request
    assert await progress_store.get_snapshot("run", run.run_id) == updated.model_dump(
        mode="json"
    )
    assert [
        stored.event for stored in await progress_store.read_events(after_cursor=0)
    ] == [event]


@pytest.mark.asyncio
async def test_reconciliation_update_exact_replay_ignores_expired_lease(
    progress_store: Any,
) -> None:
    run, lease = await _seed(progress_store)
    request = _request()
    await progress_store.create_reconciliation_request(request)
    resolved = _resolved(request)
    interrupted = run.model_copy(
        update={"status": RunStatus.INTERRUPTED, "version": 3}
    )
    event = _resolution_event(resolved)
    batch = RunProgressBatch(
        lease=lease,
        now=NOW,
        events=(event,),
        snapshots=(_run_write(interrupted),),
        reconciliation=storage_base.ReconciliationRequestWrite(request, resolved),
    )
    await progress_store.commit_run_progress(batch)

    replay = await progress_store.commit_run_progress(
        batch._replace(now=lease.expires_at)
    )

    assert replay == storage_base.CommitResult(last_cursor=1, applied=False)
    assert await progress_store.get_reconciliation_request(request.request_id) == resolved


@pytest.mark.asyncio
async def test_reconciliation_partial_target_replay_conflicts_without_mutation(
    progress_store: Any,
) -> None:
    run, lease = await _seed(progress_store)
    request = _request()
    await progress_store.create_reconciliation_request(request)
    batch, _, _ = _create_batch(run, lease, request)

    with pytest.raises(RecoveryStateConflictError):
        await progress_store.commit_run_progress(batch)

    assert await progress_store.latest_cursor() == 0
    assert await progress_store.get_snapshot("run", run.run_id) == run.model_dump(
        mode="json"
    )
    assert await progress_store.get_reconciliation_request(request.request_id) == request


@pytest.mark.parametrize(
    "invalidity",
    ("stale_expected", "foreign_scope", "changed_immutable_reason"),
)
@pytest.mark.asyncio
async def test_reconciliation_target_rejects_cas_scope_and_shape_failures(
    progress_store: Any,
    invalidity: str,
) -> None:
    run, lease = await _seed(progress_store)
    request = _request()
    interrupted = run.model_copy(
        update={"status": RunStatus.INTERRUPTED, "version": 3}
    )
    if invalidity == "foreign_scope":
        write = storage_base.ReconciliationRequestWrite(
            None,
            request.model_copy(update={"session_id": "ses_foreign"}),
        )
        event = _event(interrupted, request)
    else:
        await progress_store.create_reconciliation_request(request)
        expected = (
            request.model_copy(update={"details": {"source": "stale"}})
            if invalidity == "stale_expected"
            else request
        )
        resolved = _resolved(expected)
        if invalidity == "changed_immutable_reason":
            resolved = resolved.model_copy(update={"reason": "changed"})
        event = _resolution_event(resolved)
        write = storage_base.ReconciliationRequestWrite(expected, resolved)
    batch = RunProgressBatch(
        lease=lease,
        now=NOW,
        events=(event,),
        snapshots=(_run_write(interrupted),),
        reconciliation=write,
    )

    with pytest.raises(RecoveryStateConflictError):
        await progress_store.commit_run_progress(batch)

    assert await progress_store.latest_cursor() == 0
    assert await progress_store.get_snapshot("run", run.run_id) == run.model_dump(
        mode="json"
    )


@pytest.mark.asyncio
async def test_memory_reconciliation_target_cancellation_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    run, lease = await _seed(store)
    request = _request(details={"secret": "cancel-secret"})
    batch, _, _ = _create_batch(run, lease, request)

    def cancel_before_publish(_: list[storage_base.StoredEvent]) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(store, "_latest_sequences", cancel_before_publish)

    with pytest.raises(asyncio.CancelledError):
        await store.commit_run_progress(batch)

    assert await store.latest_cursor() == 0
    assert await store.get_reconciliation_request(request.request_id) is None
    assert await store.get_snapshot("run", run.run_id) == run.model_dump(mode="json")


@pytest.mark.asyncio
async def test_sqlite_reconciliation_target_fault_rolls_back_every_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "reconciliation-fault.db")
    try:
        run, lease = await _seed(store)
        request = _request(details={"secret": "fault-secret"})
        batch, _, _ = _create_batch(run, lease, request)

        async def fail_before_commit() -> None:
            raise RuntimeError("injected reconciliation commit fault")

        monkeypatch.setattr(store, "_commit_transaction", fail_before_commit)
        with pytest.raises(RuntimeError, match="injected reconciliation"):
            await store.commit_run_progress(batch)

        assert await store.latest_cursor() == 0
        assert await store.get_reconciliation_request(request.request_id) is None
        assert await store.get_snapshot("run", run.run_id) == run.model_dump(
            mode="json"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_reconciliation_commit_race_replays_identical_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "reconciliation-race.db")
    try:
        run, lease = await _seed(store)
        request = _request()
        batch, _, event = _create_batch(run, lease, request)
        original_commit = store._connection.commit
        committed = asyncio.Event()
        release = asyncio.Event()

        async def commit_then_wait() -> None:
            await original_commit()
            committed.set()
            await release.wait()

        with monkeypatch.context() as race:
            race.setattr(store._connection, "commit", commit_then_wait)
            task = asyncio.create_task(store.commit_run_progress(batch))
            await asyncio.wait_for(committed.wait(), timeout=2)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)

        replay = await store.commit_run_progress(batch)

        assert replay == storage_base.CommitResult(last_cursor=1, applied=False)
        assert await store.get_reconciliation_request(request.request_id) == request
        assert [
            stored.event for stored in await store.read_events(after_cursor=0)
        ] == [event]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reconciliation_target_is_removed_with_session(
    progress_store: Any,
) -> None:
    run, lease = await _seed(progress_store)
    request = _request()
    batch, _, _ = _create_batch(run, lease, request)
    await progress_store.commit_run_progress(batch)

    await progress_store.delete_session(run.session_id)

    assert await progress_store.get_reconciliation_request(request.request_id) is None
    assert await progress_store.list_pending_reconciliation_requests(run.run_id) == ()


@pytest.mark.asyncio
async def test_lazy_sqlite_forwards_exact_reconciliation_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = Lease(
        run_id="run_1",
        owner="scanner_1",
        generation=1,
        acquired_at=NOW,
        renewed_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    request = _request()
    batch = RunProgressBatch(
        lease=lease,
        now=NOW,
        reconciliation=storage_base.ReconciliationRequestWrite(None, request),
    )
    expected = storage_base.CommitResult(last_cursor=7, applied=False)

    class Recorder:
        received: RunProgressBatch | None = None

        async def commit_run_progress(
            self, received: RunProgressBatch
        ) -> storage_base.CommitResult:
            self.received = received
            return expected

    recorder = Recorder()
    lazy = _LazySQLiteStore(tmp_path / "lazy-reconciliation.db")

    async def get_recorder() -> Any:
        return recorder

    monkeypatch.setattr(lazy, "_get", get_recorder)

    result = await lazy.commit_run_progress(batch)

    assert result is expected
    assert recorder.received is batch


@pytest.mark.asyncio
async def test_reconciliation_request_can_link_operation_created_in_same_batch(
    progress_store: Any,
) -> None:
    run, lease = await _seed(progress_store)
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
    request = _request(operation_id=operation.operation_id)
    batch, _, _ = _create_batch(run, lease, request)
    batch = batch._replace(operation=ExternalOperationWrite(None, operation))

    await progress_store.commit_run_progress(batch)

    assert await progress_store.get_external_operation(operation.operation_id) == operation
    assert await progress_store.get_reconciliation_request(request.request_id) == request


@pytest.mark.asyncio
async def test_reconciliation_request_cannot_link_foreign_run_operation(
    progress_store: Any,
) -> None:
    run, lease = await _seed(progress_store)
    other_run = _run(run_id="run_other", session_id="ses_other")
    other_session = SessionSnapshot(
        session_id=other_run.session_id,
        workspaces=("other",),
        active_run_ids=(other_run.run_id,),
    )
    await progress_store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "session",
                    other_session.session_id,
                    other_session.session_id,
                    other_session.version,
                    other_session.model_dump(mode="json"),
                ),
                _run_write(other_run),
            ),
        )
    )
    other_lease = await progress_store.acquire_lease(
        run_id=other_run.run_id,
        owner="other-owner",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    foreign_operation = ModelCallOperation(
        operation_id="op_foreign",
        session_id=other_run.session_id,
        run_id=other_run.run_id,
        turn=0,
        request_fingerprint="sha256:foreign",
        lease_generation=other_lease.generation,
        status=ExternalOperationStatus.STARTED,
        provider_identity="provider:model",
    )
    await progress_store.create_external_operation(
        foreign_operation,
        lease=other_lease,
        now=NOW,
    )
    request = _request(operation_id=foreign_operation.operation_id)
    batch, _, _ = _create_batch(run, lease, request)

    with pytest.raises(RecoveryStateConflictError):
        await progress_store.commit_run_progress(batch)

    assert await progress_store.latest_cursor() == 0
    assert await progress_store.get_reconciliation_request(request.request_id) is None


def _sdk_traceback_locals(error: BaseException) -> tuple[dict[str, Any], ...]:
    frames: list[dict[str, Any]] = []
    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in filename:
            frames.append(dict(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return tuple(frames)


def _assert_constant_secret_free_conflict(
    error: RecoveryStateConflictError,
    secret: str,
) -> None:
    assert error.to_dict() == {
        "code": "conflict",
        "message": "recovery state conflict",
        "retryable": True,
    }
    assert error.__cause__ is None
    assert error.__context__ is None
    sdk_locals = _sdk_traceback_locals(error)
    assert sdk_locals
    assert all(secret not in repr(frame) for frame in sdk_locals)


@pytest.mark.parametrize("invalidity", ("missing", "foreign_scope"))
@pytest.mark.asyncio
async def test_memory_exact_reconciliation_replay_validates_linked_operation(
    invalidity: str,
) -> None:
    store = InMemoryStore()
    run, lease = await _seed(store)
    secret = f"memory-linked-operation-secret-{invalidity}-3c1"
    operation = _operation(run, lease, operation_id="op_linked_secret")
    await store.create_external_operation(operation, lease=lease, now=NOW)
    request = _request(
        operation_id=operation.operation_id,
        details={"credential": secret},
    )
    batch, _, _ = _create_batch(run, lease, request)
    await store.commit_run_progress(batch)

    async with store._lock:
        if invalidity == "missing":
            del store._external_operations[operation.operation_id]
        else:
            operation_data = json.loads(
                store._external_operations[operation.operation_id]
            )
            operation_data.update(
                {"run_id": "run_foreign", "session_id": "ses_foreign"}
            )
            store._external_operations[operation.operation_id] = json.dumps(
                operation_data,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
    await store.release_lease(lease)

    with pytest.raises(RecoveryStateConflictError) as caught:
        await store.commit_run_progress(batch)

    _assert_constant_secret_free_conflict(caught.value, secret)
    assert await store.latest_cursor() == 1


@pytest.mark.parametrize(
    "invalidity",
    (
        "request_id",
        "session_id",
        "run_id",
        "status",
        "operation_id",
        "noncanonical_json",
    ),
)
@pytest.mark.asyncio
async def test_sqlite_exact_reconciliation_replay_validates_durable_wrapper(
    tmp_path: Path,
    invalidity: str,
) -> None:
    store = await SQLiteStore.open(tmp_path / f"wrapper-{invalidity}.db")
    try:
        run, lease = await _seed(store)
        secret = f"sqlite-wrapper-secret-{invalidity}-3c1"
        request = _request(details={"credential": secret})
        batch, _, _ = _create_batch(run, lease, request)
        await store.commit_run_progress(batch)

        async with store._lock:
            await store._connection.execute(
                "PRAGMA ignore_check_constraints = ON"
            )
            if invalidity == "noncanonical_json":
                async with store._connection.execute(
                    "SELECT data_json FROM reconciliation_requests "
                    "WHERE request_id = ?",
                    (request.request_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                assert row is not None
                await store._connection.execute(
                    "UPDATE reconciliation_requests SET data_json = ? "
                    "WHERE request_id = ?",
                    (json.dumps(json.loads(row[0]), indent=1), request.request_id),
                )
            else:
                replacement: str | None = {
                    "request_id": "rec_wrapper_foreign",
                    "session_id": "ses_wrapper_foreign",
                    "run_id": "run_wrapper_foreign",
                    "status": "resolved",
                    "operation_id": "op_wrapper_foreign",
                }[invalidity]
                if invalidity == "operation_id":
                    await store._connection.execute("PRAGMA foreign_keys = OFF")
                await store._connection.execute(
                    f"UPDATE reconciliation_requests SET {invalidity} = ? "
                    "WHERE request_id = ?",
                    (replacement, request.request_id),
                )
            await store._connection.commit()

        expired = batch._replace(now=lease.expires_at)
        with pytest.raises(RecoveryStateConflictError) as caught:
            await store.commit_run_progress(expired)

        _assert_constant_secret_free_conflict(caught.value, secret)
        assert await store.latest_cursor() == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_same_batch_operation_exact_replay_validates_exact_operation_target(
    progress_store: Any,
) -> None:
    run, lease = await _seed(progress_store)
    operation = _operation(run, lease)
    request = _request(operation_id=operation.operation_id)
    batch, _, _ = _create_batch(run, lease, request)
    batch = batch._replace(operation=ExternalOperationWrite(None, operation))
    await progress_store.commit_run_progress(batch)
    await progress_store.release_lease(lease)

    replay = await progress_store.commit_run_progress(batch)

    assert replay == storage_base.CommitResult(last_cursor=1, applied=False)


@pytest.mark.asyncio
async def test_lazy_exact_replay_wrapper_conflict_discards_request_secret(
    tmp_path: Path,
) -> None:
    store = _LazySQLiteStore(tmp_path / "lazy-wrapper-secret.db")
    try:
        run, lease = await _seed(store)
        secret = "lazy-wrapper-replay-secret-3c1-91e2"
        request = _request(details={"credential": secret})
        batch, _, _ = _create_batch(run, lease, request)
        await store.commit_run_progress(batch)
        underlying = await store._get()
        async with underlying._lock:
            await underlying._connection.execute(
                "PRAGMA ignore_check_constraints = ON"
            )
            await underlying._connection.execute(
                "UPDATE reconciliation_requests SET run_id = ? "
                "WHERE request_id = ?",
                ("run_wrapper_foreign", request.request_id),
            )
            await underlying._connection.commit()
        await store.release_lease(lease)

        with pytest.raises(RecoveryStateConflictError) as caught:
            await store.commit_run_progress(batch)

        _assert_constant_secret_free_conflict(caught.value, secret)
        assert await store.latest_cursor() == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_lazy_reconciliation_conflict_traceback_discards_request_secret(
    tmp_path: Path,
) -> None:
    store = _LazySQLiteStore(tmp_path / "lazy-reconciliation-secret.db")
    try:
        run, lease = await _seed(store)
        secret = "request-secret-3c1-7b52"
        request = _request(details={"credential": secret})
        batch, _, _ = _create_batch(run, lease, request)
        batch = batch._replace(now=lease.expires_at)

        with pytest.raises(RecoveryStateConflictError) as caught:
            await store.commit_run_progress(batch)

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        sdk_locals = _sdk_traceback_locals(caught.value)
        assert sdk_locals
        assert all(secret not in repr(frame) for frame in sdk_locals)
    finally:
        await store.close()


@pytest.mark.parametrize("event_failure", ("missing", "mismatched"))
@pytest.mark.asyncio
async def test_reconciliation_update_requires_exact_matching_resolution_event(
    progress_store: Any,
    event_failure: str,
) -> None:
    run, lease = await _seed(progress_store)
    request = _request()
    await progress_store.create_reconciliation_request(request)
    resolved = _resolved(request)
    events: tuple[EventEnvelope, ...] = ()
    if event_failure == "mismatched":
        events = (
            _resolution_event(resolved).model_copy(
                update={"payload": {"request_id": request.request_id}}
            ),
        )
    batch = RunProgressBatch(
        lease=lease,
        now=NOW,
        events=events,
        reconciliation=storage_base.ReconciliationRequestWrite(request, resolved),
    )

    with pytest.raises(RecoveryStateConflictError):
        await progress_store.commit_run_progress(batch)

    assert await progress_store.latest_cursor() == 0
    assert await progress_store.get_reconciliation_request(request.request_id) == request


@pytest.mark.parametrize("illegal_invocation", ("resolved_create", "oversized_event"))
@pytest.mark.asyncio
async def test_reconciliation_target_rejects_illegal_replay_shapes_and_int64(
    progress_store: Any,
    illegal_invocation: str,
) -> None:
    run, lease = await _seed(progress_store)
    request = _request()
    await progress_store.create_reconciliation_request(request)
    resolved = _resolved(request)
    event = _resolution_event(resolved)
    expected: ReconciliationRequest | None = request
    if illegal_invocation == "resolved_create":
        expected = None
    else:
        event = event.model_copy(update={"sequence": 1 << 63})
    batch = RunProgressBatch(
        lease=lease,
        now=NOW,
        events=(event,),
        reconciliation=storage_base.ReconciliationRequestWrite(expected, resolved),
    )

    with pytest.raises(RecoveryStateConflictError):
        await progress_store.commit_run_progress(batch)

    assert await progress_store.latest_cursor() == 0
    assert await progress_store.get_reconciliation_request(request.request_id) == request
