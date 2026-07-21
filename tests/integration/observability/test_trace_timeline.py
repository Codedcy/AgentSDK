from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSpec,
    TraceStageKind,
    TraceStageStatus,
    WorkflowDefinition,
)
from agent_sdk.storage.memory import InMemoryStore


def _response() -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": "private-output"}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "cost": 0.125,
            },
        }

    return chunks()


async def _provider(**_: Any) -> AsyncIterator[dict[str, object]]:
    return _response()


@pytest.mark.asyncio
async def test_public_trace_timeline_projects_run_at_one_high_water() -> None:
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=_provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="agent", model="fake/model"),
            "private-input",
        )
        await handle.result()

        timeline = await sdk.trace.timeline(handle.run_id)

        assert timeline.root_id == handle.run_id
        assert timeline.as_of_cursor > 0
        assert [stage.kind for stage in timeline.stages] == [
            TraceStageKind.RUN,
            TraceStageKind.CONTEXT,
            TraceStageKind.STEP,
            TraceStageKind.MODEL,
        ]
        assert all(stage.status is TraceStageStatus.COMPLETED for stage in timeline.stages)
        assert timeline.stages[0].usage is not None
        assert timeline.stages[0].usage.cost_usd == 0.125
        serialized = timeline.model_dump_json()
        assert "private-input" not in serialized
        assert "private-output" not in serialized

        stream = sdk.trace.subscribe(cursor=timeline.as_of_cursor)
        waiting = asyncio.create_task(anext(stream))
        await sdk.sessions.create(workspaces=[])
        observed = await asyncio.wait_for(waiting, timeout=1)
        await stream.aclose()
        assert observed.cursor > timeline.as_of_cursor
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_workflow_timeline_includes_node_runs_in_one_parent_tree() -> None:
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=_provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/model"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        workflow = WorkflowDefinition.model_validate(
            {
                "api_version": "agent-sdk/v1",
                "kind": "Workflow",
                "name": "trace-workflow",
                "nodes": [
                    {
                        "id": "work",
                        "kind": "agent",
                        "agent_revision": "worker:1",
                        "input": "private-workflow-input",
                    }
                ],
                "edges": [],
            }
        )
        handle = await sdk.workflows.start(session.session_id, workflow)
        await handle.result()

        timeline = await sdk.trace.timeline(handle.workflow_run_id)

        by_kind = {stage.kind: stage for stage in timeline.stages}
        assert by_kind[TraceStageKind.WORKFLOW_NODE].parent_stage_id == by_kind[
            TraceStageKind.WORKFLOW
        ].stage_id
        assert by_kind[TraceStageKind.RUN].parent_stage_id == by_kind[
            TraceStageKind.WORKFLOW_NODE
        ].stage_id
        assert "private-workflow-input" not in timeline.model_dump_json()
    finally:
        await sdk.close()
