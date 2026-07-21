from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ChildLimits,
    ErrorCode,
    TaskEnvelope,
)
from agent_sdk.models.litellm_gateway import ToolCallCompleted
from agent_sdk.permissions.broker import InProcessPermissionBridge
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.tools.executor import ToolExecutor
from agent_sdk.tools.models import ToolContext, ToolResult, ToolResultStatus, ToolSpec, thaw_json


def _response(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": text}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    return chunks()


async def _noop_permission(
    _request: PermissionRequest,
    _decision: PermissionDecision | None,
) -> None:
    return None


async def _execute_for_run(
    sdk: AgentSDK,
    *,
    run_id: str,
    session_id: str,
    name: str,
    arguments: dict[str, object] | str,
    policy: PolicyEngine | None = None,
    bridge: InProcessPermissionBridge | None = None,
    transitions: list[tuple[str, PermissionRequest, PermissionDecision | None]]
    | None = None,
    call_id: str | None = None,
) -> tuple[ToolResult, list[tuple[str, dict[str, Any]]]]:
    snapshot = await sdk.runs.get(run_id)
    assert snapshot.execution_descriptor is not None
    catalog = sdk.tools.select(
        capability.spec.name
        for capability in snapshot.execution_descriptor.tools
    )
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        emitted.append((event_type, payload))

    async def requested(
        request: PermissionRequest,
        decision: PermissionDecision | None,
    ) -> None:
        if transitions is not None:
            transitions.append(("permission.requested", request, decision))

    async def resolved(
        request: PermissionRequest,
        decision: PermissionDecision | None,
    ) -> None:
        if transitions is not None:
            transitions.append(("permission.resolved", request, decision))

    result = await ToolExecutor(
        catalog,
        policy or PolicyEngine(default_outcome="allow"),
        bridge,
    ).execute(
        ToolCallCompleted(
            0,
            call_id or f"call-{name}",
            name,
            arguments if isinstance(arguments, str) else json.dumps(arguments),
        ),
        ToolContext(run_id=run_id, session_id=session_id),
        emit=emit,
        on_permission_requested=requested,
        on_permission_resolved=resolved,
    )
    return result, emitted


@pytest.mark.asyncio
async def test_spawn_agent_runs_through_registered_tool_pipeline() -> None:
    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if params["model"] == "test/researcher":
            return _response("one finding")
        return _response("parent ready")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="allow",
    )
    try:
        sdk.agents.define(
            AgentSpec(
                name="researcher",
                revision="1",
                model="test/researcher",
            )
        )
        session = await sdk.sessions.create(workspaces=("D:/work",))
        parent_agent = AgentSpec(name="parent", revision="1", model="test/parent")
        parent = await sdk.runs.start(
            session.session_id,
            parent_agent,
            "prepare",
        )
        await parent.result()
        parent_snapshot = await sdk.runs.get(parent.run_id)
        assert parent_snapshot.execution_descriptor is not None
        result, emitted = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="spawn_agent",
            call_id="call-spawn",
            arguments={
                "agent_revision": "researcher:1",
                "task": {
                    "objective": "Inspect the evidence",
                    "success_criteria": ["return one finding"],
                    "evidence_refs": ["evt-1"],
                    "allowed_tools": ["read"],
                    "workspace_scopes": ["D:/work/evidence"],
                },
            },
        )

        assert result.status is ToolResultStatus.SUCCEEDED
        value = thaw_json(result.value)
        assert value["status"] == "queued"
        assert isinstance(value["child_run_id"], str)
        terminal = await sdk.children.wait(
            value["child_run_id"],
            timeout_seconds=1,
        )
        assert terminal.status == "completed"
        assert [event_type for event_type, _ in emitted] == [
            "tool.call.authorized",
            "tool.call.started",
            "tool.call.completed",
        ]
        assert emitted[-1][1] == result.model_dump(mode="json")
    finally:
        await sdk.close()


