from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    TaskEnvelope,
    TraceStageKind,
    TraceStageStatus,
    TraceService,
    ToolContext,
    ToolSpec,
    WorkflowDefinition,
)
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.storage.base import CommitBatch
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
        assert timeline.root_kind == "run"
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

        assert timeline.root_kind == "workflow"
        by_kind = {stage.kind: stage for stage in timeline.stages}
        assert all(stage.session_id == session.session_id for stage in timeline.stages)
        assert all(stage.run_id for stage in timeline.stages)
        assert by_kind[TraceStageKind.WORKFLOW].run_id == handle.workflow_run_id
        assert by_kind[TraceStageKind.WORKFLOW_NODE].run_id == handle.workflow_run_id
        assert by_kind[TraceStageKind.WORKFLOW_NODE].parent_stage_id == by_kind[
            TraceStageKind.WORKFLOW
        ].stage_id
        assert by_kind[TraceStageKind.RUN].parent_stage_id == by_kind[
            TraceStageKind.WORKFLOW_NODE
        ].stage_id
        assert "private-workflow-input" not in timeline.model_dump_json()
    finally:
        await sdk.close()


class _ChildAfterHighWaterStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.root_run_id: str | None = None
        self.session_id: str | None = None
        self.latest_calls = 0
        self.child_run_id = "run_tail_child"

    async def latest_cursor(self) -> int:
        self.latest_calls += 1
        if self.latest_calls == 2:
            assert self.root_run_id is not None
            assert self.session_id is not None
            await RuntimeCommands(self).start_run(
                self.session_id,
                run_id=self.child_run_id,
                agent_revision="child:1",
                user_input="tail child",
                parent_run_id=self.root_run_id,
            )
        return await super().latest_cursor()


@pytest.mark.asyncio
async def test_trace_retries_when_child_is_created_after_high_water() -> None:
    store = _ChildAfterHighWaterStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        session.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    store.root_run_id = root.run_id
    store.session_id = session.session_id

    timeline = await TraceService(store).timeline(root.run_id)

    assert store.latest_calls >= 3
    assert any(
        stage.kind is TraceStageKind.CHILD
        and stage.entity_id == store.child_run_id
        for stage in timeline.stages
    )


class _InvalidTailTransitionStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.run_id: str | None = None
        self.session_id: str | None = None
        self.latest_calls = 0

    async def latest_cursor(self) -> int:
        self.latest_calls += 1
        if self.latest_calls == 2:
            assert self.run_id is not None
            assert self.session_id is not None
            await self.commit(
                CommitBatch(
                    events=(
                        EventEnvelope.new(
                            type="run.started",
                            schema_version=999,
                            session_id=self.session_id,
                            run_id=self.run_id,
                            sequence=2,
                            payload={"status": "running"},
                        ),
                    )
                )
            )
        return await super().latest_cursor()


@pytest.mark.asyncio
async def test_trace_rejects_invalid_selected_transition_in_tail_window() -> None:
    store = _InvalidTailTransitionStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        session.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    store.run_id = root.run_id
    store.session_id = session.session_id

    with pytest.raises(AgentSDKError) as captured:
        await TraceService(store).timeline(root.run_id)

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.message == "failed to load trace timeline"


