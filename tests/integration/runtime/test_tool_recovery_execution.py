from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import AgentSDK, AgentSDKError, PermissionDecision
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.models import AgentSpec, RunSnapshot, RunStatus
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    RunCheckpointPhase,
    ToolCallOperation,
)
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    RunProgressBatch,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools import (
    ToolContext,
    ToolResultStatus,
    ToolRetryPolicy,
    ToolSpec,
)


class _ToolRecoveryAuditFaultStore(InMemoryStore):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.enabled = False
        self.calls = 0
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        audit = any(
            event.type == "tool.recovery.retry.started" for event in batch.events
        )
        outcome = any(event.type == "tool.call.completed" for event in batch.events)
        target = (
            outcome if self.mode.startswith("outcome_") else audit
        )
        if not self.enabled or not target:
            return await super().commit_run_progress(batch)
        self.calls += 1
        if self.mode in {"precommit", "outcome_precommit"}:
            raise RuntimeError("private audit precommit failure")
        if self.mode == "barrier":
            self.reached.set()
            await self.release.wait()
        result = await super().commit_run_progress(batch)
        if self.mode == "post_audit_barrier" and self.calls == 1:
            self.reached.set()
            await self.release.wait()
        if self.mode == "lease_loss" and self.calls == 1:
            await self.release_lease(batch.lease)
        if self.mode in {"ambiguous", "outcome_ambiguous"} and self.calls == 1:
            raise RuntimeError("private ambiguous audit failure")
        return result


class _LeaseAssertBarrier:
    def __init__(self, delegate: Any, *, target: int) -> None:
        self._delegate = delegate
        self._target = target
        self.calls = 0
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def assert_current(self, lease: Any, *, now: Any = None) -> None:
        self.calls += 1
        if self.calls == self._target:
            self.reached.set()
            await self.release.wait()
        await self._delegate.assert_current(lease, now=now)


