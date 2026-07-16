from __future__ import annotations

import asyncio
from enum import Enum

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.ids import new_id
from agent_sdk.runtime.commands import CommandOutcome
from agent_sdk.runtime.execution import WorkflowExecutionDescriptor
from agent_sdk.runtime.models import RunResult, SessionStatus, TokenUsage
from agent_sdk.runtime.session_lifecycle import (
    detach_workflow_transition,
    exact_session_precondition,
    load_session,
    session_write,
)
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.storage.idempotency import (
    IdempotencyError,
    IdempotencyReplay,
    IdempotencyReplayMissError,
    IdempotencyWrite,
    fingerprint_command,
    validate_replay,
)
from agent_sdk.workflow.models import (
    WorkflowFailure,
    WorkflowIR,
    WorkflowNodeSnapshot,
    WorkflowNodeStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)


class _LoadFailure(Enum):
    MISSING = "missing"
    INVALID = "invalid"
    STORE = "store"
    UNSTABLE = "unstable"


class _CommitFailure(Enum):
    PRECONDITION = "precondition"
    STORE = "store"


class WorkflowState:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def create(
        self,
        session_id: str,
        workflow: WorkflowIR,
        *,
        execution_descriptor: WorkflowExecutionDescriptor | None = None,
        idempotency_key: str | None = None,
    ) -> CommandOutcome[WorkflowRunSnapshot]:
        if execution_descriptor is None and idempotency_key is not None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "legacy workflow cannot use idempotency",
                retryable=False,
            ) from None
        scope = f"session/{session_id}/workflow.start"
        if idempotency_key is not None:
            validate_replay(
                IdempotencyReplay(scope, idempotency_key, "0" * 64)
            )
        workflow_run_id = new_id("wfr")
        nodes = tuple(
            WorkflowNodeSnapshot(
                entity_id=_node_entity_id(workflow_run_id, node.id),
                workflow_run_id=workflow_run_id,
                session_id=session_id,
                node_id=node.id,
                status=WorkflowNodeStatus.PENDING,
            )
            for node in workflow.nodes
        )
        snapshot = WorkflowRunSnapshot(
            workflow_run_id=workflow_run_id,
            session_id=session_id,
            status=WorkflowRunStatus.RUNNING,
            workflow=workflow,
            nodes=nodes,
            execution_compatibility=(
                "current" if execution_descriptor is not None else "legacy_unknown"
            ),
            execution_descriptor=execution_descriptor,
        )
        snapshot_data = snapshot.model_dump(mode="json")
        fingerprint: str | None = None
        if idempotency_key is not None:
            fingerprint = fingerprint_command(
                "workflow.start",
                {
                    "session_id": session_id,
                    "workflow": workflow.model_dump(mode="json"),
                    "execution_descriptor": (
                        None
                        if execution_descriptor is None
                        else execution_descriptor.model_dump(mode="json")
                    ),
                },
            )
        workflow_event = EventEnvelope.new(
            type="workflow.started",
            session_id=session_id,
            run_id=workflow_run_id,
            sequence=1,
            payload={
                "definition_hash": workflow.definition_hash,
                "name": workflow.name,
            },
        )
        for attempt in range(8):
            session = await load_session(self._store, session_id)
            if session.status is SessionStatus.DELETING:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "session is deleting",
                    retryable=False,
                )
            has_hint = False
            if idempotency_key is not None:
                hint = await _get_idempotency(
                    self._store,
                    scope,
                    idempotency_key,
                )
                has_hint = hint is not None
                hint = None

            request: IdempotencyWrite | IdempotencyReplay | None
            if has_hint:
                assert idempotency_key is not None
                assert fingerprint is not None
                request = IdempotencyReplay(scope, idempotency_key, fingerprint)
                batch = CommitBatch(
                    events=(),
                    idempotency=request,
                    replay_preconditions=(exact_session_precondition(session),),
                )
            elif session.status is not SessionStatus.ACTIVE:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "session is not active",
                    retryable=False,
                )
            else:
                updated_session = session.model_copy(
                    update={
                        "active_workflow_run_ids": tuple(
                            sorted((*session.active_workflow_run_ids, workflow_run_id))
                        ),
                        "version": session.version + 1,
                    }
                )
                session_event = EventEnvelope.new(
                    type="session.workflow.attached",
                    session_id=session_id,
                    run_id=None,
                    sequence=updated_session.version,
                    payload={"workflow_run_id": workflow_run_id},
                )
                request = None
                if idempotency_key is not None:
                    assert fingerprint is not None
                    request = IdempotencyWrite(
                        scope=scope,
                        key=idempotency_key,
                        request_fingerprint=fingerprint,
                        session_id=session_id,
                        result=snapshot_data,
                    )
                session_precondition = exact_session_precondition(session)
                batch = CommitBatch(
                    events=(session_event, workflow_event),
                    snapshots=(
                        session_write(updated_session),
                        _workflow_write(snapshot),
                        *(_node_write(node) for node in nodes),
                    ),
                    preconditions=(session_precondition,),
                    idempotency=request,
                    replay_preconditions=(
                        (session_precondition,) if request is not None else ()
                    ),
                )
            result: CommitResult | None = None
            store_failed = False
            try:
                result = await self._store.commit(batch)
            except (SnapshotPreconditionError, IdempotencyReplayMissError):
                if attempt + 1 < 8:
                    await asyncio.sleep(0)
                continue
            except IdempotencyError:
                raise
            except Exception:
                store_failed = True
            if store_failed:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "failed to persist workflow state",
                    retryable=False,
                ) from None
            assert result is not None
            if request is None:
                return CommandOutcome(snapshot, replayed=False)
            replayed = not result.applied
            stored, validation_error = _validated_workflow_result(
                result,
                session_id=session_id,
                expected_workflow=workflow,
                expected_execution_descriptor=execution_descriptor,
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

    async def load(self, workflow_run_id: str) -> WorkflowRunSnapshot:
        result = await _load_workflow(self._store, workflow_run_id)
        if result is _LoadFailure.MISSING:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "workflow run not found",
                retryable=False,
            )
        if result is _LoadFailure.UNSTABLE:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "workflow state changed during load",
                retryable=True,
            )
        if isinstance(result, _LoadFailure):
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to load workflow run",
                retryable=False,
            )
        return result

    async def start_node(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        run_id: str,
    ) -> WorkflowRunSnapshot:
        current = snapshot.nodes[index]
        if current.status is not WorkflowNodeStatus.PENDING:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "workflow node is not pending",
                retryable=False,
            )
        node = current.model_copy(
            update={
                "status": WorkflowNodeStatus.RUNNING,
                "run_id": run_id,
                "version": current.version + 1,
            }
        )
        return await self._node_transition(
            snapshot,
            index,
            node,
            "workflow.node.started",
            {"node_id": node.node_id, "run_id": run_id},
        )

    async def complete_node(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        result: RunResult,
        *,
        related_preconditions: tuple[SnapshotPrecondition, ...] = (),
    ) -> WorkflowRunSnapshot:
        current = snapshot.nodes[index]
        node = current.model_copy(
            update={
                "status": WorkflowNodeStatus.COMPLETED,
                "output_text": result.output_text,
                "usage": result.usage,
                "version": current.version + 1,
            }
        )
        return await self._node_transition(
            snapshot,
            index,
            node,
            "workflow.node.completed",
            {
                "node_id": node.node_id,
                "run_id": node.run_id,
                "output_text": result.output_text,
                "usage": result.usage.model_dump(mode="json"),
            },
            related_preconditions=related_preconditions,
        )

    async def fail_node(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        failure: WorkflowFailure,
        *,
        related_preconditions: tuple[SnapshotPrecondition, ...] = (),
    ) -> WorkflowRunSnapshot:
        current = snapshot.nodes[index]
        node = current.model_copy(
            update={
                "status": WorkflowNodeStatus.FAILED,
                "error": failure,
                "version": current.version + 1,
            }
        )
        return await self._node_transition(
            snapshot,
            index,
            node,
            "workflow.node.failed",
            {"node_id": node.node_id, "run_id": node.run_id, "error": failure.model_dump()},
            related_preconditions=related_preconditions,
        )

    async def complete_workflow(
        self,
        snapshot: WorkflowRunSnapshot,
    ) -> WorkflowRunSnapshot:
        output_text = snapshot.nodes[-1].output_text or ""
        usage = _sum_usage(snapshot.nodes)
        completed = snapshot.model_copy(
            update={
                "status": WorkflowRunStatus.COMPLETED,
                "version": snapshot.version + 1,
                "output_text": output_text,
                "usage": usage,
            }
        )
        await self._workflow_transition(
            snapshot,
            completed,
            "workflow.completed",
            {"output_text": output_text, "usage": usage.model_dump(mode="json")},
        )
        return completed

    async def fail_workflow(
        self,
        snapshot: WorkflowRunSnapshot,
        failure: WorkflowFailure,
    ) -> WorkflowRunSnapshot:
        failed = snapshot.model_copy(
            update={
                "status": WorkflowRunStatus.FAILED,
                "version": snapshot.version + 1,
                "error": failure,
            }
        )
        await self._workflow_transition(
            snapshot,
            failed,
            "workflow.failed",
            {"error": failure.model_dump(mode="json")},
        )
        return failed

    async def _node_transition(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        node: WorkflowNodeSnapshot,
        event_type: str,
        payload: dict[str, object],
        *,
        related_preconditions: tuple[SnapshotPrecondition, ...] = (),
    ) -> WorkflowRunSnapshot:
        nodes = list(snapshot.nodes)
        previous_node = nodes[index]
        nodes[index] = node
        updated = snapshot.model_copy(
            update={"nodes": tuple(nodes), "version": snapshot.version + 1}
        )
        event = EventEnvelope.new(
            type=event_type,
            session_id=snapshot.session_id,
            run_id=snapshot.workflow_run_id,
            sequence=updated.version,
            payload=payload,
        )
        await self._commit(
            CommitBatch(
                events=(event,),
                snapshots=(_workflow_write(updated), _node_write(node)),
                preconditions=(
                    _exact_workflow_precondition(snapshot),
                    _exact_node_precondition(previous_node),
                    *related_preconditions,
                ),
            ),
            snapshot.session_id,
            conflict_message="workflow state changed concurrently",
        )
        return updated

    async def _workflow_transition(
        self,
        previous: WorkflowRunSnapshot,
        updated: WorkflowRunSnapshot,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        workflow_event = EventEnvelope.new(
            type=event_type,
            session_id=previous.session_id,
            run_id=previous.workflow_run_id,
            sequence=updated.version,
            payload=payload,
        )
        for attempt in range(8):
            session = await load_session(self._store, previous.session_id)
            updated_session, session_event_type = detach_workflow_transition(
                session,
                previous.workflow_run_id,
            )
            session_event = EventEnvelope.new(
                type=session_event_type,
                session_id=previous.session_id,
                run_id=None,
                sequence=updated_session.version,
                payload={
                    "workflow_run_id": previous.workflow_run_id,
                    "status": updated_session.status.value,
                },
            )
            result = await _commit_batch(
                self._store,
                CommitBatch(
                    events=(workflow_event, session_event),
                    snapshots=(
                        _workflow_write(updated),
                        session_write(updated_session),
                    ),
                    preconditions=(
                        _exact_workflow_precondition(previous),
                        exact_session_precondition(session),
                    ),
                ),
            )
            if result is None:
                return
            if result is _CommitFailure.PRECONDITION:
                current = await self.load(previous.workflow_run_id)
                if current != previous:
                    raise AgentSDKError(
                        ErrorCode.CONFLICT,
                        "workflow state changed concurrently",
                        retryable=True,
                    ) from None
                if attempt + 1 < 8:
                    await asyncio.sleep(0)
                continue
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to persist workflow state",
                retryable=False,
            )
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "session state changed concurrently",
            retryable=True,
        )

    async def _commit(
        self,
        batch: CommitBatch,
        session_id: str,
        *,
        conflict_message: str,
    ) -> None:
        result = await _commit_batch(self._store, batch)
        if result is None:
            return
        if result is _CommitFailure.PRECONDITION:
            session_exists = await _session_exists(self._store, session_id)
            if session_exists is False:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "workflow session no longer exists",
                    retryable=False,
                )
            if session_exists is True:
                raise AgentSDKError(
                    ErrorCode.CONFLICT,
                    conflict_message,
                    retryable=False,
                )
        raise AgentSDKError(
            ErrorCode.INTERNAL,
            "failed to persist workflow state",
            retryable=False,
        )


