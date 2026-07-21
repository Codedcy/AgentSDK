from __future__ import annotations

import asyncio
import traceback
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_sdk.models.litellm_gateway import (
    LiteLLMGateway,
    ModelEvent,
    ModelRequest,
)
from agent_sdk import AgentSDKError, ErrorCode, PermissionDecision
from agent_sdk.permissions.broker import InProcessPermissionBridge
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
)
from agent_sdk.runtime.models import AgentSpec, RunSnapshot, RunStatus
from agent_sdk.runtime.leases import Lease, LeaseLostError, LeaseManager
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    RecoveryStateConflictError,
    RunCheckpointPhase,
    ToolCallOperation,
)
from agent_sdk.runtime.models import SessionSnapshot
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    RunProgressBatch,
    SnapshotWrite,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools.models import (
    ToolContext,
    ToolResultStatus,
    ToolRetryPolicy,
    ToolSpec,
)
from agent_sdk.tools.registry import ToolRegistry


class _InitialBoundaryStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.progress_batches: list[RunProgressBatch] = []

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        result = await super().commit_run_progress(batch)
        self.progress_batches.append(batch)
        return result


class _RenewTrackingStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.renewed = asyncio.Event()
        self.current_lease: Lease | None = None

    async def acquire_lease(
        self, *, run_id: str, owner: str, now: datetime, expires_at: datetime
    ) -> Lease:
        self.current_lease = await super().acquire_lease(
            run_id=run_id, owner=owner, now=now, expires_at=expires_at
        )
        return self.current_lease

    async def renew_lease(self, lease: Lease, *, now: datetime, expires_at: datetime) -> Lease:
        self.current_lease = await super().renew_lease(lease, now=now, expires_at=expires_at)
        self.renewed.set()
        return self.current_lease


class _ManualTicker:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)
        self.sleeping = asyncio.Event()
        self._tick = asyncio.Event()

    async def sleep(self, _: float) -> None:
        self.sleeping.set()
        await self._tick.wait()
        self._tick.clear()
        self.sleeping.clear()

    def advance(self, amount: timedelta) -> None:
        self.now += amount
        self._tick.set()


class _RenewLossStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.renew_attempted = asyncio.Event()

    async def renew_lease(self, lease: Lease, *, now: datetime, expires_at: datetime) -> Lease:
        del lease, now, expires_at
        self.renew_attempted.set()
        raise LeaseLostError


class _BlockingFailingReleaseStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.release_entered = asyncio.Event()
        self.allow_release = asyncio.Event()
        self.release_finished = asyncio.Event()
        self.release_calls = 0

    async def release_lease(self, lease: Lease) -> None:
        self.release_calls += 1
        self.release_entered.set()
        await self.allow_release.wait()
        self.release_finished.set()
        del lease
        raise RuntimeError("late release failure")


class _RenewLossBlockingFailingReleaseStore(_BlockingFailingReleaseStore):
    def __init__(self) -> None:
        super().__init__()
        self.renew_attempted = asyncio.Event()

    async def renew_lease(self, lease: Lease, *, now: datetime, expires_at: datetime) -> Lease:
        del lease, now, expires_at
        self.renew_attempted.set()
        raise LeaseLostError


class _MissingModelCompletedGateway(LiteLLMGateway):
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        del request
        for event in ():
            yield event


class _AmbiguousModelStartStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.model_start_batch_ids: list[int] = []
        self.failed_once = False

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        is_model_start = any(event.type == "model.call.started" for event in batch.events)
        if is_model_start:
            self.model_start_batch_ids.append(id(batch))
        result = await super().commit_run_progress(batch)
        if is_model_start and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("ambiguous model start result")
        return result


class _ConcurrentCreatedReadStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.block_run_id: str | None = None
        self.readers = 0
        self.both_loaded = asyncio.Event()

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        snapshot = await super().get_snapshot(kind, entity_id)
        if kind == "run" and entity_id == self.block_run_id:
            self.readers += 1
            if self.readers == 2:
                self.both_loaded.set()
            await self.both_loaded.wait()
        return snapshot


class _BlockingModelStartStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.model_start_blocked = asyncio.Event()
        self.release_model_start = asyncio.Event()

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        if any(event.type == "model.call.started" for event in batch.events):
            self.model_start_blocked.set()
            await self.release_model_start.wait()
        return await super().commit_run_progress(batch)


class _RejectOrdinaryCommitStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.reject_ordinary = False
        self.progress_calls = 0

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if self.reject_ordinary:
            raise AssertionError("live Run progress used ordinary commit")
        return await super().commit(batch)

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        self.progress_calls += 1
        return await super().commit_run_progress(batch)


class _RejectToolOutcomeStore(InMemoryStore):
    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        if (
            batch.operation is not None
            and isinstance(batch.operation.updated, ToolCallOperation)
            and batch.operation.expected is not None
        ):
            raise RecoveryStateConflictError
        return await super().commit_run_progress(batch)


class _RejectTerminalStore(InMemoryStore):
    def __init__(self, *, cancel: bool) -> None:
        super().__init__()
        self.cancel = cancel
        self.terminal_attempts = 0

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        if any(event.type == "run.completed" for event in batch.events):
            self.terminal_attempts += 1
            if self.cancel:
                raise asyncio.CancelledError
            raise RuntimeError("terminal precommit failure")
        return await super().commit_run_progress(batch)


class _ConcurrentSessionTerminalStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.mutated = False

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        if not self.mutated and any(event.type == "run.completed" for event in batch.events):
            self.mutated = True
            session_write = next(
                snapshot for snapshot in batch.snapshots if snapshot.kind == "session"
            )
            current = SessionSnapshot.model_validate(
                await super().get_snapshot("session", session_write.entity_id)
            )
            changed = current.model_copy(update={"version": current.version + 1})
            await super().commit(
                CommitBatch(
                    events=(),
                    snapshots=(
                        SnapshotWrite(
                            "session",
                            changed.session_id,
                            changed.session_id,
                            changed.version,
                            changed.model_dump(mode="json"),
                        ),
                    ),
                )
            )
        return await super().commit_run_progress(batch)


