from __future__ import annotations

import asyncio
import inspect
import json
import traceback
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import agent_sdk
import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    ReconciliationAction,
    SessionStatus,
)
from agent_sdk.models.litellm_gateway import ModelRequest
from agent_sdk.runtime.leases import LeaseManager
from agent_sdk.runtime.models import RunStatus
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
    ToolCallOperation,
    RunCheckpointPhase,
    ReconciliationStatus,
    RecoveryStateConflictError,
    _canonical_record_json,
    deserialize_model_request,
    model_request_fingerprint,
    serialize_model_request,
)
from agent_sdk.storage.base import CommitBatch, RunProgressBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools import ToolContext, ToolRetryPolicy, ToolSpec


class _SecretMapping(dict[str, object]):
    def __init__(self, value: dict[str, object], secret: str) -> None:
        super().__init__(value)
        self._secret = secret

    def __repr__(self) -> str:
        return f"{super().__repr__()}<{self._secret}>"


class _RaisingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        del key
        raise RuntimeError("mapping access must be contained")

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("mapping iteration must be contained")

    def __len__(self) -> int:
        raise RuntimeError("mapping length must be contained")


class _JSONListSubclass(list[object]):
    pass


class _JSONDictSubclass(dict[str, object]):
    pass


def _nested_json_list(depth: int) -> object:
    value: object = 0
    for _ in range(depth):
        value = [value]
    return value


def _nested_json_object(depth: int) -> object:
    value: object = 0
    for _ in range(depth):
        value = {"value": value}
    return value


class _ResolutionBarrierMemoryStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.resolution_barrier_enabled = False
        self.resolution_reached = asyncio.Event()
        self.allow_resolution = asyncio.Event()
        self.resolution_evidence_barrier_enabled = False
        self.resolution_evidence_reached = asyncio.Event()
        self.allow_resolution_evidence = asyncio.Event()

    async def get_run_checkpoint(self, run_id: str) -> Any:
        if self.resolution_evidence_barrier_enabled:
            lease = await super().get_run_lease(run_id)
            if lease is not None:
                self.resolution_evidence_reached.set()
                await self.allow_resolution_evidence.wait()
        return await super().get_run_checkpoint(run_id)

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        if (
            self.resolution_barrier_enabled
            and batch.reconciliation is not None
            and batch.reconciliation.updated.status.value == "resolved"
        ):
            self.resolution_reached.set()
            await self.allow_resolution.wait()
        return await super().commit_run_progress(batch)

    async def race_reconciliation_cas(self, request: Any) -> None:
        raced = request.model_copy(
            update={"details": {**dict(request.details), "cas_race": "memory"}}
        )
        async with self._lock:
            assert self._reconciliation_requests[request.request_id] == (
                _canonical_record_json(request)
            )
            self._reconciliation_requests[request.request_id] = (
                _canonical_record_json(raced)
            )

    async def race_checkpoint_cas(self, checkpoint: Any) -> None:
        raced = checkpoint.model_copy(
            update={
                "checkpoint_version": checkpoint.checkpoint_version + 1,
            }
        )
        async with self._lock:
            assert self._run_checkpoints[checkpoint.run_id] == (
                _canonical_record_json(checkpoint)
            )
            self._run_checkpoints[checkpoint.run_id] = _canonical_record_json(raced)


class _ResolutionBarrierSQLiteStore(SQLiteStore):
    def __init__(self, connection: Any) -> None:
        super().__init__(connection)
        self.resolution_barrier_enabled = False
        self.resolution_reached = asyncio.Event()
        self.allow_resolution = asyncio.Event()
        self.resolution_evidence_barrier_enabled = False
        self.resolution_evidence_reached = asyncio.Event()
        self.allow_resolution_evidence = asyncio.Event()

    async def get_run_checkpoint(self, run_id: str) -> Any:
        if self.resolution_evidence_barrier_enabled:
            lease = await super().get_run_lease(run_id)
            if lease is not None:
                self.resolution_evidence_reached.set()
                await self.allow_resolution_evidence.wait()
        return await super().get_run_checkpoint(run_id)

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        if (
            self.resolution_barrier_enabled
            and batch.reconciliation is not None
            and batch.reconciliation.updated.status.value == "resolved"
        ):
            self.resolution_reached.set()
            await self.allow_resolution.wait()
        return await super().commit_run_progress(batch)

    async def race_reconciliation_cas(self, request: Any) -> None:
        raced = request.model_copy(
            update={"details": {**dict(request.details), "cas_race": "sqlite"}}
        )
        async with self._lock:
            self._ensure_open()
            result = await self._connection.execute(
                """
                UPDATE reconciliation_requests SET data_json = ?
                WHERE request_id = ? AND data_json = ?
                """,
                (
                    _canonical_record_json(raced),
                    request.request_id,
                    _canonical_record_json(request),
                ),
            )
            assert result.rowcount == 1
            await self._connection.commit()

    async def race_checkpoint_cas(self, checkpoint: Any) -> None:
        raced = checkpoint.model_copy(
            update={
                "checkpoint_version": checkpoint.checkpoint_version + 1,
            }
        )
        async with self._lock:
            self._ensure_open()
            result = await self._connection.execute(
                """
                UPDATE run_checkpoints SET checkpoint_version = ?, data_json = ?
                WHERE run_id = ? AND data_json = ?
                """,
                (
                    raced.checkpoint_version,
                    _canonical_record_json(raced),
                    checkpoint.run_id,
                    _canonical_record_json(checkpoint),
                ),
            )
            assert result.rowcount == 1
            await self._connection.commit()


class _AmbiguousResolutionMemoryStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False
        self.resolution_batches = 0

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        is_resolution = (
            batch.reconciliation is not None
            and batch.reconciliation.updated.status.value == "resolved"
        )
        result = await super().commit_run_progress(batch)
        if is_resolution:
            self.resolution_batches += 1
            if not self.failed_once:
                self.failed_once = True
                raise RuntimeError("ambiguous-resolution-store-secret")
        return result


class _AmbiguousResolutionSQLiteStore(SQLiteStore):
    def __init__(self, connection: Any) -> None:
        super().__init__(connection)
        self.failed_once = False
        self.resolution_batches = 0

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        is_resolution = (
            batch.reconciliation is not None
            and batch.reconciliation.updated.status.value == "resolved"
        )
        result = await super().commit_run_progress(batch)
        if is_resolution:
            self.resolution_batches += 1
            if not self.failed_once:
                self.failed_once = True
                raise RuntimeError("ambiguous-resolution-store-secret")
        return result


class _PartialAmbiguousResolutionMemoryStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        is_resolution = (
            batch.reconciliation is not None
            and batch.reconciliation.updated.status.value == "resolved"
        )
        result = await super().commit_run_progress(batch)
        if is_resolution and not self.failed_once:
            self.failed_once = True
            assert batch.operation is not None
            assert batch.operation.expected is not None
            self._external_operations[batch.operation.updated.operation_id] = (
                _canonical_record_json(batch.operation.expected)
            )
            raise RuntimeError("partial-resolution-store-secret")
        return result


class _CaptureResolutionMemoryStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.capture_resolution = False
        self.captured_batch: RunProgressBatch | None = None

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        if (
            self.capture_resolution
            and batch.reconciliation is not None
            and batch.reconciliation.updated.status is ReconciliationStatus.RESOLVED
        ):
            self.captured_batch = batch
            raise RuntimeError("capture confirmed Tool resolution")
        return await super().commit_run_progress(batch)


class _CaptureResolutionSQLiteStore(SQLiteStore):
    def __init__(self, connection: Any) -> None:
        super().__init__(connection)
        self.capture_resolution = False
        self.captured_batch: RunProgressBatch | None = None

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        if (
            self.capture_resolution
            and batch.reconciliation is not None
            and batch.reconciliation.updated.status is ReconciliationStatus.RESOLVED
        ):
            self.captured_batch = batch
            raise RuntimeError("capture confirmed Tool resolution")
        return await super().commit_run_progress(batch)


class _RejectCompletedTerminalResolutionMemoryStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.reject_terminal = True

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        if self.reject_terminal and any(
            event.type == "run.completed" for event in batch.events
        ):
            raise RuntimeError("completed-terminal-precommit")
        return await super().commit_run_progress(batch)


class _RejectCompletedTerminalResolutionSQLiteStore(SQLiteStore):
    def __init__(self, connection: Any) -> None:
        super().__init__(connection)
        self.reject_terminal = True

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        if self.reject_terminal and any(
            event.type == "run.completed" for event in batch.events
        ):
            raise RuntimeError("completed-terminal-precommit")
        return await super().commit_run_progress(batch)


def _assert_secret_free(error: BaseException, *secrets: str) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    for secret in secrets:
        assert secret not in str(error)
        assert secret not in rendered
        assert secret not in repr(error.__context__)
    sdk_frames = 0
    current = error.__traceback__
    while current is not None:
        filename = current.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in filename:
            sdk_frames += 1
            for secret in secrets:
                assert secret not in repr(current.tb_frame.f_locals)
        current = current.tb_next
    assert sdk_frames > 0


def test_public_reconciliation_resolution_contract_is_exported() -> None:
    exported = {
        "ReconciliationAction",
        "ReconciliationRequest",
        "ReconciliationResolution",
        "ReconciliationService",
    }

    assert exported <= set(agent_sdk.__all__)
    assert all(hasattr(agent_sdk, name) for name in exported)

    signature = inspect.signature(agent_sdk.RecoveryAPI.resolve)
    assert tuple(signature.parameters) == (
        "self",
        "request_id",
        "action",
        "actor",
        "evidence",
    )
    assert signature.parameters["actor"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["evidence"].kind is inspect.Parameter.KEYWORD_ONLY


async def _mark_interrupted(store: Any) -> None:
    scanner = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("scanner must not call the provider")
        ),
        permission_default="allow",
    )
    try:
        await scanner.recovery.scan()
    finally:
        await scanner.close()


async def _seed_real_model_in_flight(
    store: Any,
    tool_spec: ToolSpec | None = None,
) -> tuple[str, AgentSpec, str]:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_completion(**_: object) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    spec = AgentSpec(name="resolution-model", model="fake/resolution-model")
    seed = AgentSDK.for_test(
        store=store,
        acompletion=blocked_completion,
        permission_default="allow",
    )
    if tool_spec is not None:
        async def unused_tool(_: ToolContext, value: int) -> int:
            return value

        seed.tools.register(tool_spec, unused_tool)
    session = await seed.sessions.create(workspaces=[])
    handle = await seed.runs.start(session.session_id, spec, "resolve model")
    await asyncio.wait_for(entered.wait(), timeout=10)
    unresolved = await store.list_unresolved_external_operations(handle.run_id)
    assert len(unresolved) == 1
    operation_id = unresolved[0].operation_id
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(AgentSDKError):
        await handle.result()
    await asyncio.wait_for(cancelled.wait(), timeout=10)
    await seed.close()
    await _mark_interrupted(store)
    return handle.run_id, spec, operation_id


async def _seed_partial_model_in_flight(
    store: Any,
    *,
    deltas: tuple[str, ...] = ("partial",),
    tool_spec: ToolSpec | None = None,
) -> tuple[str, AgentSpec, str]:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def partial_completion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            for delta in deltas:
                yield {"choices": [{"delta": {"content": delta}}]}
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        return chunks()

    spec = AgentSpec(name="resolution-partial", model="fake/resolution-partial")
    seed = AgentSDK.for_test(
        store=store,
        acompletion=partial_completion,
        permission_default="allow",
    )
    if tool_spec is not None:

        async def unused_tool(_: ToolContext, value: int) -> int:
            return value

        seed.tools.register(tool_spec, unused_tool)
    session = await seed.sessions.create(workspaces=[])
    handle = await seed.runs.start(session.session_id, spec, "resolve partial model")
    await asyncio.wait_for(entered.wait(), timeout=10)
    unresolved = await store.list_unresolved_external_operations(handle.run_id)
    assert len(unresolved) == 1
    operation_id = unresolved[0].operation_id
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(AgentSDKError):
        await handle.result()
    await asyncio.wait_for(cancelled.wait(), timeout=10)
    await seed.close()
    await _mark_interrupted(store)
    return handle.run_id, spec, operation_id


def _unsafe_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="resolution_tool",
        description="resolution tool",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        version="1",
        source="application",
        effects=("external",),
        retry_policy=ToolRetryPolicy.NEVER,
    )


async def _seed_real_tool_in_flight(
    store: Any,
) -> tuple[str, AgentSpec, ToolSpec, str]:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def first_completion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_resolution",
                                    "function": {
                                        "name": "resolution_tool",
                                        "arguments": '{"value":7}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

        return chunks()

    async def blocked_handler(_: ToolContext, value: int) -> int:
        del value
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    spec = AgentSpec(name="resolution-tool", model="fake/resolution-tool")
    tool_spec = _unsafe_tool_spec()
    seed = AgentSDK.for_test(
        store=store,
        acompletion=first_completion,
        permission_default="allow",
    )
    seed.tools.register(tool_spec, blocked_handler)
    session = await seed.sessions.create(workspaces=[])
    handle = await seed.runs.start(session.session_id, spec, "resolve tool")
    await asyncio.wait_for(entered.wait(), timeout=10)
    unresolved = await store.list_unresolved_external_operations(handle.run_id)
    assert len(unresolved) == 1
    operation_id = unresolved[0].operation_id
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(AgentSDKError):
        await handle.result()
    await asyncio.wait_for(cancelled.wait(), timeout=10)
    await seed.close()
    await _mark_interrupted(store)
    return handle.run_id, spec, tool_spec, operation_id


async def _seed_later_turn_in_flight(
    store: Any,
    *,
    operation_kind: str,
    historical_usage: bool = False,
) -> tuple[str, AgentSpec, ToolSpec, str]:
    target_entered = asyncio.Event()
    target_cancelled = asyncio.Event()
    provider_attempts: list[int] = []
    historical_tool_calls: list[int] = []

    async def completion(**_: object) -> Any:
        provider_attempts.append(1)
        attempt = len(provider_attempts)
        if attempt == 1 or (attempt == 2 and operation_kind == "tool"):
            value = attempt

            async def tool_chunks() -> AsyncIterator[dict[str, object]]:
                chunk: dict[str, object] = {
                    "choices": [
                        {
                            "delta": {
                                **(
                                    {"content": "before"}
                                    if historical_usage and attempt == 1
                                    else {}
                                ),
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": f"call_later_turn_{value}",
                                        "function": {
                                            "name": "resolution_tool",
                                            "arguments": json.dumps({"value": value}),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
                if historical_usage and attempt == 1:
                    chunk["usage"] = {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": 4,
                    }
                yield chunk

            return tool_chunks()
        assert attempt == 2 and operation_kind == "model"
        target_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            target_cancelled.set()
            raise

    async def handler(_: ToolContext, value: int) -> int:
        if value == 1:
            historical_tool_calls.append(value)
            return value + 1
        assert operation_kind == "tool" and value == 2
        target_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            target_cancelled.set()
            raise

    spec = AgentSpec(
        name=f"later-turn-{operation_kind}",
        model=f"fake/later-turn-{operation_kind}",
    )
    tool_spec = _unsafe_tool_spec()
    seed = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    seed.tools.register(tool_spec, handler)
    session = await seed.sessions.create(workspaces=[])
    handle = await seed.runs.start(
        session.session_id,
        spec,
        f"resolve later-turn {operation_kind}",
    )
    await asyncio.wait_for(target_entered.wait(), timeout=10)
    unresolved = await store.list_unresolved_external_operations(handle.run_id)
    assert len(unresolved) == 1
    operation_id = unresolved[0].operation_id
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(AgentSDKError):
        await handle.result()
    await asyncio.wait_for(target_cancelled.wait(), timeout=10)
    await seed.close()
    assert len(provider_attempts) == 2
    assert historical_tool_calls == [1]
    await _mark_interrupted(store)
    return handle.run_id, spec, tool_spec, operation_id


async def _seed_pending_later_model_reconciliation(
    store: Any,
) -> tuple[str, AgentSpec, ToolSpec, str, Any]:
    run_id, spec, tool_spec, operation_id = await _seed_later_turn_in_flight(
        store,
        operation_kind="model",
        historical_usage=True,
    )
    admitter = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    admitter.agents.define(spec)

    async def unused_tool(_: ToolContext, value: int) -> int:
        return value

    admitter.tools.register(tool_spec, unused_tool)
    try:
        waiting = await admitter.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await admitter.recovery.pending_requests(run_id))[0]
        return run_id, spec, tool_spec, operation_id, request
    finally:
        await admitter.close()


async def _final_completion(
    calls: list[int],
) -> AsyncIterator[dict[str, object]]:
    calls.append(1)

    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]
        }

    return chunks()


async def _seed_pending_model_reconciliation(
    store: Any,
    tool_spec: ToolSpec | None = None,
) -> tuple[str, AgentSpec, str, Any]:
    run_id, spec, operation_id = await _seed_real_model_in_flight(store, tool_spec)
    admitter = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    admitter.agents.define(spec)
    if tool_spec is not None:
        async def unused_tool(_: ToolContext, value: int) -> int:
            return value

        admitter.tools.register(tool_spec, unused_tool)
    try:
        waiting = await admitter.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await admitter.recovery.pending_requests(run_id))[0]
        return run_id, spec, operation_id, request
    finally:
        await admitter.close()


async def _seed_pending_partial_model_reconciliation(
    store: Any,
    *,
    deltas: tuple[str, ...] = ("partial",),
    tool_spec: ToolSpec | None = None,
) -> tuple[str, AgentSpec, str, Any]:
    run_id, spec, operation_id = await _seed_partial_model_in_flight(
        store,
        deltas=deltas,
        tool_spec=tool_spec,
    )
    admitter = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    admitter.agents.define(spec)
    if tool_spec is not None:

        async def unused_tool(_: ToolContext, value: int) -> int:
            return value

        admitter.tools.register(tool_spec, unused_tool)
    try:
        waiting = await admitter.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await admitter.recovery.pending_requests(run_id))[0]
        return run_id, spec, operation_id, request
    finally:
        await admitter.close()


async def _seed_pending_tool_reconciliation(
    store: Any,
) -> tuple[str, AgentSpec, ToolSpec, str, Any]:
    run_id, spec, tool_spec, operation_id = await _seed_real_tool_in_flight(store)

    async def forbidden_handler(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("reconciliation admission must not call the tool")

    admitter = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    admitter.agents.define(spec)
    admitter.tools.register(tool_spec, forbidden_handler)
    try:
        waiting = await admitter.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await admitter.recovery.pending_requests(run_id))[0]
        return run_id, spec, tool_spec, operation_id, request
    finally:
        await admitter.close()


async def _resolution_public_state(
    store: Any,
    run_id: str,
) -> tuple[Any, ...]:
    run = await store.get_snapshot("run", run_id)
    assert run is not None
    return (
        await store.latest_cursor(),
        tuple(await store.read_events(after_cursor=0)),
        run,
        await store.get_snapshot("session", run["session_id"]),
        await store.get_run_checkpoint(run_id),
        await store.list_pending_reconciliation_requests(run_id),
    )


async def _resolution_domain_state(store: Any) -> tuple[Any, ...]:
    if isinstance(store, InMemoryStore):
        return (
            store._last_cursor,
            tuple(store._events),
            dict(store._snapshots),
            dict(store._run_checkpoints),
            dict(store._external_operations),
            dict(store._reconciliation_requests),
        )
    assert isinstance(store, SQLiteStore)
    tables = (
        "events",
        "snapshots",
        "external_operations",
        "run_checkpoints",
        "reconciliation_requests",
    )
    state: list[Any] = []
    for table in tables:
        async with store._connection.execute(
            f"SELECT * FROM {table} ORDER BY 1"
        ) as cursor:
            state.append(tuple(tuple(row) for row in await cursor.fetchall()))
    return tuple(state)


def _confirmed_provider_result(
    *,
    text: str = "confirmed",
    tool_call: dict[str, object] | None = None,
    usage: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "disposition": "completed",
        "finish_reason": "tool_calls" if tool_call is not None else "stop",
        "text": text,
        "tool_call": tool_call,
        "usage": (
            {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            }
            if usage is None
            else usage
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    ("tool_result", "expected_operation_status"),
    (
        (
            {
                "call_id": "call_resolution",
                "tool_name": "resolution_tool",
                "status": "succeeded",
                "content": "7",
                "value": 7,
                "error": None,
            },
            ExternalOperationStatus.COMPLETED,
        ),
        (
            {
                "call_id": "call_resolution",
                "tool_name": "resolution_tool",
                "status": "succeeded",
                "content": '{"nested":[1,true,null]}',
                "value": {"nested": [1, True, None]},
                "error": None,
            },
            ExternalOperationStatus.COMPLETED,
        ),
        (
            {
                "call_id": "call_resolution",
                "tool_name": "resolution_tool",
                "status": "succeeded",
                "content": "[1,2,3]",
                "value": [1, 2, 3],
                "error": None,
            },
            ExternalOperationStatus.COMPLETED,
        ),
        (
            {
                "call_id": "call_resolution",
                "tool_name": "resolution_tool",
                "status": "succeeded",
                "content": "null",
                "value": None,
                "error": None,
            },
            ExternalOperationStatus.COMPLETED,
        ),
        (
            {
                "call_id": "call_resolution",
                "tool_name": "resolution_tool",
                "status": "failed",
                "content": (
                    '{"error":"tool result is not JSON-compatible or exceeds '
                    'size limit","status":"failed"}'
                ),
                "value": None,
                "error": "tool result is not JSON-compatible or exceeds size limit",
            },
            ExternalOperationStatus.FAILED,
        ),
        (
            {
                "call_id": "call_resolution",
                "tool_name": "resolution_tool",
                "status": "denied",
                "content": '{"error":"permission denied","status":"denied"}',
                "value": None,
                "error": "permission denied",
            },
            ExternalOperationStatus.FAILED,
        ),
        (
            {
                "call_id": "call_resolution",
                "tool_name": "resolution_tool",
                "status": "timed_out",
                "content": (
                    '{"error":"tool execution timed out","status":"timed_out"}'
                ),
                "value": None,
                "error": "tool execution timed out",
            },
            ExternalOperationStatus.FAILED,
        ),
        (
            {
                "call_id": "call_resolution",
                "tool_name": "resolution_tool",
                "status": "invalid_arguments",
                "content": (
                    '{"error":"invalid tool arguments",'
                    '"status":"invalid_arguments"}'
                ),
                "value": None,
                "error": "invalid tool arguments",
            },
            ExternalOperationStatus.FAILED,
        ),
    ),
)
async def test_confirm_completed_tool_projects_and_recovers_exact_result(
    backend: str,
    tool_result: dict[str, object],
    expected_operation_status: ExternalOperationStatus,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "confirmed-tool.sqlite3")
    )
    run_id, spec, tool_spec, operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    provider_calls: list[int] = []
    tool_calls: list[int] = []

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        tool_calls.append(value)
        raise AssertionError("confirmed tool result must not repeat the tool")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(provider_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, forbidden_tool)
    actor = {"type": "operator"}
    evidence = {"tool_result": tool_result}
    try:
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=actor,
            evidence=evidence,
        )

        assert resolved.status is ReconciliationStatus.RESOLVED
        run = await store.get_snapshot("run", run_id)
        assert run is not None
        assert run["status"] == RunStatus.INTERRUPTED.value
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.READY_FOR_MODEL
        assert checkpoint.turn == 1
        assert checkpoint.operation_id is None
        assert checkpoint.model_dump(mode="json")["tool_results"] == [tool_result]
        assert checkpoint.model_dump(mode="json")["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "call_resolution",
            "name": "resolution_tool",
            "content": tool_result["content"],
        }
        operation = await store.get_external_operation(operation_id)
        assert isinstance(operation, ToolCallOperation)
        assert operation.status is expected_operation_status
        assert operation.model_dump(mode="json")["outcome"] == tool_result
        events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        assert tuple(event.type for event in events[-3:]) == (
            "reconciliation.resolved",
            "tool.call.completed",
            "step.completed",
        )
        assert events[-2].payload == tool_result
        assert events[-1].payload == {}
        resolved_cursor = await store.latest_cursor()

        replay = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=actor,
            evidence=evidence,
        )
        assert replay == resolved
        assert await store.latest_cursor() == resolved_cursor
        assert provider_calls == []
        assert tool_calls == []

        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert result.output_text == "done"
        assert result.tool_results[0].model_dump(mode="json") == tool_result
        assert provider_calls == [1]
        assert tool_calls == []

        later_cursor = await store.latest_cursor()
        assert (
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor=actor,
                evidence=evidence,
            )
            == resolved
        )
        assert await store.latest_cursor() == later_cursor
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


