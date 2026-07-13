from typing import Any, NamedTuple, Protocol

from agent_sdk.events.models import EventEnvelope


class SnapshotWrite(NamedTuple):
    kind: str
    entity_id: str
    session_id: str
    version: int
    data: dict[str, Any]


class CommitBatch(NamedTuple):
    events: tuple[EventEnvelope, ...]
    snapshots: tuple[SnapshotWrite, ...] = ()


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
    ) -> list[StoredEvent]: ...

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None: ...

    async def delete_session(self, session_id: str) -> None: ...