@pytest.mark.asyncio
async def test_lease_and_initial_checkpoint_are_durable_before_provider_entry() -> None:
    store = _InitialBoundaryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    spec = AgentSpec(name="agent", model="fake/model")
    messages = ({"role": "user", "content": "hello"},)
    descriptor = ExecutionDescriptor.create(
        agent=spec,
        messages=messages,
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    )
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="hello",
        execution_descriptor=descriptor,
    )
    observed: dict[str, object] = {}

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        observed["run"] = RunSnapshot.model_validate(await store.get_snapshot("run", run.run_id))
        observed["checkpoint"] = await store.get_run_checkpoint(run.run_id)
        observed["operations"] = await store.list_unresolved_external_operations(run.run_id)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        return chunks()

    result = await RunEngine(
        store,
        LiteLLMGateway._for_test(provider),
        policy=PolicyEngine("allow"),
    ).execute(
        run.run_id,
        ModelRequest(model="fake/model", messages=messages),
    )

    assert result.output_text == "ok"
    assert observed["run"].status is RunStatus.RUNNING  # type: ignore[union-attr]
    checkpoint = observed["checkpoint"]
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
    operations = observed["operations"]
    assert len(operations) == 1  # type: ignore[arg-type]
    assert operations[0].operation_id == checkpoint.operation_id  # type: ignore[index,union-attr]
    model_operation = operations[0]  # type: ignore[index]
    assert model_operation.turn == 0
    assert model_operation.lease_generation == 1
    assert model_operation.provider_identity == "fake/model"
    assert len(model_operation.request_fingerprint) == 64
    assert dict(model_operation.recovery_metadata) == {
        "authoritative_status": False,
        "same_operation_id_resend": False,
    }
    assert [event.type for event in store.progress_batches[0].events] == ["run.started"]
    assert store.progress_batches[0].checkpoint is not None
    assert store.progress_batches[0].checkpoint.expected is None
    assert store.progress_batches[0].checkpoint.updated.phase is RunCheckpointPhase.READY_FOR_MODEL


@pytest.mark.asyncio
async def test_legacy_created_run_uses_exact_request_checkpoint_and_stays_legacy() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    messages = ({"role": "user", "content": "hello"},)
    observed_checkpoint = None

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal observed_checkpoint
        observed_checkpoint = await store.get_run_checkpoint(run.run_id)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
        run.run_id,
        ModelRequest(model="fake/model", messages=messages),
    )

    assert run.execution_compatibility == "legacy_unknown"
    assert run.execution_descriptor is None
    assert observed_checkpoint is not None
    assert observed_checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
    assert tuple(dict(message) for message in observed_checkpoint.messages) == messages


@pytest.mark.asyncio
async def test_sqlite_live_engine_persists_fenced_boundaries(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "live-progress.db")
    try:
        commands = RuntimeCommands(store)
        session = await commands.create_session(workspaces=[])
        run = await commands.start_run(
            session.session_id,
            agent_revision="legacy:1",
            user_input="hello",
        )
        observed_operation_id = ""

        async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
            nonlocal observed_operation_id
            checkpoint = await store.get_run_checkpoint(run.run_id)
            assert checkpoint is not None
            assert checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
            assert checkpoint.operation_id is not None
            observed_operation_id = checkpoint.operation_id

            async def chunks() -> AsyncIterator[dict[str, object]]:
                yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

            return chunks()

        result = await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "hello"},),
            ),
        )

        assert result.output_text == "ok"
        operation = await store.get_external_operation(observed_operation_id)
        assert operation is not None
        assert operation.status is ExternalOperationStatus.COMPLETED
        checkpoint = await store.get_run_checkpoint(run.run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.TERMINAL
        durable_run = RunSnapshot.model_validate(await store.get_snapshot("run", run.run_id))
        assert durable_run.status is RunStatus.COMPLETED
        durable_session = SessionSnapshot.model_validate(
            await store.get_snapshot("session", session.session_id)
        )
        assert run.run_id not in durable_session.active_run_ids
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_current_descriptor_model_mismatch_invokes_provider_zero_times() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    messages = ({"role": "user", "content": "hello"},)
    descriptor = ExecutionDescriptor.create(
        agent=AgentSpec(name="agent", model="fake/model"),
        messages=messages,
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    )
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="hello",
        execution_descriptor=descriptor,
    )
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    with pytest.raises(AgentSDKError) as raised:
        await RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            policy=PolicyEngine("allow"),
        ).execute(
            run.run_id,
            ModelRequest(model="fake/changed", messages=messages),
        )

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "run execution descriptor mismatch"
    assert provider_calls == 0
    assert (
        RunSnapshot.model_validate(await store.get_snapshot("run", run.run_id)).status
        is RunStatus.CREATED
    )
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.parametrize(
    "mismatch",
    ["messages", "params", "request_tools", "tool_capability", "policy"],
)
@pytest.mark.asyncio
async def test_current_descriptor_validates_every_live_execution_field(
    mismatch: str,
) -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    messages = ({"role": "user", "content": "hello"},)
    durable_spec = ToolSpec(
        name="lookup",
        description="lookup",
        input_schema={"type": "object", "properties": {}},
        version="1",
        source="app",
        effects=("network",),
        timeout_seconds=5,
    )
    descriptor = ExecutionDescriptor.create(
        agent=AgentSpec(
            name="agent",
            model="fake/model",
            model_params={"temperature": 0.2},
        ),
        messages=messages,
        tools=(ToolCapabilityDescriptor.from_spec(durable_spec),),
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    )
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="hello",
        execution_descriptor=descriptor,
    )
    registry = ToolRegistry()
    live_spec = (
        durable_spec.model_copy(update={"version": "2"})
        if mismatch == "tool_capability"
        else durable_spec
    )

    async def handler(_: ToolContext, **__: object) -> object:
        return {"ok": True}

    registry.register(live_spec, handler)
    request = ModelRequest(
        model="fake/model",
        messages=(
            ({"role": "user", "content": "changed"},) if mismatch == "messages" else messages
        ),
        tools=() if mismatch == "request_tools" else registry.schemas(),
        params={"temperature": 0.3 if mismatch == "params" else 0.2},
    )
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    with pytest.raises(AgentSDKError, match="run execution descriptor mismatch"):
        await RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            tools=registry,
            policy=PolicyEngine("deny" if mismatch == "policy" else "allow"),
        ).execute(run.run_id, request)

    assert provider_calls == 0
    assert await store.get_run_checkpoint(run.run_id) is None


