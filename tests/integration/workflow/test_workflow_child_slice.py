from __future__ import annotations

import asyncio
import hashlib
import json
import traceback
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentNode,
    AgentSpec,
    ChildResult,
    ErrorCode,
    TaskEnvelope,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowHandle,
    WorkflowIR,
    WorkflowResult,
)
from agent_sdk.models.litellm_gateway import LiteLLMGateway
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.models import RunSnapshot, RunStatus
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.workflow import (
    WorkflowCompiler,
    WorkflowExecutor,
    WorkflowNodeStatus,
    WorkflowRunStatus,
)


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


@pytest.mark.asyncio
async def test_sequential_workflow_runs_parent_then_isolated_child() -> None:
    calls: list[dict[str, Any]] = []

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        calls.append(params)
        return _chunks("plan-secret" if params["model"] == "fake/planner" else "verified")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    agents = AgentRegistry()
    agents.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    executor = WorkflowExecutor(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        agents,
    )
    session = await commands.create_session(workspaces=[])
    ir = WorkflowCompiler().compile_yaml(
        __import__("yaml").safe_dump(DEFINITION, sort_keys=False)
    )

    handle = await executor.start(session.session_id, ir)
    result = await handle.result()
    snapshot = await executor.get(handle.workflow_run_id)

    assert result.status is WorkflowRunStatus.COMPLETED
    assert snapshot.status is WorkflowRunStatus.COMPLETED
    assert tuple(node.node_id for node in snapshot.nodes) == ("plan", "verify")
    assert all(node.status is WorkflowNodeStatus.COMPLETED for node in snapshot.nodes)
    assert result.output_text == "verified"
    assert result.usage.total_tokens == 6
    parent_run = RunSnapshot.model_validate(
        await store.get_snapshot("run", snapshot.nodes[0].run_id or "")
    )
    child_run = RunSnapshot.model_validate(
        await store.get_snapshot("run", snapshot.nodes[1].run_id or "")
    )
    assert parent_run.status is RunStatus.COMPLETED
    assert child_run.status is RunStatus.COMPLETED
    assert child_run.parent_run_id == parent_run.run_id
    assert child_run.workflow_run_id == handle.workflow_run_id
    assert child_run.workflow_node_id == "verify"
    child_request = str(calls[1]["messages"])
    assert "verify independently" in child_request
    assert "artifact:plan" in child_request
    assert "plan-secret" not in child_request
    assert "make a plan" not in child_request

    event_types = [event.event.type async for event in handle.events()]
    assert event_types == [
        "workflow.started",
        "workflow.node.started",
        "workflow.node.completed",
        "workflow.node.started",
        "workflow.node.completed",
        "workflow.completed",
    ]
    workflow_events = [
        event
        for event in await store.read_events(after_cursor=0)
        if event.event.run_id == handle.workflow_run_id
    ]
    assert [event.event.sequence for event in workflow_events] == list(range(1, 7))


@pytest.mark.asyncio
async def test_public_facade_registers_agents_and_runs_workflow() -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _chunks("done")

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    with pytest.raises(AgentSDKError) as duplicate:
        sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    assert duplicate.value.code is ErrorCode.CONFLICT

    session = await sdk.sessions.create(workspaces=[])
    definition = WorkflowDefinition.model_validate(DEFINITION)
    handle = await sdk.workflows.start(session.session_id, definition)
    result = await handle.result()

    assert isinstance(handle, WorkflowHandle)
    assert isinstance(result, WorkflowResult)
    assert isinstance((await sdk.workflows.get(handle.workflow_run_id)).workflow, WorkflowIR)
    assert isinstance(TaskEnvelope(objective="public"), TaskEnvelope)
    assert ChildResult.model_fields["usage"] is not None
    assert calls == 2
    await sdk.close()


@pytest.mark.asyncio
async def test_unknown_agent_fails_before_execution_and_close_waits_for_child() -> None:
    child_started = asyncio.Event()
    release_child = asyncio.Event()
    calls: list[str] = []

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        model = str(params["model"])
        calls.append(model)
        if model == "fake/worker":
            child_started.set()
            await release_child.wait()
        return _chunks("done")

    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    sdk.agents.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    session = await sdk.sessions.create(workspaces=[])
    with pytest.raises(AgentSDKError) as unknown:
        await sdk.workflows.start(session.session_id, WorkflowDefinition.model_validate(DEFINITION))
    assert unknown.value.code is ErrorCode.NOT_FOUND
    assert calls == []

    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    handle = await sdk.workflows.start(
        session.session_id, WorkflowDefinition.model_validate(DEFINITION)
    )
    await asyncio.wait_for(child_started.wait(), timeout=1)
    close_task = asyncio.create_task(sdk.close())
    await asyncio.sleep(0)
    assert not close_task.done()

    with pytest.raises(AgentSDKError) as closing:
        await sdk.workflows.start(
            session.session_id, WorkflowDefinition.model_validate(DEFINITION)
        )
    assert closing.value.code is ErrorCode.INVALID_STATE
    assert closing.value.message == "SDK is closing"

    release_child.set()
    await asyncio.wait_for(close_task, timeout=1)
    assert (await handle.result()).output_text == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_definition",
    [
        "secret-document: [unterminated",
        WorkflowDefinition.model_validate(
            {
                **DEFINITION,
                "nodes": [*DEFINITION["nodes"], {
                    "id": "other",
                    "kind": "agent",
                    "agent_revision": "worker:1",
                    "input": "other",
                }],
                "edges": [
                    {"source": "plan", "target": "verify"},
                    {"source": "plan", "target": "other"},
                ],
            }
        ),
    ],
)
async def test_public_facade_sanitizes_invalid_workflow_definitions(
    invalid_definition: str | WorkflowDefinition,
) -> None:
    calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _chunks("unused")

    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    session = await sdk.sessions.create(workspaces=[])

    with pytest.raises(AgentSDKError) as raised:
        await sdk.workflows.start(session.session_id, invalid_definition)

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "workflow definition is invalid"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "secret-document" not in "".join(
        traceback.format_exception(raised.value)
    )
    assert calls == 0
    assert not any(
        event.event.type == "workflow.started"
        for event in await store.read_events(after_cursor=0)
    )
    await sdk.close()