async def _replace_run_checkpoint_record(store: Any, checkpoint: Any) -> None:
    serialized = _canonical_record_json(checkpoint)
    if isinstance(store, InMemoryStore):
        store._run_checkpoints[checkpoint.run_id] = serialized
        return
    assert isinstance(store, SQLiteStore)
    await store._connection.execute(
        "UPDATE run_checkpoints SET checkpoint_version = ?, turn = ?, phase = ?, "
        "operation_id = ?, data_json = ? "
        "WHERE run_id = ?",
        (
            checkpoint.checkpoint_version,
            checkpoint.turn,
            checkpoint.phase.value,
            checkpoint.operation_id,
            serialized,
            checkpoint.run_id,
        ),
    )
    await store._connection.commit()


async def _replace_external_operation_record(store: Any, operation: Any) -> None:
    serialized = _canonical_record_json(operation)
    if isinstance(store, InMemoryStore):
        store._external_operations[operation.operation_id] = serialized
        return
    assert isinstance(store, SQLiteStore)
    await store._connection.execute(
        "UPDATE external_operations SET request_fingerprint = ?, status = ?, "
        "provider_identity = ?, tool_identity = ?, data_json = ? "
        "WHERE operation_id = ?",
        (
            operation.request_fingerprint,
            operation.status.value,
            getattr(operation, "provider_identity", None),
            getattr(operation, "tool_identity", None),
            serialized,
            operation.operation_id,
        ),
    )
    await store._connection.commit()


def _forge_model_operation_request(
    operation: ModelCallOperation,
    *,
    marker: str,
) -> ModelCallOperation:
    assert operation.prepared_request is not None
    request = deserialize_model_request(operation.prepared_request)
    forged_request = ModelRequest(
        model=request.model,
        messages=(
            *request.messages,
            {"role": "user", "content": marker},
        ),
        tools=request.tools,
        params=request.params,
        purpose=request.purpose,
    )
    payload = operation.model_dump(mode="python")
    payload.update(
        prepared_request=serialize_model_request(forged_request),
        request_fingerprint=model_request_fingerprint(forged_request),
    )
    return ModelCallOperation.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("entrypoint", ("replay", "recovery"))
@pytest.mark.parametrize(
    "corruption",
    (
        "message_content",
        "message_name",
        "message_call_id",
        "output",
        "usage",
        "tool_result",
        "phase",
        "turn",
        "checkpoint_version",
        "model_fingerprint",
        "model_outcome",
        "tool_fingerprint",
        "tool_outcome",
    ),
)
async def test_confirmed_tool_ready_model_replay_requires_exact_checkpoint_relation(
    backend: str,
    entrypoint: str,
    corruption: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path
            / f"confirmed-tool-ready-model-{entrypoint}-{corruption}.sqlite3"
        )
    )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    provider_calls = 0
    tool_calls = 0

    async def forbidden_provider(**_: object) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("corrupt READY_FOR_MODEL reached the provider")

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        nonlocal tool_calls
        del value
        tool_calls += 1
        raise AssertionError("corrupt READY_FOR_MODEL reached the Tool")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_provider,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, forbidden_tool)
    actor = {"type": "operator"}
    evidence = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": "resolution_tool",
            "status": "succeeded",
            "content": "7",
            "value": 7,
            "error": None,
        }
    }
    try:
        await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=actor,
            evidence=evidence,
        )
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        corrupted_checkpoint = None
        if corruption.startswith("message_"):
            messages = list(checkpoint.messages)
            field = {
                "message_content": "content",
                "message_name": "name",
                "message_call_id": "tool_call_id",
            }[corruption]
            messages[-1] = {**dict(messages[-1]), field: "forged"}
            corrupted_checkpoint = checkpoint.model_copy(
                update={"messages": tuple(messages)}
            )
        elif corruption == "output":
            corrupted_checkpoint = checkpoint.model_copy(
                update={"output_parts": (*checkpoint.output_parts, "forged")}
            )
        elif corruption == "usage":
            corrupted_checkpoint = checkpoint.model_copy(
                update={
                    "usage": checkpoint.usage.model_copy(
                        update={"total_tokens": 99}
                    )
                }
            )
        elif corruption == "tool_result":
            forged_result = checkpoint.tool_results[0].model_copy(
                update={"content": "forged"}
            )
            corrupted_checkpoint = checkpoint.model_copy(
                update={"tool_results": (forged_result,)}
            )
        elif corruption == "phase":
            corrupted_checkpoint = checkpoint.model_copy(
                update={"phase": RunCheckpointPhase.READY_FOR_TOOL}
            )
        elif corruption == "turn":
            corrupted_checkpoint = checkpoint.model_copy(update={"turn": 2})
        elif corruption == "checkpoint_version":
            corrupted_checkpoint = checkpoint.model_copy(
                update={"checkpoint_version": checkpoint.checkpoint_version + 1}
            )
        else:
            operations = await store.list_external_operations(run_id)
            target = next(
                operation
                for operation in operations
                if (
                    isinstance(operation, ModelCallOperation)
                    if corruption.startswith("model_")
                    else isinstance(operation, ToolCallOperation)
                )
            )
            if corruption == "model_fingerprint":
                assert isinstance(target, ModelCallOperation)
                corrupted_operation = _forge_model_operation_request(
                    target,
                    marker="forged model request",
                )
            elif corruption == "tool_fingerprint":
                corrupted_operation = target.model_copy(
                    update={"request_fingerprint": "sha256:forged"}
                )
            else:
                assert corruption.endswith("outcome")
                outcome = target.model_dump(mode="json")["outcome"]
                assert isinstance(outcome, dict)
                corrupted_operation = target.model_copy(
                    update={"outcome": {**outcome, "forged": True}}
                )
            await _replace_external_operation_record(store, corrupted_operation)
        if corrupted_checkpoint is not None:
            await _replace_run_checkpoint_record(store, corrupted_checkpoint)
        before = await _resolution_domain_state(store)

        if entrypoint == "replay":
            with pytest.raises(AgentSDKError) as caught:
                await sdk.recovery.resolve(
                    request.request_id,
                    ReconciliationAction.CONFIRM_COMPLETED,
                    actor=actor,
                    evidence=evidence,
                )

            assert caught.value.code is ErrorCode.CONFLICT
            assert caught.value.message == "recovery state conflict"
            assert caught.value.retryable is True
            assert await _resolution_domain_state(store) == before
        else:
            assert entrypoint == "recovery"
            handle = await sdk.recovery.recover_run(run_id)
            with pytest.raises(AgentSDKError) as caught:
                await handle.result()
            assert caught.value.code is ErrorCode.CONFLICT
            assert caught.value.message == "recovery required"
            assert caught.value.retryable is True
            pending = await sdk.recovery.pending_requests(run_id)
            assert len(pending) == 1
            assert pending[0].reason == "recovery_state_invalid"
        assert provider_calls == 0
        assert tool_calls == 0
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirm_completed_tool_rejects_raw_evidence_before_mutation(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "invalid-confirmed-tool.sqlite3")
    )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("invalid Tool evidence must not call the provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("invalid Tool evidence must not call the tool")

    sdk.tools.register(tool_spec, forbidden_tool)
    valid = {
        "call_id": "call_resolution",
        "tool_name": "resolution_tool",
        "status": "succeeded",
        "content": "7",
        "value": 7,
        "error": None,
    }
    cyclic_list: list[object] = []
    cyclic_list.append(cyclic_list)
    cyclic_object: dict[str, object] = {}
    cyclic_object["self"] = cyclic_object
    invalid_results: tuple[object, ...] = (
        {**valid, "value": _nested_json_list(65)},
        {**valid, "value": _nested_json_object(65)},
        {**valid, "value": cyclic_list},
        {**valid, "value": cyclic_object},
        {**valid, "value": _RaisingMapping()},
        _RaisingMapping(),
        {**valid, "value": [0] * 4096},
        {**valid, "value": {"nested": [float("inf")]}},
        {**valid, "value": _JSONListSubclass([1])},
        {**valid, "value": _JSONDictSubclass({"value": 1})},
        {**valid, "call_id": "call_wrong"},
        {**valid, "tool_name": "wrong_tool"},
        {**valid, "status": "invented"},
        {**valid, "call_id": 7},
        {**valid, "tool_name": 7},
        {**valid, "content": 7},
        {**valid, "error": 7},
        {**valid, "value": {1: "coerced-key"}},
        {**valid, "value": (1, 2)},
        {**valid, "value": float("nan")},
        {**valid, "content": "x" * (16 * 1024 + 1)},
        {**valid, "error": "x" * 513},
        {**valid, "value": "x" * (16 * 1024)},
        {**valid, "extra": True},
    )
    try:
        before = await _resolution_domain_state(store)
        for invalid in invalid_results:
            with pytest.raises(AgentSDKError) as caught:
                await sdk.recovery.resolve(
                    request.request_id,
                    ReconciliationAction.CONFIRM_COMPLETED,
                    actor={"type": "operator"},
                    evidence={"tool_result": invalid},
                )
            assert caught.value.code is ErrorCode.INVALID_STATE
            assert caught.value.message == "reconciliation decision is invalid"
            assert caught.value.retryable is False
            assert await _resolution_domain_state(store) == before

        with pytest.raises(AgentSDKError) as extra_top:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"type": "operator"},
                evidence={"tool_result": valid, "extra": True},
            )
        assert extra_top.value.code is ErrorCode.INVALID_STATE
        assert await _resolution_domain_state(store) == before
        assert (await sdk.recovery.pending_requests(run_id)) == (request,)
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("boundary", ("depth", "nodes", "bytes"))
async def test_confirm_completed_tool_accepts_strict_json_budget_boundaries(
    backend: str,
    boundary: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"confirmed-tool-{boundary}-boundary.sqlite3"
        )
    )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("confirmed Tool evidence must not call the provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("confirmed Tool evidence must not call the tool")

    sdk.tools.register(tool_spec, forbidden_tool)
    values = {
        "depth": _nested_json_list(64),
        "nodes": [0] * 4095,
        "bytes": "x" * 16382,
    }
    tool_result = {
        "call_id": "call_resolution",
        "tool_name": "resolution_tool",
        "status": "succeeded",
        "content": "bounded",
        "value": values[boundary],
        "error": None,
    }
    try:
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator"},
            evidence={"tool_result": tool_result},
        )

        assert resolved.resolution is not None
        assert resolved.resolution.model_dump(mode="json")["evidence"] == {
            "tool_result": tool_result
        }
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.tool_results[0].model_dump(mode="json") == tool_result
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirm_completed_tool_detaches_boundary_evidence_and_conflicts_changed_replay(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "detached-confirmed-tool.sqlite3")
    )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("Tool resolution must not call the provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("Tool resolution must not call the tool")

    sdk.tools.register(tool_spec, forbidden_tool)
    source_result: dict[str, Any] = {
        "call_id": "call_resolution",
        "tool_name": "resolution_tool",
        "status": "succeeded",
        "content": "x" * (16 * 1024),
        "value": {"items": [1, None]},
        "error": None,
    }
    submitted = json.loads(json.dumps(source_result))
    actor = {"type": "operator"}
    try:
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=actor,
            evidence={"tool_result": source_result},
        )
        source_result["content"] = "mutated"
        source_result["value"]["items"][0] = 99

        assert resolved.resolution is not None
        assert resolved.resolution.model_dump(mode="json")["evidence"] == {
            "tool_result": submitted
        }
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.tool_results[0].model_dump(mode="json") == submitted
        cursor = await store.latest_cursor()
        assert (
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor=actor,
                evidence={"tool_result": submitted},
            )
            == resolved
        )
        with pytest.raises(AgentSDKError) as changed:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor=actor,
                evidence={"tool_result": source_result},
            )
        assert changed.value.code is ErrorCode.CONFLICT
        assert await store.latest_cursor() == cursor
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirm_completed_tool_post_commit_ambiguity_converges_once(
    backend: str,
    tmp_path: Path,
) -> None:
    if backend == "memory":
        store: Any = _AmbiguousResolutionMemoryStore()
    else:
        opened = await SQLiteStore.open(tmp_path / "ambiguous-confirmed-tool.sqlite3")
        store = _AmbiguousResolutionSQLiteStore(opened._connection)
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("Tool resolution must not call the provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("Tool resolution must not call the tool")

    sdk.tools.register(tool_spec, forbidden_tool)
    evidence = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": "resolution_tool",
            "status": "succeeded",
            "content": "7",
            "value": 7,
            "error": None,
        }
    }
    try:
        before = await store.latest_cursor()
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator"},
            evidence=evidence,
        )

        assert resolved.status is ReconciliationStatus.RESOLVED
        assert store.resolution_batches == 2
        assert await store.latest_cursor() == before + 3
        events = await store.read_events(after_cursor=0)
        assert sum(
            item.event.type == "reconciliation.resolved" for item in events
        ) == 1
        assert (await sdk.runs.get(run_id)).status is RunStatus.INTERRUPTED
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_two_sdk_confirm_completed_tool_converges_without_callbacks(
    backend: str,
    tmp_path: Path,
) -> None:
    if backend == "memory":
        owner_store: Any = _ResolutionBarrierMemoryStore()
        follower_store: Any = owner_store
    else:
        opened = await SQLiteStore.open(tmp_path / "two-sdk-confirmed-tool.sqlite3")
        owner_store = _ResolutionBarrierSQLiteStore(opened._connection)
        follower_store = await SQLiteStore.open(
            tmp_path / "two-sdk-confirmed-tool.sqlite3"
        )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(owner_store)
    )
    callback_calls: list[str] = []

    async def forbidden_provider(**_: object) -> Any:
        callback_calls.append("provider")
        raise AssertionError("Tool resolution must not call the provider")

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        callback_calls.append("tool")
        raise AssertionError("Tool resolution must not call the tool")

    owner = AgentSDK.for_test(
        store=owner_store,
        acompletion=forbidden_provider,
        permission_default="allow",
    )
    follower = AgentSDK.for_test(
        store=follower_store,
        acompletion=forbidden_provider,
        permission_default="allow",
    )
    for sdk in (owner, follower):
        sdk.agents.define(spec)
        sdk.tools.register(tool_spec, forbidden_tool)
    evidence = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": "resolution_tool",
            "status": "failed",
            "content": '{"error":"tool handler failed","status":"failed"}',
            "value": None,
            "error": "tool handler failed",
        }
    }
    owner_store.resolution_barrier_enabled = True
    try:
        first = asyncio.create_task(
            owner.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"type": "operator"},
                evidence=evidence,
            )
        )
        await asyncio.wait_for(owner_store.resolution_reached.wait(), timeout=10)
        second = asyncio.create_task(
            follower.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"type": "operator"},
                evidence=evidence,
            )
        )
        await asyncio.sleep(0)
        owner_store.allow_resolution.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert first_result == second_result
        assert callback_calls == []
        assert (await owner.runs.get(run_id)).status is RunStatus.INTERRUPTED
        events = await owner_store.read_events(after_cursor=0)
        assert sum(
            item.event.type == "reconciliation.resolved" for item in events
        ) == 1
    finally:
        owner_store.allow_resolution.set()
        await asyncio.gather(owner.close(), follower.close())
        if backend == "sqlite":
            await owner_store.close()
            await follower_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_tool_replay_survives_later_pending_model_reconciliation(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "confirmed-tool-later-pending.sqlite3")
    )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_provider(**_: object) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("confirmed Tool must not be repeated")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=blocked_provider,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, forbidden_tool)
    actor = {"type": "operator"}
    evidence = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": "resolution_tool",
            "status": "succeeded",
            "content": "7",
            "value": 7,
            "error": None,
        }
    }
    try:
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=actor,
            evidence=evidence,
        )
        handle = await sdk.recovery.recover_run(run_id)
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert handle._task is not None
        handle._task.cancel()
        with pytest.raises(AgentSDKError):
            await handle.result()
        await asyncio.wait_for(cancelled.wait(), timeout=10)
        await sdk.close()
        await _mark_interrupted(store)

        reopened = AgentSDK.for_test(
            store=store,
            acompletion=lambda **_: (_ for _ in ()).throw(
                AssertionError("reconciliation admission must not call provider")
            ),
            permission_default="allow",
        )
        reopened.agents.define(spec)
        reopened.tools.register(tool_spec, forbidden_tool)
        try:
            waiting = await reopened.recovery.recover_run(run_id)
            with pytest.raises(AgentSDKError, match="recovery required"):
                await waiting.result()
            pending = await reopened.recovery.pending_requests(run_id)
            assert len(pending) == 1
            assert pending[0].request_id != request.request_id
            before = await store.latest_cursor()

            replay = await reopened.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor=actor,
                evidence=evidence,
            )

            assert replay == resolved
            assert await store.latest_cursor() == before
        finally:
            await reopened.close()
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


