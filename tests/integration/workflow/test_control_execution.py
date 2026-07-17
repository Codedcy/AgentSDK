from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_sdk import AgentNode, AgentSDK, AgentSDKError, AgentSpec, WorkflowIR
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.workflow import WorkflowNodeStatus, WorkflowRunStatus


def _chunks(text: str) -> AsyncIterator[dict[str, object]]:
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


def _control_yaml(*, enabled: bool = True, loop_limit: int = 3) -> str:
    return f"""
api_version: agent-sdk/v1
kind: Workflow
name: controlled
inputs:
  enabled: {str(enabled).lower()}
steps:
  - id: choose
    kind: condition
    when: {{path: inputs.enabled, op: eq, value: true}}
    then_steps:
      - {{id: selected, kind: agent, agent_revision: worker:1, input: selected}}
    else_steps:
      - {{id: unselected, kind: agent, agent_revision: worker:1, input: unselected}}
  - id: improve
    kind: loop
    until: {{path: outputs.review.done, op: exists}}
    max_iterations: {loop_limit}
    body:
      - {{id: review, kind: agent, agent_revision: worker:1, input: review}}
  - {{id: finish, kind: agent, agent_revision: worker:1, input: finish}}
"""


@pytest.mark.asyncio
async def test_condition_and_two_iteration_loop_execute_only_selected_work() -> None:
    calls: list[str] = []
    review_calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal review_calls
        prompt = str(params["messages"][-1]["content"])
        calls.append(prompt)
        if prompt == "review":
            review_calls += 1
            return _chunks('{"done":true}' if review_calls == 2 else '{"progress":1}')
        return _chunks(prompt)

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])

    compiled = sdk.workflows.compile(_control_yaml())
    assert compiled.schema_version == 2
    assert (await sdk.sessions.get(session.session_id)).active_workflow_run_ids == ()
    assert calls == []

    handle = await sdk.workflows.start(session.session_id, compiled)
    result = await handle.result()
    snapshot = await sdk.workflows.get(handle.workflow_run_id)

    assert result.status is WorkflowRunStatus.COMPLETED
    assert calls == ["selected", "review", "review", "finish"]
    assert review_calls == 2
    assert result.output_text == "finish"
    assert result.usage.total_tokens == 12
    statuses = {node.node_id: node.status for node in result.nodes}
    assert statuses == {
        "selected": WorkflowNodeStatus.COMPLETED,
        "unselected": WorkflowNodeStatus.PENDING,
        "review": WorkflowNodeStatus.COMPLETED,
        "finish": WorkflowNodeStatus.COMPLETED,
    }
    review = next(node for node in snapshot.nodes if node.node_id == "review")
    assert review.execution_count == 2
    event_types = [item.event.type async for item in handle.events()]
    assert event_types.count("workflow.condition.selected") == 1
    assert event_types.count("workflow.loop.iteration") == 2
    assert event_types.count("workflow.loop.exited") == 1
    assert event_types[-1] == "workflow.completed"
    await sdk.close()


@pytest.mark.asyncio
async def test_false_condition_executes_only_else_branch() -> None:
    calls: list[str] = []

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        prompt = str(params["messages"][-1]["content"])
        calls.append(prompt)
        if prompt == "review":
            return _chunks('{"done":true}')
        return _chunks(prompt)

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])

    result = await (
        await sdk.workflows.start(
            session.session_id,
            _control_yaml(enabled=False),
        )
    ).result()

    assert result.status is WorkflowRunStatus.COMPLETED
    assert calls == ["unselected", "review", "finish"]
    selected = next(node for node in result.nodes if node.node_id == "selected")
    assert selected.status is WorkflowNodeStatus.PENDING
    await sdk.close()


@pytest.mark.asyncio
async def test_loop_limit_failure_is_durable() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _chunks('{"progress":1}')

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.workflows.start(
        session.session_id,
        _control_yaml(loop_limit=2),
    )

    with pytest.raises(AgentSDKError, match="reached its iteration limit"):
        await handle.result()

    snapshot = await sdk.workflows.get(handle.workflow_run_id)
    assert snapshot.status is WorkflowRunStatus.FAILED
    assert snapshot.error is not None
    assert snapshot.error.code == "workflow_loop_limit"
    review = next(node for node in snapshot.nodes if node.node_id == "review")
    assert review.execution_count == 2
    assert snapshot.usage is None
    await sdk.close()


@pytest.mark.asyncio
async def test_schema_v1_execution_and_events_remain_unchanged() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _chunks("legacy")

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    workflow = WorkflowIR.create(
        name="legacy",
        nodes=(
            AgentNode(
                id="legacy",
                agent_revision="worker:1",
                input="legacy",
            ),
        ),
        edges=(),
    )

    handle = await sdk.workflows.start(session.session_id, workflow)
    result = await handle.result()
    events = [item.event async for item in handle.events()]

    assert result.output_text == "legacy"
    assert result.nodes[0].execution_count == 0
    assert [
        event.type for event in events
    ] == [
        "workflow.started",
        "workflow.node.started",
        "workflow.node.completed",
        "workflow.completed",
    ]
    assert all(
        "execution_index" not in event.payload
        for event in events
    )
    await sdk.close()