def _sdk_traceback_locals(error: BaseException) -> tuple[dict[str, Any], ...]:
    frames: list[dict[str, Any]] = []
    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in filename:
            frames.append(dict(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return tuple(frames)


def _tool_spec(
    retry_policy: ToolRetryPolicy,
    *,
    timeout_seconds: float | None = None,
    name: str = "recoverable",
    source: str = "application",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="recoverable",
        input_schema={
            "type": "object",
            "properties": {
                "value": {"type": "integer"},
                "secret": {"type": "string"},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        version="handler-v1",
        source=source,
        effects=("compute",),
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy,
    )


async def _seed_interrupted_tool_call(
    store: StateStore,
    *,
    retry_policy: ToolRetryPolicy,
    permission_default: str = "allow",
    arguments_json: str = '{"value":7}',
    timeout_seconds: float | None = None,
    tool_name: str = "recoverable",
    call_id: str = "call_recovery",
    tool_source: str = "application",
) -> tuple[str, AgentSpec, ToolSpec, str]:
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def interrupted_handler(
        _: ToolContext,
        value: int,
        secret: str | None = None,
    ) -> int:
        del value, secret
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    async def first_completion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "function": {
                                        "name": tool_name,
                                        "arguments": arguments_json,
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

        return chunks()

    spec = AgentSpec(name="tool-recovery", model="fake/tool-recovery")
    tool_spec = _tool_spec(
        retry_policy,
        timeout_seconds=timeout_seconds,
        name=tool_name,
        source=tool_source,
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=first_completion,
        permission_default=permission_default,  # type: ignore[arg-type]
    )
    sdk.tools.register(tool_spec, interrupted_handler)
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(session.session_id, spec, "recover the tool")
    if permission_default == "ask":
        permission = await asyncio.wait_for(
            sdk.permissions.next_request(handle.run_id),
            timeout=1,
        )
        await sdk.permissions.resolve(
            permission.request_id,
            PermissionDecision.allow_once(),
        )
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    unresolved = await store.list_unresolved_external_operations(handle.run_id)
    assert len(unresolved) == 1
    operation_id = unresolved[0].operation_id
    handle._task.cancel()  # type: ignore[attr-defined]
    with pytest.raises(AgentSDKError):
        await handle.result()
    await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
    await sdk.close()

    scanner = AgentSDK.for_test(
        store=store,
        acompletion=first_completion,
        permission_default=permission_default,  # type: ignore[arg-type]
    )
    try:
        await scanner.recovery.scan()
        assert (await scanner.runs.get(handle.run_id)).status is RunStatus.INTERRUPTED
    finally:
        await scanner.close()
    return handle.run_id, spec, tool_spec, operation_id


async def _seed_interrupted_second_tool_call(
    store: StateStore,
) -> tuple[str, AgentSpec, ToolSpec]:
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    model_turn = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal model_turn
        model_turn += 1
        turn = model_turn

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "content": f"turn-{turn}",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_history_{turn}",
                                    "function": {
                                        "name": "recoverable",
                                        "arguments": json.dumps({"value": turn}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": turn,
                    "completion_tokens": 1,
                    "total_tokens": turn + 1,
                },
            }

        return chunks()

    async def handler(_: ToolContext, value: int) -> int:
        if value == 1:
            return 11
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    spec = AgentSpec(name="tool-recovery-history", model="fake/tool-recovery")
    tool_spec = _tool_spec(ToolRetryPolicy.IDEMPOTENT)
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.tools.register(tool_spec, handler)
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(session.session_id, spec, "recover history")
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    handle._task.cancel()  # type: ignore[attr-defined]
    with pytest.raises(AgentSDKError):
        await handle.result()
    await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
    await sdk.close()

    scanner = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    try:
        await scanner.recovery.scan()
        assert (await scanner.runs.get(handle.run_id)).status is RunStatus.INTERRUPTED
    finally:
        await scanner.close()
    return handle.run_id, spec, tool_spec


async def _seed_safe_pre_handler_history(
    store: StateStore,
    *,
    history: str,
) -> tuple[str, AgentSpec, ToolSpec, ToolResultStatus, str]:
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    model_turn = 0

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal model_turn
        model_turn += 1
        turn = model_turn
        if turn == 1 and history == "tool_not_found":
            tool_name = "missing_history_tool"
            arguments = '{"value":1}'
        elif turn == 1 and history == "invalid_arguments":
            tool_name = "recoverable"
            arguments = '{"value":"invalid"}'
        else:
            tool_name = "recoverable"
            arguments = json.dumps({"value": turn})

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "content": f"safe-history-{history}-{turn}",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_safe_history_{turn}",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": arguments,
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": turn,
                    "completion_tokens": 1,
                    "total_tokens": turn + 1,
                },
            }

        return chunks()

    async def handler(_: ToolContext, value: int) -> int:
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    permission_default = "ask" if history == "permission_denied" else "allow"
    expected_status = {
        "permission_denied": ToolResultStatus.DENIED,
        "invalid_arguments": ToolResultStatus.INVALID_ARGUMENTS,
        "tool_not_found": ToolResultStatus.FAILED,
    }[history]
    spec = AgentSpec(name="safe-pre-handler-history", model="fake/tool-recovery")
    tool_spec = _tool_spec(ToolRetryPolicy.IDEMPOTENT)
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default=permission_default,  # type: ignore[arg-type]
    )
    sdk.tools.register(tool_spec, handler)
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(session.session_id, spec, "safe history")
    if history == "permission_denied":
        denied = await asyncio.wait_for(
            sdk.permissions.next_request(handle.run_id),
            timeout=1,
        )
        await sdk.permissions.resolve(
            denied.request_id,
            PermissionDecision.deny("historical denial"),
        )
        allowed = await asyncio.wait_for(
            sdk.permissions.next_request(handle.run_id),
            timeout=1,
        )
        await sdk.permissions.resolve(
            allowed.request_id,
            PermissionDecision.allow_once(),
        )
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    handle._task.cancel()  # type: ignore[attr-defined]
    with pytest.raises(AgentSDKError):
        await handle.result()
    await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
    checkpoint = await store.get_run_checkpoint(handle.run_id)
    assert checkpoint is not None
    assert checkpoint.tool_results[0].status is expected_status
    operations = await store.list_external_operations(handle.run_id)
    assert tuple((item.turn, item.operation_kind.value) for item in operations) == (
        (0, "model_call"),
        (1, "model_call"),
        (1, "tool_call"),
    )
    operation_id = operations[-1].operation_id
    await sdk.close()

    scanner = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default=permission_default,  # type: ignore[arg-type]
    )
    try:
        await scanner.recovery.scan()
        assert (await scanner.runs.get(handle.run_id)).status is RunStatus.INTERRUPTED
    finally:
        await scanner.close()
    return handle.run_id, spec, tool_spec, expected_status, operation_id


async def _final_completion(counter: list[int]) -> AsyncIterator[dict[str, object]]:
    counter.append(1)

    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}

    return chunks()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_policy",
    [ToolRetryPolicy.IDEMPOTENT, ToolRetryPolicy.SAFE_RETRY],
)
async def test_certified_interrupted_tool_retries_same_operation_then_resumes_model(
    retry_policy: ToolRetryPolicy,
) -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=retry_policy,
    )
    handler_calls: list[tuple[str, int]] = []
    model_calls = 0

    async def recovered_handler(context: ToolContext, value: int) -> dict[str, int]:
        handler_calls.append((context.run_id, value))
        return {"value": value + 1}

    async def following_completion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal model_calls
        model_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {"delta": {"content": "done"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            }

        return chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=following_completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, recovered_handler)
    try:
        result = await (await sdk.recovery.recover_run(run_id)).result()

        assert handler_calls == [(run_id, 7)]
        assert model_calls == 1
        assert result.output_text == "done"
        assert result.tool_results[0].value == {"value": 8}
        recovered_operation = await store.get_external_operation(operation_id)
        assert isinstance(recovered_operation, ToolCallOperation)
        assert recovered_operation.operation_id == operation_id
        assert recovered_operation.status is ExternalOperationStatus.COMPLETED
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.TERMINAL
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ]
        assert event_types.count("tool.call.started") == 1
        assert event_types.count("tool.recovery.retry.started") == 1
        assert event_types.count("tool.call.completed") == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["retry_policy", "effects", "timeout", "source", "version", "schema", "missing"],
)
async def test_changed_or_missing_tool_capability_reconciles_without_external_work(
    change: str,
) -> None:
    store = InMemoryStore()
    run_id, spec, original, _operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
    )
    updates: dict[str, Any] = {
        "retry_policy": ToolRetryPolicy.SAFE_RETRY,
        "effects": ("filesystem",),
        "timeout": 5.0,
        "source": "mcp/server",
        "version": "handler-v2",
        "schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    tool_data = original.model_dump(mode="json")
    if change == "timeout":
        tool_data["timeout_seconds"] = updates[change]
    elif change == "schema":
        tool_data["input_schema"] = updates[change]
    elif change != "missing":
        tool_data[change] = updates[change]
    current = ToolSpec.model_validate(tool_data)
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: Any) -> Any:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    if change != "missing":
        sdk.tools.register(current, handler)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert handler_calls == 0
        assert model_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "recovery_state_invalid"
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [True, False])
async def test_recovered_tool_re_evaluates_ask_permission(allow: bool) -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.SAFE_RETRY,
        permission_default="ask",
    )
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value + 1

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="ask",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        permission = await asyncio.wait_for(
            sdk.permissions.next_request(run_id),
            timeout=1,
        )
        await sdk.permissions.resolve(
            permission.request_id,
            PermissionDecision.allow_once()
            if allow
            else PermissionDecision.deny("no"),
        )
        result = await handle.result()
        assert handler_calls == int(allow)
        assert model_calls == [1]
        assert result.tool_results[0].status is (
            ToolResultStatus.SUCCEEDED if allow else ToolResultStatus.DENIED
        )
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_twenty_local_recoveries_share_one_tool_retry() -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
    )
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        await asyncio.sleep(0)
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        handles = await asyncio.gather(
            *(sdk.recovery.recover_run(run_id) for _ in range(20))
        )
        results = await asyncio.gather(*(handle.result() for handle in handles))
        assert {result.output_text for result in results} == {"done"}
        assert handler_calls == 1
        assert model_calls == [1]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_certified_tool_recovery_survives_sqlite_close_and_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tool-recovery.sqlite3"
    initial_store = await SQLiteStore.open(path)
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        initial_store,
        retry_policy=ToolRetryPolicy.SAFE_RETRY,
    )
    await initial_store.close()

    store = await SQLiteStore.open(path)
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert result.output_text == "done"
        assert handler_calls == 1
        assert model_calls == [1]
        operation = await store.get_external_operation(operation_id)
        assert isinstance(operation, ToolCallOperation)
        assert operation.status is ExternalOperationStatus.COMPLETED
    finally:
        await sdk.close()
        await store.close()