async def _seed_confirmed_tool_then_resolved_model_reconciliation(
    store: Any,
) -> tuple[str, AgentSpec, ToolSpec, Any, Any, Any, dict[str, object]]:
    run_id, spec, tool_spec, _operation_id, tool_request = (
        await _seed_pending_tool_reconciliation(store)
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_provider(**_: object) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("confirmed Tool must not be repeated")

    tool_evidence: dict[str, object] = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": "resolution_tool",
            "status": "succeeded",
            "content": "7",
            "value": 7,
            "error": None,
        }
    }
    runner = AgentSDK.for_test(
        store=store,
        acompletion=blocked_provider,
        permission_default="allow",
    )
    runner.agents.define(spec)
    runner.tools.register(tool_spec, forbidden_tool)
    try:
        tool_resolved = await runner.recovery.resolve(
            tool_request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator"},
            evidence=tool_evidence,
        )
        handle = await runner.recovery.recover_run(run_id)
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert handle._task is not None
        handle._task.cancel()
        with pytest.raises(AgentSDKError):
            await handle.result()
        await asyncio.wait_for(cancelled.wait(), timeout=10)
    finally:
        await runner.close()
    await _mark_interrupted(store)

    resolver = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("resolution must not call provider")
        ),
        permission_default="allow",
    )
    resolver.agents.define(spec)
    resolver.tools.register(tool_spec, forbidden_tool)
    try:
        waiting = await resolver.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        model_request = (await resolver.recovery.pending_requests(run_id))[0]
        model_resolved = await resolver.recovery.resolve(
            model_request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )
    finally:
        await resolver.close()
    return (
        run_id,
        spec,
        tool_spec,
        tool_request,
        tool_resolved,
        model_resolved,
        tool_evidence,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_tool_replay_survives_later_resolved_model_reconciliation(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "confirmed-tool-later-resolved.db")
    )
    (
        run_id,
        spec,
        tool_spec,
        tool_request,
        tool_resolved,
        _model_resolved,
        tool_evidence,
    ) = await _seed_confirmed_tool_then_resolved_model_reconciliation(store)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("exact replay must not repeat the Tool")

    replay = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("exact replay must not call provider")
        ),
        permission_default="allow",
    )
    replay.agents.define(spec)
    replay.tools.register(tool_spec, forbidden_tool)
    try:
        before = await _resolution_domain_state(store)
        exact = await replay.recovery.resolve(
            tool_request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator"},
            evidence=tool_evidence,
        )
        assert exact == tool_resolved
        assert await _resolution_domain_state(store) == before
        assert (await replay.runs.get(run_id)).status is RunStatus.INTERRUPTED
    finally:
        await replay.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_tool_replay_accepts_a_prior_resolved_model_attempt(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "resolved-before-confirmed-tool.db")
    )
    tool_spec = _unsafe_tool_spec()
    run_id, spec, _operation_id, model_request = (
        await _seed_pending_model_reconciliation(store, tool_spec)
    )
    tool_entered = asyncio.Event()
    tool_cancelled = asyncio.Event()

    async def tool_completion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_resolution",
                                    "function": {
                                        "name": tool_spec.name,
                                        "arguments": '{"value":7}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

        return chunks()

    async def blocked_tool(_: ToolContext, value: int) -> int:
        del value
        tool_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            tool_cancelled.set()
            raise

    runner = AgentSDK.for_test(
        store=store,
        acompletion=tool_completion,
        permission_default="allow",
    )
    runner.agents.define(spec)
    runner.tools.register(tool_spec, blocked_tool)
    try:
        await runner.recovery.resolve(
            model_request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )
        handle = await runner.recovery.recover_run(run_id)
        await asyncio.wait_for(tool_entered.wait(), timeout=10)
        assert handle._task is not None
        handle._task.cancel()
        with pytest.raises(AgentSDKError):
            await handle.result()
        await asyncio.wait_for(tool_cancelled.wait(), timeout=10)
    finally:
        await runner.close()
    await _mark_interrupted(store)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("confirmed Tool must not be repeated")

    resolver = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("Tool resolution must not call provider")
        ),
        permission_default="allow",
    )
    resolver.agents.define(spec)
    resolver.tools.register(tool_spec, forbidden_tool)
    tool_evidence = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": tool_spec.name,
            "status": "succeeded",
            "content": "7",
            "value": 7,
            "error": None,
        }
    }
    try:
        waiting = await resolver.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        tool_request = (await resolver.recovery.pending_requests(run_id))[0]
        tool_resolved = await resolver.recovery.resolve(
            tool_request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator"},
            evidence=tool_evidence,
        )
        before = await _resolution_domain_state(store)
        exact = await resolver.recovery.resolve(
            tool_request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator"},
            evidence=tool_evidence,
        )
        assert exact == tool_resolved
        assert await _resolution_domain_state(store) == before
    finally:
        await resolver.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("corruption", ("orphan", "duplicate"))
async def test_confirmed_tool_multi_resolution_replay_rejects_open_history(
    backend: str,
    corruption: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / f"tool-multi-{corruption}.db")
    )
    (
        run_id,
        spec,
        tool_spec,
        tool_request,
        tool_resolved,
        model_resolved,
        tool_evidence,
    ) = await _seed_confirmed_tool_then_resolved_model_reconciliation(store)
    if corruption == "orphan":
        model_operation = next(
            item
            for item in await store.list_external_operations(run_id)
            if isinstance(item, ModelCallOperation)
        )
        await _inject_confirmed_replay_orphan(
            store,
            resolved=tool_resolved,
            operation=model_operation,
            orphan="resolved_request",
        )
    else:
        await _insert_duplicate_run_event_before_paired_interrupt(
            store,
            run_id=run_id,
            request_id=model_resolved.request_id,
            event_type="reconciliation.resolved",
        )

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("conflicting replay must not repeat the Tool")

    replay = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("conflicting replay must not call provider")
        ),
        permission_default="allow",
    )
    replay.agents.define(spec)
    replay.tools.register(tool_spec, forbidden_tool)
    try:
        before = await _resolution_domain_state(store)
        with pytest.raises(AgentSDKError) as caught:
            await replay.recovery.resolve(
                tool_request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"type": "operator"},
                evidence=tool_evidence,
            )
        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery state conflict"
        assert await _resolution_domain_state(store) == before
    finally:
        await replay.close()
        if isinstance(store, SQLiteStore):
            await store.close()


async def _seed_confirmed_tool_then_resolved_tool_reconciliation(
    store: Any,
) -> tuple[str, AgentSpec, ToolSpec, Any, Any, Any, dict[str, object]]:
    run_id, spec, tool_spec, _operation_id, original_request = (
        await _seed_pending_tool_reconciliation(store)
    )
    tool_entered = asyncio.Event()
    tool_cancelled = asyncio.Event()

    async def next_tool_completion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_later_resolution",
                                    "function": {
                                        "name": tool_spec.name,
                                        "arguments": '{"value":9}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

        return chunks()

    async def blocked_later_tool(_: ToolContext, value: int) -> int:
        assert value == 9
        tool_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            tool_cancelled.set()
            raise

    original_evidence: dict[str, object] = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": tool_spec.name,
            "status": "succeeded",
            "content": "7",
            "value": 7,
            "error": None,
        }
    }
    runner = AgentSDK.for_test(
        store=store,
        acompletion=next_tool_completion,
        permission_default="allow",
    )
    runner.agents.define(spec)
    runner.tools.register(tool_spec, blocked_later_tool)
    try:
        original_resolved = await runner.recovery.resolve(
            original_request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator"},
            evidence=original_evidence,
        )
        handle = await runner.recovery.recover_run(run_id)
        await asyncio.wait_for(tool_entered.wait(), timeout=10)
        assert handle._task is not None
        handle._task.cancel()
        with pytest.raises(AgentSDKError):
            await handle.result()
        await asyncio.wait_for(tool_cancelled.wait(), timeout=10)
    finally:
        await runner.close()
    await _mark_interrupted(store)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("resolution must not invoke a Tool")

    resolver = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("resolution must not call provider")
        ),
        permission_default="allow",
    )
    resolver.agents.define(spec)
    resolver.tools.register(tool_spec, forbidden_tool)
    try:
        waiting = await resolver.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        later_request = (await resolver.recovery.pending_requests(run_id))[0]
        assert later_request.reason == "tool_call_unknown_outcome"
        later_resolved = await resolver.recovery.resolve(
            later_request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.READY_FOR_TOOL
        assert checkpoint.turn == 1
    finally:
        await resolver.close()
    return (
        run_id,
        spec,
        tool_spec,
        original_request,
        original_resolved,
        later_resolved,
        original_evidence,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_tool_replay_accepts_later_resolved_ready_for_tool_state(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "confirmed-tool-later-ready-tool.db")
    )
    (
        run_id,
        spec,
        tool_spec,
        original_request,
        original_resolved,
        _later_resolved,
        original_evidence,
    ) = await _seed_confirmed_tool_then_resolved_tool_reconciliation(store)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("exact replay must not invoke a Tool")

    replay = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("exact replay must not call provider")
        ),
        permission_default="allow",
    )
    replay.agents.define(spec)
    replay.tools.register(tool_spec, forbidden_tool)
    try:
        before = await _resolution_domain_state(store)
        exact = await replay.recovery.resolve(
            original_request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator"},
            evidence=original_evidence,
        )
        assert exact == original_resolved
        assert await _resolution_domain_state(store) == before
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.READY_FOR_TOOL
        assert checkpoint.turn == 1
    finally:
        await replay.close()
        if isinstance(store, SQLiteStore):
            await store.close()


async def _corrupt_confirmed_tool_current_ready_tool_state(
    store: Any,
    *,
    run_id: str,
    corruption: str,
) -> None:
    if corruption == "checkpoint":
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        corrupted = checkpoint.model_copy(
            update={"output_parts": (*checkpoint.output_parts, "corrupt")}
        )
        serialized = _canonical_record_json(corrupted)
        if isinstance(store, InMemoryStore):
            store._run_checkpoints[run_id] = serialized
        else:
            assert isinstance(store, SQLiteStore)
            await store._connection.execute(
                "UPDATE run_checkpoints SET data_json = ? WHERE run_id = ?",
                (serialized, run_id),
            )
            await store._connection.commit()
        return

    if corruption == "event":
        stored_events = list(await store.read_events(after_cursor=0))
        target = next(
            stored
            for stored in reversed(stored_events)
            if stored.event.run_id == run_id
            and stored.event.type == "model.call.completed"
        )
        replacement = target._replace(
            event=target.event.model_copy(
                update={"payload": {"finish_reason": "stop"}}
            )
        )
        stored_events[stored_events.index(target)] = replacement
        await _replace_resolution_event_log(store, stored_events)
        return

    assert corruption == "operation"
    operations = await store.list_external_operations(run_id)
    operation = next(
        item
        for item in operations
        if isinstance(item, ModelCallOperation) and item.turn == 1
    )
    corrupted = _forge_model_operation_request(
        operation,
        marker="corrupt current model request",
    )
    serialized = _canonical_record_json(corrupted)
    if isinstance(store, InMemoryStore):
        store._external_operations[operation.operation_id] = serialized
        return
    assert isinstance(store, SQLiteStore)
    await store._connection.execute(
        "UPDATE external_operations SET request_fingerprint = ?, data_json = ? "
        "WHERE operation_id = ?",
        (corrupted.request_fingerprint, serialized, corrupted.operation_id),
    )
    await store._connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("corruption", ("checkpoint", "event", "operation"))
async def test_confirmed_tool_later_ready_tool_replay_rejects_corrupt_current_state(
    backend: str,
    corruption: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / f"ready-tool-{corruption}.db")
    )
    (
        run_id,
        spec,
        tool_spec,
        original_request,
        _original_resolved,
        _later_resolved,
        original_evidence,
    ) = await _seed_confirmed_tool_then_resolved_tool_reconciliation(store)
    await _corrupt_confirmed_tool_current_ready_tool_state(
        store,
        run_id=run_id,
        corruption=corruption,
    )

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("conflicting replay must not invoke a Tool")

    replay = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("conflicting replay must not call provider")
        ),
        permission_default="allow",
    )
    replay.agents.define(spec)
    replay.tools.register(tool_spec, forbidden_tool)
    try:
        before = await _resolution_domain_state(store)
        with pytest.raises(AgentSDKError) as caught:
            await replay.recovery.resolve(
                original_request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"type": "operator"},
                evidence=original_evidence,
            )
        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery state conflict"
        assert await _resolution_domain_state(store) == before
    finally:
        await replay.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_tool_replay_survives_later_tool_turn(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "confirmed-tool-later-tool.sqlite3")
    )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    provider_calls: list[int] = []
    tool_calls: list[int] = []

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_calls.append(1)
        attempt = len(provider_calls)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if attempt == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_after_confirmed_tool",
                                        "function": {
                                            "name": "resolution_tool",
                                            "arguments": '{"value":8}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                yield {
                    "choices": [
                        {"delta": {"content": "done"}, "finish_reason": "stop"}
                    ]
                }

        return chunks()

    async def later_tool(_: ToolContext, value: int) -> int:
        tool_calls.append(value)
        return value + 1

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, later_tool)
    actor = {"type": "operator"}
    evidence = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": "resolution_tool",
            "status": "succeeded",
            "content": "7",
            "value": 7,
            "error": None,
        }
    }
    try:
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=actor,
            evidence=evidence,
        )
        result = await (await sdk.recovery.recover_run(run_id)).result()

        assert result.output_text == "done"
        assert len(provider_calls) == 2
        assert tool_calls == [8]
        assert len(result.tool_results) == 2
        cursor = await store.latest_cursor()
        assert (
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor=actor,
                evidence=evidence,
            )
            == resolved
        )
        assert await store.latest_cursor() == cursor
        assert tool_calls == [8]
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_tool_retains_closing_session_until_explicit_recovery(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "closing-confirmed-tool.sqlite3")
    )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    provider_calls: list[int] = []

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("confirmed Tool must not be repeated")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(provider_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, forbidden_tool)
    try:
        run = await sdk.runs.get(run_id)
        closing = await sdk.sessions.close(run.session_id)
        assert closing.status is SessionStatus.CLOSING
        with pytest.raises(AgentSDKError) as busy:
            await sdk.sessions.delete(run.session_id)
        assert busy.value.code is ErrorCode.CONFLICT

        await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator"},
            evidence={
                "tool_result": {
                    "call_id": "call_resolution",
                    "tool_name": "resolution_tool",
                    "status": "failed",
                    "content": (
                        '{"error":"tool handler failed","status":"failed"}'
                    ),
                    "value": None,
                    "error": "tool handler failed",
                }
            },
        )
        retained = await sdk.sessions.get(run.session_id)
        assert retained.status is SessionStatus.CLOSING
        assert retained.active_run_ids == (run_id,)
        assert provider_calls == []

        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert result.output_text == "done"
        assert provider_calls == [1]
        closed = await sdk.sessions.get(run.session_id)
        assert closed.status is SessionStatus.CLOSED
        assert closed.active_run_ids == ()
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ("cancel", "sdk_close"))
async def test_confirmed_tool_resolution_lifecycle_is_atomic(
    lifecycle: str,
) -> None:
    store = _ResolutionBarrierMemoryStore()
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    callback_calls: list[str] = []

    async def forbidden_provider(**_: object) -> Any:
        callback_calls.append("provider")
        raise AssertionError("Tool resolution must not call the provider")

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        callback_calls.append("tool")
        raise AssertionError("Tool resolution must not call the tool")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_provider,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, forbidden_tool)
    actor = {"type": "operator"}
    evidence = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": "resolution_tool",
            "status": "succeeded",
            "content": "7",
            "value": 7,
            "error": None,
        }
    }
    resolve_task: asyncio.Task[Any] | None = None
    close_task: asyncio.Task[None] | None = None
    try:
        before = await _resolution_domain_state(store)
        before_cursor = await store.latest_cursor()
        store.resolution_barrier_enabled = True
        resolve_task = asyncio.create_task(
            sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor=actor,
                evidence=evidence,
            )
        )
        await asyncio.wait_for(store.resolution_reached.wait(), timeout=10)

        if lifecycle == "cancel":
            assert resolve_task.cancel()
        else:
            close_task = asyncio.create_task(sdk.close())
            await asyncio.wait_for(sdk._lifecycle.close_signal.wait(), timeout=10)
            assert not close_task.done()
        assert await _resolution_domain_state(store) == before
        assert callback_calls == []

        store.allow_resolution.set()
        if lifecycle == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(resolve_task, timeout=10)
        else:
            resolved = await asyncio.wait_for(resolve_task, timeout=10)
            assert resolved.status is ReconciliationStatus.RESOLVED
            assert close_task is not None
            await asyncio.wait_for(close_task, timeout=10)

        durable = await store.get_reconciliation_request(request.request_id)
        assert durable is not None
        assert durable.status is ReconciliationStatus.RESOLVED
        assert await store.latest_cursor() == before_cursor + 3
        assert callback_calls == []
        assert (await store.get_snapshot("run", run_id))["status"] == (
            RunStatus.INTERRUPTED.value
        )
        if lifecycle == "cancel":
            cursor = await store.latest_cursor()
            assert (
                await sdk.recovery.resolve(
                    request.request_id,
                    ReconciliationAction.CONFIRM_COMPLETED,
                    actor=actor,
                    evidence=evidence,
                )
                == durable
            )
            assert await store.latest_cursor() == cursor
    finally:
        store.allow_resolution.set()
        if resolve_task is not None and not resolve_task.done():
            await asyncio.gather(resolve_task, return_exceptions=True)
        if close_task is not None and not close_task.done():
            await close_task
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirm_completed_tool_rejects_capability_drift_without_mutation(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "drift-confirmed-tool.sqlite3")
    )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("capability drift must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("capability drift must not call Tool")

    sdk.tools.register(
        tool_spec.model_copy(update={"version": "drift-secret"}),
        forbidden_tool,
    )
    try:
        before = await _resolution_domain_state(store)
        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"type": "operator"},
                evidence={
                    "tool_result": {
                        "call_id": "call_resolution",
                        "tool_name": "resolution_tool",
                        "status": "succeeded",
                        "content": "7",
                        "value": 7,
                        "error": None,
                    }
                },
            )

        assert caught.value.code is ErrorCode.INVALID_STATE
        assert caught.value.message == "recovery capabilities unavailable"
        assert await _resolution_domain_state(store) == before
        assert (await sdk.runs.get(run_id)).status is RunStatus.WAITING_RECONCILIATION
        _assert_secret_free(caught.value, "drift-secret")
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_store_rejects_malformed_old_generation_confirmed_tool_batches(
    backend: str,
    tmp_path: Path,
) -> None:
    if backend == "memory":
        store: Any = _CaptureResolutionMemoryStore()
    else:
        opened = await SQLiteStore.open(tmp_path / "malformed-confirmed-tool.sqlite3")
        store = _CaptureResolutionSQLiteStore(opened._connection)
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("Tool resolution must not call the provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("Tool resolution must not call the tool")

    sdk.tools.register(tool_spec, forbidden_tool)
    store.capture_resolution = True
    try:
        before = await _resolution_domain_state(store)
        with pytest.raises(AgentSDKError) as captured:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"type": "operator"},
                evidence={
                    "tool_result": {
                        "call_id": "call_resolution",
                        "tool_name": "resolution_tool",
                        "status": "succeeded",
                        "content": "7",
                        "value": 7,
                        "error": None,
                    }
                },
            )
        assert captured.value.code is ErrorCode.INTERNAL
        batch = store.captured_batch
        assert batch is not None
        assert await _resolution_domain_state(store) == before
        store.capture_resolution = False
        lease = await store.acquire_lease(
            run_id=run_id,
            owner=f"malformed-{backend}",
            now=batch.now,
            expires_at=batch.now + timedelta(seconds=30),
        )
        assert batch.operation is not None
        assert batch.operation.expected is not None
        assert batch.checkpoint is not None
        assert batch.checkpoint.expected is not None
        run_write = batch.snapshots[0]
        projected_run = RunStatus(run_write.data["status"])
        assert projected_run is RunStatus.INTERRUPTED
        malformed_run = {
            **run_write.data,
            "status": RunStatus.WAITING_RECONCILIATION.value,
        }
        malformed_batches = (
            batch._replace(
                lease=lease,
                events=(batch.events[0], batch.events[2], batch.events[1]),
            ),
            batch._replace(
                lease=lease,
                events=batch.events[:-1],
            ),
            batch._replace(
                lease=lease,
                operation=batch.operation._replace(
                    updated=batch.operation.updated.model_copy(
                        update={"status": ExternalOperationStatus.FAILED}
                    )
                ),
            ),
            batch._replace(
                lease=lease,
                checkpoint=batch.checkpoint._replace(
                    updated=batch.checkpoint.updated.model_copy(
                        update={"phase": RunCheckpointPhase.READY_FOR_TOOL}
                    )
                ),
            ),
            batch._replace(
                lease=lease,
                snapshots=(run_write._replace(data=malformed_run),),
            ),
        )
        for malformed in malformed_batches:
            with pytest.raises(RecoveryStateConflictError):
                await store.commit_run_progress(malformed)
            assert await _resolution_domain_state(store) == before
        await store.release_lease(lease)
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


