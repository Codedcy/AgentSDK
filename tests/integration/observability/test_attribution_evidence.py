from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_sdk import (
    AgentNode,
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    AttributionSummary,
    ToolContext,
    ToolSpec,
    WorkflowEdge,
    WorkflowIR,
)
from agent_sdk.observability import TraceStageKind
from agent_sdk.storage.memory import InMemoryStore


class _MismatchedWorkflowBindingStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.workflow_id: str | None = None
        self.node_id: str | None = None
        self.wrong_run_id: str | None = None

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        data = await super().get_snapshot(kind, entity_id)
        if (
            data is None
            or kind != "workflow"
            or entity_id != self.workflow_id
            or self.node_id is None
            or self.wrong_run_id is None
        ):
            return data
        nodes = data["nodes"]
        assert isinstance(nodes, list)
        node = next(item for item in nodes if item["node_id"] == self.node_id)
        node["run_id"] = self.wrong_run_id
        return data


@pytest.mark.asyncio
async def test_public_attribution_joins_real_tool_completion_to_later_context() -> None:
    turn = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal turn
        turn += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if turn == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_attribution",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"value":7}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            else:
                yield {
                    "choices": [
                        {"delta": {"content": "done"}, "finish_reason": "stop"}
                    ]
                }

        return chunks()

    async def lookup(_context: ToolContext, *, value: int) -> object:
        return {"value": value}

    store = InMemoryStore()
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=provider,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    sdk.tools.register(
        ToolSpec(
            name="lookup",
            description="lookup",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        ),
        lookup,
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="attribution", model="fake/model"),
            "use lookup",
        )
        await handle.result()
        stored = await store.read_events(after_cursor=0)
        completion = next(
            item
            for item in stored
            if item.event.run_id == handle.run_id
            and item.event.type == "tool.call.completed"
        )
        later_view = next(
            item
            for item in stored
            if item.cursor > completion.cursor
            and item.event.type == "context.view.created"
            and completion.event.event_id in item.event.payload["source_refs"]
        )
        manifest = next(
            item
            for item in stored
            if item.event.type == "prompt.manifest.created"
            and item.event.payload["context_view_id"]
            == later_view.event.payload["view_id"]
        )

        summary = await sdk.trace.attribution(handle.run_id)

        assert isinstance(summary, AttributionSummary)
        tool = next(item for item in summary.contributors if item.kind == "tool")
        assert tool.entity_id == "call_attribution"
        assert tool.disposition == "consumed"
        assert completion.event.event_id in tool.evidence_ids
        context = next(
            item
            for item in summary.contributors
            if item.kind == "context"
            and item.entity_id == later_view.event.payload["view_id"]
        )
        assert manifest.event.event_id in context.evidence_ids
        assert summary.failure is None
        assert summary.as_of_cursor >= later_view.cursor
        assert any(
            item.kind == "model" and item.disposition == "terminal"
            for item in summary.contributors
        )
        timeline = await sdk.trace.timeline(handle.run_id)
        assert any(stage.kind is TraceStageKind.TOOL for stage in timeline.stages)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_run_attribution_includes_its_real_workflow_node_evidence() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {"delta": {"content": "workflow done"}, "finish_reason": "stop"}
                ]
            }

        return chunks()

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        enable_builtin_tools=False,
    )
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/model"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        workflow = WorkflowIR.create(
            name="attribution-workflow",
            nodes=(
                AgentNode(
                    id="work",
                    agent_revision="worker:1",
                    input="work",
                ),
            ),
            edges=(),
        )
        result = await (await sdk.workflows.start(session.session_id, workflow)).result()
        run_id = result.nodes[0].run_id
        assert run_id is not None

        summary = await sdk.trace.attribution(run_id)

        workflow_contributor = next(
            item
            for item in summary.contributors
            if item.kind == "workflow" and item.entity_id == "work"
        )
        assert workflow_contributor.status == "completed"
        assert workflow_contributor.evidence_ids
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_run_attribution_excludes_real_sibling_workflow_node_evidence() -> None:
    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {"content": str(params["messages"][-1]["content"])},
                        "finish_reason": "stop",
                    }
                ]
            }

        return chunks()

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        enable_builtin_tools=False,
    )
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/model"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        workflow = WorkflowIR.create(
            name="two-node-attribution",
            nodes=(
                AgentNode(id="first", agent_revision="worker:1", input="first"),
                AgentNode(id="second", agent_revision="worker:1", input="second"),
            ),
            edges=(WorkflowEdge(source="first", target="second"),),
        )
        handle = await sdk.workflows.start(session.session_id, workflow)
        result = await handle.result()
        first_run_id = next(node.run_id for node in result.nodes if node.node_id == "first")
        assert first_run_id is not None

        summary = await sdk.trace.attribution(first_run_id)

        workflow_entities = {
            item.entity_id for item in summary.contributors if item.kind == "workflow"
        }
        assert "first" in workflow_entities
        assert "second" not in workflow_entities
        assert handle.workflow_run_id in workflow_entities
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_run_attribution_authenticates_workflow_node_run_binding() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}

        return chunks()

    store = _MismatchedWorkflowBindingStore()
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=provider,
        enable_builtin_tools=False,
    )
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/model"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        workflow = WorkflowIR.create(
            name="binding-attribution",
            nodes=(
                AgentNode(id="first", agent_revision="worker:1", input="first"),
                AgentNode(id="second", agent_revision="worker:1", input="second"),
            ),
            edges=(WorkflowEdge(source="first", target="second"),),
        )
        handle = await sdk.workflows.start(session.session_id, workflow)
        result = await handle.result()
        first_run_id = next(node.run_id for node in result.nodes if node.node_id == "first")
        second_run_id = next(node.run_id for node in result.nodes if node.node_id == "second")
        assert first_run_id is not None
        assert second_run_id is not None
        store.workflow_id = handle.workflow_run_id
        store.node_id = "first"
        store.wrong_run_id = second_run_id

        with pytest.raises(AgentSDKError, match="failed to load trace timeline"):
            await sdk.trace.attribution(first_run_id)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_bounded_loop_node_run_attribution_includes_real_loop_failure() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {"delta": {"content": '{"progress":1}'}, "finish_reason": "stop"}
                ]
            }

        return chunks()

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        enable_builtin_tools=False,
    )
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/model"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        workflow = """
api_version: agent-sdk/v1
kind: Workflow
name: bounded-attribution
steps:
  - id: improve
    kind: loop
    until: {path: outputs.review.done, op: exists}
    max_iterations: 1
    body:
      - {id: review, kind: agent, agent_revision: worker:1, input: review}
"""
        handle = await sdk.workflows.start(session.session_id, workflow)
        with pytest.raises(AgentSDKError, match="reached its iteration limit"):
            await handle.result()
        snapshot = await sdk.workflows.get(handle.workflow_run_id)
        node_run_id = next(node.run_id for node in snapshot.nodes if node.node_id == "review")
        assert node_run_id is not None

        summary = await sdk.trace.attribution(node_run_id)

        assert "workflow_loop_limit" in {hint.code for hint in summary.hints}
        assert any(
            item.kind == "workflow"
            and item.entity_id == handle.workflow_run_id
            and item.status == "failed"
            for item in summary.contributors
        )
    finally:
        await sdk.close()
