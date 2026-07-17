from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.ids import new_id
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
from agent_sdk.runtime._recovery_observability import hashed_identity
from agent_sdk.runtime.leases import Lease, LeaseHeldError, LeaseLostError, LeaseManager
from agent_sdk.runtime.execution import (
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
)
from agent_sdk.runtime.models import (
    RunFailure,
    RunResult,
    RunSnapshot,
    RunStatus,
    SessionSnapshot,
    TokenUsage,
)
from agent_sdk.runtime.provider_recovery import (
    ProviderRecoveryRegistry,
    ProviderRecoveryResult,
)
from agent_sdk.runtime.session_lifecycle import (
    RUN_LIFECYCLE_FINAL_STATUSES,
    detach_run_transition,
    exact_run_precondition,
    exact_session_precondition,
    load_session,
    session_write,
)
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
    RunCheckpoint,
    RunCheckpointPhase,
    RecoveryStateConflictError,
    ToolCallOperation,
)
from agent_sdk.storage.base import (
    CommitResult,
    ExternalOperationWrite,
    RunCheckpointWrite,
    RunProgressBatch,
    SnapshotPrecondition,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.tools.executor import ToolExecutor
from agent_sdk.tools.models import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    ToolRetryPolicy,
)
from agent_sdk.tools.registry import RegisteredTool, ToolRegistry

_DELTA_FLUSH_SECONDS = 0.05
_DELTA_FLUSH_BYTES = 4 * 1024
_MAX_TOOL_STEPS = 8
_MAX_SESSION_COMMIT_ATTEMPTS = 8
_RUN_LEASE_TTL = timedelta(seconds=30)


class _RunProgressError(AgentSDKError):
    """Private control-flow error for sanitized fenced progress failures."""


class _RunProgressConflictError(_RunProgressError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CONFLICT,
            "run lease is no longer current",
            retryable=False,
        )


class _RunProgressStorageError(_RunProgressError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.INTERNAL,
            "failed to persist run",
            retryable=False,
        )


