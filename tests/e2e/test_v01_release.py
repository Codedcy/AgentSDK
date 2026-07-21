from __future__ import annotations

import asyncio
import json
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
    PermissionDecision,
    PromptManifest,
    RunStatus,
    ToolResultStatus,
    WorkflowRunStatus,
)
from agent_sdk.tools.models import thaw_json
from agent_sdk.storage.base import CommitBatch, CommitResult, StateStore
from agent_sdk.storage.memory import InMemoryStore

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


class _CancelAfterSecondLoopIteration:
    def __init__(self, delegate: StateStore) -> None:
        self.delegate = delegate
        self.iterations = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        result = await self.delegate.commit(batch)
        self.iterations += sum(
            event.type == "workflow.loop.iteration"
            for event in batch.events
        )
        if self.iterations == 2:
            self.iterations += 1
            raise asyncio.CancelledError
        return result


@pytest.mark.asyncio
async def test_v01_release_baseline_reopens_and_deletes_history(
    v01_harness: V01Harness,
) -> None:
    workspace_file = v01_harness.workspace / "keep.txt"
    workspace_file.write_text("application-owned", encoding="utf-8")

    sdk: AgentSDK = v01_harness.open()
    session = await sdk.sessions.create(
        workspaces=(v01_harness.workspace,),
        idempotency_key="v01-session",
    )
    agent = sdk.agents.define(
        AgentSpec(
            name="release-agent",
            model="test/model",
        )
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
    assert terminal.output_text == "baseline complete"
    assert [result.tool_name for result in terminal.tool_results] == [
        "write",
        "read",
        "bash",
        "write",
    ]
    assert [result.status for result in terminal.tool_results] == [
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.DENIED,
    ]
    assert (v01_harness.workspace / "generated.txt").read_text(
        encoding="utf-8"
    ) == "created by builtin write"
    assert thaw_json(terminal.tool_results[1].value)["content"] == "application-owned"
    assert (
        "builtin bash complete"
        in thaw_json(terminal.tool_results[2].value)["stdout"]
    )
    assert v01_harness.outside_file.read_text(
        encoding="utf-8"
    ) == "outside fixture"
    timeline = await sdk.queries.timeline(handle.run_id)
    assert timeline.run_id == handle.run_id
    event_types = [item.event.type for item in timeline.events]
    assert event_types.count("permission.requested") == 1
    assert event_types.count("permission.resolved") == 1
    assert event_types.count("tool.call.completed") == 4
    assert event_types.count("tool.call.started") == 3
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


@pytest.mark.asyncio
async def test_v01_generated_workflow_is_explicit_and_restart_safe() -> None:
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
    store = InMemoryStore()
    sdk = AgentSDK.for_test(
        store=_CancelAfterSecondLoopIteration(store),
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
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(handle.result(), timeout=10)
    workflow_run_id = handle.workflow_run_id
    assert calls == ["selected", "review"]
    await asyncio.wait_for(sdk.close(), timeout=5)

    reopened = AgentSDK.for_test(store=store, acompletion=provider)
    reopened.agents.define(
        AgentSpec(name="workflow", revision="1", model="test/workflow")
    )
    recovered = await asyncio.wait_for(
        reopened.recovery.recover_workflow(workflow_run_id),
        timeout=5,
    )
    result = await asyncio.wait_for(recovered.result(), timeout=10)
    assert result.status is WorkflowRunStatus.COMPLETED
    assert calls == ["selected", "review", "review", "finish"]
    async def collect_events() -> list[str]:
        return [item.event.type async for item in recovered.events()]

    event_types = await asyncio.wait_for(collect_events(), timeout=5)
    assert "workflow.condition.selected" in event_types
    assert event_types.count("workflow.loop.iteration") == 2
    assert event_types[-1] == "workflow.completed"
    await asyncio.wait_for(reopened.close(), timeout=5)


@pytest.mark.asyncio
async def test_v01_runtime_automatically_compacts_l0_through_l4(
    monkeypatch: pytest.MonkeyPatch,
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

    store = InMemoryStore()
    skill_root = Path(__file__).parents[1] / "fixtures" / "skills"
    sdk = AgentSDK.for_test(
        store=store,
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

        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
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
        final_view = await store.get_snapshot("context_view", final_view_id)
        assert final_view is not None
        capsule_id = final_view["capsule_id"]
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
        raw_manifest = await store.get_snapshot("prompt_manifest", manifest_id)
        assert raw_manifest is not None
        manifest = PromptManifest.model_validate(raw_manifest)
        assert manifest.context_view_id == final_view_id
        assert manifest.layer_names == (
            "profile:general",
            "application",
            "skill:demo",
        )
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_v01_parent_controls_child_and_consumes_mailbox_context(
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

    store = InMemoryStore()
    sdk = AgentSDK.for_test(
        store=store,
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

        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
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
    finally:
        allow_child_message.set()
        allow_child_complete.set()
        await sdk.close()
