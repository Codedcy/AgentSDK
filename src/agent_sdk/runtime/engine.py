from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import suppress
from typing import Any

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.models.litellm_gateway import (
    LiteLLMGateway,
    ModelCompleted,
    ModelRequest,
    TextDelta,
    ToolCallCompleted,
    UsageReported,
)
from agent_sdk.permissions.broker import InProcessPermissionBridge
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.runtime.models import RunResult, RunSnapshot, RunStatus, TokenUsage
from agent_sdk.storage.base import CommitBatch, SnapshotWrite, StateStore
from agent_sdk.tools.executor import ToolExecutor
from agent_sdk.tools.models import ToolContext, ToolResult
from agent_sdk.tools.registry import ToolRegistry

_DELTA_FLUSH_SECONDS = 0.05
_DELTA_FLUSH_BYTES = 4 * 1024


class _RunEmitter:
    def __init__(self, store: StateStore, run: RunSnapshot) -> None:
        self._store = store
        self._run = run
        self._sequence = 2
        self._lock = asyncio.Lock()
        self._delta_parts: list[str] = []
        self._delta_bytes = 0
        self._timer: asyncio.Task[None] | None = None
        self._timer_error: BaseException | None = None

    @property
    def current_snapshot(self) -> RunSnapshot:
        return self._run

    async def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        snapshot: RunSnapshot | None = None,
    ) -> None:
        async with self._lock:
            self._raise_timer_error()
            await self._emit_locked(event_type, payload or {}, snapshot=snapshot)

    async def transition(
        self,
        event_type: str,
        status: RunStatus,
        payload: dict[str, Any],
        *,
        update: dict[str, Any] | None = None,
    ) -> RunSnapshot:
        async with self._lock:
            self._raise_timer_error()
            values: dict[str, Any] = {
                "status": status,
                "version": self._run.version + 1,
            }
            if update is not None:
                values.update(update)
            snapshot = self._run.model_copy(update=values)
            await self._emit_locked(event_type, payload, snapshot=snapshot)
            return snapshot

    async def add_delta(self, text: str) -> None:
        cancelled_timer: asyncio.Task[None] | None = None
        async with self._lock:
            self._raise_timer_error()
            self._delta_parts.append(text)
            self._delta_bytes += len(text.encode("utf-8"))
            if self._timer is None:
                self._timer = asyncio.create_task(self._flush_after_delay())
                self._timer.add_done_callback(self._timer_finished)
            if self._delta_bytes >= _DELTA_FLUSH_BYTES:
                cancelled_timer = self._detach_timer_locked()
                await self._flush_delta_locked()
        await self._settle_cancelled_timer(cancelled_timer)

    async def flush_delta(self) -> None:
        cancelled_timer: asyncio.Task[None] | None = None
        async with self._lock:
            self._raise_timer_error()
            cancelled_timer = self._detach_timer_locked()
            await self._flush_delta_locked()
        await self._settle_cancelled_timer(cancelled_timer)

    async def close(self) -> None:
        await self.flush_delta()

    async def _flush_after_delay(self) -> None:
        await asyncio.sleep(_DELTA_FLUSH_SECONDS)
        async with self._lock:
            if self._timer is asyncio.current_task():
                self._timer = None
            await self._flush_delta_locked()

    def _timer_finished(self, timer: asyncio.Task[None]) -> None:
        if timer.cancelled():
            return
        error = timer.exception()
        if error is not None:
            self._timer_error = error

    def _raise_timer_error(self) -> None:
        if self._timer_error is not None:
            error = self._timer_error
            self._timer_error = None
            raise error

    def _detach_timer_locked(self) -> asyncio.Task[None] | None:
        timer = self._timer
        self._timer = None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            return timer
        return None

    @staticmethod
    async def _settle_cancelled_timer(timer: asyncio.Task[None] | None) -> None:
        if timer is None:
            return
        with suppress(asyncio.CancelledError):
            await timer

    async def _flush_delta_locked(self) -> None:
        if not self._delta_parts:
            return
        text = "".join(self._delta_parts)
        await self._emit_locked("model.text.delta", {"text": text})
        self._delta_parts.clear()
        self._delta_bytes = 0

    async def _emit_locked(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        snapshot: RunSnapshot | None = None,
    ) -> None:
        event = EventEnvelope.new(
            type=event_type,
            session_id=self._run.session_id,
            run_id=self._run.run_id,
            sequence=self._sequence,
            payload=payload,
        )
        snapshots: tuple[SnapshotWrite, ...] = ()
        if snapshot is not None:
            snapshots = (
                SnapshotWrite(
                    "run",
                    snapshot.run_id,
                    snapshot.session_id,
                    snapshot.version,
                    snapshot.model_dump(mode="json"),
                ),
            )
        await self._store.commit(CommitBatch(events=(event,), snapshots=snapshots))
        if snapshot is not None:
            self._run = snapshot
        self._sequence += 1