@pytest.mark.asyncio
async def test_heartbeat_renews_lease_while_provider_is_blocked() -> None:
    store = _RenewTrackingStore()
    ticker = _ManualTicker()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    messages = ({"role": "user", "content": "hello"},)
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_entered.set()
        await release_provider.wait()

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        return chunks()

    engine = RunEngine(
        store,
        LiteLLMGateway._for_test(provider),
        policy=PolicyEngine("allow"),
        lease_manager=LeaseManager(store, ttl=timedelta(seconds=3)),
        _clock=lambda: ticker.now,
        _sleep=ticker.sleep,
        _heartbeat_interval=1.0,
    )
    task = asyncio.create_task(
        engine.execute(
            run.run_id,
            ModelRequest(model="fake/model", messages=messages),
        )
    )
    try:
        await asyncio.wait_for(provider_entered.wait(), timeout=1)
        await asyncio.wait_for(ticker.sleeping.wait(), timeout=1)
        ticker.advance(timedelta(seconds=1))
        await asyncio.wait_for(store.renewed.wait(), timeout=1)
        ticker.now += timedelta(seconds=2, milliseconds=500)
        release_provider.set()

        assert (await asyncio.wait_for(task, timeout=1)).output_text == "ok"
    finally:
        release_provider.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_heartbeat_loss_interrupts_provider_and_prevents_later_progress() -> None:
    store = _RenewLossStore()
    ticker = _ManualTicker()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    provider_entered = asyncio.Event()
    provider_cancelled = asyncio.Event()

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_cancelled.set()
            raise
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            lease_manager=LeaseManager(store, ttl=timedelta(seconds=3)),
            _clock=lambda: ticker.now,
            _sleep=ticker.sleep,
            _heartbeat_interval=1.0,
        ).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "hello"},),
            ),
        )
    )
    try:
        await asyncio.wait_for(provider_entered.wait(), timeout=1)
        await asyncio.wait_for(ticker.sleeping.wait(), timeout=1)
        ticker.advance(timedelta(seconds=1))
        await asyncio.wait_for(store.renew_attempted.wait(), timeout=1)
        for _ in range(20):
            if task.done():
                break
            await asyncio.sleep(0)

        assert task.done()
        with pytest.raises(AgentSDKError) as raised:
            await task
        assert raised.value.code is ErrorCode.CONFLICT
        assert raised.value.message == "run lease is no longer current"
        assert provider_cancelled.is_set()
        checkpoint = await store.get_run_checkpoint(run.run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
        assert len(await store.list_unresolved_external_operations(run.run_id)) == 1
        assert not any(
            stored.event.type in {"model.call.completed", "run.failed"}
            for stored in await store.read_events(after_cursor=0)
        )
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_heartbeat_loss_rejects_provider_result_that_suppresses_cancel() -> None:
    store = _RenewLossStore()
    ticker = _ManualTicker()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    provider_entered = asyncio.Event()
    provider_cancelled = asyncio.Event()
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        provider_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_cancelled.set()

        async def late_chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "late"}, "finish_reason": "stop"}]}

        return late_chunks()

    task = asyncio.create_task(
        RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            lease_manager=LeaseManager(store, ttl=timedelta(seconds=3)),
            _clock=lambda: ticker.now,
            _sleep=ticker.sleep,
            _heartbeat_interval=1.0,
        ).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "hello"},),
            ),
        )
    )
    try:
        await asyncio.wait_for(provider_entered.wait(), timeout=1)
        await asyncio.wait_for(ticker.sleeping.wait(), timeout=1)
        ticker.advance(timedelta(seconds=1))
        await asyncio.wait_for(store.renew_attempted.wait(), timeout=1)

        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(task, timeout=1)
        assert raised.value.code is ErrorCode.CONFLICT
        assert raised.value.message == "run lease is no longer current"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    checkpoint = await store.get_run_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
    assert len(await store.list_unresolved_external_operations(run.run_id)) == 1
    assert provider_calls == 1
    assert provider_cancelled.is_set()
    events = [
        stored.event.type
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run.run_id
    ]
    assert "model.text.delta" not in events
    assert "model.call.completed" not in events
    assert "run.completed" not in events


@pytest.mark.asyncio
async def test_heartbeat_loss_closes_buffer_timer_without_late_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent_sdk.runtime.engine._DELTA_FLUSH_SECONDS", 3600.0)
    store = _RenewLossStore()
    ticker = _ManualTicker()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    provider_blocked = asyncio.Event()
    provider_cancelled = asyncio.Event()

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "buffered"}}]}
            provider_blocked.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                provider_cancelled.set()
                raise
            raise AssertionError("unreachable")

        return chunks()

    task = asyncio.create_task(
        RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            lease_manager=LeaseManager(store, ttl=timedelta(seconds=3)),
            _clock=lambda: ticker.now,
            _sleep=ticker.sleep,
            _heartbeat_interval=1.0,
        ).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "hello"},),
            ),
        )
    )
    pending_flushes: list[asyncio.Task[object]] = []
    try:
        await asyncio.wait_for(provider_blocked.wait(), timeout=1)
        await asyncio.wait_for(ticker.sleeping.wait(), timeout=1)
        ticker.advance(timedelta(seconds=1))
        await asyncio.wait_for(store.renew_attempted.wait(), timeout=1)

        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(task, timeout=1)
        assert raised.value.code is ErrorCode.CONFLICT
        assert raised.value.message == "run lease is no longer current"
        assert provider_cancelled.is_set()
        pending_flushes = [
            pending
            for pending in asyncio.all_tasks()
            if "_RunEmitter._flush_after_delay"
            in getattr(pending.get_coro(), "__qualname__", "")
        ]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for pending in pending_flushes:
            pending.cancel()
        await asyncio.gather(*pending_flushes, return_exceptions=True)

    assert pending_flushes == []
    events = [
        stored.event.type
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run.run_id
    ]
    assert "model.text.delta" not in events


