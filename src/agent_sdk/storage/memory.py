import asyncio
from copy import deepcopy
from typing import Any, TypeAlias

from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import (
    canonical_snapshot_data,
    CommitBatch,
    CommitResult,
    SnapshotPreconditionError,
    SnapshotWrite,
    StoredEvent,
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
        self._last_cursor = 0

    async def commit(self, batch: CommitBatch) -> CommitResult:
        async with self._lock:
            for precondition in batch.preconditions:
                snapshot = self._snapshots.get(
                    (precondition.kind, precondition.entity_id)
                )
                if snapshot is None or (
                    precondition.version is not None
                    and snapshot.version != precondition.version
                ) or (
                    precondition.session_id is not None
                    and snapshot.session_id != precondition.session_id
                ) or (
                    precondition.data is not None
                    and canonical_snapshot_data(snapshot.data)
                    != canonical_snapshot_data(precondition.data)
                ):
                    raise SnapshotPreconditionError(
                        "snapshot precondition failed"
                    )
            events = self._events.copy()
            snapshots = self._snapshots.copy()
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

            self._events = events
            self._snapshots = snapshots
            self._last_cursor = last_cursor
            return CommitResult(last_cursor)

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

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
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

    @staticmethod
    def _latest_sequences(events: list[StoredEvent]) -> dict[_AggregateKey, int]:
        sequences: dict[_AggregateKey, int] = {}
        for stored in events:
            sequences[_aggregate_key(stored.event)] = stored.event.sequence
        return sequences
