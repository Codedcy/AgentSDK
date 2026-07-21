from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    WorkflowDefinition,
)
from agent_sdk.config import AgentSDKConfig
from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
)
from agent_sdk.runtime.failures import RunFailure
from agent_sdk.runtime.models import RunFailure as ModelsRunFailure, RunStatus
from agent_sdk.storage.base import CommitBatch, CommitResult, SnapshotWrite, StoredEvent
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.subagents import (
    ChildCoordinator,
    ChildLimits,
    SubagentService,
    TaskEnvelope,
)
from agent_sdk.subagents import models as child_models
from agent_sdk.tools.models import ToolSpec
from agent_sdk.tools.registry import ToolRegistry


def _response(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": text}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    return chunks()


def _tool(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=name, input_schema={"type": "object"})


async def _unused_tool() -> dict[str, object]:
    return {}


@pytest.mark.asyncio
async def test_normal_parent_run_spawns_child_without_workflow_identity() -> None:
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
        session.session_id,
        agent_revision="planner:1",
        user_input="private parent conversation",
    )

    child = await service.spawn(
        session_id=session.session_id,
        parent_run_id=parent.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(objective="perform isolated work"),
    )

    result = await service.await_result(child.run_id)
    assert child.status is RunStatus.CREATED
    assert child.workflow_run_id is None
    assert child.workflow_node_id is None
    assert result.output_text == "done"


def test_child_limit_and_progress_models_are_public_configuration() -> None:
    assert hasattr(child_models, "ChildLimits")
    assert hasattr(child_models, "ChildProgress")
    assert hasattr(child_models, "ChildWaitResult")
    config = AgentSDKConfig(database_path="children.db")
    assert config.child_limits.max_depth == 3  # type: ignore[attr-defined]


def test_child_coordinator_module_is_available() -> None:
    assert find_spec("agent_sdk.subagents.coordinator") is not None


def test_run_failure_remains_available_from_runtime_models() -> None:
    assert ModelsRunFailure is RunFailure


@pytest.mark.asyncio
async def test_child_capabilities_are_non_expanding_four_way_intersection(
    tmp_path: Path,
) -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("done")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    tools = ToolRegistry()
    for name in ("bash", "read", "write"):
        tools.register(_tool(name), _unused_tool)
    registry = AgentRegistry()
    registry.define(
        AgentSpec(
            name="worker",
            revision="1",
            model="fake/worker",
            tool_allowlist=("read", "write"),
            workspace_allowlist=(str(tmp_path / "parent" / "evidence" / "nested"),),
        )
    )
    engine = RunEngine(store, LiteLLMGateway._for_test(provider), tools)
    service = SubagentService(store, commands, engine, registry, tools=tools)
    session = await commands.create_session(workspaces=[tmp_path])
    parent_agent = AgentSpec(name="parent", revision="1", model="fake/parent")
    parent_messages = ({"role": "user", "content": "private"},)
    parent_catalog = tools.select(("read", "write"))
    parent_descriptor = ExecutionDescriptor.create(
        agent=parent_agent,
        messages=parent_messages,
        tools=tuple(
            ToolCapabilityDescriptor.from_spec(spec) for spec in parent_catalog.list()
        ),
        workspace_scopes=(str(tmp_path / "parent"),),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="parent:1",
            user_input="private",
            execution_descriptor=parent_descriptor,
        )
    ).value

    child = await service.spawn(
        session_id=session.session_id,
        parent_run_id=parent.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(
            objective="inspect evidence",
            allowed_tools=("bash", "read"),
            workspace_scopes=(str(tmp_path / "parent" / "evidence"),),
        ),
    )

    assert child.execution_descriptor is not None
    assert tuple(
        capability.spec.name for capability in child.execution_descriptor.tools
    ) == ("read",)
    assert child.execution_descriptor.workspace_scopes == (
        str((tmp_path / "parent" / "evidence" / "nested").resolve()),
    )
    await service.await_result(child.run_id)

    empty = await service.spawn(
        session_id=session.session_id,
        parent_run_id=parent.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(
            objective="no capabilities",
            allowed_tools=(),
            workspace_scopes=(),
        ),
    )
    assert empty.execution_descriptor is not None
    assert empty.execution_descriptor.tools == ()
    assert empty.execution_descriptor.workspace_scopes == ()
    await service.await_result(empty.run_id)


