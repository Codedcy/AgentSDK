from __future__ import annotations

import asyncio
import gc
import weakref
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from agent_sdk.api import AgentSDK
from agent_sdk.errors import AgentSDKError, ErrorCode, SessionBusyError
from agent_sdk.events.models import EventEnvelope
from agent_sdk.models.litellm_gateway import ModelRequest, ToolCallCompleted
from agent_sdk.permissions.models import PermissionDecision
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import (
    _model_request_fingerprint,
    _tool_request_fingerprint,
)
from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
)
from agent_sdk.runtime.models import AgentSpec, RunSnapshot, RunStatus, TokenUsage
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
    ReconciliationAction,
    ReconciliationRequest,
    ReconciliationResolution,
    ReconciliationStatus,
    RunCheckpoint,
    RunCheckpointPhase,
    ToolCallOperation,
)
from agent_sdk.runtime.recovery import RecoveryScanner
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    RunProgressBatch,
    SnapshotWrite,
    StoredEvent,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools.models import ToolContext, ToolResult, ToolResultStatus, ToolSpec
from agent_sdk.workflow import WorkflowExecutor

_GENERAL_SYSTEM_PROMPT = (
    resources.files("agent_sdk.prompts.profiles")
    .joinpath("general", "system.md")
    .read_text(encoding="utf-8")
)


def _assert_general_prompt_then_messages(
    actual: object,
    expected: list[dict[str, Any]],
) -> None:
    assert isinstance(actual, list)
    assert actual[0] == {
        "role": "system",
        "content": _GENERAL_SYSTEM_PROMPT,
    }
    assert actual[1:] == expected


async def _unused_acompletion(**_: object) -> Any:
    raise AssertionError("provider must not be called")


async def _success_chunks() -> AsyncIterator[dict[str, object]]:
    yield {
        "choices": [{"delta": {"content": "recovered"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
        },
    }


def _sdk_traceback_locals(error: BaseException) -> tuple[dict[str, Any], ...]:
    frames: list[dict[str, Any]] = []
    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in filename:
            frames.append(dict(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return tuple(frames)


class _RecoveryProgressFaultStore(InMemoryStore):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.enabled = False
        self.calls: list[RunProgressBatch] = []
        self.commit_reached = asyncio.Event()
        self.allow_commit = asyncio.Event()

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        recovery_start = any(
            event.type == "run.recovery.started" for event in batch.events
        )
        target = batch.reconciliation is not None or (
            self.mode.startswith("resume_") and recovery_start
        )
        if not self.enabled or not target:
            return await super().commit_run_progress(batch)
        self.calls.append(batch)
        if self.mode in {"precommit", "resume_precommit"} and len(self.calls) <= 2:
            raise RuntimeError("recovery-precommit-secret")
        if self.mode in {"barrier", "cancel", "resume_cancel"}:
            self.commit_reached.set()
            await self.allow_commit.wait()
        result = await super().commit_run_progress(batch)
        if self.mode in {"ambiguous", "resume_ambiguous"} and len(self.calls) == 1:
            raise RuntimeError("recovery-ambiguous-secret")
        return result


class _BlockingRecoveryReleaseStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.block_release = False
        self.release_calls = 0
        self.release_reached = asyncio.Event()
        self.allow_release = asyncio.Event()

    async def release_lease(self, lease: Any) -> None:
        if not self.block_release:
            await super().release_lease(lease)
            return
        self.release_calls += 1
        self.release_reached.set()
        await self.allow_release.wait()
        await super().release_lease(lease)
        raise RuntimeError("recovery-release-secret")


class _FailOwnerTerminalProgressStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.reject_failure = False

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        if self.reject_failure and any(
            event.type == "run.failed" for event in batch.events
        ):
            raise RuntimeError("owner-terminal-precommit-secret")
        return await super().commit_run_progress(batch)


class _RejectCompletedTerminalMemoryStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.reject_terminal = True

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        if self.reject_terminal and any(
            event.type == "run.completed" for event in batch.events
        ):
            raise RuntimeError("completed-terminal-precommit-secret")
        return await super().commit_run_progress(batch)


class _RejectCompletedTerminalSQLiteStore(SQLiteStore):
    def __init__(self, connection: Any) -> None:
        super().__init__(connection)
        self.reject_terminal = True

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        if self.reject_terminal and any(
            event.type == "run.completed" for event in batch.events
        ):
            raise RuntimeError("completed-terminal-precommit-secret")
        return await super().commit_run_progress(batch)


class _FollowerReadCountingStore:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.first_lease_read = asyncio.Event()
        self.snapshot_reads = 0
        self.lease_reads = 0
        self._counting = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def begin_counting(self) -> None:
        self.snapshot_reads = 0
        self.lease_reads = 0
        self._counting = True

    async def get_snapshot(
        self,
        kind: str,
        entity_id: str,
    ) -> dict[str, Any] | None:
        if self._counting and kind == "run":
            self.snapshot_reads += 1
        return await self._delegate.get_snapshot(kind, entity_id)

    async def get_run_lease(self, run_id: str) -> Any:
        if self._counting:
            self.lease_reads += 1
        lease = await self._delegate.get_run_lease(run_id)
        self.first_lease_read.set()
        return lease


class _EvidenceReadRecordingStore:
    def __init__(
        self,
        delegate: Any,
        *,
        injected_scoped_event: StoredEvent | None = None,
    ) -> None:
        self._delegate = delegate
        self._injected_scoped_event = injected_scoped_event
        self.read_calls: list[tuple[int, str | None, int | None, int | None]] = []
        self.returned_event_counts: list[int] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        events = await self._delegate.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )
        if session_id is not None and self._injected_scoped_event is not None:
            events = [*events, self._injected_scoped_event]
        self.read_calls.append((after_cursor, session_id, up_to_cursor, limit))
        self.returned_event_counts.append(len(events))
        return events


async def _seed_pristine_current_run(
    store: InMemoryStore,
    spec: AgentSpec,
    user_input: str = "resume me",
    tool_specs: tuple[ToolSpec, ...] = (),
    permission_default: str = "allow",
) -> str:
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    messages = ({"role": "user", "content": user_input},)
    descriptor = ExecutionDescriptor.create(
        agent=spec,
        messages=messages,
        tools=tuple(ToolCapabilityDescriptor.from_spec(item) for item in tool_specs),
        policy=ExecutionPolicyDescriptor.create(
            permission_default=permission_default
        ),
    )
    outcome = await commands.start_run(
        session.session_id,
        agent_revision=f"{spec.name}:{spec.revision}",
        user_input=user_input,
        execution_descriptor=descriptor,
    )
    return outcome.value.run_id


async def _seed_nonpristine_current_run(
    store: InMemoryStore,
    spec: AgentSpec,
) -> str:
    run_id = await _seed_pristine_current_run(store, spec)
    data = await store.get_snapshot("run", run_id)
    assert data is not None
    run = RunSnapshot.model_validate(data)
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.unexpected",
                    session_id=run.session_id,
                    run_id=run_id,
                    sequence=2,
                    payload={"bounded": True},
                ),
            ),
        )
    )
    return run_id


async def _seed_ready_model_interrupted(
    store: Any,
    spec: AgentSpec,
) -> tuple[str, RunCheckpoint]:
    run_id = await _seed_pristine_current_run(store, spec)
    run_data = await store.get_snapshot("run", run_id)
    assert run_data is not None
    created = RunSnapshot.model_validate(run_data)
    running = created.model_copy(update={"status": RunStatus.RUNNING, "version": 2})
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.started",
                    session_id=running.session_id,
                    run_id=running.run_id,
                    sequence=2,
                    payload={"status": "running"},
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "run",
                    running.run_id,
                    running.session_id,
                    running.version,
                    running.model_dump(mode="json"),
                ),
            ),
        )
    )
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    lease = await store.acquire_lease(
        run_id=run_id,
        owner="crashed-owner",
        now=now,
        expires_at=now + timedelta(seconds=30),
    )
    initial_messages = ({"role": "user", "content": "resume me"},)
    initial_checkpoint = RunCheckpoint(
        run_id=run_id,
        session_id=running.session_id,
        checkpoint_version=1,
        turn=0,
        phase=RunCheckpointPhase.READY_FOR_MODEL,
        messages=initial_messages,
    )
    await store.put_run_checkpoint(
        initial_checkpoint,
        expected=None,
        lease=lease,
        now=now,
    )
    model_request = ModelRequest(
        model=spec.model,
        messages=initial_messages,
        params=dict(spec.model_params),
    )
    started_model = ModelCallOperation(
        operation_id="op_ready_model_prior",
        session_id=running.session_id,
        run_id=run_id,
        turn=0,
        request_fingerprint=_model_request_fingerprint(model_request),
        lease_generation=lease.generation,
        status=ExternalOperationStatus.STARTED,
        provider_identity=spec.model,
        recovery_metadata={
            "authoritative_status": False,
            "same_operation_id_resend": False,
        },
    )
    await store.create_external_operation(started_model, lease=lease, now=now)
    model_in_flight = initial_checkpoint.model_copy(
        update={
            "checkpoint_version": 2,
            "phase": RunCheckpointPhase.MODEL_IN_FLIGHT,
            "operation_id": started_model.operation_id,
        }
    )
    await store.put_run_checkpoint(
        model_in_flight,
        expected=initial_checkpoint,
        lease=lease,
        now=now,
    )
    prior_usage = TokenUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7)
    prior_call = ToolCallCompleted(
        index=0,
        call_id="call_prior",
        name="lookup",
        arguments_json="{}",
    )
    completed_model = started_model.model_copy(
        update={
            "status": ExternalOperationStatus.COMPLETED,
            "outcome": {
                "finish_reason": "tool_calls",
                "text": "prior ",
                "tool_calls": [
                    {
                        "index": prior_call.index,
                        "call_id": prior_call.call_id,
                        "name": prior_call.name,
                        "arguments_json": prior_call.arguments_json,
                    }
                ],
                "usage": prior_usage.model_dump(mode="json"),
            },
        }
    )
    await store.transition_external_operation(
        expected=started_model,
        updated=completed_model,
        lease=lease,
        now=now,
    )
    assistant_message = {
        "role": "assistant",
        "content": "prior ",
        "tool_calls": [
            {
                "id": prior_call.call_id,
                "type": "function",
                "function": {
                    "name": prior_call.name,
                    "arguments": prior_call.arguments_json,
                },
            }
        ],
    }
    ready_for_tool = model_in_flight.model_copy(
        update={
            "checkpoint_version": 3,
            "phase": RunCheckpointPhase.READY_FOR_TOOL,
            "operation_id": None,
            "messages": (*initial_messages, assistant_message),
            "output_parts": ("prior ",),
            "usage": prior_usage,
        }
    )
    await store.put_run_checkpoint(
        ready_for_tool,
        expected=model_in_flight,
        lease=lease,
        now=now,
    )
    prior_result = ToolResult.normalized_error(
        prior_call.call_id,
        prior_call.name,
        ToolResultStatus.FAILED,
        "tool not found",
    )
    tool_message = {
        "role": "tool",
        "tool_call_id": prior_call.call_id,
        "name": prior_call.name,
        "content": prior_result.content,
    }
    checkpoint = ready_for_tool.model_copy(
        update={
            "checkpoint_version": 4,
            "turn": 1,
            "phase": RunCheckpointPhase.READY_FOR_MODEL,
            "messages": (*ready_for_tool.messages, tool_message),
            "tool_results": (prior_result,),
        }
    )
    await store.put_run_checkpoint(
        checkpoint,
        expected=ready_for_tool,
        lease=lease,
        now=now,
    )
    await store.release_lease(lease)
    history_events = tuple(
        EventEnvelope.new(
            type=event_type,
            session_id=running.session_id,
            run_id=run_id,
            sequence=index,
            payload=payload,
        )
        for index, (event_type, payload) in enumerate(
            (
                ("step.started", {}),
                ("model.call.started", {"model": spec.model}),
                ("model.text.delta", {"text": "prior "}),
                ("model.usage.reported", prior_usage.model_dump(mode="json")),
                ("model.call.completed", {"finish_reason": "tool_calls"}),
                (
                    "tool.call.proposed",
                    {"call_id": prior_call.call_id, "tool_name": prior_call.name},
                ),
                ("tool.call.completed", prior_result.model_dump(mode="json")),
                ("step.completed", {}),
            ),
            start=3,
        )
    )
    interrupted = running.model_copy(
        update={"status": RunStatus.INTERRUPTED, "version": 3}
    )
    await store.commit(
        CommitBatch(
            events=(
                *history_events,
                EventEnvelope.new(
                    type="run.interrupted",
                    session_id=interrupted.session_id,
                    run_id=interrupted.run_id,
                    sequence=3 + len(history_events),
                    payload={"status": "interrupted"},
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "run",
                    interrupted.run_id,
                    interrupted.session_id,
                    interrupted.version,
                    interrupted.model_dump(mode="json"),
                ),
            ),
        )
    )
    return run_id, checkpoint


