from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from agent_sdk.errors import AgentSDKError, ErrorCode
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
        try:
            result = await asyncio.shield(self._task)
        except AgentSDKError:
            raise
        except asyncio.CancelledError as error:
            if not self._task.cancelled():
                raise
            raise self._execution_error() from error
        except Exception as error:
            raise self._execution_error() from error

        snapshot = await self._snapshot()
        if snapshot is not None and snapshot.status in _TERMINAL_STATUSES:
            return result
        raise self._execution_error()

    async def events(self, cursor: int = 0) -> AsyncIterator[StoredEvent]:
        next_cursor = cursor
        while True:
            try:
                stored_events = await self._store.read_events(after_cursor=next_cursor)
            except AgentSDKError:
                raise
            except Exception as error:
                raise self._execution_error() from error
            for stored in stored_events:
                next_cursor = stored.cursor
                if stored.event.run_id != self.run_id:
                    continue
                yield stored
                if stored.event.type in _TERMINAL_EVENTS:
                    return

            if self._task.done():
                snapshot = await self._snapshot()
                if snapshot is not None and snapshot.status in _TERMINAL_STATUSES:
                    return
                self._raise_task_failure()
            await asyncio.sleep(0.01)

    async def _snapshot(self) -> RunSnapshot | None:
        try:
            data = await self._store.get_snapshot("run", self.run_id)
            if data is None:
                return None
            return RunSnapshot.model_validate(data)
        except AgentSDKError:
            raise
        except Exception as error:
            raise self._execution_error() from error

    def _raise_task_failure(self) -> None:
        if self._task.cancelled():
            raise self._execution_error()
        error = self._task.exception()
        if isinstance(error, AgentSDKError):
            raise error
        if error is not None:
            raise self._execution_error() from error
        raise self._execution_error()

    @staticmethod
    def _execution_error() -> AgentSDKError:
        return AgentSDKError(
            ErrorCode.INTERNAL,
            "run execution failed",
            retryable=False,
        )