async def _load_workflow(
    store: StateStore,
    workflow_run_id: str,
) -> WorkflowRunSnapshot | _LoadFailure:
    for _ in range(4):
        try:
            before = await store.get_snapshot("workflow", workflow_run_id)
        except Exception:
            return _LoadFailure.STORE
        if before is None:
            return _LoadFailure.MISSING
        try:
            workflow = WorkflowRunSnapshot.model_validate(before)
        except Exception:
            return _LoadFailure.INVALID
        nodes_match = True
        for expected_node in workflow.nodes:
            try:
                node_data = await store.get_snapshot(
                    "workflow_node", expected_node.entity_id
                )
            except Exception:
                return _LoadFailure.STORE
            if node_data is None:
                nodes_match = False
                break
            try:
                stored_node = WorkflowNodeSnapshot.model_validate(node_data)
            except Exception:
                nodes_match = False
                break
            if stored_node != expected_node:
                nodes_match = False
                break
        try:
            after = await store.get_snapshot("workflow", workflow_run_id)
        except Exception:
            return _LoadFailure.STORE
        if before != after:
            continue
        if not nodes_match:
            return _LoadFailure.INVALID
        return workflow
    return _LoadFailure.UNSTABLE


async def _commit_batch(
    store: StateStore,
    batch: CommitBatch,
) -> _CommitFailure | None:
    try:
        await store.commit(batch)
        return None
    except IdempotencyError:
        raise
    except SnapshotPreconditionError:
        return _CommitFailure.PRECONDITION
    except Exception:
        return _CommitFailure.STORE