async def _seed_ready_tool_interrupted(
    store: Any,
    spec: AgentSpec,
    tool_spec: ToolSpec,
    *,
    permission_default: str = "allow",
    relation_invalidity: str | None = None,
) -> tuple[str, RunCheckpoint]:
    run_id = await _seed_pristine_current_run(
        store,
        spec,
        tool_specs=(tool_spec,),
        permission_default=permission_default,
    )
    run_data = await store.get_snapshot("run", run_id)
    assert run_data is not None
    created = RunSnapshot.model_validate(run_data)
    running = created.model_copy(update={"status": RunStatus.RUNNING, "version": 2})
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.started",
                    session_id=running.session_id,
                    run_id=running.run_id,
                    sequence=2,
                    payload={"status": "running"},
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "run",
                    running.run_id,
                    running.session_id,
                    running.version,
                    running.model_dump(mode="json"),
                ),
            ),
        )
    )
    operation_text = "draft "
    operation_usage = TokenUsage(
        prompt_tokens=3,
        completion_tokens=1,
        total_tokens=4,
    )
    operation_call = {
        "index": 0,
        "call_id": "call_resume",
        "name": tool_spec.name,
        "arguments_json": '{"query":"value"}',
    }
    operation_outcome: dict[str, object] = {
        "finish_reason": "tool_calls",
        "text": operation_text,
        "tool_calls": [operation_call],
        "usage": operation_usage.model_dump(mode="json"),
    }
    if relation_invalidity == "outcome_text":
        operation_outcome["text"] = "different operation text"
    elif relation_invalidity == "outcome_usage":
        operation_outcome["usage"] = {
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
        }
    elif relation_invalidity in {
        "outcome_call_id",
        "outcome_call_name",
        "outcome_call_arguments",
    }:
        changed_call = dict(operation_call)
        changed_field = {
            "outcome_call_id": "call_id",
            "outcome_call_name": "name",
            "outcome_call_arguments": "arguments_json",
        }[relation_invalidity]
        changed_call[changed_field] = f"different-{changed_field}"
        operation_outcome["tool_calls"] = [changed_call]

    assistant_content = (
        "different checkpoint text"
        if relation_invalidity == "checkpoint_assistant"
        else operation_text
    )
    checkpoint_usage = (
        TokenUsage(prompt_tokens=30, completion_tokens=10, total_tokens=40)
        if relation_invalidity == "cumulative_usage"
        else operation_usage
    )
    checkpoint = RunCheckpoint(
        run_id=run_id,
        session_id=running.session_id,
        checkpoint_version=1,
        turn=0,
        phase=RunCheckpointPhase.READY_FOR_TOOL,
        messages=(
            {"role": "user", "content": "resume me"},
            {
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": [
                    {
                        "id": "call_resume",
                        "type": "function",
                        "function": {
                            "name": tool_spec.name,
                            "arguments": '{"query":"value"}',
                        },
                    }
                ],
            },
        ),
        output_parts=("draft ",),
        usage=checkpoint_usage,
    )
    model_request = ModelRequest(
        model=spec.model,
        messages=({"role": "user", "content": "resume me"},),
        tools=(
            {
                "type": "function",
                "function": {
                    "name": tool_spec.name,
                    "description": tool_spec.description,
                    "parameters": tool_spec.model_dump(mode="json")["input_schema"],
                },
            },
        ),
        params=dict(spec.model_params),
    )
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    lease = await store.acquire_lease(
        run_id=run_id,
        owner="crashed-owner",
        now=now,
        expires_at=now + timedelta(seconds=30),
    )
    model_events: list[EventEnvelope] = []
    operation_count = 2 if relation_invalidity == "duplicate" else 1
    if relation_invalidity != "missing":
        for index in range(operation_count):
            started = ModelCallOperation(
                operation_id=f"op_ready_tool_model_{index}",
                session_id=running.session_id,
                run_id=run_id,
                turn=0,
                request_fingerprint=_model_request_fingerprint(model_request),
                lease_generation=lease.generation,
                status=ExternalOperationStatus.STARTED,
                provider_identity=spec.model,
            )
            await store.create_external_operation(started, lease=lease, now=now)
            status = (
                ExternalOperationStatus.FAILED
                if relation_invalidity == "failed"
                else ExternalOperationStatus.COMPLETED
            )
            outcome: dict[str, object] = (
                {"error": {"code": "internal", "message": "model call failed"}}
                if status is ExternalOperationStatus.FAILED
                else operation_outcome
            )
            await store.transition_external_operation(
                expected=started,
                updated=started.model_copy(update={"status": status, "outcome": outcome}),
                lease=lease,
                now=now,
            )
            event_specs: tuple[tuple[str, dict[str, object]], ...] = (
                ("step.started", {}),
                ("model.call.started", {"model": spec.model}),
                ("model.text.delta", {"text": operation_text}),
                (
                    "model.usage.reported",
                    operation_usage.model_dump(mode="json"),
                ),
                (
                    "model.call.failed"
                    if status is ExternalOperationStatus.FAILED
                    else "model.call.completed",
                    (
                        {
                            "error": {
                                "code": "internal",
                                "message": "model call failed",
                            }
                        }
                        if status is ExternalOperationStatus.FAILED
                        else {
                            "finish_reason": (
                                "different-finish-reason"
                                if relation_invalidity == "completed_event"
                                else "tool_calls"
                            )
                        }
                    ),
                ),
            )
            event_sequence = 3 + len(model_events)
            model_events.extend(
                EventEnvelope.new(
                    type=event_type,
                    session_id=running.session_id,
                    run_id=run_id,
                    sequence=event_sequence + offset,
                    payload=payload,
                )
                for offset, (event_type, payload) in enumerate(event_specs)
            )
    if relation_invalidity == "event_tail":
        model_events.append(
            EventEnvelope.new(
                type="model.unexpected",
                session_id=running.session_id,
                run_id=run_id,
                sequence=3 + len(model_events),
                payload={"bounded": True},
            )
        )
    await store.put_run_checkpoint(checkpoint, expected=None, lease=lease, now=now)
    await store.release_lease(lease)
    if model_events:
        await store.commit(CommitBatch(events=tuple(model_events)))
    interrupted = running.model_copy(
        update={"status": RunStatus.INTERRUPTED, "version": 3}
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.interrupted",
                    session_id=interrupted.session_id,
                    run_id=interrupted.run_id,
                    sequence=3 + len(model_events),
                    payload={"status": "interrupted"},
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "run",
                    interrupted.run_id,
                    interrupted.session_id,
                    interrupted.version,
                    interrupted.model_dump(mode="json"),
                ),
            ),
        )
    )
    return run_id, checkpoint