class RunEngine:
    def __init__(
        self,
        store: StateStore,
        models: LiteLLMGateway,
        tools: ToolRegistry | None = None,
        policy: PolicyEngine | None = None,
        permission_bridge: InProcessPermissionBridge | None = None,
    ) -> None:
        self._store = store
        self._models = models
        self._tools = tools or ToolRegistry()
        self._policy = policy or PolicyEngine()
        self._permission_bridge = permission_bridge

    async def execute(self, run_id: str, request: ModelRequest) -> RunResult:
        created = await self._load_created_run(run_id)
        emitter = _RunEmitter(self._store, created)
        await emitter.transition(
            "run.started",
            RunStatus.RUNNING,
            {"status": RunStatus.RUNNING.value},
        )
        chunks: list[str] = []
        usage = TokenUsage()
        tool_results: list[ToolResult] = []
        messages = deepcopy(list(request.messages))
        executor = ToolExecutor(
            self._tools,
            self._policy,
            self._permission_bridge,
        )
        try:
            while True:
                await emitter.emit("step.started")
                await emitter.emit("model.call.started", {"model": request.model})
                step_chunks: list[str] = []
                calls: list[ToolCallCompleted] = []
                model_request = ModelRequest(
                    model=request.model,
                    messages=tuple(deepcopy(messages)),
                    tools=request.tools,
                    params=dict(request.params),
                )
                try:
                    async for event in self._models.stream(model_request):
                        if isinstance(event, TextDelta):
                            chunks.append(event.text)
                            step_chunks.append(event.text)
                            await emitter.add_delta(event.text)
                        elif isinstance(event, ToolCallCompleted):
                            calls.append(event)
                        elif isinstance(event, UsageReported):
                            await emitter.flush_delta()
                            usage = _add_usage(usage, event.to_usage())
                            await emitter.emit(
                                "model.usage.reported",
                                event.to_payload(),
                            )
                        elif isinstance(event, ModelCompleted):
                            await emitter.flush_delta()
                            await emitter.emit(
                                "model.call.completed",
                                event.to_payload(),
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as cause:
                    failure = AgentSDKError(
                        ErrorCode.INTERNAL,
                        "model call failed",
                        retryable=False,
                    )
                    await emitter.flush_delta()
                    await self._fail_run(
                        emitter,
                        failure,
                        chunks,
                        usage,
                        tool_results,
                        model_call_failed=True,
                    )
                    await emitter.close()
                    raise failure from cause

                if len(calls) > 1:
                    failure = AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "multiple tool calls are not supported",
                        retryable=False,
                    )
                    await self._fail_run(
                        emitter,
                        failure,
                        chunks,
                        usage,
                        tool_results,
                    )
                    await emitter.close()
                    raise failure

                if calls and tool_results:
                    failure = AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "additional tool calls are not supported",
                        retryable=False,
                    )
                    await self._fail_run(
                        emitter,
                        failure,
                        chunks,
                        usage,
                        tool_results,
                    )
                    await emitter.close()
                    raise failure

                if not calls:
                    await emitter.emit("step.completed")
                    break

                call = calls[0]
                await emitter.emit(
                    "tool.call.proposed",
                    {"call_id": call.call_id, "tool_name": call.name},
                )
                try:
                    tool_result = await executor.execute(
                        call,
                        ToolContext(
                            run_id=run_id,
                            session_id=emitter.current_snapshot.session_id,
                        ),
                        emit=emitter.emit,
                        on_permission_requested=lambda permission, decision: (
                            self._permission_transition(
                                emitter,
                                "permission.requested",
                                RunStatus.WAITING_PERMISSION,
                                permission,
                                decision,
                            )
                        ),
                        on_permission_resolved=lambda permission, decision: (
                            self._permission_transition(
                                emitter,
                                "permission.resolved",
                                RunStatus.RUNNING,
                                permission,
                                decision,
                            )
                        ),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as cause:
                    failure = (
                        cause
                        if isinstance(cause, AgentSDKError)
                        else AgentSDKError(
                            ErrorCode.INTERNAL,
                            "tool execution failed",
                            retryable=False,
                        )
                    )
                    try:
                        await self._fail_run(
                            emitter,
                            failure,
                            chunks,
                            usage,
                            tool_results,
                        )
                    except Exception:
                        pass
                    try:
                        await emitter.close()
                    except Exception:
                        pass
                    if failure is cause:
                        raise failure
                    raise failure from cause
                tool_results.append(tool_result)
                await emitter.emit("step.completed")
                messages.append(
                    {
                        "role": "assistant",
                        "content": "".join(step_chunks) or None,
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": call.arguments_json,
                                },
                            }
                        ],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "name": call.name,
                        "content": tool_result.content,
                    }
                )
        except asyncio.CancelledError:
            if self._permission_bridge is not None:
                await asyncio.shield(self._permission_bridge.cancel_run(run_id))
            await emitter.close()
            raise

        await emitter.flush_delta()
        output_text = "".join(chunks)
        run_result = RunResult(
            run_id=run_id,
            output_text=output_text,
            usage=usage,
            tool_results=tuple(tool_results),
        )
        terminal_payload: dict[str, Any] = {
            "output_text": output_text,
            "usage": usage.model_dump(),
        }
        if tool_results:
            terminal_payload["tool_results"] = [
                tool_result.model_dump(mode="json")
                for tool_result in tool_results
            ]
        await emitter.transition(
            "run.completed",
            RunStatus.COMPLETED,
            terminal_payload,
            update={
                "output_text": output_text,
                "usage": usage,
            },
        )
        await emitter.close()
        return run_result

    @staticmethod
    async def _permission_transition(
        emitter: _RunEmitter,
        event_type: str,
        status: RunStatus,
        request: PermissionRequest,
        decision: PermissionDecision | None,
    ) -> None:
        payload: dict[str, Any] = {
            "request": request.model_dump(mode="json"),
        }
        if decision is not None:
            payload["decision"] = decision.model_dump(mode="json")
        await emitter.transition(event_type, status, payload)

    @staticmethod
    async def _fail_run(
        emitter: _RunEmitter,
        failure: AgentSDKError,
        chunks: list[str],
        usage: TokenUsage,
        tool_results: list[ToolResult],
        *,
        model_call_failed: bool = False,
    ) -> None:
        payload = {"error": failure.to_dict()}
        if model_call_failed:
            await emitter.emit("model.call.failed", payload)
        await emitter.emit("step.failed", payload)
        await emitter.transition(
            "run.failed",
            RunStatus.FAILED,
            payload,
            update={
                "output_text": "".join(chunks),
                "usage": usage,
            },
        )

    async def _load_created_run(self, run_id: str) -> RunSnapshot:
        data = await self._store.get_snapshot("run", run_id)
        if data is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "run not found",
                retryable=False,
            )
        run = RunSnapshot.model_validate(data)
        if run.status is not RunStatus.CREATED:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not ready to start",
                retryable=False,
            )
        return run


def _add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    def add(first: int | None, second: int | None) -> int | None:
        if first is None:
            return second
        if second is None:
            return first
        return first + second

    return TokenUsage(
        prompt_tokens=add(left.prompt_tokens, right.prompt_tokens),
        completion_tokens=add(left.completion_tokens, right.completion_tokens),
        total_tokens=add(left.total_tokens, right.total_tokens),
    )