@pytest.mark.asyncio
async def test_legacy_parent_cannot_expand_restricted_ancestor_tools() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("done")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    tools = ToolRegistry()
    for name in ("read", "write"):
        tools.register(_tool(name), _unused_tool)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    engine = RunEngine(store, LiteLLMGateway._for_test(provider), tools)
    coordinator_type = getattr(
        import_module("agent_sdk.subagents.coordinator"),
        "ChildCoordinator",
    )
    coordinator = coordinator_type(
        store,
        commands,
        engine,
        registry,
        tools=tools,
    )
    session = await commands.create_session(workspaces=[])
    root_agent = AgentSpec(name="root", revision="1", model="fake/root")
    root_messages = ({"role": "user", "content": "root"},)
    root_catalog = tools.select(("read",))
    root_descriptor = ExecutionDescriptor.create(
        agent=root_agent,
        messages=root_messages,
        tools=tuple(
            ToolCapabilityDescriptor.from_spec(spec) for spec in root_catalog.list()
        ),
        workspace_scopes=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    root = (
        await commands.start_run(
            session.session_id,
            agent_revision="root:1",
            user_input="root",
            execution_descriptor=root_descriptor,
        )
    ).value
    legacy_middle = (
        await commands.start_run(
            session.session_id,
            agent_revision="legacy:1",
            user_input="legacy middle",
            parent_run_id=root.run_id,
            task_envelope=TaskEnvelope(objective="legacy middle"),
        )
    ).value

    child = await coordinator.spawn(
        parent_run_id=legacy_middle.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(objective="cannot expand"),
    )

    assert child.execution_descriptor is not None
    assert tuple(
        capability.spec.name for capability in child.execution_descriptor.tools
    ) == ("read",)
    await coordinator.await_result(child.run_id)


@pytest.mark.asyncio
async def test_children_per_parent_limit_rejects_before_run_creation() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("done")

    coordinator_type = getattr(
        import_module("agent_sdk.subagents.coordinator"),
        "ChildCoordinator",
    )
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    engine = RunEngine(store, LiteLLMGateway._for_test(provider))
    coordinator = coordinator_type(
        store,
        commands,
        engine,
        registry,
        limits=ChildLimits(max_children_per_parent=1),
    )
    session = await commands.create_session(workspaces=[])
    parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="planner:1",
            user_input="parent",
        )
    ).value

    first = await coordinator.spawn(
        parent_run_id=parent.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(objective="first"),
    )
    before = tuple(
        stored
        for stored in await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        if stored.event.type == "run.created"
    )
    with pytest.raises(AgentSDKError) as raised:
        await coordinator.spawn(
            parent_run_id=parent.run_id,
            agent_revision="worker:1",
            task=TaskEnvelope(objective="second"),
        )
    after = tuple(
        stored
        for stored in await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        if stored.event.type == "run.created"
    )

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "children per parent limit exceeded"
    assert after == before
    await coordinator.await_result(first.run_id)


@pytest.mark.asyncio
async def test_concurrency_limit_keeps_excess_child_durably_queued() -> None:
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        content = str(params["messages"])
        if '"objective":"first"' in content:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return _response("done")

    coordinator_type = getattr(
        import_module("agent_sdk.subagents.coordinator"),
        "ChildCoordinator",
    )
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    coordinator = coordinator_type(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        registry,
        limits=ChildLimits(max_concurrent_children=1),
    )
    session = await commands.create_session(workspaces=[])
    parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="planner:1",
            user_input="parent",
        )
    ).value
    first = await coordinator.spawn(
        parent_run_id=parent.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(objective="first"),
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = await coordinator.spawn(
        parent_run_id=parent.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(objective="second"),
    )
    try:
        await asyncio.sleep(0.05)
        progress = await coordinator.list(parent.run_id)
        by_id = {item.run_id: item for item in progress}
        assert by_id[first.run_id].status == "running"
        assert by_id[second.run_id].status == "queued"
        assert not second_started.is_set()
    finally:
        release_first.set()
        await coordinator.await_result(first.run_id)
        await coordinator.await_result(second.run_id)
    assert second_started.is_set()


@pytest.mark.asyncio
async def test_depth_limit_rejects_before_grandchild_run_creation() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("done")

    coordinator_type = getattr(
        import_module("agent_sdk.subagents.coordinator"),
        "ChildCoordinator",
    )
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    coordinator = coordinator_type(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        registry,
        limits=ChildLimits(max_depth=1),
    )
    session = await commands.create_session(workspaces=[])
    parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="planner:1",
            user_input="parent",
        )
    ).value
    child = await coordinator.spawn(
        parent_run_id=parent.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(objective="child"),
    )
    await coordinator.await_result(child.run_id)
    before = await store.read_events(after_cursor=0, session_id=session.session_id)

    with pytest.raises(AgentSDKError, match="child depth limit exceeded"):
        await coordinator.spawn(
            parent_run_id=child.run_id,
            agent_revision="worker:1",
            task=TaskEnvelope(objective="grandchild"),
        )

    assert await store.read_events(
        after_cursor=0,
        session_id=session.session_id,
    ) == before


