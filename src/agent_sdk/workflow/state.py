from __future__ import annotations

from enum import Enum

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.ids import new_id
from agent_sdk.runtime.models import RunResult, TokenUsage
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    SnapshotWrite,
    StateStore,
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


class _CommitFailure(Enum):
    PRECONDITION = "precondition"
    STORE = "store"


class WorkflowState:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def create(self, session_id: str, workflow: WorkflowIR) -> WorkflowRunSnapshot:
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
        )
        event = EventEnvelope.new(
            type="workflow.started",
            session_id=session_id,
            run_id=workflow_run_id,
            sequence=1,
            payload={
                "definition_hash": workflow.definition_hash,
                "name": workflow.name,
            },
        )
        writes = (
            _workflow_write(snapshot),
            *(_node_write(node) for node in nodes),
        )
        await self._commit(
            CommitBatch(
                events=(event,),
                snapshots=writes,
                preconditions=(SnapshotPrecondition("session", session_id),),
            ),
            session_id,
            conflict_message="workflow already exists",
        )
        return snapshot

    async def load(self, workflow_run_id: str) -> WorkflowRunSnapshot:
        result = await _load_workflow(self._store, workflow_run_id)
        if result is _LoadFailure.MISSING:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "workflow run not found",
                retryable=False,
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
        )

    async def fail_node(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        failure: WorkflowFailure,
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
                    SnapshotPrecondition("session", snapshot.session_id),
                    SnapshotPrecondition(
                        "workflow", snapshot.workflow_run_id, snapshot.version
                    ),
                    SnapshotPrecondition(
                        "workflow_node", previous_node.entity_id, previous_node.version
                    ),
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
        event = EventEnvelope.new(
            type=event_type,
            session_id=previous.session_id,
            run_id=previous.workflow_run_id,
            sequence=updated.version,
            payload=payload,
        )
        await self._commit(
            CommitBatch(
                events=(event,),
                snapshots=(_workflow_write(updated),),
                preconditions=(
                    SnapshotPrecondition("session", previous.session_id),
                    SnapshotPrecondition(
                        "workflow", previous.workflow_run_id, previous.version
                    ),
                ),
            ),
            previous.session_id,
            conflict_message="workflow state changed concurrently",
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
    try:
        data = await store.get_snapshot("workflow", workflow_run_id)
    except Exception:
        return _LoadFailure.STORE
    if data is None:
        return _LoadFailure.MISSING
    try:
        return WorkflowRunSnapshot.model_validate(data)
    except Exception:
        return _LoadFailure.INVALID


async def _commit_batch(
    store: StateStore,
    batch: CommitBatch,
) -> _CommitFailure | None:
    try:
        await store.commit(batch)
        return None
    except SnapshotPreconditionError:
        return _CommitFailure.PRECONDITION
    except Exception:
        return _CommitFailure.STORE


async def _session_exists(store: StateStore, session_id: str) -> bool | None:
    try:
        return await store.get_snapshot("session", session_id) is not None
    except Exception:
        return None


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


def _node_write(snapshot: WorkflowNodeSnapshot) -> SnapshotWrite:
    return SnapshotWrite(
        "workflow_node",
        snapshot.entity_id,
        snapshot.session_id,
        snapshot.version,
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