async def _seed_multi_turn_ready_tool_interrupted(
    store: Any,
    spec: AgentSpec,
    tool_spec: ToolSpec,
    *,
    relation_invalidity: str | None = None,
) -> tuple[str, RunCheckpoint]:
    run_id = await _seed_pristine_current_run(
        store,
        spec,
        tool_specs=(tool_spec,),
        permission_default="allow",
    )
    run_data = await store.get_snapshot("run", run_id)
    assert run_data is not None
    created = RunSnapshot.model_validate(run_data)
    running = created.model_copy(update={"status": RunStatus.RUNNING, "version": 2})
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.started",
                    session_id=running.session_id,
                    run_id=run_id,
                    sequence=2,
                    payload={"status": "running"},
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "run",
                    run_id,
                    running.session_id,
                    running.version,
                    running.model_dump(mode="json"),
                ),
            ),
        )
    )

    tool_schema = (
        {
            "type": "function",
            "function": {
                "name": tool_spec.name,
                "description": tool_spec.description,
                "parameters": tool_spec.model_dump(mode="json")["input_schema"],
            },
        },
    )
    initial_messages: tuple[dict[str, object], ...] = (
        {"role": "user", "content": "resume me"},
    )
    historical_text = "historical "
    pending_text = "pending "
    historical_usage = TokenUsage(
        prompt_tokens=2,
        completion_tokens=1,
        total_tokens=3,
    )
    pending_usage = TokenUsage(
        prompt_tokens=3,
        completion_tokens=1,
        total_tokens=4,
    )
    historical_call = ToolCallCompleted(
        index=0,
        call_id="call_historical",
        name=tool_spec.name,
        arguments_json='{"query":"historical"}',
    )
    pending_call = ToolCallCompleted(
        index=0,
        call_id="call_pending",
        name=tool_spec.name,
        arguments_json='{"query":"pending"}',
    )
    historical_result = ToolResult.succeeded(
        historical_call.call_id,
        historical_call.name,
        {"answer": "historical"},
    )
    historical_assistant = {
        "role": "assistant",
        "content": historical_text,
        "tool_calls": [
            {
                "id": historical_call.call_id,
                "type": "function",
                "function": {
                    "name": historical_call.name,
                    "arguments": historical_call.arguments_json,
                },
            }
        ],
    }
    historical_tool_message = {
        "role": "tool",
        "tool_call_id": historical_call.call_id,
        "name": historical_call.name,
        "content": historical_result.content,
    }
    pending_assistant = {
        "role": "assistant",
        "content": pending_text,
        "tool_calls": [
            {
                "id": pending_call.call_id,
                "type": "function",
                "function": {
                    "name": pending_call.name,
                    "arguments": pending_call.arguments_json,
                },
            }
        ],
    }
    turn_one_messages = (
        *initial_messages,
        historical_assistant,
        historical_tool_message,
    )
    historical_request = ModelRequest(
        model=spec.model,
        messages=initial_messages,
        tools=tool_schema,
        params=dict(spec.model_params),
    )
    pending_request = ModelRequest(
        model=spec.model,
        messages=turn_one_messages,
        tools=tool_schema,
        params=dict(spec.model_params),
    )
    historical_fingerprint = _model_request_fingerprint(historical_request)
    pending_fingerprint = _model_request_fingerprint(pending_request)
    if relation_invalidity == "historical_request_fingerprint":
        historical_fingerprint = "0" * 64
    elif relation_invalidity == "historical_wrong_turn_request":
        historical_fingerprint = pending_fingerprint

    historical_outcome_call = {
        "index": historical_call.index,
        "call_id": historical_call.call_id,
        "name": historical_call.name,
        "arguments_json": historical_call.arguments_json,
    }
    if relation_invalidity == "historical_outcome_call":
        historical_outcome_call["arguments_json"] = '{"query":"forged"}'
    historical_operation_usage = historical_usage
    pending_operation_usage = pending_usage
    if relation_invalidity == "usage_distribution":
        historical_operation_usage = TokenUsage(
            prompt_tokens=3,
            completion_tokens=1,
            total_tokens=4,
        )
        pending_operation_usage = TokenUsage(
            prompt_tokens=2,
            completion_tokens=1,
            total_tokens=3,
        )
    historical_outcome = {
        "finish_reason": "tool_calls",
        "text": (
            "forged historical "
            if relation_invalidity == "historical_outcome_text"
            else historical_text
        ),
        "tool_calls": [historical_outcome_call],
        "usage": historical_operation_usage.model_dump(mode="json"),
    }
    pending_outcome = {
        "finish_reason": "tool_calls",
        "text": pending_text,
        "tool_calls": [
            {
                "index": pending_call.index,
                "call_id": pending_call.call_id,
                "name": pending_call.name,
                "arguments_json": pending_call.arguments_json,
            }
        ],
        "usage": pending_operation_usage.model_dump(mode="json"),
    }

    now = datetime(2026, 7, 16, 9, tzinfo=UTC)
    lease = await store.acquire_lease(
        run_id=run_id,
        owner="crashed-owner",
        now=now,
        expires_at=now + timedelta(seconds=30),
    )
    historical_model = ModelCallOperation(
        operation_id="op_ready_tool_model_historical",
        session_id=running.session_id,
        run_id=run_id,
        turn=0,
        request_fingerprint=historical_fingerprint,
        lease_generation=lease.generation,
        status=ExternalOperationStatus.STARTED,
        provider_identity=spec.model,
        recovery_metadata={
            "authoritative_status": False,
            "same_operation_id_resend": False,
        },
    )
    await store.create_external_operation(historical_model, lease=lease, now=now)
    await store.transition_external_operation(
        expected=historical_model,
        updated=historical_model.model_copy(
            update={
                "status": ExternalOperationStatus.COMPLETED,
                "outcome": historical_outcome,
            }
        ),
        lease=lease,
        now=now,
    )
    capability = ToolCapabilityDescriptor.from_spec(tool_spec)
    historical_tool = ToolCallOperation(
        operation_id="op_ready_tool_historical",
        session_id=running.session_id,
        run_id=run_id,
        turn=0,
        request_fingerprint=_tool_request_fingerprint(
            historical_call,
            capability,
            {"query": "historical"},
        ),
        lease_generation=lease.generation,
        status=ExternalOperationStatus.STARTED,
        tool_identity=capability.capability_hash,
        recovery_metadata={"safe_retry": False, "retry_class": "unsafe"},
    )
    await store.create_external_operation(historical_tool, lease=lease, now=now)
    await store.transition_external_operation(
        expected=historical_tool,
        updated=historical_tool.model_copy(
            update={
                "status": ExternalOperationStatus.COMPLETED,
                "outcome": historical_result.model_dump(mode="json"),
            }
        ),
        lease=lease,
        now=now,
    )
    pending_model = ModelCallOperation(
        operation_id="op_ready_tool_model_pending",
        session_id=running.session_id,
        run_id=run_id,
        turn=1,
        request_fingerprint=pending_fingerprint,
        lease_generation=lease.generation,
        status=ExternalOperationStatus.STARTED,
        provider_identity=spec.model,
        recovery_metadata={
            "authoritative_status": False,
            "same_operation_id_resend": False,
        },
    )
    await store.create_external_operation(pending_model, lease=lease, now=now)
    await store.transition_external_operation(
        expected=pending_model,
        updated=pending_model.model_copy(
            update={
                "status": ExternalOperationStatus.COMPLETED,
                "outcome": pending_outcome,
            }
        ),
        lease=lease,
        now=now,
    )

    checkpoint_messages = [
        *initial_messages,
        historical_assistant,
        historical_tool_message,
        pending_assistant,
    ]
    if relation_invalidity == "historical_checkpoint_assistant":
        checkpoint_messages[1] = {
            **historical_assistant,
            "content": "forged checkpoint historical",
        }
    elif relation_invalidity == "historical_tool_message":
        checkpoint_messages[2] = {
            **historical_tool_message,
            "content": '{"answer":"forged"}',
        }
    elif relation_invalidity == "historical_message_order":
        checkpoint_messages[1], checkpoint_messages[2] = (
            checkpoint_messages[2],
            checkpoint_messages[1],
        )
    checkpoint = RunCheckpoint(
        run_id=run_id,
        session_id=running.session_id,
        checkpoint_version=1,
        turn=1,
        phase=RunCheckpointPhase.READY_FOR_TOOL,
        messages=tuple(checkpoint_messages),
        output_parts=(
            "forged output "
            if relation_invalidity == "historical_output"
            else historical_text,
            pending_text,
        ),
        usage=TokenUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
        tool_results=(historical_result,),
    )
    await store.put_run_checkpoint(checkpoint, expected=None, lease=lease, now=now)
    await store.release_lease(lease)

    event_items: list[tuple[str, dict[str, object]]] = [
        ("step.started", {}),
        ("model.call.started", {"model": spec.model}),
        ("model.text.delta", {"text": historical_text}),
        ("model.usage.reported", historical_usage.model_dump(mode="json")),
        ("model.call.completed", {"finish_reason": "tool_calls"}),
        (
            "tool.call.proposed",
            {"call_id": historical_call.call_id, "tool_name": historical_call.name},
        ),
        (
            "tool.call.authorized",
            {"call_id": historical_call.call_id, "tool_name": historical_call.name},
        ),
        (
            "tool.call.started",
            {"call_id": historical_call.call_id, "tool_name": historical_call.name},
        ),
        ("tool.call.completed", historical_result.model_dump(mode="json")),
        ("step.completed", {}),
        ("step.started", {}),
        ("model.call.started", {"model": spec.model}),
        ("model.text.delta", {"text": pending_text}),
        ("model.usage.reported", pending_usage.model_dump(mode="json")),
        ("model.call.completed", {"finish_reason": "tool_calls"}),
    ]
    if relation_invalidity == "historical_started_payload":
        event_items[1] = ("model.call.started", {"model": "forged/provider"})
    elif relation_invalidity == "historical_completed_payload":
        event_items[4] = ("model.call.completed", {"finish_reason": "stop"})
    elif relation_invalidity == "historical_event_order":
        event_items[1], event_items[4] = event_items[4], event_items[1]
    elif relation_invalidity == "historical_event_wrong_turn":
        historical_started = event_items.pop(1)
        event_items.insert(11, historical_started)
    events = tuple(
        EventEnvelope.new(
            type=event_type,
            session_id=running.session_id,
            run_id=run_id,
            sequence=index + 3,
            payload=payload,
        )
        for index, (event_type, payload) in enumerate(event_items)
    )
    interrupted = running.model_copy(
        update={"status": RunStatus.INTERRUPTED, "version": 3}
    )
    await store.commit(
        CommitBatch(
            events=(
                *events,
                EventEnvelope.new(
                    type="run.interrupted",
                    session_id=running.session_id,
                    run_id=run_id,
                    sequence=3 + len(events),
                    payload={"status": "interrupted"},
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "run",
                    run_id,
                    running.session_id,
                    interrupted.version,
                    interrupted.model_dump(mode="json"),
                ),
            ),
        )
    )
    return run_id, checkpoint


async def _seed_reconciliation_case(
    store: InMemoryStore,
    spec: AgentSpec,
    scenario: str,
) -> tuple[str, str | None]:
    if scenario == "legacy":
        commands = RuntimeCommands(store)
        session = await commands.create_session(workspaces=[])
        outcome = await commands.start_run(
            session.session_id,
            agent_revision=f"{spec.name}:{spec.revision}",
            user_input="resume me",
        )
        return outcome.value.run_id, None

    run_id = await _seed_pristine_current_run(store, spec)
    run_data = await store.get_snapshot("run", run_id)
    assert run_data is not None
    created = RunSnapshot.model_validate(run_data)
    running = created.model_copy(update={"status": RunStatus.RUNNING, "version": 2})
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.started",
                    session_id=running.session_id,
                    run_id=running.run_id,
                    sequence=2,
                    payload={"status": "running"},
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "run",
                    running.run_id,
                    running.session_id,
                    running.version,
                    running.model_dump(mode="json"),
                ),
            ),
        )
    )
    operation_id: str | None = None
    if scenario != "missing_checkpoint":
        phase = {
            "model_in_flight": RunCheckpointPhase.MODEL_IN_FLIGHT,
            "tool_in_flight": RunCheckpointPhase.TOOL_IN_FLIGHT,
            "waiting": RunCheckpointPhase.WAITING,
        }[scenario]
        now = datetime(2026, 7, 15, 13, tzinfo=UTC)
        lease = await store.acquire_lease(
            run_id=run_id,
            owner="crashed-owner",
            now=now,
            expires_at=now + timedelta(seconds=30),
        )
        operation = None
        if scenario == "model_in_flight":
            operation = ModelCallOperation(
                operation_id="op_unknown_model",
                session_id=running.session_id,
                run_id=run_id,
                turn=0,
                request_fingerprint="sha256:model",
                lease_generation=lease.generation,
                status=ExternalOperationStatus.STARTED,
                provider_identity="fake/recovery",
            )
        elif scenario == "tool_in_flight":
            operation = ToolCallOperation(
                operation_id="op_unknown_tool",
                session_id=running.session_id,
                run_id=run_id,
                turn=0,
                request_fingerprint="sha256:tool",
                lease_generation=lease.generation,
                status=ExternalOperationStatus.STARTED,
                tool_identity="sha256:tool-capability",
                recovery_metadata={"safe_retry": False},
            )
        if operation is not None:
            operation_id = operation.operation_id
            await store.create_external_operation(operation, lease=lease, now=now)
        checkpoint = RunCheckpoint(
            run_id=run_id,
            session_id=running.session_id,
            checkpoint_version=1,
            turn=0,
            phase=phase,
            operation_id=operation_id,
            messages=({"role": "user", "content": "resume me"},),
        )
        await store.put_run_checkpoint(
            checkpoint,
            expected=None,
            lease=lease,
            now=now,
        )
        await store.release_lease(lease)
    interrupted = running.model_copy(
        update={"status": RunStatus.INTERRUPTED, "version": 3}
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.interrupted",
                    session_id=interrupted.session_id,
                    run_id=interrupted.run_id,
                    sequence=3,
                    payload={"status": "interrupted"},
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "run",
                    interrupted.run_id,
                    interrupted.session_id,
                    interrupted.version,
                    interrupted.model_dump(mode="json"),
                ),
            ),
        )
    )
    return run_id, operation_id


@pytest.mark.asyncio
async def test_running_loop_construction_tracks_one_startup_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_started = asyncio.Event()
    allow_scan = asyncio.Event()
    calls = 0

    async def controlled_scan(_scanner: RecoveryScanner) -> None:
        nonlocal calls
        calls += 1
        scan_started.set()
        await allow_scan.wait()

    monkeypatch.setattr(RecoveryScanner, "scan", controlled_scan)
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unused_acompletion,
    )
    try:
        await asyncio.wait_for(scan_started.wait(), timeout=2)

        assert calls == 1
        assert len(sdk._active_tasks) == 1
    finally:
        allow_scan.set()
        await sdk.close()


def test_sync_construction_defers_scan_until_first_recovery_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def counted_scan(_scanner: RecoveryScanner) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(RecoveryScanner, "scan", counted_scan)
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unused_acompletion,
    )

    assert calls == 0
    assert sdk._startup_scan_task is None

    async def exercise() -> None:
        await sdk.recovery.scan()
        assert calls == 1
        await sdk.close()

    asyncio.run(exercise())


