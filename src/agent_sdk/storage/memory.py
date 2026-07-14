import asyncio
from copy import deepcopy
from datetime import datetime
from typing import Any, TypeAlias

from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.leases import Lease, LeaseHeldError, LeaseLostError
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
    detached_record,
    record_from_write,
    validate_replay,
)
from agent_sdk.tools.models import thaw_json

_AggregateKey: TypeAlias = tuple[str, str]
_SnapshotKey: TypeAlias = tuple[str, str]


def _aggregate_key(event: EventEnvelope) -> _AggregateKey:
    if event.run_id is not None:
        return ("run", event.run_id)
    return ("session", event.session_id)


class InMemoryStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._events: list[StoredEvent] = []
        self._snapshots: dict[_SnapshotKey, SnapshotWrite] = {}
        self._idempotency: dict[tuple[str, str], IdempotencyRecord] = {}
        self._leases: dict[str, Lease] = {}
        self._lease_generations: dict[str, int] = {}
        self._external_operations: dict[str, str] = {}
        self._run_checkpoints: dict[str, str] = {}
        self._reconciliation_requests: dict[str, str] = {}
        self._last_cursor = 0

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
            if request is not None:
                key = (request.scope, request.key)
                existing = self._idempotency.get(key)
                if existing is not None:
                    self._check_snapshot_preconditions(batch.replay_preconditions)
                    if existing.request_fingerprint != request.request_fingerprint:
                        raise IdempotencyConflictError("idempotency key was reused")
                    return CommitResult(
                        self._last_cursor,
                        applied=False,
                        idempotency=detached_record(existing),
                    )
                if isinstance(request, IdempotencyReplay):
                    raise IdempotencyReplayMissError(
                        "idempotency replay record no longer exists"
                    )
            if batch.event_preconditions:
                events_by_id = {
                    stored.event.event_id: stored for stored in self._events
                }
                for event_precondition in batch.event_preconditions:
                    stored = events_by_id.get(event_precondition.event_id)
                    if stored is None:
                        raise EventPreconditionNotFoundError(
                            "event precondition failed"
                        )
                    event = stored.event
                    if (
                        stored.cursor != event_precondition.cursor
                        or event.session_id != event_precondition.session_id
                        or event.run_id != event_precondition.run_id
                        or event.type != event_precondition.type
                        or event.sequence != event_precondition.sequence
                    ):
                        raise EventPreconditionConflictError(
                            "event precondition failed"
                        )
            self._check_snapshot_preconditions(batch.preconditions)
            events = self._events.copy()
            snapshots = self._snapshots.copy()
            idempotency = self._idempotency.copy()
            last_cursor = self._last_cursor
            sequences = self._latest_sequences(events)
            event_ids = {stored.event.event_id for stored in events}

            for event in batch.events:
                if event.event_id in event_ids:
                    raise ValueError("event id must be unique")
                event_ids.add(event.event_id)
                aggregate = _aggregate_key(event)
                previous_sequence = sequences.get(aggregate)
                if previous_sequence is not None and event.sequence <= previous_sequence:
                    raise ValueError("event sequence must be strictly increasing")
                sequences[aggregate] = event.sequence
                last_cursor += 1
                events.append(StoredEvent(last_cursor, deepcopy(event)))

            for snapshot in batch.snapshots:
                key = (snapshot.kind, snapshot.entity_id)
                previous_snapshot = snapshots.get(key)
                if (
                    previous_snapshot is not None
                    and snapshot.version <= previous_snapshot.version
                ):
                    raise ValueError("snapshot version must be strictly increasing")
                snapshots[key] = deepcopy(snapshot)

            if isinstance(incoming, IdempotencyRecord):
                idempotency[(incoming.scope, incoming.key)] = detached_record(incoming)

            self._events = events
            self._snapshots = snapshots
            self._idempotency = idempotency
            self._last_cursor = last_cursor
            return CommitResult(last_cursor, idempotency=incoming)

    def _check_snapshot_preconditions(
        self, preconditions: tuple[SnapshotPrecondition, ...]
    ) -> None:
        for snapshot_precondition in preconditions:
            snapshot = self._snapshots.get(
                (snapshot_precondition.kind, snapshot_precondition.entity_id)
            )
            if snapshot is None or (
                snapshot_precondition.version is not None
                and snapshot.version != snapshot_precondition.version
            ) or (
                snapshot_precondition.session_id is not None
                and snapshot.session_id != snapshot_precondition.session_id
            ) or (
                snapshot_precondition.data is not None
                and canonical_snapshot_data(snapshot.data)
                != canonical_snapshot_data(snapshot_precondition.data)
            ):
                raise SnapshotPreconditionError("snapshot precondition failed")

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
            selected: list[StoredEvent] = []
            for stored in self._events:
                if stored.cursor <= after_cursor:
                    continue
                if up_to_cursor is not None and stored.cursor > up_to_cursor:
                    break
                if session_id is not None and stored.event.session_id != session_id:
                    continue
                selected.append(deepcopy(stored))
                if limit is not None and len(selected) == limit:
                    break
            return selected

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        async with self._lock:
            snapshot = self._snapshots.get((kind, entity_id))
            if snapshot is None:
                return None
            return deepcopy(snapshot.data)

    async def latest_cursor(self) -> int:
        async with self._lock:
            return self._last_cursor

    async def get_idempotency(self, scope: str, key: str) -> IdempotencyRecord | None:
        async with self._lock:
            record = self._idempotency.get((scope, key))
            return None if record is None else detached_record(record)

    @_context_free_recovery_errors
    async def create_external_operation(
        self, operation: ExternalOperation, *, lease: Lease, now: datetime
    ) -> ExternalOperation:
        serialized = _canonical_record_json(operation)
        async with self._lock:
            self._check_recovery_run_session(operation.run_id, operation.session_id)
            self._check_recovery_lease(
                lease,
                now=now,
                run_id=operation.run_id,
                lease_generation=operation.lease_generation,
            )
            if operation.status is not ExternalOperationStatus.STARTED:
                raise RecoveryStateConflictError
            existing = self._external_operations.get(operation.operation_id)
            if existing is not None:
                if existing != serialized:
                    raise RecoveryStateConflictError
                return _external_operation_from_json(existing)
            self._external_operations[operation.operation_id] = serialized
            return _external_operation_from_json(serialized)

    async def get_external_operation(
        self, operation_id: str
    ) -> ExternalOperation | None:
        async with self._lock:
            serialized = self._external_operations.get(operation_id)
            return (
                None
                if serialized is None
                else _external_operation_from_json(serialized)
            )

    async def list_unresolved_external_operations(
        self, run_id: str
    ) -> tuple[ExternalOperation, ...]:
        async with self._lock:
            operations = tuple(
                _external_operation_from_json(serialized)
                for serialized in self._external_operations.values()
            )
            return tuple(
                sorted(
                    (
                        operation
                        for operation in operations
                        if operation.run_id == run_id
                        and operation.status is ExternalOperationStatus.STARTED
                    ),
                    key=lambda operation: (
                        operation.turn,
                        operation.operation_kind.value,
                        operation.operation_id,
                    ),
                )
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
            self._check_recovery_run_session(expected.run_id, expected.session_id)
            self._check_recovery_lease(
                lease,
                now=now,
                run_id=expected.run_id,
                lease_generation=expected.lease_generation,
            )
            if not _valid_operation_transition(expected, updated):
                raise RecoveryStateConflictError
            existing = self._external_operations.get(expected.operation_id)
            if existing == updated_json:
                return _external_operation_from_json(existing)
            if existing != expected_json:
                raise RecoveryStateConflictError
            self._external_operations[expected.operation_id] = updated_json
            return _external_operation_from_json(updated_json)

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
            self._check_recovery_run_session(
                checkpoint.run_id, checkpoint.session_id
            )
            self._check_recovery_lease(
                lease,
                now=now,
                run_id=checkpoint.run_id,
                lease_generation=lease.generation,
            )
            existing = self._run_checkpoints.get(checkpoint.run_id)
            if existing == checkpoint_json:
                self._check_checkpoint_operation(checkpoint, lease)
                return _checkpoint_from_json(existing)
            if expected is None:
                if existing is not None or checkpoint.checkpoint_version != 1:
                    raise RecoveryStateConflictError
            elif (
                existing != expected_json
                or checkpoint.run_id != expected.run_id
                or checkpoint.session_id != expected.session_id
                or checkpoint.checkpoint_version != expected.checkpoint_version + 1
            ):
                raise RecoveryStateConflictError
            self._check_checkpoint_operation(checkpoint, lease)
            self._run_checkpoints[checkpoint.run_id] = checkpoint_json
            return _checkpoint_from_json(checkpoint_json)

    async def get_run_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        async with self._lock:
            serialized = self._run_checkpoints.get(run_id)
            return None if serialized is None else _checkpoint_from_json(serialized)

    @_context_free_recovery_errors
    async def create_reconciliation_request(
        self, request: ReconciliationRequest
    ) -> ReconciliationRequest:
        serialized = _canonical_record_json(request)
        async with self._lock:
            self._check_recovery_run_session(request.run_id, request.session_id)
            if request.status is not ReconciliationStatus.PENDING:
                raise RecoveryStateConflictError
            if request.operation_id is not None:
                operation_json = self._external_operations.get(request.operation_id)
                if operation_json is None:
                    raise RecoveryStateConflictError
                operation = _external_operation_from_json(operation_json)
                if (
                    operation.run_id != request.run_id
                    or operation.session_id != request.session_id
                ):
                    raise RecoveryStateConflictError
            existing = self._reconciliation_requests.get(request.request_id)
            if existing is not None:
                if existing != serialized:
                    raise RecoveryStateConflictError
                return _reconciliation_request_from_json(existing)
            self._reconciliation_requests[request.request_id] = serialized
            return _reconciliation_request_from_json(serialized)

    async def get_reconciliation_request(
        self, request_id: str
    ) -> ReconciliationRequest | None:
        async with self._lock:
            serialized = self._reconciliation_requests.get(request_id)
            return (
                None
                if serialized is None
                else _reconciliation_request_from_json(serialized)
            )

    async def list_pending_reconciliation_requests(
        self, run_id: str
    ) -> tuple[ReconciliationRequest, ...]:
        async with self._lock:
            requests = (
                _reconciliation_request_from_json(serialized)
                for serialized in self._reconciliation_requests.values()
            )
            return tuple(
                sorted(
                    (
                        request
                        for request in requests
                        if request.run_id == run_id
                        and request.status is ReconciliationStatus.PENDING
                    ),
                    key=lambda request: request.request_id,
                )
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
            if not _valid_reconciliation_resolution(expected, resolved, event):
                raise RecoveryStateConflictError
            current = self._reconciliation_requests.get(expected.request_id)
            if current == resolved_json:
                stored_event = next(
                    (
                        stored.event
                        for stored in self._events
                        if stored.event.event_id == event.event_id
                    ),
                    None,
                )
                if stored_event != event:
                    raise RecoveryStateConflictError
                return _reconciliation_request_from_json(current)
            if current != expected_json:
                raise RecoveryStateConflictError
            if any(stored.event.event_id == event.event_id for stored in self._events):
                raise RecoveryStateConflictError
            aggregate = _aggregate_key(event)
            previous_sequence = self._latest_sequences(self._events).get(aggregate)
            if event.sequence <= 0 or (
                previous_sequence is not None and event.sequence <= previous_sequence
            ):
                raise RecoveryStateConflictError

            detached_event = EventEnvelope.model_validate_json(event.model_dump_json())
            events = self._events.copy()
            last_cursor = self._last_cursor + 1
            events.append(StoredEvent(last_cursor, detached_event))
            requests = self._reconciliation_requests.copy()
            requests[expected.request_id] = resolved_json
            self._events = events
            self._reconciliation_requests = requests
            self._last_cursor = last_cursor
            return _reconciliation_request_from_json(resolved_json)

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            run_ids = {
                snapshot.entity_id
                for snapshot in self._snapshots.values()
                if snapshot.kind == "run" and snapshot.session_id == session_id
            }
            events = [
                stored for stored in self._events if stored.event.session_id != session_id
            ]
            snapshots = {
                key: snapshot
                for key, snapshot in self._snapshots.items()
                if snapshot.session_id != session_id
            }
            self._events = events
            self._snapshots = snapshots
            self._idempotency = {
                key: record
                for key, record in self._idempotency.items()
                if record.session_id != session_id
            }
            self._leases = {
                run_id: lease
                for run_id, lease in self._leases.items()
                if run_id not in run_ids
            }
            self._lease_generations = {
                run_id: generation
                for run_id, generation in self._lease_generations.items()
                if run_id not in run_ids
            }
            self._external_operations = {
                operation_id: serialized
                for operation_id, serialized in self._external_operations.items()
                if _external_operation_from_json(serialized).session_id != session_id
            }
            self._run_checkpoints = {
                run_id: serialized
                for run_id, serialized in self._run_checkpoints.items()
                if _checkpoint_from_json(serialized).session_id != session_id
            }
            self._reconciliation_requests = {
                request_id: serialized
                for request_id, serialized in self._reconciliation_requests.items()
                if _reconciliation_request_from_json(serialized).session_id != session_id
            }

    async def acquire_lease(
        self, *, run_id: str, owner: str, now: datetime, expires_at: datetime
    ) -> Lease:
        candidate = Lease(
            run_id=run_id,
            owner=owner,
            generation=1,
            acquired_at=now,
            renewed_at=now,
            expires_at=expires_at,
        )
        async with self._lock:
            current = self._leases.get(run_id)
            if current is not None and current.expires_at > candidate.acquired_at:
                raise LeaseHeldError
            generation = self._lease_generations.get(run_id, 0) + 1
            acquired = candidate.model_copy(update={"generation": generation})
            self._leases[run_id] = acquired
            self._lease_generations[run_id] = generation
            return acquired.model_copy()

    async def renew_lease(
        self, lease: Lease, *, now: datetime, expires_at: datetime
    ) -> Lease:
        async with self._lock:
            current = self._leases.get(lease.run_id)
            if (
                current is None
                or current.owner != lease.owner
                or current.generation != lease.generation
                or current.expires_at <= now
                or now < current.renewed_at
                or expires_at < current.expires_at
            ):
                raise LeaseLostError
            renewed = Lease(
                run_id=current.run_id,
                owner=current.owner,
                generation=current.generation,
                acquired_at=current.acquired_at,
                renewed_at=now,
                expires_at=expires_at,
            )
            self._leases[lease.run_id] = renewed
            return renewed.model_copy()

    async def release_lease(self, lease: Lease) -> None:
        async with self._lock:
            current = self._leases.get(lease.run_id)
            if not _lease_matches(current, lease):
                raise LeaseLostError
            del self._leases[lease.run_id]

    async def assert_current_lease(self, lease: Lease, *, now: datetime) -> None:
        async with self._lock:
            current = self._leases.get(lease.run_id)
            if (
                current is None
                or current.owner != lease.owner
                or current.generation != lease.generation
                or current.expires_at <= now
            ):
                raise LeaseLostError

    def _check_recovery_lease(
        self,
        lease: Lease,
        *,
        now: datetime,
        run_id: str,
        lease_generation: int,
    ) -> None:
        current = self._leases.get(run_id)
        if (
            current is None
            or current.owner != lease.owner
            or current.generation != lease.generation
            or current.expires_at <= now
            or lease.run_id != run_id
            or lease_generation != lease.generation
        ):
            raise RecoveryStateConflictError

    def _check_recovery_run_session(self, run_id: str, session_id: str) -> None:
        operation_sessions = (
            _external_operation_from_json(serialized).session_id
            for serialized in self._external_operations.values()
            if _external_operation_from_json(serialized).run_id == run_id
        )
        checkpoint_sessions = (
            _checkpoint_from_json(serialized).session_id
            for serialized in self._run_checkpoints.values()
            if _checkpoint_from_json(serialized).run_id == run_id
        )
        request_sessions = (
            _reconciliation_request_from_json(serialized).session_id
            for serialized in self._reconciliation_requests.values()
            if _reconciliation_request_from_json(serialized).run_id == run_id
        )
        if any(
            owner_session != session_id
            for owner_session in (
                *operation_sessions,
                *checkpoint_sessions,
                *request_sessions,
            )
        ):
            raise RecoveryStateConflictError

    def _check_checkpoint_operation(
        self, checkpoint: RunCheckpoint, lease: Lease
    ) -> None:
        if checkpoint.operation_id is None:
            return
        operation_json = self._external_operations.get(checkpoint.operation_id)
        if operation_json is None:
            raise RecoveryStateConflictError
        operation = _external_operation_from_json(operation_json)
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

    @staticmethod
    def _latest_sequences(events: list[StoredEvent]) -> dict[_AggregateKey, int]:
        sequences: dict[_AggregateKey, int] = {}
        for stored in events:
            sequences[_aggregate_key(stored.event)] = stored.event.sequence
        return sequences


def _lease_matches(current: Lease | None, expected: Lease) -> bool:
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
