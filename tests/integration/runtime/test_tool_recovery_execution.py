from __future__ import annotations

import asyncio
import json
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
        if self.mode == "lease_loss" and self.calls == 1:
            await self.release_lease(batch.lease)
        if self.mode in {"ambiguous", "outcome_ambiguous"} and self.calls == 1:
            raise RuntimeError("private ambiguous audit failure")
        return result


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
) -> ToolSpec:
    return ToolSpec(
        name="recoverable",
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
                                    "id": "call_recovery",
                                    "function": {
                                        "name": "recoverable",
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
    tool_spec = _tool_spec(retry_policy, timeout_seconds=timeout_seconds)
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