@pytest.mark.asyncio
async def test_scan_attaches_to_startup_then_later_runs_new_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup_started = asyncio.Event()
    allow_startup = asyncio.Event()
    calls = 0

    async def controlled_scan(_scanner: RecoveryScanner) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            startup_started.set()
            await allow_startup.wait()

    monkeypatch.setattr(RecoveryScanner, "scan", controlled_scan)
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unused_acompletion,
    )
    try:
        await asyncio.wait_for(startup_started.wait(), timeout=2)
        attached = asyncio.create_task(sdk.recovery.scan())
        allow_startup.set()
        await asyncio.wait_for(attached, timeout=2)

        assert calls == 1

        await sdk.recovery.scan()

        assert calls == 2
    finally:
        allow_startup.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_startup_lazy_open_failure_is_sanitized_and_close_settles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "startup-db-secret-91f2"

    async def fail_open(_cls: type[SQLiteStore], path: str | Path) -> SQLiteStore:
        raise RuntimeError(f"{secret}:{path}")

    monkeypatch.setattr(SQLiteStore, "open", classmethod(fail_open))
    sdk = AgentSDK.for_test(
        database_path=tmp_path / secret,
        acompletion=_unused_acompletion,
    )
    try:
        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.scan()

        assert caught.value.code is ErrorCode.INTERNAL
        assert caught.value.message == "failed to scan abandoned runs"
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        frames = _sdk_traceback_locals(caught.value)
        assert frames
        assert all(secret not in repr(frame) for frame in frames)
    finally:
        await sdk.close()

    assert sdk._active_tasks == set()


@pytest.mark.asyncio
async def test_close_survives_repeated_waiter_cancel_during_startup_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_started = asyncio.Event()
    allow_scan = asyncio.Event()

    async def controlled_scan(_scanner: RecoveryScanner) -> None:
        scan_started.set()
        await allow_scan.wait()

    monkeypatch.setattr(RecoveryScanner, "scan", controlled_scan)
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unused_acompletion,
    )
    await asyncio.wait_for(scan_started.wait(), timeout=2)
    close_waiter = asyncio.create_task(sdk.close())
    close_waiter.cancel()
    close_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_waiter

    allow_scan.set()
    await asyncio.wait_for(sdk.close(), timeout=2)
    await asyncio.sleep(0)

    assert sdk._active_tasks == set()


@pytest.mark.asyncio
async def test_missing_agent_capability_mutates_nothing_then_exact_registration_recovers(
) -> None:
    store = InMemoryStore()
    spec = AgentSpec(
        name="recoverable",
        model="fake/recovery",
        model_params={"temperature": 0.2},
    )
    run_id = await _seed_pristine_current_run(store, spec)
    cursor_before = await store.latest_cursor()
    provider_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        return _success_chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    try:
        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.recover_run(run_id)

        assert caught.value.code is ErrorCode.INVALID_STATE
        assert caught.value.message == "recovery capabilities unavailable"
        assert caught.value.retryable is False
        assert provider_calls == 0
        assert await store.latest_cursor() == cursor_before
        assert await store.get_run_checkpoint(run_id) is None
        assert await store.list_pending_reconciliation_requests(run_id) == ()
        run_before = await sdk.runs.get(run_id)
        assert run_before.status is RunStatus.CREATED
        assert run_before.version == 1
        assert store._leases == {}

        sdk.agents.define(spec)
        handle = await sdk.recovery.recover_run(run_id)
        result = await handle.result()

        assert result.output_text == "recovered"
        assert provider_calls == 1
        assert (await sdk.runs.get(run_id)).status is RunStatus.COMPLETED
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_twenty_same_sdk_recoveries_share_exact_coordinator_task() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="deduplicated", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)
    provider_started = asyncio.Event()
    allow_provider = asyncio.Event()
    provider_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        provider_started.set()
        await allow_provider.wait()
        return _success_chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handles = await asyncio.gather(
            *(sdk.recovery.recover_run(run_id) for _ in range(20))
        )

        assert len({id(handle._task) for handle in handles}) == 1
        await asyncio.wait_for(provider_started.wait(), timeout=2)
        assert provider_calls == 1

        coordinator = handles[0]._task
        assert coordinator is not None
        registry_released = asyncio.Event()
        coordinator.add_done_callback(lambda _task: registry_released.set())
        allow_provider.set()
        results = await asyncio.gather(*(handle.result() for handle in handles))
        await asyncio.wait_for(registry_released.wait(), timeout=2)

        assert {result.output_text for result in results} == {"recovered"}
        assert sdk.recovery._tasks == {}
    finally:
        allow_provider.set()
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("success", "failure", "cancel"))
async def test_recovery_registry_releases_and_collects_settled_coordinator(
    outcome: str,
) -> None:
    store = InMemoryStore()
    spec = AgentSpec(name=f"registry-{outcome}", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)
    provider_started = asyncio.Event()
    allow_provider = asyncio.Event()

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_started.set()
        if outcome == "failure":
            raise RuntimeError("provider-registry-secret")
        await allow_provider.wait()
        return _success_chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        task = handle._task
        assert task is not None
        await asyncio.wait_for(provider_started.wait(), timeout=2)
        if outcome == "cancel":
            task.cancel()
            task.cancel()
        else:
            allow_provider.set()
        try:
            await handle.result()
        except AgentSDKError as error:
            assert outcome != "success"
            assert error.__cause__ is None
            assert error.__context__ is None
            del error

        for _ in range(3):
            await asyncio.sleep(0)
        assert sdk.recovery._tasks == {}
        assert task not in sdk._active_tasks

        task_reference = weakref.ref(task)
        del handle
        del task
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
        assert task_reference() is None
    finally:
        allow_provider.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_non_pristine_created_atomically_enters_reconciliation_once() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="non-pristine", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)
    run_data = await store.get_snapshot("run", run_id)
    assert run_data is not None
    run = RunSnapshot.model_validate(run_data)
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.unexpected",
                    session_id=run.session_id,
                    run_id=run.run_id,
                    sequence=2,
                    payload={"bounded": True},
                ),
            ),
        )
    )
    provider_calls = 0

    async def completion(**_: object) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("provider must not be called")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError) as caught:
            await handle.result()

        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery required"
        assert caught.value.retryable is True
        assert provider_calls == 0
        durable = await sdk.runs.get(run_id)
        assert durable.status is RunStatus.WAITING_RECONCILIATION
        assert durable.version == 3
        requests = await sdk.recovery.pending_requests(run_id)
        assert len(requests) == 1
        request = requests[0]
        assert request.run_id == run_id
        assert request.session_id == run.session_id
        assert request.operation_id is None
        assert request.reason == "created_not_pristine"
        assert request.details == {"run_status": "created"}
        events = [
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ]
        assert [(event.type, event.sequence) for event in events] == [
            ("run.created", 1),
            ("run.unexpected", 2),
            ("reconciliation.requested", 3),
        ]
        assert events[-1].payload == {
            "request_id": request.request_id,
            "operation_id": None,
            "reason": "created_not_pristine",
        }

        repeated = await sdk.recovery.recover_run(run_id)
        assert repeated.attached is False
        assert await sdk.recovery.pending_requests(run_id) == requests
        assert len(
            [
                stored
                for stored in await store.read_events(after_cursor=0)
                if stored.event.run_id == run_id
            ]
        ) == 3
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    ("checkpoint", "operation", "missing_created", "changed_created"),
)
async def test_created_requires_exact_pristine_durable_evidence(evidence: str) -> None:
    store = InMemoryStore()
    spec = AgentSpec(name=f"created-{evidence}", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)
    run = RunSnapshot.model_validate(await store.get_snapshot("run", run_id))
    if evidence in {"checkpoint", "operation"}:
        now = datetime(2026, 7, 15, 14, tzinfo=UTC)
        lease = await store.acquire_lease(
            run_id=run_id,
            owner="prior-owner",
            now=now,
            expires_at=now + timedelta(seconds=30),
        )
        if evidence == "checkpoint":
            await store.put_run_checkpoint(
                RunCheckpoint(
                    run_id=run_id,
                    session_id=run.session_id,
                    checkpoint_version=1,
                    turn=0,
                    phase=RunCheckpointPhase.READY_FOR_MODEL,
                    messages=({"role": "user", "content": "resume me"},),
                ),
                expected=None,
                lease=lease,
                now=now,
            )
        else:
            await store.create_external_operation(
                ModelCallOperation(
                    operation_id="op_prior",
                    session_id=run.session_id,
                    run_id=run_id,
                    turn=0,
                    request_fingerprint="sha256:prior",
                    lease_generation=lease.generation,
                    status=ExternalOperationStatus.STARTED,
                    provider_identity="fake/recovery",
                ),
                lease=lease,
                now=now,
            )
        await store.release_lease(lease)
    elif evidence == "missing_created":
        store._events = [
            stored for stored in store._events if stored.event.run_id != run_id
        ]
    else:
        store._events = [
            StoredEvent(
                stored.cursor,
                stored.event.model_copy(update={"payload": {"changed": True}}),
            )
            if stored.event.run_id == run_id
            else stored
            for stored in store._events
        ]

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await handle.result()

        assert (await sdk.runs.get(run_id)).status is RunStatus.WAITING_RECONCILIATION
        requests = await sdk.recovery.pending_requests(run_id)
        assert len(requests) == 1
        assert requests[0].reason == "created_not_pristine"
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_terminal_recovery_is_detached_without_capability_or_external_work() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="terminal-detached", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        return _success_chunks()

    first = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    first.agents.define(spec)
    try:
        completed = await (await first.recovery.recover_run(run_id)).result()
    finally:
        await first.close()

    async def external_trap(**_: object) -> Any:
        raise AssertionError("terminal recovery must not call provider")

    reopened = AgentSDK.for_test(
        store=store,
        acompletion=external_trap,
        permission_default="deny",
    )
    try:
        handle = await reopened.recovery.recover_run(run_id)

        assert handle.attached is False
        assert await handle.result() == completed
        assert store._leases == {}
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_startup_scan_and_explicit_recovery_do_not_invoke_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def workflow_trap(*_: object, **__: object) -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("workflow must not be invoked")

    monkeypatch.setattr(WorkflowExecutor, "start", workflow_trap)
    monkeypatch.setattr(WorkflowExecutor, "resume", workflow_trap)
    store = InMemoryStore()
    spec = AgentSpec(name="no-workflow", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        return _success_chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        await sdk.recovery.scan()
        await (await sdk.recovery.recover_run(run_id)).result()
        assert calls == 0
    finally:
        await sdk.close()


@pytest.mark.parametrize(
    ("scenario", "reason"),
    (
        ("legacy", "legacy_unknown"),
        ("missing_checkpoint", "legacy_checkpoint_missing"),
        ("model_in_flight", "model_call_unknown_outcome"),
        ("tool_in_flight", "tool_call_unknown_outcome"),
        ("waiting", "permission_wait_lost"),
    ),
)
@pytest.mark.asyncio
async def test_unsafe_recovery_cases_enter_bounded_reconciliation(
    scenario: str,
    reason: str,
) -> None:
    store = InMemoryStore()
    spec = AgentSpec(name=f"case-{scenario}", model="fake/recovery")
    run_id, operation_id = await _seed_reconciliation_case(store, spec, scenario)
    provider_calls = 0

    async def completion(**_: object) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("provider must not be called")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    if scenario != "legacy":
        sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError) as caught:
            await handle.result()

        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery required"
        assert provider_calls == 0
        requests = await sdk.recovery.pending_requests(run_id)
        assert len(requests) == 1
        assert requests[0].reason == reason
        assert requests[0].operation_id == operation_id
        assert set(requests[0].details) <= {"run_status", "checkpoint_phase"}
        durable = await sdk.runs.get(run_id)
        assert durable.status is RunStatus.WAITING_RECONCILIATION
        session = await sdk.sessions.get(durable.session_id)
        assert run_id in session.active_run_ids
        closed = await sdk.sessions.close(durable.session_id)
        assert run_id in closed.active_run_ids
        with pytest.raises(SessionBusyError):
            await sdk.sessions.delete(durable.session_id)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_missing_checkpoint_operation_relationship_fails_closed() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="malformed-link", model="fake/recovery")
    run_id, operation_id = await _seed_reconciliation_case(
        store,
        spec,
        "model_in_flight",
    )
    assert operation_id is not None
    store._external_operations.pop(operation_id)
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError) as caught:
            await handle.result()

        assert caught.value.code is ErrorCode.CONFLICT
        requests = await sdk.recovery.pending_requests(run_id)
        assert len(requests) == 1
        assert requests[0].reason == "recovery_state_invalid"
        assert requests[0].operation_id is None
        assert (await sdk.runs.get(run_id)).status is RunStatus.WAITING_RECONCILIATION
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("precommit", "ambiguous"))
async def test_reconciliation_commit_fault_is_all_or_none_and_replay_safe(
    fault: str,
) -> None:
    store = _RecoveryProgressFaultStore(fault)
    spec = AgentSpec(name=f"fault-{fault}", model="fake/recovery")
    run_id = await _seed_nonpristine_current_run(store, spec)
    store.enabled = True
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await handle.result()

        if fault == "precommit":
            assert (await sdk.runs.get(run_id)).status is RunStatus.CREATED
            assert await store.list_pending_reconciliation_requests(run_id) == ()
            assert not any(
                stored.event.type == "reconciliation.requested"
                for stored in await store.read_events(after_cursor=0)
            )
            store.enabled = False
            await asyncio.sleep(0)
            retry = await sdk.recovery.recover_run(run_id)
            with pytest.raises(AgentSDKError):
                await retry.result()
        else:
            assert len(store.calls) == 2
            assert store.calls[0] is store.calls[1]

        assert (await sdk.runs.get(run_id)).status is RunStatus.WAITING_RECONCILIATION
        assert len(await sdk.recovery.pending_requests(run_id)) == 1
        assert len(
            [
                stored
                for stored in await store.read_events(after_cursor=0)
                if stored.event.type == "reconciliation.requested"
            ]
        ) == 1
        assert store._leases == {}
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_two_sdks_admit_one_reconciliation_request_and_event() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="cross-reconcile", model="fake/recovery")
    run_id = await _seed_nonpristine_current_run(store, spec)
    sdks = tuple(
        AgentSDK.for_test(
            store=store,
            acompletion=_unused_acompletion,
            permission_default="allow",
        )
        for _ in range(2)
    )
    for sdk in sdks:
        sdk.agents.define(spec)
    try:
        handles = await asyncio.gather(
            *(sdk.recovery.recover_run(run_id) for sdk in sdks)
        )
        outcomes = await asyncio.gather(
            *(handle.result() for handle in handles),
            return_exceptions=True,
        )

        assert all(isinstance(outcome, AgentSDKError) for outcome in outcomes)
        assert len(await sdks[0].recovery.pending_requests(run_id)) == 1
        assert len(
            [
                stored
                for stored in await store.read_events(after_cursor=0)
                if stored.event.type == "reconciliation.requested"
            ]
        ) == 1
    finally:
        await asyncio.gather(*(sdk.close() for sdk in sdks))


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ("session_delete", "lease_takeover"))
async def test_reconciliation_commit_race_has_no_partial_state(race: str) -> None:
    store = _RecoveryProgressFaultStore("barrier")
    spec = AgentSpec(name=f"race-{race}", model="fake/recovery")
    run_id = await _seed_nonpristine_current_run(store, spec)
    run = RunSnapshot.model_validate(await store.get_snapshot("run", run_id))
    store.enabled = True
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    takeover = None
    try:
        handle = await sdk.recovery.recover_run(run_id)
        await asyncio.wait_for(store.commit_reached.wait(), timeout=2)
        if race == "session_delete":
            await store.delete_session(run.session_id)
        else:
            current = store._leases[run_id]
            takeover_now = current.expires_at + timedelta(seconds=1)
            takeover = await store.acquire_lease(
                run_id=run_id,
                owner="takeover",
                now=takeover_now,
                expires_at=takeover_now + timedelta(seconds=30),
            )
        store.allow_commit.set()
        with pytest.raises(AgentSDKError):
            await handle.result()

        assert await store.list_pending_reconciliation_requests(run_id) == ()
        assert not any(
            stored.event.type == "reconciliation.requested"
            for stored in await store.read_events(after_cursor=0)
        )
        if race == "session_delete":
            assert await store.get_snapshot("run", run_id) is None
        else:
            assert (await sdk.runs.get(run_id)).status is RunStatus.CREATED
    finally:
        store.allow_commit.set()
        if takeover is not None:
            await store.release_lease(takeover)
        await sdk.close()


