from agent_sdk.events.models import EventEnvelope
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot, SessionStatus
from agent_sdk.runtime.state_machine import SessionStateMachine
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.storage.idempotency import IdempotencyReplay, IdempotencyWrite


RUN_LIFECYCLE_FINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED}
)


async def load_session(store: StateStore, session_id: str) -> SessionSnapshot:
    data: dict[str, object] | None = None
    store_failed = False
    try:
        data = await store.get_snapshot("session", session_id)
    except Exception:
        store_failed = True
    if store_failed:
        raise AgentSDKError(
            ErrorCode.INTERNAL,
            "failed to load session",
            retryable=False,
        )
    if data is None:
        raise AgentSDKError(
            ErrorCode.NOT_FOUND,
            "session not found",
            retryable=False,
        )
    snapshot: SessionSnapshot | None = None
    validation_failed = False
    try:
        snapshot = SessionSnapshot.model_validate(data)
    except Exception:
        validation_failed = True
    if validation_failed:
        data = None
        raise AgentSDKError(
            ErrorCode.INTERNAL,
            "failed to load session",
            retryable=False,
        )
    assert snapshot is not None
    return snapshot


def session_write(snapshot: SessionSnapshot) -> SnapshotWrite:
    return SnapshotWrite(
        "session",
        snapshot.session_id,
        snapshot.session_id,
        snapshot.version,
        snapshot.model_dump(mode="json"),
    )


def exact_session_precondition(snapshot: SessionSnapshot) -> SnapshotPrecondition:
    return SnapshotPrecondition(
        "session",
        snapshot.session_id,
        snapshot.version,
        snapshot.session_id,
        snapshot.model_dump(mode="json"),
    )


def exact_run_precondition(snapshot: RunSnapshot) -> SnapshotPrecondition:
    return SnapshotPrecondition(
        "run",
        snapshot.run_id,
        snapshot.version,
        snapshot.session_id,
        snapshot.model_dump(mode="json"),
    )


def detach_run_transition(
    session: SessionSnapshot,
    run_id: str,
) -> tuple[SessionSnapshot, str]:
    if run_id not in session.active_run_ids:
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "run is not owned by session",
            retryable=False,
        )
    remaining = tuple(
        active_run_id
        for active_run_id in session.active_run_ids
        if active_run_id != run_id
    )
    close_now = (
        session.status is SessionStatus.CLOSING
        and not remaining
        and not session.active_workflow_run_ids
    )
    updated = session.model_copy(
        update={
            "active_run_ids": remaining,
            "status": SessionStatus.CLOSED if close_now else session.status,
            "version": session.version + 1,
        }
    )
    return updated, "session.closed" if close_now else "session.run.detached"


def detach_workflow_transition(
    session: SessionSnapshot,
    workflow_run_id: str,
) -> tuple[SessionSnapshot, str]:
    if workflow_run_id not in session.active_workflow_run_ids:
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "workflow is not owned by session",
            retryable=False,
        )
    remaining = tuple(
        active_workflow_run_id
        for active_workflow_run_id in session.active_workflow_run_ids
        if active_workflow_run_id != workflow_run_id
    )
    close_now = (
        session.status is SessionStatus.CLOSING
        and not session.active_run_ids
        and not remaining
    )
    updated = session.model_copy(
        update={
            "active_workflow_run_ids": remaining,
            "status": SessionStatus.CLOSED if close_now else session.status,
            "version": session.version + 1,
        }
    )
    return updated, "session.closed" if close_now else "session.workflow.detached"


def transition_session(
    previous: SessionSnapshot,
    target: SessionStatus,
) -> SessionSnapshot:
    SessionStateMachine.transition(previous.status, target)
    return previous.model_copy(
        update={"status": target, "version": previous.version + 1}
    )


def close_session_transition(
    previous: SessionSnapshot,
    target: SessionStatus,
) -> SessionSnapshot:
    if previous.status is SessionStatus.ACTIVE and target is SessionStatus.CLOSED:
        # Empty close is an atomic command-level composite. The public state machine
        # keeps ACTIVE -> CLOSED invalid, and no intermediate CLOSING fact is stored.
        SessionStateMachine.transition(SessionStatus.ACTIVE, SessionStatus.CLOSING)
        SessionStateMachine.transition(SessionStatus.CLOSING, SessionStatus.CLOSED)
        return previous.model_copy(
            update={"status": target, "version": previous.version + 1}
        )
    return transition_session(previous, target)


def session_transition_batch(
    previous: SessionSnapshot,
    updated: SessionSnapshot,
    event_type: str,
    *,
    idempotency: IdempotencyWrite | IdempotencyReplay | None = None,
) -> CommitBatch:
    precondition = exact_session_precondition(previous)
    return CommitBatch(
        events=(
            EventEnvelope.new(
                type=event_type,
                session_id=previous.session_id,
                run_id=None,
                sequence=updated.version,
                payload=updated.model_dump(mode="json"),
            ),
        ),
        snapshots=(session_write(updated),),
        preconditions=(precondition,),
        idempotency=idempotency,
        replay_preconditions=(precondition,) if idempotency is not None else (),
    )
