from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from agent_sdk.api import _LazySQLiteStore
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.leases import Lease
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot, TokenUsage
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
    RecoveryStateConflictError,
    RunCheckpoint,
    RunCheckpointPhase,
    ToolCallOperation,
)
from agent_sdk.storage import base as storage_base
from agent_sdk.storage.base import (
    CommitBatch,
    ExternalOperationWrite,
    EventPrecondition,
    RunCheckpointWrite,
    RunProgressBatch,
    SnapshotPrecondition,
    SnapshotWrite,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


NOW = datetime(2026, 7, 14, 8, tzinfo=UTC)
INT64_MAX = (1 << 63) - 1
INT64_MIN = -(1 << 63)
INT64_TOO_LARGE = INT64_MAX + 1
INT64_TOO_SMALL = INT64_MIN - 1


def _running_run(**updates: object) -> RunSnapshot:
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
        kind="run",
        entity_id=run.run_id,
        session_id=run.session_id,
        version=run.version,
        data=run.model_dump(mode="json"),
    )


def _model_operation(**updates: object) -> ModelCallOperation:
    values: dict[str, object] = {
        "operation_id": "op_model",
        "session_id": "ses_1",
        "run_id": "run_1",
        "turn": 0,
        "request_fingerprint": "sha256:model",
        "lease_generation": 1,
        "status": ExternalOperationStatus.STARTED,
        "provider_identity": "provider:model",
    }
    values.update(updates)
    return ModelCallOperation.model_validate(values)


def _checkpoint(**updates: object) -> RunCheckpoint:
    values: dict[str, object] = {
        "run_id": "run_1",
        "session_id": "ses_1",
        "checkpoint_version": 1,
        "turn": 0,
        "phase": RunCheckpointPhase.READY_FOR_MODEL,
        "messages": ({"role": "user", "content": "hello"},),
    }
    values.update(updates)
    return RunCheckpoint.model_validate(values)


def _event(
    event_id: str,
    sequence: int,
    *,
    run_id: str | None = "run_1",
    session_id: str = "ses_1",
    event_type: str = "run.progressed",
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        type=event_type,
        session_id=session_id,
        run_id=run_id,
        sequence=sequence,
        payload={"event_id": event_id},
        occurred_at=NOW,
    )


@pytest_asyncio.fixture(params=("memory", "sqlite"))
async def progress_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield InMemoryStore()
        return
    store = await SQLiteStore.open(tmp_path / "progress-contract.db")
    try:
        yield store
    finally:
        await store.close()


async def _seed_store(store: Any) -> tuple[Any, Lease, RunSnapshot]:
    run = _running_run()
    await store.commit(CommitBatch(events=(), snapshots=(_run_write(run),)))
    lease = await store.acquire_lease(
        run_id=run.run_id,
        owner="worker_1",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    return store, lease, run


@pytest.mark.asyncio
async def test_memory_commit_run_progress_atomically_starts_model() -> None:
    assert hasattr(storage_base, "ExternalOperationWrite")
    assert hasattr(storage_base, "RunCheckpointWrite")
    assert hasattr(storage_base, "RunProgressBatch")
    operation_write_type = storage_base.ExternalOperationWrite
    checkpoint_write_type = storage_base.RunCheckpointWrite
    batch_type = storage_base.RunProgressBatch

    store = InMemoryStore()
    run = RunSnapshot(
        run_id="run_1",
        session_id="ses_1",
        agent_revision="agent:1",
        status=RunStatus.RUNNING,
        user_input="hello",
        version=2,
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    kind="run",
                    entity_id=run.run_id,
                    session_id=run.session_id,
                    version=run.version,
                    data=run.model_dump(mode="json"),
                ),
            ),
        )
    )
    lease = await store.acquire_lease(
        run_id=run.run_id,
        owner="worker_1",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    operation = ModelCallOperation(
        operation_id="op_model",
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
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation.operation_id,
        messages=({"role": "user", "content": "hello"},),
    )
    event = EventEnvelope(
        event_id="evt_model_started",
        type="model.started",
        session_id=run.session_id,
        run_id=run.run_id,
        sequence=1,
        payload={"operation_id": operation.operation_id},
        occurred_at=NOW,
    )

    result = await store.commit_run_progress(
        batch_type(
            lease=lease,
            now=NOW,
            events=(event,),
            operation=operation_write_type(expected=None, updated=operation),
            checkpoint=checkpoint_write_type(expected=None, updated=checkpoint),
        )
    )

    assert result == storage_base.CommitResult(last_cursor=1, applied=True)
    assert [stored.event for stored in await store.read_events(after_cursor=0)] == [
        event
    ]
    assert await store.get_external_operation(operation.operation_id) == operation
    assert await store.get_run_checkpoint(run.run_id) == checkpoint


@pytest.mark.asyncio
async def test_sqlite_commit_run_progress_atomically_starts_model(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "run-progress.db")
    try:
        run = _running_run()
        await store.commit(CommitBatch(events=(), snapshots=(_run_write(run),)))
        lease = await store.acquire_lease(
            run_id=run.run_id,
            owner="worker_1",
            now=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )
        operation = _model_operation(lease_generation=lease.generation)
        checkpoint = _checkpoint(
            phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
            operation_id=operation.operation_id,
        )
        event = _event("evt_model_started", 1, event_type="model.started")

        result = await store.commit_run_progress(
            RunProgressBatch(
                lease=lease,
                now=NOW,
                events=(event,),
                operation=ExternalOperationWrite(None, operation),
                checkpoint=RunCheckpointWrite(None, checkpoint),
            )
        )

        assert result == storage_base.CommitResult(last_cursor=1, applied=True)
        assert [
            stored.event for stored in await store.read_events(after_cursor=0)
        ] == [event]
        assert await store.get_external_operation(operation.operation_id) == operation
        assert await store.get_run_checkpoint(run.run_id) == checkpoint
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_direct_operation_transition_cannot_refence_started_operation(
    progress_store: Any,
) -> None:
    store, lease, _ = await _seed_store(progress_store)
    started = _model_operation(lease_generation=lease.generation)
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=started.operation_id,
    )
    await store.commit_run_progress(
        RunProgressBatch(
            lease=lease,
            now=NOW,
            operation=ExternalOperationWrite(None, started),
            checkpoint=RunCheckpointWrite(None, checkpoint),
        )
    )
    current = await store.acquire_lease(
        run_id=started.run_id,
        owner="worker_2",
        now=lease.expires_at,
        expires_at=lease.expires_at + timedelta(seconds=30),
    )
    refenced = started.model_copy(
        update={"lease_generation": current.generation}
    )

    with pytest.raises(RecoveryStateConflictError):
        await store.transition_external_operation(
            expected=started,
            updated=refenced,
            lease=current,
            now=current.acquired_at,
        )

    assert await store.get_external_operation(started.operation_id) == started
    assert await store.get_run_checkpoint(started.run_id) == checkpoint