async def _remove_confirmed_tool_started_marker(store: Any, run_id: str) -> None:
    events = tuple(
        stored
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run_id
    )
    started = next(stored for stored in events if stored.event.type == "tool.call.started")
    authorized = next(
        stored for stored in events if stored.event.type == "tool.call.authorized"
    )
    replacement = started.event.model_copy(
        update={
            "type": authorized.event.type,
            "payload": authorized.event.payload,
        }
    )
    if isinstance(store, InMemoryStore):
        index = store._events.index(started)
        store._events[index] = started._replace(event=replacement)
        return
    await store._connection.execute(
        "UPDATE events SET type = ?, payload_json = ? WHERE cursor = ?",
        (
            replacement.type,
            json.dumps(
                replacement.payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            started.cursor,
        ),
    )
    await store._connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("entrypoint", ("replay", "recovery"))
@pytest.mark.parametrize(
    ("corruption", "marker"),
    (
        ("duplicate", "tool.call.proposed"),
        ("duplicate", "tool.call.started"),
        ("moved", "tool.call.proposed"),
        ("moved", "tool.call.authorized"),
        ("missing", "tool.call.started"),
    ),
)
async def test_confirmed_tool_replay_rejects_corrupt_lifecycle_markers(
    backend: str,
    entrypoint: str,
    corruption: str,
    marker: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path
            / f"corrupt-confirmed-tool-{entrypoint}-{corruption}-{marker}.sqlite3"
        )
    )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("corrupt replay must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("corrupt replay must not call Tool")

    sdk.tools.register(tool_spec, forbidden_tool)
    actor = {"type": "operator"}
    evidence = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": "resolution_tool",
            "status": "succeeded",
            "content": "7",
            "value": 7,
            "error": None,
        }
    }
    try:
        await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=actor,
            evidence=evidence,
        )
        if corruption == "duplicate":
            await _insert_duplicate_run_event_before_paired_interrupt(
                store,
                run_id=run_id,
                request_id=request.request_id,
                event_type=marker,
            )
        elif corruption == "moved":
            await _move_run_event_before_paired_interrupt(
                store,
                run_id=run_id,
                request_id=request.request_id,
                event_type=marker,
            )
        else:
            assert corruption == "missing"
            await _remove_confirmed_tool_started_marker(store, run_id)
        before = await _resolution_domain_state(store)

        if entrypoint == "replay":
            with pytest.raises(AgentSDKError) as conflict:
                await sdk.recovery.resolve(
                    request.request_id,
                    ReconciliationAction.CONFIRM_COMPLETED,
                    actor=actor,
                    evidence=evidence,
                )

            assert conflict.value.code is ErrorCode.CONFLICT
            assert conflict.value.message == "recovery state conflict"
            assert await _resolution_domain_state(store) == before
        else:
            assert entrypoint == "recovery"
            with pytest.raises(AgentSDKError) as conflict:
                await (await sdk.recovery.recover_run(run_id)).result()
            assert conflict.value.code is ErrorCode.CONFLICT
            assert conflict.value.message == "recovery required"
            pending = await sdk.recovery.pending_requests(run_id)
            assert len(pending) == 1
            assert pending[0].reason == "recovery_state_invalid"
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("projection", ("text", "tool_call", "failed"))
async def test_confirm_completed_model_projects_exact_durable_outcome(
    backend: str,
    projection: str,
    tmp_path: Path,
) -> None:
    store: Any
    if backend == "memory":
        store = InMemoryStore()
    else:
        store = await SQLiteStore.open(tmp_path / f"confirm-model-{projection}.db")
    tool_spec = _unsafe_tool_spec()
    run_id, spec, operation_id, request = await _seed_pending_model_reconciliation(
        store,
        tool_spec if projection == "tool_call" else None,
    )
    provider_calls: list[int] = []
    tool_calls: list[int] = []

    async def forbidden_provider(**_: object) -> Any:
        provider_calls.append(1)
        raise AssertionError("resolution must not call the provider")

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        tool_calls.append(1)
        raise AssertionError("resolution must not call a tool")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_provider,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    if projection == "tool_call":
        sdk.tools.register(tool_spec, forbidden_tool)
    try:
        if projection == "failed":
            provider_result: dict[str, object] = {
                "disposition": "failed",
                "error_code": "internal",
                "retryable": True,
            }
        else:
            raw_call = (
                {
                    "index": 0,
                    "call_id": "call_confirmed",
                    "name": tool_spec.name,
                    "arguments_json": '{"value":7}',
                }
                if projection == "tool_call"
                else None
            )
            provider_result = _confirmed_provider_result(
                text="confirmed",
                tool_call=raw_call,
            )

        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "test"},
            evidence={"provider_result": provider_result},
        )

        assert resolved.status.value == "resolved"
        assert provider_calls == []
        assert tool_calls == []
        operation = next(
            item
            for item in await store.list_external_operations(run_id)
            if item.operation_id == operation_id
        )
        assert isinstance(operation, ModelCallOperation)
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        run = await sdk.runs.get(run_id)
        session = await sdk.sessions.get(run.session_id)
        event_types = tuple(
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            or (
                stored.event.run_id is None
                and stored.event.session_id == run.session_id
                and stored.event.type in {"session.run.detached", "session.closed"}
            )
        )

        if projection == "tool_call":
            assert operation.status is ExternalOperationStatus.COMPLETED
            assert operation.outcome == {
                "finish_reason": "tool_calls",
                "text": "confirmed",
                "tool_calls": (
                    {
                        "index": 0,
                        "call_id": "call_confirmed",
                        "name": tool_spec.name,
                        "arguments_json": '{"value":7}',
                    },
                ),
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                },
            }
            assert checkpoint.phase is RunCheckpointPhase.READY_FOR_TOOL
            assert checkpoint.operation_id is None
            assert checkpoint.output_parts == ("confirmed",)
            assert run.status is RunStatus.INTERRUPTED
            assert run_id in session.active_run_ids
            assert event_types[-3:] == (
                "reconciliation.resolved",
                "model.usage.reported",
                "model.call.completed",
            )
        elif projection == "text":
            assert operation.status is ExternalOperationStatus.COMPLETED
            assert checkpoint.phase is RunCheckpointPhase.TERMINAL
            assert run.status is RunStatus.COMPLETED
            assert run.output_text == "confirmed"
            assert run.usage is not None and run.usage.total_tokens == 7
            assert run_id not in session.active_run_ids
            assert event_types[-6:] == (
                "reconciliation.resolved",
                "model.usage.reported",
                "model.call.completed",
                "step.completed",
                "run.completed",
                "session.run.detached",
            )
        else:
            assert operation.status is ExternalOperationStatus.FAILED
            assert operation.outcome == {
                "error": {"code": "internal", "message": "model call failed"}
            }
            assert checkpoint.phase is RunCheckpointPhase.TERMINAL
            assert run.status is RunStatus.FAILED
            assert run.error is not None
            assert run.error.code == "internal"
            assert run.error.message == "model call failed"
            assert run.error.retryable is True
            assert run_id not in session.active_run_ids
            assert event_types[-5:] == (
                "reconciliation.resolved",
                "model.call.failed",
                "step.failed",
                "run.failed",
                "session.run.detached",
            )
        exact_replay = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "test"},
            evidence={"provider_result": provider_result},
        )
        assert exact_replay == resolved
        with pytest.raises(AgentSDKError) as changed_replay:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "different"},
                evidence={"provider_result": provider_result},
            )
        assert changed_replay.value.code is ErrorCode.CONFLICT
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


