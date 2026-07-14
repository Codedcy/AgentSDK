import asyncio
from copy import deepcopy
from datetime import datetime
from typing import Any, TypeAlias

from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.leases import Lease, LeaseHeldError, LeaseLostError
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