@pytest.mark.asyncio
async def test_real_tool_stage_is_parented_to_its_step() -> None:
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
                                        "id": "call_trace",
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

    store = InMemoryStore()
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=provider,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    async def handler(_context: ToolContext, *, value: int) -> object:
        return {"value": value}

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
        handler,
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="tool-trace", model="fake/model"),
            "use tool",
        )
        await handle.result()

        timeline = await sdk.trace.timeline(handle.run_id)

        tool = next(stage for stage in timeline.stages if stage.kind is TraceStageKind.TOOL)
        step = next(stage for stage in timeline.stages if stage.kind is TraceStageKind.STEP)
        assert tool.parent_stage_id == step.stage_id
        events = [
            item.event
            for item in await store.read_events(after_cursor=0)
            if item.event.run_id == handle.run_id
            and item.event.type in {"tool.call.started", "tool.call.completed"}
        ]
        assert len(events) == 2
        assert events[0].schema_version == 2
        assert isinstance(events[0].payload["step_id"], str)
        assert events[0].payload["step_id"]
        assert events[1].schema_version == 1
        assert set(events[1].payload) == {
            "call_id",
            "tool_name",
            "status",
            "content",
            "value",
            "error",
        }
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_public_child_execution_projects_a_child_stage() -> None:
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_provider,
        enable_builtin_tools=False,
    )
    parent_agent = AgentSpec(name="parent", revision="1", model="fake/model")
    sdk.agents.define(parent_agent)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/model"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        parent = await sdk.runs.start(session.session_id, parent_agent, "parent")
        await parent.result()
        child = await sdk.children.spawn(
            parent.run_id,
            "worker:1",
            TaskEnvelope(objective="child objective"),
        )
        completed = await sdk.children.wait(child.run_id, timeout_seconds=1)
        assert completed.status == "completed"

        timeline = await sdk.trace.timeline(parent.run_id)

        child_stage = next(
            stage
            for stage in timeline.stages
            if stage.kind is TraceStageKind.CHILD
        )
        parent_stage = next(
            stage
            for stage in timeline.stages
            if stage.kind is TraceStageKind.RUN and stage.entity_id == parent.run_id
        )
        assert child_stage.entity_id == child.run_id
        assert child_stage.status is TraceStageStatus.COMPLETED
        assert child_stage.parent_stage_id == parent_stage.stage_id
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_public_trace_shape_survives_sqlite_reopen(tmp_path: Path) -> None:
    database = tmp_path / "trace.sqlite3"
    first = AgentSDK.for_test(database_path=database, acompletion=_provider)
    try:
        session = await first.sessions.create(workspaces=[])
        handle = await first.runs.start(
            session.session_id,
            AgentSpec(name="agent", model="fake/model"),
            "private-input",
        )
        await handle.result()
        run_id = handle.run_id
        session_id = session.session_id
        before = await first.trace.timeline(run_id)
    finally:
        await first.close()

    calls = 0

    async def must_not_call(**_: Any) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("historical trace reopen must not call the provider")

    reopened = AgentSDK.for_test(database_path=database, acompletion=must_not_call)
    try:
        after = await reopened.trace.timeline(run_id)
        assert calls == 0
        assert after == before
        assert after.root_kind == "run"
        assert all(stage.session_id == session_id for stage in after.stages)
        assert all(stage.run_id == run_id for stage in after.stages)
        model = next(stage for stage in after.stages if stage.kind is TraceStageKind.MODEL)
        run = next(stage for stage in after.stages if stage.kind is TraceStageKind.RUN)
        assert model.input_refs
        assert model.output_refs
        assert model.cost_usd == 0.125
        assert run.cost_usd == 0.125
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_failed_run_aggregates_model_usage_across_sqlite_reopen(
    tmp_path: Path,
) -> None:
    database = tmp_path / "failed-trace.sqlite3"
    turn = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal turn
        turn += 1
        if turn == 2:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "private provider failure",
                retryable=False,
            )

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_failed_trace",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"value":7}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                    "cost": 0.25,
                },
            }

        return chunks()

    sdk = AgentSDK.for_test(
        database_path=database,
        acompletion=provider,
        permission_default="allow",
        enable_builtin_tools=False,
    )

    async def handler(_context: ToolContext, *, value: int) -> object:
        return {"value": value}

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
        handler,
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="failed-trace", model="fake/model"),
            "private input",
        )
        with pytest.raises(AgentSDKError):
            await handle.result()
        run_id = handle.run_id
        before = await sdk.trace.timeline(run_id)
        model = next(
            stage
            for stage in before.stages
            if stage.kind is TraceStageKind.MODEL and stage.usage is not None
        )
        run = next(
            stage
            for stage in before.stages
            if stage.kind is TraceStageKind.RUN and stage.entity_id == run_id
        )
        assert model.cost_usd == 0.25
        assert run.usage == model.usage
        assert run.cost_usd == 0.25
    finally:
        await sdk.close()

    reopened = AgentSDK.for_test(database_path=database, acompletion=provider)
    try:
        after = await reopened.trace.timeline(run_id)
        assert after == before
    finally:
        await reopened.close()