@pytest.mark.asyncio
async def test_reconciliation_double_cancel_settles_atomic_commit_and_release() -> None:
    store = _RecoveryProgressFaultStore("cancel")
    spec = AgentSpec(name="cancel-admission", model="fake/recovery")
    run_id = await _seed_nonpristine_current_run(store, spec)
    store.enabled = True
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        task = handle._task
        assert task is not None
        await asyncio.wait_for(store.commit_reached.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        store.allow_commit.set()
        with pytest.raises(AgentSDKError):
            await handle.result()
        for _ in range(3):
            await asyncio.sleep(0)

        assert (await sdk.runs.get(run_id)).status is RunStatus.WAITING_RECONCILIATION
        assert len(await store.list_pending_reconciliation_requests(run_id)) == 1
        assert len(store.calls) == 1
        assert store._leases == {}
        assert sdk.recovery._tasks == {}
    finally:
        store.allow_commit.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_reconciliation_double_cancel_settles_one_late_failing_release() -> None:
    store = _BlockingRecoveryReleaseStore()
    spec = AgentSpec(name="cancel-release", model="fake/recovery")
    run_id = await _seed_nonpristine_current_run(store, spec)
    store.block_release = True
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        task = handle._task
        assert task is not None
        await asyncio.wait_for(store.release_reached.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        store.allow_release.set()
        with pytest.raises(AgentSDKError):
            await handle.result()
        for _ in range(3):
            await asyncio.sleep(0)

        assert store.release_calls == 1
        assert store._leases == {}
        assert sdk.recovery._tasks == {}
        assert not any(
            pending is not asyncio.current_task()
            and "release" in repr(pending.get_coro()).casefold()
            for pending in asyncio.all_tasks()
        )
    finally:
        store.allow_release.set()
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ("multiple", "foreign", "resolved_only", "missing", "status_disagreement"),
)
async def test_pending_request_corruption_is_constant_and_secret_free(
    corruption: str,
) -> None:
    secret = f"pending-secret-{corruption}-4d7a"
    store = InMemoryStore()
    spec = AgentSpec(
        name=f"pending-{corruption}",
        model="fake/recovery",
        model_params={"opaque": secret},
    )
    run_id = await _seed_nonpristine_current_run(store, spec)
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        admitted = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await admitted.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]
        run = await sdk.runs.get(run_id)

        if corruption == "multiple":
            await store.create_reconciliation_request(
                ReconciliationRequest(
                    request_id="rec_second",
                    session_id=request.session_id,
                    run_id=run_id,
                    reason="duplicate",
                    details={"opaque": secret},
                )
            )
        elif corruption == "foreign":
            foreign = request.model_copy(update={"session_id": secret})
            store._reconciliation_requests[request.request_id] = (
                foreign.model_dump_json()
            )
        elif corruption == "resolved_only":
            now = datetime(2026, 7, 15, 15, tzinfo=UTC)
            resolved = request.model_copy(
                update={
                    "status": ReconciliationStatus.RESOLVED,
                    "resolution": ReconciliationResolution(
                        action=ReconciliationAction.TERMINATE,
                        actor={"type": "test"},
                        evidence={"opaque": secret},
                        decided_at=now,
                        event_id="evt_resolved_only",
                    ),
                }
            )
            await store.resolve_reconciliation_request(
                expected=request,
                resolved=resolved,
                event=EventEnvelope(
                    event_id="evt_resolved_only",
                    type="reconciliation.resolved",
                    session_id=request.session_id,
                    run_id=run_id,
                    sequence=4,
                    payload={
                        "request_id": request.request_id,
                        "operation_id": None,
                        "action": "terminate",
                        "actor": {"type": "test"},
                        "evidence": {"opaque": secret},
                    },
                    occurred_at=now,
                ),
            )
        elif corruption == "missing":
            store._reconciliation_requests.pop(request.request_id)
        else:
            interrupted = run.model_copy(
                update={
                    "status": RunStatus.INTERRUPTED,
                    "version": run.version + 1,
                }
            )
            await store.commit(
                CommitBatch(
                    events=(),
                    snapshots=(
                        SnapshotWrite(
                            "run",
                            run_id,
                            run.session_id,
                            interrupted.version,
                            interrupted.model_dump(mode="json"),
                        ),
                    ),
                )
            )

        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.pending_requests(run_id)

        assert caught.value.code is ErrorCode.INTERNAL
        assert caught.value.message == "recovery state is invalid"
        assert caught.value.retryable is False
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        frames = _sdk_traceback_locals(caught.value)
        assert frames
        assert all(secret not in repr(frame) for frame in frames)
    finally:
        await sdk.close()


@pytest.mark.parametrize(
    "mismatch",
    (
        "agent_model",
        "model_params",
        "tool_schema",
        "tool_version",
        "tool_source",
        "tool_effects",
        "tool_timeout",
        "missing_tool",
        "policy",
    ),
)
@pytest.mark.asyncio
async def test_capability_mismatch_is_zero_mutation_then_exact_sdk_recovers(
    mismatch: str,
) -> None:
    store = InMemoryStore()
    exact_agent = AgentSpec(
        name=f"capability-{mismatch}",
        model="fake/recovery",
        model_params={"temperature": 0.25},
    )
    exact_tool = ToolSpec(
        name="lookup",
        description="lookup",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        version="3",
        source="mcp:server",
        effects=("network",),
        timeout_seconds=4,
    )
    run_id = await _seed_pristine_current_run(
        store,
        exact_agent,
        tool_specs=(exact_tool,),
    )
    cursor_before = await store.latest_cursor()
    provider_calls = 0
    tool_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        return _success_chunks()

    async def handler(_context: ToolContext, **_: object) -> object:
        nonlocal tool_calls
        tool_calls += 1
        return {"unused": True}

    selected_agent = exact_agent
    if mismatch == "agent_model":
        selected_agent = exact_agent.model_copy(update={"model": "fake/changed"})
    elif mismatch == "model_params":
        selected_agent = exact_agent.model_copy(
            update={"model_params": {"temperature": 0.75}}
        )
    selected_tool = exact_tool
    tool_updates: dict[str, object] = {
        "tool_schema": {
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "integer"}},
                "required": ["query"],
                "additionalProperties": False,
            }
        },
        "tool_version": {"version": "4"},
        "tool_source": {"source": "application"},
        "tool_effects": {"effects": ("filesystem",)},
        "tool_timeout": {"timeout_seconds": 9},
    }
    if mismatch in tool_updates:
        selected_tool = exact_tool.model_copy(update=tool_updates[mismatch])
    mismatched = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="deny" if mismatch == "policy" else "allow",
    )
    mismatched.agents.define(selected_agent)
    if mismatch != "missing_tool":
        mismatched.tools.register(selected_tool, handler)
    try:
        with pytest.raises(AgentSDKError) as caught:
            await mismatched.recovery.recover_run(run_id)

        assert caught.value.to_dict() == {
            "code": "invalid_state",
            "message": "recovery capabilities unavailable",
            "retryable": False,
        }
        assert provider_calls == 0
        assert tool_calls == 0
        assert await store.latest_cursor() == cursor_before
        assert await store.get_run_checkpoint(run_id) is None
        assert await store.list_pending_reconciliation_requests(run_id) == ()
        assert store._leases == {}
    finally:
        await mismatched.close()

    exact = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    exact.agents.define(exact_agent)
    exact.tools.register(exact_tool, handler)
    try:
        result = await (await exact.recovery.recover_run(run_id)).result()
        assert result.output_text == "recovered"
        assert provider_calls == 1
        assert tool_calls == 0
    finally:
        await exact.close()