@pytest.mark.asyncio
async def test_default_tool_recovery_reconciles_after_sqlite_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unsafe-tool-recovery.sqlite3"
    initial_store = await SQLiteStore.open(path)
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        initial_store,
        retry_policy=ToolRetryPolicy.NEVER,
    )
    await initial_store.close()

    store = await SQLiteStore.open(path)
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert handler_calls == 0
        assert model_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "tool_call_unknown_outcome"
        assert pending[0].operation_id == operation_id
    finally:
        await sdk.close()
        await store.close()


@pytest.mark.asyncio
async def test_two_sdk_instances_have_one_tool_recovery_winner() -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
    )
    handler_calls = 0
    model_calls: list[int] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        entered.set()
        await release.wait()
        return value

    first = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    second = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    for sdk in (first, second):
        sdk.agents.define(spec)
        sdk.tools.register(tool_spec, handler)
    try:
        first_handle = await first.recovery.recover_run(run_id)
        await asyncio.wait_for(entered.wait(), timeout=1)
        second_handle = await second.recovery.recover_run(run_id)
        release.set()
        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )
        assert first_result == second_result
        assert handler_calls == 1
        assert model_calls == [1]
    finally:
        release.set()
        await first.close()
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["metadata", "fingerprint", "identity", "arguments", "usage", "event_tail"],
)
async def test_corrupted_tool_recovery_evidence_reconciles_without_external_work(
    corruption: str,
) -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.SAFE_RETRY,
    )
    if corruption in {"metadata", "fingerprint", "identity"}:
        data = json.loads(store._external_operations[operation_id])
        if corruption == "metadata":
            data["recovery_metadata"] = {
                "safe_retry": True,
                "retry_class": "unknown",
            }
        elif corruption == "fingerprint":
            data["request_fingerprint"] = "0" * 64
        else:
            data["tool_identity"] = "0" * 64
        store._external_operations[operation_id] = json.dumps(
            data,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    elif corruption in {"arguments", "usage"}:
        checkpoint_data = json.loads(store._run_checkpoints[run_id])
        if corruption == "arguments":
            checkpoint_data["messages"][-1]["tool_calls"][0]["function"][
                "arguments"
            ] = '{"value":NaN}'
        else:
            checkpoint_data["usage"]["total_tokens"] = 99
        store._run_checkpoints[run_id] = json.dumps(
            checkpoint_data,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        run = await store.get_snapshot("run", run_id)
        assert run is not None
        await store.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="corrupt.trailing.event",
                        session_id=run["session_id"],
                        run_id=run_id,
                        sequence=(await store.latest_run_event_sequence(run_id) or 0) + 1,
                        payload={},
                    ),
                )
            )
        )

    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert handler_calls == 0
        assert model_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "tool_call_unknown_outcome"
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["exception", "invalid_result"])
async def test_recovered_tool_uses_normalized_handler_failure_semantics(
    outcome: str,
) -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.SAFE_RETRY,
    )
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> object:
        del value
        if outcome == "exception":
            raise RuntimeError("private handler failure")
        return object()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert result.tool_results[0].status is ToolResultStatus.FAILED
        assert result.tool_results[0].error in {
            "tool handler failed",
            "tool result is not JSON-compatible or exceeds size limit",
        }
        assert model_calls == [1]
        operation = await store.get_external_operation(operation_id)
        assert isinstance(operation, ToolCallOperation)
        assert operation.status is ExternalOperationStatus.FAILED
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_permission_wait_cancellation_leaves_same_certified_operation_recoverable() -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
        permission_default="ask",
    )
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="ask",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        permission = await asyncio.wait_for(
            sdk.permissions.next_request(run_id),
            timeout=1,
        )
        assert handle._task is not None  # type: ignore[attr-defined]
        handle._task.cancel()  # type: ignore[attr-defined]
        handle._task.cancel()  # type: ignore[attr-defined]
        with pytest.raises(AgentSDKError):
            await handle.result()
        with pytest.raises(AgentSDKError):
            await sdk.permissions.resolve(
                permission.request_id,
                PermissionDecision.allow_once(),
            )
        operation = await store.get_external_operation(operation_id)
        assert isinstance(operation, ToolCallOperation)
        assert operation.status is ExternalOperationStatus.STARTED
        assert handler_calls == 0
        assert model_calls == []

        await sdk.recovery.scan()
        retry = await sdk.recovery.recover_run(run_id)
        retried_permission = await asyncio.wait_for(
            sdk.permissions.next_request(run_id),
            timeout=1,
        )
        await sdk.permissions.resolve(
            retried_permission.request_id,
            PermissionDecision.allow_once(),
        )
        result = await retry.result()
        assert result.tool_results[0].status is ToolResultStatus.SUCCEEDED
        assert handler_calls == 1
        assert model_calls == [1]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_handler_cancellation_and_sdk_close_leave_no_tool_outcome() -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.SAFE_RETRY,
    )
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        del value
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    handle = await sdk.recovery.recover_run(run_id)
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    await sdk.close()
    await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
    with pytest.raises(AgentSDKError):
        await handle.result()
    operation = await store.get_external_operation(operation_id)
    assert isinstance(operation, ToolCallOperation)
    assert operation.status is ExternalOperationStatus.STARTED
    assert model_calls == []