@pytest.mark.asyncio
async def test_double_cancel_waits_for_single_failing_lease_release() -> None:
    store = _BlockingFailingReleaseStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    provider_entered = asyncio.Event()

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "hello"},),
            ),
        )
    )
    try:
        await asyncio.wait_for(provider_entered.wait(), timeout=1)
        task.cancel()
        await asyncio.wait_for(store.release_entered.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        store.allow_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert store.release_finished.is_set()
        assert store.release_calls == 1
    finally:
        store.allow_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if store.release_entered.is_set() and not store.release_finished.is_set():
            await asyncio.wait_for(store.release_finished.wait(), timeout=1)

    await asyncio.sleep(0)
    release_tasks = [
        pending
        for pending in asyncio.all_tasks()
        if "LeaseManager.release" in getattr(pending.get_coro(), "__qualname__", "")
    ]
    assert release_tasks == []


@pytest.mark.asyncio
async def test_lease_loss_survives_double_cancel_and_failing_release() -> None:
    store = _RenewLossBlockingFailingReleaseStore()
    ticker = _ManualTicker()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    provider_entered = asyncio.Event()

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            lease_manager=LeaseManager(store, ttl=timedelta(seconds=3)),
            _clock=lambda: ticker.now,
            _sleep=ticker.sleep,
            _heartbeat_interval=1.0,
        ).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "hello"},),
            ),
        )
    )
    try:
        await asyncio.wait_for(provider_entered.wait(), timeout=1)
        await asyncio.wait_for(ticker.sleeping.wait(), timeout=1)
        ticker.advance(timedelta(seconds=1))
        await asyncio.wait_for(store.renew_attempted.wait(), timeout=1)
        await asyncio.wait_for(store.release_entered.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        store.allow_release.set()
        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(task, timeout=1)
        assert raised.value.code is ErrorCode.CONFLICT
        assert raised.value.message == "run lease is no longer current"
        assert store.release_finished.is_set()
        assert store.release_calls == 1
    finally:
        store.allow_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if store.release_entered.is_set() and not store.release_finished.is_set():
            await asyncio.wait_for(store.release_finished.wait(), timeout=1)

    await asyncio.sleep(0)
    release_tasks = [
        pending
        for pending in asyncio.all_tasks()
        if "LeaseManager.release" in getattr(pending.get_coro(), "__qualname__", "")
    ]
    assert release_tasks == []


@pytest.mark.asyncio
async def test_model_outcome_and_safe_checkpoint_commit_atomically() -> None:
    store = _InitialBoundaryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "ok"}}]}
            yield {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }

        return chunks()

    await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
        run.run_id,
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "hello"},),
        ),
    )

    outcome_batches = [
        batch
        for batch in store.progress_batches
        if batch.operation is not None and batch.operation.expected is not None
    ]
    assert len(outcome_batches) == 1
    outcome = outcome_batches[0]
    assert outcome.operation is not None
    assert outcome.operation.updated.status is ExternalOperationStatus.COMPLETED
    assert outcome.operation.updated.model_dump(mode="json")["outcome"] == {
        "finish_reason": "stop",
        "text": "ok",
        "tool_calls": [],
        "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
        },
    }
    assert [event.type for event in outcome.events] == [
        "model.usage.reported",
        "model.call.completed",
    ]
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.updated.phase is RunCheckpointPhase.READY_FOR_MODEL
    assert outcome.checkpoint.updated.operation_id is None
    assert outcome.checkpoint.updated.output_parts == ("ok",)


@pytest.mark.asyncio
async def test_terminal_checkpoint_run_and_session_detach_commit_atomically() -> None:
    store = _InitialBoundaryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        return chunks()

    await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
        run.run_id,
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "hello"},),
        ),
    )

    terminal_batches = [
        batch
        for batch in store.progress_batches
        if any(event.type == "run.completed" for event in batch.events)
    ]
    assert len(terminal_batches) == 1
    terminal = terminal_batches[0]
    assert [event.type for event in terminal.events] == [
        "run.completed",
        "session.run.detached",
    ]
    assert {snapshot.kind for snapshot in terminal.snapshots} == {"run", "session"}
    assert terminal.checkpoint is not None
    assert terminal.checkpoint.updated.phase is RunCheckpointPhase.TERMINAL
    assert terminal.checkpoint.updated.operation_id is None
    assert terminal.checkpoint.updated.output_parts == ("ok",)
    durable_checkpoint = await store.get_run_checkpoint(run.run_id)
    assert durable_checkpoint == terminal.checkpoint.updated
    durable_session = await commands.get_session(session.session_id)
    assert durable_session.active_run_ids == ()


@pytest.mark.asyncio
async def test_provider_failure_is_atomic_and_public_traceback_is_secret_free() -> None:
    store = _InitialBoundaryStore()
    before_tasks = set(asyncio.all_tasks())
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    secret_message = "MODEL_MESSAGE_SECRET"
    secret_param = "MODEL_PARAM_SECRET"
    messages = ({"role": "user", "content": secret_message},)
    descriptor = ExecutionDescriptor.create(
        agent=AgentSpec(
            name="agent",
            model="fake/model",
            model_params={"provider_marker": secret_param},
        ),
        messages=messages,
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    )
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input=secret_message,
        execution_descriptor=descriptor,
    )

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        raise RuntimeError("PROVIDER_PAYLOAD_SECRET")

    with pytest.raises(AgentSDKError) as raised:
        await RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            policy=PolicyEngine("allow"),
        ).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=messages,
                params={"provider_marker": secret_param},
            ),
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    formatted = "".join(traceback.format_exception(raised.value))
    for secret in (secret_message, secret_param, "PROVIDER_PAYLOAD_SECRET"):
        assert secret not in formatted
    current = raised.value.__traceback__
    sdk_frames = 0
    while current is not None:
        if "/src/agent_sdk/" in current.tb_frame.f_code.co_filename.replace("\\", "/"):
            sdk_frames += 1
            locals_repr = repr(current.tb_frame.f_locals)
            for secret in (secret_message, secret_param, "PROVIDER_PAYLOAD_SECRET"):
                assert secret not in locals_repr
        current = current.tb_next
    assert sdk_frames > 0

    failure_batches = [
        batch
        for batch in store.progress_batches
        if any(event.type == "run.failed" for event in batch.events)
    ]
    assert len(failure_batches) == 1
    failure_batch = failure_batches[0]
    assert [event.type for event in failure_batch.events] == [
        "model.call.failed",
        "step.failed",
        "run.failed",
        "session.run.detached",
    ]
    assert failure_batch.operation is not None
    assert failure_batch.operation.expected is not None
    assert failure_batch.operation.updated.status is ExternalOperationStatus.FAILED
    assert failure_batch.checkpoint is not None
    assert failure_batch.checkpoint.updated.phase is RunCheckpointPhase.TERMINAL
    assert not any(
        secret in str(failure_batch.operation.updated.outcome)
        for secret in (secret_message, secret_param, "PROVIDER_PAYLOAD_SECRET")
    )
    await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) <= before_tasks