@pytest.mark.asyncio
async def test_session_child_limit_counts_across_different_parents() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("done")

    coordinator_type = getattr(
        import_module("agent_sdk.subagents.coordinator"),
        "ChildCoordinator",
    )
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    coordinator = coordinator_type(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        registry,
        limits=ChildLimits(max_children_per_session=1),
    )
    session = await commands.create_session(workspaces=[])
    first_parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="planner:1",
            user_input="first parent",
        )
    ).value
    second_parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="planner:1",
            user_input="second parent",
        )
    ).value
    child = await coordinator.spawn(
        parent_run_id=first_parent.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(objective="first child"),
    )
    await coordinator.await_result(child.run_id)
    before = await store.read_events(after_cursor=0, session_id=session.session_id)

    with pytest.raises(AgentSDKError, match="children per session limit exceeded"):
        await coordinator.spawn(
            parent_run_id=second_parent.run_id,
            agent_revision="worker:1",
            task=TaskEnvelope(objective="second child"),
        )

    assert await store.read_events(
        after_cursor=0,
        session_id=session.session_id,
    ) == before


@pytest.mark.asyncio
async def test_public_child_api_wait_timeout_does_not_cancel_child() -> None:
    child_started = asyncio.Event()
    release_child = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if '"objective":"slow child"' in str(params["messages"]):
            child_started.set()
            await release_child.wait()
            return _response("child complete")
        return _response("parent complete")

    store = InMemoryStore()
    sdk = AgentSDK.for_test(
        acompletion=provider,
        store=store,
        enable_builtin_tools=False,
    )
    parent_agent = AgentSpec(name="parent", revision="1", model="fake/parent")
    sdk.agents.define(parent_agent)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    parent_handle = await sdk.runs.start(session.session_id, parent_agent, "parent")
    await parent_handle.result()

    assert hasattr(sdk, "children")
    child = await sdk.children.spawn(
        parent_handle.run_id,
        "worker:1",
        TaskEnvelope(objective="slow child"),
    )
    await asyncio.wait_for(child_started.wait(), timeout=1)
    pending = await sdk.children.wait(child.run_id, timeout_seconds=0.01)
    assert pending.status == "pending"
    assert not release_child.is_set()

    release_child.set()
    completed = await sdk.children.wait(child.run_id, timeout_seconds=1)
    assert completed.status == "completed"
    assert completed.result is not None
    assert completed.result.output_text == "child complete"
    progress = await sdk.children.list(parent_handle.run_id)
    assert tuple(item.status for item in progress) == ("completed",)
    await sdk.close()


@pytest.mark.asyncio
async def test_public_child_api_returns_failed_child_as_wait_result() -> None:
    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if '"objective":"fail child"' in str(params["messages"]):
            raise RuntimeError("private provider detail")
        return _response("parent complete")

    sdk = AgentSDK.for_test(
        acompletion=provider,
        store=InMemoryStore(),
        enable_builtin_tools=False,
    )
    parent_agent = AgentSpec(name="parent", revision="1", model="fake/parent")
    sdk.agents.define(parent_agent)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    parent = await sdk.runs.start(session.session_id, parent_agent, "parent")
    await parent.result()
    child = await sdk.children.spawn(
        parent.run_id,
        "worker:1",
        TaskEnvelope(objective="fail child"),
    )

    failed = await sdk.children.wait(child.run_id, timeout_seconds=1)

    assert failed.status == "failed"
    assert failed.result is None
    assert failed.error is not None
    assert failed.error.message == "model call failed"
    assert "private provider detail" not in failed.error.message
    await sdk.close()


