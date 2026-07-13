from __future__ import annotations

import asyncio
import traceback
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import AgentSDKError, AgentSpec, ErrorCode
from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.models import RunSnapshot, RunStatus
from agent_sdk.storage.base import CommitBatch, CommitResult, StoredEvent
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.subagents import SubagentService, TaskEnvelope


def _response(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": text}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }

    return chunks()


@pytest.mark.asyncio
async def test_child_is_isolated_related_normal_run_and_reopens_from_sqlite(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        requests.append(params)
        content = str(params["messages"][0]["content"])
        return _response("PARENT-SECRET" if "parent private" in content else "verified")

    database = tmp_path / "child.db"
    store = await SQLiteStore.open(database)
    commands = RuntimeCommands(store)
    engine = RunEngine(store, LiteLLMGateway._for_test(provider))
    registry = AgentRegistry()
    registry.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    service = SubagentService(store, commands, engine, registry)
    session = await commands.create_session(workspaces=[tmp_path])
    parent = await commands.start_run(
        session.session_id,
        agent_revision="planner:1",
        user_input="parent private message",
    )
    await engine.execute(
        parent.run_id,
        ModelRequest(
            model="fake/planner",
            messages=({"role": "user", "content": "parent private message"},),
        ),
    )
    envelope = TaskEnvelope(
        objective="verify independently",
        success_criteria=["return evidence"],
        instructions=["do only the task"],
        evidence_refs=["artifact:first", "artifact:second"],
    )

    child = await service.spawn(
        session_id=session.session_id,
        parent_run_id=parent.run_id,
        workflow_run_id="wfr_1",
        workflow_node_id="verify",
        agent_revision="worker:1",
        task=envelope,
    )
    result = await service.await_result(child.run_id)

    child_request = requests[-1]
    rendered = str(child_request["messages"])
    assert result.output_text == "verified"
    assert child.status is RunStatus.CREATED
    assert "verify independently" in rendered
    assert rendered.index("artifact:first") < rendered.index("artifact:second")
    assert "PARENT-SECRET" not in rendered
    assert "parent private message" not in rendered
    persisted = RunSnapshot.model_validate(await store.get_snapshot("run", child.run_id))
    assert persisted.status is RunStatus.COMPLETED
    assert persisted.parent_run_id == parent.run_id
    assert persisted.workflow_run_id == "wfr_1"
    assert persisted.workflow_node_id == "verify"
    assert persisted.task_envelope == envelope
    events = await store.read_events(after_cursor=0, session_id=session.session_id)
    child_events = [event.event.type for event in events if event.event.run_id == child.run_id]
    assert child_events[0] == "run.created"
    assert child_events[-1] == "run.completed"
    await store.close()

    reopened = await SQLiteStore.open(database)
    try:
        recovered = RunSnapshot.model_validate(await reopened.get_snapshot("run", child.run_id))
        assert recovered.parent_run_id == parent.run_id
        assert recovered.task_envelope == envelope
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_child_cancellation_propagates_without_completed_result() -> None:
    provider_started = asyncio.Event()

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            provider_started.set()
            await asyncio.Event().wait()
            yield {"choices": []}

        return chunks()

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    service = SubagentService(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        registry,
    )
    session = await commands.create_session(workspaces=[])
    parent = await commands.start_run(
        session.session_id, agent_revision="planner:1", user_input="parent"
    )
    child = await service.spawn(
        session_id=session.session_id,
        parent_run_id=parent.run_id,
        workflow_run_id="wfr_1",
        workflow_node_id="child",
        agent_revision="worker:1",
        task=TaskEnvelope(objective="wait"),
    )
    waiter = asyncio.create_task(service.await_result(child.run_id))
    await asyncio.wait_for(provider_started.wait(), timeout=1)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)
    snapshot = RunSnapshot.model_validate(await store.get_snapshot("run", child.run_id))
    assert snapshot.status is RunStatus.RUNNING
    events = await store.read_events(after_cursor=0)
    assert not any(
        event.event.run_id == child.run_id and event.event.type == "run.completed"
        for event in events
    )
    assert child.run_id not in service._tasks  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_spawn_rejects_missing_or_cross_session_parent_run() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    service = SubagentService(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        registry,
    )
    first = await commands.create_session(workspaces=[])
    second = await commands.create_session(workspaces=[])
    parent = await commands.start_run(
        first.session_id, agent_revision="planner:1", user_input="parent"
    )

    for session_id, parent_run_id in (
        (second.session_id, parent.run_id),
        (first.session_id, "run_missing"),
    ):
        with pytest.raises(AgentSDKError) as raised:
            await service.spawn(
                session_id=session_id,
                parent_run_id=parent_run_id,
                workflow_run_id="wfr_1",
                workflow_node_id="child",
                agent_revision="worker:1",
                task=TaskEnvelope(objective="work"),
            )
        assert raised.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_completed_child_task_is_released_and_result_comes_from_snapshot() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("done")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    service = SubagentService(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        registry,
    )
    session = await commands.create_session(workspaces=[])
    parent = await commands.start_run(
        session.session_id, agent_revision="planner:1", user_input="parent"
    )
    child = await service.spawn(
        session_id=session.session_id,
        parent_run_id=parent.run_id,
        workflow_run_id="wfr_1",
        workflow_node_id="child",
        agent_revision="worker:1",
        task=TaskEnvelope(objective="work"),
    )

    assert (await service.await_result(child.run_id)).output_text == "done"
    await asyncio.sleep(0)
    assert child.run_id not in service._tasks  # type: ignore[attr-defined]
    assert (await service.await_result(child.run_id)).output_text == "done"


class _BlockingRunCreatedStore:
    def __init__(self) -> None:
        self.delegate = InMemoryStore()
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(event.type == "run.created" for event in batch.events):
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
async def test_start_run_delete_race_cannot_resurrect_session_data() -> None:
    store = _BlockingRunCreatedStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    task = asyncio.create_task(
        commands.start_run(
            session.session_id,
            agent_revision="worker:1",
            user_input="race",
        )
    )
    await asyncio.wait_for(store.blocked.wait(), timeout=1)
    await store.delete_session(session.session_id)
    store.release.set()

    with pytest.raises(AgentSDKError) as raised:
        await task
    assert raised.value.code is ErrorCode.NOT_FOUND
    assert await store.read_events(after_cursor=0) == []


@pytest.mark.asyncio
async def test_all_run_emits_require_the_session_to_still_exist() -> None:
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            provider_started.set()
            await release_provider.wait()
            yield {"choices": [{"delta": {"content": "late"}}]}

        return chunks()

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id, agent_revision="worker:1", user_input="race"
    )
    task = asyncio.create_task(
        RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
            run.run_id,
            ModelRequest(model="fake/worker", messages=({"role": "user", "content": "x"},)),
        )
    )
    await asyncio.wait_for(provider_started.wait(), timeout=1)
    await store.delete_session(session.session_id)
    release_provider.set()

    with pytest.raises(AgentSDKError) as raised:
        await task
    assert raised.value.code is ErrorCode.NOT_FOUND
    assert await store.read_events(after_cursor=0) == []