@pytest.mark.asyncio
async def test_missing_model_completed_atomically_fails_started_operation() -> None:
    store = _InitialBoundaryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )

    with pytest.raises(AgentSDKError) as raised:
        await RunEngine(store, _MissingModelCompletedGateway()).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "hello"},),
            ),
        )

    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "model call failed"
    failure_batches = [
        batch
        for batch in store.progress_batches
        if any(event.type == "run.failed" for event in batch.events)
    ]
    assert len(failure_batches) == 1
    failure_batch = failure_batches[0]
    assert [event.type for event in failure_batch.events] == [
        "model.call.failed",
        "step.failed",
        "run.failed",
        "session.run.detached",
    ]
    assert failure_batch.operation is not None
    assert failure_batch.operation.expected is not None
    assert failure_batch.operation.updated.status is ExternalOperationStatus.FAILED
    assert failure_batch.checkpoint is not None
    assert failure_batch.checkpoint.updated.phase is RunCheckpointPhase.TERMINAL
    assert failure_batch.checkpoint.updated.operation_id is None
    assert await store.list_unresolved_external_operations(run.run_id) == ()
    assert (await commands.get_session(session.session_id)).active_run_ids == ()


@pytest.mark.asyncio
async def test_authorized_tool_start_and_outcome_fence_handler_io() -> None:
    store = _InitialBoundaryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="use tool",
    )
    registry = ToolRegistry()
    handler_calls = 0

    async def handler(_: ToolContext, value: int) -> object:
        nonlocal handler_calls
        handler_calls += 1
        checkpoint = await store.get_run_checkpoint(run.run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.TOOL_IN_FLIGHT
        operations = await store.list_unresolved_external_operations(run.run_id)
        assert len(operations) == 1
        assert operations[0].operation_id == checkpoint.operation_id
        assert any(
            stored.event.type == "tool.call.started"
            for stored in await store.read_events(after_cursor=0)
        )
        return {"value": value + 1}

    registry.register(
        ToolSpec(
            name="increment",
            description="increment",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            effects=("compute",),
        ),
        handler,
    )
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if provider_calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {
                                            "name": "increment",
                                            "arguments": '{"value":1}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                yield {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}

        return chunks()

    result = await RunEngine(
        store,
        LiteLLMGateway._for_test(provider),
        tools=registry,
        policy=PolicyEngine("allow"),
    ).execute(
        run.run_id,
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "use tool"},),
            tools=registry.schemas(),
        ),
    )

    assert result.output_text == "done"
    assert handler_calls == 1
    tool_batches = [
        batch
        for batch in store.progress_batches
        if batch.operation is not None
        and batch.operation.updated.operation_kind.value == "tool_call"
    ]
    assert len(tool_batches) == 2
    started, outcome = tool_batches
    assert started.operation is not None and started.operation.expected is None
    started_operation = started.operation.updated
    assert isinstance(started_operation, ToolCallOperation)
    assert started_operation.turn == 0
    assert started_operation.lease_generation == 1
    assert (
        started_operation.tool_identity
        == ToolCapabilityDescriptor.from_spec(registry.get("increment").spec).capability_hash
    )
    assert len(started_operation.request_fingerprint) == 64
    assert dict(started_operation.recovery_metadata) == {
        "safe_retry": False,
        "retry_class": "unsafe",
    }
    assert [event.type for event in started.events] == ["tool.call.started"]
    assert started.checkpoint is not None
    assert started.checkpoint.updated.phase is RunCheckpointPhase.TOOL_IN_FLIGHT
    assert outcome.operation is not None and outcome.operation.expected is not None
    assert outcome.operation.expected == started_operation
    assert outcome.operation.updated.status is ExternalOperationStatus.COMPLETED
    assert [event.type for event in outcome.events] == ["tool.call.completed"]
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.updated.phase is RunCheckpointPhase.READY_FOR_MODEL
    assert outcome.checkpoint.updated.turn == 1
    assert len(outcome.checkpoint.updated.tool_results) == 1
    assert outcome.checkpoint.updated.messages[-1]["role"] == "tool"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_policy", "retry_class"),
    [
        (ToolRetryPolicy.IDEMPOTENT, "idempotent"),
        (ToolRetryPolicy.SAFE_RETRY, "safe_retry"),
    ],
)
async def test_certified_tool_start_stamps_policy_before_handler(
    retry_policy: ToolRetryPolicy,
    retry_class: str,
) -> None:
    store = _InitialBoundaryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="use tool",
    )
    registry = ToolRegistry()
    observed_metadata: list[dict[str, object]] = []

    async def handler(_: ToolContext) -> str:
        operations = await store.list_unresolved_external_operations(run.run_id)
        observed_metadata.extend(dict(operation.recovery_metadata) for operation in operations)
        return "ok"

    registry.register(
        ToolSpec(
            name="target",
            description="target",
            input_schema={"type": "object", "additionalProperties": False},
            retry_policy=retry_policy,
        ),
        handler,
    )
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if provider_calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_certified",
                                        "function": {
                                            "name": "target",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    result = await RunEngine(
        store,
        LiteLLMGateway._for_test(provider),
        tools=registry,
        policy=PolicyEngine("allow"),
    ).execute(
        run.run_id,
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "use tool"},),
            tools=registry.schemas(),
        ),
    )
    assert result.tool_results[0].status is ToolResultStatus.SUCCEEDED
    assert observed_metadata == [
        {"safe_retry": True, "retry_class": retry_class}
    ]