@pytest.mark.asyncio
async def test_public_send_message_enforces_direct_relation() -> None:
    parent_started = asyncio.Event()
    child_started = asyncio.Event()
    release_parent = asyncio.Event()
    release_child = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        content = str(params["messages"])
        if '"objective":"message child"' in content:
            child_started.set()
            await release_child.wait()
            return _response("child complete")
        parent_started.set()
        await release_parent.wait()
        return _response("parent complete")

    sdk = AgentSDK.for_test(
        acompletion=provider,
        store=InMemoryStore(),
        enable_builtin_tools=False,
    )
    parent_agent = AgentSpec(name="parent", revision="1", model="fake/parent")
    sdk.agents.define(parent_agent)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    parent = await sdk.runs.start(session.session_id, parent_agent, "hold parent")
    await asyncio.wait_for(parent_started.wait(), timeout=1)
    child = await sdk.children.spawn(
        parent.run_id,
        "worker:1",
        TaskEnvelope(objective="message child"),
    )
    await asyncio.wait_for(child_started.wait(), timeout=1)
    try:
        message = await sdk.children.send_message(
            parent.run_id,
            child.run_id,
            "new evidence",
        )
        assert message.sender_run_id == parent.run_id
        assert message.recipient_run_id == child.run_id
        assert message.sequence == 1
        with pytest.raises(AgentSDKError, match="direct parent or child"):
            await sdk.children.send_message(
                parent.run_id,
                parent.run_id,
                "invalid self message",
            )
    finally:
        release_child.set()
        release_parent.set()
        await sdk.children.wait(child.run_id, timeout_seconds=1)
        await parent.result()
        await sdk.close()


@pytest.mark.asyncio
async def test_sqlite_reopen_reads_terminal_child_relation_and_progress(
    tmp_path: Path,
) -> None:
    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if '"objective":"durable child"' in str(params["messages"]):
            return _response("durable result")
        return _response("parent complete")

    database = tmp_path / "children.db"
    first = AgentSDK.for_test(
        acompletion=provider,
        database_path=database,
        enable_builtin_tools=False,
    )
    parent_agent = AgentSpec(name="parent", revision="1", model="fake/parent")
    first.agents.define(parent_agent)
    first.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await first.sessions.create(workspaces=[])
    parent = await first.runs.start(session.session_id, parent_agent, "parent")
    await parent.result()
    child = await first.children.spawn(
        parent.run_id,
        "worker:1",
        TaskEnvelope(objective="durable child"),
    )
    terminal = await first.children.wait(child.run_id, timeout_seconds=1)
    before = await first.children.list(parent.run_id)
    assert terminal.status == "completed"
    await first.close()

    reopened = AgentSDK.for_test(
        acompletion=provider,
        database_path=database,
        enable_builtin_tools=False,
    )
    try:
        restored = await reopened.children.wait(child.run_id, timeout_seconds=0)
        after = await reopened.children.list(parent.run_id)
        assert restored.status == "completed"
        assert restored.result is not None
        assert restored.result.output_text == "durable result"
        assert after == before
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_workflow_and_public_children_share_concurrency_gate() -> None:
    plan_started = asyncio.Event()
    holder_started = asyncio.Event()
    workflow_child_started = asyncio.Event()
    release_plan = asyncio.Event()
    release_holder = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if params["model"] == "fake/holder":
            holder_started.set()
            await release_holder.wait()
            return _response("holder complete")
        if params["model"] == "fake/planner":
            plan_started.set()
            await release_plan.wait()
            return _response("plan complete")
        if params["model"] == "fake/worker":
            workflow_child_started.set()
            return _response("workflow child complete")
        return _response("parent complete")

    sdk = AgentSDK.for_test(
        acompletion=provider,
        store=InMemoryStore(),
        enable_builtin_tools=False,
        child_limits=ChildLimits(max_concurrent_children=1),
    )
    parent_agent = AgentSpec(name="parent", revision="1", model="fake/parent")
    sdk.agents.define(parent_agent)
    sdk.agents.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    sdk.agents.define(AgentSpec(name="holder", revision="1", model="fake/holder"))
    session = await sdk.sessions.create(workspaces=[])
    public_parent = await sdk.runs.start(
        session.session_id,
        parent_agent,
        "public parent",
    )
    await public_parent.result()
    definition = WorkflowDefinition.model_validate(
        {
            "api_version": "agent-sdk/v1",
            "kind": "Workflow",
            "name": "shared-gate",
            "nodes": [
                {
                    "id": "plan",
                    "kind": "agent",
                    "agent_revision": "planner:1",
                    "input": "plan",
                },
                {
                    "id": "execute",
                    "kind": "agent",
                    "agent_revision": "worker:1",
                    "input": "execute",
                    "run_as": "child",
                },
            ],
            "edges": [{"source": "plan", "target": "execute"}],
        }
    )
    workflow = await sdk.workflows.start(session.session_id, definition)
    await asyncio.wait_for(plan_started.wait(), timeout=1)
    holder = await sdk.children.spawn(
        public_parent.run_id,
        "holder:1",
        TaskEnvelope(objective="gate holder"),
    )
    await asyncio.wait_for(holder_started.wait(), timeout=1)
    try:
        release_plan.set()
        workflow_snapshot = None
        for _ in range(200):
            workflow_snapshot = await sdk.workflows.get(workflow.workflow_run_id)
            if workflow_snapshot.nodes[1].run_id is not None:
                break
            await asyncio.sleep(0.01)
        assert workflow_snapshot is not None
        assert workflow_snapshot.nodes[1].run_id is not None
        root_run_id = workflow_snapshot.nodes[0].run_id
        assert root_run_id is not None
        await asyncio.sleep(0.05)
        progress = await sdk.children.list(root_run_id)
        assert tuple(item.status for item in progress) == ("queued",)
        assert not workflow_child_started.is_set()
    finally:
        release_plan.set()
        release_holder.set()
        await sdk.children.wait(holder.run_id, timeout_seconds=1)
        workflow_result = await workflow.result()
        await sdk.close()
    assert workflow_result.output_text == "workflow child complete"
    assert workflow_child_started.is_set()


