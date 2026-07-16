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
    from agent_sdk.runtime.reconciliation import (
        ExternalOperation,
        ReconciliationRequest,
        RunCheckpoint,
    )


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


class ExternalOperationWrite(NamedTuple):
    expected: ExternalOperation | None
    updated: ExternalOperation


class RunCheckpointWrite(NamedTuple):
    expected: RunCheckpoint | None
    updated: RunCheckpoint


class ReconciliationRequestWrite(NamedTuple):
    expected: ReconciliationRequest | None
    updated: ReconciliationRequest


class RunProgressBatch(NamedTuple):
    lease: Lease
    now: datetime
    events: tuple[EventEnvelope, ...] = ()
    snapshots: tuple[SnapshotWrite, ...] = ()
    preconditions: tuple[SnapshotPrecondition, ...] = ()
    event_preconditions: tuple[EventPrecondition, ...] = ()
    operation: ExternalOperationWrite | None = None
    checkpoint: RunCheckpointWrite | None = None
    reconciliation: ReconciliationRequestWrite | None = None
    checkpoint_precondition: RunCheckpoint | None = None
    operation_precondition: ExternalOperation | None = None


_SIGNED_INT64_MIN = -(1 << 63)
_SIGNED_INT64_MAX = (1 << 63) - 1


def _valid_run_progress_int64_fields(batch: RunProgressBatch) -> bool:
    def valid(value: object) -> bool:
        return (
            type(value) is int
            and _SIGNED_INT64_MIN <= value <= _SIGNED_INT64_MAX
        )

    if not valid(batch.lease.generation):
        return False
    for event in batch.events:
        if not valid(event.schema_version) or not valid(event.sequence):
            return False
    for snapshot in batch.snapshots:
        if not valid(snapshot.version):
            return False
    for snapshot_precondition in batch.preconditions:
        if snapshot_precondition.version is not None and not valid(
            snapshot_precondition.version
        ):
            return False
    for event_precondition in batch.event_preconditions:
        if not valid(event_precondition.cursor) or not valid(
            event_precondition.sequence
        ):
            return False
    if batch.operation is not None:
        operations = [batch.operation.updated]
        if batch.operation.expected is not None:
            operations.append(batch.operation.expected)
        for operation in operations:
            if not valid(operation.turn) or not valid(
                operation.lease_generation
            ):
                return False
    if batch.operation_precondition is not None:
        operation = batch.operation_precondition
        if not valid(operation.turn) or not valid(operation.lease_generation):
            return False
    if batch.checkpoint is not None:
        checkpoints = [batch.checkpoint.updated]
        if batch.checkpoint.expected is not None:
            checkpoints.append(batch.checkpoint.expected)
        for checkpoint in checkpoints:
            if not valid(checkpoint.checkpoint_version) or not valid(
                checkpoint.turn
            ):
                return False
    if batch.checkpoint_precondition is not None:
        checkpoint = batch.checkpoint_precondition
        if not valid(checkpoint.checkpoint_version) or not valid(checkpoint.turn):
            return False
    return True


class CommitResult(NamedTuple):
    last_cursor: int
    applied: bool = True
    idempotency: IdempotencyRecord | None = None


class StoredEvent(NamedTuple):
    cursor: int
    event: EventEnvelope


class StateStore(Protocol):
    async def commit(self, batch: CommitBatch) -> CommitResult: ...

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult: ...

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

    async def get_run_lease(self, run_id: str) -> Lease | None: ...

    async def list_abandoned_run_ids(self, *, now: datetime) -> tuple[str, ...]: ...

    async def latest_run_event_sequence(self, run_id: str) -> int | None: ...

    async def create_external_operation(
        self, operation: ExternalOperation, *, lease: Lease, now: datetime
    ) -> ExternalOperation: ...

    async def get_external_operation(
        self, operation_id: str
    ) -> ExternalOperation | None: ...

    async def list_unresolved_external_operations(
        self, run_id: str
    ) -> tuple[ExternalOperation, ...]: ...

    async def list_external_operations(
        self, run_id: str
    ) -> tuple[ExternalOperation, ...]: ...

    async def transition_external_operation(
        self,
        *,
        expected: ExternalOperation,
        updated: ExternalOperation,
        lease: Lease,
        now: datetime,
    ) -> ExternalOperation: ...

    async def put_run_checkpoint(
        self,
        checkpoint: RunCheckpoint,
        *,
        expected: RunCheckpoint | None,
        lease: Lease,
        now: datetime,
    ) -> RunCheckpoint: ...

    async def get_run_checkpoint(self, run_id: str) -> RunCheckpoint | None: ...

    async def create_reconciliation_request(
        self, request: ReconciliationRequest
    ) -> ReconciliationRequest: ...

    async def get_reconciliation_request(
        self, request_id: str
    ) -> ReconciliationRequest | None: ...

    async def list_reconciliation_requests(
        self, run_id: str
    ) -> tuple[ReconciliationRequest, ...]: ...

    async def list_pending_reconciliation_requests(
        self, run_id: str
    ) -> tuple[ReconciliationRequest, ...]: ...

    async def resolve_reconciliation_request(
        self,
        *,
        expected: ReconciliationRequest,
        resolved: ReconciliationRequest,
        event: EventEnvelope,
    ) -> ReconciliationRequest: ...