@pytest.mark.asyncio
async def test_model_operation_usage_is_per_turn_while_checkpoint_accumulates() -> None:
    store = _InitialBoundaryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="use tool",
    )
    registry = ToolRegistry()

    async def handler(_: ToolContext) -> object:
        return {"ok": True}

    registry.register(
        ToolSpec(
            name="target",
            description="target",
            input_schema={"type": "object", "additionalProperties": False},
        ),
        handler,
    )
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if provider_calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_usage",
                                        "function": {
                                            "name": "target",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                }
            else:
                yield {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "total_tokens": 6,
                    },
                }

        return chunks()

    await RunEngine(
        store,
        LiteLLMGateway._for_test(provider),
        tools=registry,
        policy=PolicyEngine("allow"),
    ).execute(
        run.run_id,
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "use tool"},),
            tools=registry.schemas(),
        ),
    )

    model_outcomes = [
        batch
        for batch in store.progress_batches
        if batch.operation is not None
        and batch.operation.updated.operation_kind.value == "model_call"
        and batch.operation.expected is not None
    ]
    assert [
        batch.operation.updated.model_dump(mode="json")["outcome"]["usage"]
        for batch in model_outcomes
        if batch.operation is not None
    ] == [
        {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    ]
    checkpoint = await store.get_run_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.usage.model_dump(mode="json") == {
        "prompt_tokens": 6,
        "completion_tokens": 3,
        "total_tokens": 9,
    }


@pytest.mark.asyncio
async def test_permission_waiting_and_resolution_advance_exact_checkpoints() -> None:
    store = _InitialBoundaryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="use tool",
    )
    registry = ToolRegistry()
    bridge = InProcessPermissionBridge()
    handler_entered = asyncio.Event()

    async def handler(_: ToolContext) -> object:
        handler_entered.set()
        return {"ok": True}

    registry.register(
        ToolSpec(
            name="effect",
            description="effect",
            input_schema={"type": "object", "additionalProperties": False},
            effects=("external",),
        ),
        handler,
    )
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if provider_calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_effect",
                                        "function": {
                                            "name": "effect",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    task = asyncio.create_task(
        RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            tools=registry,
            policy=PolicyEngine("ask"),
            permission_bridge=bridge,
        ).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "use tool"},),
                tools=registry.schemas(),
            ),
        )
    )
    try:
        permission = await asyncio.wait_for(bridge.next_request(run.run_id), timeout=1)
        waiting = await store.get_run_checkpoint(run.run_id)
        assert waiting is not None
        assert waiting.phase is RunCheckpointPhase.WAITING
        assert (
            RunSnapshot.model_validate(await store.get_snapshot("run", run.run_id)).status
            is RunStatus.WAITING_PERMISSION
        )

        await bridge.resolve(permission.request_id, PermissionDecision.allow_once())
        await asyncio.wait_for(handler_entered.wait(), timeout=1)
        resolution_batches = [
            batch
            for batch in store.progress_batches
            if any(event.type == "permission.resolved" for event in batch.events)
        ]
        assert len(resolution_batches) == 1
        resolved = resolution_batches[0]
        assert resolved.checkpoint is not None
        assert resolved.checkpoint.expected is not None
        assert resolved.checkpoint.expected.phase is RunCheckpointPhase.WAITING
        assert resolved.checkpoint.updated.phase is RunCheckpointPhase.READY_FOR_TOOL
        assert any(snapshot.kind == "run" for snapshot in resolved.snapshots)
        await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ambiguous_model_start_replays_same_batch_before_one_provider_call() -> None:
    store = _AmbiguousModelStartStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        return chunks()

    result = await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
        run.run_id,
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "hello"},),
        ),
    )

    assert result.output_text == "ok"
    assert provider_calls == 1
    assert len(store.model_start_batch_ids) == 2
    assert len(set(store.model_start_batch_ids)) == 1
    events = [
        stored.event.type
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run.run_id
    ]
    assert events.count("model.call.started") == 1


@pytest.mark.asyncio
async def test_simultaneous_engines_have_one_lease_winner_and_provider_call() -> None:
    store = _ConcurrentCreatedReadStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    store.block_run_id = run.run_id
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        provider_entered.set()
        await release_provider.wait()

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        return chunks()

    first = RunEngine(store, LiteLLMGateway._for_test(provider))
    second = RunEngine(store, LiteLLMGateway._for_test(provider))
    request = ModelRequest(
        model="fake/model",
        messages=({"role": "user", "content": "hello"},),
    )
    tasks = (
        asyncio.create_task(first.execute(run.run_id, request)),
        asyncio.create_task(second.execute(run.run_id, request)),
    )
    try:
        await asyncio.wait_for(store.both_loaded.wait(), timeout=1)
        await asyncio.wait_for(provider_entered.wait(), timeout=1)
        for _ in range(20):
            if any(task.done() for task in tasks):
                break
            await asyncio.sleep(0)
        loser = next(task for task in tasks if task.done())
        with pytest.raises(AgentSDKError) as conflict:
            await loser
        assert conflict.value.code is ErrorCode.CONFLICT
        assert conflict.value.message == "run lease is held"
        assert provider_calls == 1
        release_provider.set()
        winner = next(task for task in tasks if task is not loser)
        assert (await asyncio.wait_for(winner, timeout=1)).output_text == "ok"
        run_started = [
            event
            for event in await store.read_events(after_cursor=0)
            if event.event.run_id == run.run_id and event.event.type == "run.started"
        ]
        assert len(run_started) == 1
    finally:
        release_provider.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    ("case", "expected_status"),
    [
        ("missing", ToolResultStatus.FAILED),
        ("invalid", ToolResultStatus.INVALID_ARGUMENTS),
        ("denied", ToolResultStatus.DENIED),
    ],
)
@pytest.mark.asyncio
async def test_safe_tool_rejections_checkpoint_without_fake_operation(
    case: str,
    expected_status: ToolResultStatus,
) -> None:
    store = _InitialBoundaryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="use tool",
    )
    registry = ToolRegistry()
    handler_calls = 0
    if case != "missing":

        async def handler(_: ToolContext, value: int) -> object:
            nonlocal handler_calls
            handler_calls += 1
            return value

        registry.register(
            ToolSpec(
                name="target",
                description="target",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            handler,
        )
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if provider_calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_safe",
                                        "function": {
                                            "name": "target",
                                            "arguments": (
                                                "{}" if case == "invalid" else '{"value":1}'
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    result = await RunEngine(
        store,
        LiteLLMGateway._for_test(provider),
        tools=registry,
        policy=PolicyEngine("deny" if case == "denied" else "allow"),
    ).execute(
        run.run_id,
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "use tool"},),
            tools=registry.schemas(),
        ),
    )

    assert result.tool_results[0].status is expected_status
    assert handler_calls == 0
    assert not any(
        batch.operation is not None and batch.operation.updated.operation_kind.value == "tool_call"
        for batch in store.progress_batches
    )
    safe_batches = [
        batch
        for batch in store.progress_batches
        if any(event.type == "tool.call.completed" for event in batch.events)
    ]
    assert len(safe_batches) == 1
    safe = safe_batches[0]
    assert safe.operation is None
    assert safe.checkpoint is not None
    assert safe.checkpoint.expected is not None
    assert safe.checkpoint.expected.phase is RunCheckpointPhase.READY_FOR_TOOL
    assert safe.checkpoint.updated.phase is RunCheckpointPhase.READY_FOR_MODEL


