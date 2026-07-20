from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_sdk import AgentSDK, AgentSDKError, AgentSpec, ErrorCode
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.workflow import WorkflowRunStatus


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


def _branch_child_yaml(*, enabled: bool) -> str:
    return f"""
api_version: agent-sdk/v1
kind: Workflow
name: branch-child
inputs: {{enabled: {str(enabled).lower()}}}
steps:
  - id: choose
    kind: condition
    when: {{path: inputs.enabled, op: eq, value: true}}
    then_steps:
      - {{id: true_parent, kind: agent, agent_revision: worker:1, input: "true"}}
    else_steps:
      - {{id: false_parent, kind: agent, agent_revision: worker:1, input: "false"}}
  - id: child
    kind: agent
    agent_revision: worker:1
    input: child
    run_as: child
    success_criteria: [return child result]
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "selected_parent"),
    ((True, "true_parent"), (False, "false_parent")),
)
async def test_branch_child_binds_to_selected_parent(
    enabled: bool,
    selected_parent: str,
) -> None:
    calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _chunks("done")

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    try:
        handle = await sdk.workflows.start(
            session.session_id,
            _branch_child_yaml(enabled=enabled),
        )
        result = await handle.result()

        assert result.status is WorkflowRunStatus.COMPLETED
        parent = next(
            node for node in result.nodes if node.node_id == selected_parent
        )
        child = next(node for node in result.nodes if node.node_id == "child")
        assert parent.run_id is not None
        assert child.run_id is not None
        child_run = await sdk.runs.get(child.run_id)
        assert child_run.parent_run_id == parent.run_id
        assert calls == 2
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_root_child_fails_before_provider_dispatch() -> None:
    calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _chunks("unexpected")

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    definition = """
api_version: agent-sdk/v1
kind: Workflow
name: root-child
steps:
  - id: child
    kind: agent
    agent_revision: worker:1
    input: child
    run_as: child
    success_criteria: [return child result]
"""
    try:
        handle = await sdk.workflows.start(session.session_id, definition)
        with pytest.raises(AgentSDKError) as invalid:
            await handle.result()
        assert invalid.value.code is ErrorCode.INVALID_STATE
        assert invalid.value.message == "root workflow node cannot be a child"
        assert calls == 0
    finally:
        await sdk.close()