class _RunEmitter:
    def __init__(
        self,
        store: StateStore,
        run: RunSnapshot,
        lease: Lease,
        clock: Callable[[], datetime],
        lease_error: Callable[[], BaseException | None],
        *,
        checkpoint: RunCheckpoint | None = None,
        sequence: int = 2,
        provider_recovery: ProviderRecoveryRegistry | None = None,
    ) -> None:
        self._store = store
        self._run = run
        self._lease = lease
        self._clock = clock
        self._lease_error = lease_error
        self._checkpoint = checkpoint
        self._sequence = sequence
        self._provider_recovery = provider_recovery or ProviderRecoveryRegistry()
        self._lock = asyncio.Lock()
        self._delta_parts: list[str] = []
        self._delta_bytes = 0
        self._timer: asyncio.Task[None] | None = None
        self._timer_error: BaseException | None = None

    @property
    def current_snapshot(self) -> RunSnapshot:
        return self._run

    @property
    def current_checkpoint(self) -> RunCheckpoint:
        assert self._checkpoint is not None
        return self._checkpoint

    async def initialize(self, messages: tuple[dict[str, Any], ...]) -> None:
        async with self._lock:
            self._ensure_lease_current()
            assert self._checkpoint is None
            snapshot = self._run.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "version": self._run.version + 1,
                }
            )
            checkpoint = RunCheckpoint(
                run_id=self._run.run_id,
                session_id=self._run.session_id,
                checkpoint_version=1,
                turn=0,
                phase=RunCheckpointPhase.READY_FOR_MODEL,
                messages=messages,
            )
            event = self._new_event("run.started", {"status": RunStatus.RUNNING.value})
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=self._lease,
                    now=self._clock(),
                    events=(event,),
                    snapshots=(self._run_write(snapshot),),
                    preconditions=(
                        SnapshotPrecondition("session", self._run.session_id),
                        exact_run_precondition(self._run),
                    ),
                    checkpoint=RunCheckpointWrite(None, checkpoint),
                ),
            )
            self._run = snapshot
            self._checkpoint = checkpoint
            self._sequence += 1

    async def resume(self, session: SessionSnapshot) -> None:
        async with self._lock:
            self._ensure_lease_current()
            assert self._checkpoint is not None
            if self._run.status is not RunStatus.INTERRUPTED:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "run is not safe to resume",
                    retryable=False,
                ) from None
            snapshot = self._run.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "version": self._run.version + 1,
                }
            )
            event = self._new_event(
                "run.recovery.started",
                {"status": RunStatus.RUNNING.value},
            )
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=self._lease,
                    now=self._clock(),
                    events=(event,),
                    snapshots=(self._run_write(snapshot),),
                    preconditions=(
                        exact_session_precondition(session),
                        exact_run_precondition(self._run),
                    ),
                    checkpoint_precondition=self._checkpoint,
                ),
            )
            self._run = snapshot
            self._sequence += 1

    async def start_model(self, request: ModelRequest) -> ModelCallOperation:
        async with self._lock:
            self._ensure_lease_current()
            assert self._checkpoint is not None
            adapter = self._provider_recovery.resolve(request.model)
            recovery_metadata: dict[str, object]
            if adapter is None:
                recovery_metadata = {
                    "authoritative_status": False,
                    "same_operation_id_resend": False,
                }
            else:
                recovery_metadata = {
                    "adapter_id": adapter.adapter_id,
                    "adapter_version": adapter.version,
                    "authoritative_status": adapter.authoritative_status,
                    "same_operation_id_resend": adapter.same_operation_id_resend,
                }
            operation = ModelCallOperation(
                operation_id=new_id("op_model"),
                session_id=self._run.session_id,
                run_id=self._run.run_id,
                turn=self._checkpoint.turn,
                request_fingerprint=_model_request_fingerprint(request),
                lease_generation=self._lease.generation,
                status=ExternalOperationStatus.STARTED,
                provider_identity=request.model,
                recovery_metadata=recovery_metadata,
            )
            checkpoint = self._checkpoint.model_copy(
                update={
                    "checkpoint_version": self._checkpoint.checkpoint_version + 1,
                    "phase": RunCheckpointPhase.MODEL_IN_FLIGHT,
                    "operation_id": operation.operation_id,
                }
            )
            events = (
                self._new_event("step.started", {}),
                self._new_event("model.call.started", {"model": request.model}, offset=1),
            )
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=self._lease,
                    now=self._clock(),
                    events=events,
                    preconditions=(
                        SnapshotPrecondition("session", self._run.session_id),
                        exact_run_precondition(self._run),
                    ),
                    operation=ExternalOperationWrite(None, operation),
                    checkpoint=RunCheckpointWrite(self._checkpoint, checkpoint),
                ),
            )
            self._checkpoint = checkpoint
            self._sequence += len(events)
            return operation

    async def complete_model(
        self,
        operation: ModelCallOperation,
        *,
        finish_reason: str | None,
        text: str,
        calls: list[ToolCallCompleted],
        operation_usage: TokenUsage,
        usage: TokenUsage,
        usage_payload: dict[str, int | None] | None,
        output_parts: list[str],
    ) -> None:
        async with self._lock:
            self._ensure_lease_current()
            assert self._checkpoint is not None
            assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                assistant["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments_json,
                        },
                    }
                    for call in calls
                ]
            completed = operation.model_copy(
                update={
                    "status": ExternalOperationStatus.COMPLETED,
                    "outcome": {
                        "finish_reason": finish_reason,
                        "text": text,
                        "tool_calls": [
                            {
                                "index": call.index,
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments_json": call.arguments_json,
                            }
                            for call in calls
                        ],
                        "usage": operation_usage.model_dump(mode="json"),
                    },
                }
            )
            checkpoint = self._checkpoint.model_copy(
                update={
                    "checkpoint_version": self._checkpoint.checkpoint_version + 1,
                    "phase": (
                        RunCheckpointPhase.READY_FOR_TOOL
                        if calls
                        else RunCheckpointPhase.READY_FOR_MODEL
                    ),
                    "operation_id": None,
                    "messages": (*self._checkpoint.messages, assistant),
                    "output_parts": tuple(output_parts),
                    "usage": usage,
                }
            )
            event_specs: list[tuple[str, dict[str, Any]]] = []
            if usage_payload is not None:
                event_specs.append(("model.usage.reported", usage_payload))
            event_specs.append(("model.call.completed", {"finish_reason": finish_reason}))
            events = tuple(
                self._new_event(event_type, payload, offset=index)
                for index, (event_type, payload) in enumerate(event_specs)
            )
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=self._lease,
                    now=self._clock(),
                    events=events,
                    preconditions=(
                        SnapshotPrecondition("session", self._run.session_id),
                        exact_run_precondition(self._run),
                    ),
                    operation=ExternalOperationWrite(operation, completed),
                    checkpoint=RunCheckpointWrite(self._checkpoint, checkpoint),
                ),
            )
            self._checkpoint = checkpoint
            self._sequence += len(events)

    async def fail_model(
        self,
        operation: ModelCallOperation,
        failure: AgentSDKError,
        *,
        output_parts: list[str],
        usage: TokenUsage,
        tool_results: list[ToolResult],
    ) -> None:
        async with self._lock:
            self._ensure_lease_current()
            assert self._checkpoint is not None
            payload = {"error": failure.to_dict()}
            failed_operation = operation.model_copy(
                update={
                    "status": ExternalOperationStatus.FAILED,
                    "outcome": {
                        "error": {
                            "code": failure.code.value,
                            "message": "model call failed",
                        }
                    },
                }
            )
            terminal_checkpoint = self._checkpoint.model_copy(
                update={
                    "checkpoint_version": self._checkpoint.checkpoint_version + 1,
                    "phase": RunCheckpointPhase.TERMINAL,
                    "operation_id": None,
                    "output_parts": tuple(output_parts),
                    "usage": usage,
                    "tool_results": tuple(tool_results),
                }
            )
            snapshot = self._run.model_copy(
                update={
                    "status": RunStatus.FAILED,
                    "version": self._run.version + 1,
                    "output_text": "".join(output_parts),
                    "usage": usage,
                    "tool_results": tuple(tool_results),
                    "error": RunFailure(
                        code=failure.code.value,
                        message=failure.message,
                        retryable=failure.retryable,
                    ),
                }
            )
            run_events = (
                self._new_event("model.call.failed", payload),
                self._new_event("step.failed", payload, offset=1),
                self._new_event("run.failed", payload, offset=2),
            )
            for attempt in range(_MAX_SESSION_COMMIT_ATTEMPTS):
                session = await load_session(self._store, self._run.session_id)
                updated_session, session_event_type = detach_run_transition(
                    session, self._run.run_id
                )
                session_event = EventEnvelope.new(
                    type=session_event_type,
                    session_id=session.session_id,
                    run_id=None,
                    sequence=updated_session.version,
                    payload={
                        "run_id": self._run.run_id,
                        "status": updated_session.status.value,
                    },
                )
                try:
                    await _commit_progress(
                        self._store,
                        RunProgressBatch(
                            lease=self._lease,
                            now=self._clock(),
                            events=(*run_events, session_event),
                            snapshots=(
                                self._run_write(snapshot),
                                session_write(updated_session),
                            ),
                            preconditions=(
                                exact_run_precondition(self._run),
                                exact_session_precondition(session),
                            ),
                            operation=ExternalOperationWrite(operation, failed_operation),
                            checkpoint=RunCheckpointWrite(self._checkpoint, terminal_checkpoint),
                        ),
                    )
                except _RunProgressConflictError:
                    try:
                        await self._store.assert_current_lease(self._lease, now=self._clock())
                    except Exception:
                        raise _RunProgressConflictError from None
                    current = await self._load_run_snapshot()
                    if current != self._run:
                        raise AgentSDKError(
                            ErrorCode.CONFLICT,
                            "run state changed concurrently",
                            retryable=True,
                        ) from None
                    if attempt + 1 < _MAX_SESSION_COMMIT_ATTEMPTS:
                        await asyncio.sleep(0)
                    continue
                self._run = snapshot
                self._checkpoint = terminal_checkpoint
                self._sequence += len(run_events)
                return
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "session state changed concurrently",
                retryable=True,
            )

    async def start_tool(
        self,
        call: ToolCallCompleted,
        registered: RegisteredTool,
        arguments: Mapping[str, Any],
    ) -> ToolCallOperation:
        async with self._lock:
            self._ensure_lease_current()
            assert self._checkpoint is not None
            capability = ToolCapabilityDescriptor.from_spec(registered.spec)
            retry_policy = registered.spec.retry_policy
            operation = ToolCallOperation(
                operation_id=new_id("op_tool"),
                session_id=self._run.session_id,
                run_id=self._run.run_id,
                turn=self._checkpoint.turn,
                request_fingerprint=_tool_request_fingerprint(call, capability, arguments),
                lease_generation=self._lease.generation,
                status=ExternalOperationStatus.STARTED,
                tool_identity=capability.capability_hash,
                recovery_metadata=(
                    {"safe_retry": False, "retry_class": "unsafe"}
                    if retry_policy is ToolRetryPolicy.NEVER
                    else {
                        "safe_retry": True,
                        "retry_class": retry_policy.value,
                    }
                ),
            )
            checkpoint = self._checkpoint.model_copy(
                update={
                    "checkpoint_version": self._checkpoint.checkpoint_version + 1,
                    "phase": RunCheckpointPhase.TOOL_IN_FLIGHT,
                    "operation_id": operation.operation_id,
                }
            )
            event = self._new_event(
                "tool.call.started",
                {"call_id": call.call_id, "tool_name": call.name},
            )
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=self._lease,
                    now=self._clock(),
                    events=(event,),
                    preconditions=(
                        SnapshotPrecondition("session", self._run.session_id),
                        exact_run_precondition(self._run),
                    ),
                    operation=ExternalOperationWrite(None, operation),
                    checkpoint=RunCheckpointWrite(self._checkpoint, checkpoint),
                ),
            )
            self._checkpoint = checkpoint
            self._sequence += 1
            return operation

    async def complete_tool(
        self,
        call: ToolCallCompleted,
        result: ToolResult,
        operation: ToolCallOperation | None,
    ) -> None:
        async with self._lock:
            self._ensure_lease_current()
            assert self._checkpoint is not None
            operation_write: ExternalOperationWrite | None = None
            if operation is not None:
                operation_status = (
                    ExternalOperationStatus.COMPLETED
                    if result.status is ToolResultStatus.SUCCEEDED
                    else ExternalOperationStatus.FAILED
                )
                finished_operation = operation.model_copy(
                    update={
                        "status": operation_status,
                        "outcome": result.model_dump(mode="json"),
                    }
                )
                operation_write = ExternalOperationWrite(operation, finished_operation)
            tool_message = {
                "role": "tool",
                "tool_call_id": call.call_id,
                "name": call.name,
                "content": result.content,
            }
            checkpoint = self._checkpoint.model_copy(
                update={
                    "checkpoint_version": self._checkpoint.checkpoint_version + 1,
                    "turn": self._checkpoint.turn + 1,
                    "phase": RunCheckpointPhase.READY_FOR_MODEL,
                    "operation_id": None,
                    "messages": (*self._checkpoint.messages, tool_message),
                    "tool_results": (*self._checkpoint.tool_results, result),
                }
            )
            event = self._new_event("tool.call.completed", result.model_dump(mode="json"))
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=self._lease,
                    now=self._clock(),
                    events=(event,),
                    preconditions=(
                        SnapshotPrecondition("session", self._run.session_id),
                        exact_run_precondition(self._run),
                    ),
                    operation=operation_write,
                    checkpoint=RunCheckpointWrite(self._checkpoint, checkpoint),
                ),
            )
            self._checkpoint = checkpoint
            self._sequence += 1

    async def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        snapshot: RunSnapshot | None = None,
    ) -> None:
        async with self._lock:
            self._ensure_lease_current()
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
            self._ensure_lease_current()
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

    async def permission_transition(
        self,
        event_type: str,
        status: RunStatus,
        payload: dict[str, Any],
    ) -> None:
        async with self._lock:
            self._ensure_lease_current()
            assert self._checkpoint is not None
            snapshot = self._run.model_copy(
                update={"status": status, "version": self._run.version + 1}
            )
            checkpoint = self._checkpoint.model_copy(
                update={
                    "checkpoint_version": self._checkpoint.checkpoint_version + 1,
                    "phase": (
                        RunCheckpointPhase.WAITING
                        if status is RunStatus.WAITING_PERMISSION
                        else RunCheckpointPhase.READY_FOR_TOOL
                    ),
                    "operation_id": None,
                }
            )
            event = self._new_event(event_type, payload)
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=self._lease,
                    now=self._clock(),
                    events=(event,),
                    snapshots=(self._run_write(snapshot),),
                    preconditions=(
                        SnapshotPrecondition("session", self._run.session_id),
                        exact_run_precondition(self._run),
                    ),
                    checkpoint=RunCheckpointWrite(self._checkpoint, checkpoint),
                ),
            )
            self._run = snapshot
            self._checkpoint = checkpoint
            self._sequence += 1

    async def recovery_permission_transition(
        self,
        event_type: str,
        status: RunStatus,
        payload: dict[str, Any],
    ) -> None:
        async with self._lock:
            self._ensure_lease_current()
            assert self._checkpoint is not None
            assert self._checkpoint.phase is RunCheckpointPhase.TOOL_IN_FLIGHT
            snapshot = self._run.model_copy(
                update={"status": status, "version": self._run.version + 1}
            )
            event = self._new_event(event_type, payload)
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=self._lease,
                    now=self._clock(),
                    events=(event,),
                    snapshots=(self._run_write(snapshot),),
                    preconditions=(
                        SnapshotPrecondition("session", self._run.session_id),
                        exact_run_precondition(self._run),
                    ),
                    checkpoint_precondition=self._checkpoint,
                ),
            )
            self._run = snapshot
            self._sequence += 1

    async def add_delta(self, text: str) -> None:
        cancelled_timer: asyncio.Task[None] | None = None
        async with self._lock:
            self._ensure_lease_current()
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
            self._ensure_lease_current()
            self._raise_timer_error()
            cancelled_timer = self._detach_timer_locked()
            await self._flush_delta_locked()
        await self._settle_cancelled_timer(cancelled_timer)

    async def close(self) -> None:
        close_task = asyncio.create_task(self._close_owned())
        cancellation = await _settle_background_task(close_task)
        if cancellation is not None:
            raise cancellation from None
        close_task.result()

    async def _close_owned(self) -> None:
        cancelled_timer: asyncio.Task[None] | None = None
        close_error: BaseException | None = None
        lease_lost = False
        async with self._lock:
            cancelled_timer = self._detach_timer_locked()
            lease_lost = self._lease_error() is not None
            timer_error = self._timer_error
            self._timer_error = None
            if lease_lost:
                self._delta_parts.clear()
                self._delta_bytes = 0
            else:
                try:
                    if timer_error is not None:
                        raise timer_error
                    await self._flush_delta_locked()
                except BaseException as error:
                    close_error = error

        if cancelled_timer is not None:
            await asyncio.gather(cancelled_timer, return_exceptions=True)
            if (
                close_error is None
                and not lease_lost
                and not cancelled_timer.cancelled()
            ):
                close_error = cancelled_timer.exception()
        if lease_lost:
            raise LeaseLostError from None
        if close_error is not None:
            raise close_error

    async def _flush_after_delay(self) -> None:
        await asyncio.sleep(_DELTA_FLUSH_SECONDS)
        async with self._lock:
            self._ensure_lease_current()
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

    def _ensure_lease_current(self) -> None:
        if self._lease_error() is not None:
            raise LeaseLostError from None

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
        self._ensure_lease_current()
        event = self._new_event(event_type, payload)
        if snapshot is not None and snapshot.status in RUN_LIFECYCLE_FINAL_STATUSES:
            await self._commit_terminal(event, snapshot)
            return
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
        store_failed = False
        try:
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=self._lease,
                    now=self._clock(),
                    events=(event,),
                    snapshots=snapshots,
                    preconditions=(
                        SnapshotPrecondition("session", self._run.session_id),
                        SnapshotPrecondition("run", self._run.run_id, self._run.version),
                    ),
                ),
            )
        except _RunProgressError:
            raise
        except Exception:
            store_failed = True
        if store_failed:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to persist run",
                retryable=False,
            ) from None
        if snapshot is not None:
            self._run = snapshot
        self._sequence += 1

    def _new_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        offset: int = 0,
    ) -> EventEnvelope:
        return EventEnvelope.new(
            type=event_type,
            session_id=self._run.session_id,
            run_id=self._run.run_id,
            sequence=self._sequence + offset,
            payload=payload,
        )

    @staticmethod
    def _run_write(snapshot: RunSnapshot) -> SnapshotWrite:
        return SnapshotWrite(
            "run",
            snapshot.run_id,
            snapshot.session_id,
            snapshot.version,
            snapshot.model_dump(mode="json"),
        )

    async def _commit_terminal(
        self,
        run_event: EventEnvelope,
        snapshot: RunSnapshot,
    ) -> None:
        self._ensure_lease_current()
        assert self._checkpoint is not None
        terminal_checkpoint = self._checkpoint.model_copy(
            update={
                "checkpoint_version": self._checkpoint.checkpoint_version + 1,
                "phase": RunCheckpointPhase.TERMINAL,
                "operation_id": None,
            }
        )
        for attempt in range(_MAX_SESSION_COMMIT_ATTEMPTS):
            session = await load_session(self._store, self._run.session_id)
            updated_session, session_event_type = detach_run_transition(
                session,
                self._run.run_id,
            )
            session_event = EventEnvelope.new(
                type=session_event_type,
                session_id=session.session_id,
                run_id=None,
                sequence=updated_session.version,
                payload={
                    "run_id": self._run.run_id,
                    "status": updated_session.status.value,
                },
            )
            store_failed = False
            try:
                await _commit_progress(
                    self._store,
                    RunProgressBatch(
                        lease=self._lease,
                        now=self._clock(),
                        events=(run_event, session_event),
                        snapshots=(
                            SnapshotWrite(
                                "run",
                                snapshot.run_id,
                                snapshot.session_id,
                                snapshot.version,
                                snapshot.model_dump(mode="json"),
                            ),
                            session_write(updated_session),
                        ),
                        preconditions=(
                            exact_run_precondition(self._run),
                            exact_session_precondition(session),
                        ),
                        checkpoint=RunCheckpointWrite(self._checkpoint, terminal_checkpoint),
                    ),
                )
            except _RunProgressConflictError:
                try:
                    await self._store.assert_current_lease(self._lease, now=self._clock())
                except Exception:
                    raise _RunProgressConflictError from None
                current = await self._load_run_snapshot()
                if current != self._run:
                    raise AgentSDKError(
                        ErrorCode.CONFLICT,
                        "run state changed concurrently",
                        retryable=True,
                    ) from None
                if attempt + 1 < _MAX_SESSION_COMMIT_ATTEMPTS:
                    await asyncio.sleep(0)
                continue
            except Exception:
                store_failed = True
            if store_failed:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "failed to persist run",
                    retryable=False,
                ) from None
            self._run = snapshot
            self._checkpoint = terminal_checkpoint
            self._sequence += 1
            return
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "session state changed concurrently",
            retryable=True,
        )

    async def _load_run_snapshot(self) -> RunSnapshot:
        data: dict[str, Any] | None = None
        store_failed = False
        try:
            data = await self._store.get_snapshot("run", self._run.run_id)
        except Exception:
            store_failed = True
        if store_failed:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to load run",
                retryable=False,
            ) from None
        if data is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "run not found",
                retryable=False,
            )
        snapshot: RunSnapshot | None = None
        validation_failed = False
        try:
            snapshot = RunSnapshot.model_validate(data)
        except Exception:
            validation_failed = True
        data = None
        if validation_failed:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to load run",
                retryable=False,
            ) from None
        assert snapshot is not None
        return snapshot