@pytest.mark.asyncio
async def test_cancellation_waits_for_pending_model_start_commit() -> None:
    store = _BlockingModelStartStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    task = asyncio.create_task(
        RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "hello"},),
            ),
        )
    )
    try:
        await asyncio.wait_for(store.model_start_blocked.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        store.release_model_start.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    finally:
        store.release_model_start.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    checkpoint = await store.get_run_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
    operations = await store.list_unresolved_external_operations(run.run_id)
    assert len(operations) == 1
    assert operations[0].operation_id == checkpoint.operation_id
    assert provider_calls == 0


@pytest.mark.asyncio
async def test_handler_cancellation_leaves_durable_tool_in_flight_boundary() -> None:
    store = InMemoryStore()
    before_tasks = set(asyncio.all_tasks())
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="use tool",
    )
    registry = ToolRegistry()
    handler_entered = asyncio.Event()
    handler_cancelled = asyncio.Event()
    provider_calls = 0
    handler_calls = 0

    async def handler(_: ToolContext, value: int) -> object:
        nonlocal handler_calls
        handler_calls += 1
        handler_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        return value

    registry.register(
        ToolSpec(
            name="target",
            description="target",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        ),
        handler,
    )

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_cancel",
                                    "function": {
                                        "name": "target",
                                        "arguments": '{"value":1}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

        return chunks()

    task = asyncio.create_task(
        RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            tools=registry,
            policy=PolicyEngine("allow"),
        ).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "use tool"},),
                tools=registry.schemas(),
            ),
        )
    )
    try:
        await asyncio.wait_for(handler_entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    checkpoint = await store.get_run_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.TOOL_IN_FLIGHT
    operations = await store.list_unresolved_external_operations(run.run_id)
    assert len(operations) == 1
    assert operations[0].operation_id == checkpoint.operation_id
    assert provider_calls == 1
    assert handler_calls == 1
    events = [
        stored.event.type
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run.run_id
    ]
    assert events.count("tool.call.started") == 1
    assert "tool.call.completed" not in events
    await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) <= before_tasks


@pytest.mark.asyncio
async def test_every_live_progress_write_uses_fenced_api() -> None:
    store = _RejectOrdinaryCommitStore()
    before_tasks = set(asyncio.all_tasks())
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    store.reject_ordinary = True

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "ok"}}]}
            yield {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }

        return chunks()

    result = await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
        run.run_id,
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "hello"},),
        ),
    )

    assert result.output_text == "ok"
    assert store.progress_calls >= 5
    await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) <= before_tasks


@pytest.mark.parametrize(
    ("case", "expected_status"),
    [
        ("failure", ToolResultStatus.FAILED),
        ("timeout", ToolResultStatus.TIMED_OUT),
    ],
)
@pytest.mark.asyncio
async def test_authorized_tool_normalized_outcome_is_failed_and_safe(
    case: str,
    expected_status: ToolResultStatus,
) -> None:
    store = _InitialBoundaryStore()
    before_tasks = set(asyncio.all_tasks())
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="use tool",
    )
    registry = ToolRegistry()
    handler_cancelled = asyncio.Event()

    async def handler(_: ToolContext, value: int) -> object:
        if case == "failure":
            raise RuntimeError("private handler payload")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        return value

    registry.register(
        ToolSpec(
            name="target",
            description="target",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            timeout_seconds=0.001 if case == "timeout" else None,
        ),
        handler,
    )
    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if provider_calls == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_normalized",
                                        "function": {
                                            "name": "target",
                                            "arguments": '{"value":1}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    result = await RunEngine(
        store,
        LiteLLMGateway._for_test(provider),
        tools=registry,
        policy=PolicyEngine("allow"),
    ).execute(
        run.run_id,
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "use tool"},),
            tools=registry.schemas(),
        ),
    )

    assert result.tool_results[0].status is expected_status
    if case == "timeout":
        await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
    tool_outcomes = [
        batch
        for batch in store.progress_batches
        if batch.operation is not None
        and isinstance(batch.operation.updated, ToolCallOperation)
        and batch.operation.expected is not None
    ]
    assert len(tool_outcomes) == 1
    outcome = tool_outcomes[0]
    assert outcome.operation is not None
    assert outcome.operation.updated.status is ExternalOperationStatus.FAILED
    assert outcome.operation.updated.outcome == result.tool_results[0].model_dump(mode="json")
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.updated.phase is RunCheckpointPhase.READY_FOR_MODEL
    assert outcome.checkpoint.updated.operation_id is None
    assert outcome.checkpoint.updated.tool_results[-1] == result.tool_results[0]
    await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) <= before_tasks


@pytest.mark.asyncio
async def test_generation_takeover_rejects_late_model_outcome() -> None:
    store = InMemoryStore()
    ticker = _ManualTicker()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        provider_entered.set()
        await release_provider.wait()

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "late"}, "finish_reason": "stop"}]}

        return chunks()

    task = asyncio.create_task(
        RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            lease_manager=LeaseManager(store, ttl=timedelta(seconds=3)),
            _clock=lambda: ticker.now,
            _sleep=ticker.sleep,
            _heartbeat_interval=1.0,
        ).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "hello"},),
            ),
        )
    )
    try:
        await asyncio.wait_for(provider_entered.wait(), timeout=1)
        ticker.now += timedelta(seconds=4)
        takeover = await store.acquire_lease(
            run_id=run.run_id,
            owner="new-owner",
            now=ticker.now,
            expires_at=ticker.now + timedelta(seconds=3),
        )
        release_provider.set()
        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(task, timeout=1)
        assert raised.value.code is ErrorCode.CONFLICT
        await store.assert_current_lease(takeover, now=ticker.now)
    finally:
        release_provider.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    checkpoint = await store.get_run_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
    assert len(await store.list_unresolved_external_operations(run.run_id)) == 1
    events = [
        stored.event.type
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run.run_id
    ]
    assert "model.call.completed" not in events
    assert "run.failed" not in events


