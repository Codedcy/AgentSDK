from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.storage.base import StateStore, StoredEvent
from agent_sdk.workflow.models import WorkflowResult, WorkflowRunStatus
from agent_sdk.workflow.state import WorkflowState

_TERMINAL_EVENTS = {"workflow.completed", "workflow.failed"}


class WorkflowHandle:
    def __init__(
        self,
        workflow_run_id: str,
        store: StateStore,
        task: asyncio.Task[WorkflowResult],
    ) -> None:
        self.workflow_run_id = workflow_run_id
        self._store = store
        self._task = task

    async def result(self) -> WorkflowResult:
        result = await asyncio.shield(self._task)
        snapshot = await WorkflowState(self._store).load(self.workflow_run_id)
        if snapshot.status is not WorkflowRunStatus.COMPLETED:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "workflow execution did not reach a completed state",
                retryable=False,
            )
        return result

    async def events(self, cursor: int = 0) -> AsyncIterator[StoredEvent]:
        next_cursor = cursor
        completion_observed = False
        while True:
            events = await _read_events(self._store, next_cursor)
            if events is None:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "failed to read workflow events",
                    retryable=False,
                )
            for stored in events:
                next_cursor = stored.cursor
                if stored.event.run_id != self.workflow_run_id:
                    continue
                yield stored
                if stored.event.type in _TERMINAL_EVENTS:
                    return
            if self._task.done():
                if not completion_observed:
                    completion_observed = True
                    continue
                snapshot = await WorkflowState(self._store).load(self.workflow_run_id)
                if snapshot.status in {
                    WorkflowRunStatus.COMPLETED,
                    WorkflowRunStatus.FAILED,
                }:
                    return
                if self._task.cancelled():
                    raise asyncio.CancelledError
                error = self._task.exception()
                if isinstance(error, AgentSDKError):
                    raise error
                if error is not None:
                    raise AgentSDKError(
                        ErrorCode.INTERNAL,
                        "workflow execution failed",
                        retryable=False,
                    ) from None
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "workflow execution ended without terminal state",
                    retryable=False,
                )
            await asyncio.sleep(0.01)


async def _read_events(
    store: StateStore,
    cursor: int,
) -> list[StoredEvent] | None:
    try:
        return await store.read_events(after_cursor=cursor)
    except Exception:
        return None