async def _replace_resolution_event_log(
    store: Any,
    stored_events: list[Any],
) -> None:
    stored_events.sort(key=lambda stored: stored.cursor)
    if isinstance(store, InMemoryStore):
        store._events = stored_events
        store._last_cursor = max(
            (stored.cursor for stored in stored_events),
            default=0,
        )
        return

    assert isinstance(store, SQLiteStore)
    await store._connection.execute("DELETE FROM events")
    for stored in stored_events:
        event = stored.event
        await store._connection.execute(
            """
            INSERT INTO events(
                cursor, event_id, session_id, run_id, sequence, type,
                schema_version, occurred_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored.cursor,
                event.event_id,
                event.session_id,
                event.run_id,
                event.sequence,
                event.type,
                event.schema_version,
                event.occurred_at.isoformat(),
                json.dumps(
                    event.payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
    await store._connection.execute(
        "UPDATE sqlite_sequence SET seq = ? WHERE name = 'events'",
        (max((stored.cursor for stored in stored_events), default=0),),
    )
    await store._connection.commit()


async def _corrupt_partial_model_deltas(
    store: Any,
    *,
    run_id: str,
    corruption: str,
) -> None:
    stored_events = list(await store.read_events(after_cursor=0))
    deltas = tuple(
        stored
        for stored in stored_events
        if stored.event.run_id == run_id
        and stored.event.type == "model.text.delta"
    )
    if corruption == "moved":
        assert len(deltas) == 1
        text = deltas[0].event.payload["text"]
        assert isinstance(text, str) and len(text) > 1
        split = len(text) // 2
        index = stored_events.index(deltas[0])
        stored_events[index] = deltas[0]._replace(
            event=deltas[0].event.model_copy(
                update={"payload": {"text": text[split:] + text[:split]}}
            )
        )
    else:
        assert corruption == "corrupt"
        assert deltas
        index = stored_events.index(deltas[0])
        stored_events[index] = deltas[0]._replace(
            event=deltas[0].event.model_copy(
                update={"payload": {"text": "corrupt"}}
            )
        )
    await _replace_resolution_event_log(store, stored_events)


async def _corrupt_confirmed_terminal_history(
    store: Any,
    *,
    run_id: str,
    projection: str,
    corruption: str,
) -> None:
    stored_events = list(await store.read_events(after_cursor=0))
    terminal_type = "run.completed" if projection == "completed" else "run.failed"
    terminal = next(
        stored
        for stored in stored_events
        if stored.event.run_id == run_id and stored.event.type == terminal_type
    )
    session_event = next(
        stored
        for stored in stored_events
        if stored.event.run_id is None
        and stored.event.type in {"session.run.detached", "session.closed"}
        and stored.event.payload.get("run_id") == run_id
    )
    if corruption == "step_payload":
        step = next(
            stored
            for stored in stored_events
            if stored.event.run_id == run_id and stored.event.type == "step.completed"
        )
        index = stored_events.index(step)
        stored_events[index] = step._replace(
            event=step.event.model_copy(update={"payload": {"corrupt": True}})
        )
    elif corruption == "terminal_payload":
        index = stored_events.index(terminal)
        stored_events[index] = terminal._replace(
            event=terminal.event.model_copy(update={"payload": {"corrupt": True}})
        )
    elif corruption == "session_missing":
        stored_events.remove(session_event)
    elif corruption == "session_duplicate":
        duplicate_sequence = max(
            stored.event.sequence
            for stored in stored_events
            if stored.event.session_id == session_event.event.session_id
            and stored.event.run_id is None
        ) + 1
        stored_events.append(
            session_event._replace(
                cursor=max(stored.cursor for stored in stored_events) + 1,
                event=session_event.event.model_copy(
                    update={
                        "event_id": "evt_duplicate_terminal_session",
                        "sequence": duplicate_sequence,
                    }
                ),
            )
        )
    elif corruption == "session_moved":
        terminal_index = stored_events.index(terminal)
        session_index = stored_events.index(session_event)
        stored_events[terminal_index] = terminal._replace(
            cursor=session_event.cursor
        )
        stored_events[session_index] = session_event._replace(
            cursor=terminal.cursor
        )
    else:
        assert corruption in {"session_payload", "session_deleting_status"}
        index = stored_events.index(session_event)
        stored_events[index] = session_event._replace(
            event=session_event.event.model_copy(
                update={
                    "payload": {
                        "run_id": run_id,
                        "status": (
                            SessionStatus.DELETING.value
                            if corruption == "session_deleting_status"
                            else "corrupt"
                        ),
                    }
                }
            )
        )
    await _replace_resolution_event_log(store, stored_events)


async def _corrupt_confirmed_terminal_lifecycle(
    store: Any,
    *,
    run_id: str,
    marker: str,
    corruption: str,
) -> None:
    stored_events = list(await store.read_events(after_cursor=0))
    requested = next(
        stored
        for stored in stored_events
        if stored.event.run_id == run_id
        and stored.event.type == "reconciliation.requested"
    )
    target = next(
        stored
        for stored in stored_events
        if stored.cursor < requested.cursor
        and stored.event.run_id == run_id
        and stored.event.type == marker
    )
    partner = next(
        stored
        for stored in stored_events
        if stored.cursor < requested.cursor
        and stored.event.run_id == run_id
        and stored.event.type == "step.completed"
    )
    target_index = stored_events.index(target)
    partner_index = stored_events.index(partner)
    if corruption == "missing":
        stored_events[target_index] = target._replace(
            event=target.event.model_copy(
                update={
                    "type": "model.text.delta",
                    "payload": {"text": "orphan"},
                }
            )
        )
    elif corruption == "duplicate":
        stored_events[partner_index] = partner._replace(
            event=partner.event.model_copy(
                update={
                    "type": target.event.type,
                    "payload": target.event.payload,
                }
            )
        )
    else:
        assert corruption == "moved"
        stored_events[target_index] = target._replace(
            event=target.event.model_copy(
                update={
                    "type": partner.event.type,
                    "payload": partner.event.payload,
                }
            )
        )
        stored_events[partner_index] = partner._replace(
            event=partner.event.model_copy(
                update={
                    "type": target.event.type,
                    "payload": target.event.payload,
                }
            )
        )
    await _replace_resolution_event_log(store, stored_events)


async def _resolve_confirmed_later_model(
    store: Any,
) -> tuple[AgentSDK, str, Any, Any, dict[str, object]]:
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_later_model_reconciliation(store)
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("confirmation and replay must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)

    async def unused_tool(_: ToolContext, value: int) -> int:
        return value

    sdk.tools.register(tool_spec, unused_tool)
    resolution_evidence = {
        "provider_result": _confirmed_provider_result(text="confirmed later")
    }
    resolved = await sdk.recovery.resolve(
        request.request_id,
        ReconciliationAction.CONFIRM_COMPLETED,
        actor={"operator": "terminal-lifecycle"},
        evidence=resolution_evidence,
    )
    return sdk, run_id, request, resolved, resolution_evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    ("projection", "corruption"),
    (
        ("completed", "step_payload"),
        ("completed", "terminal_payload"),
        ("completed", "session_missing"),
        ("failed", "session_duplicate"),
        ("completed", "session_moved"),
        ("failed", "session_payload"),
        ("failed", "session_deleting_status"),
        ("failed", "terminal_payload"),
    ),
)
async def test_confirm_completed_terminal_replay_authenticates_entire_batch(
    backend: str,
    projection: str,
    corruption: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"terminal-replay-{projection}-{corruption}.db"
        )
    )
    run_id, spec, _operation_id, request = await _seed_pending_model_reconciliation(
        store
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("exact replay must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    provider_result = (
        _confirmed_provider_result()
        if projection == "completed"
        else {
            "disposition": "failed",
            "error_code": "internal",
            "retryable": True,
        }
    )
    try:
        await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "terminal-replay"},
            evidence={"provider_result": provider_result},
        )
        await _corrupt_confirmed_terminal_history(
            store,
            run_id=run_id,
            projection=projection,
            corruption=corruption,
        )
        before = await _resolution_domain_state(store)
        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "terminal-replay"},
                evidence={"provider_result": provider_result},
            )
        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery state conflict"
        assert await _resolution_domain_state(store) == before
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_terminal_replay_accepts_complete_multiturn_tool_history(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "confirmed-multiturn-positive.db")
    )
    sdk, run_id, request, resolved, resolution_evidence = (
        await _resolve_confirmed_later_model(store)
    )
    try:
        run = await sdk.runs.get(run_id)
        checkpoint = await store.get_run_checkpoint(run_id)
        events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        assert run.status is RunStatus.COMPLETED
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.TERMINAL
        assert len(checkpoint.tool_results) == 1
        assert sum(event.type == "step.started" for event in events) == 2
        assert sum(event.type == "model.call.started" for event in events) == 2
        assert sum(event.type == "model.call.completed" for event in events) == 2
        assert sum(event.type == "tool.call.completed" for event in events) == 1

        replay = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "terminal-lifecycle"},
            evidence=resolution_evidence,
        )
        assert replay == resolved
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "marker",
    (
        "step.started",
        "model.call.started",
        "model.usage.reported",
        "model.call.completed",
    ),
)
@pytest.mark.parametrize("corruption", ("missing", "duplicate", "moved"))
async def test_confirmed_terminal_replay_authenticates_complete_lifecycle_history(
    backend: str,
    marker: str,
    corruption: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"confirmed-lifecycle-{marker}-{corruption}.db"
        )
    )
    sdk, run_id, request, _resolved, resolution_evidence = (
        await _resolve_confirmed_later_model(store)
    )
    try:
        await _corrupt_confirmed_terminal_lifecycle(
            store,
            run_id=run_id,
            marker=marker,
            corruption=corruption,
        )
        before = await _resolution_domain_state(store)

        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "terminal-lifecycle"},
                evidence=resolution_evidence,
            )
        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery state conflict"
        assert await _resolution_domain_state(store) == before
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_terminal_partial_text_resolves_and_replays(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "confirmed-partial-text.db")
    )
    run_id, spec, _operation_id, request = (
        await _seed_pending_partial_model_reconciliation(store)
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("confirmed resolution must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    evidence = {
        "provider_result": _confirmed_provider_result(text="partial-confirmed")
    }
    try:
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "partial-text"},
            evidence=evidence,
        )
        assert (await sdk.runs.get(run_id)).status is RunStatus.COMPLETED

        replay = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "partial-text"},
            evidence=evidence,
        )
        assert replay == resolved
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


async def _resolve_prior_model_decision_then_terminal(
    store: Any,
    *,
    prior_action: ReconciliationAction,
    terminal_projection: str,
) -> tuple[
    AgentSDK,
    str,
    Any,
    Any,
    dict[str, object],
    Any,
    dict[str, object],
]:
    run_id, spec, _operation_id, prior_request = (
        await _seed_pending_model_reconciliation(store)
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_provider(**_: object) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runner = AgentSDK.for_test(
        store=store,
        acompletion=blocked_provider,
        permission_default="allow",
    )
    runner.agents.define(spec)
    prior_evidence: dict[str, object] = (
        {"disposition": "not_executed"}
        if prior_action is ReconciliationAction.CONFIRM_NOT_EXECUTED
        else {"acknowledge_duplicate_side_effect_risk": True}
    )
    try:
        prior_resolved = await runner.recovery.resolve(
            prior_request.request_id,
            prior_action,
            actor={"operator": "prior-decision"},
            evidence=prior_evidence,
        )
        handle = await runner.recovery.recover_run(run_id)
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert handle._task is not None
        handle._task.cancel()
        with pytest.raises(AgentSDKError):
            await handle.result()
        await asyncio.wait_for(cancelled.wait(), timeout=10)
    finally:
        await runner.close()
    await _mark_interrupted(store)

    resolver = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("terminal decision must not call provider")
        ),
        permission_default="allow",
    )
    resolver.agents.define(spec)
    waiting = await resolver.recovery.recover_run(run_id)
    with pytest.raises(AgentSDKError, match="recovery required"):
        await waiting.result()
    terminal_request = (await resolver.recovery.pending_requests(run_id))[0]
    terminal_evidence: dict[str, object] = {
        "provider_result": (
            _confirmed_provider_result(text="terminal-confirmed")
            if terminal_projection == "completed"
            else {
                "disposition": "failed",
                "error_code": "internal",
                "retryable": True,
            }
        )
    }
    terminal_resolved = await resolver.recovery.resolve(
        terminal_request.request_id,
        ReconciliationAction.CONFIRM_COMPLETED,
        actor={"operator": "terminal-decision"},
        evidence=terminal_evidence,
    )
    return (
        resolver,
        run_id,
        prior_request,
        prior_resolved,
        prior_evidence,
        terminal_resolved,
        terminal_evidence,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "prior_action",
    (
        ReconciliationAction.CONFIRM_NOT_EXECUTED,
        ReconciliationAction.RETRY,
    ),
)
@pytest.mark.parametrize("terminal_projection", ("completed", "failed"))
async def test_cumulative_model_decisions_replay_after_terminal_confirmation(
    backend: str,
    prior_action: ReconciliationAction,
    terminal_projection: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path
            / f"cumulative-{prior_action.value}-{terminal_projection}.sqlite3"
        )
    )
    resolver: AgentSDK | None = None
    try:
        (
            resolver,
            run_id,
            prior_request,
            prior_resolved,
            prior_evidence,
            terminal_resolved,
            terminal_evidence,
        ) = await _resolve_prior_model_decision_then_terminal(
            store,
            prior_action=prior_action,
            terminal_projection=terminal_projection,
        )
        expected_status = (
            RunStatus.COMPLETED
            if terminal_projection == "completed"
            else RunStatus.FAILED
        )
        assert (await resolver.runs.get(run_id)).status is expected_status
        before = await _resolution_domain_state(store)

        assert (
            await resolver.recovery.resolve(
                prior_request.request_id,
                prior_action,
                actor={"operator": "prior-decision"},
                evidence=prior_evidence,
            )
            == prior_resolved
        )
        assert (
            await resolver.recovery.resolve(
                terminal_resolved.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "terminal-decision"},
                evidence=terminal_evidence,
            )
            == terminal_resolved
        )
        assert await _resolution_domain_state(store) == before
    finally:
        if resolver is not None:
            await resolver.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("terminal_projection", ("completed", "failed"))
async def test_confirmed_tool_replays_after_later_terminal_confirmation(
    backend: str,
    terminal_projection: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"tool-then-terminal-{terminal_projection}.sqlite3"
        )
    )
    run_id, spec, tool_spec, _operation_id, tool_request = (
        await _seed_pending_tool_reconciliation(store)
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_provider(**_: object) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("confirmed Tool must not repeat")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=blocked_provider,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, forbidden_tool)
    tool_evidence: dict[str, object] = {
        "tool_result": {
            "call_id": "call_resolution",
            "tool_name": tool_spec.name,
            "status": "succeeded",
            "content": "7",
            "value": 7,
            "error": None,
        }
    }
    terminal_resolver: AgentSDK | None = None
    try:
        tool_resolved = await sdk.recovery.resolve(
            tool_request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "tool-decision"},
            evidence=tool_evidence,
        )
        handle = await sdk.recovery.recover_run(run_id)
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert handle._task is not None
        handle._task.cancel()
        with pytest.raises(AgentSDKError):
            await handle.result()
        await asyncio.wait_for(cancelled.wait(), timeout=10)
        await sdk.close()
        await _mark_interrupted(store)

        terminal_resolver = AgentSDK.for_test(
            store=store,
            acompletion=lambda **_: (_ for _ in ()).throw(
                AssertionError("terminal confirmation must not call provider")
            ),
            permission_default="allow",
        )
        terminal_resolver.agents.define(spec)
        terminal_resolver.tools.register(tool_spec, forbidden_tool)
        waiting = await terminal_resolver.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        terminal_request = (
            await terminal_resolver.recovery.pending_requests(run_id)
        )[0]
        terminal_evidence: dict[str, object] = {
            "provider_result": (
                _confirmed_provider_result(text="terminal-after-tool")
                if terminal_projection == "completed"
                else {
                    "disposition": "failed",
                    "error_code": "internal",
                    "retryable": True,
                }
            )
        }
        terminal_resolved = await terminal_resolver.recovery.resolve(
            terminal_request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "terminal-decision"},
            evidence=terminal_evidence,
        )
        before = await _resolution_domain_state(store)

        assert (
            await terminal_resolver.recovery.resolve(
                tool_request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "tool-decision"},
                evidence=tool_evidence,
            )
            == tool_resolved
        )
        assert (
            await terminal_resolver.recovery.resolve(
                terminal_request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "terminal-decision"},
                evidence=terminal_evidence,
            )
            == terminal_resolved
        )
        assert await _resolution_domain_state(store) == before
    finally:
        await sdk.close()
        if terminal_resolver is not None:
            await terminal_resolver.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_partial_tool_call_recovers_and_replays(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "confirmed-partial-tool.db")
    )
    tool_spec = _unsafe_tool_spec()
    run_id, spec, _operation_id, request = (
        await _seed_pending_partial_model_reconciliation(
            store,
            tool_spec=tool_spec,
        )
    )
    evidence = {
        "provider_result": _confirmed_provider_result(
            text="partial-confirmed",
            tool_call={
                "index": 0,
                "call_id": "call_partial_confirmed",
                "name": tool_spec.name,
                "arguments_json": '{"value":7}',
            },
        )
    }
    resolver = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("confirmed resolution must not call provider")
        ),
        permission_default="allow",
    )
    resolver.agents.define(spec)

    async def resolver_tool(_: ToolContext, value: int) -> int:
        return value

    resolver.tools.register(tool_spec, resolver_tool)
    resolved = await resolver.recovery.resolve(
        request.request_id,
        ReconciliationAction.CONFIRM_COMPLETED,
        actor={"operator": "partial-tool"},
        evidence=evidence,
    )
    await resolver.close()

    provider_calls: list[int] = []
    tool_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        tool_calls.append(value)
        return value + 1

    recovery = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(provider_calls),
        permission_default="allow",
    )
    recovery.agents.define(spec)
    recovery.tools.register(tool_spec, handler)
    try:
        result = await (await recovery.recovery.recover_run(run_id)).result()
        assert result.output_text == "partial-confirmeddone"
        assert tool_calls == [7]
        assert provider_calls == [1]
        recovered_events = await store.read_events(after_cursor=0)
        uncorrelated_step_terminals = tuple(
            stored.event
            for stored in recovered_events
            if stored.event.run_id == run_id
            and stored.event.type == "step.completed"
            and not stored.event.payload
        )
        assert uncorrelated_step_terminals
        assert all(
            event.schema_version == 1 for event in uncorrelated_step_terminals
        )
        uncorrelated_tool_starts = tuple(
            stored.event
            for stored in recovered_events
            if stored.event.run_id == run_id
            and stored.event.type == "tool.call.started"
            and stored.event.payload.get("step_id") is None
        )
        assert uncorrelated_tool_starts
        assert all(event.schema_version == 1 for event in uncorrelated_tool_starts)
        assert all(
            set(event.payload) == {"call_id", "tool_name"}
            for event in uncorrelated_tool_starts
        )

        replay = await recovery.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "partial-tool"},
            evidence=evidence,
        )
        assert replay == resolved
    finally:
        await recovery.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    ("corruption", "deltas", "confirmed_text"),
    (
        ("non_prefix", ("partial",), "different"),
        ("overlong", ("partial-too-long",), "partial"),
        ("moved", ("par", "tial"), "partial-confirmed"),
        ("corrupt", ("partial",), "partial-confirmed"),
    ),
)
async def test_confirmed_partial_text_rejects_non_prefix_history_before_commit(
    backend: str,
    corruption: str,
    deltas: tuple[str, ...],
    confirmed_text: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / f"partial-prefix-{corruption}.db")
    )
    run_id, spec, _operation_id, request = (
        await _seed_pending_partial_model_reconciliation(store, deltas=deltas)
    )
    if corruption in {"moved", "corrupt"}:
        await _corrupt_partial_model_deltas(
            store,
            run_id=run_id,
            corruption=corruption,
        )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("invalid confirmation must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    before = await _resolution_domain_state(store)
    try:
        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "partial-prefix"},
                evidence={
                    "provider_result": _confirmed_provider_result(
                        text=confirmed_text
                    )
                },
            )
        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery state conflict"
        assert await _resolution_domain_state(store) == before
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("projection", ("completed", "failed"))
async def test_confirmed_terminal_replay_accepts_later_session_run_evolution(
    backend: str,
    projection: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"terminal-session-successor-{projection}.db"
        )
    )
    run_id, spec, _operation_id, request = await _seed_pending_model_reconciliation(
        store
    )
    provider_calls: list[int] = []

    async def later_completion(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_calls.append(1)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {"delta": {"content": "later"}, "finish_reason": "stop"}
                ]
            }

        return chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=later_completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    provider_result = (
        _confirmed_provider_result()
        if projection == "completed"
        else {
            "disposition": "failed",
            "error_code": "internal",
            "retryable": True,
        }
    )
    evidence = {"provider_result": provider_result}
    try:
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "session-successor"},
            evidence=evidence,
        )
        original = await sdk.runs.get(run_id)
        later = await sdk.runs.start(original.session_id, spec, "later run")
        later_result = await later.result()
        assert later_result.output_text == "later"
        assert provider_calls == [1]

        replay = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "session-successor"},
            evidence=evidence,
        )
        assert replay == resolved
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


async def _inject_confirmed_replay_orphan(
    store: Any,
    *,
    resolved: Any,
    operation: ModelCallOperation,
    orphan: str,
) -> None:
    if orphan in {"pending_request", "resolved_request"}:
        if orphan == "pending_request":
            record = resolved.model_copy(
                update={
                    "request_id": "rec_orphan_confirmed_pending",
                    "status": ReconciliationStatus.PENDING,
                    "resolution": None,
                }
            )
        else:
            assert resolved.resolution is not None
            record = resolved.model_copy(
                update={
                    "request_id": "rec_orphan_confirmed_resolved",
                    "resolution": resolved.resolution.model_copy(
                        update={"event_id": "evt_orphan_confirmed_resolved"}
                    ),
                }
            )
        serialized = _canonical_record_json(record)
        if isinstance(store, InMemoryStore):
            store._reconciliation_requests[record.request_id] = serialized
        else:
            assert isinstance(store, SQLiteStore)
            await store._connection.execute(
                """
                INSERT INTO reconciliation_requests(
                    request_id, session_id, run_id, operation_id, status, data_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.request_id,
                    record.session_id,
                    record.run_id,
                    record.operation_id,
                    record.status.value,
                    serialized,
                ),
            )
            await store._connection.commit()
        return

    assert orphan == "completed_model_operation"
    orphan_operation = operation.model_copy(
        update={"operation_id": "op_orphan_confirmed_completed"}
    )
    serialized = _canonical_record_json(orphan_operation)
    if isinstance(store, InMemoryStore):
        store._external_operations[orphan_operation.operation_id] = serialized
        return
    assert isinstance(store, SQLiteStore)
    await store._connection.execute(
        """
        INSERT INTO external_operations(
            operation_id, operation_kind, session_id, run_id, turn,
            request_fingerprint, provider_identity, tool_identity,
            lease_generation, status, data_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            orphan_operation.operation_id,
            orphan_operation.operation_kind.value,
            orphan_operation.session_id,
            orphan_operation.run_id,
            orphan_operation.turn,
            orphan_operation.request_fingerprint,
            orphan_operation.provider_identity,
            orphan_operation.tool_identity,
            orphan_operation.lease_generation,
            orphan_operation.status.value,
            serialized,
        ),
    )
    await store._connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "orphan",
    ("pending_request", "resolved_request", "completed_model_operation"),
)
async def test_confirmed_terminal_replay_rejects_orphan_closed_world_records(
    backend: str,
    orphan: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / f"confirmed-orphan-{orphan}.db")
    )
    run_id, spec, operation_id, request = await _seed_pending_model_reconciliation(
        store
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("exact replay must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    evidence = {"provider_result": _confirmed_provider_result()}
    try:
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "closed-world"},
            evidence=evidence,
        )
        operation = next(
            item
            for item in await store.list_external_operations(run_id)
            if item.operation_id == operation_id
        )
        assert isinstance(operation, ModelCallOperation)
        await _inject_confirmed_replay_orphan(
            store,
            resolved=resolved,
            operation=operation,
            orphan=orphan,
        )
        before = await _resolution_domain_state(store)

        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "closed-world"},
                evidence=evidence,
            )
        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery state conflict"
        assert await _resolution_domain_state(store) == before
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirm_completed_terminalization_gap_preserves_model_outcome(
    backend: str,
    tmp_path: Path,
) -> None:
    if backend == "memory":
        store: Any = _RejectCompletedTerminalResolutionMemoryStore()
    else:
        store = await _RejectCompletedTerminalResolutionSQLiteStore.open(
            tmp_path / "confirm-terminal-gap.db"
        )
    provider_calls: list[int] = []

    async def completion(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_calls.append(1)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {"delta": {"content": "durable"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            }

        return chunks()

    spec = AgentSpec(name=f"terminal-gap-{backend}", model="fake/terminal-gap")
    first = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    session = await first.sessions.create(workspaces=[])
    handle = await first.runs.start(session.session_id, spec, "terminal gap")
    with pytest.raises(AgentSDKError) as terminal_failure:
        await handle.result()
    assert terminal_failure.value.code is ErrorCode.INTERNAL
    operations_before = await store.list_external_operations(handle.run_id)
    assert len(operations_before) == 1
    operation_before = operations_before[0]
    assert isinstance(operation_before, ModelCallOperation)
    assert operation_before.outcome is not None
    await first.close()
    store.reject_terminal = False

    await _mark_interrupted(store)
    later_provider_calls: list[int] = []

    async def later_completion(**_: object) -> AsyncIterator[dict[str, object]]:
        later_provider_calls.append(1)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {"delta": {"content": "later"}, "finish_reason": "stop"}
                ]
            }

        return chunks()

    second = AgentSDK.for_test(
        store=store,
        acompletion=later_completion,
        permission_default="allow",
    )
    second.agents.define(spec)
    try:
        waiting = await second.recovery.recover_run(handle.run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await second.recovery.pending_requests(handle.run_id))[0]
        event_types_before = tuple(
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == handle.run_id
        )
        outcome = operation_before.model_dump(mode="json")["outcome"]
        assert isinstance(outcome, dict)
        resolved = await second.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "gap"},
            evidence={
                "provider_result": {
                    "disposition": "completed",
                    "finish_reason": outcome["finish_reason"],
                    "text": outcome["text"],
                    "tool_call": None,
                    "usage": outcome["usage"],
                }
            },
        )

        assert resolved.status.value == "resolved"
        run = await second.runs.get(handle.run_id)
        assert run.status is RunStatus.COMPLETED
        assert run.output_text == "durable"
        operations_after = await store.list_external_operations(handle.run_id)
        assert operations_after == operations_before
        checkpoint = await store.get_run_checkpoint(handle.run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.TERMINAL
        event_types_after = tuple(
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == handle.run_id
        )
        assert event_types_after.count("model.usage.reported") == 1
        assert event_types_after.count("model.call.completed") == 1
        assert event_types_after.count("step.completed") == 1
        assert event_types_after[: len(event_types_before)] == event_types_before
        assert event_types_after[-2:] == (
            "reconciliation.resolved",
            "run.completed",
        )
        assert provider_calls == [1]
        resolution_evidence = {
            "provider_result": {
                "disposition": "completed",
                "finish_reason": outcome["finish_reason"],
                "text": outcome["text"],
                "tool_call": None,
                "usage": outcome["usage"],
            }
        }
        later = await second.runs.start(run.session_id, spec, "later gap run")
        later_result = await later.result()
        assert later_result.output_text == "later"
        assert later_provider_calls == [1]
        exact_replay = await second.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "gap"},
            evidence=resolution_evidence,
        )
        assert exact_replay == resolved
    finally:
        await second.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("usage", ("reported", "empty"))
async def test_confirmed_model_tool_call_resumes_only_on_explicit_recovery(
    backend: str,
    usage: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "confirm-tool-resume.db")
    )
    tool_spec = _unsafe_tool_spec()
    run_id, spec, _operation_id, request = await _seed_pending_model_reconciliation(
        store,
        tool_spec,
    )
    resolver = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("resolve must not call provider")
        ),
        permission_default="allow",
    )
    resolver.agents.define(spec)

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("resolve must not call tool")

    resolver.tools.register(tool_spec, forbidden_tool)
    resolution_evidence = {
        "provider_result": _confirmed_provider_result(
            text="confirmed",
            tool_call={
                "index": 0,
                "call_id": "call_resume",
                "name": tool_spec.name,
                "arguments_json": '{"value":7}',
            },
            usage={} if usage == "empty" else None,
        )
    }
    resolved = await resolver.recovery.resolve(
        request.request_id,
        ReconciliationAction.CONFIRM_COMPLETED,
        actor={"operator": "resume"},
        evidence=resolution_evidence,
    )
    assert (await resolver.runs.get(run_id)).status is RunStatus.INTERRUPTED
    await resolver.close()

    provider_calls: list[int] = []
    tool_calls: list[int] = []

    async def final_completion(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_calls.append(1)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "after"}, "finish_reason": "stop"}]}

        return chunks()

    async def handler(_: ToolContext, value: int) -> int:
        tool_calls.append(value)
        return value + 1

    recovery = AgentSDK.for_test(
        store=store,
        acompletion=final_completion,
        permission_default="allow",
    )
    recovery.agents.define(spec)
    recovery.tools.register(tool_spec, handler)
    try:
        recovered = await recovery.recovery.recover_run(run_id)
        result = await recovered.result()
        assert result.output_text == "confirmedafter"
        assert tool_calls == [7]
        assert provider_calls == [1]
        assert (await recovery.runs.get(run_id)).status is RunStatus.COMPLETED
        replay = await recovery.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "resume"},
            evidence=resolution_evidence,
        )
        assert replay == resolved
    finally:
        await recovery.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "orphan",
    (None, "pending_request", "resolved_request", "completed_model_operation"),
    ids=("canonical", "pending-request", "resolved-request", "model-operation"),
)
async def test_confirmed_tool_call_later_pending_history_is_closed_world(
    backend: str,
    orphan: str | None,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "confirmed-then-crash.db")
    )
    tool_spec = _unsafe_tool_spec()
    run_id, spec, operation_id, request = await _seed_pending_model_reconciliation(
        store,
        tool_spec,
    )
    resolution_evidence = {
        "provider_result": _confirmed_provider_result(
            text="confirmed",
            tool_call={
                "index": 0,
                "call_id": "call_then_crash",
                "name": tool_spec.name,
                "arguments_json": '{"value":7}',
            },
        )
    }
    resolver = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("resolution must not call provider")
        ),
        permission_default="allow",
    )
    resolver.agents.define(spec)

    async def resolver_tool(_: ToolContext, value: int) -> int:
        return value

    resolver.tools.register(tool_spec, resolver_tool)
    resolved = await resolver.recovery.resolve(
        request.request_id,
        ReconciliationAction.CONFIRM_COMPLETED,
        actor={"operator": "then-crash"},
        evidence=resolution_evidence,
    )
    await resolver.close()

    provider_entered = asyncio.Event()
    provider_cancelled = asyncio.Event()
    tool_calls: list[int] = []

    async def blocked_completion(**_: object) -> Any:
        provider_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_cancelled.set()
            raise

    async def handler(_: ToolContext, value: int) -> int:
        tool_calls.append(value)
        return value + 1

    runner = AgentSDK.for_test(
        store=store,
        acompletion=blocked_completion,
        permission_default="allow",
    )
    runner.agents.define(spec)
    runner.tools.register(tool_spec, handler)
    handle = await runner.recovery.recover_run(run_id)
    await asyncio.wait_for(provider_entered.wait(), timeout=10)
    assert tool_calls == [7]
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(AgentSDKError):
        await handle.result()
    await asyncio.wait_for(provider_cancelled.wait(), timeout=10)
    await runner.close()
    await _mark_interrupted(store)

    recovery = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("unknown outcome must not call provider")
        ),
        permission_default="allow",
    )
    recovery.agents.define(spec)
    recovery.tools.register(tool_spec, handler)
    try:
        waiting = await recovery.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        pending = await recovery.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "model_call_unknown_outcome"
        if orphan is None:
            replay = await recovery.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "then-crash"},
                evidence=resolution_evidence,
            )
            assert replay == resolved
        else:
            operation = next(
                item
                for item in await store.list_external_operations(run_id)
                if item.operation_id == operation_id
            )
            assert isinstance(operation, ModelCallOperation)
            await _inject_confirmed_replay_orphan(
                store,
                resolved=resolved,
                operation=operation,
                orphan=orphan,
            )
            before = await _resolution_domain_state(store)
            with pytest.raises(AgentSDKError) as caught:
                await recovery.recovery.resolve(
                    request.request_id,
                    ReconciliationAction.CONFIRM_COMPLETED,
                    actor={"operator": "then-crash"},
                    evidence=resolution_evidence,
                )
            assert caught.value.code is ErrorCode.CONFLICT
            assert caught.value.message == "recovery state conflict"
            assert await _resolution_domain_state(store) == before
    finally:
        await recovery.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "invalid_result",
    (
        {"disposition": "pending"},
        {
            "disposition": "completed",
            "text": "ok",
            "usage": {
                "prompt_tokens": "1",
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        },
        {
            "disposition": "completed",
            "text": "ok",
            "usage": {},
            "extra": True,
        },
        {
            "disposition": "completed",
            "text": "x" * (64 * 1024 + 1),
            "usage": {},
        },
        {
            "disposition": "completed",
            "finish_reason": "x" * 129,
            "text": "ok",
            "usage": {},
        },
        {
            "disposition": "completed",
            "text": "ok",
            "tool_call": {
                "index": 0,
                "call_id": "call_invalid",
                "name": "resolution_tool",
                "arguments_json": '{"value":NaN}',
            },
            "usage": {},
        },
        {
            "disposition": "completed",
            "text": "ok",
            "tool_call": {
                "index": 0,
                "call_id": "call_invalid",
                "name": "resolution_tool",
                "arguments_json": "[]",
            },
            "usage": {},
        },
        {
            "disposition": "failed",
            "error_code": "not_public",
            "retryable": False,
        },
        {
            "disposition": "failed",
            "error_code": "internal",
            "retryable": 1,
        },
    ),
    ids=(
        "unsupported-disposition",
        "coerced-usage",
        "extra-field",
        "unbounded-text",
        "unbounded-finish-reason",
        "non-finite-tool-arguments",
        "non-object-tool-arguments",
        "non-public-error-code",
        "coerced-retryable",
    ),
)
async def test_confirm_completed_rejects_invalid_provider_result_without_mutation(
    backend: str,
    invalid_result: dict[str, object],
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "invalid-confirmed-model.db")
    )
    run_id, spec, _operation_id, request = await _seed_pending_model_reconciliation(
        store
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("invalid resolution must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        before = await _resolution_domain_state(store)
        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "invalid"},
                evidence={"provider_result": invalid_result},
            )
        assert caught.value.code is ErrorCode.INVALID_STATE
        assert caught.value.message == "reconciliation decision is invalid"
        assert caught.value.retryable is False
        assert await _resolution_domain_state(store) == before
        assert (await sdk.runs.get(run_id)).status is RunStatus.WAITING_RECONCILIATION
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirm_completed_post_commit_ambiguity_converges_exactly_once(
    backend: str,
    tmp_path: Path,
) -> None:
    if backend == "memory":
        store: Any = _AmbiguousResolutionMemoryStore()
    else:
        opened = await SQLiteStore.open(tmp_path / "ambiguous-confirmed-model.db")
        store = _AmbiguousResolutionSQLiteStore(opened._connection)
    run_id, spec, _operation_id, request = await _seed_pending_model_reconciliation(
        store
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("resolution must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        before = await store.latest_cursor()
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "ambiguous"},
            evidence={"provider_result": _confirmed_provider_result()},
        )
        assert resolved.status.value == "resolved"
        assert store.resolution_batches == 2
        assert await store.latest_cursor() == before + 6
        events = await store.read_events(after_cursor=0)
        assert sum(
            stored.event.type == "reconciliation.resolved" for stored in events
        ) == 1
        assert (await sdk.runs.get(run_id)).status is RunStatus.COMPLETED
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_two_sdk_confirm_completed_converges_on_same_terminal_projection(
    backend: str,
    tmp_path: Path,
) -> None:
    if backend == "memory":
        owner_store: Any = _ResolutionBarrierMemoryStore()
        follower_store: Any = owner_store
    else:
        opened = await SQLiteStore.open(tmp_path / "two-sdk-confirmed-model.db")
        owner_store = _ResolutionBarrierSQLiteStore(opened._connection)
        follower_store = await SQLiteStore.open(
            tmp_path / "two-sdk-confirmed-model.db"
        )
    run_id, spec, _operation_id, request = await _seed_pending_model_reconciliation(
        owner_store
    )
    owner = AgentSDK.for_test(
        store=owner_store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("resolution must not call provider")
        ),
        permission_default="allow",
    )
    follower = AgentSDK.for_test(
        store=follower_store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("resolution must not call provider")
        ),
        permission_default="allow",
    )
    owner.agents.define(spec)
    follower.agents.define(spec)
    evidence = {"provider_result": _confirmed_provider_result()}
    owner_store.resolution_barrier_enabled = True
    try:
        first = asyncio.create_task(
            owner.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "concurrent"},
                evidence=evidence,
            )
        )
        await asyncio.wait_for(owner_store.resolution_reached.wait(), timeout=10)
        second = asyncio.create_task(
            follower.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor={"operator": "concurrent"},
                evidence=evidence,
            )
        )
        await asyncio.sleep(0)
        owner_store.allow_resolution.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result == second_result
        assert (await owner.runs.get(run_id)).status is RunStatus.COMPLETED
        events = await owner_store.read_events(after_cursor=0)
        assert sum(
            stored.event.type == "reconciliation.resolved" for stored in events
        ) == 1
    finally:
        owner_store.allow_resolution.set()
        await asyncio.gather(owner.close(), follower.close())
        if backend == "sqlite":
            await owner_store.close()
            await follower_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirm_completed_closes_a_closing_session_atomically(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "closing-confirmed-model.db")
    )
    run_id, spec, _operation_id, request = await _seed_pending_model_reconciliation(
        store
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("resolution must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        run = await sdk.runs.get(run_id)
        closing = await sdk.sessions.close(run.session_id)
        assert closing.status is SessionStatus.CLOSING
        evidence = {"provider_result": _confirmed_provider_result()}
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "close"},
            evidence=evidence,
        )
        closed = await sdk.sessions.get(run.session_id)
        assert closed.status is SessionStatus.CLOSED
        assert closed.active_run_ids == ()
        events = await store.read_events(after_cursor=0)
        assert sum(stored.event.type == "session.closed" for stored in events) == 1
        replay = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"operator": "close"},
            evidence=evidence,
        )
        assert replay == resolved
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


async def _insert_duplicate_run_event_before_paired_interrupt(
    store: Any,
    *,
    run_id: str,
    request_id: str,
    event_type: str,
) -> None:
    stored_events = await store.read_events(after_cursor=0)
    run_events = tuple(
        stored for stored in stored_events if stored.event.run_id == run_id
    )
    requested_index = next(
        index
        for index, stored in enumerate(run_events)
        if stored.event.type == "reconciliation.requested"
        and stored.event.payload.get("request_id") == request_id
    )
    assert requested_index > 0
    interrupt = run_events[requested_index - 1]
    assert interrupt.event.type == "run.interrupted"
    source = next(
        stored
        for stored in reversed(run_events[: requested_index - 1])
        if stored.event.type == event_type
    )
    duplicate = source.event.model_copy(
        update={
            "event_id": f"evt_duplicate_{event_type.replace('.', '_')}",
            "sequence": interrupt.event.sequence,
            "occurred_at": interrupt.event.occurred_at,
        }
    )
    if isinstance(store, InMemoryStore):
        rewritten: list[Any] = []
        for stored in store._events:
            if stored.cursor == interrupt.cursor:
                rewritten.append(
                    interrupt._replace(cursor=interrupt.cursor, event=duplicate)
                )
            event = stored.event
            if (
                event.run_id == run_id
                and event.sequence >= interrupt.event.sequence
            ):
                event = event.model_copy(update={"sequence": event.sequence + 1})
            rewritten.append(
                stored._replace(
                    cursor=(
                        stored.cursor + 1
                        if stored.cursor >= interrupt.cursor
                        else stored.cursor
                    ),
                    event=event,
                )
            )
        store._events = rewritten
        store._last_cursor += 1
        return

    assert isinstance(store, SQLiteStore)
    async with store._connection.execute(
        "SELECT cursor, sequence FROM events "
        "WHERE run_id = ? AND sequence >= ? ORDER BY sequence DESC",
        (run_id, interrupt.event.sequence),
    ) as cursor:
        run_rows = await cursor.fetchall()
    for cursor_value, sequence in run_rows:
        await store._connection.execute(
            "UPDATE events SET sequence = ? WHERE cursor = ?",
            (sequence + 1, cursor_value),
        )
    async with store._connection.execute(
        "SELECT cursor FROM events WHERE cursor >= ? ORDER BY cursor DESC",
        (interrupt.cursor,),
    ) as cursor:
        cursor_rows = await cursor.fetchall()
    for (cursor_value,) in cursor_rows:
        await store._connection.execute(
            "UPDATE events SET cursor = ? WHERE cursor = ?",
            (cursor_value + 1, cursor_value),
        )
    await store._connection.execute(
        """
        INSERT INTO events(
            cursor, event_id, session_id, run_id, sequence, type,
            schema_version, occurred_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            interrupt.cursor,
            duplicate.event_id,
            duplicate.session_id,
            duplicate.run_id,
            duplicate.sequence,
            duplicate.type,
            duplicate.schema_version,
            duplicate.occurred_at.isoformat(),
            json.dumps(
                duplicate.payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    await store._connection.execute(
        "UPDATE sqlite_sequence SET seq = seq + 1 WHERE name = 'events'"
    )
    await store._connection.commit()


async def _move_run_event_before_paired_interrupt(
    store: Any,
    *,
    run_id: str,
    request_id: str,
    event_type: str,
) -> None:
    stored_events = list(await store.read_events(after_cursor=0))
    run_events = tuple(
        stored for stored in stored_events if stored.event.run_id == run_id
    )
    requested_index = next(
        index
        for index, stored in enumerate(run_events)
        if stored.event.type == "reconciliation.requested"
        and stored.event.payload.get("request_id") == request_id
    )
    assert requested_index > 0
    interrupt = run_events[requested_index - 1]
    assert interrupt.event.type == "run.interrupted"
    source = next(
        stored
        for stored in reversed(run_events[: requested_index - 1])
        if stored.event.type == event_type
    )
    original_identity = (
        source.event.event_id,
        source.event.payload,
        source.event.schema_version,
        source.event.occurred_at,
    )
    original_cursors = tuple(stored.cursor for stored in stored_events)
    original_sequences = tuple(
        stored.event.sequence
        for stored in stored_events
        if stored.event.run_id == run_id
    )
    source_index = stored_events.index(source)
    interrupt_index = stored_events.index(interrupt)
    assert source_index < interrupt_index
    moved = stored_events.pop(source_index)
    interrupt_index = stored_events.index(interrupt)
    stored_events.insert(interrupt_index, moved)

    run_sequence = iter(original_sequences)
    rewritten: list[Any] = []
    for cursor_value, stored in zip(original_cursors, stored_events, strict=True):
        event = stored.event
        if event.run_id == run_id:
            event = event.model_copy(update={"sequence": next(run_sequence)})
        rewritten.append(stored._replace(cursor=cursor_value, event=event))

    moved_event = next(
        stored.event
        for stored in rewritten
        if stored.event.event_id == source.event.event_id
    )
    assert (
        moved_event.event_id,
        moved_event.payload,
        moved_event.schema_version,
        moved_event.occurred_at,
    ) == original_identity
    rewritten_run_events = tuple(
        stored for stored in rewritten if stored.event.run_id == run_id
    )
    rewritten_interrupt_index = next(
        index
        for index, stored in enumerate(rewritten_run_events)
        if stored.event.event_id == interrupt.event.event_id
    )
    assert rewritten_run_events[rewritten_interrupt_index - 1].event.event_id == (
        source.event.event_id
    )
    assert tuple(stored.cursor for stored in rewritten) == original_cursors
    assert tuple(stored.event.sequence for stored in rewritten_run_events) == (
        original_sequences
    )

    if isinstance(store, InMemoryStore):
        store._events = rewritten
        return

    assert isinstance(store, SQLiteStore)
    await store._connection.execute("DELETE FROM events")
    for stored in rewritten:
        event = stored.event
        await store._connection.execute(
            """
            INSERT INTO events(
                cursor, event_id, session_id, run_id, sequence, type,
                schema_version, occurred_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored.cursor,
                event.event_id,
                event.session_id,
                event.run_id,
                event.sequence,
                event.type,
                event.schema_version,
                event.occurred_at.isoformat(),
                json.dumps(
                    event.payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
    await store._connection.execute(
        "UPDATE sqlite_sequence SET seq = ? WHERE name = 'events'",
        (max(original_cursors, default=0),),
    )
    await store._connection.commit()


async def _open_resolution_barrier_store(
    backend: str,
    path: Path,
) -> Any:
    if backend == "memory":
        return _ResolutionBarrierMemoryStore()
    opened = await SQLiteStore.open(path)
    return _ResolutionBarrierSQLiteStore(opened._connection)


async def _race_resolution_cas(
    store: Any,
    target: str,
    run_id: str,
    request: Any,
) -> None:
    if target == "run":
        run = await store.get_snapshot("run", run_id)
        assert run is not None
        raced = {**run, "version": run["version"] + 1}
        await store.commit(
            CommitBatch(
                events=(),
                snapshots=(
                    SnapshotWrite(
                        "run",
                        run_id,
                        run["session_id"],
                        raced["version"],
                        raced,
                    ),
                ),
            )
        )
        return
    if target == "checkpoint":
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        await store.race_checkpoint_cas(checkpoint)
        return
    assert target == "reconciliation"
    await store.race_reconciliation_cas(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirm_not_executed_rewinds_real_model_for_one_explicit_retry(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "model-resolution.sqlite3")
    )
    run_id, spec, operation_id = await _seed_real_model_in_flight(store)
    model_calls: list[int] = []
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]

        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator", "id": "user-1"},
            evidence={"disposition": "not_executed"},
        )

        assert resolved.status.value == "resolved"
        assert model_calls == []
        assert (await sdk.runs.get(run_id)).status is RunStatus.INTERRUPTED
        old_operation = await store.get_external_operation(operation_id)
        assert old_operation is not None
        assert old_operation.status is ExternalOperationStatus.FAILED

        result = await (await sdk.recovery.recover_run(run_id)).result()

        assert result.output_text == "done"
        assert model_calls == [1]
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_retry_rewinds_real_tool_only_after_explicit_risk_acknowledgement(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "tool-resolution.sqlite3")
    )
    run_id, spec, tool_spec, operation_id = await _seed_real_tool_in_flight(store)
    handler_calls: list[int] = []
    model_calls: list[int] = []

    async def handler(_: ToolContext, value: int) -> int:
        handler_calls.append(value)
        return value + 1

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, handler)
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]

        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.RETRY,
            actor={"type": "operator", "id": "user-1"},
            evidence={"acknowledge_duplicate_side_effect_risk": True},
        )

        assert resolved.status.value == "resolved"
        assert handler_calls == []
        assert model_calls == []
        old_operation = await store.get_external_operation(operation_id)
        assert old_operation is not None
        assert old_operation.status is ExternalOperationStatus.FAILED

        result = await (await sdk.recovery.recover_run(run_id)).result()

        assert result.output_text == "done"
        assert handler_calls == [7]
        assert model_calls == [1]
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_terminate_unknown_tool_fails_run_without_replay_and_reopens_exactly(
    backend: str,
    tmp_path: Path,
) -> None:
    database = tmp_path / "terminate-tool-resolution.sqlite3"
    store: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(database)
    )
    run_id, spec, tool_spec, operation_id = await _seed_real_tool_in_flight(store)
    provider_calls: list[int] = []
    tool_calls: list[int] = []

    async def forbidden_provider(**_: object) -> Any:
        provider_calls.append(1)
        raise AssertionError("terminate must not call the provider")

    async def forbidden_tool(_: ToolContext, value: int) -> int:
        tool_calls.append(value)
        raise AssertionError("terminate must not call the Tool")

    actor_secret = "abort-actor-secret"
    actor = {
        "type": "operator",
        "id": "release-controller",
        "note": f"Authorization: Bearer {actor_secret}",
    }
    raw_secret = "abort-bearer-secret"
    evidence = {"reason": f"operator abort; Authorization: Bearer {raw_secret}"}
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_provider,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, forbidden_tool)
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]

        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.TERMINATE,
            actor=actor,
            evidence=evidence,
        )

        assert resolved.status is ReconciliationStatus.RESOLVED
        assert resolved.resolution is not None
        assert dict(resolved.resolution.actor)["note"] == (
            "Authorization: Bearer [REDACTED]"
        )
        assert dict(resolved.resolution.evidence) == {
            "reason": "operator abort; Authorization: Bearer [REDACTED]"
        }
        assert raw_secret not in resolved.model_dump_json()
        assert actor_secret not in resolved.model_dump_json()
        assert provider_calls == []
        assert tool_calls == []
        run = await sdk.runs.get(run_id)
        assert run.status is RunStatus.FAILED
        assert run.error is not None
        assert run.error.model_dump(mode="json") == {
            "code": "application_resolution_aborted",
            "message": "operator abort; Authorization: Bearer [REDACTED]",
            "retryable": False,
        }
        operation = await store.get_external_operation(operation_id)
        assert operation is not None
        assert operation.status is ExternalOperationStatus.FAILED
        assert operation.model_dump(mode="json")["outcome"] == {
            "reconciliation": {
                "request_id": request.request_id,
                "action": "terminate",
                "outcome_known": False,
            }
        }
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.TERMINAL
        assert checkpoint.operation_id is None
        events = await store.read_events(after_cursor=0)
        assert [
            stored.event.type
            for stored in events
            if stored.event.type
            in {
                "reconciliation.resolved",
                "step.failed",
                "run.failed",
                "session.run.detached",
            }
        ][-4:] == [
            "reconciliation.resolved",
            "step.failed",
            "run.failed",
            "session.run.detached",
        ]
        assert (await sdk.recovery.pending_requests(run_id)) == ()
        timeline = await sdk.trace.timeline(run_id)
        assert ("run", "failed") in {
            (stage.kind.value, stage.status.value) for stage in timeline.stages
        }
        assert ("recovery", "completed") in {
            (stage.kind.value, stage.status.value) for stage in timeline.stages
        }
        terminal_cursor = await store.latest_cursor()
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()

    reopened_store: Any = (
        store if backend == "memory" else await SQLiteStore.open(database)
    )
    reopened = AgentSDK.for_test(
        store=reopened_store,
        acompletion=forbidden_provider,
        permission_default="allow",
    )
    reopened.agents.define(spec)
    reopened.tools.register(tool_spec, forbidden_tool)
    try:
        await reopened.recovery.scan()
        exact = await reopened.recovery.resolve(
            request.request_id,
            ReconciliationAction.TERMINATE,
            actor=actor,
            evidence=evidence,
        )
        assert exact == resolved
        assert await reopened_store.latest_cursor() == terminal_cursor
        with pytest.raises(AgentSDKError) as changed:
            await reopened.recovery.resolve(
                request.request_id,
                ReconciliationAction.TERMINATE,
                actor=actor,
                evidence={"reason": "different application decision"},
            )
        assert changed.value.code is ErrorCode.CONFLICT
        assert changed.value.message == "recovery state conflict"
        with pytest.raises(AgentSDKError) as retry:
            await reopened.recovery.resolve(
                request.request_id,
                ReconciliationAction.RETRY,
                actor=actor,
                evidence={"acknowledge_duplicate_side_effect_risk": True},
            )
        assert retry.value.code is ErrorCode.CONFLICT
        assert retry.value.message == "recovery state conflict"
        assert await reopened_store.latest_cursor() == terminal_cursor
        assert provider_calls == []
        assert tool_calls == []
    finally:
        await reopened.close()
        if backend == "sqlite":
            await reopened_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    ("actor", "evidence"),
    (
        ({"type": "operator"}, {"reason": ""}),
        ({"type": "operator"}, {"reason": "   \n\t"}),
        ({"type": "operator"}, {"reason": "x" * 257}),
        ({"type": "operator"}, {}),
        ({"type": "operator"}, {"reason": "ok", "extra": True}),
        ({"type": "operator", "token": "actor-secret"}, {"reason": "ok"}),
        ({"type": "x" * 1025}, {"reason": "ok"}),
    ),
    ids=(
        "empty",
        "whitespace",
        "oversized",
        "missing",
        "extra-evidence",
        "secret-actor-key",
        "oversized-actor",
    ),
)
async def test_terminate_rejects_unbounded_or_unsafe_reason_without_mutation(
    backend: str,
    actor: dict[str, object],
    evidence: dict[str, object],
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / f"invalid-terminate-{backend}.sqlite3")
    )
    run_id, spec, _operation_id, request = await _seed_pending_model_reconciliation(
        store
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: (_ for _ in ()).throw(
            AssertionError("invalid terminate must not call provider")
        ),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        before = await _resolution_domain_state(store)
        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.TERMINATE,
                actor=actor,
                evidence=evidence,
            )
        assert caught.value.code is ErrorCode.INVALID_STATE
        assert caught.value.message == "reconciliation decision is invalid"
        assert caught.value.retryable is False
        assert await _resolution_domain_state(store) == before
        assert (await sdk.runs.get(run_id)).status is RunStatus.WAITING_RECONCILIATION
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_resolution_replay_and_unsupported_actions_are_zero_mutation(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "replay-resolution.sqlite3")
    )
    run_id, spec, _operation_id = await _seed_real_model_in_flight(store)
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]
        initial_cursor = await store.latest_cursor()

        with pytest.raises(AgentSDKError) as malformed:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": "operator"},
                evidence={"disposition": "unknown"},
            )
        assert malformed.value.code is ErrorCode.INVALID_STATE
        assert await store.latest_cursor() == initial_cursor
        assert await store.get_reconciliation_request(request.request_id) == request

        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )
        resolved_cursor = await store.latest_cursor()
        replay = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )

        assert replay == resolved
        assert await store.latest_cursor() == resolved_cursor
        for changed_action, changed_actor, changed_evidence in (
            (
                ReconciliationAction.RETRY,
                {"type": "operator"},
                {"acknowledge_duplicate_side_effect_risk": True},
            ),
            (
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                {"type": "different"},
                {"disposition": "not_executed"},
            ),
        ):
            with pytest.raises(AgentSDKError) as conflict:
                await sdk.recovery.resolve(
                    request.request_id,
                    changed_action,
                    actor=changed_actor,
                    evidence=changed_evidence,
                )
            assert conflict.value.code is ErrorCode.CONFLICT
            assert conflict.value.message == "recovery state conflict"
            assert await store.latest_cursor() == resolved_cursor
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_resolution_replay_rejects_orphan_resolved_request_without_event(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "orphan-resolution.sqlite3")
    )
    run_id, spec, _operation_id, request = (
        await _seed_pending_model_reconciliation(store)
    )
    provider_calls: list[int] = []

    async def forbidden_completion(**_: object) -> Any:
        provider_calls.append(1)
        raise AssertionError("resolution replay must not call the provider")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )
        assert resolved.resolution is not None
        orphan = resolved.model_copy(
            update={
                "request_id": "rec_orphan_resolved",
                "resolution": resolved.resolution.model_copy(
                    update={"event_id": "evt_orphan_resolved"}
                ),
            }
        )
        orphan_json = _canonical_record_json(orphan)
        if isinstance(store, InMemoryStore):
            store._reconciliation_requests[orphan.request_id] = orphan_json
        else:
            await store._connection.execute(
                """
                INSERT INTO reconciliation_requests(
                    request_id, session_id, run_id, operation_id, status, data_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    orphan.request_id,
                    orphan.session_id,
                    orphan.run_id,
                    orphan.operation_id,
                    orphan.status.value,
                    orphan_json,
                ),
            )
            await store._connection.commit()
        before = await _resolution_domain_state(store)

        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": "operator"},
                evidence={"disposition": "not_executed"},
            )

        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery state conflict"
        assert provider_calls == []
        assert await _resolution_domain_state(store) == before
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "corruption",
    (
        "wrong_action_evidence",
        "noncanonical_reason",
        "noncanonical_details",
        "operation_fingerprint",
    ),
)
async def test_recovery_rejects_lockstep_corrupt_resolved_history_before_external_work(
    backend: str,
    corruption: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"corrupt-resolved-history-{corruption}.sqlite3"
        )
    )
    run_id, spec, _operation_id, request = (
        await _seed_pending_model_reconciliation(store)
    )
    provider_calls: list[int] = []
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(provider_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )
        assert resolved.resolution is not None
        operation = await store.get_external_operation(resolved.operation_id)
        assert operation is not None
        requested_event = next(
            stored
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type == "reconciliation.requested"
        )
        resolved_event = next(
            stored
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type == "reconciliation.resolved"
        )
        corrupted = resolved
        corrupted_operation = operation
        event_updates: list[tuple[Any, dict[str, Any]]] = []
        if corruption == "wrong_action_evidence":
            wrong_evidence = {"acknowledge_duplicate_side_effect_risk": True}
            corrupted = resolved.model_copy(
                update={
                    "resolution": resolved.resolution.model_copy(
                        update={"evidence": wrong_evidence}
                    )
                }
            )
            event_updates.append(
                (
                    resolved_event,
                    {**resolved_event.event.payload, "evidence": wrong_evidence},
                )
            )
        elif corruption == "noncanonical_reason":
            corrupted = resolved.model_copy(
                update={"reason": "tool_call_unknown_outcome"}
            )
            event_updates.append(
                (
                    requested_event,
                    {
                        **requested_event.event.payload,
                        "reason": corrupted.reason,
                    },
                )
            )
        elif corruption == "noncanonical_details":
            corrupted = resolved.model_copy(
                update={
                    "details": {
                        "checkpoint_phase": RunCheckpointPhase.READY_FOR_MODEL.value
                    }
                }
            )
        else:
            assert corruption == "operation_fingerprint"
            assert isinstance(operation, ModelCallOperation)
            corrupted_operation = _forge_model_operation_request(
                operation,
                marker="wrong attempt model request",
            )
        if isinstance(store, InMemoryStore):
            store._reconciliation_requests[corrupted.request_id] = (
                _canonical_record_json(corrupted)
            )
            store._external_operations[corrupted_operation.operation_id] = (
                _canonical_record_json(corrupted_operation)
            )
            for stored_event, payload in event_updates:
                event_index = store._events.index(stored_event)
                store._events[event_index] = stored_event._replace(
                    event=stored_event.event.model_copy(
                        update={"payload": payload}
                    )
                )
        else:
            await store._connection.execute(
                "UPDATE reconciliation_requests SET data_json = ? "
                "WHERE request_id = ?",
                (_canonical_record_json(corrupted), corrupted.request_id),
            )
            await store._connection.execute(
                "UPDATE external_operations "
                "SET request_fingerprint = ?, data_json = ? "
                "WHERE operation_id = ?",
                (
                    corrupted_operation.request_fingerprint,
                    _canonical_record_json(corrupted_operation),
                    corrupted_operation.operation_id,
                ),
            )
            for stored_event, payload in event_updates:
                await store._connection.execute(
                    "UPDATE events SET payload_json = ? WHERE cursor = ?",
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        stored_event.cursor,
                    ),
                )
            await store._connection.commit()

        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await sdk.recovery.recover_run(run_id)).result()

        assert provider_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].operation_id is None
        assert pending[0].reason == "recovery_state_invalid"
        events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        assert not any(event.type == "run.recovery.started" for event in events)
        assert not any(event.type == "permission.requested" for event in events)
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_moved_model_attempt_start_fails_closed_before_external_work(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "moved-model-start.sqlite3")
    )
    run_id, spec, _operation_id, request = (
        await _seed_pending_partial_model_reconciliation(store)
    )
    provider_calls: list[int] = []
    action = ReconciliationAction.CONFIRM_NOT_EXECUTED
    decision_evidence = {"disposition": "not_executed"}
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(provider_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        await sdk.recovery.resolve(
            request.request_id,
            action,
            actor={"type": "operator"},
            evidence=decision_evidence,
        )
        await _move_run_event_before_paired_interrupt(
            store,
            run_id=run_id,
            request_id=request.request_id,
            event_type="step.started",
        )
        before_events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        interrupt_index = next(
            index
            for index, event in enumerate(before_events)
            if event.type == "run.interrupted"
            and before_events[index + 1].type == "reconciliation.requested"
            and before_events[index + 1].payload.get("request_id")
            == request.request_id
        )
        assert before_events[interrupt_index - 1].type == "step.started"
        assert any(
            event.type == "model.call.started"
            for event in before_events[: interrupt_index - 1]
        )
        assert any(
            event.type == "model.text.delta"
            for event in before_events[: interrupt_index - 1]
        )

        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await sdk.recovery.recover_run(run_id)).result()

        assert provider_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].operation_id is None
        assert pending[0].reason == "recovery_state_invalid"
        after_events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        for external_event_type in (
            "run.recovery.started",
            "permission.requested",
            "tool.recovery.retry.started",
        ):
            assert sum(
                event.type == external_event_type for event in after_events
            ) == sum(
                event.type == external_event_type for event in before_events
            )
        before_replay = await _resolution_domain_state(store)
        with pytest.raises(AgentSDKError) as replay_conflict:
            await sdk.recovery.resolve(
                request.request_id,
                action,
                actor={"type": "operator"},
                evidence=decision_evidence,
            )
        assert replay_conflict.value.code is ErrorCode.CONFLICT
        assert replay_conflict.value.message == "recovery state conflict"
        assert provider_calls == []
        assert await _resolution_domain_state(store) == before_replay
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_moved_tool_attempt_start_fails_closed_before_external_work(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "moved-tool-start.sqlite3")
    )
    run_id, spec, tool_spec, _operation_id, request = (
        await _seed_pending_tool_reconciliation(store)
    )
    provider_calls: list[int] = []
    tool_calls: list[int] = []
    action = ReconciliationAction.RETRY
    decision_evidence = {"acknowledge_duplicate_side_effect_risk": True}

    async def forbidden_tool_handler(_: ToolContext, value: int) -> int:
        tool_calls.append(value)
        raise AssertionError("corrupt resolved history must not call the tool")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(provider_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, forbidden_tool_handler)
    try:
        await sdk.recovery.resolve(
            request.request_id,
            action,
            actor={"type": "operator"},
            evidence=decision_evidence,
        )
        await _move_run_event_before_paired_interrupt(
            store,
            run_id=run_id,
            request_id=request.request_id,
            event_type="tool.call.proposed",
        )
        before_events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        interrupt_index = next(
            index
            for index, event in enumerate(before_events)
            if event.type == "run.interrupted"
            and before_events[index + 1].type == "reconciliation.requested"
            and before_events[index + 1].payload.get("request_id")
            == request.request_id
        )
        assert before_events[interrupt_index - 1].type == "tool.call.proposed"
        assert any(
            event.type == "tool.call.authorized"
            for event in before_events[: interrupt_index - 1]
        )
        assert any(
            event.type == "tool.call.started"
            for event in before_events[: interrupt_index - 1]
        )

        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await sdk.recovery.recover_run(run_id)).result()

        assert provider_calls == []
        assert tool_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].operation_id is None
        assert pending[0].reason == "recovery_state_invalid"
        after_events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        for external_event_type in (
            "run.recovery.started",
            "permission.requested",
            "tool.recovery.retry.started",
        ):
            assert sum(
                event.type == external_event_type for event in after_events
            ) == sum(
                event.type == external_event_type for event in before_events
            )
        before_replay = await _resolution_domain_state(store)
        with pytest.raises(AgentSDKError) as replay_conflict:
            await sdk.recovery.resolve(
                request.request_id,
                action,
                actor={"type": "operator"},
                evidence=decision_evidence,
            )
        assert replay_conflict.value.code is ErrorCode.CONFLICT
        assert replay_conflict.value.message == "recovery state conflict"
        assert provider_calls == []
        assert tool_calls == []
        assert await _resolution_domain_state(store) == before_replay
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("operation_kind", ("model", "tool"))
async def test_duplicate_attempt_start_fails_closed_before_external_work(
    backend: str,
    operation_kind: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"duplicate-{operation_kind}-start.sqlite3"
        )
    )
    tool_spec: ToolSpec | None = None
    if operation_kind == "model":
        run_id, spec, _operation_id, request = (
            await _seed_pending_model_reconciliation(store)
        )
        action = ReconciliationAction.CONFIRM_NOT_EXECUTED
        decision_evidence = {"disposition": "not_executed"}
        event_type = "step.started"
    else:
        assert operation_kind == "tool"
        run_id, spec, tool_spec, _operation_id, request = (
            await _seed_pending_tool_reconciliation(store)
        )
        action = ReconciliationAction.RETRY
        decision_evidence = {"acknowledge_duplicate_side_effect_risk": True}
        event_type = "tool.call.proposed"
    provider_calls: list[int] = []
    tool_calls: list[int] = []

    async def forbidden_tool_handler(_: ToolContext, value: int) -> int:
        tool_calls.append(value)
        raise AssertionError("corrupt resolved history must not call the tool")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(provider_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    if tool_spec is not None:
        sdk.tools.register(tool_spec, forbidden_tool_handler)
    try:
        await sdk.recovery.resolve(
            request.request_id,
            action,
            actor={"type": "operator"},
            evidence=decision_evidence,
        )
        await _insert_duplicate_run_event_before_paired_interrupt(
            store,
            run_id=run_id,
            request_id=request.request_id,
            event_type=event_type,
        )
        before_events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )

        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await sdk.recovery.recover_run(run_id)).result()

        assert provider_calls == []
        assert tool_calls == []
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].operation_id is None
        assert pending[0].reason == "recovery_state_invalid"
        after_events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        for external_event_type in (
            "run.recovery.started",
            "permission.requested",
            "tool.recovery.retry.started",
        ):
            assert sum(
                event.type == external_event_type for event in after_events
            ) == sum(
                event.type == external_event_type for event in before_events
            )
        before_replay = await _resolution_domain_state(store)
        with pytest.raises(AgentSDKError) as replay_conflict:
            await sdk.recovery.resolve(
                request.request_id,
                action,
                actor={"type": "operator"},
                evidence=decision_evidence,
            )
        assert replay_conflict.value.code is ErrorCode.CONFLICT
        assert replay_conflict.value.message == "recovery state conflict"
        assert provider_calls == []
        assert tool_calls == []
        assert await _resolution_domain_state(store) == before_replay
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("operation_kind", ("model", "tool"))
async def test_later_turn_canonical_resolution_replays_and_recovers_once(
    backend: str,
    operation_kind: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"later-turn-{operation_kind}-resolution.sqlite3"
        )
    )
    run_id, spec, tool_spec, operation_id = await _seed_later_turn_in_flight(
        store,
        operation_kind=operation_kind,
    )
    provider_calls: list[int] = []
    tool_calls: list[int] = []

    async def final_handler(_: ToolContext, value: int) -> int:
        tool_calls.append(value)
        return value + 1

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(provider_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    sdk.tools.register(tool_spec, final_handler)
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]
        assert request.operation_id == operation_id
        action = (
            ReconciliationAction.CONFIRM_NOT_EXECUTED
            if operation_kind == "model"
            else ReconciliationAction.RETRY
        )
        decision_evidence = (
            {"disposition": "not_executed"}
            if operation_kind == "model"
            else {"acknowledge_duplicate_side_effect_risk": True}
        )
        resolved = await sdk.recovery.resolve(
            request.request_id,
            action,
            actor={"type": "operator"},
            evidence=decision_evidence,
        )
        before_replay = await _resolution_domain_state(store)

        replay = await sdk.recovery.resolve(
            request.request_id,
            action,
            actor={"type": "operator"},
            evidence=decision_evidence,
        )

        assert replay == resolved
        assert provider_calls == []
        assert tool_calls == []
        assert await _resolution_domain_state(store) == before_replay

        result = await (await sdk.recovery.recover_run(run_id)).result()

        assert result.output_text == "done"
        assert provider_calls == [1]
        assert tool_calls == ([] if operation_kind == "model" else [2])
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("same_decision", (True, False))
async def test_two_sdk_resolution_owner_and_follower_converge_or_conflict(
    backend: str,
    same_decision: bool,
    tmp_path: Path,
) -> None:
    if backend == "memory":
        owner_store: Any = _ResolutionBarrierMemoryStore()
        follower_store: Any = owner_store
    else:
        opened = await SQLiteStore.open(tmp_path / f"two-sdk-{same_decision}.sqlite3")
        owner_store = _ResolutionBarrierSQLiteStore(opened._connection)
        follower_store = await SQLiteStore.open(
            tmp_path / f"two-sdk-{same_decision}.sqlite3"
        )
    run_id, spec, _operation_id = await _seed_real_model_in_flight(owner_store)
    owner = AgentSDK.for_test(
        store=owner_store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    follower = AgentSDK.for_test(
        store=follower_store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    for sdk in (owner, follower):
        sdk.agents.define(spec)
    try:
        waiting = await owner.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await waiting.result()
        request = (await owner.recovery.pending_requests(run_id))[0]
        owner_store.resolution_barrier_enabled = True
        before = await owner_store.latest_cursor()
        owner_task = asyncio.create_task(
            owner.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": "operator"},
                evidence={"disposition": "not_executed"},
            )
        )
        await asyncio.wait_for(owner_store.resolution_reached.wait(), timeout=10)
        follower_action = (
            ReconciliationAction.CONFIRM_NOT_EXECUTED
            if same_decision
            else ReconciliationAction.RETRY
        )
        follower_evidence = (
            {"disposition": "not_executed"}
            if same_decision
            else {"acknowledge_duplicate_side_effect_risk": True}
        )
        follower_task = asyncio.create_task(
            follower.recovery.resolve(
                request.request_id,
                follower_action,
                actor={"type": "operator"},
                evidence=follower_evidence,
            )
        )
        await asyncio.sleep(0)
        owner_store.allow_resolution.set()
        results = await asyncio.gather(
            owner_task,
            follower_task,
            return_exceptions=True,
        )

        successes = [item for item in results if not isinstance(item, BaseException)]
        failures = [item for item in results if isinstance(item, BaseException)]
        if same_decision:
            assert len(successes) == 2
            assert successes[0] == successes[1]
            assert failures == []
        else:
            assert len(successes) == 1
            assert len(failures) == 1
            assert isinstance(failures[0], AgentSDKError)
            assert failures[0].code is ErrorCode.CONFLICT
        assert await owner_store.latest_cursor() == before + 1
        events = await owner_store.read_events(after_cursor=0)
        assert sum(
            item.event.type == "reconciliation.resolved" for item in events
        ) == 1
    finally:
        owner_store.allow_resolution.set()
        await asyncio.gather(owner.close(), follower.close())
        if backend == "sqlite":
            await owner_store.close()
            await follower_store.close()


@pytest.mark.asyncio
async def test_public_resolution_cancellation_at_memory_precommit_is_atomic() -> None:
    store = _ResolutionBarrierMemoryStore()
    run_id, spec, operation_id = await _seed_real_model_in_flight(store)
    model_calls: list[int] = []
    actor_secret = "cancelled-resolution-actor-secret"
    evidence_secret = "cancelled-resolution-evidence-secret"
    actor = {"type": "operator", "id": actor_secret}
    evidence = _SecretMapping(
        {"disposition": "not_executed"},
        evidence_secret,
    )

    async def forbidden_completion(**_: object) -> Any:
        model_calls.append(1)
        raise AssertionError("resolution must not call the provider")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    resolve_task: asyncio.Task[Any] | None = None
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]
        before_cursor = await store.latest_cursor()
        before_events = await store.read_events(after_cursor=0)
        before_checkpoint = await store.get_run_checkpoint(run_id)
        before_operation = await store.get_external_operation(operation_id)
        before_run = await sdk.runs.get(run_id)

        store.resolution_barrier_enabled = True
        resolve_task = asyncio.create_task(
            sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor=actor,
                evidence=evidence,
            )
        )
        await asyncio.wait_for(store.resolution_reached.wait(), timeout=10)

        assert resolve_task.cancel()
        assert resolve_task.cancelling() == 1
        assert await store.get_reconciliation_request(request.request_id) == request
        assert await store.read_events(after_cursor=0) == before_events
        assert await store.get_run_checkpoint(run_id) == before_checkpoint
        assert await store.get_external_operation(operation_id) == before_operation
        assert await sdk.runs.get(run_id) == before_run
        assert await store.latest_cursor() == before_cursor
        assert model_calls == []

        store.allow_resolution.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(resolve_task, timeout=10)
        _assert_secret_free(cancelled.value, actor_secret, evidence_secret)

        resolved = await store.get_reconciliation_request(request.request_id)
        assert resolved is not None
        assert resolved.status.value == "resolved"
        assert await store.latest_cursor() == before_cursor + 1
        events = await store.read_events(after_cursor=0)
        assert sum(
            item.event.type == "reconciliation.resolved" for item in events
        ) == 1
        assert await store.get_run_checkpoint(run_id) != before_checkpoint
        assert await store.get_external_operation(operation_id) != before_operation
        assert await sdk.runs.get(run_id) != before_run
        assert model_calls == []

        replay_cursor = await store.latest_cursor()
        replay = await asyncio.wait_for(
            sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor=actor,
                evidence=evidence,
            ),
            timeout=10,
        )

        assert replay == resolved
        assert await store.latest_cursor() == replay_cursor
        replay_events = await store.read_events(after_cursor=0)
        assert sum(
            item.event.type == "reconciliation.resolved" for item in replay_events
        ) == 1
        assert model_calls == []
    finally:
        store.allow_resolution.set()
        if resolve_task is not None and not resolve_task.done():
            await asyncio.wait_for(
                asyncio.gather(resolve_task, return_exceptions=True),
                timeout=10,
            )
        await sdk.close()


