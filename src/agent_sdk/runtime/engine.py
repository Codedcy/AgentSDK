from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.models.litellm_gateway import (
    LiteLLMGateway,
    ModelCompleted,
    ModelRequest,
    TextDelta,
    UsageReported,
)
from agent_sdk.runtime.models import RunResult, RunSnapshot, RunStatus, TokenUsage
from agent_sdk.storage.base import CommitBatch, SnapshotWrite, StateStore

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
        self._sequence += 1


class RunEngine:
    def __init__(self, store: StateStore, models: LiteLLMGateway) -> None:
        self._store = store
        self._models = models

    async def execute(self, run_id: str, request: ModelRequest) -> RunResult:
        created = await self._load_created_run(run_id)
        emitter = _RunEmitter(self._store, created)
        running = created.model_copy(
            update={"status": RunStatus.RUNNING, "version": 2}
        )
        await emitter.emit(
            "run.started",
            {"status": RunStatus.RUNNING.value},
            snapshot=running,
        )
        await emitter.emit("step.started")
        await emitter.emit("model.call.started", {"model": request.model})

        chunks: list[str] = []
        usage = TokenUsage()
        try:
            async for event in self._models.stream(request):
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
                    await emitter.add_delta(event.text)
                elif isinstance(event, UsageReported):
                    await emitter.flush_delta()
                    usage = event.to_usage()
                    await emitter.emit("model.usage.reported", event.to_payload())
                elif isinstance(event, ModelCompleted):
                    await emitter.flush_delta()
                    await emitter.emit("model.call.completed", event.to_payload())
        except asyncio.CancelledError:
            await emitter.close()
            raise
        except Exception as cause:
            failure = AgentSDKError(
                ErrorCode.INTERNAL,
                "model call failed",
                retryable=False,
            )
            await emitter.flush_delta()
            payload = {"error": failure.to_dict()}
            await emitter.emit("model.call.failed", payload)
            await emitter.emit("step.failed", payload)
            failed = running.model_copy(
                update={
                    "status": RunStatus.FAILED,
                    "version": 3,
                    "output_text": "".join(chunks),
                    "usage": usage,
                }
            )
            await emitter.emit("run.failed", payload, snapshot=failed)
            await emitter.close()
            raise failure from cause

        await emitter.flush_delta()
        await emitter.emit("step.completed")
        output_text = "".join(chunks)
        completed = running.model_copy(
            update={
                "status": RunStatus.COMPLETED,
                "version": 3,
                "output_text": output_text,
                "usage": usage,
            }
        )
        result = RunResult(run_id=run_id, output_text=output_text, usage=usage)
        await emitter.emit(
            "run.completed",
            {"output_text": output_text, "usage": usage.model_dump()},
            snapshot=completed,
        )
        await emitter.close()
        return result

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
