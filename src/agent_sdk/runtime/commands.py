from collections.abc import Iterable
from pathlib import Path

from agent_sdk.events.models import EventEnvelope
from agent_sdk.ids import new_id
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot
from agent_sdk.storage.base import CommitBatch, SnapshotWrite, StateStore


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

    async def start_run(
        self,
        session_id: str,
        *,
        agent_revision: str,
        user_input: str,
    ) -> RunSnapshot:
        snapshot = RunSnapshot(
            run_id=new_id("run"),
            session_id=session_id,
            agent_revision=agent_revision,
            status=RunStatus.CREATED,
            user_input=user_input,
        )
        data = snapshot.model_dump(mode="json")
        event = EventEnvelope.new(
            type="run.created",
            session_id=session_id,
            run_id=snapshot.run_id,
            sequence=1,
            payload=data,
        )
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
            )
        )
        return snapshot