@pytest.mark.asyncio
async def test_sdk_close_during_admitted_memory_resolution_is_atomic() -> None:
    store = _ResolutionBarrierMemoryStore()
    run_id, spec, operation_id = await _seed_real_model_in_flight(store)
    provider_calls: list[int] = []

    async def forbidden_completion(**_: object) -> Any:
        provider_calls.append(1)
        raise AssertionError("resolution must not call the provider")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    resolve_task: asyncio.Task[Any] | None = None
    close_task: asyncio.Task[None] | None = None
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]
        before_cursor = await store.latest_cursor()
        before_events = await store.read_events(after_cursor=0)
        before_run = await store.get_snapshot("run", run_id)
        before_checkpoint = await store.get_run_checkpoint(run_id)
        before_operation = await store.get_external_operation(operation_id)
        assert before_run is not None
        assert before_checkpoint is not None
        assert before_operation is not None

        store.resolution_barrier_enabled = True
        resolve_task = asyncio.create_task(
            sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": "operator"},
                evidence={"disposition": "not_executed"},
            )
        )
        await asyncio.wait_for(store.resolution_reached.wait(), timeout=10)

        close_task = asyncio.create_task(sdk.close())
        await asyncio.wait_for(sdk._lifecycle.close_signal.wait(), timeout=10)
        assert not close_task.done()
        assert resolve_task.cancel()
        store.allow_resolution.set()
        results = await asyncio.wait_for(
            asyncio.gather(resolve_task, close_task, return_exceptions=True),
            timeout=10,
        )
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None

        after_cursor = await store.latest_cursor()
        after_events = await store.read_events(after_cursor=0)
        after_run = await store.get_snapshot("run", run_id)
        after_checkpoint = await store.get_run_checkpoint(run_id)
        after_operation = await store.get_external_operation(operation_id)
        after_request = await store.get_reconciliation_request(request.request_id)
        decision_events = tuple(
            item
            for item in after_events
            if item.event.type == "reconciliation.resolved"
        )

        assert after_cursor - before_cursor in {0, 1}
        assert len(decision_events) == after_cursor - before_cursor
        if after_cursor == before_cursor:
            assert after_events == before_events
            assert after_run == before_run
            assert after_checkpoint == before_checkpoint
            assert after_operation == before_operation
            assert after_request == request
        else:
            assert after_cursor == before_cursor + 1
            assert after_events[:-1] == before_events
            assert after_request is not None
            assert after_request.status.value == "resolved"
            assert after_request.resolution is not None
            assert after_request.resolution.event_id == decision_events[0].event.event_id
            assert after_request.resolution.action is ReconciliationAction.CONFIRM_NOT_EXECUTED
            assert after_request.resolution.actor == {"type": "operator"}
            assert after_request.resolution.evidence == {
                "disposition": "not_executed"
            }
            assert decision_events[0].event.payload == {
                "request_id": request.request_id,
                "operation_id": operation_id,
                "action": ReconciliationAction.CONFIRM_NOT_EXECUTED.value,
                "actor": {"type": "operator"},
                "evidence": {"disposition": "not_executed"},
            }
            assert after_run is not None
            assert after_run["status"] == RunStatus.INTERRUPTED.value
            assert after_run["version"] == before_run["version"] + 1
            assert after_checkpoint is not None
            assert after_checkpoint.checkpoint_version == (
                before_checkpoint.checkpoint_version + 1
            )
            assert after_checkpoint.phase is RunCheckpointPhase.READY_FOR_MODEL
            assert after_checkpoint.operation_id is None
            assert after_operation is not None
            assert after_operation.status is ExternalOperationStatus.FAILED
            assert after_operation.outcome == {
                "reconciliation": {
                    "request_id": request.request_id,
                    "action": ReconciliationAction.CONFIRM_NOT_EXECUTED.value,
                }
            }
        assert provider_calls == []
    finally:
        store.allow_resolution.set()
        pending = tuple(
            task
            for task in (resolve_task, close_task)
            if task is not None and not task.done()
        )
        if pending:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=10,
            )
        if close_task is None:
            await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("lease_race", ("lost", "expired"))
