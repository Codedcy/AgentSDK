from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

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
        task: asyncio.Task[RunResult] | None,
    ) -> None:
        self.run_id = run_id
        self._store = store
        self._task = task

    @property
    def attached(self) -> bool:
        return self._task is not None

    async def result(self) -> RunResult:
        if self._task is None:
            snapshot = await self._snapshot()
            if snapshot is None:
                raise self._execution_error()
            return self._durable_result(snapshot)

        public_error: AgentSDKError | None = None
        task_cancelled = False
        try:
            await asyncio.shield(self._task)
        except AgentSDKError as error:
            public_error = AgentSDKError(
                error.code,
                error.message,
                retryable=error.retryable,
            )
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise
            task_cancelled = True
        except Exception:
            public_error = self._execution_error()
        if task_cancelled:
            raise self._execution_error() from None
        if public_error is not None:
            raise public_error from None

        snapshot = await self._snapshot()
        if snapshot is not None and snapshot.status in _TERMINAL_STATUSES:
            return self._durable_result(snapshot)
        raise self._execution_error()

    async def events(self, cursor: int = 0) -> AsyncIterator[StoredEvent]:
        if self._task is None:
            up_to_cursor = await self._latest_cursor()
            next_cursor = cursor
            while next_cursor < up_to_cursor:
                stored_events = await self._read_events(
                    next_cursor,
                    up_to_cursor=up_to_cursor,
                    limit=100,
                )
                if not stored_events:
                    return
                for stored in stored_events:
                    next_cursor = stored.cursor
                    if stored.event.run_id == self.run_id:
                        yield stored
                if len(stored_events) < 100:
                    return
            return

        next_cursor = cursor
        while True:
            stored_events = await self._read_events(next_cursor)
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
        data: dict[str, Any] | None = None
        load_failed = False
        try:
            data = await self._store.get_snapshot("run", self.run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            load_failed = True
        if load_failed:
            raise self._execution_error() from None
        if data is None:
            return None
        snapshot: RunSnapshot | None = None
        validation_failed = False
        try:
            snapshot = RunSnapshot.model_validate(data)
        except Exception:
            validation_failed = True
        data = None
        if validation_failed:
            raise self._execution_error() from None
        assert snapshot is not None
        return snapshot

    async def _read_events(
        self,
        after_cursor: int,
        *,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        failed = False
        result: list[StoredEvent] | None = None
        try:
            if up_to_cursor is None and limit is None:
                result = await self._store.read_events(after_cursor=after_cursor)
            else:
                result = await self._store.read_events(
                    after_cursor=after_cursor,
                    up_to_cursor=up_to_cursor,
                    limit=limit,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True
        if failed:
            raise self._execution_error() from None
        assert result is not None
        return result

    async def _latest_cursor(self) -> int:
        failed = False
        cursor = 0
        try:
            cursor = await self._store.latest_cursor()
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True
        if failed:
            raise self._execution_error() from None
        return cursor

    def _raise_task_failure(self) -> None:
        assert self._task is not None
        if self._task.cancelled():
            raise self._execution_error()
        error = self._task.exception()
        if isinstance(error, AgentSDKError):
            public_error = AgentSDKError(
                error.code,
                error.message,
                retryable=error.retryable,
            )
            error = None
            raise public_error from None
        if error is not None:
            error = None
            raise self._execution_error() from None
        raise self._execution_error()

    @staticmethod
    def _durable_result(snapshot: RunSnapshot) -> RunResult:
        if snapshot.status is RunStatus.COMPLETED:
            assert snapshot.output_text is not None
            assert snapshot.usage is not None
            return RunResult(
                run_id=snapshot.run_id,
                output_text=snapshot.output_text,
                usage=snapshot.usage,
                tool_results=snapshot.tool_results,
            )
        if snapshot.status is RunStatus.FAILED:
            failure = snapshot.error
            assert failure is not None
            try:
                code = ErrorCode(failure.code)
            except ValueError:
                code = ErrorCode.INTERNAL
            raise AgentSDKError(
                code,
                failure.message,
                retryable=failure.retryable,
            ) from None
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "recovery required",
            retryable=True,
        )

    @staticmethod
    def _execution_error() -> AgentSDKError:
        return AgentSDKError(
            ErrorCode.INTERNAL,
            "run execution failed",
            retryable=False,
        )
