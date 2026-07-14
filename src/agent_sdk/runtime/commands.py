import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from agent_sdk.events.models import EventEnvelope
from agent_sdk.errors import AgentSDKError, ErrorCode, SessionBusyError
from agent_sdk.ids import new_id
from agent_sdk.runtime.idempotency import _idempotency_public_error
from agent_sdk.runtime.execution import ExecutionDescriptor
from agent_sdk.runtime.models import (
    RunSnapshot,
    RunStatus,
    SessionSnapshot,
    SessionStatus,
)
from agent_sdk.runtime.session_lifecycle import (
    close_session_transition,
    exact_session_precondition,
    load_session,
    session_transition_batch,
    session_write,
    transition_session,
)
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    SnapshotPreconditionError,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.storage.idempotency import (
    IdempotencyError,
    IdempotencyRecord,
    IdempotencyReplay,
    IdempotencyReplayMissError,
    IdempotencyWrite,
    fingerprint_command,
    validate_replay,
)
from agent_sdk.subagents.models import TaskEnvelope

_MAX_SESSION_COMMIT_ATTEMPTS = 8
T = TypeVar("T")


@dataclass(frozen=True)
class CommandOutcome(Generic[T]):
    value: T
    replayed: bool

    def __getattr__(self, name: str) -> Any:
        """Keep M01 internal snapshot reads source-compatible during migration."""
        return getattr(self.value, name)


def session_result_idempotency(
    snapshot: SessionSnapshot,
    key: str,
) -> IdempotencyWrite:
    return IdempotencyWrite(
        scope=f"session/{snapshot.session_id}/close",
        key=key,
        request_fingerprint=fingerprint_command(
            "session.close", {"session_id": snapshot.session_id}
        ),
        session_id=snapshot.session_id,
        result=snapshot.model_dump(mode="json"),
    )


def validate_session_result(result: Mapping[str, Any]) -> SessionSnapshot:
    snapshot: SessionSnapshot | None = None
    validation_failed = False
    try:
        snapshot = SessionSnapshot.model_validate(dict(result))
    except Exception:
        validation_failed = True
    result = {}
    if validation_failed:
        raise AgentSDKError(
            ErrorCode.INTERNAL,
            "session command result is invalid",
            retryable=False,
        )
    assert snapshot is not None
    return snapshot


