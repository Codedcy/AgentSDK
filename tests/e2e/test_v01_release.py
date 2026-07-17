from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    PermissionDecision,
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
