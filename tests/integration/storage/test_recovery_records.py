from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from agent_sdk.api import _LazySQLiteStore
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
    ReconciliationAction,
    ReconciliationRequest,
    ReconciliationResolution,
    ReconciliationStatus,
    RecoveryStateConflictError,
    RunCheckpoint,
    RunCheckpointPhase,
    ToolCallOperation,
)
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


NOW = datetime(2026, 7, 14, 8, tzinfo=UTC)


def _model_operation(**updates: Any) -> ModelCallOperation:
    values: dict[str, Any] = {
        "operation_id": "op_model",
        "session_id": "ses_1",
        "run_id": "run_1",
        "turn": 0,
        "request_fingerprint": "sha256:model",
        "lease_generation": 1,
        "status": ExternalOperationStatus.STARTED,
        "provider_identity": "provider:model",
        "recovery_metadata": {"query": {"supported": True}},
    }
    values.update(updates)
    return ModelCallOperation(**values)


def _tool_operation(**updates: Any) -> ToolCallOperation:
    values: dict[str, Any] = {
        "operation_id": "op_tool",
        "session_id": "ses_1",
        "run_id": "run_1",
        "turn": 1,
        "request_fingerprint": "sha256:tool",
        "lease_generation": 1,
        "status": ExternalOperationStatus.STARTED,
        "tool_identity": "tool:search",
    }
    values.update(updates)
    return ToolCallOperation(**values)


def _checkpoint(**updates: Any) -> RunCheckpoint:
    values: dict[str, Any] = {
        "run_id": "run_1",
        "session_id": "ses_1",
        "checkpoint_version": 1,
        "turn": 0,
        "phase": RunCheckpointPhase.READY_FOR_MODEL,
        "messages": ({"role": "user", "content": "hello"},),
    }
    values.update(updates)
    return RunCheckpoint(**values)


def _request(**updates: Any) -> ReconciliationRequest:
    values: dict[str, Any] = {
        "request_id": "rec_1",
        "session_id": "ses_1",
        "run_id": "run_1",
        "reason": "operation outcome is unknown",
        "details": {"source": "provider"},
    }
    values.update(updates)
    return ReconciliationRequest(**values)


def _resolved_request(
    request: ReconciliationRequest,
    *,
    event_id: str = "evt_resolution",
    action: ReconciliationAction = ReconciliationAction.TERMINATE,
) -> ReconciliationRequest:
    return request.model_copy(
        update={
            "status": ReconciliationStatus.RESOLVED,
            "resolution": ReconciliationResolution(
                action=action,
                actor={"type": "user", "id": "operator"},
                evidence={"reason": "provider result unavailable"},
                decided_at=NOW,
                event_id=event_id,
            ),
        }
    )


def _resolution_event(
    resolved: ReconciliationRequest,
    *,
    sequence: int = 1,
) -> EventEnvelope:
    assert resolved.resolution is not None
    return EventEnvelope(
        event_id=resolved.resolution.event_id,
        type="reconciliation.resolved",
        session_id=resolved.session_id,
        run_id=resolved.run_id,
        sequence=sequence,
        payload={
            "request_id": resolved.request_id,
            "operation_id": resolved.operation_id,
            "action": resolved.resolution.action.value,
            "actor": {"type": "user", "id": "operator"},
            "evidence": {"reason": "provider result unavailable"},
        },
        occurred_at=resolved.resolution.decided_at,
    )


async def _lease(store: Any, run_id: str = "run_1") -> Any:
    return await store.acquire_lease(
        run_id=run_id,
        owner="worker_1",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )


async def _put_authoritative_run_snapshot(
    store: Any,
    *,
    run_id: str = "run_1",
    session_id: str = "ses_1",
) -> None:
    run = RunSnapshot(
        run_id=run_id,
        session_id=session_id,
        agent_revision="agent:1",
        status=RunStatus.CREATED,
        user_input="hello",
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    kind="run",
                    entity_id=run_id,
                    session_id=session_id,
                    version=run.version,
                    data=run.model_dump(mode="json"),
                ),
            ),
        )
    )


