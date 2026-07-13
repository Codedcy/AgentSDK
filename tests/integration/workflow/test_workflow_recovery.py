from __future__ import annotations

import asyncio
import traceback
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import AgentSDKError, AgentSpec, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.models import RunSnapshot, RunStatus
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    SnapshotWrite,
    StateStore,
    StoredEvent,
)
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.workflow import (
    WorkflowCompiler,
    WorkflowExecutor,
    WorkflowHandle,
    WorkflowIR,
)
from agent_sdk.workflow import WorkflowNodeStatus, WorkflowRunStatus

DEFINITION = {
    "api_version": "agent-sdk/v1",
    "kind": "Workflow",
    "name": "parent-child",
    "nodes": [
        {
            "id": "plan",
            "kind": "agent",
            "agent_revision": "planner:1",
            "input": "make a plan",
        },
        {
            "id": "verify",
            "kind": "agent",
            "agent_revision": "worker:1",
            "input": "verify independently",
            "run_as": "child",
            "success_criteria": ["return verification"],
            "evidence_refs": ["artifact:plan"],
        },
    ],
    "edges": [{"source": "plan", "target": "verify"}],
}


def _chunks(text: str) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": text}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }

    return generate()


class _CommitThenCancelStore:
    def __init__(self, delegate: StateStore, event_type: str) -> None:
        self.delegate = delegate
        self.event_type = event_type
        self.triggered = False

    async def commit(self, batch: CommitBatch) -> CommitResult:
        result = await self.delegate.commit(batch)
        if not self.triggered and any(event.type == self.event_type for event in batch.events):
            self.triggered = True
            raise asyncio.CancelledError
        return result

    async def read_events(
        self, *, after_cursor: int, session_id: str | None = None
    ) -> list[StoredEvent]:
        return await self.delegate.read_events(
            after_cursor=after_cursor, session_id=session_id
        )

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        return await self.delegate.get_snapshot(kind, entity_id)

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


def _agents() -> AgentRegistry:
    agents = AgentRegistry()
    agents.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    return agents


def _ir() -> WorkflowIR:
    import yaml

    return WorkflowCompiler().compile_yaml(yaml.safe_dump(DEFINITION, sort_keys=False))