async def _session_exists(store: StateStore, session_id: str) -> bool | None:
    try:
        return await store.get_snapshot("session", session_id) is not None
    except Exception:
        return None


async def _get_idempotency(
    store: StateStore,
    scope: str,
    key: str,
) -> object | None:
    result: object | None = None
    store_failed = False
    try:
        result = await store.get_idempotency(scope, key)
    except IdempotencyError:
        raise
    except Exception:
        store_failed = True
    if store_failed:
        raise AgentSDKError(
            ErrorCode.INTERNAL,
            "failed to persist workflow state",
            retryable=False,
        ) from None
    return result


def _validated_workflow_result(
    result: CommitResult,
    *,
    session_id: str,
    expected_workflow: WorkflowIR,
    expected_execution_descriptor: WorkflowExecutionDescriptor | None,
) -> tuple[WorkflowRunSnapshot | None, AgentSDKError | None]:
    record = result.idempotency
    if record is None:
        return (
            None,
            AgentSDKError(
                ErrorCode.INTERNAL,
                "workflow command result is missing",
                retryable=False,
            ),
        )
    payload = dict(record.result)
    record = None
    del result
    snapshot: WorkflowRunSnapshot | None = None
    validation_failed = False
    try:
        snapshot = WorkflowRunSnapshot.model_validate(payload)
    except Exception:
        validation_failed = True
    finally:
        payload.clear()
    if (
        validation_failed
        or snapshot is None
        or snapshot.session_id != session_id
        or snapshot.workflow != expected_workflow
        or snapshot.execution_compatibility != "current"
        or snapshot.execution_descriptor != expected_execution_descriptor
    ):
        snapshot = None
        del expected_workflow
        del expected_execution_descriptor
        return (
            None,
            AgentSDKError(
                ErrorCode.INTERNAL,
                "workflow command result is invalid",
                retryable=False,
            ),
        )
    return snapshot, None