async def _put_authoritative_run_history(store: Any) -> None:
    session = SessionSnapshot(session_id="ses_1", workspaces=("workspace",))
    run = RunSnapshot(
        run_id="run_1",
        session_id=session.session_id,
        agent_revision="agent:1",
        status=RunStatus.CREATED,
        user_input="hello",
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope(
                    event_id="evt_session_created",
                    type="session.created",
                    session_id=session.session_id,
                    run_id=None,
                    sequence=1,
                    payload=session.model_dump(mode="json"),
                    occurred_at=NOW,
                ),
                EventEnvelope(
                    event_id="evt_run_created",
                    type="run.created",
                    session_id=session.session_id,
                    run_id=run.run_id,
                    sequence=1,
                    payload=run.model_dump(mode="json"),
                    occurred_at=NOW,
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


@pytest_asyncio.fixture(params=("memory", "sqlite", "lazy_sqlite"))
async def recovery_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield InMemoryStore()
        return
    store: Any
    if request.param == "sqlite":
        store = await SQLiteStore.open(tmp_path / "recovery.db")
    else:
        store = _LazySQLiteStore(tmp_path / "lazy-recovery-fixture.db")
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def seeded_recovery_store(recovery_store: Any) -> Any:
    await _put_authoritative_run_snapshot(recovery_store)
    await _put_authoritative_run_snapshot(recovery_store, run_id="run_initial")
    return recovery_store


async def _create_first_recovery_record(
    store: Any, record_kind: str, *, session_id: str
) -> Any:
    if record_kind == "operation":
        lease = await _lease(store)
        operation = _model_operation(session_id=session_id)
        return await store.create_external_operation(
            operation, lease=lease, now=NOW
        )
    if record_kind == "checkpoint":
        lease = await _lease(store)
        checkpoint = _checkpoint(session_id=session_id)
        return await store.put_run_checkpoint(
            checkpoint, expected=None, lease=lease, now=NOW
        )
    request = _request(session_id=session_id)
    assert request.operation_id is None
    return await store.create_reconciliation_request(request)


@pytest.mark.parametrize("record_kind", ("operation", "checkpoint", "request"))
@pytest.mark.asyncio
async def test_first_recovery_record_rejects_missing_authoritative_run_snapshot(
    recovery_store: Any,
    record_kind: str,
) -> None:
    with pytest.raises(RecoveryStateConflictError):
        await _create_first_recovery_record(
            recovery_store, record_kind, session_id="ses_1"
        )


@pytest.mark.parametrize("record_kind", ("operation", "checkpoint", "request"))
@pytest.mark.asyncio
async def test_first_recovery_record_rejects_wrong_authoritative_session(
    recovery_store: Any,
    record_kind: str,
) -> None:
    await _put_authoritative_run_snapshot(recovery_store)

    with pytest.raises(RecoveryStateConflictError):
        await _create_first_recovery_record(
            recovery_store, record_kind, session_id="ses_other"
        )


@pytest.mark.parametrize("record_kind", ("operation", "checkpoint", "request"))
@pytest.mark.asyncio
async def test_first_recovery_record_accepts_matching_authoritative_run_snapshot(
    recovery_store: Any,
    record_kind: str,
) -> None:
    await _put_authoritative_run_snapshot(recovery_store)

    created = await _create_first_recovery_record(
        recovery_store, record_kind, session_id="ses_1"
    )

    if record_kind == "operation":
        assert await recovery_store.get_external_operation(created.operation_id) == created
    elif record_kind == "checkpoint":
        assert await recovery_store.get_run_checkpoint(created.run_id) == created
    else:
        assert await recovery_store.get_reconciliation_request(created.request_id) == created


@pytest.mark.asyncio
async def test_first_recovery_record_rejects_malformed_run_snapshot(
    recovery_store: Any,
) -> None:
    await recovery_store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    kind="run",
                    entity_id="run_1",
                    session_id="ses_1",
                    version=1,
                    data={"run_id": "run_1", "session_id": "ses_1"},
                ),
            ),
        )
    )

    with pytest.raises(RecoveryStateConflictError):
        await recovery_store.create_reconciliation_request(_request())