@pytest.mark.parametrize("failure_mode", ["provider", "malformed"])
@pytest.mark.parametrize("await_after_callback", [False, True])
@pytest.mark.asyncio
async def test_direct_child_failure_is_durable_stable_and_sanitized(
    failure_mode: str,
    await_after_callback: bool,
) -> None:
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        provider_started.set()
        await release_provider.wait()
        if failure_mode == "provider":
            raise RuntimeError("RAW_DIRECT_CHILD_PROVIDER_SECRET")

        async def malformed() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [{"delta": {"content": "partial"}}],
                "usage": {
                    "prompt_tokens": "RAW_DIRECT_CHILD_MALFORMED_SECRET"
                },
            }

        return malformed()

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    service = SubagentService(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        registry,
    )
    session = await commands.create_session(workspaces=[])
    parent = await commands.start_run(
        session.session_id, agent_revision="planner:1", user_input="parent"
    )
    child = await service.spawn(
        session_id=session.session_id,
        parent_run_id=parent.run_id,
        workflow_run_id="wfr_failure",
        workflow_node_id="child",
        agent_revision="worker:1",
        task=TaskEnvelope(objective="fail safely"),
    )
    await asyncio.wait_for(provider_started.wait(), timeout=1)
    waiter: asyncio.Task[object] | None = None
    if not await_after_callback:
        waiter = asyncio.create_task(service.await_result(child.run_id))
        await asyncio.sleep(0)
    release_provider.set()
    if await_after_callback:
        while child.run_id in service._tasks:  # type: ignore[attr-defined]
            await asyncio.sleep(0)

    with pytest.raises(AgentSDKError) as raised:
        if waiter is None:
            await service.await_result(child.run_id)
        else:
            await waiter

    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "model call failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    formatted = "".join(traceback.format_exception(raised.value))
    assert "RAW_DIRECT_CHILD_PROVIDER_SECRET" not in formatted
    assert "RAW_DIRECT_CHILD_MALFORMED_SECRET" not in formatted
    for frame in _sdk_traceback_frames(raised.value):
        assert not any(
            "RAW_DIRECT_CHILD_PROVIDER_SECRET" in repr(value)
            or "RAW_DIRECT_CHILD_MALFORMED_SECRET" in repr(value)
            for value in frame.f_locals.values()
        )
    persisted = RunSnapshot.model_validate(await store.get_snapshot("run", child.run_id))
    assert persisted.status is RunStatus.FAILED
    assert persisted.error is not None
    assert persisted.error.message == "model call failed"


def _sdk_traceback_frames(error: BaseException) -> list[Any]:
    frames: list[Any] = []
    cursor = error.__traceback__
    while cursor is not None:
        normalized = cursor.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in normalized:
            frames.append(cursor.tb_frame)
        cursor = cursor.tb_next
    return frames


@pytest.mark.asyncio
async def test_direct_child_preserves_parameterized_cancelled_error_instance() -> None:
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    expected = asyncio.CancelledError("child-stop", 7)

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        provider_started.set()
        await release_provider.wait()
        raise expected

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    service = SubagentService(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        registry,
    )
    session = await commands.create_session(workspaces=[])
    parent = await commands.start_run(
        session.session_id, agent_revision="planner:1", user_input="parent"
    )
    child = await service.spawn(
        session_id=session.session_id,
        parent_run_id=parent.run_id,
        workflow_run_id="wfr_cancel",
        workflow_node_id="child",
        agent_revision="worker:1",
        task=TaskEnvelope(objective="cancel"),
    )
    waiter = asyncio.create_task(service.await_result(child.run_id))
    await asyncio.wait_for(provider_started.wait(), timeout=1)
    release_provider.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await waiter

    assert raised.value is expected
    assert raised.value.args == ("child-stop", 7)