@pytest.mark.asyncio
async def test_recovered_tool_timeout_uses_normal_tool_result_and_cancels_handler() -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.SAFE_RETRY,
        timeout_seconds=0.01,
    )
    handler_cancelled = asyncio.Event()
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        del value
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        result = await (await sdk.recovery.recover_run(run_id)).result()
        await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
        assert result.tool_results[0].status is ToolResultStatus.TIMED_OUT
        assert model_calls == [1]
        operation = await store.get_external_operation(operation_id)
        assert isinstance(operation, ToolCallOperation)
        assert operation.status is ExternalOperationStatus.FAILED
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recovery_permission_events_do_not_expose_tool_arguments() -> None:
    secret = "argument-secret-3d2"
    store = InMemoryStore()
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
        permission_default="ask",
        arguments_json=json.dumps({"value": 7, "secret": secret}),
    )
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int, secret: str) -> int:
        del value, secret
        raise AssertionError("denied recovery must not invoke handler")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="ask",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        permission = await asyncio.wait_for(
            sdk.permissions.next_request(run_id),
            timeout=1,
        )
        assert permission.arguments["secret"] == secret
        await sdk.permissions.resolve(
            permission.request_id,
            PermissionDecision.deny("private decision evidence"),
        )
        await handle.result()
        events = [
            stored.event.model_dump(mode="json")
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ]
        recovery_events = events[events.index(next(
            event for event in events if event["type"] == "tool.recovery.retry.started"
        )) :]
        assert secret not in repr(recovery_events)
        assert "private decision evidence" not in repr(recovery_events)
        assert model_calls == [1]
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["precommit", "ambiguous"])
async def test_tool_recovery_audit_commit_is_exactly_replayed(mode: str) -> None:
    store = _ToolRecoveryAuditFaultStore(mode)
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
    )
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    store.enabled = True
    try:
        handle = await sdk.recovery.recover_run(run_id)
        if mode == "precommit":
            with pytest.raises(AgentSDKError):
                await handle.result()
            assert handler_calls == 0
            assert model_calls == []
            event_types = [
                stored.event.type
                for stored in await store.read_events(after_cursor=0)
                if stored.event.run_id == run_id
            ]
            assert "tool.recovery.retry.started" not in event_types
            operation = await store.get_external_operation(operation_id)
            assert isinstance(operation, ToolCallOperation)
            assert operation.lease_generation == 1
        else:
            result = await handle.result()
            assert result.output_text == "done"
            assert handler_calls == 1
            assert model_calls == [1]
            event_types = [
                stored.event.type
                for stored in await store.read_events(after_cursor=0)
                if stored.event.run_id == run_id
            ]
            assert event_types.count("tool.recovery.retry.started") == 1
            assert store.calls == 2
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_tool_recovery_audit_run_cas_failure_has_no_partial_refence() -> None:
    store = _ToolRecoveryAuditFaultStore("barrier")
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.SAFE_RETRY,
    )
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    store.enabled = True
    try:
        handle = await sdk.recovery.recover_run(run_id)
        await asyncio.wait_for(store.reached.wait(), timeout=1)
        run_data = await store.get_snapshot("run", run_id)
        assert run_data is not None
        run = RunSnapshot.model_validate(run_data)
        changed = run.model_copy(update={"version": run.version + 1})
        await store.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="concurrent.run.changed",
                        session_id=run.session_id,
                        run_id=run_id,
                        sequence=(await store.latest_run_event_sequence(run_id) or 0) + 1,
                        payload={},
                    ),
                ),
                snapshots=(
                    SnapshotWrite(
                        "run",
                        run_id,
                        run.session_id,
                        changed.version,
                        changed.model_dump(mode="json"),
                    ),
                ),
            )
        )
        store.release.set()
        with pytest.raises(AgentSDKError):
            await handle.result()
        assert handler_calls == 0
        assert model_calls == []
        operation = await store.get_external_operation(operation_id)
        assert isinstance(operation, ToolCallOperation)
        assert operation.lease_generation == 1
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ]
        assert "tool.recovery.retry.started" not in event_types
    finally:
        store.release.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_lease_loss_after_audit_keeps_certified_operation_recoverable() -> None:
    secret = "lease-loss-tool-argument-secret-3d2"
    store = _ToolRecoveryAuditFaultStore("lease_loss")
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
        arguments_json=json.dumps({"value": 7, "secret": secret}),
    )
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int, secret: str) -> int:
        nonlocal handler_calls
        del secret
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    store.enabled = True
    try:
        first = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await first.result()
        first_task = first._task  # type: ignore[attr-defined]
        assert first_task is not None
        first_error = first_task.exception()
        assert isinstance(first_error, AgentSDKError)
        assert all(
            secret not in repr(frame)
            for frame in _sdk_traceback_locals(first_error)
        )
        assert handler_calls == 0
        assert model_calls == []
        started = await store.get_external_operation(operation_id)
        assert isinstance(started, ToolCallOperation)
        assert started.status is ExternalOperationStatus.STARTED

        store.enabled = False
        second = await sdk.recovery.recover_run(run_id)
        result = await second.result()
        assert result.output_text == "done"
        assert handler_calls == 1
        assert model_calls == [1]
        completed = await store.get_external_operation(operation_id)
        assert isinstance(completed, ToolCallOperation)
        assert completed.status is ExternalOperationStatus.COMPLETED
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ]
        assert event_types.count("tool.recovery.retry.started") == 2
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_unsafe_tool_reconciliation_task_traceback_does_not_retain_arguments() -> None:
    secret = "retained-tool-argument-secret-3d2"
    store = InMemoryStore()
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
        arguments_json=json.dumps({"value": 7, "secret": secret}),
    )
    operation_data = json.loads(store._external_operations[operation_id])
    operation_data["recovery_metadata"] = {
        "safe_retry": False,
        "retry_class": "unsafe",
    }
    store._external_operations[operation_id] = json.dumps(
        operation_data,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    async def handler(_: ToolContext, **__: object) -> None:
        raise AssertionError("unsafe recovery must not invoke handler")

    model_calls: list[int] = []
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    handle = await sdk.recovery.recover_run(run_id)
    try:
        with pytest.raises(AgentSDKError) as caught:
            await handle.result()
        task = handle._task  # type: ignore[attr-defined]
        assert task is not None and task.done()
        task_error = task.exception()
        assert isinstance(task_error, AgentSDKError)
        for error in (caught.value, task_error):
            assert secret not in repr(error.to_dict())
            assert all(
                secret not in repr(frame)
                for frame in _sdk_traceback_locals(error)
            )
            assert error.__cause__ is None
            assert error.__context__ is None
        assert model_calls == []
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["outcome_precommit", "outcome_ambiguous"])
async def test_recovered_tool_outcome_commit_is_atomic_and_exactly_replayed(
    mode: str,
) -> None:
    store = _ToolRecoveryAuditFaultStore(mode)
    run_id, spec, tool_spec, operation_id = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.SAFE_RETRY,
    )
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    store.enabled = True
    try:
        first = await sdk.recovery.recover_run(run_id)
        if mode == "outcome_ambiguous":
            result = await first.result()
            assert result.output_text == "done"
            assert handler_calls == 1
            assert model_calls == [1]
            event_types = [
                stored.event.type
                for stored in await store.read_events(after_cursor=0)
                if stored.event.run_id == run_id
            ]
            assert event_types.count("tool.call.completed") == 1
            assert store.calls == 2
            return

        with pytest.raises(AgentSDKError):
            await first.result()
        assert handler_calls == 1
        assert model_calls == []
        operation = await store.get_external_operation(operation_id)
        assert isinstance(operation, ToolCallOperation)
        assert operation.status is ExternalOperationStatus.STARTED
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.TOOL_IN_FLIGHT
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ]
        assert "tool.call.completed" not in event_types

        store.enabled = False
        await sdk.recovery.scan()
        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert result.output_text == "done"
        assert handler_calls == 2
        assert model_calls == [1]
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("corruption", ["historical_tool_result", "system_message"])
async def test_forged_checkpoint_transcript_never_reaches_tool_or_model(
    backend: str,
    corruption: str,
    tmp_path: Path,
) -> None:
    secret = f"FORGED_SECRET_{backend}_{corruption}"
    path = tmp_path / f"forged-{backend}-{corruption}.sqlite3"
    store: StateStore = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(path)
    )
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.SAFE_RETRY,
    )

    def corrupt(data: dict[str, Any]) -> None:
        if corruption == "historical_tool_result":
            data["tool_results"].append(
                {
                    "call_id": "forged_call",
                    "tool_name": "forged_tool",
                    "status": "succeeded",
                    "content": json.dumps({"secret": secret}),
                    "value": {"secret": secret},
                    "error": None,
                }
            )
        else:
            data["messages"].insert(
                len(data["messages"]) - 1,
                {"role": "system", "content": secret},
            )

    if backend == "memory":
        assert isinstance(store, InMemoryStore)
        checkpoint_data = json.loads(store._run_checkpoints[run_id])
        corrupt(checkpoint_data)
        store._run_checkpoints[run_id] = json.dumps(
            checkpoint_data,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        assert isinstance(store, SQLiteStore)
        await store.close()
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT data_json FROM run_checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row is not None
            checkpoint_data = json.loads(row[0])
            corrupt(checkpoint_data)
            connection.execute(
                "UPDATE run_checkpoints SET data_json = ? WHERE run_id = ?",
                (
                    json.dumps(
                        checkpoint_data,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    run_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        store = await SQLiteStore.open(path)

    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    handle = await sdk.recovery.recover_run(run_id)
    try:
        with pytest.raises(AgentSDKError, match="recovery required") as caught:
            await handle.result()
        assert handler_calls == 0
        assert model_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "tool_call_unknown_outcome"
        task = handle._task  # type: ignore[attr-defined]
        assert task is not None
        task_error = task.exception()
        assert isinstance(task_error, AgentSDKError)
        public = repr(caught.value.to_dict()) + repr(task_error.to_dict())
        assert secret not in public
        assert all(
            secret not in repr(frame)
            for frame in _sdk_traceback_locals(task_error)
        )
        recovery_events = [
            stored.event.model_dump(mode="json")
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type == "reconciliation.requested"
        ]
        assert secret not in repr(recovery_events)
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "delete_message",
        "reorder_messages",
        "modify_tool_result",
        "model_fingerprint",
        "model_outcome",
        "tool_fingerprint",
        "tool_outcome",
        "delete_event",
        "reorder_events",
        "modify_event",
    ],
)
async def test_historical_recovery_evidence_is_reconstructed_exactly(
    corruption: str,
) -> None:
    secret = f"forged-history-{corruption}"
    store = InMemoryStore()
    run_id, spec, tool_spec = await _seed_interrupted_second_tool_call(store)

    def canonical(data: dict[str, Any]) -> str:
        return json.dumps(
            data,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    if corruption in {
        "delete_message",
        "reorder_messages",
        "modify_tool_result",
    }:
        checkpoint = json.loads(store._run_checkpoints[run_id])
        tool_index = next(
            index
            for index, message in enumerate(checkpoint["messages"])
            if message["role"] == "tool"
        )
        if corruption == "delete_message":
            del checkpoint["messages"][tool_index]
        elif corruption == "reorder_messages":
            checkpoint["messages"][tool_index - 1 : tool_index + 1] = reversed(
                checkpoint["messages"][tool_index - 1 : tool_index + 1]
            )
        else:
            checkpoint["tool_results"][0]["content"] = secret
        store._run_checkpoints[run_id] = canonical(checkpoint)
    elif corruption in {
        "model_fingerprint",
        "model_outcome",
        "tool_fingerprint",
        "tool_outcome",
    }:
        records = [
            (operation_id, json.loads(serialized))
            for operation_id, serialized in store._external_operations.items()
        ]
        kind = "model_call" if corruption.startswith("model_") else "tool_call"
        operation_id, operation = next(
            item
            for item in records
            if item[1]["turn"] == 0 and item[1]["operation_kind"] == kind
        )
        if corruption.endswith("fingerprint"):
            operation["request_fingerprint"] = secret
        elif corruption == "model_outcome":
            operation["outcome"]["text"] = secret
        else:
            operation["outcome"]["content"] = secret
        store._external_operations[operation_id] = canonical(operation)
    else:
        run_events = [
            (index, stored)
            for index, stored in enumerate(store._events)
            if stored.event.run_id == run_id
        ]
        if corruption == "delete_event":
            index, _ = next(
                item for item in run_events if item[1].event.type == "tool.call.proposed"
            )
            del store._events[index]
        elif corruption == "reorder_events":
            proposed_index, proposed = next(
                item for item in run_events if item[1].event.type == "tool.call.proposed"
            )
            authorized_index, authorized = next(
                item for item in run_events if item[1].event.type == "tool.call.authorized"
            )
            store._events[proposed_index] = type(proposed)(
                proposed.cursor,
                proposed.event.model_copy(update={"type": "tool.call.authorized"}),
            )
            store._events[authorized_index] = type(authorized)(
                authorized.cursor,
                authorized.event.model_copy(update={"type": "tool.call.proposed"}),
            )
        else:
            event_index, stored = next(
                item for item in run_events if item[1].event.type == "model.call.completed"
            )
            store._events[event_index] = type(stored)(
                stored.cursor,
                stored.event.model_copy(
                    update={"payload": {"finish_reason": secret}}
                ),
            )

    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    handle = await sdk.recovery.recover_run(run_id)
    try:
        with pytest.raises(AgentSDKError, match="recovery required") as caught:
            await handle.result()
        assert handler_calls == 0
        assert model_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "tool_call_unknown_outcome"
        assert secret not in repr(caught.value.to_dict())
        reconciliation_events = [
            stored.event.model_dump(mode="json")
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type == "reconciliation.requested"
        ]
        assert secret not in repr(reconciliation_events)
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "history",
    ["permission_denied", "invalid_arguments", "tool_not_found"],
)
async def test_safe_pre_handler_history_allows_certified_current_retry(
    backend: str,
    history: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"safe-history-{backend}-{history}.sqlite3"
    store: StateStore = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(path)
    )
    (
        run_id,
        spec,
        tool_spec,
        expected_status,
        operation_id,
    ) = await _seed_safe_pre_handler_history(store, history=history)
    if isinstance(store, SQLiteStore):
        await store.close()
        store = await SQLiteStore.open(path)
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value + 10

    permission_default = "ask" if history == "permission_denied" else "allow"
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default=permission_default,  # type: ignore[arg-type]
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    permission_task: asyncio.Task[None] | None = None

    async def allow_recovery() -> None:
        request = await sdk.permissions.next_request(run_id)
        await sdk.permissions.resolve(
            request.request_id,
            PermissionDecision.allow_once(),
        )

    if history == "permission_denied":
        permission_task = asyncio.create_task(allow_recovery())
    handle = await sdk.recovery.recover_run(run_id)
    try:
        result = await handle.result()
        if permission_task is not None:
            await permission_task
        assert handler_calls == 1
        assert model_calls == [1]
        assert tuple(item.status for item in result.tool_results) == (
            expected_status,
            ToolResultStatus.SUCCEEDED,
        )
        operation = await store.get_external_operation(operation_id)
        assert isinstance(operation, ToolCallOperation)
        assert operation.status is ExternalOperationStatus.COMPLETED
    finally:
        if permission_task is not None and not permission_task.done():
            permission_task.cancel()
            await asyncio.gather(permission_task, return_exceptions=True)
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history", "corruption"),
    [
        ("invalid_arguments", "modify_tool_result"),
        ("tool_not_found", "insert_tool_result"),
        ("permission_denied", "modify_permission_event"),
        ("invalid_arguments", "insert_permission_event"),
    ],
)
async def test_forged_safe_pre_handler_history_never_reaches_external_work(
    history: str,
    corruption: str,
) -> None:
    secret = f"forged-safe-history-{corruption}"
    store = InMemoryStore()
    run_id, spec, tool_spec, _, _ = await _seed_safe_pre_handler_history(
        store,
        history=history,
    )

    if corruption in {"modify_tool_result", "insert_tool_result"}:
        checkpoint = json.loads(store._run_checkpoints[run_id])
        if corruption == "modify_tool_result":
            checkpoint["tool_results"][0]["content"] = secret
        else:
            checkpoint["tool_results"].append(
                {
                    "call_id": "forged_safe_call",
                    "tool_name": "forged_safe_tool",
                    "status": "succeeded",
                    "content": json.dumps({"secret": secret}),
                    "value": {"secret": secret},
                    "error": None,
                }
            )
        store._run_checkpoints[run_id] = json.dumps(
            checkpoint,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    elif corruption == "modify_permission_event":
        event_index, stored = next(
            (index, stored)
            for index, stored in enumerate(store._events)
            if stored.event.run_id == run_id
            and stored.event.type == "permission.resolved"
        )
        payload = dict(stored.event.payload)
        payload["decision"] = {
            "action": "allow",
            "scope": "once",
            "reason": secret,
        }
        store._events[event_index] = type(stored)(
            stored.cursor,
            stored.event.model_copy(update={"payload": payload}),
        )
    else:
        target_index, target = next(
            (index, stored)
            for index, stored in enumerate(store._events)
            if stored.event.run_id == run_id
            and stored.event.type == "tool.call.completed"
        )
        target_cursor = target.cursor
        target_sequence = target.event.sequence
        shifted = []
        for stored in store._events:
            cursor = stored.cursor + (stored.cursor >= target_cursor)
            event = stored.event
            if event.run_id == run_id and event.sequence >= target_sequence:
                event = event.model_copy(
                    update={"sequence": event.sequence + 1}
                )
            shifted.append(type(stored)(cursor, event))
        inserted = EventEnvelope.new(
            type="permission.requested",
            session_id=target.event.session_id,
            run_id=run_id,
            sequence=target_sequence,
            payload={"request": {"request_id": secret}},
        )
        shifted.insert(target_index, type(target)(target_cursor, inserted))
        store._events = shifted
        store._last_cursor += 1

    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    permission_default = "ask" if history == "permission_denied" else "allow"
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default=permission_default,  # type: ignore[arg-type]
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    handle = await sdk.recovery.recover_run(run_id)
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert handler_calls == 0
        assert model_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "tool_call_unknown_outcome"
        reconciliation_events = [
            stored.event.model_dump(mode="json")
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type == "reconciliation.requested"
        ]
        assert secret not in repr(reconciliation_events)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_modified_recovery_permission_evidence_forces_reconciliation() -> None:
    secret = "forged-recovery-permission-event"
    store = InMemoryStore()
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
        permission_default="ask",
    )
    first_handler_started = asyncio.Event()
    first_handler_cancelled = asyncio.Event()

    async def first_handler(_: ToolContext, value: int) -> int:
        del value
        first_handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_handler_cancelled.set()
            raise

    first_sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="ask",
    )
    first_sdk.agents.define(spec)
    first_sdk.tools.register(tool_spec, first_handler)
    first = await first_sdk.recovery.recover_run(run_id)
    permission = await asyncio.wait_for(
        first_sdk.permissions.next_request(run_id),
        timeout=1,
    )
    await first_sdk.permissions.resolve(
        permission.request_id,
        PermissionDecision.allow_once(),
    )
    await asyncio.wait_for(first_handler_started.wait(), timeout=1)
    first._task.cancel()  # type: ignore[attr-defined]
    with pytest.raises(AgentSDKError):
        await first.result()
    await asyncio.wait_for(first_handler_cancelled.wait(), timeout=1)
    await first_sdk.close()

    scanner = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="ask",
    )
    try:
        await scanner.recovery.scan()
    finally:
        await scanner.close()

    event_index, stored = next(
        (index, stored)
        for index, stored in enumerate(store._events)
        if stored.event.run_id == run_id
        and stored.event.type == "permission.resolved"
        and "allowed" in stored.event.payload
    )
    forged_payload = dict(stored.event.payload)
    forged_payload["tool"] = {"sha256": secret}
    store._events[event_index] = type(stored)(
        stored.cursor,
        stored.event.model_copy(update={"payload": forged_payload}),
    )

    second_handler_calls = 0
    model_calls: list[int] = []

    async def second_handler(_: ToolContext, value: int) -> int:
        nonlocal second_handler_calls
        second_handler_calls += 1
        return value

    second_sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="ask",
    )
    second_sdk.agents.define(spec)
    second_sdk.tools.register(tool_spec, second_handler)
    handle = await second_sdk.recovery.recover_run(run_id)
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert second_handler_calls == 0
        assert model_calls == []
        pending = await second_sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "tool_call_unknown_outcome"
        reconciliation_events = [
            stored.event.model_dump(mode="json")
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type == "reconciliation.requested"
        ]
        assert secret not in repr(reconciliation_events)
    finally:
        await second_sdk.close()


