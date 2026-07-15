import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, TypeAlias

from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.leases import Lease, LeaseHeldError, LeaseLostError
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot
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
    _valid_checkpoint_replay_shape,
)
from agent_sdk.storage.base import (
    canonical_snapshot_data,
    CommitBatch,
    CommitResult,
    EventPreconditionConflictError,
    EventPreconditionNotFoundError,
    RunProgressBatch,
    SnapshotPreconditionError,
    SnapshotPrecondition,
    SnapshotWrite,
    StoredEvent,
    _valid_run_progress_int64_fields,
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

    @_context_free_recovery_errors
    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        if not _valid_run_progress_int64_fields(batch):
            raise RecoveryStateConflictError
        try:
            return await self._commit_run_progress(batch)
        except (TypeError, ValueError):
            raise RecoveryStateConflictError from None

    async def _commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        async with self._lock:
            if not (
                batch.events
                or batch.snapshots
                or batch.operation is not None
                or batch.checkpoint is not None
                or batch.reconciliation is not None
            ):
                raise RecoveryStateConflictError
            if batch.now.tzinfo is None or batch.now.utcoffset() is None:
                raise RecoveryStateConflictError
            try:
                for event in batch.events:
                    json.dumps(
                        event.payload,
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                    json.dumps(
                        event.model_dump(mode="json", warnings="error"),
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                for snapshot in batch.snapshots:
                    canonical_snapshot_data(snapshot.data)
                if batch.operation is not None:
                    _canonical_record_json(batch.operation.updated)
                    if batch.operation.expected is not None:
                        _canonical_record_json(batch.operation.expected)
                if batch.checkpoint is not None:
                    _canonical_record_json(batch.checkpoint.updated)
                    if batch.checkpoint.expected is not None:
                        _canonical_record_json(batch.checkpoint.expected)
                if batch.checkpoint_precondition is not None:
                    _canonical_record_json(batch.checkpoint_precondition)
                if batch.reconciliation is not None:
                    _canonical_record_json(batch.reconciliation.updated)
                    if batch.reconciliation.expected is not None:
                        _canonical_record_json(batch.reconciliation.expected)
            except (TypeError, ValueError):
                raise RecoveryStateConflictError from None
            run_snapshot = self._snapshots.get(("run", batch.lease.run_id))
            if run_snapshot is None:
                raise RecoveryStateConflictError
            try:
                run = RunSnapshot.model_validate(run_snapshot.data)
            except ValueError:
                raise RecoveryStateConflictError from None
            self._check_recovery_run_session(run.run_id, run.session_id)
            if run.run_id != batch.lease.run_id:
                raise RecoveryStateConflictError
            for event in batch.events:
                if event.session_id != run.session_id or event.run_id not in {
                    None,
                    run.run_id,
                }:
                    raise RecoveryStateConflictError
            for snapshot in batch.snapshots:
                if snapshot.session_id != run.session_id:
                    raise RecoveryStateConflictError
                try:
                    if snapshot.kind == "run":
                        target_run = RunSnapshot.model_validate(snapshot.data)
                        if (
                            snapshot.entity_id != run.run_id
                            or target_run.run_id != run.run_id
                            or target_run.session_id != run.session_id
                            or target_run.version != snapshot.version
                        ):
                            raise RecoveryStateConflictError
                    elif snapshot.kind == "session":
                        target_session = SessionSnapshot.model_validate(
                            snapshot.data
                        )
                        if (
                            snapshot.entity_id != run.session_id
                            or target_session.session_id != run.session_id
                            or target_session.version != snapshot.version
                        ):
                            raise RecoveryStateConflictError
                except ValueError:
                    raise RecoveryStateConflictError from None

            operation_write = batch.operation
            checkpoint_write = batch.checkpoint
            reconciliation_write = batch.reconciliation
            operation_json: str | None = None
            checkpoint_json: str | None = None
            reconciliation_json: str | None = None

            if operation_write is not None:
                if operation_write.expected is None:
                    legal_operation_shape = (
                        operation_write.updated.status
                        is ExternalOperationStatus.STARTED
                    )
                else:
                    legal_operation_shape = _valid_operation_transition(
                        operation_write.expected,
                        operation_write.updated,
                        allow_refence=True,
                    )
                if not legal_operation_shape or not _valid_operation_refence_shape(
                    batch
                ):
                    raise RecoveryStateConflictError
            if checkpoint_write is not None and not _valid_checkpoint_replay_shape(
                checkpoint_write.updated, checkpoint_write.expected
            ):
                raise RecoveryStateConflictError
            if reconciliation_write is not None:
                if reconciliation_write.expected is None:
                    legal_reconciliation_shape = (
                        reconciliation_write.updated.status
                        is ReconciliationStatus.PENDING
                    )
                else:
                    resolution = reconciliation_write.updated.resolution
                    resolution_event = (
                        None
                        if resolution is None
                        else next(
                            (
                                event
                                for event in batch.events
                                if event.event_id == resolution.event_id
                            ),
                            None,
                        )
                    )
                    legal_reconciliation_shape = (
                        resolution_event is not None
                        and _valid_reconciliation_resolution(
                            reconciliation_write.expected,
                            reconciliation_write.updated,
                            resolution_event,
                        )
                    )
                if not legal_reconciliation_shape:
                    raise RecoveryStateConflictError
            self._check_run_progress_internal_targets(batch)

            if operation_write is not None:
                operation = operation_write.updated
                if (
                    operation.run_id != run.run_id
                    or operation.session_id != run.session_id
                    or operation.lease_generation != batch.lease.generation
                ):
                    raise RecoveryStateConflictError
            if checkpoint_write is not None:
                checkpoint = checkpoint_write.updated
                if (
                    checkpoint.run_id != run.run_id
                    or checkpoint.session_id != run.session_id
                ):
                    raise RecoveryStateConflictError
                operations = self._external_operations.copy()
                if operation_write is not None:
                    operations[operation_write.updated.operation_id] = (
                        _canonical_record_json(operation_write.updated)
                    )
                self._check_checkpoint_operation_in(
                    checkpoint, batch.lease, operations
                )
            checkpoint_precondition = batch.checkpoint_precondition
            if checkpoint_precondition is not None:
                if (
                    checkpoint_precondition.run_id != run.run_id
                    or checkpoint_precondition.session_id != run.session_id
                    or self._run_checkpoints.get(checkpoint_precondition.run_id)
                    != _canonical_record_json(checkpoint_precondition)
                ):
                    raise RecoveryStateConflictError
            if reconciliation_write is not None:
                request = reconciliation_write.updated
                if (
                    request.run_id != run.run_id
                    or request.session_id != run.session_id
                ):
                    raise RecoveryStateConflictError

            target_states = self._run_progress_target_states(batch)
            exact_targets = target_states.count("exact")
            if "conflict" in target_states or (
                exact_targets and exact_targets != len(target_states)
            ):
                raise RecoveryStateConflictError
            if target_states and exact_targets == len(target_states):
                return CommitResult(last_cursor=self._last_cursor, applied=False)

            self._check_recovery_lease(
                batch.lease,
                now=batch.now,
                run_id=run.run_id,
                lease_generation=batch.lease.generation,
            )
            self._check_run_progress_preconditions(batch)

            if operation_write is not None:
                operation = operation_write.updated
                self._check_recovery_run_session(
                    operation.run_id, operation.session_id
                )
                self._check_recovery_lease(
                    batch.lease,
                    now=batch.now,
                    run_id=operation.run_id,
                    lease_generation=operation.lease_generation,
                )
                operation_json = _canonical_record_json(operation)
                existing_operation = self._external_operations.get(
                    operation.operation_id
                )
                if operation_write.expected is None:
                    if (
                        existing_operation is not None
                    ):
                        raise RecoveryStateConflictError
                else:
                    expected_operation = operation_write.expected
                    if (
                        not _valid_operation_transition(
                            expected_operation,
                            operation,
                            allow_refence=True,
                        )
                        or existing_operation
                        != _canonical_record_json(expected_operation)
                    ):
                        raise RecoveryStateConflictError

            if checkpoint_write is not None:
                checkpoint = checkpoint_write.updated
                self._check_recovery_run_session(
                    checkpoint.run_id, checkpoint.session_id
                )
                self._check_recovery_lease(
                    batch.lease,
                    now=batch.now,
                    run_id=checkpoint.run_id,
                    lease_generation=batch.lease.generation,
                )
                existing_checkpoint = self._run_checkpoints.get(checkpoint.run_id)
                if checkpoint_write.expected is None:
                    if (
                        checkpoint.checkpoint_version != 1
                        or existing_checkpoint is not None
                    ):
                        raise RecoveryStateConflictError
                else:
                    expected_checkpoint = checkpoint_write.expected
                    if (
                        existing_checkpoint
                        != _canonical_record_json(expected_checkpoint)
                        or checkpoint.run_id != expected_checkpoint.run_id
                        or checkpoint.session_id
                        != expected_checkpoint.session_id
                        or checkpoint.checkpoint_version
                        != expected_checkpoint.checkpoint_version + 1
                    ):
                        raise RecoveryStateConflictError
                operations = self._external_operations.copy()
                if operation_write is not None and operation_json is not None:
                    operations[operation_write.updated.operation_id] = operation_json
                self._check_checkpoint_operation_in(checkpoint, batch.lease, operations)
                checkpoint_json = _canonical_record_json(checkpoint)

            if reconciliation_write is not None:
                request = reconciliation_write.updated
                self._check_recovery_run_session(request.run_id, request.session_id)
                if request.operation_id is not None:
                    linked_operation_json = self._external_operations.get(
                        request.operation_id
                    )
                    if operation_write is not None and (
                        operation_write.updated.operation_id == request.operation_id
                    ):
                        linked_operation_json = _canonical_record_json(
                            operation_write.updated
                        )
                    if linked_operation_json is None:
                        raise RecoveryStateConflictError
                    linked = _external_operation_from_json(linked_operation_json)
                    if (
                        linked.run_id != request.run_id
                        or linked.session_id != request.session_id
                    ):
                        raise RecoveryStateConflictError
                existing_request = self._reconciliation_requests.get(
                    request.request_id
                )
                if reconciliation_write.expected is None:
                    if existing_request is not None:
                        raise RecoveryStateConflictError
                elif existing_request != _canonical_record_json(
                    reconciliation_write.expected
                ):
                    raise RecoveryStateConflictError
                reconciliation_json = _canonical_record_json(request)

            events = self._events.copy()
            snapshots = self._snapshots.copy()
            last_cursor = self._last_cursor
            sequences = self._latest_sequences(events)
            event_ids = {stored.event.event_id for stored in events}
            for event in batch.events:
                if event.event_id in event_ids:
                    raise RecoveryStateConflictError
                event_ids.add(event.event_id)
                aggregate = _aggregate_key(event)
                previous_sequence = sequences.get(aggregate)
                if previous_sequence is not None and event.sequence <= previous_sequence:
                    raise RecoveryStateConflictError
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
                    raise RecoveryStateConflictError
                snapshots[key] = deepcopy(snapshot)

            operations = self._external_operations.copy()
            checkpoints = self._run_checkpoints.copy()
            requests = self._reconciliation_requests.copy()
            if operation_write is not None and operation_json is not None:
                operations[operation_write.updated.operation_id] = operation_json
            if checkpoint_write is not None and checkpoint_json is not None:
                checkpoints[checkpoint_write.updated.run_id] = checkpoint_json
            if (
                reconciliation_write is not None
                and reconciliation_json is not None
            ):
                requests[reconciliation_write.updated.request_id] = (
                    reconciliation_json
                )
            self._events = events
            self._snapshots = snapshots
            self._external_operations = operations
            self._run_checkpoints = checkpoints
            self._reconciliation_requests = requests
            self._last_cursor = last_cursor
            return CommitResult(last_cursor=last_cursor)

    @staticmethod
    def _check_run_progress_internal_targets(batch: RunProgressBatch) -> None:
        event_ids: set[str] = set()
        sequences: dict[_AggregateKey, int] = {}
        for event in batch.events:
            if event.event_id in event_ids:
                raise RecoveryStateConflictError
            event_ids.add(event.event_id)
            aggregate = _aggregate_key(event)
            previous_sequence = sequences.get(aggregate)
            if (
                previous_sequence is not None
                and event.sequence <= previous_sequence
            ):
                raise RecoveryStateConflictError
            sequences[aggregate] = event.sequence

        snapshot_keys: set[_SnapshotKey] = set()
        for snapshot in batch.snapshots:
            key = (snapshot.kind, snapshot.entity_id)
            if key in snapshot_keys:
                raise RecoveryStateConflictError
            snapshot_keys.add(key)

    def _run_progress_target_states(
        self, batch: RunProgressBatch
    ) -> list[str]:
        states: list[str] = []
        events_by_id = {
            stored.event.event_id: stored.event for stored in self._events
        }
        for event in batch.events:
            current_event = events_by_id.get(event.event_id)
            states.append(
                "absent"
                if current_event is None
                else "exact"
                if current_event == event
                else "conflict"
            )
        for snapshot_target in batch.snapshots:
            current_snapshot = self._snapshots.get(
                (snapshot_target.kind, snapshot_target.entity_id)
            )
            if (
                current_snapshot is None
                or current_snapshot.version < snapshot_target.version
            ):
                states.append("absent")
            elif (
                current_snapshot.session_id == snapshot_target.session_id
                and current_snapshot.version == snapshot_target.version
                and canonical_snapshot_data(current_snapshot.data)
                == canonical_snapshot_data(snapshot_target.data)
            ):
                states.append("exact")
            else:
                states.append("conflict")
        if batch.operation is not None:
            operation_target = batch.operation.updated
            current_operation_json = self._external_operations.get(
                operation_target.operation_id
            )
            target_json = _canonical_record_json(operation_target)
            if current_operation_json == target_json:
                states.append("exact")
            elif current_operation_json is None or (
                batch.operation.expected is not None
                and current_operation_json
                == _canonical_record_json(batch.operation.expected)
            ):
                states.append("absent")
            else:
                states.append("conflict")
        if batch.checkpoint is not None:
            checkpoint_target = batch.checkpoint.updated
            current_checkpoint_json = self._run_checkpoints.get(
                checkpoint_target.run_id
            )
            checkpoint_target_json = _canonical_record_json(checkpoint_target)
            if current_checkpoint_json == checkpoint_target_json:
                states.append("exact")
            elif current_checkpoint_json is None or (
                batch.checkpoint.expected is not None
                and current_checkpoint_json
                == _canonical_record_json(batch.checkpoint.expected)
            ):
                states.append("absent")
            else:
                states.append("conflict")
        if batch.reconciliation is not None:
            request_target = batch.reconciliation.updated
            current_request_json = self._reconciliation_requests.get(
                request_target.request_id
            )
            target_json = _canonical_record_json(request_target)
            current_request: ReconciliationRequest | None = None
            if current_request_json is not None:
                current_request = _reconciliation_request_from_json(
                    current_request_json
                )
                if (
                    current_request.request_id != request_target.request_id
                    or _canonical_record_json(current_request)
                    != current_request_json
                ):
                    states.append("conflict")
                    return states
            if current_request_json == target_json:
                assert current_request is not None
                self._check_exact_reconciliation_operation(
                    batch, current_request
                )
                states.append("exact")
            elif current_request_json is None or (
                batch.reconciliation.expected is not None
                and current_request_json
                == _canonical_record_json(batch.reconciliation.expected)
            ):
                states.append("absent")
            else:
                states.append("conflict")
        return states

    def _check_exact_reconciliation_operation(
        self,
        batch: RunProgressBatch,
        request: ReconciliationRequest,
    ) -> None:
        if request.operation_id is None:
            return
        operation_json = self._external_operations.get(request.operation_id)
        if operation_json is None:
            raise RecoveryStateConflictError
        if (
            batch.operation is not None
            and batch.operation.updated.operation_id == request.operation_id
            and operation_json
            != _canonical_record_json(batch.operation.updated)
        ):
            raise RecoveryStateConflictError
        operation = _external_operation_from_json(operation_json)
        if (
            _canonical_record_json(operation) != operation_json
            or operation.run_id != request.run_id
            or operation.session_id != request.session_id
        ):
            raise RecoveryStateConflictError

    def _check_run_progress_preconditions(self, batch: RunProgressBatch) -> None:
        events_by_id = {
            stored.event.event_id: stored for stored in self._events
        }
        for event_precondition in batch.event_preconditions:
            stored = events_by_id.get(event_precondition.event_id)
            if stored is None:
                raise RecoveryStateConflictError
            event = stored.event
            if (
                stored.cursor != event_precondition.cursor
                or event.session_id != event_precondition.session_id
                or event.run_id != event_precondition.run_id
                or event.type != event_precondition.type
                or event.sequence != event_precondition.sequence
            ):
                raise RecoveryStateConflictError
        for snapshot_precondition in batch.preconditions:
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
                raise RecoveryStateConflictError

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

    @_context_free_recovery_errors
    async def list_abandoned_run_ids(self, *, now: datetime) -> tuple[str, ...]:
        try:
            if now.tzinfo is None or now.utcoffset() is None:
                raise RecoveryStateConflictError
            normalized_now = now.astimezone(UTC)
            async with self._lock:
                leases: dict[str, Lease] = {}
                for run_id, stored_lease in self._leases.items():
                    lease = Lease.model_validate(stored_lease)
                    if (
                        lease.run_id != run_id
                        or self._lease_generations.get(run_id) != lease.generation
                    ):
                        raise RecoveryStateConflictError
                    leases[run_id] = lease

                abandoned: list[str] = []
                for snapshot in self._snapshots.values():
                    if snapshot.kind != "run":
                        continue
                    run = RunSnapshot.model_validate(snapshot.data)
                    if (
                        snapshot.entity_id != run.run_id
                        or snapshot.session_id != run.session_id
                        or snapshot.version != run.version
                    ):
                        raise RecoveryStateConflictError
                    session_write = self._snapshots.get(("session", run.session_id))
                    if session_write is None:
                        raise RecoveryStateConflictError
                    session = SessionSnapshot.model_validate(session_write.data)
                    session_owns_run = run.run_id in session.active_run_ids
                    run_is_final = run.status in {
                        RunStatus.COMPLETED,
                        RunStatus.FAILED,
                    }
                    if (
                        session_write.entity_id != session.session_id
                        or session_write.session_id != session.session_id
                        or session_write.version != session.version
                        or session.session_id != run.session_id
                        or session_owns_run == run_is_final
                    ):
                        raise RecoveryStateConflictError
                    if run.status not in {
                        RunStatus.RUNNING,
                        RunStatus.WAITING_PERMISSION,
                    }:
                        continue
                    run_lease = leases.get(run.run_id)
                    if run_lease is None or run_lease.expires_at <= normalized_now:
                        abandoned.append(run.run_id)
                return tuple(sorted(set(abandoned)))
        except RecoveryStateConflictError:
            raise
        except Exception:
            raise RecoveryStateConflictError from None

    @_context_free_recovery_errors
    async def latest_run_event_sequence(self, run_id: str) -> int | None:
        try:
            if not isinstance(run_id, str) or not run_id.strip():
                raise RecoveryStateConflictError
            async with self._lock:
                run_write = self._snapshots.get(("run", run_id))
                if run_write is None:
                    if any(
                        stored.event.run_id == run_id for stored in self._events
                    ):
                        raise RecoveryStateConflictError
                    return None
                run = RunSnapshot.model_validate(run_write.data)
                if (
                    run_write.entity_id != run.run_id
                    or run_write.session_id != run.session_id
                    or run_write.version != run.version
                    or run.run_id != run_id
                ):
                    raise RecoveryStateConflictError
                session_write = self._snapshots.get(("session", run.session_id))
                if session_write is None:
                    raise RecoveryStateConflictError
                session = SessionSnapshot.model_validate(session_write.data)
                if (
                    session_write.entity_id != session.session_id
                    or session_write.session_id != session.session_id
                    or session_write.version != session.version
                    or session.session_id != run.session_id
                    or (run.run_id in session.active_run_ids) != (
                        run.status
                        not in {RunStatus.COMPLETED, RunStatus.FAILED}
                    )
                ):
                    raise RecoveryStateConflictError
                sequences: list[int] = []
                event_ids: set[str] = set()
                for stored in self._events:
                    event = stored.event
                    if event.run_id != run_id:
                        continue
                    if (
                        type(stored.cursor) is not int
                        or stored.cursor <= 0
                        or not isinstance(event.event_id, str)
                        or not event.event_id.strip()
                        or event.event_id in event_ids
                        or type(event.schema_version) is not int
                        or event.schema_version <= 0
                        or not isinstance(event.type, str)
                        or not event.type.strip()
                        or event.session_id != run.session_id
                        or type(event.sequence) is not int
                        or event.sequence <= 0
                        or event.sequence in sequences
                        or event.occurred_at.tzinfo is None
                        or event.occurred_at.utcoffset() is None
                    ):
                        raise RecoveryStateConflictError
                    canonical_snapshot_data(event.payload)
                    event_ids.add(event.event_id)
                    sequences.append(event.sequence)
                return max(sequences, default=None)
        except RecoveryStateConflictError:
            raise
        except Exception:
            raise RecoveryStateConflictError from None

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
    async def list_external_operations(
        self, run_id: str
    ) -> tuple[ExternalOperation, ...]:
        async with self._lock:
            run_write = self._snapshots.get(("run", run_id))
            if run_write is None:
                raise RecoveryStateConflictError
            try:
                run = RunSnapshot.model_validate(run_write.data)
            except ValueError:
                raise RecoveryStateConflictError from None
            if (
                run.run_id != run_id
                or run.session_id != run_write.session_id
                or run.version != run_write.version
            ):
                raise RecoveryStateConflictError
            session_write = self._snapshots.get(("session", run.session_id))
            if session_write is None:
                raise RecoveryStateConflictError
            try:
                session = SessionSnapshot.model_validate(session_write.data)
            except ValueError:
                raise RecoveryStateConflictError from None
            if (
                session.session_id != run.session_id
                or session.session_id != session_write.session_id
                or session.version != session_write.version
                or run.run_id not in session.active_run_ids
            ):
                raise RecoveryStateConflictError
            operations: list[ExternalOperation] = []
            identities: set[str] = set()
            for wrapper_id, serialized in self._external_operations.items():
                try:
                    operation = _external_operation_from_json(serialized)
                except (TypeError, ValueError):
                    raise RecoveryStateConflictError from None
                if _canonical_record_json(operation) != serialized:
                    raise RecoveryStateConflictError
                if operation.operation_id != wrapper_id:
                    raise RecoveryStateConflictError
                if operation.run_id != run_id:
                    continue
                if (
                    operation.operation_id in identities
                    or operation.session_id != run.session_id
                ):
                    raise RecoveryStateConflictError
                identities.add(operation.operation_id)
                operations.append(operation)
            return tuple(
                sorted(
                    operations,
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
                if not _valid_checkpoint_replay_shape(checkpoint, expected):
                    raise RecoveryStateConflictError
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
            self._check_recovery_run_session(expected.run_id, expected.session_id)
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

    @_context_free_recovery_errors
    async def get_run_lease(self, run_id: str) -> Lease | None:
        try:
            async with self._lock:
                current = self._leases.get(run_id)
                if current is None:
                    return None
                lease = Lease.model_validate(current)
                if (
                    lease.run_id != run_id
                    or self._lease_generations.get(run_id) != lease.generation
                ):
                    raise RecoveryStateConflictError
                return lease.model_copy()
        except RecoveryStateConflictError:
            raise
        except Exception:
            raise RecoveryStateConflictError from None

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
        snapshot = self._snapshots.get(("run", run_id))
        if snapshot is None:
            raise RecoveryStateConflictError
        try:
            run = RunSnapshot.model_validate(snapshot.data)
        except ValueError:
            raise RecoveryStateConflictError from None
        if (
            snapshot.entity_id != run_id
            or snapshot.session_id != session_id
            or snapshot.version != run.version
            or run.run_id != run_id
            or run.session_id != session_id
        ):
            raise RecoveryStateConflictError
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
        self._check_checkpoint_operation_in(
            checkpoint, lease, self._external_operations
        )

    @staticmethod
    def _check_checkpoint_operation_in(
        checkpoint: RunCheckpoint,
        lease: Lease,
        operations: dict[str, str],
    ) -> None:
        if checkpoint.operation_id is None:
            return
        operation_json = operations.get(checkpoint.operation_id)
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
    expected: ExternalOperation,
    updated: ExternalOperation,
    *,
    allow_refence: bool = False,
) -> bool:
    if (
        expected.status is not ExternalOperationStatus.STARTED
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
        "provider_identity",
        "tool_identity",
        "recovery_metadata",
    )
    same_identity = all(
        getattr(expected, field) == getattr(updated, field)
        for field in immutable_fields
    )
    if not same_identity:
        return False
    if updated.status is ExternalOperationStatus.STARTED:
        return allow_refence and updated.lease_generation > expected.lease_generation
    return updated.lease_generation == expected.lease_generation


def _valid_operation_refence_shape(batch: RunProgressBatch) -> bool:
    operation_write = batch.operation
    if operation_write is None or operation_write.expected is None:
        return True
    expected = operation_write.expected
    updated = operation_write.updated
    if updated.status is not ExternalOperationStatus.STARTED:
        return True
    checkpoint = batch.checkpoint_precondition
    expected_phase = (
        RunCheckpointPhase.MODEL_IN_FLIGHT
        if isinstance(expected, ModelCallOperation)
        else RunCheckpointPhase.TOOL_IN_FLIGHT
    )
    return (
        checkpoint is not None
        and batch.checkpoint is None
        and checkpoint.run_id == expected.run_id
        and checkpoint.session_id == expected.session_id
        and checkpoint.turn == expected.turn
        and checkpoint.phase is expected_phase
        and checkpoint.operation_id == expected.operation_id
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
