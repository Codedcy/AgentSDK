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


def _repeated_child_loop_yaml() -> str:
    return """
api_version: agent-sdk/v1
kind: Workflow
name: repeated-child-loop
steps:
  - {id: seed, kind: agent, agent_revision: worker:1, input: seed}
  - id: repeat
    kind: loop
    until: {path: outputs.child.done, op: exists}
    max_iterations: 2
    body:
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
async def test_consecutive_children_form_exact_run_chain() -> None:
    calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _chunks("done")

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    definition = """
api_version: agent-sdk/v1
kind: Workflow
name: consecutive-children
inputs: {enabled: true}
steps:
  - id: choose
    kind: condition
    when: {path: inputs.enabled, op: eq, value: true}
    then_steps:
      - {id: selected, kind: agent, agent_revision: worker:1, input: selected}
    else_steps:
      - {id: skipped, kind: agent, agent_revision: worker:1, input: skipped}
  - id: child_one
    kind: agent
    agent_revision: worker:1
    input: child one
    run_as: child
    success_criteria: [return first child result]
  - id: child_two
    kind: agent
    agent_revision: worker:1
    input: child two
    run_as: child
    success_criteria: [return second child result]
"""
    try:
        result = await (
            await sdk.workflows.start(session.session_id, definition)
        ).result()

        assert result.status is WorkflowRunStatus.COMPLETED
        selected = next(node for node in result.nodes if node.node_id == "selected")
        child_one = next(node for node in result.nodes if node.node_id == "child_one")
        child_two = next(node for node in result.nodes if node.node_id == "child_two")
        assert selected.run_id is not None
        assert child_one.run_id is not None
        assert child_two.run_id is not None
        child_one_run = await sdk.runs.get(child_one.run_id)
        child_two_run = await sdk.runs.get(child_two.run_id)
        assert child_one_run.parent_run_id == selected.run_id
        assert child_two_run.parent_run_id == child_one.run_id
        assert calls == 3
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_repeated_child_loop_binds_to_previous_generation() -> None:
    calls = 0
    child_calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls, child_calls
        calls += 1
        if calls == 1:
            return _chunks("seed")
        child_calls += 1
        return _chunks('{"done":true}' if child_calls == 2 else '{"progress":1}')

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    session = await sdk.sessions.create(workspaces=[])
    try:
        result = await (
            await sdk.workflows.start(
                session.session_id,
                _repeated_child_loop_yaml(),
            )
        ).result()

        assert result.status is WorkflowRunStatus.COMPLETED
        seed = next(node for node in result.nodes if node.node_id == "seed")
        child = next(node for node in result.nodes if node.node_id == "child")
        assert seed.run_id is not None
        assert child.run_id is not None
        generation_two = await sdk.runs.get(child.run_id)
        assert generation_two.workflow_node_execution == 2
        assert generation_two.parent_run_id is not None
        generation_one = await sdk.runs.get(generation_two.parent_run_id)
        assert generation_one.workflow_node_execution == 1
        assert generation_one.parent_run_id == seed.run_id
        assert calls == 3
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