@pytest.mark.asyncio
async def test_generation_takeover_rejects_late_tool_outcome() -> None:
    store = InMemoryStore()
    ticker = _ManualTicker()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="use tool",
    )
    registry = ToolRegistry()
    handler_entered = asyncio.Event()
    release_handler = asyncio.Event()
    provider_calls = 0

    async def handler(_: ToolContext, value: int) -> object:
        handler_entered.set()
        await release_handler.wait()
        return {"value": value}

    registry.register(
        ToolSpec(
            name="target",
            description="target",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        ),
        handler,
    )

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_takeover",
                                    "function": {
                                        "name": "target",
                                        "arguments": '{"value":1}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

        return chunks()

    task = asyncio.create_task(
        RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            tools=registry,
            policy=PolicyEngine("allow"),
            lease_manager=LeaseManager(store, ttl=timedelta(seconds=3)),
            _clock=lambda: ticker.now,
            _sleep=ticker.sleep,
            _heartbeat_interval=1.0,
        ).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "use tool"},),
                tools=registry.schemas(),
            ),
        )
    )
    try:
        await asyncio.wait_for(handler_entered.wait(), timeout=1)
        ticker.now += timedelta(seconds=4)
        takeover = await store.acquire_lease(
            run_id=run.run_id,
            owner="new-owner",
            now=ticker.now,
            expires_at=ticker.now + timedelta(seconds=3),
        )
        release_handler.set()
        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(task, timeout=1)
        assert raised.value.code is ErrorCode.CONFLICT
        await store.assert_current_lease(takeover, now=ticker.now)
    finally:
        release_handler.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    checkpoint = await store.get_run_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.TOOL_IN_FLIGHT
    assert len(await store.list_unresolved_external_operations(run.run_id)) == 1
    events = [
        stored.event.type
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run.run_id
    ]
    assert "tool.call.completed" not in events
    assert "run.failed" not in events


@pytest.mark.asyncio
async def test_tool_outcome_storage_conflict_is_secret_free_and_nonterminal() -> None:
    store = _RejectToolOutcomeStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="use tool",
    )
    registry = ToolRegistry()
    argument_secret = "TOOL_ARGUMENT_SECRET"
    result_secret = "TOOL_RESULT_SECRET"

    async def handler(_: ToolContext, value: str) -> object:
        assert value == argument_secret
        return {"private": result_secret}

    registry.register(
        ToolSpec(
            name="target",
            description="target",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        ),
        handler,
    )

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_secret",
                                    "function": {
                                        "name": "target",
                                        "arguments": ('{"value":"' + argument_secret + '"}'),
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

        return chunks()

    try:
        await RunEngine(
            store,
            LiteLLMGateway._for_test(provider),
            tools=registry,
            policy=PolicyEngine("allow"),
        ).execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "use tool"},),
                tools=registry.schemas(),
            ),
        )
    except AgentSDKError as error:
        caught = error
    else:
        raise AssertionError("storage conflict must fail execution")

    assert caught.code is ErrorCode.CONFLICT
    assert caught.__cause__ is None
    assert caught.__context__ is None
    formatted = "".join(traceback.format_exception(type(caught), caught, caught.__traceback__))
    for secret in (argument_secret, result_secret):
        assert secret not in formatted
    extracted = traceback.extract_tb(caught.__traceback__)
    assert extracted
    for frame, _ in traceback.walk_tb(caught.__traceback__):
        if "agent_sdk" in frame.f_code.co_filename:
            local_values = repr(frame.f_locals)
            for secret in (argument_secret, result_secret):
                assert secret not in local_values

    checkpoint = await store.get_run_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.TOOL_IN_FLIGHT
    assert len(await store.list_unresolved_external_operations(run.run_id)) == 1
    assert (
        RunSnapshot.model_validate(await store.get_snapshot("run", run.run_id)).status
        is RunStatus.RUNNING
    )


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.asyncio
async def test_terminal_precommit_failure_or_cancel_has_no_partial_targets(
    cancel: bool,
) -> None:
    store = _RejectTerminalStore(cancel=cancel)
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        return chunks()

    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
                run.run_id,
                ModelRequest(
                    model="fake/model",
                    messages=({"role": "user", "content": "hello"},),
                ),
            )
    else:
        with pytest.raises(AgentSDKError) as raised:
            await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
                run.run_id,
                ModelRequest(
                    model="fake/model",
                    messages=({"role": "user", "content": "hello"},),
                ),
            )
        assert raised.value.code is ErrorCode.INTERNAL

    checkpoint = await store.get_run_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.READY_FOR_MODEL
    assert (
        RunSnapshot.model_validate(await store.get_snapshot("run", run.run_id)).status
        is RunStatus.RUNNING
    )
    durable_session = SessionSnapshot.model_validate(
        await store.get_snapshot("session", session.session_id)
    )
    assert run.run_id in durable_session.active_run_ids
    events = [
        stored.event.type
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run.run_id
    ]
    assert "run.completed" not in events


@pytest.mark.asyncio
async def test_terminal_retries_only_session_target_after_concurrent_change() -> None:
    store = _ConcurrentSessionTerminalStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="legacy:1",
        user_input="hello",
    )

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        return chunks()

    result = await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
        run.run_id,
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "hello"},),
        ),
    )

    assert result.output_text == "ok"
    assert store.mutated
    events = [
        stored.event
        for stored in await store.read_events(after_cursor=0)
        if stored.event.run_id == run.run_id and stored.event.type == "run.completed"
    ]
    assert len(events) == 1
    checkpoint = await store.get_run_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.phase is RunCheckpointPhase.TERMINAL
