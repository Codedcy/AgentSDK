from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_sdk import (
    AgentNode,
    AgentSDK,
    AgentSpec,
    AttributionSummary,
    ToolContext,
    ToolSpec,
    WorkflowIR,
)
from agent_sdk.observability import TraceStageKind
from agent_sdk.storage.memory import InMemoryStore


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