class RunEngine:
    def __init__(
        self,
        store: StateStore,
        models: LiteLLMGateway,
        tools: ToolRegistry | None = None,
        policy: PolicyEngine | None = None,
        permission_bridge: InProcessPermissionBridge | None = None,
        *,
        lease_manager: LeaseManager | None = None,
        _clock: Callable[[], datetime] | None = None,
        _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        _heartbeat_interval: float = _RUN_LEASE_TTL.total_seconds() / 3,
        provider_recovery: ProviderRecoveryRegistry | None = None,
    ) -> None:
        self._store = store
        self._models = models
        self._tools = tools or ToolRegistry()
        self._policy = policy or PolicyEngine()
        self._permission_bridge = permission_bridge
        self._leases = lease_manager or LeaseManager(store, ttl=_RUN_LEASE_TTL)
        self._clock = _clock or (lambda: datetime.now(UTC))
        self._sleep = _sleep
        self._heartbeat_interval = _heartbeat_interval
        self._provider_recovery = provider_recovery or ProviderRecoveryRegistry()

    async def execute(self, run_id: str, request: ModelRequest) -> RunResult:
        public_error: tuple[ErrorCode, str, bool] | None = None
        lease_held = False
        try:
            return await self._execute_private(run_id, request)
        except LeaseHeldError:
            lease_held = True
        except AgentSDKError as error:
            public_error = (error.code, error.message, error.retryable)
        del self, run_id, request
        if lease_held:
            raise LeaseHeldError from None
        assert public_error is not None
        raise AgentSDKError(
            public_error[0],
            public_error[1],
            retryable=public_error[2],
        ) from None

    async def resume(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        request: ModelRequest,
    ) -> RunResult:
        public_error: tuple[ErrorCode, str, bool] | None = None
        lease_held = False
        try:
            return await self._resume_private(run_id, checkpoint, request)
        except LeaseHeldError:
            lease_held = True
        except AgentSDKError as error:
            public_error = (error.code, error.message, error.retryable)
        del self, run_id, checkpoint, request
        if lease_held:
            raise LeaseHeldError from None
        assert public_error is not None
        raise AgentSDKError(
            public_error[0],
            public_error[1],
            retryable=public_error[2],
        ) from None

    async def resume_recovered_model(
        self,
        run: RunSnapshot,
        session: SessionSnapshot,
        checkpoint: RunCheckpoint,
        operation: ModelCallOperation,
        request: ModelRequest,
        result: ProviderRecoveryResult,
        lease: Lease,
        *,
        sequence: int,
    ) -> RunResult:
        return await self._execute_owned(
            run,
            request,
            lease,
            lambda: None,
            checkpoint=checkpoint,
            sequence=sequence,
            recovery_session=session,
            recovered_model=(operation, result),
        )

    async def resume_recovered_tool(
        self,
        run: RunSnapshot,
        session: SessionSnapshot,
        checkpoint: RunCheckpoint,
        operation: ToolCallOperation,
        call: ToolCallCompleted,
        registered: RegisteredTool,
        request: ModelRequest,
        lease: Lease,
        *,
        sequence: int,
    ) -> RunResult:
        return await self._execute_owned(
            run,
            request,
            lease,
            lambda: None,
            checkpoint=checkpoint,
            sequence=sequence,
            recovery_session=session,
            recovered_tool=(operation, call, registered),
        )

    async def fail_recovered_model(
        self,
        run: RunSnapshot,
        checkpoint: RunCheckpoint,
        operation: ModelCallOperation,
        result: ProviderRecoveryResult,
        lease: Lease,
        *,
        sequence: int,
    ) -> RunResult:
        assert result.error_code is not None
        assert result.retryable is not None
        emitter = _RunEmitter(
            self._store,
            run,
            lease,
            self._clock,
            lambda: None,
            checkpoint=checkpoint,
            sequence=sequence,
            provider_recovery=self._provider_recovery,
        )
        failure = AgentSDKError(
            result.error_code,
            "model call failed",
            retryable=result.retryable,
        )
        await emitter.fail_model(
            operation,
            failure,
            output_parts=list(checkpoint.output_parts),
            usage=checkpoint.usage,
            tool_results=list(checkpoint.tool_results),
        )
        await emitter.close()
        raise failure from None

    def validate_resume_checkpoint(self, checkpoint: RunCheckpoint) -> None:
        """Validate recovery-only checkpoint content before lease admission."""
        if checkpoint.phase is RunCheckpointPhase.READY_FOR_TOOL:
            if len(checkpoint.tool_results) >= _MAX_TOOL_STEPS:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "run is not safe to resume",
                    retryable=False,
                ) from None
            self._tool_call_from_checkpoint(checkpoint)

    async def _execute_private(self, run_id: str, request: ModelRequest) -> RunResult:
        created = await self._load_created_run(run_id)
        self._validate_live_execution(created, request)
        lease = await self._leases.acquire(run_id, new_id("coord"), now=self._clock())
        owner = asyncio.current_task()
        assert owner is not None
        heartbeat_error: BaseException | None = None
        heartbeat = asyncio.create_task(self._heartbeat(lease))

        def heartbeat_finished(task: asyncio.Task[None]) -> None:
            nonlocal heartbeat_error
            if task.cancelled():
                return
            heartbeat_error = task.exception()
            if heartbeat_error is not None and not owner.done():
                owner.cancel()

        heartbeat.add_done_callback(heartbeat_finished)
        try:
            try:
                return await self._execute_owned(
                    created,
                    request,
                    lease,
                    lambda: heartbeat_error,
                )
            except _RunProgressConflictError:
                session = await self._store.get_snapshot("session", created.session_id)
                if session is None:
                    raise AgentSDKError(
                        ErrorCode.NOT_FOUND,
                        "run session no longer exists",
                        retryable=False,
                    ) from None
                raise
        except asyncio.CancelledError:
            if heartbeat_error is not None:
                raise LeaseLostError from None
            raise
        finally:
            active_error = sys.exception()
            heartbeat.cancel()
            cleanup_cancellation = await _settle_background_task(heartbeat)
            release = asyncio.create_task(self._leases.release(lease))
            release_cancellation = await _settle_background_task(release)
            if cleanup_cancellation is None:
                cleanup_cancellation = release_cancellation
            if active_error is None and cleanup_cancellation is not None:
                raise cleanup_cancellation from None

    async def _resume_private(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        request: ModelRequest,
    ) -> RunResult:
        interrupted = await self._load_run(run_id)
        if interrupted.status is not RunStatus.INTERRUPTED:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not safe to resume",
                retryable=False,
            ) from None
        if (
            checkpoint.run_id != interrupted.run_id
            or checkpoint.session_id != interrupted.session_id
            or checkpoint.phase
            not in {
                RunCheckpointPhase.READY_FOR_MODEL,
                RunCheckpointPhase.READY_FOR_TOOL,
            }
            or checkpoint.operation_id is not None
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not safe to resume",
                retryable=False,
            ) from None
        self.validate_resume_checkpoint(checkpoint)
        durable_checkpoint = await self._store.get_run_checkpoint(run_id)
        if durable_checkpoint != checkpoint:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not safe to resume",
                retryable=False,
            ) from None
        if await self._store.list_unresolved_external_operations(run_id):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not safe to resume",
                retryable=False,
            ) from None
        session = await load_session(self._store, interrupted.session_id)
        if interrupted.run_id not in session.active_run_ids:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not safe to resume",
                retryable=False,
            ) from None
        sequence = await self._store.latest_run_event_sequence(run_id)
        if sequence is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not safe to resume",
                retryable=False,
            ) from None
        self._validate_live_execution(interrupted, request)
        lease = await self._leases.acquire(run_id, new_id("coord"), now=self._clock())
        owner = asyncio.current_task()
        assert owner is not None
        heartbeat_error: BaseException | None = None
        heartbeat = asyncio.create_task(self._heartbeat(lease))

        def heartbeat_finished(task: asyncio.Task[None]) -> None:
            nonlocal heartbeat_error
            if task.cancelled():
                return
            heartbeat_error = task.exception()
            if heartbeat_error is not None and not owner.done():
                owner.cancel()

        heartbeat.add_done_callback(heartbeat_finished)
        try:
            return await self._execute_owned(
                interrupted,
                request,
                lease,
                lambda: heartbeat_error,
                checkpoint=checkpoint,
                sequence=sequence + 1,
                recovery_session=session,
            )
        except asyncio.CancelledError:
            if heartbeat_error is not None:
                raise LeaseLostError from None
            raise
        finally:
            active_error = sys.exception()
            heartbeat.cancel()
            cleanup_cancellation = await _settle_background_task(heartbeat)
            release = asyncio.create_task(self._leases.release(lease))
            release_cancellation = await _settle_background_task(release)
            if cleanup_cancellation is None:
                cleanup_cancellation = release_cancellation
            if active_error is None and cleanup_cancellation is not None:
                raise cleanup_cancellation from None

    async def _heartbeat(self, lease: Lease) -> None:
        while True:
            await self._sleep(self._heartbeat_interval)
            lease = await self._leases.renew(lease, now=self._clock())

    def _validate_live_execution(
        self,
        created: RunSnapshot,
        request: ModelRequest,
    ) -> None:
        descriptor = created.execution_descriptor
        if descriptor is None:
            return
        policy_config = self._policy.execution_config()
        live_policy = ExecutionPolicyDescriptor.create(
            permission_default=policy_config["permission_default"],
            permission_rules=policy_config["permission_rules"],
        )
        live_tools = tuple(ToolCapabilityDescriptor.from_spec(spec) for spec in self._tools.list())
        request_messages = tuple(deepcopy(message) for message in request.messages)
        descriptor_messages = tuple(dict(message) for message in descriptor.messages)
        descriptor_params = descriptor.agent.model_dump(mode="json")["model_params"]
        mismatched = (
            descriptor.agent.model != request.model
            or descriptor_messages != request_messages
            or descriptor_params != request.params
            or descriptor.tools != live_tools
            or request.tools != self._tools.schemas()
            or descriptor.policy != live_policy
        )
        if mismatched:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run execution descriptor mismatch",
                retryable=False,
            ) from None

    async def _execute_owned(
        self,
        created: RunSnapshot,
        request: ModelRequest,
        lease: Lease,
        lease_error: Callable[[], BaseException | None],
        *,
        checkpoint: RunCheckpoint | None = None,
        sequence: int = 2,
        recovery_session: SessionSnapshot | None = None,
        recovered_model: tuple[ModelCallOperation, ProviderRecoveryResult] | None = None,
        recovered_tool: tuple[
            ToolCallOperation,
            ToolCallCompleted,
            RegisteredTool,
        ]
        | None = None,
    ) -> RunResult:
        run_id = created.run_id
        emitter = _RunEmitter(
            self._store,
            created,
            lease,
            self._clock,
            lease_error,
            checkpoint=checkpoint,
            sequence=sequence,
            provider_recovery=self._provider_recovery,
        )
        if checkpoint is None:
            initial_messages = request.messages
            if created.execution_descriptor is not None:
                initial_messages = tuple(
                    dict(message) for message in created.execution_descriptor.messages
                )
            await emitter.initialize(initial_messages)
        else:
            assert recovery_session is not None
            await emitter.resume(recovery_session)
        current_checkpoint = emitter.current_checkpoint
        chunks = list(current_checkpoint.output_parts)
        usage = current_checkpoint.usage
        tool_results = list(current_checkpoint.tool_results)
        messages = list(current_checkpoint.model_dump(mode="json")["messages"])
        executor = ToolExecutor(
            self._tools,
            self._policy,
            self._permission_bridge,
        )
        try:
            if recovered_tool is not None:
                recovered_operation, pending_call, recovered_registered = recovered_tool
                tool_result = await self._execute_recovered_tool_call(
                    executor,
                    emitter,
                    pending_call,
                    recovered_operation,
                    recovered_registered,
                    lease,
                    run_id=run_id,
                    chunks=chunks,
                    usage=usage,
                    tool_results=tool_results,
                )
                tool_results.append(tool_result)
                await emitter.emit("step.completed")
                messages = list(
                    emitter.current_checkpoint.model_dump(mode="json")["messages"]
                )
            if current_checkpoint.phase is RunCheckpointPhase.READY_FOR_TOOL:
                if len(tool_results) >= _MAX_TOOL_STEPS:
                    failure = AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "tool step limit exceeded",
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
                pending_call = self._tool_call_from_checkpoint(current_checkpoint)
                tool_result = await self._execute_tool_call(
                    executor,
                    emitter,
                    pending_call,
                    run_id=run_id,
                    chunks=chunks,
                    usage=usage,
                    tool_results=tool_results,
                )
                tool_results.append(tool_result)
                await emitter.emit("step.completed")
                messages = list(
                    emitter.current_checkpoint.model_dump(mode="json")["messages"]
                )
            while True:
                step_chunks: list[str] = []
                step_usage = TokenUsage()
                calls: list[ToolCallCompleted] = []
                model_completed: ModelCompleted | None = None
                usage_payload: dict[str, int | None] | None = None
                if recovered_model is not None:
                    operation, recovered_result = recovered_model
                    recovered_model = None
                    assert recovered_result.text is not None
                    assert recovered_result.usage is not None
                    chunks.append(recovered_result.text)
                    step_chunks.append(recovered_result.text)
                    step_usage = recovered_result.usage
                    usage = _add_usage(usage, step_usage)
                    usage_payload = step_usage.model_dump(mode="json")
                    if recovered_result.tool_call is not None:
                        calls.append(recovered_result.tool_call)
                    model_completed = ModelCompleted(recovered_result.finish_reason)
                else:
                    model_request = ModelRequest(
                        model=request.model,
                        messages=tuple(deepcopy(messages)),
                        tools=request.tools,
                        params=dict(request.params),
                    )
                    operation = await emitter.start_model(model_request)
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
                                reported_usage = event.to_usage()
                                step_usage = _add_usage(step_usage, reported_usage)
                                usage = _add_usage(usage, reported_usage)
                                usage_payload = event.to_payload()
                            elif isinstance(event, ModelCompleted):
                                await emitter.flush_delta()
                                model_completed = event
                    except asyncio.CancelledError:
                        raise
                    except (LeaseLostError, _RunProgressError):
                        raise
                    except Exception as cause:
                        failure = AgentSDKError(
                            ErrorCode.INTERNAL,
                            "model call failed",
                            retryable=False,
                        )
                        await emitter.flush_delta()
                        await emitter.fail_model(
                            operation,
                            failure,
                            output_parts=chunks,
                            usage=usage,
                            tool_results=tool_results,
                        )
                        await emitter.close()
                        del cause
                        raise failure from None

                if model_completed is None:
                    failure = AgentSDKError(
                        ErrorCode.INTERNAL,
                        "model call failed",
                        retryable=False,
                    )
                    await emitter.flush_delta()
                    await emitter.fail_model(
                        operation,
                        failure,
                        output_parts=chunks,
                        usage=usage,
                        tool_results=tool_results,
                    )
                    await emitter.close()
                    raise failure from None
                await emitter.complete_model(
                    operation,
                    finish_reason=model_completed.finish_reason,
                    text="".join(step_chunks),
                    calls=calls,
                    operation_usage=step_usage,
                    usage=usage,
                    usage_payload=usage_payload,
                    output_parts=chunks,
                )

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

                if calls and len(tool_results) >= _MAX_TOOL_STEPS:
                    failure = AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "tool step limit exceeded",
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
                tool_result = await self._execute_tool_call(
                    executor,
                    emitter,
                    call,
                    run_id=run_id,
                    chunks=chunks,
                    usage=usage,
                    tool_results=tool_results,
                )
                tool_results.append(tool_result)
                await emitter.emit("step.completed")
                messages = list(
                    emitter.current_checkpoint.model_dump(mode="json")["messages"]
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
                tool_result.model_dump(mode="json") for tool_result in tool_results
            ]
        await emitter.transition(
            "run.completed",
            RunStatus.COMPLETED,
            terminal_payload,
            update={
                "output_text": output_text,
                "usage": usage,
                "tool_results": tuple(tool_results),
            },
        )
        await emitter.close()
        return run_result

    async def _execute_tool_call(
        self,
        executor: ToolExecutor,
        emitter: _RunEmitter,
        call: ToolCallCompleted,
        *,
        run_id: str,
        chunks: list[str],
        usage: TokenUsage,
        tool_results: list[ToolResult],
    ) -> ToolResult:
        await emitter.emit(
            "tool.call.proposed",
            {"call_id": call.call_id, "tool_name": call.name},
        )
        tool_operation: ToolCallOperation | None = None

        async def before_handler(
            hook_call: ToolCallCompleted,
            registered: RegisteredTool,
            arguments: Mapping[str, Any],
        ) -> None:
            nonlocal tool_operation
            tool_operation = await emitter.start_tool(
                hook_call,
                registered,
                arguments,
            )

        async def call_completed(
            hook_call: ToolCallCompleted,
            result: ToolResult,
        ) -> None:
            await emitter.complete_tool(hook_call, result, tool_operation)

        try:
            return await executor.execute(
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
                on_before_handler=before_handler,
                on_call_completed=call_completed,
            )
        except asyncio.CancelledError:
            raise
        except (LeaseLostError, _RunProgressError):
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

    async def _execute_recovered_tool_call(
        self,
        executor: ToolExecutor,
        emitter: _RunEmitter,
        call: ToolCallCompleted,
        operation: ToolCallOperation,
        expected_registered: RegisteredTool,
        lease: Lease,
        *,
        run_id: str,
        chunks: list[str],
        usage: TokenUsage,
        tool_results: list[ToolResult],
    ) -> ToolResult:
        expected_capability = ToolCapabilityDescriptor.from_spec(
            expected_registered.spec
        )
        expected_metadata = {
            "safe_retry": True,
            "retry_class": expected_registered.spec.retry_policy.value,
        }

        def validate_registered_tool() -> None:
            try:
                current = self._tools.get(call.name)
            except AgentSDKError:
                raise RecoveryStateConflictError from None
            if current is not expected_registered:
                raise RecoveryStateConflictError
            capability = ToolCapabilityDescriptor.from_spec(current.spec)
            if (
                current.spec != expected_registered.spec
                or capability != expected_capability
                or operation.tool_identity != capability.capability_hash
                or dict(operation.recovery_metadata) != expected_metadata
            ):
                raise RecoveryStateConflictError

        async def preflight() -> None:
            validate_registered_tool()
            await self._leases.assert_current(lease, now=self._clock())
            validate_registered_tool()

        async def before_handler(
            hook_call: ToolCallCompleted,
            registered: RegisteredTool,
            arguments: Mapping[str, Any],
        ) -> None:
            if hook_call != call or registered is not expected_registered:
                raise RecoveryStateConflictError
            await preflight()
            if (
                _tool_request_fingerprint(call, expected_capability, arguments)
                != operation.request_fingerprint
            ):
                raise RecoveryStateConflictError

        async def call_completed(
            hook_call: ToolCallCompleted,
            result: ToolResult,
        ) -> None:
            await emitter.complete_tool(hook_call, result, operation)

        async def recovery_emit(event_type: str, payload: dict[str, Any]) -> None:
            if event_type == "tool.call.authorized":
                payload = {
                    "call": hashed_identity(call.call_id),
                    "tool": hashed_identity(call.name),
                }
            await emitter.emit(event_type, payload)

        try:
            return await executor.execute(
                call,
                ToolContext(
                    run_id=run_id,
                    session_id=emitter.current_snapshot.session_id,
                ),
                emit=recovery_emit,
                on_permission_requested=lambda permission, decision: (
                    self._recovery_permission_transition(
                        emitter,
                        "permission.requested",
                        RunStatus.WAITING_PERMISSION,
                        permission,
                        decision,
                    )
                ),
                on_permission_resolved=lambda permission, decision: (
                    self._recovery_permission_transition(
                        emitter,
                        "permission.resolved",
                        RunStatus.RUNNING,
                        permission,
                        decision,
                    )
                ),
                on_before_handler=before_handler,
                on_call_completed=call_completed,
                on_preflight=preflight,
                sanitize_permission_denial=True,
            )
        except asyncio.CancelledError:
            raise
        except (LeaseLostError, RecoveryStateConflictError, _RunProgressError):
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

    def _tool_call_from_checkpoint(
        self,
        checkpoint: RunCheckpoint,
    ) -> ToolCallCompleted:
        data = checkpoint.model_dump(mode="json")
        messages = data["messages"]
        try:
            assistant = messages[-1]
            calls = assistant["tool_calls"]
            if assistant["role"] != "assistant" or len(calls) != 1:
                raise ValueError
            raw_call = calls[0]
            function = raw_call["function"]
            if raw_call["type"] != "function":
                raise ValueError
            call_id = raw_call["id"]
            name = function["name"]
            arguments_json = function["arguments"]
            if not all(
                isinstance(value, str) and value
                for value in (call_id, name, arguments_json)
            ):
                raise ValueError
            self._tools.get(name)
        except (AgentSDKError, KeyError, TypeError, ValueError):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not safe to resume",
                retryable=False,
            ) from None
        return ToolCallCompleted(
            index=0,
            call_id=call_id,
            name=name,
            arguments_json=arguments_json,
        )

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
        await emitter.permission_transition(event_type, status, payload)

    @staticmethod
    async def _recovery_permission_transition(
        emitter: _RunEmitter,
        event_type: str,
        status: RunStatus,
        request: PermissionRequest,
        decision: PermissionDecision | None,
    ) -> None:
        payload: dict[str, Any] = {
            "request": hashed_identity(request.request_id),
            "tool": hashed_identity(request.tool_name),
        }
        if decision is not None:
            payload["allowed"] = decision.allowed
        await emitter.recovery_permission_transition(event_type, status, payload)

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
                "tool_results": tuple(tool_results),
                "error": RunFailure(
                    code=failure.code.value,
                    message=failure.message,
                    retryable=failure.retryable,
                ),
            },
        )

    async def _load_created_run(self, run_id: str) -> RunSnapshot:
        run = await self._load_run(run_id)
        if run.status is not RunStatus.CREATED:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not ready to start",
                retryable=False,
            )
        return run

    async def _load_run(self, run_id: str) -> RunSnapshot:
        data = await self._store.get_snapshot("run", run_id)
        if data is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "run not found",
                retryable=False,
            )
        try:
            run = RunSnapshot.model_validate(data)
        except ValueError:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to load run",
                retryable=False,
            ) from None
        if run.run_id != run_id:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to load run",
                retryable=False,
            ) from None
        return run