@pytest.mark.asyncio
async def test_corrupt_child_agent_revision_rejects_before_run_creation() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    coordinator_type = getattr(
        import_module("agent_sdk.subagents.coordinator"),
        "ChildCoordinator",
    )
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    registry._agents["worker:1"] = {"damaged": True}  # type: ignore[assignment]
    coordinator = coordinator_type(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        registry,
    )
    session = await commands.create_session(workspaces=[])
    parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="planner:1",
            user_input="parent",
        )
    ).value
    before = await store.read_events(after_cursor=0, session_id=session.session_id)

    with pytest.raises(AgentSDKError) as raised:
        await coordinator.spawn(
            parent_run_id=parent.run_id,
            agent_revision="worker:1",
            task=TaskEnvelope(objective="must not start"),
        )

    assert raised.value.code is ErrorCode.INTERNAL
    assert await store.read_events(
        after_cursor=0,
        session_id=session.session_id,
    ) == before


@pytest.mark.asyncio
async def test_deleted_session_children_do_not_consume_new_session_limit() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("done")

    coordinator_type = getattr(
        import_module("agent_sdk.subagents.coordinator"),
        "ChildCoordinator",
    )
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    engine = RunEngine(store, LiteLLMGateway._for_test(provider))
    coordinator = coordinator_type(
        store,
        commands,
        engine,
        registry,
        limits=ChildLimits(max_children_per_session=1),
    )
    first_session = await commands.create_session(workspaces=[])
    first_parent = (
        await commands.start_run(
            first_session.session_id,
            agent_revision="planner:1",
            user_input="first parent",
        )
    ).value
    await engine.execute(
        first_parent.run_id,
        ModelRequest(
            model="fake/planner",
            messages=({"role": "user", "content": "first parent"},),
        ),
    )
    first_child = await coordinator.spawn(
        parent_run_id=first_parent.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(objective="first child"),
    )
    await coordinator.await_result(first_child.run_id)
    await commands.close_session(first_session.session_id)
    await commands.delete_session(first_session.session_id)

    second_session = await commands.create_session(workspaces=[])
    second_parent = (
        await commands.start_run(
            second_session.session_id,
            agent_revision="planner:1",
            user_input="second parent",
        )
    ).value
    second_child = await coordinator.spawn(
        parent_run_id=second_parent.run_id,
        agent_revision="worker:1",
        task=TaskEnvelope(objective="second child"),
    )

    assert (await coordinator.await_result(second_child.run_id)).output_text == "done"


