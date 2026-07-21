from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ContextPlanner,
    ContextRuntimeConfig,
    ErrorCode,
    EvaluationVerdict,
    EventFilter,
    ExactOutputEvaluator,
    MCPManager,
    MCPServerConfig,
    PermissionDecision,
    PromptComposer,
    ReconciliationAction,
    RunStatus,
    StdioMCPTransport,
    TraceStageKind,
    ToolContext,
    ToolResultStatus,
    ToolSpec,
    WorkflowRunStatus,
)

if TYPE_CHECKING:
    from tests.fixtures.v01_runtime import V01Harness


pytest_plugins = ("tests.fixtures.v01_runtime",)


def _v01_text_stream(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {"delta": {"content": text}, "finish_reason": "stop"}
            ]
        }
        yield {
            "choices": [],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        }

    return chunks()


def _v01_tool_stream(
    *,
    call_id: str,
    name: str,
    arguments: dict[str, object],
) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    return chunks()


async def _collect_until_run_completed(
    sdk: AgentSDK,
    session_id: str,
    observed_types: list[str],
) -> None:
    async for item in sdk.trace.subscribe(
        filters=EventFilter(session_id=session_id),
        cursor=0,
    ):
        observed_types.append(item.event.type)
        if item.event.type == "run.completed":
            return


