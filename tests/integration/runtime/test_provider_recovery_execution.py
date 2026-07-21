from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    PermissionDecision,
    PermissionRequest,
    ProviderRecoveryAdapter,
    ProviderRecoveryDisposition,
    ProviderRecoveryRequest,
    ProviderRecoveryResult,
    RunStatus,
    TokenUsage,
    ToolRetryPolicy,
)
from agent_sdk.events.models import EventEnvelope
from agent_sdk.models.litellm_gateway import ModelRequest, ToolCallCompleted
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import _model_request_fingerprint
from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
)
from agent_sdk.runtime.models import RunSnapshot, SessionSnapshot
from agent_sdk.runtime.leases import Lease, LeaseLostError
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
    RunCheckpoint,
    RunCheckpointPhase,
    _canonical_record_json,
    _checkpoint_from_json,
    _external_operation_from_json,
)
from agent_sdk.storage.base import (
    CommitBatch,
    ExternalOperationWrite,
    RunCheckpointWrite,
    RunProgressBatch,
    SnapshotWrite,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools.models import ToolContext, ToolSpec
from agent_sdk.tools.registry import ToolRegistry


_AdapterCallable = Callable[
    [ProviderRecoveryRequest], Awaitable[ProviderRecoveryResult]
]


def _sdk_traceback_locals(error: BaseException) -> tuple[dict[str, Any], ...]:
    frames: list[dict[str, Any]] = []
    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in filename:
            frames.append(dict(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return tuple(frames)


class _RecoveryAuditFaultStore(InMemoryStore):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.enabled = False
        self.audit_calls = 0
        self.lose_on_assert = False
        self.lease_removed = asyncio.Event()
        self.allow_owner_loss = asyncio.Event()

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        audit = any(
            event.type.startswith("model.recovery.") for event in batch.events
        )
        if not self.enabled or not audit:
            return await super().commit_run_progress(batch)
        self.audit_calls += 1
        if self.audit_calls == 1 and self.mode == "checkpoint_cas":
            serialized = self._run_checkpoints[batch.lease.run_id]
            checkpoint = _checkpoint_from_json(serialized)
            raced = checkpoint.model_copy(
                update={"checkpoint_version": checkpoint.checkpoint_version + 1}
            )
            self._run_checkpoints[batch.lease.run_id] = _canonical_record_json(raced)
        if self.audit_calls == 1 and self.mode == "operation_cas":
            operation_id = batch.checkpoint_precondition.operation_id
            assert operation_id is not None
            operation = _external_operation_from_json(
                self._external_operations[operation_id]
            )
            raced = operation.model_copy(update={"request_fingerprint": "0" * 64})
            self._external_operations[operation_id] = _canonical_record_json(raced)
        if self.audit_calls == 1 and self.mode == "event_cas":
            run_record = self._snapshots[("run", batch.lease.run_id)]
            await super().commit(
                CommitBatch(
                    events=(
                        EventEnvelope.new(
                            type="model.concurrent.race",
                            session_id=run_record.session_id,
                            run_id=batch.lease.run_id,
                            sequence=batch.events[0].sequence,
                            payload={"bounded": True},
                        ),
                    ),
                )
            )
        if self.mode == "session_delete":
            run_record = self._snapshots[("run", batch.lease.run_id)]
            self._snapshots.pop(("session", run_record.session_id), None)
        if self.mode == "precommit":
            raise RuntimeError("provider-recovery-precommit-secret")
        result = await super().commit_run_progress(batch)
        if self.mode in {"lease_loss", "lease_takeover"} and self.audit_calls == 1:
            self.lose_on_assert = True
        if self.mode == "ambiguous" and self.audit_calls == 1:
            raise RuntimeError("provider-recovery-ambiguous-secret")
        return result

    async def assert_current_lease(self, lease: Lease, *, now: datetime) -> None:
        if self.lose_on_assert:
            self.lose_on_assert = False
            self._leases.pop(lease.run_id, None)
            self.lease_removed.set()
            if self.mode == "lease_takeover":
                await self.allow_owner_loss.wait()
            raise LeaseLostError
        await super().assert_current_lease(lease, now=now)


class _ProviderLeaseAssertBarrier:
    def __init__(self, delegate: Any, *, target: int) -> None:
        self._delegate = delegate
        self._target = target
        self.calls = 0
        self.reached = asyncio.Event()
        self.release_gate = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def assert_current(self, lease: Lease, *, now: datetime) -> None:
        self.calls += 1
        if self.calls == self._target:
            self.reached.set()
            await self.release_gate.wait()
        await self._delegate.assert_current(lease, now=now)


async def _unused_acompletion(**kwargs: object) -> Any:
    raise AssertionError(f"provider recovery called LiteLLM: {sorted(kwargs)}")


async def _seed_model_in_flight(
    store: Any,
    *,
    metadata: dict[str, object] | None = None,
    fingerprint: str | None = None,
    tool_specs: tuple[ToolSpec, ...] = (),
    provider_identity: str | None = None,
) -> tuple[AgentSpec, str, str, ModelRequest]:
    spec = AgentSpec(name="agent", model="provider/model", model_params={"temperature": 0})
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    messages = ({"role": "user", "content": "recover me"},)
    tool_registry = ToolRegistry()

    async def unused_tool(_context: ToolContext, **_arguments: object) -> object:
        raise AssertionError("seed Tool handler must not run")

    for tool_spec in tool_specs:
        tool_registry.register(tool_spec, unused_tool)
    descriptor = ExecutionDescriptor.create(
        agent=spec,
        messages=messages,
        tools=tuple(
            ToolCapabilityDescriptor.from_spec(tool_spec)
            for tool_spec in tool_specs
        ),
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    )
    created = (
        await commands.start_run(
            session.session_id,
            agent_revision="agent:1",
            user_input="recover me",
            execution_descriptor=descriptor,
        )
    ).value
    running = created.model_copy(update={"status": RunStatus.RUNNING, "version": 2})
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.started",
                    session_id=session.session_id,
                    run_id=created.run_id,
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
    request = ModelRequest(
        model=spec.model,
        messages=messages,
        tools=tool_registry.schemas(),
        params={"temperature": 0},
    )
    operation_id = "op_model_original"
    now = datetime(2026, 7, 15, 1, tzinfo=UTC)
    lease = await store.acquire_lease(
        run_id=running.run_id,
        owner="crashed-owner",
        now=now,
        expires_at=now + timedelta(seconds=30),
    )
    operation = ModelCallOperation(
        operation_id=operation_id,
        session_id=running.session_id,
        run_id=running.run_id,
        turn=0,
        request_fingerprint=fingerprint or _model_request_fingerprint(request),
        lease_generation=lease.generation,
        status=ExternalOperationStatus.STARTED,
        provider_identity=provider_identity or spec.model,
        recovery_metadata=metadata
        or {
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": True,
        },
    )
    checkpoint = RunCheckpoint(
        run_id=running.run_id,
        session_id=running.session_id,
        checkpoint_version=1,
        turn=0,
        phase=RunCheckpointPhase.MODEL_IN_FLIGHT,
        operation_id=operation_id,
        messages=messages,
    )
    await store.commit_run_progress(
        RunProgressBatch(
            lease=lease,
            now=now,
            events=(
                EventEnvelope.new(
                    type="step.started",
                    session_id=running.session_id,
                    run_id=running.run_id,
                    sequence=3,
                    payload={},
                ),
                EventEnvelope.new(
                    type="model.call.started",
                    session_id=running.session_id,
                    run_id=running.run_id,
                    sequence=4,
                    payload={"model": spec.model},
                ),
            ),
            operation=ExternalOperationWrite(None, operation),
            checkpoint=RunCheckpointWrite(None, checkpoint),
        )
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
                    session_id=running.session_id,
                    run_id=running.run_id,
                    sequence=5,
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
    return spec, running.run_id, operation_id, request


async def _insert_provider_run_event(
    store: Any,
    run_id: str,
    *,
    anchor_type: str,
    after: bool,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    all_events = await store.read_events(after_cursor=0)
    anchor = next(
        stored
        for stored in all_events
        if stored.event.run_id == run_id and stored.event.type == anchor_type
    )
    cursor = anchor.cursor + (1 if after else 0)
    sequence = anchor.event.sequence + (1 if after else 0)
    inserted = EventEnvelope.new(
        type=event_type,
        session_id=anchor.event.session_id,
        run_id=run_id,
        sequence=sequence,
        payload=payload,
    )
    if isinstance(store, InMemoryStore):
        shifted = []
        for stored in store._events:
            event = stored.event
            if event.run_id == run_id and event.sequence >= sequence:
                event = event.model_copy(update={"sequence": event.sequence + 1})
            shifted.append(
                type(stored)(
                    stored.cursor + (stored.cursor >= cursor),
                    event,
                )
            )
        shifted.append(type(anchor)(cursor, inserted))
        store._events = sorted(shifted, key=lambda stored: stored.cursor)
        store._last_cursor += 1
        return
    assert isinstance(store, SQLiteStore)
    offset = (await store.latest_cursor()) + 10_000
    await store._connection.execute(
        "UPDATE events SET cursor = cursor + ? WHERE cursor >= ?",
        (offset, cursor),
    )
    await store._connection.execute(
        "UPDATE events SET cursor = cursor - ? + 1 WHERE cursor >= ?",
        (offset, cursor + offset),
    )
    await store._connection.execute(
        "UPDATE events SET sequence = sequence + ? WHERE run_id = ? AND sequence >= ?",
        (offset, run_id, sequence),
    )
    await store._connection.execute(
        "UPDATE events SET sequence = sequence - ? + 1 "
        "WHERE run_id = ? AND sequence >= ?",
        (offset, run_id, sequence + offset),
    )
    await store._connection.execute(
        """
        INSERT INTO events(
            cursor, event_id, session_id, run_id, sequence, type,
            schema_version, occurred_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cursor,
            inserted.event_id,
            inserted.session_id,
            inserted.run_id,
            inserted.sequence,
            inserted.type,
            inserted.schema_version,
            inserted.occurred_at.isoformat(),
            json.dumps(
                inserted.payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    await store._connection.execute(
        "UPDATE sqlite_sequence SET seq = (SELECT MAX(cursor) FROM events) "
        "WHERE name = 'events'"
    )
    await store._connection.commit()


async def _replace_provider_run_event_payload(
    store: Any,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    selected = next(
        stored
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run_id and stored.event.type == event_type
    )
    changed = selected.event.model_copy(update={"payload": payload})
    if isinstance(store, InMemoryStore):
        store._events = [
            type(stored)(stored.cursor, changed)
            if stored.event.event_id == selected.event.event_id
            else stored
            for stored in store._events
        ]
        return
    assert isinstance(store, SQLiteStore)
    await store._connection.execute(
        "UPDATE events SET payload_json = ? WHERE event_id = ?",
        (
            json.dumps(
                changed.payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            changed.event_id,
        ),
    )
    await store._connection.commit()


def _adapter(
    query: _AdapterCallable | None,
    resend: _AdapterCallable | None,
    *,
    version: str = "1",
) -> ProviderRecoveryAdapter:
    return ProviderRecoveryAdapter(
        provider_identity="provider/model",
        adapter_id="application.adapter",
        version=version,
        authoritative_status=query is not None,
        same_operation_id_resend=resend is not None,
        query_status=query,
        resend=resend,
    )


async def _sdk(
    store: Any,
    spec: AgentSpec,
    adapter: ProviderRecoveryAdapter,
    *,
    acompletion: Callable[..., Awaitable[Any]] = _unused_acompletion,
    provider_recovery_timeout_seconds: float = 30.0,
) -> AgentSDK:
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=acompletion,
        permission_default="allow",
        enable_builtin_tools=False,
        provider_recovery_timeout_seconds=provider_recovery_timeout_seconds,
    )
    sdk.agents.define(spec)
    sdk.recovery.register_adapter(adapter)
    return sdk


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("position", ["before_interrupt", "after_interrupt"])
async def test_duplicate_run_created_never_reaches_certified_provider_work(
    backend: str,
    position: str,
    tmp_path: Path,
) -> None:
    secret = f"duplicate-provider-created-{backend}-{position}"
    path = tmp_path / f"duplicate-provider-created-{backend}-{position}.sqlite3"
    store: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(path)
    )
    metadata = {
        "adapter_id": "application.adapter",
        "adapter_version": "1",
        "authoritative_status": True,
        "same_operation_id_resend": True,
    }
    spec, run_id, _operation_id, _request = await _seed_model_in_flight(
        store,
        metadata=metadata,
    )
    created = next(
        stored.event
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run_id and stored.event.type == "run.created"
    )
    payload = dict(created.payload)
    payload["agent_revision"] = secret
    await _insert_provider_run_event(
        store,
        run_id,
        anchor_type="run.interrupted",
        after=position == "after_interrupt",
        event_type="run.created",
        payload=payload,
    )

    permission_calls = 0
    handler_calls = 0
    mcp_calls = 0
    litellm_calls = 0
    query_calls = 0
    resend_calls = 0

    async def completion(**_: object) -> Any:
        nonlocal litellm_calls
        litellm_calls += 1
        raise AssertionError("duplicate creation reached LiteLLM")

    async def query(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="must-not-complete",
            usage=TokenUsage(total_tokens=1),
        )

    async def resend(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal resend_calls
        resend_calls += 1
        raise AssertionError("duplicate creation reached provider resend")

    sdk = await _sdk(
        store,
        spec,
        _adapter(query, resend),
        acompletion=completion,
    )
    handle = await sdk.recovery.recover_run(run_id)
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert permission_calls == 0
        assert handler_calls == 0
        assert mcp_calls == 0
        assert litellm_calls == 0
        assert query_calls == 0
        assert resend_calls == 0
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "model_call_unknown_outcome"
        reconciliation = [
            stored.event.model_dump(mode="json")
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type == "reconciliation.requested"
        ]
        assert len(reconciliation) == 1
        assert secret not in repr(reconciliation)
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("corruption", "event_type", "payload", "after_interrupt"),
    [
        ("unknown", "application.lifecycle.note", {"secret": "unknown"}, False),
        ("run_started", "run.started", {"status": "running"}, False),
        (
            "model_started",
            "model.call.started",
            {"model": "provider/model"},
            False,
        ),
        (
            "run_interrupted",
            "run.interrupted",
            {"status": "interrupted"},
            True,
        ),
        (
            "malformed_audit",
            "model.recovery.query.started",
            {"secret": "malformed-audit"},
            True,
        ),
    ],
)
async def test_unknown_or_duplicate_provider_lifecycle_event_is_not_certified(
    corruption: str,
    event_type: str,
    payload: dict[str, Any],
    after_interrupt: bool,
) -> None:
    store = InMemoryStore()
    spec, run_id, _operation_id, _request = await _seed_model_in_flight(store)
    await _insert_provider_run_event(
        store,
        run_id,
        anchor_type="run.interrupted",
        after=after_interrupt,
        event_type=event_type,
        payload=payload,
    )

    litellm_calls = 0
    query_calls = 0
    resend_calls = 0

    async def completion(**_: object) -> Any:
        nonlocal litellm_calls
        litellm_calls += 1
        raise AssertionError(f"{corruption} reached LiteLLM")

    async def query(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="must-not-complete",
            usage=TokenUsage(total_tokens=1),
        )

    async def resend(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal resend_calls
        resend_calls += 1
        raise AssertionError(f"{corruption} reached provider resend")

    sdk = await _sdk(
        store,
        spec,
        _adapter(query, resend),
        acompletion=completion,
    )
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert (litellm_calls, query_calls, resend_calls) == (0, 0, 0)
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "model_call_unknown_outcome"
        reconciliation = [
            stored.event.model_dump(mode="json")
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
            and stored.event.type == "reconciliation.requested"
        ]
        assert len(reconciliation) == 1
        assert "secret" not in repr(reconciliation)
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_provider_recovery_audit_before_initial_interrupt_is_not_certified(
    backend: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"provider-audit-before-interrupt-{backend}.sqlite3"
    store: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(path)
    )
    spec, run_id, operation_id, _request = await _seed_model_in_flight(store)
    await _insert_provider_run_event(
        store,
        run_id,
        anchor_type="run.interrupted",
        after=False,
        event_type="model.recovery.query.started",
        payload={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "operation_id": operation_id,
            "action": "query",
        },
    )

    litellm_calls = 0
    query_calls = 0
    resend_calls = 0

    async def completion(**_: object) -> Any:
        nonlocal litellm_calls
        litellm_calls += 1
        raise AssertionError("pre-interrupt audit reached LiteLLM")

    async def query(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="must-not-complete",
            usage=TokenUsage(total_tokens=1),
        )

    async def resend(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal resend_calls
        resend_calls += 1
        raise AssertionError("pre-interrupt audit reached provider resend")

    sdk = await _sdk(
        store,
        spec,
        _adapter(query, resend),
        acompletion=completion,
    )
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert (litellm_calls, query_calls, resend_calls) == (0, 0, 0)
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "model_call_unknown_outcome"
        assert sum(
            stored.event.type == "reconciliation.requested"
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ) == 1
    finally:
        await sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


_RECOVERY_PERMISSION_REQUEST = {
    "request": {"sha256": "1" * 64},
    "tool": {"sha256": "2" * 64},
}
_RECOVERY_PERMISSION_RESOLUTION = {
    **_RECOVERY_PERMISSION_REQUEST,
    "allowed": True,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "usage_before_interrupt",
        "permission_pair_before_interrupt",
        "permission_pair_between_audit_and_recovery",
        "audit_after_recovery_started",
        "audit_between_permission_states",
        "wrong_operation_after_interrupt",
        "resend_before_interrupt",
        "recovery_started_before_interrupt",
        "completed_before_interrupt",
        "failed_before_interrupt",
        "authorized_before_interrupt",
    ],
)
async def test_provider_known_lifecycle_token_in_unreachable_position_is_not_certified(
    corruption: str,
) -> None:
    store = InMemoryStore()
    spec, run_id, operation_id, _request = await _seed_model_in_flight(store)
    audit = (
        "model.recovery.query.started",
        {
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "operation_id": operation_id,
            "action": "query",
        },
    )

    if corruption == "usage_before_interrupt":
        await _insert_provider_run_event(
            store,
            run_id,
            anchor_type="run.interrupted",
            after=False,
            event_type="model.usage.reported",
            payload={
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        )
    elif corruption == "permission_pair_before_interrupt":
        for event_type, payload in (
            ("permission.requested", _RECOVERY_PERMISSION_REQUEST),
            ("permission.resolved", _RECOVERY_PERMISSION_RESOLUTION),
        ):
            await _insert_provider_run_event(
                store,
                run_id,
                anchor_type="run.interrupted",
                after=False,
                event_type=event_type,
                payload=payload,
            )
    elif corruption in {
        "wrong_operation_after_interrupt",
        "resend_before_interrupt",
        "recovery_started_before_interrupt",
        "completed_before_interrupt",
        "failed_before_interrupt",
        "authorized_before_interrupt",
    }:
        if corruption == "wrong_operation_after_interrupt":
            event_type = "model.recovery.query.started"
            payload = {**audit[1], "operation_id": "op_model_wrong_turn"}
            after = True
        elif corruption == "resend_before_interrupt":
            event_type = "model.recovery.resend.started"
            payload = {**audit[1], "action": "resend"}
            after = False
        elif corruption == "recovery_started_before_interrupt":
            event_type = "run.recovery.started"
            payload = {"status": "running"}
            after = False
        elif corruption == "completed_before_interrupt":
            event_type = "model.call.completed"
            payload = {"finish_reason": "stop"}
            after = False
        elif corruption == "failed_before_interrupt":
            event_type = "model.call.failed"
            payload = {"code": "internal", "message": "bounded"}
            after = False
        else:
            event_type = "tool.call.authorized"
            payload = {"call_id": "call_impossible", "tool_name": "impossible"}
            after = False
        await _insert_provider_run_event(
            store,
            run_id,
            anchor_type="run.interrupted",
            after=after,
            event_type=event_type,
            payload=payload,
        )
    else:
        middle: tuple[tuple[str, dict[str, Any]], ...]
        if corruption == "permission_pair_between_audit_and_recovery":
            middle = (
                audit,
                ("permission.requested", _RECOVERY_PERMISSION_REQUEST),
                ("permission.resolved", _RECOVERY_PERMISSION_RESOLUTION),
                ("run.recovery.started", {"status": "running"}),
                ("run.interrupted", {"status": "interrupted"}),
            )
        elif corruption == "audit_after_recovery_started":
            middle = (
                audit,
                ("run.recovery.started", {"status": "running"}),
                audit,
                ("run.interrupted", {"status": "interrupted"}),
            )
        else:
            middle = (
                audit,
                ("run.recovery.started", {"status": "running"}),
                ("permission.requested", _RECOVERY_PERMISSION_REQUEST),
                audit,
                ("permission.resolved", _RECOVERY_PERMISSION_RESOLUTION),
                ("run.interrupted", {"status": "interrupted"}),
            )
        for event_type, payload in reversed(middle):
            await _insert_provider_run_event(
                store,
                run_id,
                anchor_type="run.interrupted",
                after=True,
                event_type=event_type,
                payload=payload,
            )

    query_calls = 0
    resend_calls = 0
    litellm_calls = 0

    async def completion(**_: object) -> Any:
        nonlocal litellm_calls
        litellm_calls += 1
        raise AssertionError(f"{corruption} reached LiteLLM")

    async def query(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="must-not-complete",
            usage=TokenUsage(total_tokens=1),
        )

    async def resend(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal resend_calls
        resend_calls += 1
        raise AssertionError(f"{corruption} reached provider resend")

    sdk = await _sdk(
        store,
        spec,
        _adapter(query, resend),
        acompletion=completion,
    )
    try:
        handle = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert (query_calls, resend_calls, litellm_calls) == (0, 0, 0)
        pending = await sdk.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].reason == "model_call_unknown_outcome"
        assert sum(
            stored.event.type == "reconciliation.requested"
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ) == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_authoritative_completed_text_finishes_same_operation_without_litellm() -> None:
    store = InMemoryStore()
    spec, run_id, operation_id, original_request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )
    observed: list[ProviderRecoveryRequest] = []

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        observed.append(request)
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="recovered",
            usage=TokenUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        )

    sdk = await _sdk(store, spec, _adapter(query, None))
    try:
        result = await (await sdk.recovery.recover_run(run_id)).result()
    finally:
        await sdk.close()

    assert result.output_text == "recovered"
    assert result.usage == TokenUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3)
    assert len(observed) == 1
    request = observed[0]
    assert request.run_id == run_id
    assert request.operation_id == operation_id
    assert request.request_fingerprint == _model_request_fingerprint(original_request)
    assert request.model_request == original_request
    events = [
        item.event
        for item in await store.read_events(after_cursor=0)
        if item.event.run_id == run_id
    ]
    assert [event.type for event in events].count("model.call.started") == 1
    assert [event.type for event in events].count("model.recovery.query.started") == 1
    assert [event.type for event in events].count("model.call.completed") == 1


@pytest.mark.asyncio
async def test_authoritative_failed_terminalizes_run_with_sanitized_failure() -> None:
    store = InMemoryStore()
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        del request
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.FAILED,
            error_code=ErrorCode.INTERNAL,
            retryable=False,
        )

    sdk = await _sdk(store, spec, _adapter(query, None))
    try:
        with pytest.raises(AgentSDKError) as failure:
            await (await sdk.recovery.recover_run(run_id)).result()
    finally:
        await sdk.close()

    assert failure.value.code is ErrorCode.INTERNAL
    assert failure.value.message == "model call failed"
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    snapshot = RunSnapshot.model_validate(await store.get_snapshot("run", run_id))
    session = SessionSnapshot.model_validate(
        await store.get_snapshot("session", snapshot.session_id)
    )
    checkpoint = await store.get_run_checkpoint(run_id)
    operation = await store.get_external_operation(operation_id)
    assert snapshot.status is RunStatus.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == ErrorCode.INTERNAL.value
    assert snapshot.error.message == "model call failed"
    assert snapshot.error.retryable is False
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.TERMINAL
    assert checkpoint.operation_id is None
    assert operation is not None
    assert operation.status is ExternalOperationStatus.FAILED
    assert operation.outcome == {
        "error": {"code": ErrorCode.INTERNAL.value, "message": "model call failed"}
    }
    assert run_id not in session.active_run_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_certified_resend_reuses_original_operation_id(direct: bool) -> None:
    store = InMemoryStore()
    metadata = {
        "adapter_id": "application.adapter",
        "adapter_version": "1",
        "authoritative_status": not direct,
        "same_operation_id_resend": True,
    }
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata=metadata,
    )
    actions: list[tuple[str, str]] = []

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        actions.append(("query", request.operation_id))
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.NOT_EXECUTED
        )

    async def resend(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        actions.append(("resend", request.operation_id))
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="resent",
            usage=TokenUsage(total_tokens=1),
        )

    sdk = await _sdk(store, spec, _adapter(None if direct else query, resend))
    try:
        result = await (await sdk.recovery.recover_run(run_id)).result()
    finally:
        await sdk.close()

    assert result.output_text == "resent"
    assert actions == (
        [("resend", operation_id)]
        if direct
        else [("query", operation_id), ("resend", operation_id)]
    )


@pytest.mark.asyncio
async def test_unknown_result_creates_one_reconciliation_without_resend() -> None:
    store = InMemoryStore()
    spec, run_id, operation_id, _request = await _seed_model_in_flight(store)
    calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        assert request.operation_id == operation_id
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.UNKNOWN
        )

    async def resend(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        raise AssertionError(f"unexpected resend for {request.operation_id}")

    sdk = await _sdk(store, spec, _adapter(query, resend))
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await sdk.recovery.recover_run(run_id)).result()
        pending = await sdk.recovery.pending_requests(run_id)
    finally:
        await sdk.close()

    assert calls == 1
    assert len(pending) == 1
    assert pending[0].operation_id == operation_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        "metadata",
        "fingerprint",
        "legacy_false",
        "malformed_metadata",
        "unknown_provider",
    ],
)
async def test_certification_or_fingerprint_mismatch_never_calls_adapter(
    mismatch: str,
) -> None:
    store = InMemoryStore()
    kwargs: dict[str, object] = {}
    if mismatch == "metadata":
        kwargs["metadata"] = {
            "adapter_id": "application.adapter",
            "adapter_version": "old",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        }
    elif mismatch == "fingerprint":
        kwargs["fingerprint"] = "0" * 64
    elif mismatch == "legacy_false":
        kwargs["metadata"] = {
            "authoritative_status": False,
            "same_operation_id_resend": False,
        }
    elif mismatch == "malformed_metadata":
        kwargs["metadata"] = {
            "adapter_id": "application.adapter",
            "adapter_version": 1,
            "authoritative_status": True,
            "same_operation_id_resend": False,
        }
    else:
        kwargs["provider_identity"] = "provider/unknown"
    spec, run_id, _operation_id, _request = await _seed_model_in_flight(
        store,
        **kwargs,  # type: ignore[arg-type]
    )

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        raise AssertionError(f"unexpected adapter call for {request.operation_id}")

    sdk = await _sdk(store, spec, _adapter(query, None))
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await sdk.recovery.recover_run(run_id)).result()
        pending = await sdk.recovery.pending_requests(run_id)
    finally:
        await sdk.close()

    assert len(pending) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "pending",
        "not_executed_without_resend",
        "invalid_result",
        "query_failure",
        "query_timeout",
        "resend_failure",
    ],
)
async def test_nonterminal_invalid_or_failed_adapter_result_reconciles_once(
    mode: str,
) -> None:
    store = InMemoryStore()
    resend_enabled = mode == "resend_failure"
    metadata = {
        "adapter_id": "application.adapter",
        "adapter_version": "1",
        "authoritative_status": True,
        "same_operation_id_resend": resend_enabled,
    }
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata=metadata,
    )
    query_calls = 0
    resend_calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        assert request.operation_id == operation_id
        if mode == "pending":
            return ProviderRecoveryResult(
                disposition=ProviderRecoveryDisposition.PENDING
            )
        if mode in {"not_executed_without_resend", "resend_failure"}:
            return ProviderRecoveryResult(
                disposition=ProviderRecoveryDisposition.NOT_EXECUTED
            )
        if mode == "invalid_result":
            return object()  # type: ignore[return-value]
        if mode == "query_timeout":
            raise TimeoutError("provider-timeout-secret")
        raise RuntimeError("provider-query-secret")

    async def resend(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal resend_calls
        resend_calls += 1
        assert request.operation_id == operation_id
        raise RuntimeError("provider-resend-secret")

    sdk = await _sdk(
        store,
        spec,
        _adapter(query, resend if resend_enabled else None),
    )
    try:
        with pytest.raises(AgentSDKError) as caught:
            await (await sdk.recovery.recover_run(run_id)).result()
        pending = await sdk.recovery.pending_requests(run_id)
        events = [
            item.event
            for item in await store.read_events(after_cursor=0)
            if item.event.run_id == run_id
        ]
    finally:
        await sdk.close()

    assert caught.value.to_dict() == {
        "code": "conflict",
        "message": "recovery required",
        "retryable": True,
    }
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert query_calls == 1
    assert resend_calls == (1 if resend_enabled else 0)
    assert len(pending) == 1
    assert pending[0].operation_id == operation_id
    assert [event.type for event in events].count("reconciliation.requested") == 1
    serialized = "\n".join(
        str(event.model_dump(mode="json")) for event in events
    ) + str(caught.value.to_dict())
    assert "provider-query-secret" not in serialized
    assert "provider-timeout-secret" not in serialized
    assert "provider-resend-secret" not in serialized


@pytest.mark.asyncio
async def test_constructed_invalid_exact_result_is_sanitized_and_reconciled() -> None:
    secret = "constructed-provider-result-secret-7ac1"
    store = InMemoryStore()
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": True,
        },
    )
    model_calls = 0
    query_calls = 0
    resend_calls = 0

    async def acompletion(**kwargs: Any) -> Any:
        nonlocal model_calls
        model_calls += 1
        raise AssertionError(f"unexpected LiteLLM call: {kwargs}")

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        assert request.operation_id == operation_id
        return ProviderRecoveryResult.model_construct(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text=secret,
            usage=None,
        )

    async def resend(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal resend_calls
        resend_calls += 1
        raise AssertionError(f"unexpected resend: {request.operation_id}")

    sdk = await _sdk(
        store,
        spec,
        _adapter(query, resend),
        acompletion=acompletion,
    )
    handle = await sdk.recovery.recover_run(run_id)
    try:
        with pytest.raises(AgentSDKError) as caught:
            await handle.result()
        pending = await sdk.recovery.pending_requests(run_id)
        operation = await store.get_external_operation(operation_id)
        run = RunSnapshot.model_validate(await store.get_snapshot("run", run_id))
        recovery_events = [
            item.event
            for item in await store.read_events(after_cursor=0)
            if item.event.run_id == run_id
            and (
                item.event.type.startswith("model.recovery.")
                or item.event.type == "reconciliation.requested"
            )
        ]
        task = handle._task
        assert task is not None
        assert task.done()
        task_error = task.exception()
        assert isinstance(task_error, AgentSDKError)

        expected_error = {
            "code": "conflict",
            "message": "recovery required",
            "retryable": True,
        }
        assert caught.value.to_dict() == expected_error
        assert task_error.to_dict() == expected_error
        for error in (caught.value, task_error):
            frames = _sdk_traceback_locals(error)
            assert frames
            assert all(secret not in repr(frame) for frame in frames)
            assert secret not in repr(error.to_dict())
            assert error.__cause__ is None
            assert error.__context__ is None

        assert model_calls == 0
        assert query_calls == 1
        assert resend_calls == 0
        assert len(pending) == 1
        assert pending[0].operation_id == operation_id
        assert pending[0].details == {
            "action": "query",
            "disposition": "invalid",
            "error_category": "invalid_result",
        }
        assert operation is not None
        assert operation.status is ExternalOperationStatus.STARTED
        assert operation.lease_generation == 2
        assert run.status is RunStatus.WAITING_RECONCILIATION
        assert [event.type for event in recovery_events] == [
            "model.recovery.query.started",
            "reconciliation.requested",
        ]
        assert set(recovery_events[0].payload) == {
            "adapter_id",
            "adapter_version",
            "operation_id",
            "action",
        }
        assert set(recovery_events[1].payload) == {
            "request_id",
            "operation_id",
            "reason",
        }
        assert secret not in repr(
            [event.model_dump(mode="json") for event in recovery_events]
        )
    finally:
        await sdk.close()

    assert sdk._active_tasks == set()
    assert not any(
        task is not asyncio.current_task()
        and (
            "_coordinate_provider_recovery" in repr(task.get_coro())
            or "constructed_invalid_exact_result" in repr(task.get_coro())
        )
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_injected_real_timeout_cancels_adapter_task_and_reconciles() -> None:
    store = InMemoryStore()
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )
    cancelled = asyncio.Event()

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        assert request.operation_id == operation_id
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    sdk = await _sdk(
        store,
        spec,
        _adapter(query, None),
        provider_recovery_timeout_seconds=0.0,
    )
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await sdk.recovery.recover_run(run_id)).result()
        pending = await sdk.recovery.pending_requests(run_id)
    finally:
        await sdk.close()

    assert len(pending) == 1
    assert pending[0].details["error_category"] == "timeout"
    assert sdk._active_tasks == set()
    assert cancelled.is_set() or not any(
        task is not asyncio.current_task()
        and "query" in repr(task.get_coro())
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_same_sdk_twenty_callers_share_one_authoritative_query() -> None:
    store = InMemoryStore()
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )
    calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        assert request.operation_id == operation_id
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="once",
            usage=TokenUsage(total_tokens=1),
        )

    sdk = await _sdk(store, spec, _adapter(query, None))
    try:
        handles = await asyncio.gather(
            *(sdk.recovery.recover_run(run_id) for _ in range(20))
        )
        results = await asyncio.gather(*(handle.result() for handle in handles))
    finally:
        await sdk.close()

    assert calls == 1
    assert {result.output_text for result in results} == {"once"}


@pytest.mark.asyncio
async def test_two_sdk_instances_share_one_authoritative_query() -> None:
    store = InMemoryStore()
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )
    entered = asyncio.Event()
    allow = asyncio.Event()
    calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        assert request.operation_id == operation_id
        entered.set()
        await allow.wait()
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="cross-sdk-once",
            usage=TokenUsage(total_tokens=1),
        )

    adapter = _adapter(query, None)
    first = await _sdk(store, spec, adapter)
    second = await _sdk(store, spec, adapter)
    try:
        first_result = asyncio.create_task(
            (await first.recovery.recover_run(run_id)).result()
        )
        await entered.wait()
        second_result = asyncio.create_task(
            (await second.recovery.recover_run(run_id)).result()
        )
        await asyncio.sleep(0)
        allow.set()
        results = await asyncio.gather(first_result, second_result)
    finally:
        allow.set()
        await first.close()
        await second.close()

    assert calls == 1
    assert [result.output_text for result in results] == [
        "cross-sdk-once",
        "cross-sdk-once",
    ]


@pytest.mark.asyncio
async def test_twenty_callers_and_two_sdks_share_one_same_id_resend() -> None:
    store = InMemoryStore()
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": False,
            "same_operation_id_resend": True,
        },
    )
    entered = asyncio.Event()
    allow = asyncio.Event()
    calls = 0
    side_effect_operation_ids: set[str] = set()

    async def resend(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        side_effect_operation_ids.add(request.operation_id)
        entered.set()
        await allow.wait()
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="resent-once",
            usage=TokenUsage(total_tokens=1),
        )

    adapter = _adapter(None, resend)
    first = await _sdk(store, spec, adapter)
    second = await _sdk(store, spec, adapter)
    try:
        first_handles = await asyncio.gather(
            *(first.recovery.recover_run(run_id) for _ in range(20))
        )
        first_results = [
            asyncio.create_task(handle.result()) for handle in first_handles
        ]
        await entered.wait()
        second_result = asyncio.create_task(
            (await second.recovery.recover_run(run_id)).result()
        )
        await asyncio.sleep(0)
        allow.set()
        results = await asyncio.gather(*first_results, second_result)
    finally:
        allow.set()
        await first.close()
        await second.close()

    assert calls == 1
    assert side_effect_operation_ids == {operation_id}
    assert {result.output_text for result in results} == {"resent-once"}


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["completed", "failed"])
async def test_sqlite_close_reopen_applies_authoritative_terminal_outcome(
    tmp_path: Path,
    disposition: str,
) -> None:
    path = tmp_path / f"provider-recovery-{disposition}.db"
    initial = await SQLiteStore.open(path)
    metadata = {
        "adapter_id": "application.adapter",
        "adapter_version": "1",
        "authoritative_status": True,
        "same_operation_id_resend": False,
    }
    try:
        spec, run_id, operation_id, _request = await _seed_model_in_flight(
            initial,
            metadata=metadata,
        )
    finally:
        await initial.close()
    calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        assert request.operation_id == operation_id
        if disposition == "failed":
            return ProviderRecoveryResult(
                disposition=ProviderRecoveryDisposition.FAILED,
                error_code=ErrorCode.INTERNAL,
                retryable=False,
            )
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="sqlite-recovered",
            usage=TokenUsage(total_tokens=2),
        )

    sdk = AgentSDK.for_test(
        database_path=path,
        acompletion=_unused_acompletion,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    sdk.agents.define(spec)
    sdk.recovery.register_adapter(_adapter(query, None))
    try:
        handle = await sdk.recovery.recover_run(run_id)
        if disposition == "failed":
            with pytest.raises(AgentSDKError, match="model call failed"):
                await handle.result()
        else:
            result = await handle.result()
            assert result.output_text == "sqlite-recovered"
    finally:
        await sdk.close()

    verified = await SQLiteStore.open(path)
    try:
        snapshot = RunSnapshot.model_validate(
            await verified.get_snapshot("run", run_id)
        )
        session = SessionSnapshot.model_validate(
            await verified.get_snapshot("session", snapshot.session_id)
        )
        checkpoint = await verified.get_run_checkpoint(run_id)
        operation = await verified.get_external_operation(operation_id)
        events = [
            item.event.type
            for item in await verified.read_events(after_cursor=0)
            if item.event.run_id == run_id
        ]
    finally:
        await verified.close()
    assert calls == 1
    assert snapshot.status is (
        RunStatus.FAILED if disposition == "failed" else RunStatus.COMPLETED
    )
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.TERMINAL
    assert checkpoint.operation_id is None
    assert operation is not None
    assert operation.status is (
        ExternalOperationStatus.FAILED
        if disposition == "failed"
        else ExternalOperationStatus.COMPLETED
    )
    assert run_id not in session.active_run_ids
    if disposition == "failed":
        assert snapshot.error is not None
        assert snapshot.error.code == ErrorCode.INTERNAL.value
        assert snapshot.error.message == "model call failed"
        assert snapshot.error.retryable is False
        assert operation.outcome == {
            "error": {"code": ErrorCode.INTERNAL.value, "message": "model call failed"}
        }
    assert events.count("model.recovery.query.started") == 1
    assert events.count(
        "model.call.failed" if disposition == "failed" else "model.call.completed"
    ) == 1


@pytest.mark.asyncio
async def test_recovered_tool_call_executes_tool_then_uses_litellm_for_next_turn() -> None:
    store = InMemoryStore()
    tool_spec = ToolSpec(
        name="lookup",
        description="Lookup a value",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
        tool_specs=(tool_spec,),
    )
    query_calls = 0
    tool_calls = 0
    litellm_calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        assert request.operation_id == operation_id
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="tool_calls",
            text="tool:",
            tool_call=ToolCallCompleted(
                index=0,
                call_id="call_1",
                name="lookup",
                arguments_json='{"value":7}',
            ),
            usage=TokenUsage(total_tokens=1),
        )

    async def handler(_context: ToolContext, *, value: int) -> object:
        nonlocal tool_calls
        tool_calls += 1
        return {"value": value}

    async def completion(**kwargs: object) -> Any:
        nonlocal litellm_calls
        litellm_calls += 1
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        assert messages[-1]["role"] == "tool"

        async def chunks() -> Any:
            yield {
                "choices": [
                    {"delta": {"content": "done"}, "finish_reason": "stop"}
                ]
            }

        return chunks()

    sdk = await _sdk(
        store,
        spec,
        _adapter(query, None),
        acompletion=completion,
    )
    sdk.tools.register(tool_spec, handler)
    try:
        result = await (await sdk.recovery.recover_run(run_id)).result()
    finally:
        await sdk.close()

    assert result.output_text == "tool:done"
    assert query_calls == 1
    assert tool_calls == 1
    assert litellm_calls == 1
    assert len(result.tool_results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_provider_recovery_to_interrupted_tool_can_cross_kind_recover(
    backend: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"provider-to-tool-cycle-{backend}.sqlite3"
    store: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(path)
    )
    tool_spec = ToolSpec(
        name="lookup",
        description="Lookup a value",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
    )
    spec, run_id, model_operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
        tool_specs=(tool_spec,),
    )
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    query_calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        assert request.operation_id == model_operation_id
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="tool_calls",
            text="provider-recovered:",
            tool_call=ToolCallCompleted(
                index=0,
                call_id="call_cross_kind",
                name="lookup",
                arguments_json='{"value":7}',
            ),
            usage=TokenUsage(total_tokens=2),
        )

    async def interrupted_handler(_context: ToolContext, *, value: int) -> object:
        del value
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    async def unexpected_completion(**_: object) -> Any:
        raise AssertionError("following model must not start before Tool recovery")

    first = await _sdk(
        store,
        spec,
        _adapter(query, None),
        acompletion=unexpected_completion,
    )
    first.tools.register(tool_spec, interrupted_handler)
    first_handle = await first.recovery.recover_run(run_id)
    started_wait = asyncio.create_task(handler_started.wait())
    done, _pending = await asyncio.wait(
        {started_wait, first_handle._task},  # type: ignore[attr-defined]
        timeout=1,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if started_wait not in done:
        started_wait.cancel()
        await asyncio.gather(started_wait, return_exceptions=True)
        await first_handle.result()
    await first.close()
    await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
    with pytest.raises(AgentSDKError):
        await first_handle.result()

    scanner = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        enable_builtin_tools=False,
    )
    try:
        await scanner.recovery.scan()
        assert (await scanner.runs.get(run_id)).status is RunStatus.INTERRUPTED
    finally:
        await scanner.close()

    handler_calls: list[int] = []
    model_calls = 0
    final_model_started = asyncio.Event()
    release_final_model = asyncio.Event()

    async def recovered_handler(_context: ToolContext, *, value: int) -> object:
        handler_calls.append(value)
        return {"value": value + 1}

    async def final_completion(**_: object) -> Any:
        nonlocal model_calls
        model_calls += 1
        final_model_started.set()
        await release_final_model.wait()

        async def chunks() -> Any:
            yield {
                "choices": [
                    {"delta": {"content": "done"}, "finish_reason": "stop"}
                ]
            }

        return chunks()

    second = AgentSDK.for_test(
        store=store,
        acompletion=final_completion,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    second.agents.define(spec)
    second.tools.register(tool_spec, recovered_handler)
    try:
        second_handle = await second.recovery.recover_run(run_id)
        await asyncio.wait_for(final_model_started.wait(), timeout=1)
        in_progress = await store.list_external_operations(run_id)
        assert tuple(
            (item.operation_kind.value, item.turn, item.status.value)
            for item in in_progress
        ) == (
            ("model_call", 0, "completed"),
            ("tool_call", 0, "completed"),
            ("model_call", 1, "started"),
        )
        release_final_model.set()
        result = await second_handle.result()
        assert result.output_text == "provider-recovered:done"
        assert handler_calls == [7]
        assert model_calls == 1
        assert query_calls == 1
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        ]
        assert event_types.count("model.recovery.query.started") == 1
        assert event_types.count("tool.recovery.retry.started") == 1
        assert event_types.count("run.recovery.started") == 2
    finally:
        await second.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("tool_outcome", ["success", "failed"])
async def test_tool_recovery_to_interrupted_model_can_cross_kind_recover(
    backend: str,
    tool_outcome: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"tool-to-provider-cycle-{backend}-{tool_outcome}.sqlite3"
    store: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(path)
    )
    spec = AgentSpec(name="cross-kind-agent", model="provider/model")
    tool_spec = ToolSpec(
        name="lookup",
        description="Lookup a value",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
    )
    first_handler_started = asyncio.Event()
    first_handler_cancelled = asyncio.Event()

    async def initial_completion(**_: object) -> Any:
        async def chunks() -> Any:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_tool_first",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"value":3}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

        return chunks()

    async def first_handler(_context: ToolContext, *, value: int) -> object:
        del value
        first_handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_handler_cancelled.set()
            raise

    seed = AgentSDK.for_test(
        store=store,
        acompletion=initial_completion,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    seed.tools.register(tool_spec, first_handler)
    session = await seed.sessions.create(workspaces=[])
    seed_handle = await seed.runs.start(session.session_id, spec, "cross kind")
    await asyncio.wait_for(first_handler_started.wait(), timeout=1)
    seed_handle._task.cancel()  # type: ignore[attr-defined]
    with pytest.raises(AgentSDKError):
        await seed_handle.result()
    await asyncio.wait_for(first_handler_cancelled.wait(), timeout=1)
    await seed.close()

    scanner = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        enable_builtin_tools=False,
    )
    try:
        await scanner.recovery.scan()
        assert (await scanner.runs.get(seed_handle.run_id)).status is RunStatus.INTERRUPTED
    finally:
        await scanner.close()

    second_model_started = asyncio.Event()
    second_model_cancelled = asyncio.Event()
    tool_calls: list[int] = []
    query_calls = 0
    provider_requests: list[ProviderRecoveryRequest] = []
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        provider_requests.append(request)
        query_started.set()
        await release_query.wait()
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text=f"provider:{request.turn}",
            usage=TokenUsage(total_tokens=4),
        )

    async def recovered_tool(_context: ToolContext, *, value: int) -> object:
        tool_calls.append(value)
        if tool_outcome == "failed":
            raise RuntimeError("private recovered Tool failure")
        return {"value": value + 1}

    async def interrupted_model(**_: object) -> Any:
        second_model_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            second_model_cancelled.set()
            raise

    second = AgentSDK.for_test(
        store=store,
        acompletion=interrupted_model,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    second.agents.define(spec)
    second.tools.register(tool_spec, recovered_tool)
    second.recovery.register_adapter(_adapter(query, None))
    second_handle = await second.recovery.recover_run(seed_handle.run_id)
    await asyncio.wait_for(second_model_started.wait(), timeout=1)
    await second.close()
    await asyncio.wait_for(second_model_cancelled.wait(), timeout=1)
    with pytest.raises(AgentSDKError):
        await second_handle.result()

    scanner = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        enable_builtin_tools=False,
    )
    try:
        await scanner.recovery.scan()
        assert (await scanner.runs.get(seed_handle.run_id)).status is RunStatus.INTERRUPTED
    finally:
        await scanner.close()

    third = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    third.agents.define(spec)
    third.tools.register(tool_spec, recovered_tool)
    third.recovery.register_adapter(_adapter(query, None))
    try:
        third_handle = await third.recovery.recover_run(seed_handle.run_id)
        await asyncio.wait_for(query_started.wait(), timeout=1)
        in_progress = await store.list_external_operations(seed_handle.run_id)
        assert tuple(
            (item.operation_kind.value, item.turn, item.status.value)
            for item in in_progress
        ) == (
            ("model_call", 0, "completed"),
            (
                "tool_call",
                0,
                "completed" if tool_outcome == "success" else "failed",
            ),
            ("model_call", 1, "started"),
        )
        release_query.set()
        result = await third_handle.result()
        assert result.output_text == "provider:1"
        durable = await third.runs.get(seed_handle.run_id)
        assert durable.execution_descriptor is not None
        assert tuple(map(dict, durable.execution_descriptor.messages)) == (
            {"role": "user", "content": "cross kind"},
        )
        assert provider_requests[0].model_request.messages[0]["role"] == "system"
        assert tool_calls == [3]
        assert query_calls == 1
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == seed_handle.run_id
        ]
        assert event_types.count("tool.recovery.retry.started") == 1
        assert event_types.count("model.recovery.query.started") == 1
        assert event_types.count("run.recovery.started") == 2
    finally:
        await third.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "mutation",
    ["forbidden_extra", "malformed_arguments", "tool_field_mismatch"],
)
async def test_provider_historical_permission_request_is_strictly_reconstructed(
    backend: str,
    mutation: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"provider-permission-request-{backend}-{mutation}.sqlite3"
    store: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(path)
    )
    spec = AgentSpec(name="permission-history", model="provider/model")
    tool_spec = ToolSpec(
        name="lookup",
        description="Lookup a value",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        effects=("network",),
    )
    model_turn = 0
    current_model_started = asyncio.Event()
    current_model_cancelled = asyncio.Event()
    handler_calls = 0

    async def completion(**_: object) -> Any:
        nonlocal model_turn
        model_turn += 1
        if model_turn == 1:
            async def chunks() -> Any:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_permission_history",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"value":5}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }

            return chunks()
        current_model_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            current_model_cancelled.set()
            raise

    async def handler(_context: ToolContext, *, value: int) -> object:
        nonlocal handler_calls
        handler_calls += 1
        return {"value": value}

    async def unused_query(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        raise AssertionError("seed run must not invoke recovery adapter")

    seed = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="ask",
        enable_builtin_tools=False,
    )
    seed.tools.register(tool_spec, handler)
    seed.recovery.register_adapter(_adapter(unused_query, None))
    session = await seed.sessions.create(workspaces=[])
    seed_handle = await seed.runs.start(session.session_id, spec, "permission history")
    permission = await asyncio.wait_for(
        seed.permissions.next_request(seed_handle.run_id),
        timeout=1,
    )
    await seed.permissions.resolve(
        permission.request_id,
        PermissionDecision.allow_once(),
    )
    await asyncio.wait_for(current_model_started.wait(), timeout=1)
    seed_handle._task.cancel()  # type: ignore[attr-defined]
    with pytest.raises(AgentSDKError):
        await seed_handle.result()
    await asyncio.wait_for(current_model_cancelled.wait(), timeout=1)
    await seed.close()

    scanner = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        enable_builtin_tools=False,
    )
    try:
        await scanner.recovery.scan()
        assert (await scanner.runs.get(seed_handle.run_id)).status is RunStatus.INTERRUPTED
    finally:
        await scanner.close()

    events = [
        stored.event
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == seed_handle.run_id
    ]
    requested = next(event for event in events if event.type == "permission.requested")
    resolved = next(event for event in events if event.type == "permission.resolved")
    request_payload = dict(requested.payload["request"])
    if mutation == "forbidden_extra":
        request_payload["forbidden"] = f"forbidden-{backend}"
    elif mutation == "malformed_arguments":
        request_payload["arguments"] = ["not", "a", "mapping"]
    else:
        request_payload["tool_name"] = "other_tool"
    await _replace_provider_run_event_payload(
        store,
        seed_handle.run_id,
        "permission.requested",
        {"request": request_payload},
    )
    await _replace_provider_run_event_payload(
        store,
        seed_handle.run_id,
        "permission.resolved",
        {**resolved.payload, "request": request_payload},
    )

    query_calls = 0
    litellm_calls = 0

    async def query(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="must-not-complete",
            usage=TokenUsage(total_tokens=1),
        )

    async def unexpected_completion(**_: object) -> Any:
        nonlocal litellm_calls
        litellm_calls += 1
        raise AssertionError("malformed permission history reached LiteLLM")

    recovered = AgentSDK.for_test(
        store=store,
        acompletion=unexpected_completion,
        permission_default="ask",
        enable_builtin_tools=False,
    )
    recovered.agents.define(spec)
    recovered.tools.register(tool_spec, handler)
    recovered.recovery.register_adapter(_adapter(query, None))
    try:
        handle = await recovered.recovery.recover_run(seed_handle.run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await handle.result()
        assert query_calls == 0
        assert litellm_calls == 0
        assert handler_calls == 1
        pending = await recovered.recovery.pending_requests(seed_handle.run_id)
        assert len(pending) == 1
        assert pending[0].reason == "model_call_unknown_outcome"
        assert sum(
            event.type == "reconciliation.requested"
            for event in (
                stored.event
                for stored in await store.read_events(after_cursor=0)
                if stored.event.run_id == seed_handle.run_id
            )
        ) == 1
    finally:
        await recovered.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "scenario",
    [
        "allow",
        "deny",
        "direct_allow",
        "direct_allow_forged_permission",
        "direct_deny",
        "direct_deny_forged_permission",
        "handler_failed",
        "handler_invalid",
        "handler_timeout",
        "forged_value",
        "forged_status",
        "forged_content",
    ],
)
async def test_provider_historical_tool_result_is_authoritatively_reconstructed(
    backend: str,
    scenario: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"provider-permission-{backend}-{scenario}.sqlite3"
    store: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(path)
    )
    spec = AgentSpec(name="permission-positive", model="provider/model")
    tool_spec = ToolSpec(
        name="lookup",
        description="Lookup a value",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        effects=("network",),
        timeout_seconds=0.01 if scenario == "handler_timeout" else None,
    )
    model_turn = 0
    current_model_started = asyncio.Event()
    current_model_cancelled = asyncio.Event()
    handler_calls: list[int] = []
    recorded_default = (
        "allow"
        if scenario in {"direct_allow", "direct_allow_forged_permission"}
        else (
            "deny"
            if scenario in {"direct_deny", "direct_deny_forged_permission"}
            else "ask"
        )
    )

    async def completion(**_: object) -> Any:
        nonlocal model_turn
        model_turn += 1
        if model_turn == 1:

            async def chunks() -> Any:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": f"call_permission_{scenario}",
                                        "function": {
                                            "name": "lookup",
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
        current_model_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            current_model_cancelled.set()
            raise

    async def handler(_context: ToolContext, *, value: int) -> object:
        handler_calls.append(value)
        if scenario == "handler_failed":
            raise RuntimeError("private historical Tool failure")
        if scenario == "handler_invalid":
            return object()
        if scenario == "handler_timeout":
            await asyncio.Event().wait()
        return {"value": value}

    async def unused_query(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        raise AssertionError("seed run must not invoke recovery adapter")

    async def unused_resend(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        raise AssertionError("seed run must not invoke recovery resend")

    seed = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default=recorded_default,  # type: ignore[arg-type]
        enable_builtin_tools=False,
    )
    seed.tools.register(tool_spec, handler)
    seed.recovery.register_adapter(_adapter(unused_query, unused_resend))
    session = await seed.sessions.create(workspaces=[])
    seed_handle = await seed.runs.start(session.session_id, spec, "permission positive")
    if recorded_default == "ask":
        permission = await asyncio.wait_for(
            seed.permissions.next_request(seed_handle.run_id),
            timeout=1,
        )
        decision = (
            PermissionDecision.allow_once()
            if scenario != "deny"
            else PermissionDecision.deny("operator denied")
        )
        await seed.permissions.resolve(permission.request_id, decision)
    await asyncio.wait_for(current_model_started.wait(), timeout=1)
    seed_handle._task.cancel()  # type: ignore[attr-defined]
    with pytest.raises(AgentSDKError):
        await seed_handle.result()
    await asyncio.wait_for(current_model_cancelled.wait(), timeout=1)
    await seed.close()

    scanner = AgentSDK.for_test(
        store=store,
        acompletion=_unused_acompletion,
        enable_builtin_tools=False,
    )
    try:
        await scanner.recovery.scan()
        assert (await scanner.runs.get(seed_handle.run_id)).status is RunStatus.INTERRUPTED
    finally:
        await scanner.close()

    forged = scenario.startswith("forged_") or scenario in {
        "direct_allow_forged_permission",
        "direct_deny_forged_permission",
    }
    if scenario.startswith("forged_"):
        completed = next(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == seed_handle.run_id
            and stored.event.type == "tool.call.completed"
        )
        forged_payload = dict(completed.payload)
        if scenario == "forged_value":
            forged_payload["value"] = {"value": 10}
        elif scenario == "forged_status":
            forged_payload["status"] = "failed"
        else:
            forged_payload["content"] = '{"value":10}'
        await _replace_provider_run_event_payload(
            store,
            seed_handle.run_id,
            "tool.call.completed",
            forged_payload,
        )
    elif scenario in {
        "direct_allow_forged_permission",
        "direct_deny_forged_permission",
    }:
        forged_request = PermissionRequest(
            request_id="prm_forged_direct_deny",
            run_id=seed_handle.run_id,
            session_id=session.session_id,
            tool_name="lookup",
            arguments={"value": 9},
            effects=("network",),
        ).model_dump(mode="json")
        await _insert_provider_run_event(
            store,
            seed_handle.run_id,
            anchor_type="tool.call.proposed",
            after=True,
            event_type="permission.requested",
            payload={"request": forged_request},
        )
        await _insert_provider_run_event(
            store,
            seed_handle.run_id,
            anchor_type="permission.requested",
            after=True,
            event_type="permission.resolved",
            payload={
                "request": forged_request,
                "decision": (
                    PermissionDecision.allow_once()
                    if scenario == "direct_allow_forged_permission"
                    else PermissionDecision.deny()
                ).model_dump(mode="json"),
            },
        )

    query_calls = 0
    resend_calls = 0

    async def query(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text=f"recovered-{scenario}",
            usage=TokenUsage(total_tokens=1),
        )

    async def resend(_: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal resend_calls
        resend_calls += 1
        raise AssertionError("authoritative query adapter must not resend")

    async def unexpected_completion(**_: object) -> Any:
        raise AssertionError("certified Provider recovery must not call LiteLLM")

    recovered = AgentSDK.for_test(
        store=store,
        acompletion=unexpected_completion,
        permission_default=recorded_default,  # type: ignore[arg-type]
        enable_builtin_tools=False,
    )
    recovered.agents.define(spec)
    recovered.tools.register(tool_spec, handler)
    recovered.recovery.register_adapter(_adapter(query, resend))
    try:
        handle = await recovered.recovery.recover_run(seed_handle.run_id)
        if forged:
            with pytest.raises(AgentSDKError, match="recovery required"):
                await handle.result()
            assert query_calls == 0
            assert resend_calls == 0
            assert handler_calls == (
                [] if scenario == "direct_deny_forged_permission" else [9]
            )
            pending = await recovered.recovery.pending_requests(seed_handle.run_id)
            assert len(pending) == 1
            assert pending[0].reason == "model_call_unknown_outcome"
            run_events = [
                stored.event
                for stored in await store.read_events(after_cursor=0)
                if stored.event.run_id == seed_handle.run_id
            ]
            assert sum(event.type == "permission.requested" for event in run_events) == 1
            assert sum(
                event.type == "reconciliation.requested" for event in run_events
            ) == 1
        else:
            result = await handle.result()
            assert result.output_text == f"recovered-{scenario}"
            assert query_calls == 1
            assert resend_calls == 0
            assert handler_calls == (
                [] if scenario in {"deny", "direct_deny"} else [9]
            )
            assert not any(
                stored.event.type == "reconciliation.requested"
                for stored in await store.read_events(after_cursor=0)
                if stored.event.run_id == seed_handle.run_id
            )
    finally:
        await recovered.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["precommit", "checkpoint_cas", "operation_cas", "event_cas"],
)
async def test_recovery_audit_precommit_or_cas_failure_calls_no_adapter(
    mode: str,
) -> None:
    store = _RecoveryAuditFaultStore(mode)
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )
    store.enabled = True
    calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected adapter call for {request.operation_id}")

    sdk = await _sdk(store, spec, _adapter(query, None))
    try:
        with pytest.raises(AgentSDKError):
            await (await sdk.recovery.recover_run(run_id)).result()
    finally:
        await sdk.close()

    operation = await store.get_external_operation(operation_id)
    checkpoint = await store.get_run_checkpoint(run_id)
    events = [
        item.event.type
        for item in await store.read_events(after_cursor=0)
        if item.event.run_id == run_id
    ]
    assert calls == 0
    assert operation is not None
    assert operation.lease_generation == 1
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
    assert not any(event.startswith("model.recovery.") for event in events)
    assert await store.get_run_lease(run_id) is None


@pytest.mark.asyncio
async def test_recovery_audit_ambiguous_commit_replays_exactly_then_calls_once() -> None:
    store = _RecoveryAuditFaultStore("ambiguous")
    spec, run_id, _operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )
    store.enabled = True
    calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="ambiguous-safe",
            usage=TokenUsage(total_tokens=1),
        )

    sdk = await _sdk(store, spec, _adapter(query, None))
    try:
        result = await (await sdk.recovery.recover_run(run_id)).result()
    finally:
        await sdk.close()

    events = [
        item.event.type
        for item in await store.read_events(after_cursor=0)
        if item.event.run_id == run_id
    ]
    assert result.output_text == "ambiguous-safe"
    assert calls == 1
    assert store.audit_calls == 2
    assert events.count("model.recovery.query.started") == 1


@pytest.mark.asyncio
async def test_lease_loss_after_audit_start_calls_no_adapter_or_terminal_commit() -> None:
    store = _RecoveryAuditFaultStore("lease_loss")
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )
    store.enabled = True
    calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="must-not-commit",
            usage=TokenUsage(total_tokens=1),
        )

    sdk = await _sdk(store, spec, _adapter(query, None))
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await sdk.recovery.recover_run(run_id)).result()
    finally:
        await sdk.close()

    operation = await store.get_external_operation(operation_id)
    run = RunSnapshot.model_validate(await store.get_snapshot("run", run_id))
    events = [
        item.event.type
        for item in await store.read_events(after_cursor=0)
        if item.event.run_id == run_id
    ]
    assert calls == 0
    assert operation is not None
    assert operation.status is ExternalOperationStatus.STARTED
    assert operation.lease_generation == 2
    assert run.status is RunStatus.INTERRUPTED
    assert events.count("model.recovery.query.started") == 1
    assert "model.call.completed" not in events


@pytest.mark.asyncio
async def test_lease_takeover_owner_and_loser_converge_on_one_adapter_outcome() -> None:
    store = _RecoveryAuditFaultStore("lease_takeover")
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )
    store.enabled = True
    adapter_entered = asyncio.Event()
    allow_adapter = asyncio.Event()
    calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        assert request.operation_id == operation_id
        adapter_entered.set()
        await allow_adapter.wait()
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="takeover-once",
            usage=TokenUsage(total_tokens=1),
        )

    adapter = _adapter(query, None)
    first = await _sdk(store, spec, adapter)
    second = await _sdk(store, spec, adapter)
    try:
        first_result = asyncio.create_task(
            (await first.recovery.recover_run(run_id)).result()
        )
        await store.lease_removed.wait()
        second_result = asyncio.create_task(
            (await second.recovery.recover_run(run_id)).result()
        )
        await adapter_entered.wait()
        store.allow_owner_loss.set()
        allow_adapter.set()
        results = await asyncio.gather(first_result, second_result)
    finally:
        store.allow_owner_loss.set()
        allow_adapter.set()
        await first.close()
        await second.close()

    assert calls == 1
    assert [result.output_text for result in results] == [
        "takeover-once",
        "takeover-once",
    ]
    assert await store.get_run_lease(run_id) is None


@pytest.mark.asyncio
async def test_session_delete_race_rejects_audit_without_partial_refence() -> None:
    store = _RecoveryAuditFaultStore("session_delete")
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )
    run = RunSnapshot.model_validate(await store.get_snapshot("run", run_id))
    store.enabled = True
    calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected adapter call for {request.operation_id}")

    sdk = await _sdk(store, spec, _adapter(query, None))
    try:
        with pytest.raises(AgentSDKError):
            await (await sdk.recovery.recover_run(run_id)).result()
    finally:
        await sdk.close()

    events = [
        item.event.type
        for item in await store.read_events(after_cursor=0)
        if item.event.run_id == run_id
    ]
    serialized_operation = store._external_operations[operation_id]
    assert calls == 0
    assert await store.get_snapshot("session", run.session_id) is None
    assert '"lease_generation":1' in serialized_operation
    assert not any(event.startswith("model.recovery.") for event in events)


@pytest.mark.asyncio
async def test_double_caller_cancel_does_not_cancel_shared_recovery_task() -> None:
    store = InMemoryStore()
    spec, run_id, _operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )
    entered = asyncio.Event()
    allow = asyncio.Event()

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        del request
        entered.set()
        await allow.wait()
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="survived-caller-cancel",
            usage=TokenUsage(total_tokens=1),
        )

    sdk = await _sdk(store, spec, _adapter(query, None))
    try:
        handle = await sdk.recovery.recover_run(run_id)
        caller = asyncio.create_task(handle.result())
        await entered.wait()
        caller.cancel()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert len(sdk._active_tasks) == 1
        allow.set()
        result = await handle.result()
    finally:
        allow.set()
        await sdk.close()

    assert result.output_text == "survived-caller-cancel"
    assert sdk._active_tasks == set()
    assert await store.get_run_lease(run_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["query", "resend"])
async def test_sdk_close_cancels_adapter_and_leaves_same_operation_recoverable(
    action: str,
) -> None:
    store = InMemoryStore()
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": action == "query",
            "same_operation_id_resend": action == "resend",
        },
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    side_effect_operation_ids: set[str] = set()

    async def blocked_query(
        request: ProviderRecoveryRequest,
    ) -> ProviderRecoveryResult:
        assert request.operation_id == operation_id
        side_effect_operation_ids.add(request.operation_id)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    sdk = await _sdk(
        store,
        spec,
        _adapter(
            blocked_query if action == "query" else None,
            blocked_query if action == "resend" else None,
        ),
    )
    handle = await sdk.recovery.recover_run(run_id)
    result_task = asyncio.create_task(handle.result())
    await entered.wait()
    await sdk.close()
    await cancelled.wait()
    with pytest.raises(AgentSDKError):
        await result_task

    operation = await store.get_external_operation(operation_id)
    assert operation is not None
    assert operation.status is ExternalOperationStatus.STARTED
    assert operation.lease_generation == 2
    assert await store.get_run_lease(run_id) is None
    assert sdk._active_tasks == set()

    calls = 0

    async def retry_query(
        request: ProviderRecoveryRequest,
    ) -> ProviderRecoveryResult:
        nonlocal calls
        calls += 1
        assert request.operation_id == operation_id
        side_effect_operation_ids.add(request.operation_id)
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="retried-query",
            usage=TokenUsage(total_tokens=1),
        )

    reopened = await _sdk(
        store,
        spec,
        _adapter(
            retry_query if action == "query" else None,
            retry_query if action == "resend" else None,
        ),
    )
    try:
        result = await (await reopened.recovery.recover_run(run_id)).result()
    finally:
        await reopened.close()
    assert calls == 1
    assert side_effect_operation_ids == {operation_id}
    assert result.output_text == "retried-query"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize(
    ("callback_boundary", "registry_change"),
    [
        ("query", "unregister"),
        ("query", "same_metadata"),
        ("query", "version"),
        ("query", "adapter_id"),
        ("query", "certification"),
        ("resend", "same_metadata"),
    ],
)
async def test_provider_registry_change_at_final_callback_preflight_reconciles_once(
    backend: str,
    callback_boundary: str,
    registry_change: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"provider-final-preflight-{callback_boundary}-{registry_change}.db"
    store: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(path)
    )
    metadata = {
        "adapter_id": "application.adapter",
        "adapter_version": "1",
        "authoritative_status": True,
        "same_operation_id_resend": True,
    }
    spec, run_id, operation_id, _request = await _seed_model_in_flight(
        store,
        metadata=metadata,
    )
    query_calls = 0
    resend_calls = 0
    replacement_calls = 0

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal query_calls
        query_calls += 1
        assert request.operation_id == operation_id
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.NOT_EXECUTED
        )

    async def resend(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        nonlocal resend_calls
        resend_calls += 1
        raise AssertionError(f"stale resend reached provider: {request.operation_id}")

    async def replacement_callback(
        request: ProviderRecoveryRequest,
    ) -> ProviderRecoveryResult:
        nonlocal replacement_calls
        replacement_calls += 1
        raise AssertionError(
            f"replacement adapter reached provider: {request.operation_id}"
        )

    initial = _adapter(query, resend)
    owner = await _sdk(store, spec, initial)
    follower = await _sdk(store, spec, initial)
    service = owner.recovery._service  # type: ignore[attr-defined]
    barrier = _ProviderLeaseAssertBarrier(
        service._leases,
        target=1 if callback_boundary == "query" else 2,
    )
    service._leases = barrier
    owner_handle = await owner.recovery.recover_run(run_id)
    owner_result = asyncio.create_task(owner_handle.result())
    await asyncio.wait_for(barrier.reached.wait(), timeout=2)
    follower_handle = await follower.recovery.recover_run(run_id)
    follower_result = asyncio.create_task(follower_handle.result())
    await asyncio.sleep(0)

    registered = owner.recovery.get_adapter("provider/model")
    assert owner.recovery.unregister_adapter(
        "provider/model",
        expected=registered,
    )
    if registry_change != "unregister":
        replacement = ProviderRecoveryAdapter(
            provider_identity="provider/model",
            adapter_id=(
                "replacement.adapter"
                if registry_change == "adapter_id"
                else "application.adapter"
            ),
            version="2" if registry_change == "version" else "1",
            authoritative_status=registry_change != "certification",
            same_operation_id_resend=True,
            query_status=(
                None if registry_change == "certification" else replacement_callback
            ),
            resend=replacement_callback,
        )
        owner.recovery.register_adapter(replacement)
    barrier.release_gate.set()
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await owner_result
        with pytest.raises(AgentSDKError, match="recovery required"):
            await follower_result
        assert query_calls == (1 if callback_boundary == "resend" else 0)
        assert resend_calls == 0
        assert replacement_calls == 0
        pending = await owner.recovery.pending_requests(run_id)
        assert len(pending) == 1
        assert pending[0].operation_id == operation_id
        assert pending[0].reason == "recovery_state_invalid"
        events = tuple(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run_id
        )
        assert sum(event.type == "reconciliation.requested" for event in events) == 1
        assert sum(event.type == "model.recovery.query.started" for event in events) == 1
        assert sum(event.type == "model.recovery.resend.started" for event in events) == (
            1 if callback_boundary == "resend" else 0
        )
    finally:
        barrier.release_gate.set()
        await owner.close()
        await follower.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
async def test_adapter_exception_and_request_secret_are_absent_from_public_boundary() -> None:
    secret = "provider-recovery-secret-91f2"
    store = InMemoryStore()
    spec, run_id, _operation_id, _request = await _seed_model_in_flight(
        store,
        metadata={
            "adapter_id": "application.adapter",
            "adapter_version": "1",
            "authoritative_status": True,
            "same_operation_id_resend": False,
        },
    )

    async def query(request: ProviderRecoveryRequest) -> ProviderRecoveryResult:
        request.model_request.params["credential"] = secret
        invalid_result = {"secret": secret}
        raise RuntimeError(f"{secret}:{invalid_result}")

    sdk = await _sdk(store, spec, _adapter(query, None))
    try:
        with pytest.raises(AgentSDKError) as caught:
            await (await sdk.recovery.recover_run(run_id)).result()
        recovery_events = [
            item.event
            for item in await store.read_events(after_cursor=0)
            if item.event.run_id == run_id
            and (
                item.event.type.startswith("model.recovery.")
                or item.event.type == "reconciliation.requested"
            )
        ]
        frames = _sdk_traceback_locals(caught.value)
        assert frames
        assert all(secret not in repr(frame) for frame in frames)
        assert secret not in repr(caught.value.to_dict())
        assert secret not in repr(
            [event.model_dump(mode="json") for event in recovery_events]
        )
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
    finally:
        await sdk.close()

    assert sdk._active_tasks == set()