async def _settle_background_task(
    task: asyncio.Task[Any],
) -> asyncio.CancelledError | None:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.done() and task.cancelled():
                break
            if cancellation is None:
                cancellation = error
            continue
        except Exception:
            break
    if task.done() and not task.cancelled():
        task.exception()
    return cancellation


async def _commit_progress(
    store: StateStore,
    batch: RunProgressBatch,
) -> CommitResult:
    task = asyncio.create_task(store.commit_run_progress(batch))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        await _settle_commit_task(task)
        if task.done() and not task.cancelled() and task.exception() is not None:
            replay = asyncio.create_task(store.commit_run_progress(batch))
            await _settle_commit_task(replay)
        raise cancellation from None
    except Exception as first_error:
        del first_error

    replay = asyncio.create_task(store.commit_run_progress(batch))
    try:
        return await asyncio.shield(replay)
    except RecoveryStateConflictError:
        raise _RunProgressConflictError from None
    except asyncio.CancelledError as cancellation:
        await _settle_commit_task(replay)
        raise cancellation from None
    except Exception as replay_error:
        del replay_error
    raise _RunProgressStorageError from None


async def _settle_commit_task(task: asyncio.Task[CommitResult]) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if task.done() and not task.cancelled():
        task.exception()


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


def _model_request_fingerprint(request: ModelRequest) -> str:
    encoded = json.dumps(
        {
            "model": request.model,
            "messages": request.messages,
            "tools": request.tools,
            "params": request.params,
            "purpose": request.purpose,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _tool_request_fingerprint(
    call: ToolCallCompleted,
    capability: ToolCapabilityDescriptor,
    arguments: Mapping[str, Any],
) -> str:
    encoded = json.dumps(
        {
            "call_id": call.call_id,
            "tool_name": call.name,
            "arguments": arguments,
            "capability": capability.model_dump(mode="json"),
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()