@pytest.mark.asyncio
async def test_composite_operation_refence_requires_exact_checkpoint_precondition(
    progress_store: Any,
) -> None:
    store, lease, _ = await _seed_store(progress_store)
    started = _model_operation(lease_generation=lease.generation)
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=started.operation_id,
    )
    await store.commit_run_progress(
        RunProgressBatch(
            lease=lease,
            now=NOW,
            operation=ExternalOperationWrite(None, started),
            checkpoint=RunCheckpointWrite(None, checkpoint),
        )
    )
    current = await store.acquire_lease(
        run_id=started.run_id,
        owner="worker_2",
        now=lease.expires_at,
        expires_at=lease.expires_at + timedelta(seconds=30),
    )
    refenced = started.model_copy(
        update={"lease_generation": current.generation}
    )
    audit = _event("evt_recovery_audit", 1, event_type="model.recovery.started")

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(
            RunProgressBatch(
                lease=current,
                now=current.acquired_at,
                events=(audit,),
                operation=ExternalOperationWrite(started, refenced),
            )
        )

    assert await store.latest_cursor() == 0
    assert await store.get_external_operation(started.operation_id) == started
    assert await store.get_run_checkpoint(started.run_id) == checkpoint


@pytest.mark.asyncio
async def test_composite_operation_refence_accepts_exact_checkpoint_precondition(
    progress_store: Any,
) -> None:
    store, lease, _ = await _seed_store(progress_store)
    started = _model_operation(lease_generation=lease.generation)
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=started.operation_id,
    )
    await store.commit_run_progress(
        RunProgressBatch(
            lease=lease,
            now=NOW,
            operation=ExternalOperationWrite(None, started),
            checkpoint=RunCheckpointWrite(None, checkpoint),
        )
    )
    current = await store.acquire_lease(
        run_id=started.run_id,
        owner="worker_2",
        now=lease.expires_at,
        expires_at=lease.expires_at + timedelta(seconds=30),
    )
    refenced = started.model_copy(
        update={"lease_generation": current.generation}
    )
    audit = _event("evt_recovery_audit", 1, event_type="model.recovery.started")

    result = await store.commit_run_progress(
        RunProgressBatch(
            lease=current,
            now=current.acquired_at,
            events=(audit,),
            operation=ExternalOperationWrite(started, refenced),
            checkpoint_precondition=checkpoint,
        )
    )

    assert result == storage_base.CommitResult(last_cursor=1, applied=True)
    assert await store.get_external_operation(started.operation_id) == refenced
    assert await store.get_run_checkpoint(started.run_id) == checkpoint


