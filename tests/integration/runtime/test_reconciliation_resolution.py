from __future__ import annotations

import asyncio
import inspect
import json
import traceback
from collections.abc import AsyncIterator
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
from agent_sdk.runtime.models import RunStatus
from agent_sdk.runtime.leases import LeaseManager
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    RunCheckpointPhase,
    _canonical_record_json,
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
                yield {
                    "choices": [
                        {
                            "delta": {
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
) -> tuple[str, AgentSpec, str, Any]:
    run_id, spec, operation_id = await _seed_real_model_in_flight(store)
    admitter = AgentSDK.for_test(
        store=store,
        acompletion=lambda **_: _final_completion([]),
        permission_default="allow",
    )
    admitter.agents.define(spec)
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

        for unsupported in (
            ReconciliationAction.CONFIRM_COMPLETED,
            ReconciliationAction.TERMINATE,
        ):
            with pytest.raises(AgentSDKError) as caught:
                await sdk.recovery.resolve(
                    request.request_id,
                    unsupported,
                    actor={"type": "operator"},
                    evidence={"unsupported": True},
                )
            assert caught.value.code is ErrorCode.INVALID_STATE
            assert caught.value.message == "reconciliation action is not supported"
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
            corrupted_operation = operation.model_copy(
                update={"request_fingerprint": "sha256:wrong-attempt-fingerprint"}
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
async def test_resolution_rejects_capability_drift_or_corrupt_durable_state(
    backend: str,
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
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                actor={"type": "operator"},
                evidence={"disposition": "not_executed"},
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
        expected_message = "reconciliation action is not supported"
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