@pytest.mark.asyncio
async def test_recovery_start_is_bounded_by_wait_timeout_and_reused() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    registry = AgentRegistry()
    tracked: list[asyncio.Task[Any]] = []
    coordinator = ChildCoordinator(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        registry,
        limits=ChildLimits(max_wait_seconds=0.01),
        track_task=tracked.append,
    )
    session = await commands.create_session(workspaces=[])
    parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="parent:1",
            user_input="parent",
        )
    ).value
    child = (
        await commands.start_run(
            session.session_id,
            agent_revision="worker:1",
            user_input="child",
            parent_run_id=parent.run_id,
            task_envelope=TaskEnvelope(objective="recover"),
        )
    ).value
    recovery_started = asyncio.Event()
    release_recovery = asyncio.Event()
    recovery_calls = 0

    class _RecoveredHandle:
        async def result(self) -> Any:
            return None

    async def recover(run_id: str) -> Any:
        nonlocal recovery_calls
        assert run_id == child.run_id
        recovery_calls += 1
        recovery_started.set()
        await release_recovery.wait()
        return _RecoveredHandle()

    coordinator.set_recover_run(recover)
    try:
        zero = await asyncio.wait_for(
            coordinator.wait(child.run_id, timeout_seconds=0),
            timeout=0.2,
        )
        await asyncio.wait_for(recovery_started.wait(), timeout=0.2)
        short = await asyncio.wait_for(
            coordinator.wait(child.run_id, timeout_seconds=0.001),
            timeout=0.2,
        )
        clamped = await asyncio.wait_for(
            coordinator.wait(child.run_id, timeout_seconds=999.0),
            timeout=0.2,
        )
        assert (zero.status, short.status, clamped.status) == (
            "pending",
            "pending",
            "pending",
        )
        assert recovery_calls == 1
        assert len(tracked) == 1
        assert not tracked[0].cancelled()
    finally:
        release_recovery.set()
        if tracked:
            await asyncio.gather(*tracked, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_timeout",
    [True, "1", float("nan"), float("inf"), float("-inf"), -0.1],
)
async def test_wait_rejects_invalid_timeout_values(invalid_timeout: Any) -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    coordinator = ChildCoordinator(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        AgentRegistry(),
    )
    session = await commands.create_session(workspaces=[])
    parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="parent:1",
            user_input="parent",
        )
    ).value
    child = (
        await commands.start_run(
            session.session_id,
            agent_revision="worker:1",
            user_input="child",
            parent_run_id=parent.run_id,
            task_envelope=TaskEnvelope(objective="wait"),
        )
    ).value

    with pytest.raises(AgentSDKError) as raised:
        await coordinator.wait(child.run_id, timeout_seconds=invalid_timeout)

    assert raised.value.code is ErrorCode.INVALID_STATE


@pytest.mark.asyncio
async def test_wait_rejects_missing_cross_session_and_cyclic_parent_chains() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    coordinator = ChildCoordinator(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        AgentRegistry(),
    )
    first = await commands.create_session(workspaces=[])
    second = await commands.create_session(workspaces=[])
    foreign_parent = (
        await commands.start_run(
            second.session_id,
            agent_revision="parent:1",
            user_input="foreign",
        )
    ).value
    missing = (
        await commands.start_run(
            first.session_id,
            agent_revision="worker:1",
            user_input="missing",
            parent_run_id="missing-parent",
            task_envelope=TaskEnvelope(objective="missing"),
        )
    ).value
    cross_session = (
        await commands.start_run(
            first.session_id,
            agent_revision="worker:1",
            user_input="cross",
            parent_run_id=foreign_parent.run_id,
            task_envelope=TaskEnvelope(objective="cross"),
        )
    ).value
    cycle_a = (
        await commands.start_run(
            first.session_id,
            run_id="cycle-a",
            agent_revision="worker:1",
            user_input="cycle a",
            parent_run_id="cycle-b",
            task_envelope=TaskEnvelope(objective="cycle a"),
        )
    ).value
    await commands.start_run(
        first.session_id,
        run_id="cycle-b",
        agent_revision="worker:1",
        user_input="cycle b",
        parent_run_id=cycle_a.run_id,
        task_envelope=TaskEnvelope(objective="cycle b"),
    )

    for child_run_id in (missing.run_id, cross_session.run_id, cycle_a.run_id):
        with pytest.raises(AgentSDKError):
            await coordinator.wait(child_run_id, timeout_seconds=0)