async def test_resolution_lease_loss_or_expiry_is_zero_mutation(
    backend: str,
    lease_race: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        _ResolutionBarrierMemoryStore()
        if backend == "memory"
        else _ResolutionBarrierSQLiteStore(
            (
                await SQLiteStore.open(
                    tmp_path / f"resolution-{lease_race}.sqlite3"
                )
            )._connection
        )
    )
    run_id, spec, _operation_id, request = (
        await _seed_pending_model_reconciliation(store)
    )
    provider_calls: list[int] = []

    async def forbidden_completion(**_: object) -> Any:
        provider_calls.append(1)
        raise AssertionError("resolution must not call the provider")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    clock = [datetime(2030, 1, 1, tzinfo=UTC)]
    sdk.recovery._service._clock = lambda: clock[0]
    sdk.recovery._service._leases = LeaseManager(store, ttl=timedelta(seconds=1))
    resolve_task: asyncio.Task[Any] | None = None
    try:
        before = await _resolution_domain_state(store)
        store.resolution_evidence_barrier_enabled = True
        resolve_task = asyncio.create_task(
            sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": "operator"},
                evidence={"disposition": "not_executed"},
            )
        )
        await asyncio.wait_for(
            store.resolution_evidence_reached.wait(),
            timeout=10,
        )
        lease = await store.get_run_lease(run_id)
        assert lease is not None
        assert lease.expires_at == clock[0] + timedelta(seconds=1)
        if lease_race == "lost":
            await store.release_lease(lease)
        else:
            clock[0] = lease.expires_at
        store.allow_resolution_evidence.set()

        with pytest.raises(AgentSDKError) as caught:
            await asyncio.wait_for(resolve_task, timeout=10)

        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery state conflict"
        assert await _resolution_domain_state(store) == before
        assert provider_calls == []
    finally:
        store.allow_resolution_evidence.set()
        if resolve_task is not None and not resolve_task.done():
            await asyncio.wait_for(
                asyncio.gather(resolve_task, return_exceptions=True),
                timeout=10,
            )
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("cas_target", ("run", "checkpoint", "reconciliation"))
async def test_resolution_run_checkpoint_or_request_cas_conflict_is_atomic(
    backend: str,
    cas_target: str,
    tmp_path: Path,
) -> None:
    store = await _open_resolution_barrier_store(
        backend,
        tmp_path / f"resolution-cas-{cas_target}.sqlite3",
    )
    run_id, spec, _operation_id, request = (
        await _seed_pending_model_reconciliation(store)
    )
    provider_calls: list[int] = []

    async def forbidden_completion(**_: object) -> Any:
        provider_calls.append(1)
        raise AssertionError("resolution must not call the provider")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    resolve_task: asyncio.Task[Any] | None = None
    try:
        store.resolution_barrier_enabled = True
        resolve_task = asyncio.create_task(
            sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": "operator"},
                evidence={"disposition": "not_executed"},
            )
        )
        await asyncio.wait_for(store.resolution_reached.wait(), timeout=10)
        await _race_resolution_cas(store, cas_target, run_id, request)
        raced = await _resolution_domain_state(store)
        store.allow_resolution.set()

        with pytest.raises(AgentSDKError) as caught:
            await asyncio.wait_for(resolve_task, timeout=10)

        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery state conflict"
        assert await _resolution_domain_state(store) == raced
        events = await store.read_events(after_cursor=0)
        assert all(
            item.event.type != "reconciliation.resolved" for item in events
        )
        assert provider_calls == []
    finally:
        store.allow_resolution.set()
        if resolve_task is not None and not resolve_task.done():
            await asyncio.wait_for(
                asyncio.gather(resolve_task, return_exceptions=True),
                timeout=10,
            )
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
async def test_resolved_model_attempt_is_excluded_from_next_same_turn_attempt() -> None:
    store = InMemoryStore()
    run_id, spec, _first_operation = await _seed_real_model_in_flight(store)
    first = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    first.agents.define(spec)
    try:
        waiting = await first.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await waiting.result()
        first_request = (await first.recovery.pending_requests(run_id))[0]
        await first.recovery.resolve(
            first_request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )
    finally:
        await first.close()

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_retry(**_: object) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    second = AgentSDK.for_test(
        store=store,
        acompletion=blocked_retry,
        permission_default="allow",
    )
    second.agents.define(spec)
    try:
        retry = await second.recovery.recover_run(run_id)
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert retry._task is not None
        retry._task.cancel()
        with pytest.raises(AgentSDKError):
            await retry.result()
        await asyncio.wait_for(cancelled.wait(), timeout=10)
    finally:
        await second.close()
    await _mark_interrupted(store)

    final_calls: list[int] = []
    final = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(final_calls),
        permission_default="allow",
    )
    final.agents.define(spec)
    try:
        waiting = await final.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await waiting.result()
        second_request = (await final.recovery.pending_requests(run_id))[0]

        await final.recovery.resolve(
            second_request.request_id,
            ReconciliationAction.RETRY,
            actor={"type": "operator"},
            evidence={"acknowledge_duplicate_side_effect_risk": True},
        )
        resolved_operations = await store.list_external_operations(run_id)
        assert len(resolved_operations) == 2
        assert all(
            item.status is ExternalOperationStatus.FAILED
            for item in resolved_operations
        )
        result = await (await final.recovery.recover_run(run_id)).result()

        assert result.output_text == "done"
        assert final_calls == [1]
        events = await store.read_events(after_cursor=0)
        assert sum(item.event.type == "model.call.started" for item in events) == 3
    finally:
        await final.close()