@pytest.mark.asyncio
async def test_ready_for_model_resume_preserves_exact_checkpoint_state() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="ready-model", model="fake/recovery")
    run_id, checkpoint = await _seed_ready_model_interrupted(store, spec)
    provider_requests: list[dict[str, object]] = []

    async def completion(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        provider_requests.append(kwargs)
        return _success_chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        result = await handle.result()

        assert result.output_text == "prior recovered"
        assert result.usage == TokenUsage(
            prompt_tokens=7,
            completion_tokens=3,
            total_tokens=10,
        )
        assert result.tool_results == checkpoint.tool_results
        assert len(provider_requests) == 1
        _assert_general_prompt_then_messages(
            provider_requests[0]["messages"],
            [
                dict(message)
                for message in checkpoint.model_dump(mode="json")["messages"]
            ],
        )
        events = [
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ]
        recovery_started = next(
            event for event in events if event.type == "run.recovery.started"
        )
        interrupted = next(
            event for event in events if event.type == "run.interrupted"
        )
        assert recovery_started.sequence == interrupted.sequence + 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("resume_precommit", "resume_ambiguous"))
async def test_recovery_start_commit_is_all_or_none_and_replay_safe(
    fault: str,
) -> None:
    store = _RecoveryProgressFaultStore(fault)
    spec = AgentSpec(name=fault, model="fake/recovery")
    run_id, checkpoint = await _seed_ready_model_interrupted(store, spec)
    provider_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        return _success_chunks()

    store.enabled = True
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        if fault == "resume_precommit":
            with pytest.raises(AgentSDKError):
                await handle.result()
            assert provider_calls == 0
            assert (await sdk.runs.get(run_id)).status is RunStatus.INTERRUPTED
            assert await store.get_run_checkpoint(run_id) == checkpoint
            assert not any(
                stored.event.type == "run.recovery.started"
                for stored in await store.read_events(after_cursor=0)
            )
            store.enabled = False
            await asyncio.sleep(0)
            result = await (await sdk.recovery.recover_run(run_id)).result()
        else:
            result = await handle.result()
            assert len(store.calls) == 2
            assert store.calls[0] is store.calls[1]

        assert result.output_text == "prior recovered"
        assert provider_calls == 1
        assert store._leases == {}
        assert len(
            [
                stored
                for stored in await store.read_events(after_cursor=0)
                if stored.event.type == "run.recovery.started"
            ]
        ) == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kind", ("memory", "sqlite"))
