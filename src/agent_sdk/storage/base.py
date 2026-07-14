from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.idempotency import (
    IdempotencyRecord,
    IdempotencyReplay,
    IdempotencyWrite,
)

if TYPE_CHECKING:
    from agent_sdk.runtime.leases import Lease


def canonical_snapshot_data(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class SnapshotWrite(NamedTuple):
    kind: str
    entity_id: str
    session_id: str
    version: int
    data: dict[str, Any]


class SnapshotPrecondition(NamedTuple):
    kind: str
    entity_id: str
    version: int | None = None
    session_id: str | None = None
    data: dict[str, Any] | None = None


class SnapshotPreconditionError(ValueError):
    """A required snapshot was missing or no longer at the expected version."""


class EventPrecondition(NamedTuple):
    event_id: str
    cursor: int
    session_id: str
    run_id: str | None
    type: str
    sequence: int


class EventPreconditionError(ValueError):
    """A required evidence event was missing or had changed identity."""


class EventPreconditionNotFoundError(EventPreconditionError):
    """A required evidence event no longer exists."""


class EventPreconditionConflictError(EventPreconditionError):
    """A required evidence event exists with a different durable identity."""


class CommitBatch(NamedTuple):
    events: tuple[EventEnvelope, ...]
    snapshots: tuple[SnapshotWrite, ...] = ()
    preconditions: tuple[SnapshotPrecondition, ...] = ()
    event_preconditions: tuple[EventPrecondition, ...] = ()
    idempotency: IdempotencyWrite | IdempotencyReplay | None = None
    replay_preconditions: tuple[SnapshotPrecondition, ...] = ()


class CommitResult(NamedTuple):
    last_cursor: int
    applied: bool = True
    idempotency: IdempotencyRecord | None = None


class StoredEvent(NamedTuple):
    cursor: int
    event: EventEnvelope


class StateStore(Protocol):
    async def commit(self, batch: CommitBatch) -> CommitResult: ...

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]: ...

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None: ...

    async def get_idempotency(
        self, scope: str, key: str
    ) -> IdempotencyRecord | None: ...

    async def latest_cursor(self) -> int: ...

    async def delete_session(self, session_id: str) -> None: ...

    async def acquire_lease(
        self, *, run_id: str, owner: str, now: datetime, expires_at: datetime
    ) -> Lease: ...

    async def renew_lease(
        self, lease: Lease, *, now: datetime, expires_at: datetime
    ) -> Lease: ...

    async def release_lease(self, lease: Lease) -> None: ...

    async def assert_current_lease(self, lease: Lease, *, now: datetime) -> None: ...