@pytest.mark.asyncio
async def test_sqlite_resume_skips_commit_then_cancelled_completed_node(
    tmp_path: Path,
) -> None:
    calls = {"fake/planner": 0, "fake/worker": 0}

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        model = str(params["model"])
        calls[model] += 1
        return _chunks("planned" if model == "fake/planner" else "verified")

    database = tmp_path / "recover.db"
    sqlite = await SQLiteStore.open(database)
    store = _CommitThenCancelStore(sqlite, "workflow.node.completed")
    commands = RuntimeCommands(store)
    executor = WorkflowExecutor(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    session = await commands.create_session(workspaces=[])
    handle = await executor.start(session.session_id, _ir())

    with pytest.raises(asyncio.CancelledError):
        await handle.result()
    assert calls["fake/planner"] == 1
    await sqlite.close()

    reopened = await SQLiteStore.open(database)
    try:
        resumed = WorkflowExecutor(
            reopened,
            RuntimeCommands(reopened),
            RunEngine(reopened, LiteLLMGateway._for_test(provider)),
            _agents(),
        )
        recovered = await resumed.resume(handle.workflow_run_id)
        assert (await recovered.result()).output_text == "verified"
        assert calls == {"fake/planner": 1, "fake/worker": 1}

        completed = await resumed.resume(handle.workflow_run_id)
        assert (await completed.result()).output_text == "verified"
        assert calls == {"fake/planner": 1, "fake/worker": 1}

        read_only = WorkflowExecutor(
            reopened,
            RuntimeCommands(reopened),
            RunEngine(reopened, LiteLLMGateway._for_test(provider)),
            AgentRegistry(),
        )
        assert (
            await (await read_only.resume(handle.workflow_run_id)).result()
        ).output_text == "verified"
        assert calls == {"fake/planner": 1, "fake/worker": 1}

        different_data = dict(DEFINITION)
        different_data["name"] = "different"
        import yaml

        different = WorkflowCompiler().compile_yaml(
            yaml.safe_dump(different_data, sort_keys=False)
        )
        with pytest.raises(AgentSDKError) as mismatch:
            await resumed.resume(handle.workflow_run_id, expected_workflow=different)
        assert mismatch.value.code is ErrorCode.CONFLICT
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_resume_reconciles_terminal_related_run_without_reexecution(
    tmp_path: Path,
) -> None:
    calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _chunks("already done")

    sqlite = await SQLiteStore.open(tmp_path / "reconcile.db")
    store = _CommitThenCancelStore(sqlite, "workflow.node.started")
    commands = RuntimeCommands(store)
    engine = RunEngine(store, LiteLLMGateway._for_test(provider))
    executor = WorkflowExecutor(store, commands, engine, _agents())
    session = await commands.create_session(workspaces=[])
    handle = await executor.start(session.session_id, _ir())
    with pytest.raises(asyncio.CancelledError):
        await handle.result()

    snapshot = await executor.get(handle.workflow_run_id)
    selected_run_id = snapshot.nodes[0].run_id
    assert selected_run_id is not None
    await commands.start_run(
        session.session_id,
        run_id=selected_run_id,
        agent_revision="planner:1",
        user_input="make a plan",
        workflow_run_id=handle.workflow_run_id,
        workflow_node_id="plan",
    )
    await engine.execute(
        selected_run_id,
        ModelRequest(
            model="fake/planner",
            messages=({"role": "user", "content": "make a plan"},),
        ),
    )
    assert calls == 1

    resumed = WorkflowExecutor(sqlite, RuntimeCommands(sqlite), engine, _agents())
    result = await (await resumed.resume(handle.workflow_run_id)).result()
    assert result.output_text == "already done"
    assert calls == 2  # reconciled plan plus one child, never a second planner call
    await sqlite.close()


@pytest.mark.asyncio
async def test_session_delete_removes_workflow_node_and_related_run_data(
    tmp_path: Path,
) -> None:
    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        return _chunks("done")

    store = await SQLiteStore.open(tmp_path / "delete.db")
    commands = RuntimeCommands(store)
    executor = WorkflowExecutor(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    session = await commands.create_session(workspaces=[])
    handle = await executor.start(session.session_id, _ir())
    result = await handle.result()
    run_ids = [node.run_id for node in result.nodes]
    node_entity_ids = [node.entity_id for node in result.nodes]

    await store.delete_session(session.session_id)

    assert await store.get_snapshot("workflow", handle.workflow_run_id) is None
    assert [
        await store.get_snapshot("workflow_node", item) for item in node_entity_ids
    ] == [None, None]
    assert [await store.get_snapshot("run", item or "") for item in run_ids] == [
        None,
        None,
    ]
    assert await store.read_events(after_cursor=0, session_id=session.session_id) == []
    await store.close()


@pytest.mark.asyncio
async def test_resume_fails_closed_for_inflight_run_without_replay(
    tmp_path: Path,
) -> None:
    calls = 0
    provider_started = asyncio.Event()

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        provider_started.set()
        await asyncio.Event().wait()
        return _chunks("unreachable")

    store = await SQLiteStore.open(tmp_path / "inflight.db")
    commands = RuntimeCommands(store)
    executor = WorkflowExecutor(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    session = await commands.create_session(workspaces=[])
    handle = await executor.start(session.session_id, _ir())
    while True:
        snapshot = await executor.get(handle.workflow_run_id)
        if snapshot.nodes[0].run_id is not None:
            run = RunSnapshot.model_validate(
                await store.get_snapshot("run", snapshot.nodes[0].run_id)
            )
            if run.status is RunStatus.RUNNING:
                break
        await asyncio.sleep(0.01)
    await asyncio.wait_for(provider_started.wait(), timeout=1)
    handle._task.cancel()  # type: ignore[attr-defined]
    with pytest.raises(asyncio.CancelledError):
        await handle.result()

    resumed = WorkflowExecutor(
        store,
        RuntimeCommands(store),
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    with pytest.raises(AgentSDKError) as raised:
        await (await resumed.resume(handle.workflow_run_id)).result()
    assert raised.value.code is ErrorCode.INVALID_STATE
    assert calls == 1
    await store.close()


class _BlockingBeforeWorkflowCommitStore:
    def __init__(self) -> None:
        self.delegate = InMemoryStore()
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(event.type == "workflow.node.completed" for event in batch.events):
            self.blocked.set()
            await self.release.wait()
        return await self.delegate.commit(batch)

    async def read_events(
        self, *, after_cursor: int, session_id: str | None = None
    ) -> list[StoredEvent]:
        return await self.delegate.read_events(
            after_cursor=after_cursor, session_id=session_id
        )

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        return await self.delegate.get_snapshot(kind, entity_id)

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


@pytest.mark.asyncio
async def test_delete_racing_workflow_transition_cannot_resurrect_any_state() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _chunks("done")

    store = _BlockingBeforeWorkflowCommitStore()
    commands = RuntimeCommands(store)
    executor = WorkflowExecutor(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    session = await commands.create_session(workspaces=[])
    handle = await executor.start(session.session_id, _ir())
    await asyncio.wait_for(store.blocked.wait(), timeout=1)
    before_delete = await executor.get(handle.workflow_run_id)
    run_id = before_delete.nodes[0].run_id

    await store.delete_session(session.session_id)
    store.release.set()

    with pytest.raises(AgentSDKError) as raised:
        await handle.result()
    assert raised.value.code is ErrorCode.NOT_FOUND
    assert await store.get_snapshot("workflow", handle.workflow_run_id) is None
    assert [
        await store.get_snapshot("workflow_node", node.entity_id)
        for node in before_delete.nodes
    ] == [None, None]
    assert await store.get_snapshot("run", run_id or "") is None
    assert await store.read_events(after_cursor=0) == []


@pytest.mark.asyncio
async def test_provider_failure_is_sanitized_and_durably_fails_node_and_workflow() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        raise RuntimeError("RAW_PROVIDER_SECRET")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    executor = WorkflowExecutor(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    session = await commands.create_session(workspaces=[])
    handle = await executor.start(session.session_id, _ir())

    with pytest.raises(AgentSDKError) as raised:
        await handle.result()

    assert raised.value.code is ErrorCode.INTERNAL
    assert "RAW_PROVIDER_SECRET" not in "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert all(
        "RAW_PROVIDER_SECRET" not in repr(value)
        for frame in _traceback_frames(raised.value)
        for value in frame.f_locals.values()
    )
    snapshot = await executor.get(handle.workflow_run_id)
    assert snapshot.status is WorkflowRunStatus.FAILED
    assert snapshot.nodes[0].status is WorkflowNodeStatus.FAILED
    assert snapshot.nodes[1].status is WorkflowNodeStatus.PENDING
    workflow_events = [
        event.event.type
        for event in await store.read_events(after_cursor=0)
        if event.event.run_id == handle.workflow_run_id
    ]
    assert workflow_events[-2:] == ["workflow.node.failed", "workflow.failed"]


class _FailingWorkflowCommitStore(_BlockingBeforeWorkflowCommitStore):
    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(event.type == "workflow.started" for event in batch.events):
            raise RuntimeError("RAW_STORE_SECRET")
        return await self.delegate.commit(batch)


@pytest.mark.asyncio
async def test_store_failure_is_sanitized_before_workflow_exposure() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _chunks("unused")

    store = _FailingWorkflowCommitStore()
    commands = RuntimeCommands(store)
    executor = WorkflowExecutor(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    session = await commands.create_session(workspaces=[])

    with pytest.raises(AgentSDKError) as raised:
        await executor.start(session.session_id, _ir())

    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "failed to persist workflow state"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "RAW_STORE_SECRET" not in "".join(traceback.format_exception(raised.value))
    assert all(
        "RAW_STORE_SECRET" not in repr(value)
        for frame in _traceback_frames(raised.value)
        for value in frame.f_locals.values()
    )
    assert not any(
        event.event.type == "workflow.started"
        for event in await store.read_events(after_cursor=0)
    )


def _traceback_frames(error: BaseException) -> list[Any]:
    frames: list[Any] = []
    current = error.__traceback__
    while current is not None:
        frames.append(current.tb_frame)
        current = current.tb_next
    return frames


@pytest.mark.parametrize("foreign_status", ["created", "completed"])
@pytest.mark.asyncio
async def test_resume_rejects_cross_session_selected_run_without_model_or_projection(
    foreign_status: str,
) -> None:
    calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _chunks("FOREIGN_SESSION_SECRET")

    delegate = InMemoryStore()
    interrupted = _CommitThenCancelStore(delegate, "workflow.node.started")
    commands = RuntimeCommands(interrupted)
    executor = WorkflowExecutor(
        interrupted,
        commands,
        RunEngine(interrupted, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    owner = await commands.create_session(workspaces=[])
    handle = await executor.start(owner.session_id, _ir())
    with pytest.raises(asyncio.CancelledError):
        await handle.result()
    workflow = await executor.get(handle.workflow_run_id)

    foreign_session = await commands.create_session(workspaces=[])
    foreign = await commands.start_run(
        foreign_session.session_id,
        agent_revision="planner:1",
        user_input="make a plan",
    )
    if foreign_status == "completed":
        await RunEngine(
            interrupted, LiteLLMGateway._for_test(provider)
        ).execute(
            foreign.run_id,
            ModelRequest(
                model="fake/planner",
                messages=({"role": "user", "content": "make a plan"},),
            ),
        )
    calls = 0

    injected_node = workflow.nodes[0].model_copy(update={"run_id": foreign.run_id})
    injected = workflow.model_copy(
        update={
            "nodes": (injected_node, *workflow.nodes[1:]),
        }
    )
    await delegate.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "workflow",
                    workflow.workflow_run_id,
                    workflow.session_id,
                    workflow.version + 1,
                    injected.model_dump(mode="json"),
                ),
                SnapshotWrite(
                    "workflow_node",
                    injected_node.entity_id,
                    workflow.session_id,
                    injected_node.version + 1,
                    injected_node.model_dump(mode="json"),
                ),
            ),
        )
    )
    resumed = WorkflowExecutor(
        delegate,
        RuntimeCommands(delegate),
        RunEngine(delegate, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    events_before = await delegate.read_events(after_cursor=0)

    with pytest.raises(AgentSDKError) as raised:
        await (await resumed.resume(workflow.workflow_run_id)).result()

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "related run does not match workflow node"
    assert "FOREIGN_SESSION_SECRET" not in str(raised.value)
    assert calls == 0
    assert await resumed.get(workflow.workflow_run_id) == injected
    assert await delegate.read_events(after_cursor=0) == events_before


@pytest.mark.asyncio
async def test_resume_rejects_corrupt_embedded_node_owner_before_write_or_model() -> None:
    calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _chunks("unused")

    delegate = InMemoryStore()
    interrupted = _CommitThenCancelStore(delegate, "workflow.node.started")
    commands = RuntimeCommands(interrupted)
    executor = WorkflowExecutor(
        interrupted,
        commands,
        RunEngine(interrupted, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    session = await commands.create_session(workspaces=[])
    handle = await executor.start(session.session_id, _ir())
    with pytest.raises(asyncio.CancelledError):
        await handle.result()
    workflow = await executor.get(handle.workflow_run_id)
    corrupt_node = workflow.nodes[0].model_copy(update={"session_id": "ses_foreign"})
    corrupt = workflow.model_copy(
        update={
            "nodes": (corrupt_node, *workflow.nodes[1:]),
        }
    )
    await delegate.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "workflow",
                    workflow.workflow_run_id,
                    workflow.session_id,
                    workflow.version + 1,
                    corrupt.model_dump(mode="json"),
                ),
            ),
        )
    )
    events_before = await delegate.read_events(after_cursor=0)
    resumed = WorkflowExecutor(
        delegate,
        RuntimeCommands(delegate),
        RunEngine(delegate, LiteLLMGateway._for_test(provider)),
        _agents(),
    )

    with pytest.raises(AgentSDKError) as raised:
        await resumed.resume(workflow.workflow_run_id)

    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "failed to load workflow run"
    assert calls == 0
    assert await delegate.read_events(after_cursor=0) == events_before


class _BlockingNodeReadStore:
    def __init__(self) -> None:
        self.delegate = InMemoryStore()
        self.block_next_node_read = False
        self.node_read_blocked = asyncio.Event()
        self.release_node_read = asyncio.Event()

    async def commit(self, batch: CommitBatch) -> CommitResult:
        return await self.delegate.commit(batch)

    async def read_events(
        self, *, after_cursor: int, session_id: str | None = None
    ) -> list[StoredEvent]:
        return await self.delegate.read_events(
            after_cursor=after_cursor, session_id=session_id
        )

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        if kind == "workflow_node" and self.block_next_node_read:
            self.block_next_node_read = False
            self.node_read_blocked.set()
            await self.release_node_read.wait()
        return await self.delegate.get_snapshot(kind, entity_id)

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


@pytest.mark.asyncio
async def test_get_retries_when_workflow_transitions_between_aggregate_reads() -> None:
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if params["model"] == "fake/planner":
            provider_started.set()
            await release_provider.wait()
        return _chunks("done")

    store = _BlockingNodeReadStore()
    commands = RuntimeCommands(store)
    executor = WorkflowExecutor(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    session = await commands.create_session(workspaces=[])
    handle = await executor.start(session.session_id, _ir())
    await asyncio.wait_for(provider_started.wait(), timeout=1)
    store.block_next_node_read = True
    get_task = asyncio.create_task(executor.get(handle.workflow_run_id))
    await asyncio.wait_for(store.node_read_blocked.wait(), timeout=1)

    release_provider.set()
    await handle.result()
    store.release_node_read.set()
    observed = await asyncio.wait_for(get_task, timeout=1)

    assert observed.status is WorkflowRunStatus.COMPLETED


class _BusyUnrelatedEventStore:
    def __init__(self, delegate: StateStore) -> None:
        self.delegate = delegate

    async def commit(self, batch: CommitBatch) -> CommitResult:
        return await self.delegate.commit(batch)

    async def read_events(
        self, *, after_cursor: int, session_id: str | None = None
    ) -> list[StoredEvent]:
        del session_id
        cursor = after_cursor + 1
        return [
            StoredEvent(
                cursor,
                EventEnvelope.new(
                    type="noise",
                    session_id="ses_noise",
                    run_id=f"run_noise_{cursor}",
                    sequence=1,
                    payload={},
                ),
            )
        ]

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        return await self.delegate.get_snapshot(kind, entity_id)

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


@pytest.mark.asyncio
async def test_events_final_drain_ends_with_busy_unrelated_store() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _chunks("done")

    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    executor = WorkflowExecutor(
        delegate,
        commands,
        RunEngine(delegate, LiteLLMGateway._for_test(provider)),
        _agents(),
    )
    session = await commands.create_session(workspaces=[])
    original = await executor.start(session.session_id, _ir())
    await original.result()
    durable = await delegate.read_events(after_cursor=0)
    terminal_cursor = max(event.cursor for event in durable)
    handle = WorkflowHandle(
        original.workflow_run_id,
        _BusyUnrelatedEventStore(delegate),
        original._task,  # type: ignore[attr-defined]
    )

    observed = await asyncio.wait_for(
        _collect_workflow_events(handle, cursor=terminal_cursor),
        timeout=0.2,
    )

    assert observed == []


async def _collect_workflow_events(
    handle: WorkflowHandle,
    *,
    cursor: int,
) -> list[StoredEvent]:
    return [event async for event in handle.events(cursor=cursor)]