@pytest.mark.asyncio
async def test_registry_removed_after_plan_becomes_durable_reconciliation() -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
    )
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    registered = sdk.tools.register(tool_spec, handler)
    handle = await sdk.recovery.recover_run(run_id)
    assert sdk.tools.unregister(tool_spec.name, expected=registered)
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert handler_calls == 0
        assert model_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "recovery_state_invalid"
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "retry_policy",
        "schema",
        "version",
        "source",
        "effects",
        "timeout",
        "handler",
    ],
)
async def test_registry_change_after_audit_reconciles_before_permission_or_handler(
    change: str,
) -> None:
    store = _ToolRecoveryAuditFaultStore("post_audit_barrier")
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.SAFE_RETRY,
        tool_source="mcp/server" if change == "handler" else "application",
    )
    original_calls = 0
    replacement_calls = 0
    model_calls: list[int] = []

    async def original(_: ToolContext, value: int) -> int:
        nonlocal original_calls
        original_calls += 1
        return value

    async def replacement(_: ToolContext, value: Any) -> Any:
        nonlocal replacement_calls
        replacement_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    registered = sdk.tools.register(tool_spec, original)
    store.enabled = True
    handle = await sdk.recovery.recover_run(run_id)
    await asyncio.wait_for(store.reached.wait(), timeout=1)
    assert sdk.tools.unregister(tool_spec.name, expected=registered)
    data = tool_spec.model_dump(mode="json")
    if change == "retry_policy":
        data["retry_policy"] = ToolRetryPolicy.IDEMPOTENT
    elif change == "schema":
        data["input_schema"] = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
    elif change == "version":
        data["version"] = "replacement-v2"
    elif change == "source":
        data["source"] = "mcp/replacement"
    elif change == "effects":
        data["effects"] = ["filesystem"]
    elif change == "timeout":
        data["timeout_seconds"] = 5.0
    if change != "missing":
        replacement_spec = ToolSpec.model_validate(data)
        sdk.tools.register(replacement_spec, replacement)
    store.release.set()
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert original_calls == 0
        assert replacement_calls == 0
        assert model_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "recovery_state_invalid"
    finally:
        store.release.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_handler_swap_during_ask_deny_reconciles_without_early_completion() -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
        permission_default="ask",
        tool_source="mcp/server",
    )
    handler_calls = 0
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="ask",
    )
    sdk.agents.define(spec)
    original = sdk.tools.register(tool_spec, handler)
    handle = await sdk.recovery.recover_run(run_id)
    permission = await asyncio.wait_for(
        sdk.permissions.next_request(run_id),
        timeout=1,
    )
    assert sdk.tools.unregister(tool_spec.name, expected=original)
    sdk.tools.register(tool_spec, handler)
    await sdk.permissions.resolve(
        permission.request_id,
        PermissionDecision.deny("must not become a Tool result"),
    )
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert handler_calls == 0
        assert model_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "recovery_state_invalid"
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_registry_swap_during_final_handler_preflight_reconciles() -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
        tool_source="mcp/server",
    )
    old_handler_calls = 0
    new_handler_calls = 0
    model_calls: list[int] = []

    async def old_handler(_: ToolContext, value: int) -> int:
        nonlocal old_handler_calls
        old_handler_calls += 1
        return value

    async def new_handler(_: ToolContext, value: int) -> int:
        nonlocal new_handler_calls
        new_handler_calls += 1
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    original = sdk.tools.register(tool_spec, old_handler)
    engine = sdk.recovery._service._engine  # type: ignore[attr-defined]
    barrier = _LeaseAssertBarrier(engine._leases, target=4)
    engine._leases = barrier
    handle = await sdk.recovery.recover_run(run_id)
    await asyncio.wait_for(barrier.reached.wait(), timeout=1)
    assert sdk.tools.unregister(tool_spec.name, expected=original)
    sdk.tools.register(tool_spec, new_handler)
    barrier.release.set()
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert old_handler_calls == 0
        assert new_handler_calls == 0
        assert model_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "recovery_state_invalid"
    finally:
        barrier.release.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_recovery_observability_hashes_unbounded_tool_identities() -> None:
    secret = "private-identity-3d2"
    tool_name = "tool_" + secret + ("x" * 4_096)
    call_id = "call_" + secret + ("y" * 4_096)
    store = InMemoryStore()
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
        permission_default="ask",
        tool_name=tool_name,
        call_id=call_id,
    )
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="ask",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        handle = await sdk.recovery.recover_run(run_id)
        permission = await asyncio.wait_for(
            sdk.permissions.next_request(run_id),
            timeout=1,
        )
        await sdk.permissions.resolve(
            permission.request_id,
            PermissionDecision.allow_once(),
        )
        await handle.result()
        events = [
            stored.event.model_dump(mode="json")
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type
            in {
                "tool.recovery.retry.started",
                "permission.requested",
                "permission.resolved",
                "tool.call.authorized",
            }
        ]
        recovery = events[
            next(
                index
                for index, event in enumerate(events)
                if event["type"] == "tool.recovery.retry.started"
            ) :
        ]
        serialized = json.dumps(recovery, ensure_ascii=False, sort_keys=True)
        assert secret not in serialized
        assert tool_name not in serialized
        assert call_id not in serialized
        assert len(serialized.encode("utf-8")) < 4_096

        expected_tool = hashlib.sha256(tool_name.encode()).hexdigest()
        expected_call = hashlib.sha256(call_id.encode()).hexdigest()
        audit = recovery[0]["payload"]
        assert audit["tool"] == {"sha256": expected_tool}
        assert audit["call"] == {"sha256": expected_call}
        assert set(audit["operation"]) == {"sha256"}
        for event in recovery:
            payload = event["payload"]
            if event["type"] == "tool.call.authorized":
                assert payload == {
                    "call": {"sha256": expected_call},
                    "tool": {"sha256": expected_tool},
                }
            if event["type"].startswith("permission."):
                assert payload["tool"] == {"sha256": expected_tool}
                assert set(payload["request"]) == {"sha256"}
        assert model_calls == [1]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_long_registry_conflict_identity_is_not_retained_by_public_task() -> None:
    secret = "private-registry-conflict-identity"
    tool_name = "tool_" + secret + ("x" * 4_096)
    call_id = "call_" + secret + ("y" * 4_096)
    store = InMemoryStore()
    run_id, spec, tool_spec, _ = await _seed_interrupted_tool_call(
        store,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
        tool_name=tool_name,
        call_id=call_id,
    )

    async def handler(_: ToolContext, value: int) -> int:
        return value

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    registered = sdk.tools.register(tool_spec, handler)
    handle = await sdk.recovery.recover_run(run_id)
    assert sdk.tools.unregister(tool_name, expected=registered)
    try:
        with pytest.raises(AgentSDKError, match="recovery required") as caught:
            await handle.result()
        task = handle._task  # type: ignore[attr-defined]
        assert task is not None
        task_error = task.exception()
        assert isinstance(task_error, AgentSDKError)
        assert secret not in repr(caught.value.to_dict())
        assert secret not in repr(task_error.to_dict())
        assert all(
            secret not in repr(frame)
            for frame in _sdk_traceback_locals(task_error)
        )
        reconciliation_events = [
            stored.event.model_dump(mode="json")
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type == "reconciliation.requested"
        ]
        assert secret not in repr(reconciliation_events)
    finally:
        await sdk.close()
