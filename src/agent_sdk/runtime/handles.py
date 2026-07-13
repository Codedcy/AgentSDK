from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from agent_sdk.runtime.models import RunResult, RunSnapshot, RunStatus
from agent_sdk.storage.base import StateStore, StoredEvent

_TERMINAL_EVENTS = {"run.completed", "run.failed"}
_TERMINAL_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED}


class RunHandle:
    def __init__(
        self,
        run_id: str,
        store: StateStore,
        task: asyncio.Task[RunResult],
    ) -> None:
        self.run_id = run_id
        self._store = store
        self._task = task

    async def result(self) -> RunResult:
        return await asyncio.shield(self._task)

    async def events(self, cursor: int = 0) -> AsyncIterator[StoredEvent]:
        next_cursor = cursor
        while True:
            stored_events = await self._store.read_events(after_cursor=next_cursor)
            matched = False
            for stored in stored_events:
                next_cursor = stored.cursor
                if stored.event.run_id != self.run_id:
                    continue
                matched = True
                yield stored
                if stored.event.type in _TERMINAL_EVENTS:
                    return

            if self._task.done() and not matched:
                data = await self._store.get_snapshot("run", self.run_id)
                if data is not None:
                    snapshot = RunSnapshot.model_validate(data)
                    if snapshot.status in _TERMINAL_STATUSES:
                        return
            await asyncio.sleep(0.01)
