import json
from typing import Any, NamedTuple, Protocol

from agent_sdk.events.models import EventEnvelope


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


class CommitBatch(NamedTuple):
    events: tuple[EventEnvelope, ...]
    snapshots: tuple[SnapshotWrite, ...] = ()
    preconditions: tuple[SnapshotPrecondition, ...] = ()


class CommitResult(NamedTuple):
    last_cursor: int


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

    async def latest_cursor(self) -> int: ...

    async def delete_session(self, session_id: str) -> None: ...