@pytest.mark.asyncio
async def test_wait_rejects_root_run_before_recovery() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    coordinator = ChildCoordinator(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        AgentRegistry(),
    )
    session = await commands.create_session(workspaces=[])
    root = (
        await commands.start_run(
            session.session_id,
            agent_revision="root:1",
            user_input="root",
        )
    ).value
    recovery_calls = 0

    async def recover(_: str) -> Any:
        nonlocal recovery_calls
        recovery_calls += 1
        raise AssertionError("recovery must not start")

    coordinator.set_recover_run(recover)

    with pytest.raises(AgentSDKError, match="run is not a child"):
        await coordinator.wait(root.run_id, timeout_seconds=0)

    assert recovery_calls == 0


@pytest.mark.asyncio
async def test_wait_rejects_corrupt_parent_creation_evidence_and_owner() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    for corrupt in ("event", "owner"):
        store = InMemoryStore()
        commands = RuntimeCommands(store)
        coordinator = ChildCoordinator(
            store,
            commands,
            RunEngine(store, LiteLLMGateway._for_test(provider)),
            AgentRegistry(),
        )
        session = await commands.create_session(workspaces=[])
        parent = (
            await commands.start_run(
                session.session_id,
                agent_revision="parent:1",
                user_input="parent",
            )
        ).value
        child = (
            await commands.start_run(
                session.session_id,
                agent_revision="worker:1",
                user_input="child",
                parent_run_id=parent.run_id,
                task_envelope=TaskEnvelope(objective="corrupt"),
            )
        ).value
        if corrupt == "event":
            for index, stored in enumerate(store._events):
                if stored.event.run_id == parent.run_id:
                    payload = dict(stored.event.payload)
                    payload["agent_revision"] = "tampered:1"
                    store._events[index] = StoredEvent(
                        stored.cursor,
                        stored.event.model_copy(update={"payload": payload}),
                    )
                    break
        else:
            current = store._snapshots[("run", parent.run_id)]
            store._snapshots[("run", parent.run_id)] = SnapshotWrite(
                current.kind,
                current.entity_id,
                "wrong-session-owner",
                current.version,
                current.data,
            )

        with pytest.raises(AgentSDKError):
            await coordinator.wait(child.run_id, timeout_seconds=0)


@pytest.mark.asyncio
async def test_spawn_rejects_corrupt_parent_creation_evidence_and_owner() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    for corrupt in ("event", "owner"):
        store = InMemoryStore()
        commands = RuntimeCommands(store)
        registry = AgentRegistry()
        registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
        coordinator = ChildCoordinator(
            store,
            commands,
            RunEngine(store, LiteLLMGateway._for_test(provider)),
            registry,
        )
        session = await commands.create_session(workspaces=[])
        parent = (
            await commands.start_run(
                session.session_id,
                agent_revision="parent:1",
                user_input="parent",
            )
        ).value
        if corrupt == "event":
            for index, stored in enumerate(store._events):
                if stored.event.run_id == parent.run_id:
                    payload = dict(stored.event.payload)
                    payload["agent_revision"] = "tampered:1"
                    store._events[index] = StoredEvent(
                        stored.cursor,
                        stored.event.model_copy(update={"payload": payload}),
                    )
                    break
        else:
            current = store._snapshots[("run", parent.run_id)]
            store._snapshots[("run", parent.run_id)] = SnapshotWrite(
                current.kind,
                current.entity_id,
                "wrong-session-owner",
                current.version,
                current.data,
            )
        before = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )

        with pytest.raises(AgentSDKError):
            await coordinator.spawn(
                parent_run_id=parent.run_id,
                agent_revision="worker:1",
                task=TaskEnvelope(objective="must not start"),
            )

        assert await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        ) == before


