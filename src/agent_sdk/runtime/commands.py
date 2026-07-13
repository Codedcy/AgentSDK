from collections.abc import Iterable
from pathlib import Path

from agent_sdk.events.models import EventEnvelope
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.ids import new_id
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.subagents.models import TaskEnvelope


class RuntimeCommands:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def create_session(self, *, workspaces: Iterable[str | Path]) -> SessionSnapshot:
        snapshot = SessionSnapshot(
            session_id=new_id("ses"),
            workspaces=tuple(str(workspace) for workspace in workspaces),
        )
        data = snapshot.model_dump(mode="json")
        event = EventEnvelope.new(
            type="session.created",
            session_id=snapshot.session_id,
            run_id=None,
            sequence=1,
            payload=data,
        )
        await self._store.commit(
            CommitBatch(
                events=(event,),
                snapshots=(
                    SnapshotWrite(
                        "session",
                        snapshot.session_id,
                        snapshot.session_id,
                        snapshot.version,
                        data,
                    ),
                ),
            )
        )
        return snapshot

    async def delete_session(self, session_id: str) -> None:
        try:
            await self._store.delete_session(session_id)
        except AgentSDKError:
            raise
        except Exception:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to delete session",
                retryable=False,
            ) from None

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
    ) -> RunSnapshot:
        snapshot = RunSnapshot(
            run_id=run_id or new_id("run"),
            session_id=session_id,
            agent_revision=agent_revision,
            status=RunStatus.CREATED,
            user_input=user_input,
            parent_run_id=parent_run_id,
            workflow_run_id=workflow_run_id,
            workflow_node_id=workflow_node_id,
            task_envelope=task_envelope,
        )
        data = snapshot.model_dump(mode="json")
        event = EventEnvelope.new(
            type="run.created",
            session_id=session_id,
            run_id=snapshot.run_id,
            sequence=1,
            payload=data,
        )
        try:
            await self._store.commit(
                CommitBatch(
                    events=(event,),
                    snapshots=(
                        SnapshotWrite(
                            "run",
                            snapshot.run_id,
                            session_id,
                            snapshot.version,
                            data,
                        ),
                    ),
                    preconditions=(SnapshotPrecondition("session", session_id),),
                )
            )
        except SnapshotPreconditionError:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "session not found",
                retryable=False,
            ) from None
        return snapshot