@pytest.mark.asyncio
async def test_workflow_result_waiter_cancellation_does_not_cancel_execution() -> None:
    child_started = asyncio.Event()
    release_child = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if params["model"] == "fake/worker":
            child_started.set()
            await release_child.wait()
        return _chunks("done")

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.workflows.start(
        session.session_id, WorkflowDefinition.model_validate(DEFINITION)
    )
    waiter = asyncio.create_task(handle.result())
    await asyncio.wait_for(child_started.wait(), timeout=1)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release_child.set()

    assert (await asyncio.wait_for(handle.result(), timeout=1)).output_text == "done"
    await sdk.close()


@pytest.mark.asyncio
async def test_live_events_final_drain_yields_success_tail_after_task_finishes() -> None:
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if params["model"] == "fake/planner":
            provider_started.set()
            await release_provider.wait()
        return _chunks("done")

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.workflows.start(
        session.session_id, WorkflowDefinition.model_validate(DEFINITION)
    )
    events = handle.events()
    assert (await anext(events)).event.type == "workflow.started"
    assert (await anext(events)).event.type == "workflow.node.started"
    await asyncio.wait_for(provider_started.wait(), timeout=1)

    release_provider.set()
    await handle.result()
    tail = [stored.event.type async for stored in events]

    assert tail[-1] == "workflow.completed"
    assert tail.count("workflow.node.completed") == 2
    await sdk.close()


@pytest.mark.asyncio
async def test_live_events_final_drain_yields_failure_tail_before_task_error() -> None:
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        provider_started.set()
        await release_provider.wait()
        raise RuntimeError("RAW_LIVE_FAILURE")

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.workflows.start(
        session.session_id, WorkflowDefinition.model_validate(DEFINITION)
    )
    events = handle.events()
    assert (await anext(events)).event.type == "workflow.started"
    assert (await anext(events)).event.type == "workflow.node.started"
    await asyncio.wait_for(provider_started.wait(), timeout=1)

    release_provider.set()
    with pytest.raises(AgentSDKError):
        await handle.result()
    tail = [stored.event.type async for stored in events]

    assert tail[-2:] == ["workflow.node.failed", "workflow.failed"]
    await sdk.close()


@pytest.mark.asyncio
async def test_public_facade_revalidates_constructed_ir_before_any_write() -> None:
    calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _chunks("unused")

    nodes = tuple(
        AgentNode(
            id=node_id,
            agent_revision=f"{node_id}:1",
            input=node_id,
        )
        for node_id in ("root", "left", "right")
    )
    edges = (
        WorkflowEdge(source="root", target="left"),
        WorkflowEdge(source="root", target="right"),
    )
    content = {
        "schema_version": 1,
        "name": "unsafe",
        "nodes": [node.model_dump(mode="json") for node in nodes],
        "edges": [edge.model_dump(mode="json") for edge in edges],
    }
    unsafe = WorkflowIR.model_construct(
        schema_version=1,
        name="unsafe",
        nodes=nodes,
        edges=edges,
        definition_hash=hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    session = await sdk.sessions.create(workspaces=[])

    with pytest.raises(AgentSDKError) as raised:
        await sdk.workflows.start(session.session_id, unsafe)

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "workflow IR is invalid"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert calls == 0
    assert not any(
        event.event.type == "workflow.started"
        for event in await store.read_events(after_cursor=0)
    )
    await sdk.close()


@pytest.mark.asyncio
async def test_public_facade_sanitizes_unserializable_constructed_ir() -> None:
    calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _chunks("unused")

    unsafe = WorkflowIR.model_construct(
        schema_version=1,
        name="unserializable",
        nodes=(object(),),
        edges=(),
        definition_hash="0" * 64,
    )
    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    session = await sdk.sessions.create(workspaces=[])

    with pytest.raises(AgentSDKError) as raised:
        await sdk.workflows.start(session.session_id, unsafe)

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "workflow IR is invalid"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert calls == 0
    assert not any(
        event.event.type == "workflow.started"
        for event in await store.read_events(after_cursor=0)
    )
    await sdk.close()