@pytest.mark.asyncio
async def test_commit_run_progress_atomically_records_model_outcome(
    progress_store: Any,
) -> None:
    store, lease, _ = await _seed_store(progress_store)
    started = _model_operation()
    in_flight = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=started.operation_id,
    )
    await store.commit_run_progress(
        RunProgressBatch(
            lease=lease,
            now=NOW,
            operation=ExternalOperationWrite(None, started),
            checkpoint=RunCheckpointWrite(None, in_flight),
        )
    )
    completed = started.model_copy(
        update={
            "status": ExternalOperationStatus.COMPLETED,
            "outcome": {"text": "ok"},
        }
    )
    safe = _checkpoint(
        checkpoint_version=2,
        phase=RunCheckpointPhase.READY_FOR_MODEL,
    )
    outcome = _event("evt_model_completed", 1, event_type="model.completed")

    outcome_batch = RunProgressBatch(
        lease=lease,
        now=NOW,
        events=(outcome,),
        operation=ExternalOperationWrite(started, completed),
        checkpoint=RunCheckpointWrite(in_flight, safe),
    )
    result = await store.commit_run_progress(outcome_batch)

    assert result == storage_base.CommitResult(last_cursor=1, applied=True)
    assert await store.get_external_operation(started.operation_id) == completed
    assert await store.get_run_checkpoint("run_1") == safe
    assert [stored.event for stored in await store.read_events(after_cursor=0)] == [
        outcome
    ]
    await store.release_lease(lease)
    replay = await store.commit_run_progress(
        outcome_batch._replace(now=lease.expires_at)
    )
    assert replay == storage_base.CommitResult(last_cursor=1, applied=False)