def test_child_control_tools_have_exact_builtin_specs() -> None:
    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
    )
    try:
        control_names = {
            "spawn_agent",
            "send_message",
            "wait_child",
            "list_children",
        }
        specs = {
            spec.name: spec
            for spec in sdk.tools.list()
            if spec.name in control_names
        }
        assert set(specs) == control_names
        assert {
            name: (spec.source, spec.effects)
            for name, spec in specs.items()
        } == {
            "spawn_agent": ("builtin", ("agent.spawn",)),
            "send_message": ("builtin", ("agent.message",)),
            "wait_child": ("builtin", ("agent.inspect",)),
            "list_children": ("builtin", ("agent.inspect",)),
        }
        assert all(
            spec.input_schema["additionalProperties"] is False
            for spec in specs.values()
        )
    finally:
        # Construction starts no background work outside an event loop.
        pass


def test_disabling_builtin_tools_also_disables_child_control_tools() -> None:
    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        enable_builtin_tools=False,
    )
    assert sdk.tools.list() == ()


@pytest.mark.asyncio
async def test_send_list_and_wait_use_context_parent_through_tool_pipeline() -> None:
    parent_started = asyncio.Event()
    child_started = asyncio.Event()
    release_parent = asyncio.Event()
    release_child = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if params["model"] == "test/researcher":
            child_started.set()
            await release_child.wait()
            return _response("child finding")
        parent_started.set()
        await release_parent.wait()
        return _response("parent done")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="allow",
    )
    try:
        sdk.agents.define(
            AgentSpec(name="researcher", revision="1", model="test/researcher")
        )
        session = await sdk.sessions.create(workspaces=("D:/work",))
        parent = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="parent", revision="1", model="test/parent"),
            "hold",
        )
        await asyncio.wait_for(parent_started.wait(), timeout=1)
        child = await sdk.children.spawn(
            parent.run_id,
            "researcher:1",
            task=TaskEnvelope(objective="wait for evidence"),
        )
        await asyncio.wait_for(child_started.wait(), timeout=1)

        sent, sent_events = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="send_message",
            arguments={
                "target_run_id": child.run_id,
                "content": "Use source evt-2",
            },
        )
        listed, listed_events = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="list_children",
            arguments={},
        )
        pending, pending_events = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="wait_child",
            arguments={"child_run_id": child.run_id, "timeout_seconds": 0},
        )

        assert sent.status is ToolResultStatus.SUCCEEDED
        assert thaw_json(sent.value)["sender_run_id"] == parent.run_id
        assert thaw_json(sent.value)["recipient_run_id"] == child.run_id
        assert thaw_json(listed.value) == [
            {
                **thaw_json(listed.value)[0],
                "run_id": child.run_id,
                "parent_run_id": parent.run_id,
                "status": "running",
            }
        ]
        assert thaw_json(pending.value) == {
            "child_run_id": child.run_id,
            "status": "pending",
            "result": None,
            "error": None,
        }
        for result, events in (
            (sent, sent_events),
            (listed, listed_events),
            (pending, pending_events),
        ):
            assert [event_type for event_type, _ in events] == [
                "tool.call.authorized",
                "tool.call.started",
                "tool.call.completed",
            ]
            assert events[-1][1] == result.model_dump(mode="json")

        release_child.set()
        terminal, terminal_events = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="wait_child",
            call_id="call-wait-terminal",
            arguments={"child_run_id": child.run_id, "timeout_seconds": 1},
        )
        assert terminal.status is ToolResultStatus.SUCCEEDED
        terminal_value = thaw_json(terminal.value)
        assert terminal_value["status"] == "completed"
        assert terminal_value["result"]["output_text"] == "child finding"
        assert terminal_events[-1][1] == terminal.model_dump(mode="json")
    finally:
        release_child.set()
        release_parent.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_wait_child_returns_failed_child_as_successful_tool_value() -> None:
    parent_started = asyncio.Event()
    release_parent = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if params["model"] == "test/researcher":
            raise RuntimeError("private provider failure")
        parent_started.set()
        await release_parent.wait()
        return _response("parent done")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="allow",
    )
    try:
        sdk.agents.define(
            AgentSpec(name="researcher", revision="1", model="test/researcher")
        )
        session = await sdk.sessions.create(workspaces=())
        parent = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="parent", revision="1", model="test/parent"),
            "hold",
        )
        await asyncio.wait_for(parent_started.wait(), timeout=1)
        child = await sdk.children.spawn(
            parent.run_id,
            "researcher:1",
            task=TaskEnvelope(objective="fail"),
        )

        result, emitted = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="wait_child",
            arguments={"child_run_id": child.run_id, "timeout_seconds": 1},
        )

        assert result.status is ToolResultStatus.SUCCEEDED
        value = thaw_json(result.value)
        assert value["status"] == "failed"
        assert value["error"]["message"] == "model call failed"
        assert "private provider failure" not in result.content
        assert emitted[-1][1] == result.model_dump(mode="json")
    finally:
        release_parent.set()
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("spawn_agent", "not-json"),
        (
            "spawn_agent",
            {
                "agent_revision": "researcher:1",
                "parent_run_id": "model-controlled",
                "task": {"objective": "invalid identity"},
            },
        ),
        (
            "spawn_agent",
            {
                "agent_revision": "researcher:1",
                "task": {"objective": "invalid task", "unexpected": True},
            },
        ),
        ("send_message", {"target_run_id": "x", "content": "y", "sender_run_id": "x"}),
        ("wait_child", {"child_run_id": "x", "parent_run_id": "x"}),
        ("list_children", {"parent_run_id": "model-controlled"}),
    ],
)
async def test_control_tool_schemas_reject_malformed_or_model_supplied_identity(
    name: str,
    arguments: dict[str, object] | str,
) -> None:
    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        return _response("ready")

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    try:
        sdk.agents.define(
            AgentSpec(name="researcher", revision="1", model="test/researcher")
        )
        session = await sdk.sessions.create(workspaces=())
        parent = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="parent", revision="1", model="test/parent"),
            "ready",
        )
        await parent.result()

        result, emitted = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name=name,
            arguments=arguments,
        )

        assert result.status is ToolResultStatus.INVALID_ARGUMENTS
        assert [event_type for event_type, _ in emitted] == ["tool.call.completed"]
        assert emitted[0][1] == result.model_dump(mode="json")
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_spawn_unknown_agent_and_limit_rejection_are_normalized_tool_failures() -> None:
    child_started = asyncio.Event()
    release_child = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        if params["model"] == "test/researcher":
            child_started.set()
            await release_child.wait()
            return _response("done")
        return _response("ready")

    store = InMemoryStore()
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=provider,
        permission_default="allow",
        child_limits=ChildLimits(max_children_per_parent=1),
    )
    try:
        sdk.agents.define(
            AgentSpec(name="researcher", revision="1", model="test/researcher")
        )
        session = await sdk.sessions.create(workspaces=())
        parent = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="parent", revision="1", model="test/parent"),
            "ready",
        )
        await parent.result()
        unknown, unknown_events = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="spawn_agent",
            call_id="call-unknown",
            arguments={
                "agent_revision": "unknown:1",
                "task": {"objective": "unknown"},
            },
        )
        assert unknown.status is ToolResultStatus.FAILED
        assert unknown.error == "tool handler failed"
        first = await sdk.children.spawn(
            parent.run_id,
            "researcher:1",
            task=TaskEnvelope(objective="first"),
        )
        await asyncio.wait_for(child_started.wait(), timeout=1)
        limited, limited_events = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="spawn_agent",
            call_id="call-limit",
            arguments={
                "agent_revision": "researcher:1",
                "task": {"objective": "second"},
            },
        )
        assert limited.status is ToolResultStatus.FAILED
        assert limited.error == "tool handler failed"
        assert all(
            [event_type for event_type, _ in events]
            == [
                "tool.call.authorized",
                "tool.call.started",
                "tool.call.completed",
            ]
            for events in (unknown_events, limited_events)
        )
        children = await sdk.children.list(parent.run_id)
        assert tuple(item.run_id for item in children) == (first.run_id,)
    finally:
        release_child.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_send_message_rejects_invalid_context_relation() -> None:
    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        return _response("ready")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="allow",
    )
    try:
        session = await sdk.sessions.create(workspaces=())
        parent = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="parent", revision="1", model="test/parent"),
            "ready",
        )
        await parent.result()
        result, emitted = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="send_message",
            arguments={"target_run_id": parent.run_id, "content": "self"},
        )
        assert result.status is ToolResultStatus.FAILED
        assert result.error == "tool handler failed"
        assert emitted[-1][1] == result.model_dump(mode="json")
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_control_tool_permissions_deny_and_ask_before_handler() -> None:
    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        return _response("ready")

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    try:
        session = await sdk.sessions.create(workspaces=())
        parent = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="parent", revision="1", model="test/parent"),
            "ready",
        )
        await parent.result()
        denied, denied_events = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="list_children",
            arguments={},
            policy=PolicyEngine(default_outcome="deny"),
        )
        assert denied.status is ToolResultStatus.DENIED
        assert [event_type for event_type, _ in denied_events] == [
            "tool.call.completed"
        ]

        bridge = InProcessPermissionBridge()
        transitions: list[
            tuple[str, PermissionRequest, PermissionDecision | None]
        ] = []
        execution = asyncio.create_task(
            _execute_for_run(
                sdk,
                run_id=parent.run_id,
                session_id=session.session_id,
                name="list_children",
                arguments={},
                policy=PolicyEngine(default_outcome="ask"),
                bridge=bridge,
                transitions=transitions,
                call_id="call-list-ask",
            )
        )
        permission = await asyncio.wait_for(
            bridge.next_request(parent.run_id),
            timeout=1,
        )
        assert permission.tool_name == "list_children"
        assert permission.effects == ("agent.inspect",)
        assert permission.arguments == {}
        await bridge.resolve(permission.request_id, PermissionDecision.allow_once())
        asked, asked_events = await asyncio.wait_for(execution, timeout=1)
        assert asked.status is ToolResultStatus.SUCCEEDED
        assert [event_type for event_type, _, _ in transitions] == [
            "permission.requested",
            "permission.resolved",
        ]
        assert [event_type for event_type, _ in asked_events] == [
            "tool.call.authorized",
            "tool.call.started",
            "tool.call.completed",
        ]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_run_catalog_and_ancestor_intersection_prevent_spawn_expansion() -> None:
    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        return _response("ready")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="allow",
    )
    try:
        sdk.agents.define(
            AgentSpec(
                name="child",
                revision="1",
                model="test/child",
                tool_allowlist=("read", "spawn_agent"),
            )
        )
        session = await sdk.sessions.create(workspaces=())
        parent = await sdk.runs.start(
            session.session_id,
            AgentSpec(
                name="parent",
                revision="1",
                model="test/parent",
                tool_allowlist=("read",),
            ),
            "ready",
        )
        await parent.result()
        blocked, events = await _execute_for_run(
            sdk,
            run_id=parent.run_id,
            session_id=session.session_id,
            name="spawn_agent",
            arguments={
                "agent_revision": "child:1",
                "task": {
                    "objective": "cannot dispatch",
                    "allowed_tools": ["read", "spawn_agent"],
                },
            },
        )
        assert blocked.status is ToolResultStatus.FAILED
        assert blocked.error == "tool not found"
        assert [event_type for event_type, _ in events] == ["tool.call.completed"]

        child = await sdk.children.spawn(
            parent.run_id,
            "child:1",
            task=TaskEnvelope(
                objective="cannot expand",
                allowed_tools=("read", "spawn_agent"),
            ),
        )
        assert child.execution_descriptor is not None
        assert tuple(
            capability.spec.name
            for capability in child.execution_descriptor.tools
        ) == ("read",)
        await sdk.children.wait(child.run_id, timeout_seconds=1)
    finally:
        await sdk.close()


def test_sdk_initialization_rejects_exact_control_tool_name_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_sdk import api as api_module

    original = api_module.register_builtin_tools

    async def conflicting_handler(_context: ToolContext) -> dict[str, object]:
        return {}

    def register_conflict(**kwargs: Any) -> None:
        original(**kwargs)
        kwargs["registry"].register(
            ToolSpec(
                name="spawn_agent",
                description="application collision",
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                },
            ),
            conflicting_handler,
        )

    monkeypatch.setattr(api_module, "register_builtin_tools", register_conflict)

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        return _response("unused")

    with pytest.raises(AgentSDKError) as raised:
        AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    assert raised.value.code is ErrorCode.CONFLICT
    assert raised.value.message == (
        "child control tool name already registered: spawn_agent"
    )