async def test_recovery_start_cas_rejects_checkpoint_changed_after_engine_read(
    store_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store: Any
    if store_kind == "memory":
        store = InMemoryStore()
    else:
        store = await SQLiteStore.open(tmp_path / "recovery-start-cas.db")
    spec = AgentSpec(name=f"checkpoint-race-{store_kind}", model="fake/recovery")
    run_id, checkpoint = await _seed_ready_model_interrupted(store, spec)
    checkpoint_read = asyncio.Event()
    allow_engine = asyncio.Event()
    reads = 0
    provider_calls = 0
    original_get_checkpoint = store.get_run_checkpoint

    async def controlled_get_checkpoint(target_run_id: str) -> RunCheckpoint | None:
        nonlocal reads
        durable = await original_get_checkpoint(target_run_id)
        reads += 1
        if reads == 1:
            checkpoint_read.set()
            await allow_engine.wait()
        return durable

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        return _success_chunks()

    monkeypatch.setattr(store, "get_run_checkpoint", controlled_get_checkpoint)
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    task = asyncio.create_task(
        sdk.recovery._engine.resume(
            run_id,
            checkpoint,
            ModelRequest(
                model=spec.model,
                messages=({"role": "user", "content": "resume me"},),
            ),
        )
    )
    try:
        await asyncio.wait_for(checkpoint_read.wait(), timeout=2)
        now = datetime.now(UTC)
        lease = await store.acquire_lease(
            run_id=run_id,
            owner="checkpoint-racer",
            now=now,
            expires_at=now + timedelta(seconds=30),
        )
        changed = checkpoint.model_copy(
            update={
                "checkpoint_version": checkpoint.checkpoint_version + 1,
                "output_parts": (*checkpoint.output_parts, "changed "),
            }
        )
        await store.put_run_checkpoint(
            changed,
            expected=checkpoint,
            lease=lease,
            now=now,
        )
        await store.release_lease(lease)
        allow_engine.set()

        with pytest.raises(AgentSDKError):
            await task

        assert provider_calls == 0
        assert (await sdk.runs.get(run_id)).status is RunStatus.INTERRUPTED
        assert await original_get_checkpoint(run_id) == changed
        assert not any(
            stored.event.type == "run.recovery.started"
            for stored in await store.read_events(after_cursor=0)
        )
    finally:
        allow_engine.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
async def test_cancelled_recovery_start_is_scannable_and_resumable() -> None:
    store = _RecoveryProgressFaultStore("resume_cancel")
    spec = AgentSpec(name="resume-cancel", model="fake/recovery")
    run_id, checkpoint = await _seed_ready_model_interrupted(store, spec)
    provider_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        return _success_chunks()

    store.enabled = True
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        task = handle._task
        assert task is not None
        await asyncio.wait_for(store.commit_reached.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        store.allow_commit.set()
        with pytest.raises(AgentSDKError):
            await handle.result()
        for _ in range(3):
            await asyncio.sleep(0)

        assert provider_calls == 0
        assert (await sdk.runs.get(run_id)).status is RunStatus.RUNNING
        assert await store.get_run_checkpoint(run_id) == checkpoint
        assert store._leases == {}
        assert sdk.recovery._tasks == {}

        store.enabled = False
        await sdk.recovery.scan()
        assert (await sdk.runs.get(run_id)).status is RunStatus.INTERRUPTED
        result = await (await sdk.recovery.recover_run(run_id)).result()

        assert result.output_text == "prior recovered"
        assert provider_calls == 1
        assert not any(
            pending is not asyncio.current_task()
            and "heartbeat" in repr(pending.get_coro()).casefold()
            for pending in asyncio.all_tasks()
        )
    finally:
        store.allow_commit.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_ready_for_tool_resume_executes_pending_call_once_before_model() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="ready-tool", model="fake/recovery")
    tool_spec = ToolSpec(
        name="lookup",
        description="look up a value",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        version="7",
        source="mcp:test-server",
        effects=("network",),
        timeout_seconds=5,
    )
    run_id, checkpoint = await _seed_ready_tool_interrupted(
        store,
        spec,
        tool_spec,
    )
    tool_calls = 0
    provider_requests: list[dict[str, object]] = []

    async def handler(_context: ToolContext, **arguments: object) -> object:
        nonlocal tool_calls
        tool_calls += 1
        assert arguments == {"query": "value"}
        return {"answer": 42}

    async def completion(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        provider_requests.append(kwargs)
        return _success_chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        result = await handle.result()

        assert tool_calls == 1
        assert len(provider_requests) == 1
        messages = provider_requests[0]["messages"]
        assert isinstance(messages, list)
        assert [message["role"] for message in messages] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]
        assert len([message for message in messages if message["role"] == "assistant"]) == 1
        _assert_general_prompt_then_messages(
            messages,
            [
                *checkpoint.model_dump(mode="json")["messages"],
                messages[3],
            ],
        )
        assert messages[3]["tool_call_id"] == "call_resume"
        assert result.output_text == "draft recovered"
        assert len(result.tool_results) == 1
        assert result.tool_results[0].value == {"answer": 42}
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_ready_for_tool_resume_permission_deny_is_durable_and_observable() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="ready-tool-deny", model="fake/recovery")
    tool_spec = ToolSpec(
        name="lookup",
        description="look up a value",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    run_id, _checkpoint = await _seed_ready_tool_interrupted(
        store,
        spec,
        tool_spec,
        permission_default="ask",
    )
    tool_calls = 0

    async def handler(_context: ToolContext, **_: object) -> object:
        nonlocal tool_calls
        tool_calls += 1
        return {"must": "not run"}

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        return _success_chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="ask",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        permission = await asyncio.wait_for(
            sdk.permissions.next_request(run_id),
            timeout=2,
        )
        waiting = await store.get_run_checkpoint(run_id)
        assert waiting is not None
        assert waiting.phase is RunCheckpointPhase.WAITING
        assert (await sdk.runs.get(run_id)).status is RunStatus.WAITING_PERMISSION

        await sdk.permissions.resolve(
            permission.request_id,
            PermissionDecision.deny("recovery deny"),
        )
        result = await handle.result()

        assert tool_calls == 0
        assert len(result.tool_results) == 1
        assert result.tool_results[0].status.value == "denied"
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ]
        assert "permission.requested" in event_types
        assert "permission.resolved" in event_types
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_malformed_ready_tool_checkpoint_reconciles_before_recovery_start() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="ready-tool-malformed", model="fake/recovery")
    tool_spec = ToolSpec(
        name="lookup",
        description="lookup",
        input_schema={"type": "object", "additionalProperties": False},
    )
    run_id, checkpoint = await _seed_ready_tool_interrupted(store, spec, tool_spec)
    malformed = checkpoint.model_copy(
        update={"messages": ({"role": "user", "content": "resume me"},)}
    )
    store._run_checkpoints[run_id] = malformed.model_dump_json()
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, lambda _context: {"must": "not run"})
    try:
        with pytest.raises(AgentSDKError) as rejected:
            await sdk.recovery._engine.resume(
                run_id,
                malformed,
                ModelRequest(
                    model=spec.model,
                    messages=({"role": "user", "content": "resume me"},),
                    tools=sdk.tools.schemas(),
                ),
            )
        assert rejected.value.code is ErrorCode.INVALID_STATE
        assert store._leases == {}

        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await handle.result()

        requests = await sdk.recovery.pending_requests(run_id)
        assert len(requests) == 1
        assert requests[0].reason == "recovery_state_invalid"
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ]
        assert "run.recovery.started" not in event_types
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_ready_for_model_resume_survives_real_sqlite_reopen(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ready-model-reopen.db"
    initial = await SQLiteStore.open(database)
    spec = AgentSpec(name="sqlite-ready-model", model="fake/recovery")
    try:
        run_id, checkpoint = await _seed_ready_model_interrupted(initial, spec)  # type: ignore[arg-type]
    finally:
        await initial.close()
    provider_requests: list[dict[str, object]] = []

    async def completion(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        provider_requests.append(kwargs)
        return _success_chunks()

    reopened = AgentSDK.for_test(
        database_path=database,
        acompletion=completion,
        permission_default="allow",
    )
    reopened.agents.define(spec)
    try:
        result = await (await reopened.recovery.recover_run(run_id)).result()

        assert result.output_text == "prior recovered"
        assert result.tool_results == checkpoint.tool_results
        _assert_general_prompt_then_messages(
            provider_requests[0]["messages"],
            checkpoint.model_dump(mode="json")["messages"],
        )
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_two_sdks_recover_one_pristine_run_and_loser_follows() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="cross-sdk-created", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)
    provider_started = asyncio.Event()
    allow_provider = asyncio.Event()
    provider_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        provider_started.set()
        await allow_provider.wait()
        return _success_chunks()

    first = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    second = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    first.agents.define(spec)
    second.agents.define(spec)
    try:
        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_run(run_id),
            second.recovery.recover_run(run_id),
        )
        await asyncio.wait_for(provider_started.wait(), timeout=2)

        assert provider_calls == 1

        allow_provider.set()
        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )

        assert first_result == second_result
        assert first_result.output_text == "recovered"
        assert provider_calls == 1
    finally:
        allow_provider.set()
        await first.close()
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_default_cross_sdk_follower_polling_is_bounded(
    backend: str,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "bounded-follower-polling.db"
    if backend == "memory":
        owner_store: Any = InMemoryStore()
        follower_delegate = owner_store
    else:
        owner_store = await SQLiteStore.open(database_path)
        follower_delegate = await SQLiteStore.open(database_path)
    follower_store = _FollowerReadCountingStore(follower_delegate)
    spec = AgentSpec(name=f"bounded-follower-{backend}", model="fake/recovery")
    run_id = await _seed_pristine_current_run(owner_store, spec)
    provider_started = asyncio.Event()
    allow_provider = asyncio.Event()
    provider_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        provider_started.set()
        await allow_provider.wait()
        return _success_chunks()

    owner = AgentSDK.for_test(
        store=owner_store,
        acompletion=completion,
        permission_default="allow",
    )
    follower = AgentSDK.for_test(
        store=follower_store,
        acompletion=completion,
        permission_default="allow",
    )
    owner.agents.define(spec)
    follower.agents.define(spec)
    owner_handle = None
    follower_handle = None
    try:
        owner_handle = await owner.recovery.recover_run(run_id)
        await asyncio.wait_for(provider_started.wait(), timeout=2)
        follower_handle = await follower.recovery.recover_run(run_id)
        await asyncio.wait_for(follower_store.first_lease_read.wait(), timeout=2)

        follower_store.begin_counting()
        await asyncio.sleep(0.18)

        assert follower_store.snapshot_reads <= 4
        assert follower_store.lease_reads <= 4
        assert provider_calls == 1

        allow_provider.set()
        owner_result = await asyncio.wait_for(owner_handle.result(), timeout=2)
        follower_result = await asyncio.wait_for(follower_handle.result(), timeout=0.2)

        assert follower_result == owner_result
        assert follower_result.output_text == "recovered"
        assert provider_calls == 1
    finally:
        allow_provider.set()
        if owner_handle is not None:
            await asyncio.gather(owner_handle.result(), return_exceptions=True)
        if follower_handle is not None:
            await asyncio.gather(follower_handle.result(), return_exceptions=True)
        await owner.close()
        await follower.close()
        if backend == "sqlite":
            await owner_store.close()
            await follower_delegate.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_recovery_evidence_read_is_bounded_to_target_session(
    backend: str,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "session-scoped-evidence.db"
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(database_path)
    )
    spec = AgentSpec(name=f"session-evidence-{backend}", model="fake/recovery")
    run_id, _checkpoint = await _seed_ready_model_interrupted(store, spec)
    run_data = await store.get_snapshot("run", run_id)
    assert run_data is not None
    run = RunSnapshot.model_validate(run_data)
    target_events_before_noise = await store.read_events(
        after_cursor=0,
        session_id=run.session_id,
    )
    commands = RuntimeCommands(store)
    for _ in range(64):
        await commands.create_session(workspaces=[])
    fixed_cursor = await store.latest_cursor()
    all_events = await store.read_events(
        after_cursor=0,
        up_to_cursor=fixed_cursor,
    )
    recording_store = _EvidenceReadRecordingStore(store)
    sdk = AgentSDK.for_test(
        store=recording_store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        plan = await sdk.recovery._service.plan(run_id)

        assert plan.kind == "resume"
        assert len(all_events) > len(target_events_before_noise) + 60
        assert recording_store.read_calls == [
            (0, run.session_id, fixed_cursor, None)
        ]
        assert recording_store.returned_event_counts == [
            len(target_events_before_noise)
        ]
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("foreign_session", "duplicate_event_id"))
async def test_session_scoped_evidence_rejects_materialized_corruption(
    corruption: str,
) -> None:
    store = InMemoryStore()
    spec = AgentSpec(name=f"session-corruption-{corruption}", model="fake/recovery")
    run_id, _checkpoint = await _seed_ready_model_interrupted(store, spec)
    run_data = await store.get_snapshot("run", run_id)
    assert run_data is not None
    run = RunSnapshot.model_validate(run_data)
    target_events = await store.read_events(
        after_cursor=0,
        session_id=run.session_id,
    )
    if corruption == "foreign_session":
        foreign = await RuntimeCommands(store).create_session(workspaces=[])
        foreign_events = await store.read_events(
            after_cursor=0,
            session_id=foreign.session_id,
        )
        injected = foreign_events[0]
    else:
        injected = next(
            stored for stored in target_events if stored.event.run_id is None
        )
    fixed_cursor = await store.latest_cursor()
    recording_store = _EvidenceReadRecordingStore(
        store,
        injected_scoped_event=injected,
    )
    sdk = AgentSDK.for_test(
        store=recording_store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        plan = await sdk.recovery._service.plan(run_id)

        assert plan.kind == "reconcile"
        assert plan.reason == "recovery_state_invalid"
        assert recording_store.read_calls == [
            (0, run.session_id, fixed_cursor, None)
        ]
        assert recording_store.returned_event_counts == [len(target_events) + 1]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_two_sdks_resume_one_ready_model_checkpoint_once() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="cross-sdk-model", model="fake/recovery")
    run_id, _checkpoint = await _seed_ready_model_interrupted(store, spec)
    provider_started = asyncio.Event()
    allow_provider = asyncio.Event()
    provider_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        provider_started.set()
        await allow_provider.wait()
        return _success_chunks()

    sdks = tuple(
        AgentSDK.for_test(
            store=store,
            acompletion=completion,
            permission_default="allow",
        )
        for _ in range(2)
    )
    for sdk in sdks:
        sdk.agents.define(spec)
    try:
        handles = await asyncio.gather(
            *(sdk.recovery.recover_run(run_id) for sdk in sdks)
        )
        await asyncio.wait_for(provider_started.wait(), timeout=2)
        assert provider_calls == 1

        allow_provider.set()
        results = await asyncio.gather(*(handle.result() for handle in handles))

        assert results[0] == results[1]
        assert results[0].output_text == "prior recovered"
        assert provider_calls == 1
    finally:
        allow_provider.set()
        await asyncio.gather(*(sdk.close() for sdk in sdks))


@pytest.mark.asyncio
async def test_two_sdks_resume_one_ready_tool_checkpoint_once() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="cross-sdk-tool", model="fake/recovery")
    tool_spec = ToolSpec(
        name="lookup",
        description="lookup",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    run_id, _checkpoint = await _seed_ready_tool_interrupted(store, spec, tool_spec)
    tool_started = asyncio.Event()
    allow_tool = asyncio.Event()
    tool_calls = 0
    provider_calls = 0

    async def handler(_context: ToolContext, **_: object) -> object:
        nonlocal tool_calls
        tool_calls += 1
        tool_started.set()
        await allow_tool.wait()
        return {"answer": 42}

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        return _success_chunks()

    sdks = tuple(
        AgentSDK.for_test(
            store=store,
            acompletion=completion,
            permission_default="allow",
        )
        for _ in range(2)
    )
    for sdk in sdks:
        sdk.agents.define(spec)
        sdk.tools.register(tool_spec, handler)
    try:
        handles = await asyncio.gather(
            *(sdk.recovery.recover_run(run_id) for sdk in sdks)
        )
        await asyncio.wait_for(tool_started.wait(), timeout=2)

        assert tool_calls == 1
        assert provider_calls == 0

        allow_tool.set()
        results = await asyncio.gather(*(handle.result() for handle in handles))

        assert results[0] == results[1]
        assert tool_calls == 1
        assert provider_calls == 1
    finally:
        allow_tool.set()
        await asyncio.gather(*(sdk.close() for sdk in sdks))


@pytest.mark.asyncio
async def test_cross_sdk_follower_cancel_and_close_do_not_affect_owner() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="cross-sdk-cancel", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)
    provider_started = asyncio.Event()
    allow_provider = asyncio.Event()
    provider_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        provider_started.set()
        await allow_provider.wait()
        return _success_chunks()

    owner = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    follower = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    owner.agents.define(spec)
    follower.agents.define(spec)
    owner_handle = None
    try:
        owner_handle = await owner.recovery.recover_run(run_id)
        await asyncio.wait_for(provider_started.wait(), timeout=2)
        follower_handle = await follower.recovery.recover_run(run_id)
        follower_task = follower_handle._task
        assert follower_task is not None
        follower_task.cancel()
        follower_task.cancel()
        with pytest.raises(AgentSDKError):
            await follower_handle.result()
        await follower.close()

        allow_provider.set()
        result = await owner_handle.result()

        assert result.output_text == "recovered"
        assert provider_calls == 1
        assert follower.recovery._tasks == {}
    finally:
        allow_provider.set()
        if owner_handle is not None:
            await asyncio.gather(owner_handle.result(), return_exceptions=True)
        await owner.close()
        await follower.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_outcome", ("cancel", "terminal_precommit_failure"))
async def test_cross_sdk_follower_stops_when_owner_disappears(
    owner_outcome: str,
) -> None:
    store = _FailOwnerTerminalProgressStore()
    spec = AgentSpec(name=f"cross-sdk-{owner_outcome}", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)
    provider_started = asyncio.Event()
    allow_provider = asyncio.Event()
    follower_observed_owner = asyncio.Event()
    allow_follower_poll = asyncio.Event()
    follower_yields = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_started.set()
        await allow_provider.wait()
        if owner_outcome == "terminal_precommit_failure":
            raise RuntimeError("provider-owner-secret")
        return _success_chunks()

    async def bounded_follower_yield() -> None:
        nonlocal follower_yields
        follower_yields += 1
        if follower_yields > 1:
            raise AssertionError("follower did not observe the released lease")
        follower_observed_owner.set()
        await allow_follower_poll.wait()

    owner = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    follower = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    owner.agents.define(spec)
    follower.agents.define(spec)
    follower.recovery._service._yield = bounded_follower_yield
    owner_handle = None
    follower_handle = None
    try:
        owner_handle = await owner.recovery.recover_run(run_id)
        await provider_started.wait()
        follower_handle = await follower.recovery.recover_run(run_id)
        await follower_observed_owner.wait()

        owner_task = owner_handle._task
        assert owner_task is not None
        if owner_outcome == "cancel":
            owner_task.cancel()
            owner_task.cancel()
        else:
            store.reject_failure = True
            allow_provider.set()
        with pytest.raises(AgentSDKError):
            await owner_handle.result()

        allow_follower_poll.set()
        with pytest.raises(AgentSDKError) as caught:
            await follower_handle.result()

        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery required"
        assert caught.value.retryable is True
    finally:
        allow_provider.set()
        allow_follower_poll.set()
        if owner_handle is not None:
            await asyncio.gather(owner_handle.result(), return_exceptions=True)
        if follower_handle is not None:
            task = follower_handle._task
            if task is not None and not task.done():
                task.cancel()
            await asyncio.gather(follower_handle.result(), return_exceptions=True)
        await owner.close()
        await follower.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_outcome", ("expiry", "takeover"))