class RuntimeCommands:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def create_session(
        self,
        *,
        workspaces: Iterable[str | Path],
        idempotency_key: str | None = None,
    ) -> SessionSnapshot:
        normalized_workspaces = tuple(str(workspace) for workspace in workspaces)
        snapshot = SessionSnapshot(
            session_id=new_id("ses"),
            workspaces=normalized_workspaces,
        )
        data = snapshot.model_dump(mode="json")
        event = EventEnvelope.new(
            type="session.created",
            session_id=snapshot.session_id,
            run_id=None,
            sequence=1,
            payload=data,
        )
        candidate = None
        if idempotency_key is not None:
            candidate = IdempotencyWrite(
                scope="session.create",
                key=idempotency_key,
                request_fingerprint=fingerprint_command(
                    "session.create", {"workspaces": list(normalized_workspaces)}
                ),
                session_id=snapshot.session_id,
                result=data,
            )
        batch = CommitBatch(
            events=(event,),
            snapshots=(session_write(snapshot),),
        )

        for _ in range(_MAX_SESSION_COMMIT_ATTEMPTS):
            public_error: AgentSDKError | None = None
            try:
                if candidate is None:
                    await self._commit_session_batch(
                        batch,
                        failure_message="failed to create session",
                    )
                    return snapshot
                hint = await self._get_idempotency(
                    candidate.scope,
                    candidate.key,
                    failure_message="failed to create session",
                )
                has_hint = hint is not None
                hint = None
                request: IdempotencyWrite | IdempotencyReplay = candidate
                if has_hint:
                    request = IdempotencyReplay(
                        candidate.scope,
                        candidate.key,
                        candidate.request_fingerprint,
                    )
                return self._validated_session_result(
                    await self._commit_session_batch(
                        batch._replace(idempotency=request),
                        failure_message="failed to create session",
                    ),
                    expected_workspaces=normalized_workspaces,
                )
            except IdempotencyReplayMissError:
                continue
            except IdempotencyError as error:
                public_error = _idempotency_public_error(error)
            if public_error is not None:
                raise public_error
        raise _idempotency_public_error(
            IdempotencyReplayMissError("idempotency replay retry exhausted")
        ) from None

    async def get_session(self, session_id: str) -> SessionSnapshot:
        current = await load_session(self._store, session_id)
        if current.status is SessionStatus.DELETING:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "session is deleting",
                retryable=False,
            )
        return current

    async def close_session(
        self,
        session_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> SessionSnapshot:
        for _ in range(_MAX_SESSION_COMMIT_ATTEMPTS):
            public_error: AgentSDKError | None = None
            current = await load_session(self._store, session_id)
            if current.status is SessionStatus.DELETING:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "session is deleting",
                    retryable=False,
                )
            try:
                if current.status in {SessionStatus.CLOSING, SessionStatus.CLOSED}:
                    return await self._record_session_result(
                        current,
                        idempotency_key,
                    )
                target = (
                    SessionStatus.CLOSED
                    if not current.active_run_ids
                    and not current.active_workflow_run_ids
                    else SessionStatus.CLOSING
                )
                updated = close_session_transition(current, target)
                request: IdempotencyWrite | IdempotencyReplay | None = None
                if idempotency_key is not None:
                    candidate = session_result_idempotency(updated, idempotency_key)
                    hint = await self._get_idempotency(
                        candidate.scope,
                        candidate.key,
                        failure_message="failed to close session",
                    )
                    has_hint = hint is not None
                    hint = None
                    request = candidate
                    if has_hint:
                        request = IdempotencyReplay(
                            candidate.scope,
                            candidate.key,
                            candidate.request_fingerprint,
                        )
                transition_batch = session_transition_batch(
                    current,
                    updated,
                    "session.closed"
                    if target is SessionStatus.CLOSED
                    else "session.closing",
                    idempotency=request,
                )
                if request is None:
                    await self._commit_session_batch(
                        transition_batch,
                        failure_message="failed to close session",
                    )
                    return updated
                return self._validated_session_result(
                    await self._commit_session_batch(
                        transition_batch,
                        failure_message="failed to close session",
                    )
                )
            except (SnapshotPreconditionError, IdempotencyReplayMissError):
                continue
            except IdempotencyError as error:
                public_error = _idempotency_public_error(error)
            if public_error is not None:
                raise public_error
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "session changed concurrently",
            retryable=True,
        )

    async def delete_session(self, session_id: str) -> None:
        for _ in range(_MAX_SESSION_COMMIT_ATTEMPTS):
            current = await load_session(self._store, session_id)
            if current.status in {SessionStatus.ACTIVE, SessionStatus.CLOSING}:
                raise SessionBusyError()
            if current.status is SessionStatus.CLOSED:
                deleting = transition_session(current, SessionStatus.DELETING)
                try:
                    await self._commit_session_batch(
                        session_transition_batch(
                            current,
                            deleting,
                            "session.deleting",
                        ),
                        failure_message="failed to delete session",
                    )
                except SnapshotPreconditionError:
                    continue
            delete_failed = False
            try:
                await self._store.delete_session(session_id)
            except Exception:
                delete_failed = True
            if delete_failed:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "failed to delete session",
                    retryable=False,
                )
            return
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "session changed concurrently",
            retryable=True,
        )

    async def _record_session_result(
        self,
        current: SessionSnapshot,
        key: str | None,
    ) -> SessionSnapshot:
        if key is None:
            return current
        candidate = session_result_idempotency(current, key)
        hint = await self._get_idempotency(
            candidate.scope,
            key,
            failure_message="failed to close session",
        )
        has_hint = hint is not None
        hint = None
        request: IdempotencyWrite | IdempotencyReplay = candidate
        if has_hint:
            request = IdempotencyReplay(
                candidate.scope,
                key,
                candidate.request_fingerprint,
            )
        precondition = exact_session_precondition(current)
        return self._validated_session_result(
            await self._commit_session_batch(
                CommitBatch(
                    events=(),
                    preconditions=(precondition,),
                    idempotency=request,
                    replay_preconditions=(precondition,),
                ),
                failure_message="failed to close session",
            )
        )

    async def _get_idempotency(
        self,
        scope: str,
        key: str,
        *,
        failure_message: str,
    ) -> IdempotencyRecord | None:
        record: IdempotencyRecord | None = None
        store_failed = False
        try:
            record = await self._store.get_idempotency(scope, key)
        except IdempotencyError:
            raise
        except Exception:
            store_failed = True
        if store_failed:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                failure_message,
                retryable=False,
            )
        return record

    async def _commit_session_batch(
        self,
        batch: CommitBatch,
        *,
        failure_message: str,
    ) -> CommitResult:
        result: CommitResult | None = None
        store_failed = False
        try:
            result = await self._store.commit(batch)
        except (IdempotencyError, SnapshotPreconditionError):
            raise
        except Exception:
            store_failed = True
        if store_failed:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                failure_message,
                retryable=False,
            )
        assert result is not None
        return result

    @staticmethod
    def _validated_session_result(
        result: CommitResult,
        *,
        expected_workspaces: tuple[str, ...] | None = None,
    ) -> SessionSnapshot:
        record = result.idempotency
        if record is None:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "session command result is missing",
                retryable=False,
            )
        payload = dict(record.result)
        del result
        del record
        snapshot: SessionSnapshot | None = None
        try:
            snapshot = validate_session_result(payload)
        finally:
            payload.clear()
        if (
            expected_workspaces is not None
            and snapshot.workspaces != expected_workspaces
        ):
            snapshot = None
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "session command result is invalid",
                retryable=False,
            ) from None
        return snapshot

    async def start_run(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        agent_revision: str,
        user_input: str,
        parent_run_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_node_id: str | None = None,
        task_envelope: TaskEnvelope | None = None,
        execution_descriptor: ExecutionDescriptor | None = None,
        idempotency_key: str | None = None,
    ) -> CommandOutcome[RunSnapshot]:
        if execution_descriptor is None and idempotency_key is not None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "legacy run cannot use idempotency",
                retryable=False,
            ) from None
        scope = f"session/{session_id}/run.start"
        invalid_key: AgentSDKError | None = None
        if idempotency_key is not None:
            try:
                validate_replay(
                    IdempotencyReplay(scope, idempotency_key, "0" * 64)
                )
            except IdempotencyError as error:
                invalid_key = _idempotency_public_error(error)
        if invalid_key is not None:
            idempotency_key = None
            raise invalid_key from None

        selected_run_id = run_id or new_id("run")
        compatibility: Literal["legacy_unknown", "current"] = (
            "current" if execution_descriptor is not None else "legacy_unknown"
        )
        fingerprint: str | None = None
        if idempotency_key is not None:
            fingerprint = fingerprint_command(
                "run.start",
                {
                    "session_id": session_id,
                    "execution_descriptor": (
                        None
                        if execution_descriptor is None
                        else execution_descriptor.model_dump(mode="json")
                    ),
                    "user_input": user_input,
                    "parent_run_id": parent_run_id,
                    "workflow_run_id": workflow_run_id,
                    "workflow_node_id": workflow_node_id,
                    "task_envelope": (
                        None
                        if task_envelope is None
                        else task_envelope.model_dump(mode="json")
                    ),
                },
            )

        for attempt in range(_MAX_SESSION_COMMIT_ATTEMPTS):
            public_error: AgentSDKError | None = None
            current = await load_session(self._store, session_id)
            if current.status is SessionStatus.DELETING:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "session is deleting",
                    retryable=False,
                )

            has_hint = False
            if idempotency_key is not None:
                hint_error: AgentSDKError | None = None
                try:
                    hint = await self._get_idempotency(
                        scope,
                        idempotency_key,
                        failure_message="failed to start run",
                    )
                except IdempotencyError as error:
                    hint_error = _idempotency_public_error(error)
                if hint_error is not None:
                    raise hint_error from None
                has_hint = hint is not None
                hint = None

            request: IdempotencyWrite | IdempotencyReplay | None
            if has_hint:
                assert idempotency_key is not None
                assert fingerprint is not None
                request = IdempotencyReplay(scope, idempotency_key, fingerprint)
                precondition = exact_session_precondition(current)
                batch = CommitBatch(
                    events=(),
                    idempotency=request,
                    replay_preconditions=(precondition,),
                )
                snapshot = None
            else:
                if current.status is not SessionStatus.ACTIVE:
                    raise AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "session is not active",
                        retryable=False,
                    )
                snapshot = RunSnapshot(
                    run_id=selected_run_id,
                    session_id=session_id,
                    agent_revision=agent_revision,
                    status=RunStatus.CREATED,
                    user_input=user_input,
                    parent_run_id=parent_run_id,
                    workflow_run_id=workflow_run_id,
                    workflow_node_id=workflow_node_id,
                    task_envelope=task_envelope,
                    execution_compatibility=compatibility,
                    execution_descriptor=execution_descriptor,
                )
                run_data = snapshot.model_dump(mode="json")
                active_run_ids = tuple(
                    sorted((*current.active_run_ids, snapshot.run_id))
                )
                updated_session = current.model_copy(
                    update={
                        "active_run_ids": active_run_ids,
                        "version": current.version + 1,
                    }
                )
                session_event = EventEnvelope.new(
                    type="session.run.attached",
                    session_id=session_id,
                    run_id=None,
                    sequence=updated_session.version,
                    payload={"run_id": snapshot.run_id},
                )
                run_event = EventEnvelope.new(
                    type="run.created",
                    session_id=session_id,
                    run_id=snapshot.run_id,
                    sequence=1,
                    payload=run_data,
                )
                request = None
                if idempotency_key is not None:
                    assert fingerprint is not None
                    request = IdempotencyWrite(
                        scope=scope,
                        key=idempotency_key,
                        request_fingerprint=fingerprint,
                        session_id=session_id,
                        result=run_data,
                    )
                session_precondition = exact_session_precondition(current)
                batch = CommitBatch(
                    events=(session_event, run_event),
                    snapshots=(
                        session_write(updated_session),
                        SnapshotWrite(
                            "run",
                            snapshot.run_id,
                            session_id,
                            snapshot.version,
                            run_data,
                        ),
                    ),
                    preconditions=(session_precondition,),
                    idempotency=request,
                    replay_preconditions=(
                        (session_precondition,) if request is not None else ()
                    ),
                )

            try:
                result: CommitResult | None = await self._commit_session_batch(
                    batch,
                    failure_message="failed to start run",
                )
            except (SnapshotPreconditionError, IdempotencyReplayMissError):
                if attempt + 1 < _MAX_SESSION_COMMIT_ATTEMPTS:
                    await asyncio.sleep(0)
                continue
            except IdempotencyError as error:
                public_error = _idempotency_public_error(error)
            if public_error is not None:
                raise public_error from None

            assert result is not None
            if request is None:
                assert snapshot is not None
                return CommandOutcome(snapshot, replayed=False)
            replayed = not result.applied
            stored, validation_error = self._validated_run_result(
                result,
                session_id=session_id,
                agent_revision=agent_revision,
                user_input=user_input,
                parent_run_id=parent_run_id,
                workflow_run_id=workflow_run_id,
                workflow_node_id=workflow_node_id,
                task_envelope=task_envelope,
                execution_descriptor=execution_descriptor,
            )
            result = None
            if validation_error is not None:
                raise validation_error from None
            assert stored is not None
            return CommandOutcome(stored, replayed=replayed)

        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "session state changed concurrently",
            retryable=True,
        )

    @staticmethod
    def _validated_run_result(
        result: CommitResult,
        *,
        session_id: str,
        agent_revision: str,
        user_input: str,
        parent_run_id: str | None,
        workflow_run_id: str | None,
        workflow_node_id: str | None,
        task_envelope: TaskEnvelope | None,
        execution_descriptor: ExecutionDescriptor | None,
    ) -> tuple[RunSnapshot | None, AgentSDKError | None]:
        record = result.idempotency
        if record is None:
            return (
                None,
                AgentSDKError(
                    ErrorCode.INTERNAL,
                    "run command result is missing",
                    retryable=False,
                ),
            )
        payload = dict(record.result)
        record = None
        del result
        snapshot: RunSnapshot | None = None
        validation_failed = False
        try:
            snapshot = RunSnapshot.model_validate(payload)
        except Exception:
            validation_failed = True
        finally:
            payload.clear()
        if (
            validation_failed
            or snapshot is None
            or snapshot.session_id != session_id
            or snapshot.agent_revision != agent_revision
            or snapshot.user_input != user_input
            or snapshot.parent_run_id != parent_run_id
            or snapshot.workflow_run_id != workflow_run_id
            or snapshot.workflow_node_id != workflow_node_id
            or snapshot.task_envelope != task_envelope
            or snapshot.execution_compatibility != "current"
            or snapshot.execution_descriptor != execution_descriptor
        ):
            snapshot = None
            del task_envelope
            del execution_descriptor
            return (
                None,
                AgentSDKError(
                    ErrorCode.INTERNAL,
                    "run command result is invalid",
                    retryable=False,
                ),
            )
        return snapshot, None
