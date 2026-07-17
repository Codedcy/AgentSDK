from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import AgentSDK, AgentSDKError, AgentSpec, ErrorCode, RunStatus
from agent_sdk.storage.base import CommitBatch, CommitResult, StateStore
from agent_sdk.storage.sqlite import SQLiteStore
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


def _workflow_yaml() -> str:
    return """
api_version: agent-sdk/v1
kind: Workflow
name: recover-control
inputs: {enabled: true}
steps:
  - id: choose
    kind: condition
    when: {path: inputs.enabled, op: eq, value: true}
    then_steps:
      - {id: selected, kind: agent, agent_revision: worker:1, input: selected}
    else_steps:
      - {id: skipped, kind: agent, agent_revision: worker:1, input: skipped}
  - id: improve
    kind: loop
    until: {path: outputs.review.done, op: exists}
    max_iterations: 3
    body:
      - {id: review, kind: agent, agent_revision: worker:1, input: review}
  - {id: finish, kind: agent, agent_revision: worker:1, input: finish}
"""


class _CancelAfterNthEventStore:
    def __init__(
        self,
        delegate: StateStore,
        event_type: str,
        occurrence: int,
    ) -> None:
        self.delegate = delegate
        self.event_type = event_type
        self.occurrence = occurrence
        self.seen = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        result = await self.delegate.commit(batch)
        self.seen += sum(
            event.type == self.event_type for event in batch.events
        )
        if self.seen == self.occurrence:
            self.seen += 1
            raise asyncio.CancelledError
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "occurrence", "calls_before"),
    (
        ("workflow.condition.selected", 1, ()),
        ("workflow.loop.iteration", 2, ("selected", "review")),
        ("workflow.node.completed", 1, ("selected",)),
    ),
)
async def test_sqlite_restart_does_not_repeat_persisted_logical_execution(
    tmp_path: Path,
    event_type: str,
    occurrence: int,
    calls_before: tuple[str, ...],
) -> None:
    calls: list[str] = []
    review_calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal review_calls
        prompt = str(params["messages"][-1]["content"])
        calls.append(prompt)
        if prompt == "review":
            review_calls += 1
            return _chunks(
                '{"done":true}' if review_calls == 2 else '{"progress":1}'
            )
        return _chunks(prompt)

    database = tmp_path / f"{event_type}-{occurrence}.sqlite3"
    sqlite = await SQLiteStore.open(database)
    store = _CancelAfterNthEventStore(sqlite, event_type, occurrence)
    first = AgentSDK.for_test(store=store, acompletion=provider)
    first.agents.define(
        AgentSpec(name="worker", revision="1", model="fake/worker")
    )
    session = await first.sessions.create(workspaces=[])
    handle = await first.workflows.start(session.session_id, _workflow_yaml())

    with pytest.raises(asyncio.CancelledError):
        await handle.result()
    assert calls == list(calls_before)
    workflow_run_id = handle.workflow_run_id
    await first.close()
    await sqlite.close()

    reopened = AgentSDK.for_test(database_path=database, acompletion=provider)
    reopened.agents.define(
        AgentSpec(name="worker", revision="1", model="fake/worker")
    )
    try:
        await reopened.recovery.scan()
        recovered = await reopened.recovery.recover_workflow(workflow_run_id)
        result = await recovered.result()

        assert result.status is WorkflowRunStatus.COMPLETED
        assert calls == ["selected", "review", "review", "finish"]
        assert review_calls == 2
        assert result.output_text == "finish"
        assert result.usage.total_tokens == 12
        review = next(node for node in result.nodes if node.node_id == "review")
        assert review.execution_count == 2
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_unknown_child_outcome_stays_recoverable_without_replay(
    tmp_path: Path,
) -> None:
    calls = 0
    child_started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _chunks("parent")
        child_started.set()
        await release.wait()
        raise AssertionError("abandoned child provider call must not finish")

    definition = """
api_version: agent-sdk/v1
kind: Workflow
name: recover-child
inputs: {enabled: true}
steps:
  - id: choose
    kind: condition
    when: {path: inputs.enabled, op: eq, value: true}
    then_steps:
      - {id: parent, kind: agent, agent_revision: worker:1, input: parent}
      - id: child
        kind: agent
        agent_revision: worker:1
        input: child
        run_as: child
        success_criteria: [return child result]
    else_steps:
      - {id: skipped, kind: agent, agent_revision: worker:1, input: skipped}
"""
    database = tmp_path / "unknown-child.sqlite3"
    sqlite = await SQLiteStore.open(database)
    first = AgentSDK.for_test(store=sqlite, acompletion=blocking_provider)
    first.agents.define(
        AgentSpec(name="worker", revision="1", model="fake/worker")
    )
    session = await first.sessions.create(workspaces=[])
    handle = await first.workflows.start(session.session_id, definition)
    await asyncio.wait_for(child_started.wait(), timeout=10)
    workflow = await first.workflows.get(handle.workflow_run_id)
    child = next(node for node in workflow.nodes if node.node_id == "child")
    assert child.run_id is not None
    lease = await sqlite.get_run_lease(child.run_id)
    assert lease is not None
    first._recovery_scanner._clock = (  # type: ignore[attr-defined]
        lambda: lease.expires_at + timedelta(seconds=1)
    )
    await first.recovery.scan()
    interrupted = await first.runs.get(child.run_id)
    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.workflow_node_execution == 1
    tasks = tuple(first._active_tasks)  # type: ignore[attr-defined]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.gather(handle.result(), return_exceptions=True)
    release.set()
    await first.close()
    await sqlite.close()

    async def forbidden_provider(**_: Any) -> Any:
        raise AssertionError("unknown child outcome must not replay Provider")

    reopened = AgentSDK.for_test(
        database_path=database,
        acompletion=forbidden_provider,
    )
    reopened.agents.define(
        AgentSpec(name="worker", revision="1", model="fake/worker")
    )
    try:
        await reopened.recovery.scan()
        durable_run = await reopened.runs.get(child.run_id)
        durable_workflow = await reopened.workflows.get(handle.workflow_run_id)
        assert durable_run.status is RunStatus.INTERRUPTED
        assert durable_workflow.status is WorkflowRunStatus.RUNNING

        recovery = await reopened.recovery.recover_workflow(
            handle.workflow_run_id
        )
        with pytest.raises(AgentSDKError) as required:
            await recovery.result()
        assert required.value.code is ErrorCode.CONFLICT
        assert required.value.retryable is True
        assert calls == 2
        assert await reopened.recovery.pending_requests(child.run_id)
    finally:
        await reopened.close()