async def test_cross_sdk_follower_observes_lease_expiry_and_takeover(
    lease_outcome: str,
) -> None:
    store = InMemoryStore()
    spec = AgentSpec(name=f"lease-{lease_outcome}", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)
    now = datetime.now(UTC)
    initial = await store.acquire_lease(
        run_id=run_id,
        owner="initial-owner",
        now=now,
        expires_at=now + timedelta(seconds=30),
    )
    clock = [now]
    observed: asyncio.Queue[None] = asyncio.Queue()
    resume_poll: asyncio.Queue[None] = asyncio.Queue()
    max_active_yields = 1 if lease_outcome == "expiry" else 2
    yield_count = 0

    async def bounded_follower_yield() -> None:
        nonlocal yield_count
        yield_count += 1
        if yield_count > max_active_yields:
            raise AssertionError("follower did not converge after lease loss")
        await observed.put(None)
        await resume_poll.get()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.recovery._service._clock = lambda: clock[0]
    sdk.recovery._service._yield = bounded_follower_yield
    handle = None
    takeover = None
    try:
        handle = await sdk.recovery.recover_run(run_id)
        await observed.get()

        clock[0] = initial.expires_at + timedelta(seconds=1)
        if lease_outcome == "takeover":
            takeover = await store.acquire_lease(
                run_id=run_id,
                owner="takeover-owner",
                now=clock[0],
                expires_at=clock[0] + timedelta(seconds=30),
            )
        await resume_poll.put(None)

        if takeover is not None:
            await observed.get()
            task = handle._task
            assert task is not None
            assert not task.done()
            await store.release_lease(takeover)
            takeover = None
            await resume_poll.put(None)

        with pytest.raises(AgentSDKError) as caught:
            await handle.result()

        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery required"
        assert caught.value.retryable is True
    finally:
        await resume_poll.put(None)
        if takeover is not None:
            await store.release_lease(takeover)
        elif lease_outcome == "expiry":
            await store.release_lease(initial)
        if handle is not None:
            task = handle._task
            if task is not None and not task.done():
                task.cancel()
            await asyncio.gather(handle.result(), return_exceptions=True)
        await sdk.close()


@pytest.mark.asyncio
async def test_closing_follower_sdk_settles_without_stopping_owner() -> None:
    store = InMemoryStore()
    spec = AgentSpec(name="cross-sdk-close", model="fake/recovery")
    run_id = await _seed_pristine_current_run(store, spec)
    provider_started = asyncio.Event()
    allow_provider = asyncio.Event()
    follower_polling = asyncio.Event()
    provider_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        provider_started.set()
        await allow_provider.wait()
        return _success_chunks()

    async def observable_yield() -> None:
        follower_polling.set()
        await asyncio.sleep(0)

    owner = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    follower = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    owner.agents.define(spec)
    follower.agents.define(spec)
    follower.recovery._service._yield = observable_yield
    owner_handle = None
    follower_handle = None
    close_task = None
    try:
        owner_handle = await owner.recovery.recover_run(run_id)
        await provider_started.wait()
        follower_handle = await follower.recovery.recover_run(run_id)
        await follower_polling.wait()

        close_task = asyncio.create_task(follower.close())
        for _ in range(8):
            await asyncio.sleep(0)

        assert close_task.done()
        await close_task
        with pytest.raises(AgentSDKError) as caught:
            await follower_handle.result()
        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery required"

        allow_provider.set()
        result = await owner_handle.result()
        assert result.output_text == "recovered"
        assert provider_calls == 1
    finally:
        allow_provider.set()
        if follower_handle is not None:
            task = follower_handle._task
            if task is not None and not task.done():
                task.cancel()
            await asyncio.gather(follower_handle.result(), return_exceptions=True)
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        if owner_handle is not None:
            await asyncio.gather(owner_handle.result(), return_exceptions=True)
        await owner.close()
        await follower.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_completed_model_terminal_precommit_gap_reconciles_without_resend(
    backend: str,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "completed-terminal-gap.db"
    if backend == "memory":
        initial: Any = _RejectCompletedTerminalMemoryStore()
    else:
        initial = await _RejectCompletedTerminalSQLiteStore.open(database_path)
    spec = AgentSpec(name=f"terminal-gap-{backend}", model="fake/recovery")
    run_id = await _seed_pristine_current_run(initial, spec)
    provider_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        return _success_chunks()

    first = AgentSDK.for_test(
        store=initial,
        acompletion=completion,
        permission_default="allow",
    )
    first.agents.define(spec)
    first_handle = await first.recovery.recover_run(run_id)
    with pytest.raises(AgentSDKError) as failed_terminalization:
        await first_handle.result()
    assert failed_terminalization.value.code is ErrorCode.INTERNAL
    assert provider_calls == 1

    running = await first.runs.get(run_id)
    assert running.status is RunStatus.RUNNING
    checkpoint = await initial.get_run_checkpoint(run_id)
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.READY_FOR_MODEL
    operations = await initial.list_external_operations(run_id)
    assert len(operations) == 1
    completed_model = operations[0]
    assert isinstance(completed_model, ModelCallOperation)
    assert completed_model.status is ExternalOperationStatus.COMPLETED
    assert completed_model.outcome is not None
    assert completed_model.outcome["tool_calls"] == ()
    await first.close()

    if backend == "memory":
        initial.reject_terminal = False
        reopened = initial
    else:
        await initial.close()
        reopened = await SQLiteStore.open(database_path)

    second = AgentSDK.for_test(
        store=reopened,
        acompletion=completion,
        permission_default="allow",
    )
    second.agents.define(spec)
    try:
        await second.recovery.scan()
        assert (await second.runs.get(run_id)).status is RunStatus.INTERRUPTED

        recovered = await second.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError) as caught:
            await recovered.result()

        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery required"
        assert caught.value.retryable is True
        assert provider_calls == 1
        requests = await second.recovery.pending_requests(run_id)
        assert len(requests) == 1
        assert requests[0].operation_id == completed_model.operation_id
        assert requests[0].reason == "model_call_completed_terminalization_unknown"
        assert requests[0].details == {
            "checkpoint_phase": RunCheckpointPhase.READY_FOR_MODEL.value,
            "operation_status": ExternalOperationStatus.COMPLETED.value,
        }
        assert (await second.runs.get(run_id)).status is RunStatus.WAITING_RECONCILIATION
    finally:
        await second.close()
        if backend == "sqlite":
            await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "relation_invalidity",
    (
        "failed",
        "missing",
        "duplicate",
        "outcome_text",
        "outcome_usage",
        "outcome_call_id",
        "outcome_call_name",
        "outcome_call_arguments",
        "checkpoint_assistant",
        "cumulative_usage",
        "completed_event",
        "event_tail",
    ),
)
async def test_ready_tool_requires_exact_completed_model_relation_after_reopen(
    backend: str,
    relation_invalidity: str,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / f"ready-tool-relation-{relation_invalidity}.db"
    if backend == "memory":
        initial: Any = InMemoryStore()
    else:
        initial = await SQLiteStore.open(database_path)
    spec = AgentSpec(
        name=f"ready-tool-relation-{backend}-{relation_invalidity}",
        model="fake/recovery",
    )
    tool_spec = ToolSpec(
        name="lookup",
        description="lookup",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    run_id, _checkpoint = await _seed_ready_tool_interrupted(
        initial,
        spec,
        tool_spec,
        relation_invalidity=relation_invalidity,
    )
    if backend == "sqlite":
        await initial.close()
        store = await SQLiteStore.open(database_path)
    else:
        store = initial
    provider_calls = 0
    tool_calls = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        return _success_chunks()

    async def handler(_context: ToolContext, **_: object) -> object:
        nonlocal tool_calls
        tool_calls += 1
        return {"answer": 42}

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        recovered = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError) as caught:
            await recovered.result()

        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery required"
        assert caught.value.retryable is True
        assert provider_calls == 0
        assert tool_calls == 0
        requests = await sdk.recovery.pending_requests(run_id)
        assert len(requests) == 1
        assert requests[0].operation_id is None
        assert requests[0].reason == "recovery_state_invalid"
        assert requests[0].details == {
            "checkpoint_phase": RunCheckpointPhase.READY_FOR_TOOL.value
        }
        assert (await sdk.runs.get(run_id)).status is RunStatus.WAITING_RECONCILIATION
        events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        reconciliation_events = tuple(
            event for event in events if event.type == "reconciliation.requested"
        )
        assert len(reconciliation_events) == 1
        assert reconciliation_events[0].payload == {
            "request_id": requests[0].request_id,
            "operation_id": None,
            "reason": "recovery_state_invalid",
        }
        assert not any(event.type == "run.recovery.started" for event in events)
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_multi_turn_ready_tool_authoritative_history_resumes_after_reopen(
    backend: str,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "multi-turn-ready-tool-positive.db"
    initial: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(database_path)
    )
    spec = AgentSpec(name=f"multi-turn-ready-tool-{backend}", model="fake/recovery")
    tool_spec = ToolSpec(
        name="lookup",
        description="lookup",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        source="mcp:test-server",
    )
    run_id, checkpoint = await _seed_multi_turn_ready_tool_interrupted(
        initial,
        spec,
        tool_spec,
    )
    if backend == "sqlite":
        await initial.close()
        store: Any = await SQLiteStore.open(database_path)
    else:
        store = initial
    tool_calls: list[str] = []
    provider_messages: list[list[dict[str, object]]] = []

    async def handler(_context: ToolContext, *, query: str) -> object:
        tool_calls.append(query)
        return {"answer": query}

    async def completion(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        provider_messages.append(messages)
        return _success_chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert tool_calls == ["pending"]
        assert len(provider_messages) == 1
        assert [message["role"] for message in provider_messages[0]] == [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
        ]
        _assert_general_prompt_then_messages(
            provider_messages[0],
            [
                *checkpoint.model_dump(mode="json")["messages"],
                provider_messages[0][5],
            ],
        )
        assert result.output_text == "historical pending recovered"
        assert len(result.tool_results) == 2
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "relation_invalidity",
    (
        "historical_started_payload",
        "historical_completed_payload",
        "historical_event_order",
        "historical_event_wrong_turn",
        "historical_request_fingerprint",
        "historical_wrong_turn_request",
        "historical_outcome_text",
        "historical_outcome_call",
        "historical_checkpoint_assistant",
        "historical_tool_message",
        "historical_message_order",
        "historical_output",
        "usage_distribution",
    ),
)
async def test_multi_turn_ready_tool_requires_each_authoritative_model_relation(
    backend: str,
    relation_invalidity: str,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / f"multi-turn-ready-tool-{relation_invalidity}.db"
    initial: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(database_path)
    )
    spec = AgentSpec(
        name=f"multi-turn-ready-tool-{backend}-{relation_invalidity}",
        model="fake/recovery",
    )
    tool_spec = ToolSpec(
        name="lookup",
        description="lookup",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        source="mcp:test-server",
    )
    run_id, _checkpoint = await _seed_multi_turn_ready_tool_interrupted(
        initial,
        spec,
        tool_spec,
        relation_invalidity=relation_invalidity,
    )
    if backend == "sqlite":
        await initial.close()
        store: Any = await SQLiteStore.open(database_path)
    else:
        store = initial
    tool_calls = 0
    provider_calls = 0

    async def handler(_context: ToolContext, **_: object) -> object:
        nonlocal tool_calls
        tool_calls += 1
        raise AssertionError("forged history reached Tool/MCP handler")

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("forged history reached LiteLLM")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await sdk.recovery.recover_run(run_id)).result()
        assert tool_calls == 0
        assert provider_calls == 0
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].operation_id is None
        assert pending[0].reason == "recovery_state_invalid"
        events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        assert sum(event.type == "reconciliation.requested" for event in events) == 1
        assert not any(event.type == "run.recovery.started" for event in events)
        assert not any(event.type == "permission.requested" for event in events)
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()