@pytest.mark.asyncio
async def test_external_operation_create_replay_get_and_ordered_list(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    lease = await _lease(store)
    operations = (
        _tool_operation(operation_id="op_z", turn=2),
        _tool_operation(operation_id="op_b", turn=1),
        _model_operation(operation_id="op_c", turn=1),
        _model_operation(operation_id="op_a", turn=1),
    )

    for operation in operations:
        created = await store.create_external_operation(
            operation, lease=lease, now=NOW
        )
        assert created == operation
        assert created is not operation

    replay = await store.create_external_operation(
        operations[0], lease=lease, now=NOW
    )
    assert replay == operations[0]
    assert replay is not operations[0]
    assert await store.get_external_operation("missing") is None
    fetched = await store.get_external_operation("op_a")
    assert fetched == operations[3]
    assert fetched is not operations[3]
    assert tuple(
        operation.operation_id
        for operation in await store.list_unresolved_external_operations("run_1")
    ) == ("op_a", "op_c", "op_b", "op_z")

    with pytest.raises(RecoveryStateConflictError):
        await store.create_external_operation(
            operations[0].model_copy(update={"request_fingerprint": "different"}),
            lease=lease,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_external_operation_terminal_cas_replay_and_fencing(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    lease = await _lease(store)
    started = _model_operation()
    await store.create_external_operation(started, lease=lease, now=NOW)
    completed = started.model_copy(
        update={"status": ExternalOperationStatus.COMPLETED, "outcome": {"text": "ok"}}
    )

    transitioned = await store.transition_external_operation(
        expected=started,
        updated=completed,
        lease=lease,
        now=NOW,
    )
    assert transitioned == completed
    assert transitioned is not completed
    assert await store.list_unresolved_external_operations("run_1") == ()
    assert await store.transition_external_operation(
        expected=started,
        updated=completed,
        lease=lease,
        now=NOW,
    ) == completed

    with pytest.raises(RecoveryStateConflictError):
        await store.transition_external_operation(
            expected=started,
            updated=completed.model_copy(update={"outcome": {"text": "different"}}),
            lease=lease,
            now=NOW,
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.transition_external_operation(
            expected=completed,
            updated=completed,
            lease=lease,
            now=NOW,
        )

    second = _tool_operation(operation_id="op_stale", turn=2)
    await store.create_external_operation(second, lease=lease, now=NOW)
    current = await store.acquire_lease(
        run_id="run_1",
        owner="worker_2",
        now=lease.expires_at,
        expires_at=lease.expires_at + timedelta(seconds=30),
    )
    failed = second.model_copy(
        update={"status": ExternalOperationStatus.FAILED, "outcome": {}}
    )
    with pytest.raises(RecoveryStateConflictError):
        await store.transition_external_operation(
            expected=second,
            updated=failed,
            lease=lease,
            now=current.acquired_at,
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.create_external_operation(
            _tool_operation(operation_id="op_wrong_generation", turn=3),
            lease=current,
            now=current.acquired_at,
        )


@pytest.mark.asyncio
async def test_checkpoint_full_record_cas_and_operation_identity(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    lease = await _lease(store)
    first = _checkpoint()

    created = await store.put_run_checkpoint(
        first, expected=None, lease=lease, now=NOW
    )
    assert created == first
    assert created is not first
    assert await store.put_run_checkpoint(
        first, expected=None, lease=lease, now=NOW
    ) == first
    fetched = await store.get_run_checkpoint("run_1")
    assert fetched == first
    assert fetched is not first

    operation = _model_operation()
    await store.create_external_operation(operation, lease=lease, now=NOW)
    second = _checkpoint(
        checkpoint_version=2,
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation.operation_id,
    )
    assert await store.put_run_checkpoint(
        second, expected=first, lease=lease, now=NOW
    ) == second

    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            _checkpoint(checkpoint_version=4),
            expected=second,
            lease=lease,
            now=NOW,
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            second.model_copy(update={"checkpoint_version": 3}),
            expected=first,
            lease=lease,
            now=NOW,
        )

    other_kind = _tool_operation(operation_id="op_wrong_kind")
    await store.create_external_operation(other_kind, lease=lease, now=NOW)
    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            _checkpoint(
                checkpoint_version=3,
                phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
                operation_id=other_kind.operation_id,
            ),
            expected=second,
            lease=lease,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_checkpoint_rejects_missing_operation_and_stale_lease(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    lease = await _lease(store)
    first = _checkpoint()
    await store.put_run_checkpoint(first, expected=None, lease=lease, now=NOW)

    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            _checkpoint(
                checkpoint_version=2,
                phase=RunCheckpointPhase.TOOL_IN_FLIGHT,
                operation_id="op_missing",
            ),
            expected=first,
            lease=lease,
            now=NOW,
        )

    current = await store.acquire_lease(
        run_id="run_1",
        owner="worker_2",
        now=lease.expires_at,
        expires_at=lease.expires_at + timedelta(seconds=30),
    )
    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            _checkpoint(checkpoint_version=2),
            expected=first,
            lease=lease,
            now=current.acquired_at,
        )


@pytest.mark.asyncio
async def test_checkpoint_exact_target_replay_validates_expected_shape_and_adjacency(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    initial_lease = await _lease(store, run_id="run_initial")
    initial = _checkpoint(run_id="run_initial")
    await store.put_run_checkpoint(
        initial, expected=None, lease=initial_lease, now=NOW
    )
    assert await store.put_run_checkpoint(
        initial, expected=None, lease=initial_lease, now=NOW
    ) == initial

    lease = await _lease(store)
    first = _checkpoint()
    second = _checkpoint(checkpoint_version=2, turn=1)
    third = _checkpoint(checkpoint_version=3, turn=2)
    await store.put_run_checkpoint(first, expected=None, lease=lease, now=NOW)
    await store.put_run_checkpoint(second, expected=first, lease=lease, now=NOW)

    assert await store.put_run_checkpoint(
        second, expected=first, lease=lease, now=NOW
    ) == second
    alternate_adjacent_predecessor = first.model_copy(
        update={"messages": ({"role": "user", "content": "alternate"},)}
    )
    assert await store.put_run_checkpoint(
        second,
        expected=alternate_adjacent_predecessor,
        lease=lease,
        now=NOW,
    ) == second

    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            second, expected=None, lease=lease, now=NOW
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            second, expected=second, lease=lease, now=NOW
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            second,
            expected=first.model_copy(update={"run_id": "run_other"}),
            lease=lease,
            now=NOW,
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            second,
            expected=first.model_copy(update={"session_id": "ses_other"}),
            lease=lease,
            now=NOW,
        )

    await store.put_run_checkpoint(third, expected=second, lease=lease, now=NOW)
    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            third, expected=first, lease=lease, now=NOW
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            third, expected=third, lease=lease, now=NOW
        )


@pytest.mark.asyncio
async def test_reconciliation_create_replay_identity_and_ordering(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    lease = await _lease(store)
    operation = _model_operation()
    await store.create_external_operation(operation, lease=lease, now=NOW)
    requests = (
        _request(request_id="rec_z", operation_id=operation.operation_id),
        _request(request_id="rec_a"),
    )

    for request in requests:
        created = await store.create_reconciliation_request(request)
        assert created == request
        assert created is not request
    assert await store.create_reconciliation_request(requests[0]) == requests[0]
    assert await store.get_reconciliation_request("missing") is None
    assert tuple(
        request.request_id
        for request in await store.list_pending_reconciliation_requests("run_1")
    ) == ("rec_a", "rec_z")

    with pytest.raises(RecoveryStateConflictError):
        await store.create_reconciliation_request(
            requests[0].model_copy(update={"reason": "different"})
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.create_reconciliation_request(
            _request(request_id="rec_missing", operation_id="op_missing")
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.create_reconciliation_request(
            _request(
                request_id="rec_wrong_owner",
                run_id="run_other",
                operation_id=operation.operation_id,
            )
        )


@pytest.mark.asyncio
async def test_reconciliation_resolution_is_atomic_audited_and_replayable(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    request = _request()
    await store.create_reconciliation_request(request)
    resolved = _resolved_request(request)
    event = _resolution_event(resolved)

    result = await store.resolve_reconciliation_request(
        expected=request,
        resolved=resolved,
        event=event,
    )
    assert result == resolved
    assert result is not resolved
    assert await store.list_pending_reconciliation_requests("run_1") == ()
    assert await store.resolve_reconciliation_request(
        expected=request,
        resolved=resolved,
        event=event,
    ) == resolved
    events = await store.read_events(after_cursor=0)
    assert [stored.event for stored in events] == [event]

    with pytest.raises(RecoveryStateConflictError):
        await store.resolve_reconciliation_request(
            expected=request,
            resolved=_resolved_request(
                request,
                event_id="evt_other",
                action=ReconciliationAction.RETRY,
            ),
            event=_resolution_event(
                _resolved_request(
                    request,
                    event_id="evt_other",
                    action=ReconciliationAction.RETRY,
                )
            ),
        )
    assert len(await store.read_events(after_cursor=0)) == 1


@pytest.mark.asyncio
async def test_failed_resolution_does_not_mutate_request_or_events(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    request = _request(request_id="rec_atomic")
    await store.create_reconciliation_request(request)
    resolved = _resolved_request(request, event_id="evt_atomic")
    invalid_event = _resolution_event(resolved).model_copy(
        update={"payload": {"secret": "wrong"}}
    )

    with pytest.raises(RecoveryStateConflictError):
        await store.resolve_reconciliation_request(
            expected=request,
            resolved=resolved,
            event=invalid_event,
        )

    assert await store.get_reconciliation_request(request.request_id) == request
    assert await store.read_events(after_cursor=0) == []


@pytest.mark.asyncio
async def test_delete_session_removes_every_recovery_record(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    lease = await _lease(store)
    operation = _model_operation()
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation.operation_id,
    )
    request = _request(operation_id=operation.operation_id)
    await store.create_external_operation(operation, lease=lease, now=NOW)
    await store.put_run_checkpoint(
        checkpoint, expected=None, lease=lease, now=NOW
    )
    await store.create_reconciliation_request(request)

    await store.delete_session("ses_other")
    assert await store.get_external_operation(operation.operation_id) == operation
    assert await store.get_run_checkpoint("run_1") == checkpoint
    assert await store.get_reconciliation_request(request.request_id) == request

    await store.delete_session("ses_1")

    assert await store.get_external_operation(operation.operation_id) is None
    assert await store.get_run_checkpoint("run_1") is None
    assert await store.get_reconciliation_request(request.request_id) is None
    assert await store.list_unresolved_external_operations("run_1") == ()
    assert await store.list_pending_reconciliation_requests("run_1") == ()
    assert await store.get_snapshot("run", "run_1") is None
    with pytest.raises(RecoveryStateConflictError):
        await store.create_reconciliation_request(
            _request(request_id="rec_false_owner", session_id="ses_other")
        )


@pytest.mark.asyncio
async def test_sqlite_reopen_round_trips_exact_typed_recovery_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reopen.db"
    store = await SQLiteStore.open(path)
    await _put_authoritative_run_history(store)
    lease = await _lease(store)
    operation = _model_operation(
        recovery_metadata={"status_query": {"supported": True}}
    )
    checkpoint = _checkpoint(
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation.operation_id,
        output_parts=("partial",),
    )
    request = _request(operation_id=operation.operation_id)
    await store.create_external_operation(operation, lease=lease, now=NOW)
    await store.put_run_checkpoint(
        checkpoint, expected=None, lease=lease, now=NOW
    )
    await store.create_reconciliation_request(request)
    resolved = _resolved_request(request)
    event = _resolution_event(resolved, sequence=2)
    await store.resolve_reconciliation_request(
        expected=request,
        resolved=resolved,
        event=event,
    )
    await store.close()

    reopened = await SQLiteStore.open(path)
    try:
        fetched_operation = await reopened.get_external_operation(operation.operation_id)
        fetched_checkpoint = await reopened.get_run_checkpoint(checkpoint.run_id)
        fetched_request = await reopened.get_reconciliation_request(request.request_id)
        assert fetched_operation == operation
        assert type(fetched_operation) is ModelCallOperation
        assert fetched_operation is not operation
        assert fetched_checkpoint == checkpoint
        assert fetched_checkpoint is not checkpoint
        assert fetched_request == resolved
        assert fetched_request is not resolved
        events = await reopened.read_events(after_cursor=0)
        assert len(events) == 3
        assert events[-1].event == event
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_lazy_sqlite_facade_forwards_the_recovery_contract(
    tmp_path: Path,
) -> None:
    store = _LazySQLiteStore(tmp_path / "lazy-recovery.db")
    await _put_authoritative_run_snapshot(store)
    lease = await _lease(store)
    operation = _model_operation()
    checkpoint = _checkpoint()
    request = _request(operation_id=operation.operation_id)

    try:
        assert await store.create_external_operation(
            operation, lease=lease, now=NOW
        ) == operation
        assert await store.get_external_operation(operation.operation_id) == operation
        assert await store.list_unresolved_external_operations("run_1") == (
            operation,
        )
        completed = operation.model_copy(
            update={
                "status": ExternalOperationStatus.COMPLETED,
                "outcome": {"text": "done"},
            }
        )
        assert await store.transition_external_operation(
            expected=operation,
            updated=completed,
            lease=lease,
            now=NOW,
        ) == completed
        assert await store.put_run_checkpoint(
            checkpoint, expected=None, lease=lease, now=NOW
        ) == checkpoint
        assert await store.get_run_checkpoint("run_1") == checkpoint
        assert await store.create_reconciliation_request(request) == request
        assert await store.get_reconciliation_request(request.request_id) == request
        assert await store.list_pending_reconciliation_requests("run_1") == (
            request,
        )
        resolved = _resolved_request(request)
        assert await store.resolve_reconciliation_request(
            expected=request,
            resolved=resolved,
            event=_resolution_event(resolved),
        ) == resolved
    finally:
        await store.close()


def _sdk_traceback_locals(error: BaseException) -> tuple[dict[str, Any], ...]:
    locals_by_frame: list[dict[str, Any]] = []
    traceback = error.__traceback__
    while traceback is not None:
        if _is_sdk_traceback_filename(traceback.tb_frame.f_code.co_filename):
            locals_by_frame.append(dict(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return tuple(locals_by_frame)


def _is_sdk_traceback_filename(filename: str) -> bool:
    return "/src/agent_sdk/" in filename.replace("\\", "/")


@pytest.mark.parametrize(
    "filename",
    (
        "/workspace/src/agent_sdk/storage/memory.py",
        r"D:\workspace\src\agent_sdk\storage\sqlite.py",
    ),
)
def test_sdk_traceback_frame_detection_is_cross_platform(filename: str) -> None:
    assert _is_sdk_traceback_filename(filename)


@pytest.mark.asyncio
async def test_recovery_conflict_traceback_does_not_retain_secret_records(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    lease = await _lease(store)
    secret = "provider-secret-4bd6b8b7"
    operation = _model_operation(recovery_metadata={"credential": secret})
    await store.create_external_operation(operation, lease=lease, now=NOW)

    with pytest.raises(RecoveryStateConflictError) as caught:
        await store.create_external_operation(
            operation.model_copy(update={"request_fingerprint": "different"}),
            lease=lease,
            now=NOW,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    sdk_locals = _sdk_traceback_locals(caught.value)
    assert sdk_locals
    assert all(secret not in repr(frame_locals) for frame_locals in sdk_locals)


@pytest.mark.asyncio
async def test_recovery_conflict_discards_underlying_store_error_context(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    existing = EventEnvelope(
        event_id="evt_existing",
        type="run.marker",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={},
        occurred_at=NOW,
    )
    await store.commit(CommitBatch(events=(existing,)))
    request = _request(request_id="rec_sequence")
    await store.create_reconciliation_request(request)
    resolved = _resolved_request(request, event_id="evt_sequence")

    with pytest.raises(RecoveryStateConflictError) as caught:
        await store.resolve_reconciliation_request(
            expected=request,
            resolved=resolved,
            event=_resolution_event(resolved, sequence=1),
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert await store.get_reconciliation_request(request.request_id) == request
    assert [stored.event for stored in await store.read_events(after_cursor=0)] == [
        existing
    ]


@pytest.mark.asyncio
async def test_store_rejects_cross_record_run_session_identity(
    seeded_recovery_store: Any,
) -> None:
    store = seeded_recovery_store
    lease = await _lease(store)
    operation = _model_operation()
    await store.create_external_operation(operation, lease=lease, now=NOW)

    with pytest.raises(RecoveryStateConflictError):
        await store.create_external_operation(
            _tool_operation(
                operation_id="op_other_session",
                session_id="ses_other",
            ),
            lease=lease,
            now=NOW,
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.put_run_checkpoint(
            _checkpoint(session_id="ses_other"),
            expected=None,
            lease=lease,
            now=NOW,
        )
    with pytest.raises(RecoveryStateConflictError):
        await store.create_reconciliation_request(
            _request(request_id="rec_other_session", session_id="ses_other")
        )