@pytest.mark.asyncio
async def test_wait_expected_parent_mismatch_has_no_recovery_side_effect() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    coordinator = ChildCoordinator(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider)),
        AgentRegistry(),
    )
    session = await commands.create_session(workspaces=[])
    parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="parent:1",
            user_input="parent",
        )
    ).value
    unrelated = (
        await commands.start_run(
            session.session_id,
            agent_revision="other:1",
            user_input="other",
        )
    ).value
    child = (
        await commands.start_run(
            session.session_id,
            agent_revision="worker:1",
            user_input="child",
            parent_run_id=parent.run_id,
            task_envelope=TaskEnvelope(objective="expected parent"),
        )
    ).value
    recovery_calls = 0

    async def recover(_: str) -> Any:
        nonlocal recovery_calls
        recovery_calls += 1
        raise AssertionError("recovery must not start")

    coordinator.set_recover_run(recover)

    with pytest.raises(AgentSDKError):
        await coordinator.wait(
            child.run_id,
            timeout_seconds=0,
            expected_parent_run_id=unrelated.run_id,
        )

    assert recovery_calls == 0


class _AncestorMutationStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self._mutation: tuple[str, ExecutionDescriptor] | None = None

    def arm(self, run_id: str, descriptor: ExecutionDescriptor) -> None:
        self._mutation = (run_id, descriptor)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        mutation = self._mutation
        if mutation is not None and any(
            event.type == "run.created" and event.payload.get("parent_run_id") is not None
            for event in batch.events
        ):
            self._mutation = None
            run_id, descriptor = mutation
            current = self._snapshots[("run", run_id)]
            data = dict(current.data)
            data["execution_descriptor"] = descriptor.model_dump(mode="json")
            self._snapshots[("run", run_id)] = SnapshotWrite(
                current.kind,
                current.entity_id,
                current.session_id,
                current.version,
                data,
            )
        return await super().commit(batch)


@pytest.mark.asyncio
async def test_spawn_binds_every_authenticated_ancestor_snapshot_exactly() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    store = _AncestorMutationStore()
    commands = RuntimeCommands(store)
    tools = ToolRegistry()
    for name in ("read", "write"):
        tools.register(_tool(name), _unused_tool)
    registry = AgentRegistry()
    registry.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    coordinator = ChildCoordinator(
        store,
        commands,
        RunEngine(store, LiteLLMGateway._for_test(provider), tools),
        registry,
        tools=tools,
    )
    session = await commands.create_session(workspaces=[])
    root_agent = AgentSpec(name="root", revision="1", model="fake/root")
    messages = ({"role": "user", "content": "root"},)

    def descriptor(names: tuple[str, ...]) -> ExecutionDescriptor:
        return ExecutionDescriptor.create(
            agent=root_agent,
            messages=messages,
            tools=tuple(
                ToolCapabilityDescriptor.from_spec(spec)
                for spec in tools.select(names).list()
            ),
            workspace_scopes=(),
            policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
        )

    root = (
        await commands.start_run(
            session.session_id,
            agent_revision="root:1",
            user_input="root",
            execution_descriptor=descriptor(("read", "write")),
        )
    ).value
    legacy_middle = (
        await commands.start_run(
            session.session_id,
            agent_revision="legacy:1",
            user_input="legacy",
            parent_run_id=root.run_id,
            task_envelope=TaskEnvelope(objective="legacy"),
        )
    ).value
    before = tuple(
        stored
        for stored in await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        if stored.event.type == "run.created"
    )
    store.arm(root.run_id, descriptor(("read",)))

    with pytest.raises(AgentSDKError) as raised:
        await coordinator.spawn(
            parent_run_id=legacy_middle.run_id,
            agent_revision="worker:1",
            task=TaskEnvelope(objective="must not expand"),
        )

    assert raised.value.code is ErrorCode.CONFLICT
    after = tuple(
        stored
        for stored in await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        if stored.event.type == "run.created"
    )
    assert after == before


@pytest.mark.asyncio
async def test_direct_service_spawn_binds_parent_snapshot_owner() -> None:
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
    session = await commands.create_session(workspaces=[])
    parent = (
        await commands.start_run(
            session.session_id,
            agent_revision="parent:1",
            user_input="parent",
        )
    ).value
    current = store._snapshots[("run", parent.run_id)]
    store._snapshots[("run", parent.run_id)] = SnapshotWrite(
        current.kind,
        current.entity_id,
        "wrong-session-owner",
        current.version,
        current.data,
    )
    before = await store.read_events(after_cursor=0, session_id=session.session_id)

    with pytest.raises(AgentSDKError):
        await service.spawn(
            session_id=session.session_id,
            parent_run_id=parent.run_id,
            agent_revision="worker:1",
            task=TaskEnvelope(objective="owner bound"),
        )

    assert await store.read_events(
        after_cursor=0,
        session_id=session.session_id,
    ) == before