@pytest.mark.asyncio
async def test_commit_run_progress_fences_event_and_snapshot_only(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    progressed = run.model_copy(update={"version": 3})
    event = _event("evt_progress", 1)

    result = await store.commit_run_progress(
        RunProgressBatch(
            lease=lease,
            now=NOW,
            events=(event,),
            snapshots=(_run_write(progressed),),
        )
    )

    assert result == storage_base.CommitResult(last_cursor=1, applied=True)
    assert await store.get_snapshot("run", run.run_id) == progressed.model_dump(
        mode="json"
    )
    assert await store.get_external_operation("op_model") is None
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.asyncio
async def test_commit_run_progress_atomically_finishes_run_and_session(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    checkpoint = _checkpoint()
    await store.commit_run_progress(
        RunProgressBatch(
            lease=lease,
            now=NOW,
            checkpoint=RunCheckpointWrite(None, checkpoint),
        )
    )
    session = SessionSnapshot(
        session_id=run.session_id,
        workspaces=("workspace",),
        version=2,
    )
    completed = run.model_copy(
        update={
            "status": RunStatus.COMPLETED,
            "version": 3,
            "output_text": "ok",
            "usage": TokenUsage(),
        }
    )
    terminal = _checkpoint(
        checkpoint_version=2,
        phase=RunCheckpointPhase.TERMINAL,
    )
    run_event = _event("evt_run_completed", 1, event_type="run.completed")
    session_event = _event(
        "evt_session_completed",
        1,
        run_id=None,
        event_type="session.updated",
    )
    session_write = SnapshotWrite(
        "session",
        session.session_id,
        session.session_id,
        session.version,
        session.model_dump(mode="json"),
    )

    result = await store.commit_run_progress(
        RunProgressBatch(
            lease=lease,
            now=NOW,
            events=(run_event, session_event),
            snapshots=(_run_write(completed), session_write),
            checkpoint=RunCheckpointWrite(checkpoint, terminal),
        )
    )

    assert result == storage_base.CommitResult(last_cursor=2, applied=True)
    assert await store.get_snapshot("run", run.run_id) == completed.model_dump(
        mode="json"
    )
    assert await store.get_snapshot("session", session.session_id) == (
        session.model_dump(mode="json")
    )
    assert await store.get_run_checkpoint(run.run_id) == terminal
    assert [stored.event for stored in await store.read_events(after_cursor=0)] == [
        run_event,
        session_event,
    ]


def _complete_target_batch(
    lease: Lease, run: RunSnapshot
) -> tuple[RunProgressBatch, ModelCallOperation, RunCheckpoint, EventEnvelope]:
    progressed = run.model_copy(update={"version": 3})
    operation = _model_operation(lease_generation=lease.generation)
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation.operation_id,
    )
    event = _event("evt_model_started", 1, event_type="model.started")
    return (
        RunProgressBatch(
            lease=lease,
            now=NOW,
            events=(event,),
            snapshots=(_run_write(progressed),),
            operation=ExternalOperationWrite(None, operation),
            checkpoint=RunCheckpointWrite(None, checkpoint),
        ),
        operation,
        checkpoint,
        event,
    )


@pytest.mark.asyncio
async def test_commit_run_progress_exact_replay_after_release(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    batch, operation, checkpoint, event = _complete_target_batch(lease, run)
    first = await store.commit_run_progress(batch)
    await store.release_lease(lease)

    replay = await store.commit_run_progress(
        batch._replace(now=lease.expires_at + timedelta(seconds=30))
    )

    assert first == storage_base.CommitResult(last_cursor=1, applied=True)
    assert replay == storage_base.CommitResult(last_cursor=1, applied=False)
    assert [stored.event for stored in await store.read_events(after_cursor=0)] == [
        event
    ]
    assert await store.get_snapshot("run", run.run_id) == batch.snapshots[0].data
    assert await store.get_external_operation(operation.operation_id) == operation
    assert await store.get_run_checkpoint(run.run_id) == checkpoint


@pytest.mark.asyncio
async def test_commit_run_progress_rejects_illegal_exact_replay_shape(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    batch, operation, checkpoint, _ = _complete_target_batch(lease, run)
    await store.commit_run_progress(batch)
    illegal = batch._replace(
        operation=ExternalOperationWrite(operation, operation),
        checkpoint=RunCheckpointWrite(checkpoint, checkpoint),
    )

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(illegal)


@pytest.mark.asyncio
async def test_commit_run_progress_rejects_partial_target_replay(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    batch, operation, _, event = _complete_target_batch(lease, run)
    partial = batch._replace(snapshots=(), checkpoint=None)
    await store.commit(CommitBatch(events=(event,)))

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(partial)

    assert await store.get_external_operation(operation.operation_id) is None
    assert [stored.event for stored in await store.read_events(after_cursor=0)] == [
        event
    ]


@pytest.mark.parametrize(
    "lease_failure",
    ("owner", "generation", "expired", "released"),
)
@pytest.mark.asyncio
async def test_commit_run_progress_fence_failure_mutates_nothing(
    progress_store: Any,
    lease_failure: str,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    batch, operation, _, _ = _complete_target_batch(lease, run)
    if lease_failure == "owner":
        rejected = batch._replace(
            lease=lease.model_copy(update={"owner": "worker_other"})
        )
    elif lease_failure == "generation":
        rejected = batch._replace(
            lease=lease.model_copy(update={"generation": lease.generation + 1})
        )
    elif lease_failure == "expired":
        rejected = batch._replace(now=lease.expires_at)
    else:
        await store.release_lease(lease)
        rejected = batch

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(rejected)

    assert await store.latest_cursor() == 0
    assert await store.get_snapshot("run", run.run_id) == run.model_dump(mode="json")
    assert await store.get_external_operation(operation.operation_id) is None
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.parametrize(
    "failed_precondition",
    ("snapshot", "event"),
)
@pytest.mark.asyncio
async def test_run_progress_precondition_failure_rolls_back_recovery(
    progress_store: Any,
    failed_precondition: str,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    evidence = _event("evt_evidence", 1)
    await store.commit(CommitBatch(events=(evidence,)))
    operation = _model_operation()
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation.operation_id,
    )
    snapshot_preconditions: tuple[SnapshotPrecondition, ...] = ()
    event_preconditions: tuple[EventPrecondition, ...] = ()
    if failed_precondition == "snapshot":
        snapshot_preconditions = (
            SnapshotPrecondition("run", run.run_id, version=999),
        )
    else:
        event_preconditions = (
            EventPrecondition(
                evidence.event_id,
                1,
                evidence.session_id,
                evidence.run_id,
                "wrong.type",
                evidence.sequence,
            ),
        )

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(
            RunProgressBatch(
                lease=lease,
                now=NOW,
                events=(_event("evt_target", 2),),
                preconditions=snapshot_preconditions,
                event_preconditions=event_preconditions,
                operation=ExternalOperationWrite(None, operation),
                checkpoint=RunCheckpointWrite(None, checkpoint),
            )
        )

    assert await store.latest_cursor() == 1
    assert await store.get_external_operation(operation.operation_id) is None
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.asyncio
async def test_commit_run_progress_rejects_empty_batch(
    progress_store: Any,
) -> None:
    store, lease, _ = await _seed_store(progress_store)

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(RunProgressBatch(lease=lease, now=NOW))


@pytest.mark.parametrize(
    "mismatch",
    (
        "event_run",
        "event_session",
        "snapshot_run",
        "snapshot_session",
        "snapshot_data",
        "operation_run",
        "operation_session",
        "operation_generation",
        "checkpoint_run",
        "checkpoint_session",
        "checkpoint_version",
        "checkpoint_kind",
    ),
)
@pytest.mark.asyncio
async def test_run_progress_rejects_cross_record_mismatch(
    progress_store: Any,
    mismatch: str,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    event = _event("evt_target", 1)
    snapshot = _run_write(run.model_copy(update={"version": 3}))
    operation: ModelCallOperation | ToolCallOperation = _model_operation()
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation.operation_id,
    )
    if mismatch == "event_run":
        event = _event("evt_target", 1, run_id="run_other")
    elif mismatch == "event_session":
        event = _event("evt_target", 1, session_id="ses_other")
    elif mismatch == "snapshot_run":
        snapshot = snapshot._replace(entity_id="run_other")
    elif mismatch == "snapshot_session":
        snapshot = snapshot._replace(session_id="ses_other")
    elif mismatch == "snapshot_data":
        snapshot = snapshot._replace(
            data={**snapshot.data, "run_id": "run_other"}
        )
    elif mismatch == "operation_run":
        operation = _model_operation(run_id="run_other")
    elif mismatch == "operation_session":
        operation = _model_operation(session_id="ses_other")
    elif mismatch == "operation_generation":
        operation = _model_operation(lease_generation=lease.generation + 1)
    elif mismatch == "checkpoint_run":
        checkpoint = _checkpoint(
            run_id="run_other",
            phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
            operation_id=operation.operation_id,
        )
    elif mismatch == "checkpoint_session":
        checkpoint = _checkpoint(
            session_id="ses_other",
            phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
            operation_id=operation.operation_id,
        )
    elif mismatch == "checkpoint_version":
        checkpoint = _checkpoint(
            checkpoint_version=2,
            phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
            operation_id=operation.operation_id,
        )
    else:
        operation = ToolCallOperation(
            operation_id="op_tool",
            session_id="ses_1",
            run_id="run_1",
            turn=0,
            request_fingerprint="sha256:tool",
            lease_generation=lease.generation,
            status=ExternalOperationStatus.STARTED,
            tool_identity="tool:search",
        )
        checkpoint = _checkpoint(
            phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
            operation_id=operation.operation_id,
        )

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(
            RunProgressBatch(
                lease=lease,
                now=NOW,
                events=(event,),
                snapshots=(snapshot,),
                operation=ExternalOperationWrite(None, operation),
                checkpoint=RunCheckpointWrite(None, checkpoint),
            )
        )

    assert await store.latest_cursor() == 0
    assert await store.get_snapshot("run", run.run_id) == run.model_dump(mode="json")
    assert await store.get_external_operation(operation.operation_id) is None
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.parametrize(
    "failure", ("event_id", "sequence", "snapshot_version")
)
@pytest.mark.asyncio
async def test_progress_target_failure_rolls_back_recovery(
    progress_store: Any,
    failure: str,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    await store.commit(CommitBatch(events=(_event("evt_existing", 1),)))
    operation = _model_operation()
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation.operation_id,
    )
    target_event = _event(
        "evt_existing" if failure == "event_id" else "evt_target",
        1 if failure == "sequence" else 2,
    )
    target_snapshot = _run_write(run.model_copy(update={"version": 3}))
    if failure == "snapshot_version":
        target_snapshot = target_snapshot._replace(version=1)

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(
            RunProgressBatch(
                lease=lease,
                now=NOW,
                events=(target_event,),
                snapshots=(target_snapshot,),
                operation=ExternalOperationWrite(None, operation),
                checkpoint=RunCheckpointWrite(None, checkpoint),
            )
        )

    assert await store.latest_cursor() == 1
    assert await store.get_external_operation(operation.operation_id) is None
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.asyncio
async def test_run_progress_accepts_exact_event_and_snapshot_preconditions(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    evidence = _event("evt_evidence", 1)
    await store.commit(CommitBatch(events=(evidence,)))
    progressed = run.model_copy(update={"version": 3})

    result = await store.commit_run_progress(
        RunProgressBatch(
            lease=lease,
            now=NOW,
            events=(_event("evt_target", 2),),
            snapshots=(_run_write(progressed),),
            preconditions=(
                SnapshotPrecondition(
                    "run",
                    run.run_id,
                    version=run.version,
                    session_id=run.session_id,
                    data=run.model_dump(mode="json"),
                ),
            ),
            event_preconditions=(
                EventPrecondition(
                    evidence.event_id,
                    1,
                    evidence.session_id,
                    evidence.run_id,
                    evidence.type,
                    evidence.sequence,
                ),
            ),
        )
    )

    assert result == storage_base.CommitResult(last_cursor=2, applied=True)


@pytest.mark.asyncio
async def test_run_progress_rejects_naive_batch_time(
    progress_store: Any,
) -> None:
    store, lease, _ = await _seed_store(progress_store)

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(
            RunProgressBatch(
                lease=lease,
                now=NOW.replace(tzinfo=None),
                events=(_event("evt_target", 1),),
            )
        )


@pytest.mark.asyncio
async def test_event_snapshot_only_exact_replay_after_expiry(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    batch = RunProgressBatch(
        lease=lease,
        now=NOW,
        events=(_event("evt_target", 1),),
        snapshots=(_run_write(run.model_copy(update={"version": 3})),),
    )
    await store.commit_run_progress(batch)

    replay = await store.commit_run_progress(
        batch._replace(now=lease.expires_at)
    )

    assert replay == storage_base.CommitResult(last_cursor=1, applied=False)
    assert len(await store.read_events(after_cursor=0)) == 1


@pytest.mark.asyncio
async def test_batch_created_recovery_is_deleted_with_session(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    batch, operation, _, _ = _complete_target_batch(lease, run)
    await store.commit_run_progress(batch)

    await store.delete_session(run.session_id)

    assert await store.get_external_operation(operation.operation_id) is None
    assert await store.get_run_checkpoint(run.run_id) is None
    assert await store.read_events(after_cursor=0) == []


@pytest.mark.asyncio
async def test_identical_commit_race_returns_durable_exact_outcome(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    batch, operation, checkpoint, event = _complete_target_batch(lease, run)

    first, second = await asyncio.gather(
        store.commit_run_progress(batch),
        store.commit_run_progress(batch),
    )

    assert {first.applied, second.applied} == {True, False}
    assert [stored.event for stored in await store.read_events(after_cursor=0)] == [
        event
    ]
    assert await store.get_external_operation(operation.operation_id) == operation
    assert await store.get_run_checkpoint(run.run_id) == checkpoint


def _sdk_traceback_locals(error: BaseException) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    traceback = error.__traceback__
    while traceback is not None:
        if "agent_sdk" in traceback.tb_frame.f_code.co_filename:
            frames.append(dict(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return frames


@pytest.mark.asyncio
async def test_run_progress_conflict_traceback_is_sanitized(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    secret = "progress-secret-749df1"
    operation = _model_operation(recovery_metadata={"token": secret})
    batch = RunProgressBatch(
        lease=lease,
        now=lease.expires_at,
        operation=ExternalOperationWrite(None, operation),
    )

    with pytest.raises(RecoveryStateConflictError) as caught:
        await store.commit_run_progress(batch)

    assert caught.value.to_dict() == {
        "code": "conflict",
        "message": "recovery state conflict",
        "retryable": True,
    }
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    sdk_locals = _sdk_traceback_locals(caught.value)
    assert sdk_locals
    assert all(secret not in repr(frame_locals) for frame_locals in sdk_locals)


@pytest.mark.asyncio
async def test_lazy_sqlite_forwards_exact_run_progress_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = Lease(
        run_id="run_1",
        owner="worker_1",
        generation=1,
        acquired_at=NOW,
        renewed_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    batch = RunProgressBatch(
        lease=lease,
        now=NOW,
        events=(_event("evt_target", 1),),
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
    lazy = _LazySQLiteStore(tmp_path / "lazy-forward.db")

    async def get_recorder() -> Any:
        return recorder

    monkeypatch.setattr(lazy, "_get", get_recorder)

    result = await lazy.commit_run_progress(batch)

    assert result is expected
    assert recorder.received is batch


@pytest.mark.asyncio
async def test_memory_run_progress_cancellation_before_publish_mutates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, lease, run = await _seed_store(InMemoryStore())
    batch, operation, _, _ = _complete_target_batch(lease, run)

    def cancel_before_publish(_: list[storage_base.StoredEvent]) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(store, "_latest_sequences", cancel_before_publish)

    with pytest.raises(asyncio.CancelledError):
        await store.commit_run_progress(batch)

    assert await store.latest_cursor() == 0
    assert await store.get_external_operation(operation.operation_id) is None
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.asyncio
async def test_sqlite_run_progress_fault_before_commit_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "progress-fault.db")
    try:
        _, lease, run = await _seed_store(store)
        batch, operation, _, _ = _complete_target_batch(lease, run)

        async def fail_before_commit() -> None:
            raise RuntimeError("injected progress commit fault")

        with monkeypatch.context() as fault:
            fault.setattr(store, "_commit_transaction", fail_before_commit)
            with pytest.raises(RuntimeError, match="injected progress commit fault"):
                await store.commit_run_progress(batch)

        assert await store.latest_cursor() == 0
        assert await store.get_external_operation(operation.operation_id) is None
        assert await store.get_run_checkpoint(run.run_id) is None
        assert await store.get_snapshot("run", run.run_id) == run.model_dump(
            mode="json"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_run_progress_cancellation_before_commit_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "progress-cancel.db")
    try:
        _, lease, run = await _seed_store(store)
        batch, operation, _, _ = _complete_target_batch(lease, run)
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause_before_commit() -> None:
            reached.set()
            await release.wait()

        monkeypatch.setattr(store, "_commit_transaction", pause_before_commit)
        task = asyncio.create_task(store.commit_run_progress(batch))
        await asyncio.wait_for(reached.wait(), timeout=2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        assert await store.latest_cursor() == 0
        assert await store.get_external_operation(operation.operation_id) is None
        assert await store.get_run_checkpoint(run.run_id) is None
        assert await store.get_snapshot("run", run.run_id) == run.model_dump(
            mode="json"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_run_progress_cancel_racing_commit_replays_durable_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await SQLiteStore.open(tmp_path / "progress-commit-race.db")
    try:
        _, lease, run = await _seed_store(store)
        batch, operation, checkpoint, event = _complete_target_batch(lease, run)
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
        assert [
            stored.event for stored in await store.read_events(after_cursor=0)
        ] == [event]
        assert await store.get_external_operation(operation.operation_id) == operation
        assert await store.get_run_checkpoint(run.run_id) == checkpoint
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_lazy_run_progress_conflict_traceback_is_sanitized(
    tmp_path: Path,
) -> None:
    store = _LazySQLiteStore(tmp_path / "lazy-conflict.db")
    try:
        _, lease, _ = await _seed_store(store)
        secret = "lazy-progress-secret-96c0e4"
        batch = RunProgressBatch(
            lease=lease,
            now=lease.expires_at,
            operation=ExternalOperationWrite(
                None,
                _model_operation(recovery_metadata={"token": secret}),
            ),
        )

        with pytest.raises(RecoveryStateConflictError) as caught:
            await store.commit_run_progress(batch)

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        sdk_locals = _sdk_traceback_locals(caught.value)
        assert sdk_locals
        assert all(
            secret not in repr(frame_locals) for frame_locals in sdk_locals
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_exact_operation_replay_still_requires_batch_run_identity(
    progress_store: Any,
) -> None:
    store, lease, _ = await _seed_store(progress_store)
    other_run = _running_run(run_id="run_other", session_id="ses_other")
    await store.commit(
        CommitBatch(events=(), snapshots=(_run_write(other_run),))
    )
    other_lease = await store.acquire_lease(
        run_id=other_run.run_id,
        owner="worker_other",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    operation = _model_operation(
        run_id=other_run.run_id,
        session_id=other_run.session_id,
        lease_generation=other_lease.generation,
    )
    await store.create_external_operation(
        operation, lease=other_lease, now=NOW
    )

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(
            RunProgressBatch(
                lease=lease,
                now=NOW,
                operation=ExternalOperationWrite(None, operation),
            )
        )


@pytest.mark.parametrize("invalid_target", ("event", "snapshot"))
@pytest.mark.asyncio
async def test_run_progress_rejects_non_json_target_without_partial_mutation(
    progress_store: Any,
    invalid_target: str,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    operation = _model_operation()
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation.operation_id,
    )
    event = _event("evt_target", 1)
    snapshot = SnapshotWrite(
        "custom",
        "custom_1",
        run.session_id,
        1,
        {"value": "ok"},
    )
    if invalid_target == "event":
        event = event.model_copy(update={"payload": {"value": float("nan")}})
    else:
        snapshot = snapshot._replace(data={"value": float("nan")})

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(
            RunProgressBatch(
                lease=lease,
                now=NOW,
                events=(event,),
                snapshots=(snapshot,),
                operation=ExternalOperationWrite(None, operation),
                checkpoint=RunCheckpointWrite(None, checkpoint),
            )
        )

    assert await store.latest_cursor() == 0
    assert await store.get_snapshot(snapshot.kind, snapshot.entity_id) is None
    assert await store.get_external_operation(operation.operation_id) is None
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.asyncio
async def test_exact_replay_rejects_duplicate_event_target_in_invocation(
    progress_store: Any,
) -> None:
    store, lease, _ = await _seed_store(progress_store)
    event = _event("evt_target", 1)
    await store.commit_run_progress(
        RunProgressBatch(lease=lease, now=NOW, events=(event,))
    )
    await store.release_lease(lease)

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(
            RunProgressBatch(
                lease=lease,
                now=lease.expires_at,
                events=(event, event),
            )
        )


@pytest.mark.parametrize("lease_state", ("active", "released"))
@pytest.mark.asyncio
async def test_run_progress_rejects_multiple_snapshots_for_same_identity(
    progress_store: Any,
    lease_state: str,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    version_3 = _run_write(run.model_copy(update={"version": 3}))
    version_4 = _run_write(run.model_copy(update={"version": 4}))
    if lease_state == "released":
        await store.release_lease(lease)

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(
            RunProgressBatch(
                lease=lease,
                now=NOW,
                snapshots=(version_3, version_4),
            )
        )

    assert await store.latest_cursor() == 0
    assert await store.get_snapshot("run", run.run_id) == run.model_dump(
        mode="json"
    )
    assert await store.get_external_operation("op_model") is None
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.asyncio
async def test_run_progress_never_replays_multiple_snapshot_target_shape(
    progress_store: Any,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    version_3 = _run_write(run.model_copy(update={"version": 3}))
    version_4 = _run_write(run.model_copy(update={"version": 4}))
    await store.commit(
        CommitBatch(events=(), snapshots=(version_3, version_4))
    )
    await store.release_lease(lease)

    with pytest.raises(RecoveryStateConflictError):
        await store.commit_run_progress(
            RunProgressBatch(
                lease=lease,
                now=lease.expires_at,
                snapshots=(version_3, version_4),
            )
        )

    assert await store.get_snapshot("run", run.run_id) == version_4.data


_INT64_FIELD_CASES = (
    "event_schema_version",
    "event_sequence_high",
    "event_sequence_low",
    "snapshot_version_high",
    "snapshot_version_low",
    "snapshot_precondition_version",
    "event_precondition_cursor",
    "event_precondition_sequence",
    "operation_turn",
    "operation_lease_generation",
    "checkpoint_version",
    "checkpoint_turn",
    "lease_generation",
)


async def _batch_with_oversized_integer(
    store: Any,
    lease: Lease,
    run: RunSnapshot,
    field: str,
    secret: str,
) -> RunProgressBatch:
    replay_fields = {
        "snapshot_precondition_version",
        "event_precondition_cursor",
        "event_precondition_sequence",
        "lease_generation",
    }
    if field in replay_fields:
        event = _event("evt_exact_numeric_target", 1).model_copy(
            update={"payload": {"secret": secret}}
        )
        await store.commit_run_progress(
            RunProgressBatch(lease=lease, now=NOW, events=(event,))
        )
        await store.release_lease(lease)
        batch_lease = lease
        preconditions: tuple[SnapshotPrecondition, ...] = ()
        event_preconditions: tuple[EventPrecondition, ...] = ()
        if field == "snapshot_precondition_version":
            preconditions = (
                SnapshotPrecondition(
                    "run", run.run_id, version=INT64_TOO_LARGE
                ),
            )
        elif field == "event_precondition_cursor":
            event_preconditions = (
                EventPrecondition(
                    event.event_id,
                    INT64_TOO_LARGE,
                    event.session_id,
                    event.run_id,
                    event.type,
                    event.sequence,
                ),
            )
        elif field == "event_precondition_sequence":
            event_preconditions = (
                EventPrecondition(
                    event.event_id,
                    1,
                    event.session_id,
                    event.run_id,
                    event.type,
                    INT64_TOO_LARGE,
                ),
            )
        else:
            batch_lease = lease.model_copy(
                update={"generation": INT64_TOO_LARGE}
            )
        return RunProgressBatch(
            lease=batch_lease,
            now=lease.expires_at,
            events=(event,),
            preconditions=preconditions,
            event_preconditions=event_preconditions,
        )

    event = _event("evt_oversized_numeric", 1).model_copy(
        update={"payload": {"secret": secret}}
    )
    snapshot = _run_write(run.model_copy(update={"version": 3}))
    operation = _model_operation(recovery_metadata={"secret": secret})
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation.operation_id,
    )
    if field == "event_schema_version":
        event = event.model_copy(update={"schema_version": INT64_TOO_LARGE})
    elif field == "event_sequence_high":
        event = event.model_copy(update={"sequence": INT64_TOO_LARGE})
    elif field == "event_sequence_low":
        event = event.model_copy(update={"sequence": INT64_TOO_SMALL})
    elif field == "snapshot_version_high":
        oversized_run = run.model_copy(update={"version": INT64_TOO_LARGE})
        snapshot = _run_write(oversized_run)
    elif field == "snapshot_version_low":
        snapshot = SnapshotWrite(
            "custom",
            "custom_oversized",
            run.session_id,
            INT64_TOO_SMALL,
            {"secret": secret},
        )
    elif field == "operation_turn":
        operation = _model_operation(
            turn=INT64_TOO_LARGE,
            recovery_metadata={"secret": secret},
        )
        checkpoint = checkpoint.model_copy(
            update={"operation_id": operation.operation_id}
        )
    elif field == "operation_lease_generation":
        operation = _model_operation(
            lease_generation=INT64_TOO_LARGE,
            recovery_metadata={"secret": secret},
        )
        checkpoint = checkpoint.model_copy(
            update={"operation_id": operation.operation_id}
        )
    elif field == "checkpoint_version":
        checkpoint = checkpoint.model_copy(
            update={"checkpoint_version": INT64_TOO_LARGE}
        )
    elif field == "checkpoint_turn":
        checkpoint = checkpoint.model_copy(update={"turn": INT64_TOO_LARGE})
    else:
        raise AssertionError(f"unhandled int64 field: {field}")
    return RunProgressBatch(
        lease=lease,
        now=NOW,
        events=(event,),
        snapshots=(snapshot,),
        operation=ExternalOperationWrite(None, operation),
        checkpoint=RunCheckpointWrite(None, checkpoint),
    )


@pytest.mark.parametrize("numeric_field", _INT64_FIELD_CASES)
@pytest.mark.asyncio
async def test_run_progress_rejects_out_of_int64_numeric_fields_safely(
    progress_store: Any,
    numeric_field: str,
) -> None:
    store, lease, run = await _seed_store(progress_store)
    secret = f"int64-secret-{numeric_field}-80f75d"
    batch = await _batch_with_oversized_integer(
        store, lease, run, numeric_field, secret
    )
    before_cursor = await store.latest_cursor()
    before_events = await store.read_events(after_cursor=0)
    before_run = await store.get_snapshot("run", run.run_id)

    with pytest.raises(RecoveryStateConflictError) as caught:
        await store.commit_run_progress(batch)

    assert caught.value.to_dict() == {
        "code": "conflict",
        "message": "recovery state conflict",
        "retryable": True,
    }
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    sdk_locals = _sdk_traceback_locals(caught.value)
    assert sdk_locals
    assert all(secret not in repr(frame_locals) for frame_locals in sdk_locals)
    assert await store.latest_cursor() == before_cursor
    assert await store.read_events(after_cursor=0) == before_events
    assert await store.get_snapshot("run", run.run_id) == before_run
    assert await store.get_external_operation("op_model") is None
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.asyncio
async def test_lazy_run_progress_rejects_out_of_int64_with_sanitized_traceback(
    tmp_path: Path,
) -> None:
    store = _LazySQLiteStore(tmp_path / "lazy-int64.db")
    try:
        _, lease, run = await _seed_store(store)
        secret = "lazy-int64-secret-6f12d8"
        batch = await _batch_with_oversized_integer(
            store, lease, run, "event_sequence_high", secret
        )

        with pytest.raises(RecoveryStateConflictError) as caught:
            await store.commit_run_progress(batch)

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        sdk_locals = _sdk_traceback_locals(caught.value)
        assert sdk_locals
        assert all(
            secret not in repr(frame_locals) for frame_locals in sdk_locals
        )
        assert await store.latest_cursor() == 0
        assert await store.get_external_operation("op_model") is None
        assert await store.get_run_checkpoint(run.run_id) is None
    finally:
        await store.close()