def _node_entity_id(workflow_run_id: str, node_id: str) -> str:
    return f"{workflow_run_id}:{node_id}"


def _workflow_write(snapshot: WorkflowRunSnapshot) -> SnapshotWrite:
    return SnapshotWrite(
        "workflow",
        snapshot.workflow_run_id,
        snapshot.session_id,
        snapshot.version,
        snapshot.model_dump(mode="json"),
    )


def _exact_workflow_precondition(
    snapshot: WorkflowRunSnapshot,
) -> SnapshotPrecondition:
    return SnapshotPrecondition(
        "workflow",
        snapshot.workflow_run_id,
        snapshot.version,
        snapshot.session_id,
        snapshot.model_dump(mode="json"),
    )


def _node_write(snapshot: WorkflowNodeSnapshot) -> SnapshotWrite:
    return SnapshotWrite(
        "workflow_node",
        snapshot.entity_id,
        snapshot.session_id,
        snapshot.version,
        snapshot.model_dump(mode="json"),
    )


def _exact_node_precondition(
    snapshot: WorkflowNodeSnapshot,
) -> SnapshotPrecondition:
    return SnapshotPrecondition(
        "workflow_node",
        snapshot.entity_id,
        snapshot.version,
        snapshot.session_id,
        snapshot.model_dump(mode="json"),
    )


def _sum_usage(nodes: tuple[WorkflowNodeSnapshot, ...]) -> TokenUsage:
    prompt = completion = total = 0
    prompt_known = completion_known = total_known = False
    for node in nodes:
        if node.usage is None:
            continue
        if node.usage.prompt_tokens is not None:
            prompt += node.usage.prompt_tokens
            prompt_known = True
        if node.usage.completion_tokens is not None:
            completion += node.usage.completion_tokens
            completion_known = True
        if node.usage.total_tokens is not None:
            total += node.usage.total_tokens
            total_known = True
    return TokenUsage(
        prompt_tokens=prompt if prompt_known else None,
        completion_tokens=completion if completion_known else None,
        total_tokens=total if total_known else None,
    )
