from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
from agent_sdk.permissions.policy import PolicyEngine

_PermissionCallback = Callable[
    [PermissionRequest, PermissionDecision | None], Awaitable[None]
]
_RESOLUTION_HISTORY_LIMIT = 64


@dataclass
class _PendingPermission:
    request: PermissionRequest
    submitted: asyncio.Future[PermissionDecision]
    committed: asyncio.Future[None]
    resolved: bool = False


class InProcessPermissionBridge:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._queues: dict[str, deque[str]] = {}
        self._pending: dict[str, _PendingPermission] = {}
        self._resolved_history: OrderedDict[str, None] = OrderedDict()

    async def wait(self, request: PermissionRequest) -> PermissionDecision:
        loop = asyncio.get_running_loop()
        pending = _PendingPermission(
            request=request,
            submitted=loop.create_future(),
            committed=loop.create_future(),
        )
        pending.committed.add_done_callback(_consume_future_error)
        async with self._condition:
            self._pending[request.request_id] = pending
            self._queues.setdefault(request.run_id, deque()).append(
                request.request_id
            )
            self._condition.notify_all()
        return await pending.submitted

    async def next_request(self, run_id: str) -> PermissionRequest:
        async with self._condition:
            while True:
                queue = self._queues.get(run_id)
                while queue:
                    request_id = queue.popleft()
                    if not queue:
                        self._queues.pop(run_id, None)
                    pending = self._pending.get(request_id)
                    if pending is not None:
                        return pending.request.model_copy(deep=True)
                await self._condition.wait()

    async def resolve(
        self,
        request_id: str,
        decision: PermissionDecision,
    ) -> None:
        async with self._condition:
            if request_id in self._resolved_history:
                raise AgentSDKError(
                    ErrorCode.CONFLICT,
                    "permission request already resolved",
                    retryable=False,
                )
            pending = self._pending.get(request_id)
            if pending is None:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "permission request not found",
                    retryable=False,
                )
            if pending.resolved:
                raise AgentSDKError(
                    ErrorCode.CONFLICT,
                    "permission request already resolved",
                    retryable=False,
                )
            if decision.action == "ask":
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "permission resolution must allow or deny",
                    retryable=False,
                )
            pending.resolved = True
            pending.submitted.set_result(decision)
            committed = pending.committed
        await asyncio.shield(committed)

    async def mark_committed(self, request_id: str) -> None:
        async with self._condition:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                return
            self._remember_resolution(request_id)
            self._remove_from_queue(pending.request.run_id, request_id)
            if not pending.committed.done():
                pending.committed.set_result(None)

    async def mark_failed(
        self,
        request_id: str,
        error: AgentSDKError,
    ) -> None:
        async with self._condition:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                return
            self._remember_resolution(request_id)
            self._remove_from_queue(pending.request.run_id, request_id)
            if not pending.committed.done():
                pending.committed.set_exception(error)
            self._condition.notify_all()

    async def cancel(self, request_id: str) -> None:
        async with self._condition:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                return
            self._remove_from_queue(pending.request.run_id, request_id)
            if not pending.submitted.done():
                pending.submitted.cancel()
            if not pending.committed.done():
                pending.committed.cancel()
            self._condition.notify_all()

    async def cancel_run(self, run_id: str) -> None:
        async with self._condition:
            request_ids = [
                request_id
                for request_id, pending in self._pending.items()
                if pending.request.run_id == run_id
            ]
            for request_id in request_ids:
                pending = self._pending.pop(request_id)
                if not pending.submitted.done():
                    pending.submitted.cancel()
                if not pending.committed.done():
                    pending.committed.cancel()
            self._queues.pop(run_id, None)
            self._condition.notify_all()

    def _remove_from_queue(self, run_id: str, request_id: str) -> None:
        queue = self._queues.get(run_id)
        if queue is None:
            return
        try:
            queue.remove(request_id)
        except ValueError:
            pass
        if not queue:
            self._queues.pop(run_id, None)

    def _remember_resolution(self, request_id: str) -> None:
        self._resolved_history[request_id] = None
        self._resolved_history.move_to_end(request_id)
        if len(self._resolved_history) > _RESOLUTION_HISTORY_LIMIT:
            self._resolved_history.popitem(last=False)


class PermissionBroker:
    def __init__(
        self,
        policy: PolicyEngine,
        bridge: InProcessPermissionBridge | None,
    ) -> None:
        self._policy = policy
        self._bridge = bridge

    async def authorize(
        self,
        request: PermissionRequest,
        *,
        on_requested: _PermissionCallback,
        on_resolved: _PermissionCallback,
    ) -> PermissionDecision:
        decision = self._policy.evaluate(request)
        if decision.action != "ask":
            return decision
        if self._bridge is None:
            return PermissionDecision.deny("permission bridge unavailable")

        await on_requested(request, None)
        try:
            decision = await self._bridge.wait(request)
            await on_resolved(request, decision)
            await self._bridge.mark_committed(request.request_id)
            return decision
        except asyncio.CancelledError:
            await asyncio.shield(self._bridge.cancel(request.request_id))
            raise
        except Exception as cause:
            failure = AgentSDKError(
                ErrorCode.INTERNAL,
                "permission resolution failed",
                retryable=False,
            )
            await asyncio.shield(
                self._bridge.mark_failed(request.request_id, failure)
            )
            raise failure from cause


def _consume_future_error(future: asyncio.Future[None]) -> None:
    if not future.cancelled():
        future.exception()