async def _accept_steps_1_2_5_10_11_13(
    v01_harness: V01Harness,
) -> None:
    workspace_file = v01_harness.workspace / "keep.txt"
    workspace_file.write_text("application-owned", encoding="utf-8")

    sdk: AgentSDK = v01_harness.open()
    async def app_echo(_: ToolContext, *, text: str) -> dict[str, str]:
        return {"text": text}

    sdk.tools.register(
        ToolSpec(
            name="app_echo",
            description="Echo application text",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            effects=("application.read",),
        ),
        app_echo,
    )
    manager = MCPManager(sdk.tools)
    await asyncio.wait_for(
        manager.connect(
            MCPServerConfig(
                name="demo",
                transport=StdioMCPTransport(
                    command=sys.executable,
                    args=(
                        str(
                            Path(__file__).parents[1]
                            / "fixtures"
                            / "mcp_server.py"
                        ),
                    ),
                    cwd=v01_harness.workspace,
                ),
            )
        ),
        timeout=10,
    )
    session = await sdk.sessions.create(
        workspaces=(v01_harness.workspace,),
        idempotency_key="v01-session",
    )
    agent = sdk.agents.define(
        AgentSpec(
            name="release-agent",
            model="test/model",
            system_prompt="Application release policy.",
        )
    )
    live_event_types: list[str] = []
    live_monitor = asyncio.create_task(
        _collect_until_run_completed(sdk, session.session_id, live_event_types)
    )
    handle = await sdk.runs.start(
        session.session_id,
        agent,
        "baseline",
        idempotency_key="v01-run",
    )
    permission = await asyncio.wait_for(
        sdk.permissions.next_request(handle.run_id),
        timeout=2,
    )
    assert permission.tool_name == "read"
    assert permission.arguments["path"] == str(workspace_file.resolve())
    await asyncio.wait_for(
        sdk.permissions.resolve(
            permission.request_id,
            PermissionDecision.allow_once(),
        ),
        timeout=2,
    )
    terminal = await asyncio.wait_for(handle.result(), timeout=5)
    await asyncio.wait_for(live_monitor, timeout=5)
    await asyncio.wait_for(manager.close(), timeout=5)
    assert terminal.output_text == "baseline complete"
    assert [result.tool_name for result in terminal.tool_results] == [
        "app_echo",
        "write",
        "read",
        "bash",
        "write",
        "mcp.demo.echo",
    ]
    assert [result.status for result in terminal.tool_results] == [
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.DENIED,
        ToolResultStatus.SUCCEEDED,
    ]
    assert (v01_harness.workspace / "generated.txt").read_text(
        encoding="utf-8"
    ) == "created by builtin write"
    assert terminal.tool_results[2].value["content"] == "application-owned"
    assert (
        "builtin bash complete"
        in terminal.tool_results[3].value["stdout"]
    )
    assert v01_harness.outside_file.read_text(
        encoding="utf-8"
    ) == "outside fixture"
    timeline = await sdk.queries.timeline(handle.run_id)
    assert timeline.run_id == handle.run_id
    event_types = [item.event.type for item in timeline.events]
    assert event_types.count("permission.requested") == 1
    assert event_types.count("permission.resolved") == 1
    assert event_types.count("tool.call.completed") == 6
    assert event_types.count("tool.call.started") == 5
    assert "run.completed" in live_event_types
    trace = await sdk.trace.timeline(handle.run_id)
    assert trace.root_id == handle.run_id
    assert any(stage.kind is TraceStageKind.PERMISSION for stage in trace.stages)
    evaluation = await sdk.evaluations.evaluate(
        handle.run_id,
        ExactOutputEvaluator(expected="baseline complete"),
    )
    assert evaluation.verdict is EvaluationVerdict.PASS
    success_rate = await sdk.analytics.success_rate(evaluator_id="exact_output")
    assert success_rate.value == 1.0
    assert success_rate.sample_count == 1
    tool_failure_rate = await sdk.analytics.tool_failure_rate()
    assert tool_failure_rate.value == pytest.approx(1 / 6)
    assert tool_failure_rate.sample_count == 6
    attribution = await sdk.trace.attribution(handle.run_id)
    assert attribution.method == "deterministic_event_evidence_v1"
    assert attribution.terminal_status is RunStatus.COMPLETED
    assert attribution.failure is None
    assert {contributor.kind for contributor in attribution.contributors} >= {
        "context",
        "evaluation",
        "model",
        "tool",
    }
    await asyncio.wait_for(sdk.close(), timeout=5)

    provider_calls = 0

    async def must_not_call(**_: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("completed Run must not call LiteLLM after reopen")

    reopened = v01_harness.reopen(must_not_call)
    observed = await reopened.queries.get_run(handle.run_id)
    assert observed.snapshot.status is RunStatus.COMPLETED
    await reopened.sessions.close(session.session_id)
    await reopened.sessions.delete(session.session_id)
    with pytest.raises(AgentSDKError) as deleted:
        await reopened.sessions.get(session.session_id)
    assert deleted.value.code is ErrorCode.NOT_FOUND
    assert workspace_file.read_text(encoding="utf-8") == "application-owned"
    assert v01_harness.outside_file.read_text(
        encoding="utf-8"
    ) == "outside fixture"
    await asyncio.wait_for(reopened.close(), timeout=5)
    assert provider_calls == 0


async def _accept_steps_7_8_and_12_safe_boundary(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    review_calls = 0

    def chunks(text: str) -> AsyncIterator[dict[str, object]]:
        async def generate() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": text}}]}
            yield {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }

        return generate()

    async def provider(**params: object) -> AsyncIterator[dict[str, object]]:
        nonlocal review_calls
        messages = params["messages"]
        assert isinstance(messages, (list, tuple))
        assert isinstance(messages[-1], dict)
        prompt = str(messages[-1]["content"])
        calls.append(prompt)
        if prompt == "review":
            review_calls += 1
            return chunks(
                '{"done":true}' if review_calls == 2 else '{"progress":1}'
            )
        return chunks(prompt)

    generated_yaml = """
api_version: agent-sdk/v1
kind: Workflow
name: generated-control
inputs: {enabled: true}
steps:
  - id: choose
    kind: condition
    when: {path: inputs.enabled, op: eq, value: true}
    then_steps:
      - {id: selected, kind: agent, agent_revision: workflow:1, input: selected}
    else_steps:
      - {id: skipped, kind: agent, agent_revision: workflow:1, input: skipped}
  - id: improve
    kind: loop
    until: {path: outputs.review.done, op: exists}
    max_iterations: 3
    body:
      - {id: review, kind: agent, agent_revision: workflow:1, input: review}
  - {id: finish, kind: agent, agent_revision: workflow:1, input: finish}
    """
    sdk = AgentSDK.for_test(
        database_path=tmp_path / "workflow.sqlite3",
        acompletion=provider,
    )
    sdk.agents.define(AgentSpec(name="workflow", revision="1", model="test/workflow"))
    session = await asyncio.wait_for(
        sdk.sessions.create(workspaces=[]),
        timeout=5,
    )
    compiled = sdk.workflows.compile(generated_yaml)
    assert compiled.schema_version == 2
    assert calls == []
    observed_session = await asyncio.wait_for(
        sdk.sessions.get(session.session_id),
        timeout=5,
    )
    assert observed_session.active_workflow_run_ids == ()

    handle = await asyncio.wait_for(
        sdk.workflows.start(session.session_id, compiled),
        timeout=5,
    )
    result = await asyncio.wait_for(handle.result(), timeout=10)
    workflow_run_id = handle.workflow_run_id
    assert result.status is WorkflowRunStatus.COMPLETED
    assert calls == ["selected", "review", "review", "finish"]
    async def collect_events() -> list[str]:
        return [item.event.type async for item in handle.events()]

    event_types = await asyncio.wait_for(collect_events(), timeout=5)
    assert "workflow.condition.selected" in event_types
    assert event_types.count("workflow.loop.iteration") == 2
    assert event_types[-1] == "workflow.completed"
    await asyncio.wait_for(sdk.close(), timeout=5)
    reopen_calls = 0

    async def must_not_call(**_: object) -> object:
        nonlocal reopen_calls
        reopen_calls += 1
        raise AssertionError("safe-boundary reopen must not call LiteLLM")

    reopened = AgentSDK.for_test(
        database_path=tmp_path / "workflow.sqlite3",
        acompletion=must_not_call,
    )
    observed = await asyncio.wait_for(
        reopened.workflows.get(workflow_run_id),
        timeout=5,
    )
    assert observed.status is WorkflowRunStatus.COMPLETED
    await asyncio.wait_for(reopened.close(), timeout=5)
    assert reopen_calls == 0


async def _accept_steps_3_4_and_6(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stage_tokens = {
        "stage-l0": 10,
        "stage-l1": 70,
        "stage-l2": 80,
        "stage-l3-invalid": 90,
        "stage-l3-valid": 90,
        "stage-l4": 96,
    }

    def controlled_estimate(
        _planner: ContextPlanner,
        messages: list[dict[str, Any]],
    ) -> int:
        serialized = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        latest = max(
            (
                (serialized.rfind(stage), tokens)
                for stage, tokens in stage_tokens.items()
            ),
            key=lambda item: item[0],
        )
        return latest[1] if latest[0] >= 0 else 10

    monkeypatch.setattr(
        ContextPlanner,
        "_estimate_messages",
        controlled_estimate,
    )

    def text_stream() -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {"content": "completed"},
                        "finish_reason": "stop",
                    }
                ]
            }

        return chunks()

    compaction_operations: list[str] = []

    async def provider(**params: object) -> object:
        if params.get("stream") is not False:
            return text_stream()
        messages = params["messages"]
        assert isinstance(messages, list)
        document = json.loads(str(messages[-1]["content"]))
        operation = str(document["operation"])
        compaction_operations.append(operation)
        if len(compaction_operations) == 1:
            return {
                "choices": [{"message": {"content": "{invalid-json"}}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }
        source_refs = [
            str(source["event_id"])
            for source in document.get("sources", [])
        ]
        capsule_refs = [
            str(capsule_id)
            for capsule_id in document.get("capsule_ids", [])
        ]
        return {
            "choices": [
                {
                    "message": {
                        "parsed": {
                            "objective": "preserve runtime context",
                            "constraints": ["retain durable evidence"],
                            "decisions": [],
                            "facts": [],
                            "next_actions": ["continue"],
                            "artifact_refs": [],
                            "source_event_ids": [*capsule_refs, *source_refs],
                        }
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        }

    skill_root = Path(__file__).parents[1] / "fixtures" / "skills"
    sdk = AgentSDK.for_test(
        database_path=tmp_path / "context.sqlite3",
        acompletion=provider,
        skill_roots=(skill_root,),
        enable_builtin_tools=False,
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        agent = AgentSpec(
            name="automatic-context",
            model="test/context",
            system_prompt="Application runtime policy.",
            skills=("demo",),
            context=ContextRuntimeConfig(
                model_window=100,
                output_reserve=0,
                safety_reserve=0,
                recent_messages=2,
            ),
        )
        run_ids: list[str] = []
        for stage in stage_tokens:
            handle = await sdk.runs.start(session.session_id, agent, stage)
            result = await handle.result()
            assert result.output_text == "completed"
            run_ids.append(handle.run_id)

        event_page = await sdk.queries.query_events(
            EventFilter(session_id=session.session_id),
            after_cursor=0,
            limit=1_000,
        )
        events = event_page.events
        views = [
            item.event
            for item in events
            if item.event.type == "context.view.created"
        ]
        assert [
            event.payload["recommended_level"] for event in views
        ] == ["L0", "L1", "L2", "L3", "L3", "L4"]
        assert [
            event.payload["applied_level"] for event in views
        ] == ["L0", "L1", "L2", "L2", "L3", "L4"]
        assert views[3].payload["fallback_from"] == "L3"
        assert compaction_operations == ["summarize", "summarize", "rebase"]

        original = next(
            item.event
            for item in events
            if item.event.type == "run.created"
            and item.event.run_id == run_ids[0]
        )
        assert any(item.event.event_id == original.event_id for item in events)
        final_view_id = str(views[-1].payload["view_id"])
        capsule_id = views[-1].payload["capsule_id"]
        assert isinstance(capsule_id, str)
        recovered_sources = await sdk.context.read_sources(
            capsule_id,
            session_id=session.session_id,
        )
        assert original.event_id in {
            observed.event.event_id for observed in recovered_sources
        }

        last_started = next(
            item.event
            for item in reversed(events)
            if item.event.type == "model.call.started"
            and item.event.run_id == run_ids[-1]
        )
        manifest_id = str(last_started.payload["prompt_manifest_id"])
        assert manifest_id.startswith("pmf_")
        assert last_started.payload["context_view_id"] == final_view_id
        manifest_view = await sdk.context.build(
            session.session_id,
            model="test/context",
            model_window=100,
            force_level="L2",
        )
        activated = sdk.skills.activate("demo")
        prompt = PromptComposer().compose(
            profile="general",
            context_view=manifest_view,
            model="test/context",
            application="Application runtime policy.",
            skills=(activated,),
        )
        assert "Follow this demo skill." in activated.instructions
        assert prompt.manifest.context_view_id == manifest_view.view_id
        assert prompt.manifest.layer_names == (
            "profile:general",
            "application",
            "skill:demo",
        )
    finally:
        await sdk.close()


async def _accept_step_9_and_child_trace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    nested_evidence = workspace / "evidence" / "nested"
    nested_evidence.mkdir(parents=True)
    allow_child_message = asyncio.Event()
    child_context_received = asyncio.Event()
    allow_child_complete = asyncio.Event()
    parent_calls = 0
    child_calls = 0
    child_run_id: str | None = None

    def tool_names(params: dict[str, object]) -> tuple[str, ...]:
        raw_tools = params["tools"]
        assert isinstance(raw_tools, (list, tuple))
        names: list[str] = []
        for raw in raw_tools:
            assert isinstance(raw, dict)
            function = raw["function"]
            assert isinstance(function, dict)
            names.append(str(function["name"]))
        return tuple(names)

    def messages(params: dict[str, object]) -> tuple[dict[str, object], ...]:
        raw = params["messages"]
        assert isinstance(raw, (list, tuple))
        assert all(isinstance(item, dict) for item in raw)
        return tuple(raw)  # type: ignore[return-value]

    def last_tool_value(
        params: dict[str, object],
        expected_name: str,
    ) -> dict[str, object]:
        tool_messages = [
            message
            for message in messages(params)
            if message.get("role") == "tool"
        ]
        assert tool_messages
        latest = tool_messages[-1]
        assert latest["name"] == expected_name
        value = json.loads(str(latest["content"]))
        assert isinstance(value, dict)
        return value

    async def provider(**raw_params: object) -> object:
        nonlocal parent_calls, child_calls, child_run_id
        params = dict(raw_params)
        model = params["model"]
        if model == "test/child":
            child_calls += 1
            assert tool_names(params) == ("send_message",)
            if child_calls == 1:
                await asyncio.wait_for(allow_child_message.wait(), timeout=2)
                assert child_run_id is not None
                assert parent_run_id
                return _v01_tool_stream(
                    call_id="child-message",
                    name="send_message",
                    arguments={
                        "target_run_id": parent_run_id,
                        "content": "child update: source evt-2 accepted",
                    },
                )
            assert child_calls == 2
            assert any(
                "Agent message from" in str(message.get("content"))
                and "Use source evt-2" in str(message.get("content"))
                for message in messages(params)
            )
            child_context_received.set()
            await asyncio.wait_for(allow_child_complete.wait(), timeout=2)
            return _v01_text_stream("verified child finding from evt-2")

        assert model == "test/parent"
        parent_calls += 1
        assert tool_names(params) == (
            "list_children",
            "send_message",
            "spawn_agent",
            "wait_child",
        )
        if parent_calls == 1:
            return _v01_tool_stream(
                call_id="parent-spawn",
                name="spawn_agent",
                arguments={
                    "agent_revision": "researcher:1",
                    "task": {
                        "objective": "Inspect the evidence",
                        "success_criteria": ["return one finding"],
                        "evidence_refs": ["evt-1"],
                        "allowed_tools": ["read", "send_message"],
                        "workspace_scopes": [str(workspace / "evidence")],
                    },
                },
            )
        if parent_calls == 2:
            spawned = last_tool_value(params, "spawn_agent")
            child_run_id = str(spawned["child_run_id"])
            assert spawned["status"] == "queued"
            return _v01_tool_stream(
                call_id="parent-message",
                name="send_message",
                arguments={
                    "target_run_id": child_run_id,
                    "content": "Use source evt-2",
                },
            )
        assert child_run_id is not None
        if parent_calls == 3:
            sent = last_tool_value(params, "send_message")
            assert sent["recipient_run_id"] == child_run_id
            return _v01_tool_stream(
                call_id="parent-list",
                name="list_children",
                arguments={},
            )
        if parent_calls == 4:
            listed = json.loads(
                str(
                    next(
                        message["content"]
                        for message in reversed(messages(params))
                        if message.get("role") == "tool"
                        and message.get("name") == "list_children"
                    )
                )
            )
            assert isinstance(listed, list)
            assert listed[0]["run_id"] == child_run_id
            allow_child_message.set()
            await asyncio.wait_for(child_context_received.wait(), timeout=2)
            return _v01_tool_stream(
                call_id="parent-wait-pending",
                name="wait_child",
                arguments={
                    "child_run_id": child_run_id,
                    "timeout_seconds": 0,
                },
            )
        if parent_calls == 5:
            pending = last_tool_value(params, "wait_child")
            assert pending["status"] == "pending"
            assert any(
                "Agent message from" in str(message.get("content"))
                and "child update: source evt-2 accepted"
                in str(message.get("content"))
                for message in messages(params)
            )
            allow_child_complete.set()
            return _v01_tool_stream(
                call_id="parent-wait-terminal",
                name="wait_child",
                arguments={
                    "child_run_id": child_run_id,
                    "timeout_seconds": 1,
                },
            )
        assert parent_calls == 6
        terminal = last_tool_value(params, "wait_child")
        assert terminal["status"] == "completed"
        assert terminal["result"]["output_text"] == (
            "verified child finding from evt-2"
        )
        return _v01_text_stream(
            "parent used verified child finding from evt-2"
        )

    sdk = AgentSDK.for_test(
        database_path=tmp_path / "child.sqlite3",
        acompletion=provider,
        permission_default="allow",
    )
    parent_agent = AgentSpec(
        name="parent",
        revision="1",
        model="test/parent",
        tool_allowlist=(
            "spawn_agent",
            "send_message",
            "list_children",
            "wait_child",
        ),
        workspace_allowlist=(str(workspace),),
    )
    sdk.agents.define(
        AgentSpec(
            name="researcher",
            revision="1",
            model="test/child",
            tool_allowlist=("read", "send_message"),
            workspace_allowlist=(str(nested_evidence),),
        )
    )
    session = await sdk.sessions.create(workspaces=(workspace,))
    parent_run_id = ""
    try:
        parent = await sdk.runs.start(
            session.session_id,
            parent_agent,
            "coordinate child evidence",
        )
        parent_run_id = parent.run_id
        result = await asyncio.wait_for(parent.result(), timeout=5)
        assert result.output_text == "parent used verified child finding from evt-2"
        assert [tool.tool_name for tool in result.tool_results] == [
            "spawn_agent",
            "send_message",
            "list_children",
            "wait_child",
            "wait_child",
        ]
        assert child_run_id is not None

        child = await sdk.runs.get(child_run_id)
        assert child.parent_run_id == parent_run_id
        assert child.execution_descriptor is not None
        assert tuple(
            capability.spec.name
            for capability in child.execution_descriptor.tools
        ) == ("send_message",)
        assert child.execution_descriptor.workspace_scopes == (
            str(nested_evidence.resolve()),
        )
        assert child.output_text == "verified child finding from evt-2"
        assert [tool.tool_name for tool in child.tool_results] == ["send_message"]

        progress = await sdk.children.list(parent_run_id)
        assert len(progress) == 1
        assert progress[0].run_id == child_run_id
        assert progress[0].parent_run_id == parent_run_id
        assert progress[0].status == "completed"
        tree = await sdk.queries.execution_tree(parent_run_id)
        assert [(node.snapshot.run_id, node.parent_run_id) for node in tree.nodes] == [
            (parent_run_id, None),
            (child_run_id, parent_run_id),
        ]
        assert all(node.snapshot.status is RunStatus.COMPLETED for node in tree.nodes)

        event_page = await sdk.queries.query_events(
            EventFilter(session_id=session.session_id),
            after_cursor=0,
            limit=1_000,
        )
        events = event_page.events
        messages_sent = [
            stored.event
            for stored in events
            if stored.event.type == "agent.message.sent"
        ]
        assert len(messages_sent) == 2
        parent_message = next(
            event
            for event in messages_sent
            if event.payload["sender_run_id"] == parent_run_id
        )
        child_message = next(
            event
            for event in messages_sent
            if event.payload["sender_run_id"] == child_run_id
        )
        parent_view_ids = {
            stored.event.payload["context_view_id"]
            for stored in events
            if stored.event.type == "model.call.started"
            and stored.event.run_id == parent_run_id
        }
        child_view_ids = {
            stored.event.payload["context_view_id"]
            for stored in events
            if stored.event.type == "model.call.started"
            and stored.event.run_id == child_run_id
        }
        parent_views = [
            stored.event
            for stored in events
            if stored.event.type == "context.view.created"
            and stored.event.payload["view_id"] in parent_view_ids
        ]
        child_views = [
            stored.event
            for stored in events
            if stored.event.type == "context.view.created"
            and stored.event.payload["view_id"] in child_view_ids
        ]
        assert len(parent_views) == 6
        assert len(child_views) == 2
        assert any(
            child_message.payload["message_id"]
            in event.payload["consumed_message_ids"]
            and child_message.payload["message_id"] in event.payload["message_refs"]
            for event in parent_views
        )
        assert any(
            parent_message.payload["message_id"]
            in event.payload["consumed_message_ids"]
            and parent_message.payload["message_id"] in event.payload["message_refs"]
            for event in child_views
        )

        parent_timeline = await sdk.queries.timeline(parent_run_id)
        child_timeline = await sdk.queries.timeline(child_run_id)
        parent_types = [item.event.type for item in parent_timeline.events]
        child_types = [item.event.type for item in child_timeline.events]
        for event_type in (
            "tool.call.proposed",
            "tool.call.authorized",
            "tool.call.started",
            "tool.call.completed",
        ):
            assert parent_types.count(event_type) == 5
            assert child_types.count(event_type) == 1
        assert parent_types.count("model.call.started") == 6
        assert child_types.count("model.call.started") == 2
        assert parent_types[0] == "run.created"
        assert parent_types[-1] == "run.completed"
        assert child_types[0] == "run.created"
        assert child_types[-1] == "run.completed"
        trace = await sdk.trace.timeline(parent_run_id)
        assert {stage.kind for stage in trace.stages} >= {
            TraceStageKind.RUN,
            TraceStageKind.CONTEXT,
            TraceStageKind.MODEL,
            TraceStageKind.TOOL,
            TraceStageKind.CHILD,
            TraceStageKind.MESSAGE,
        }
        attribution = await sdk.trace.attribution(parent_run_id)
        assert attribution.method == "deterministic_event_evidence_v1"
        assert {contributor.kind for contributor in attribution.contributors} >= {
            "child",
            "context",
            "model",
            "tool",
        }
    finally:
        allow_child_message.set()
        allow_child_complete.set()
        await sdk.close()


async def _accept_step_12_unknown_inflight(
    tmp_path: Path,
) -> None:
    database = tmp_path / "interrupted.sqlite3"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.fixtures.v01_runtime",
        "--seed-interrupted-tool",
        str(database),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        assert line, (await process.stderr.read()).decode("utf-8", errors="replace")
        seeded = json.loads(line)
        assert seeded["status"] == "tool_in_flight"
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), timeout=5)

    provider_calls = 0
    tool_calls = 0

    async def recovery_provider(**_: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        return _v01_text_stream("recovery complete")

    async def recovered_effect(*_: object, **kwargs: object) -> object:
        nonlocal tool_calls
        tool_calls += 1
        return {"value": kwargs["value"]}

    sdk = AgentSDK.for_test(
        database_path=database,
        acompletion=recovery_provider,
        permission_default="allow",
    )
    sdk.agents.define(AgentSpec(name="recovery", revision="1", model="test/recovery"))
    sdk.tools.register(
        ToolSpec(
            name="external_effect",
            description="Block until the fixture process is terminated",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            version="1",
            source="application",
            effects=("external.write",),
        ),
        recovered_effect,
    )
    try:
        run_id = str(seeded["run_id"])
        deadline = asyncio.get_running_loop().time() + 45
        while True:
            await asyncio.wait_for(sdk.recovery.scan(), timeout=10)
            interrupted = await sdk.runs.get(run_id)
            if interrupted.status is RunStatus.INTERRUPTED:
                break
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.25)
        assert interrupted.status is RunStatus.INTERRUPTED
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await asyncio.wait_for(waiting.result(), timeout=5)
        request = (await sdk.recovery.pending_requests(run_id))[0]
        assert provider_calls == 0
        assert tool_calls == 0
        resolved = await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.RETRY,
            actor={"type": "operator", "id": "v01-acceptance"},
            evidence={"acknowledge_duplicate_side_effect_risk": True},
        )
        assert resolved.status.value == "resolved"
        assert provider_calls == 0
        assert tool_calls == 0
        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert result.output_text == "recovery complete"
        assert provider_calls == 1
        assert tool_calls == 1
    finally:
        await asyncio.wait_for(sdk.close(), timeout=5)


@pytest.mark.asyncio
async def test_v01_release_public_acceptance_thirteen_steps(
    v01_harness: V01Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Prove the installed/public v0.1 contract as one ordered acceptance.

    1 SQLite workspace Session; 2 configurable prompt; 3 automatic Context;
    4 L0-L4 ledger; 5 application/built-in/MCP authorization; 6 Skill/manifest;
    7 condition/bounded-loop candidate; 8 validate/confirm/start; 9 agent-driven
    Child controls; 10 live/historical Trace; 11 evaluation/analytics/attribution;
    12 safe reopen plus interrupted explicit recovery; 13 delete history, keep files.
    """
    await _accept_steps_1_2_5_10_11_13(v01_harness)
    await _accept_steps_3_4_and_6(monkeypatch, tmp_path)
    await _accept_steps_7_8_and_12_safe_boundary(tmp_path)
    await _accept_step_9_and_child_trace(tmp_path)
    await _accept_step_12_unknown_inflight(tmp_path)