@pytest.mark.asyncio
async def test_resolved_tool_attempt_is_excluded_from_next_same_turn_attempt() -> None:
    store = InMemoryStore()
    run_id, spec, tool_spec, _first_operation = (
        await _seed_real_tool_in_flight(store)
    )

    async def forbidden_handler(_: ToolContext, value: int) -> int:
        del value
        raise AssertionError("resolution must not call the tool")

    first = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    first.agents.define(spec)
    first.tools.register(tool_spec, forbidden_handler)
    try:
        waiting = await first.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        first_request = (await first.recovery.pending_requests(run_id))[0]
        await first.recovery.resolve(
            first_request.request_id,
            ReconciliationAction.RETRY,
            actor={"type": "operator"},
            evidence={"acknowledge_duplicate_side_effect_risk": True},
        )
    finally:
        await first.close()

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_retry(_: ToolContext, value: int) -> int:
        del value
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    second = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    second.agents.define(spec)
    second.tools.register(tool_spec, blocked_retry)
    try:
        retry = await second.recovery.recover_run(run_id)
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert retry._task is not None
        retry._task.cancel()
        with pytest.raises(AgentSDKError):
            await retry.result()
        await asyncio.wait_for(cancelled.wait(), timeout=10)
    finally:
        await second.close()
    await _mark_interrupted(store)

    tool_calls: list[int] = []
    provider_calls: list[int] = []

    async def final_handler(_: ToolContext, value: int) -> int:
        tool_calls.append(value)
        return value + 1

    final = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(provider_calls),
        permission_default="allow",
    )
    final.agents.define(spec)
    final.tools.register(tool_spec, final_handler)
    try:
        waiting = await final.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        second_request = (await final.recovery.pending_requests(run_id))[0]
        await final.recovery.resolve(
            second_request.request_id,
            ReconciliationAction.RETRY,
            actor={"type": "operator"},
            evidence={"acknowledge_duplicate_side_effect_risk": True},
        )
        result = await (await final.recovery.recover_run(run_id)).result()

        assert result.output_text == "done"
        assert tool_calls == [7]
        assert provider_calls == [1]
        events = await store.read_events(after_cursor=0)
        assert sum(
            item.event.type == "tool.call.proposed" for item in events
        ) == 3
    finally:
        await final.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "corruption",
    (
        "request_wrong_link",
        "request_missing",
        "request_legacy",
        "checkpoint_phase",
        "checkpoint_turn",
        "checkpoint_operation",
        "operation_status",
        "operation_id",
        "operation_kind",
        "operation_turn",
        "operation_fingerprint",
    ),
)
async def test_resolution_rejects_corrupt_request_operation_or_checkpoint(
    backend: str,
    corruption: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"linkage-{backend}-{corruption}.sqlite3"
        )
    )
    run_id, spec, operation_id, request = (
        await _seed_pending_model_reconciliation(store)
    )
    checkpoint = await store.get_run_checkpoint(run_id)
    operation = await store.get_external_operation(operation_id)
    assert checkpoint is not None
    assert operation is not None
    corruption_secret = "durable-linkage-corruption-secret"

    target_table: str | None = None
    target_key: str | None = None
    corrupted_json: str | None = None
    if corruption == "request_missing":
        if isinstance(store, InMemoryStore):
            store._reconciliation_requests.pop(request.request_id)
        else:
            await store._connection.execute(
                "DELETE FROM reconciliation_requests WHERE request_id = ?",
                (request.request_id,),
            )
            await store._connection.commit()
    elif corruption in {"request_wrong_link", "request_legacy"}:
        corrupted_request = request.model_copy(
            update=(
                {"operation_id": corruption_secret}
                if corruption == "request_wrong_link"
                else {
                    "operation_id": None,
                    "reason": "recovery_state_invalid",
                    "details": {},
                }
            )
        )
        target_table = "reconciliation_requests"
        target_key = request.request_id
        corrupted_json = _canonical_record_json(corrupted_request)
    elif corruption.startswith("checkpoint_"):
        checkpoint_updates: dict[str, Any]
        if corruption == "checkpoint_phase":
            checkpoint_updates = {"phase": RunCheckpointPhase.TOOL_IN_FLIGHT}
        elif corruption == "checkpoint_turn":
            checkpoint_updates = {"turn": checkpoint.turn + 1}
        else:
            checkpoint_updates = {"operation_id": corruption_secret}
        corrupted_checkpoint = checkpoint.model_copy(update=checkpoint_updates)
        target_table = "run_checkpoints"
        target_key = run_id
        corrupted_json = _canonical_record_json(corrupted_checkpoint)
    else:
        operation_data = operation.model_dump(mode="json")
        if corruption == "operation_status":
            operation_data.update(
                status=ExternalOperationStatus.FAILED.value,
                outcome={"error": {"message": "corrupt"}},
            )
        elif corruption == "operation_id":
            operation_data["operation_id"] = corruption_secret
        elif corruption == "operation_kind":
            operation_data.update(
                operation_kind="tool_call",
                provider_identity=None,
                tool_identity="sha256:corrupt-tool-capability",
            )
        elif corruption == "operation_turn":
            operation_data["turn"] = operation.turn + 1
        else:
            assert corruption == "operation_fingerprint"
            operation_data["request_fingerprint"] = corruption_secret
        target_table = "external_operations"
        target_key = operation_id
        corrupted_json = json.dumps(
            operation_data,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    if corrupted_json is not None:
        assert target_table is not None
        assert target_key is not None
        if isinstance(store, InMemoryStore):
            target = {
                "reconciliation_requests": store._reconciliation_requests,
                "run_checkpoints": store._run_checkpoints,
                "external_operations": store._external_operations,
            }[target_table]
            target[target_key] = corrupted_json
        else:
            await store._connection.execute("PRAGMA ignore_check_constraints = ON")
            key_column = {
                "reconciliation_requests": "request_id",
                "run_checkpoints": "run_id",
                "external_operations": "operation_id",
            }[target_table]
            await store._connection.execute(
                f"UPDATE {target_table} SET data_json = ? WHERE {key_column} = ?",
                (corrupted_json, target_key),
            )
            await store._connection.commit()

    provider_calls: list[int] = []

    async def forbidden_completion(**_: object) -> Any:
        provider_calls.append(1)
        raise AssertionError("resolution must not call the provider")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        before = await _resolution_domain_state(store)

        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": "operator"},
                evidence={"disposition": "not_executed"},
            )

        if corruption == "request_missing":
            assert caught.value.code is ErrorCode.NOT_FOUND
            assert caught.value.message == "reconciliation request not found"
        else:
            assert caught.value.code is ErrorCode.CONFLICT
            assert caught.value.message == "recovery state conflict"
        assert provider_calls == []
        assert await _resolution_domain_state(store) == before
        if corruption in {
            "request_wrong_link",
            "checkpoint_operation",
            "operation_id",
            "operation_fingerprint",
        }:
            _assert_secret_free(caught.value, corruption_secret)
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "admission_case",
    (
        "capability_drift",
        "event_id",
        "event_sequence",
        "event_payload",
        "session_ownership",
        "duplicate_pending",
    ),
)
@pytest.mark.parametrize("decision", ("safe", "confirmed"))
async def test_resolution_rejects_capability_drift_or_corrupt_durable_state(
    backend: str,
    decision: str,
    admission_case: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"admission-{backend}-{admission_case}.sqlite3"
        )
    )
    run_id, spec, _operation_id, request = (
        await _seed_pending_model_reconciliation(store)
    )
    corruption_secret = "event-payload-corruption-secret"
    if admission_case in {"event_id", "event_sequence", "event_payload"}:
        requested = next(
            stored
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type == "reconciliation.requested"
        )
        event_update: dict[str, Any]
        if admission_case == "event_id":
            event_update = {"event_id": ""}
        elif admission_case == "event_sequence":
            event_update = {"sequence": requested.event.sequence + 7}
        else:
            event_update = {
                "payload": {
                    **requested.event.payload,
                    "reason": corruption_secret,
                }
            }
        if isinstance(store, InMemoryStore):
            index = store._events.index(requested)
            store._events[index] = requested._replace(
                event=requested.event.model_copy(update=event_update)
            )
        else:
            if admission_case == "event_payload":
                await store._connection.execute(
                    "UPDATE events SET payload_json = ? WHERE cursor = ?",
                    (
                        json.dumps(
                            event_update["payload"],
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        requested.cursor,
                    ),
                )
            else:
                column = "event_id" if admission_case == "event_id" else "sequence"
                await store._connection.execute(
                    f"UPDATE events SET {column} = ? WHERE cursor = ?",
                    (event_update[column], requested.cursor),
                )
            await store._connection.commit()
    elif admission_case == "session_ownership":
        run = await store.get_snapshot("run", run_id)
        assert run is not None
        session_id = run["session_id"]
        session = await store.get_snapshot("session", session_id)
        assert session is not None
        corrupted_session = {**session, "active_run_ids": []}
        if isinstance(store, InMemoryStore):
            key = ("session", session_id)
            store._snapshots[key] = store._snapshots[key]._replace(
                data=corrupted_session
            )
        else:
            await store._connection.execute(
                "UPDATE snapshots SET data_json = ? "
                "WHERE kind = 'session' AND entity_id = ?",
                (
                    json.dumps(
                        corrupted_session,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    session_id,
                ),
            )
            await store._connection.commit()
    elif admission_case == "duplicate_pending":
        duplicate = request.model_copy(
            update={"request_id": "rec_duplicate_pending"}
        )
        serialized = _canonical_record_json(duplicate)
        if isinstance(store, InMemoryStore):
            store._reconciliation_requests[duplicate.request_id] = serialized
        else:
            await store._connection.execute(
                "INSERT INTO reconciliation_requests("
                "request_id, session_id, run_id, operation_id, status, data_json"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    duplicate.request_id,
                    duplicate.session_id,
                    duplicate.run_id,
                    duplicate.operation_id,
                    duplicate.status.value,
                    serialized,
                ),
            )
            await store._connection.commit()

    provider_calls: list[int] = []

    async def forbidden_completion(**_: object) -> Any:
        provider_calls.append(1)
        raise AssertionError("resolution must not call the provider")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_completion,
        permission_default="allow",
    )
    selected_spec = (
        spec.model_copy(update={"model": "fake/capability-drift-secret"})
        if admission_case == "capability_drift"
        else spec
    )
    sdk.agents.define(selected_spec)
    try:
        before = await _resolution_public_state(store, run_id)

        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                (
                    ReconciliationAction.CONFIRM_COMPLETED
                    if decision == "confirmed"
                    else ReconciliationAction.CONFIRM_NOT_EXECUTED
                ),
                actor={"type": "operator"},
                evidence=(
                    {"provider_result": _confirmed_provider_result()}
                    if decision == "confirmed"
                    else {"disposition": "not_executed"}
                ),
            )

        if admission_case == "capability_drift":
            assert caught.value.code is ErrorCode.INVALID_STATE
            assert caught.value.message == "recovery capabilities unavailable"
        else:
            assert caught.value.code is ErrorCode.CONFLICT
            assert caught.value.message == "recovery state conflict"
        assert provider_calls == []
        assert await _resolution_public_state(store, run_id) == before
        if admission_case == "capability_drift":
            _assert_secret_free(caught.value, "fake/capability-drift-secret")
        elif admission_case == "event_payload":
            _assert_secret_free(caught.value, corruption_secret)
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "admission_case",
    (
        "malformed_actor",
        "malformed_evidence",
        "confirm_completed",
        "terminate",
    ),
)
async def test_resolution_rejects_malformed_or_unsupported_decision_secret_free(
    backend: str,
    admission_case: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"decision-{backend}-{admission_case}.sqlite3"
        )
    )
    run_id, spec, _operation_id, request = (
        await _seed_pending_model_reconciliation(store)
    )
    actor_secret = "malformed-actor-secret"
    evidence_secret = "malformed-evidence-secret"
    action = ReconciliationAction.CONFIRM_NOT_EXECUTED
    actor: Any = {"type": "operator"}
    evidence: Any = {"disposition": "not_executed"}
    expected_message = "reconciliation decision is invalid"
    secrets: tuple[str, ...]
    if admission_case == "malformed_actor":
        actor = {"type": "operator", "secret": actor_secret, "bad": {actor_secret}}
        secrets = (actor_secret,)
    elif admission_case == "malformed_evidence":
        actor = {"type": "operator", "secret": actor_secret}
        evidence = {
            "disposition": "not_executed",
            "secret": evidence_secret,
        }
        secrets = (actor_secret, evidence_secret)
    else:
        action = (
            ReconciliationAction.CONFIRM_COMPLETED
            if admission_case == "confirm_completed"
            else ReconciliationAction.TERMINATE
        )
        actor = {"type": "operator", "secret": actor_secret}
        evidence = {"secret": evidence_secret}
        expected_message = "reconciliation decision is invalid"
        secrets = (actor_secret, evidence_secret)

    provider_calls: list[int] = []

    async def forbidden_completion(**_: object) -> Any:
        provider_calls.append(1)
        raise AssertionError("resolution must not call the provider")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=forbidden_completion,
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        before = await _resolution_public_state(store, run_id)

        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                action,
                actor=actor,
                evidence=evidence,
            )

        assert caught.value.code is ErrorCode.INVALID_STATE
        assert caught.value.message == expected_message
        assert provider_calls == []
        assert await _resolution_public_state(store, run_id) == before
        _assert_secret_free(caught.value, *secrets)
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_post_commit_resolution_ambiguity_replays_exact_batch_once(
    backend: str,
    tmp_path: Path,
) -> None:
    if backend == "memory":
        store: Any = _AmbiguousResolutionMemoryStore()
    else:
        opened = await SQLiteStore.open(tmp_path / "ambiguous-resolution.sqlite3")
        store = _AmbiguousResolutionSQLiteStore(opened._connection)
    run_id, spec, _operation_id = await _seed_real_model_in_flight(store)
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]
        before = await store.latest_cursor()

        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )

        assert resolved.status.value == "resolved"
        assert store.resolution_batches == 2
        assert await store.latest_cursor() == before + 1
        events = await store.read_events(after_cursor=0)
        assert sum(
            item.event.type == "reconciliation.resolved" for item in events
        ) == 1
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
async def test_post_commit_partial_resolution_state_is_constant_conflict() -> None:
    store = _PartialAmbiguousResolutionMemoryStore()
    run_id, spec, _operation_id = await _seed_real_model_in_flight(store)
    model_calls: list[int] = []
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]

        with pytest.raises(AgentSDKError) as ambiguous:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": "operator"},
                evidence={"disposition": "not_executed"},
            )
        assert ambiguous.value.code is ErrorCode.CONFLICT
        assert ambiguous.value.message == "recovery state conflict"
        assert ambiguous.value.retryable is True
        _assert_secret_free(ambiguous.value, "partial-resolution-store-secret")
        before = await store.latest_cursor()
        events_before = await store.read_events(after_cursor=0)
        assert sum(
            item.event.type == "reconciliation.resolved" for item in events_before
        ) == 1

        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": "operator"},
                evidence={"disposition": "not_executed"},
            )

        assert caught.value.code is ErrorCode.CONFLICT
        assert caught.value.message == "recovery state conflict"
        assert caught.value.retryable is True
        assert await store.latest_cursor() == before
        events_after = await store.read_events(after_cursor=0)
        assert sum(
            item.event.type == "reconciliation.resolved" for item in events_after
        ) == 1
        assert model_calls == []
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_resolution_retains_closing_session_until_explicit_recovery(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "closing-resolution.sqlite3")
    )
    run_id, spec, _operation_id = await _seed_real_model_in_flight(store)
    model_calls: list[int] = []
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion(model_calls),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await waiting.result()
        run = await sdk.runs.get(run_id)
        request = (await sdk.recovery.pending_requests(run_id))[0]

        closing = await sdk.sessions.close(run.session_id)
        assert closing.status is SessionStatus.CLOSING
        with pytest.raises(AgentSDKError) as busy:
            await sdk.sessions.delete(run.session_id)
        assert busy.value.code is ErrorCode.CONFLICT
        assert busy.value.message == "session has active work"

        await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )
        retained = await sdk.sessions.get(run.session_id)
        assert retained.status is SessionStatus.CLOSING
        assert retained.active_run_ids == (run_id,)
        assert model_calls == []

        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert result.output_text == "done"
        assert model_calls == [1]
        assert (await sdk.sessions.get(run.session_id)).status is SessionStatus.CLOSED
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
async def test_closed_sdk_rejects_resolution_without_mutation() -> None:
    store = InMemoryStore()
    run_id, spec, _operation_id = await _seed_real_model_in_flight(store)
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    waiting = await sdk.recovery.recover_run(run_id)
    with pytest.raises(AgentSDKError):
        await waiting.result()
    request = (await sdk.recovery.pending_requests(run_id))[0]
    before = await store.latest_cursor()
    await sdk.close()

    with pytest.raises(AgentSDKError) as caught:
        await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )

    assert caught.value.code is ErrorCode.INVALID_STATE
    assert await store.latest_cursor() == before
    assert await store.get_reconciliation_request(request.request_id) == request


@pytest.mark.asyncio
async def test_changed_resolution_replay_is_constant_and_secret_free() -> None:
    store = InMemoryStore()
    run_id, spec, _operation_id = await _seed_real_model_in_flight(store)
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    sdk.agents.define(spec)
    actor_secret = "actor-replay-secret"
    evidence_secret = "evidence-replay-secret"
    try:
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]
        await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator"},
            evidence={"disposition": "not_executed"},
        )
        before = await store.latest_cursor()

        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": actor_secret},
                evidence={
                    "disposition": "not_executed",
                    "secret": evidence_secret,
                },
            )

        assert caught.value.code is ErrorCode.INVALID_STATE
        assert caught.value.message == "reconciliation decision is invalid"
        assert await store.latest_cursor() == before
        _assert_secret_free(caught.value, actor_secret, evidence_secret)
    finally:
        await sdk.close()
