from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    ReconciliationAction,
    ToolContext,
)
from agent_sdk.errors import SessionBusyError
from agent_sdk.runtime.models import RunStatus, SessionStatus
from agent_sdk.runtime.reconciliation import RunCheckpointPhase
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.subagents.service import render_task_envelope
from agent_sdk.tools.models import ToolResult, ToolResultStatus, ToolRetryPolicy, ToolSpec
from agent_sdk.workflow import WorkflowNodeStatus, WorkflowRunStatus


AGENT = AgentSpec(name="planner", revision="1", model="fake/planner")
WORKER = AgentSpec(name="worker", revision="1", model="fake/worker")
TOOL = ToolSpec(
    name="inspect",
    description="Inspect one value",
    input_schema={
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    },
    version="1",
    source="application",
    effects=("external",),
    retry_policy=ToolRetryPolicy.NEVER,
)
ONE_NODE_DEFINITION = {
    "api_version": "agent-sdk/v1",
    "kind": "Workflow",
    "name": "confirmed-outcome",
    "nodes": [
        {
            "id": "plan",
            "kind": "agent",
            "agent_revision": "planner:1",
            "input": "make a plan",
        }
    ],
    "edges": [],
}
TWO_NODE_DEFINITION = {
    "api_version": "agent-sdk/v1",
    "kind": "Workflow",
    "name": "confirmed-outcome-child",
    "nodes": [
        {
            "id": "plan",
            "kind": "agent",
            "agent_revision": "planner:1",
            "input": "make a plan",
        },
        {
            "id": "verify",
            "kind": "agent",
            "agent_revision": "worker:1",
            "input": "verify the plan",
            "run_as": "child",
            "success_criteria": ["return verification"],
            "evidence_refs": ["artifact:plan"],
            "allowed_tools": ["inspect"],
            "workspace_scopes": ["workspace"],
        },
    ],
    "edges": [{"source": "plan", "target": "verify"}],
}
_TIMEOUT = 10.0


class _BlockingCompletion:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, **_: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        raise AssertionError("the abandoned Provider call must not complete")


class _ToolCallCompletion:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_confirmed_workflow",
                                    "function": {
                                        "name": TOOL.name,
                                        "arguments": '{"value":7}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

        return chunks()


class _BlockingTool:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, _: ToolContext, value: int) -> object:
        assert value == 7
        self.calls += 1
        self.started.set()
        await self.release.wait()
        raise AssertionError("the abandoned Tool call must not complete")


class _FinalCompletion:
    def __init__(self, text: str = "done") -> None:
        self.text = text
        self.calls = 0
        self.messages: list[tuple[dict[str, Any], ...]] = []

    async def __call__(self, **params: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        self.messages.append(tuple(dict(message) for message in params["messages"]))

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {"delta": {"content": self.text}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

        return chunks()


class _LeaseBarrier:
    def __init__(self) -> None:
        self.arrivals = 0
        self.ready = asyncio.Event()


class _LeaseBarrierStore:
    def __init__(self, delegate: Any, barrier: _LeaseBarrier) -> None:
        self.delegate = delegate
        self.barrier = barrier

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def acquire_lease(self, **values: Any) -> Any:
        self.barrier.arrivals += 1
        if self.barrier.arrivals == 2:
            self.barrier.ready.set()
        await asyncio.wait_for(self.barrier.ready.wait(), timeout=_TIMEOUT)
        return await self.delegate.acquire_lease(**values)


class _ProjectionBarrier:
    def __init__(self) -> None:
        self.arrivals = 0
        self.ready = asyncio.Event()
        self.committed = asyncio.Event()


class _ProjectionBarrierStore:
    def __init__(
        self,
        delegate: Any,
        barrier: _ProjectionBarrier,
        *,
        winner: bool,
    ) -> None:
        self.delegate = delegate
        self.barrier = barrier
        self.winner = winner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: Any) -> Any:
        if any(event.type == "workflow.node.completed" for event in batch.events):
            self.barrier.arrivals += 1
            if self.barrier.arrivals == 2:
                self.barrier.ready.set()
            await asyncio.wait_for(self.barrier.ready.wait(), timeout=_TIMEOUT)
            if self.winner:
                result = await self.delegate.commit(batch)
                self.barrier.committed.set()
                return result
            await asyncio.wait_for(self.barrier.committed.wait(), timeout=_TIMEOUT)
        return await self.delegate.commit(batch)


class _CancelAfterWorkflowCommitStore:
    def __init__(self, delegate: Any, event_type: str) -> None:
        self.delegate = delegate
        self.event_type = event_type
        self.fired = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: Any) -> Any:
        result = await self.delegate.commit(batch)
        if not self.fired and any(
            event.type == self.event_type for event in batch.events
        ):
            self.fired = True
            raise asyncio.CancelledError
        return result


async def _open_store(backend: str, path: Path) -> Any:
    if backend == "memory":
        return InMemoryStore()
    return await SQLiteStore.open(path)


def _workflow_yaml(definition: dict[str, Any] = ONE_NODE_DEFINITION) -> str:
    return yaml.safe_dump(definition, sort_keys=False)


def _register(
    sdk: AgentSDK,
    *,
    include_worker: bool = False,
    tool_handler: Any | None = None,
) -> None:
    sdk.agents.define(AGENT)
    if include_worker:
        sdk.agents.define(WORKER)
    if tool_handler is not None:
        sdk.tools.register(TOOL, tool_handler)


async def _cancel_sdk_tasks(sdk: AgentSDK) -> None:
    tasks = tuple(sdk._active_tasks)  # type: ignore[attr-defined]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _interrupt_active_workflow(
    sdk: AgentSDK,
    store: Any,
    workflow_handle: Any,
    entered: asyncio.Event,
) -> tuple[str, str]:
    await asyncio.wait_for(entered.wait(), timeout=_TIMEOUT)
    workflow = await sdk.workflows.get(workflow_handle.workflow_run_id)
    run_id = workflow.nodes[0].run_id
    assert run_id is not None
    lease = await store.get_run_lease(run_id)
    assert lease is not None
    sdk._recovery_scanner._clock = (  # type: ignore[attr-defined]
        lambda: lease.expires_at + timedelta(seconds=1)
    )
    await sdk.recovery.scan()
    assert (await sdk.runs.get(run_id)).status is RunStatus.INTERRUPTED
    await _cancel_sdk_tasks(sdk)
    await asyncio.gather(workflow_handle.result(), return_exceptions=True)
    await sdk.close()
    return workflow.workflow_run_id, run_id


async def _admit_workflow_reconciliation(
    sdk: AgentSDK,
    workflow_run_id: str,
    run_id: str,
) -> Any:
    handle = await sdk.recovery.recover_workflow(workflow_run_id)
    with pytest.raises(AgentSDKError) as required:
        await handle.result()
    assert required.value.code is ErrorCode.CONFLICT
    assert required.value.message == "recovery required"
    requests = await sdk.recovery.pending_requests(run_id)
    assert len(requests) == 1
    return requests[0]


def _provider_result(projection: str) -> dict[str, object]:
    if projection == "failed":
        return {
            "disposition": "failed",
            "error_code": ErrorCode.INTERNAL.value,
            "retryable": True,
        }
    return {
        "disposition": "completed",
        "finish_reason": "stop",
        "text": "operator-confirmed",
        "tool_call": None,
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": 7,
        },
    }


async def _forbidden_tool(_: ToolContext, value: int) -> object:
    del value
    raise AssertionError("confirmed Tool side effect must never repeat")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("projection", ("text", "failed"))
async def test_confirmed_model_projects_only_on_explicit_workflow_recovery(
    backend: str,
    projection: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"confirmed-model-workflow-{projection}.db"
    store = await _open_store(backend, path)
    blocking = _BlockingCompletion()
    owner = AgentSDK.for_test(store=store, acompletion=blocking)
    _register(owner)
    session = await owner.sessions.create(workspaces=[])
    original = await owner.workflows.start(session.session_id, _workflow_yaml())
    workflow_run_id = ""
    run_id = ""
    reopened: AgentSDK | None = None
    try:
        workflow_run_id, run_id = await _interrupt_active_workflow(
            owner,
            store,
            original,
            blocking.started,
        )
        blocking.release.set()
        if isinstance(store, SQLiteStore):
            await store.close()
            store = await SQLiteStore.open(path)

        async def forbidden_provider(**_: Any) -> Any:
            raise AssertionError("terminal confirmed Run must not call Provider")

        reopened = AgentSDK.for_test(store=store, acompletion=forbidden_provider)
        _register(reopened)
        request = await _admit_workflow_reconciliation(
            reopened,
            workflow_run_id,
            run_id,
        )
        before_workflow = await reopened.workflows.get(workflow_run_id)
        before_node = await store.get_snapshot(
            "workflow_node", before_workflow.nodes[0].entity_id
        )
        assert before_workflow.nodes[0].status is WorkflowNodeStatus.RUNNING

        actor = {"type": "operator", "id": "workflow-test"}
        evidence = {"provider_result": _provider_result(projection)}
        resolved = await reopened.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=actor,
            evidence=evidence,
        )

        assert await reopened.workflows.get(workflow_run_id) == before_workflow
        assert (
            await store.get_snapshot("workflow_node", before_workflow.nodes[0].entity_id)
            == before_node
        )
        terminal_run = await reopened.runs.get(run_id)
        assert terminal_run.status is (
            RunStatus.FAILED if projection == "failed" else RunStatus.COMPLETED
        )

        projected = await reopened.recovery.recover_workflow(workflow_run_id)
        if projection == "failed":
            with pytest.raises(AgentSDKError) as failed:
                await projected.result()
            assert failed.value.code is ErrorCode.INTERNAL
            durable = await reopened.workflows.get(workflow_run_id)
            assert durable.status is WorkflowRunStatus.FAILED
            assert durable.nodes[0].status is WorkflowNodeStatus.FAILED
            assert durable.error is not None
            assert durable.error.code == terminal_run.error.code  # type: ignore[union-attr]
            assert durable.nodes[0].error == durable.error
        else:
            result = await projected.result()
            assert result.status is WorkflowRunStatus.COMPLETED
            assert result.output_text == "operator-confirmed"
            assert result.usage.total_tokens == 7
            durable = await reopened.workflows.get(workflow_run_id)
            assert durable.nodes[0].output_text == "operator-confirmed"
            assert durable.nodes[0].usage == terminal_run.usage

        durable_session = await reopened.sessions.get(session.session_id)
        assert durable_session.active_run_ids == ()
        assert durable_session.active_workflow_run_ids == ()
        events = await store.read_events(after_cursor=0, session_id=session.session_id)
        expected_node_event = (
            "workflow.node.failed" if projection == "failed" else "workflow.node.completed"
        )
        expected_workflow_event = (
            "workflow.failed" if projection == "failed" else "workflow.completed"
        )
        assert sum(item.event.type == expected_node_event for item in events) == 1
        assert sum(item.event.type == expected_workflow_event for item in events) == 1
        assert sum(item.event.type == "session.workflow.detached" for item in events) == 1
        cursor = await store.latest_cursor()
        assert (
            await reopened.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor=actor,
                evidence=evidence,
            )
            == resolved
        )
        assert await store.latest_cursor() == cursor
        assert blocking.calls == 1
    finally:
        blocking.release.set()
        if reopened is not None:
            await reopened.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize("tool_projection", ("succeeded", "failed"))
async def test_confirmed_tool_result_resumes_workflow_without_repeating_tool(
    backend: str,
    tool_projection: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"confirmed-tool-workflow-{tool_projection}.db"
    store = await _open_store(backend, path)
    first_provider = _ToolCallCompletion()
    blocking_tool = _BlockingTool()
    owner = AgentSDK.for_test(
        store=store,
        acompletion=first_provider,
        permission_default="allow",
    )
    _register(owner, tool_handler=blocking_tool)
    session = await owner.sessions.create(workspaces=[])
    original = await owner.workflows.start(session.session_id, _workflow_yaml())
    reopened: AgentSDK | None = None
    try:
        workflow_run_id, run_id = await _interrupt_active_workflow(
            owner,
            store,
            original,
            blocking_tool.started,
        )
        blocking_tool.release.set()
        if isinstance(store, SQLiteStore):
            await store.close()
            store = await SQLiteStore.open(path)

        final_provider = _FinalCompletion("workflow-finished")
        reopened = AgentSDK.for_test(
            store=store,
            acompletion=final_provider,
            permission_default="allow",
        )
        _register(reopened, tool_handler=_forbidden_tool)
        request = await _admit_workflow_reconciliation(
            reopened,
            workflow_run_id,
            run_id,
        )
        if tool_projection == "succeeded":
            result_model = ToolResult.succeeded(
                "call_confirmed_workflow",
                TOOL.name,
                {"confirmed": True},
            )
        else:
            result_model = ToolResult.normalized_error(
                "call_confirmed_workflow",
                TOOL.name,
                ToolResultStatus.FAILED,
                "operator confirmed failure",
            )
        tool_result = result_model.model_dump(mode="json")
        before_workflow = await reopened.workflows.get(workflow_run_id)
        before_node = await store.get_snapshot(
            "workflow_node", before_workflow.nodes[0].entity_id
        )
        actor = {"type": "operator", "id": "workflow-test"}
        evidence = {"tool_result": tool_result}
        resolved = await reopened.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=actor,
            evidence=evidence,
        )

        assert await reopened.workflows.get(workflow_run_id) == before_workflow
        assert (
            await store.get_snapshot("workflow_node", before_workflow.nodes[0].entity_id)
            == before_node
        )
        interrupted = await reopened.runs.get(run_id)
        assert interrupted.status is RunStatus.INTERRUPTED
        checkpoint = await store.get_run_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.READY_FOR_MODEL
        assert checkpoint.tool_results[-1].model_dump(mode="json") == tool_result

        workflow_result = await (
            await reopened.recovery.recover_workflow(workflow_run_id)
        ).result()
        assert workflow_result.status is WorkflowRunStatus.COMPLETED
        assert workflow_result.output_text == "workflow-finished"
        assert final_provider.calls == 1
        assert blocking_tool.calls == 1
        assert final_provider.messages[-1][-1] == {
            "role": "tool",
            "tool_call_id": "call_confirmed_workflow",
            "name": TOOL.name,
            "content": tool_result["content"],
        }
        terminal_run = await reopened.runs.get(run_id)
        assert terminal_run.tool_results[-1].model_dump(mode="json") == tool_result
        tree = await reopened.queries.execution_tree(run_id)
        assert tree.root_run_id == run_id
        assert [node.snapshot for node in tree.nodes] == [terminal_run]
        assert (await reopened.sessions.get(session.session_id)).active_workflow_run_ids == ()

        cursor = await store.latest_cursor()
        assert (
            await reopened.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor=actor,
                evidence=evidence,
            )
            == resolved
        )
        assert await store.latest_cursor() == cursor
        assert final_provider.calls == 1
        assert blocking_tool.calls == 1
    finally:
        blocking_tool.release.set()
        if reopened is not None:
            await reopened.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_first_node_starts_exact_child_once_and_queries_tree(
    backend: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "confirmed-model-multinode.db"
    store = await _open_store(backend, path)
    blocking = _BlockingCompletion()
    owner = AgentSDK.for_test(
        store=store,
        acompletion=blocking,
        permission_default="allow",
    )
    _register(owner, include_worker=True, tool_handler=_forbidden_tool)
    session = await owner.sessions.create(workspaces=[])
    original = await owner.workflows.start(
        session.session_id,
        _workflow_yaml(TWO_NODE_DEFINITION),
    )
    reopened: AgentSDK | None = None
    try:
        workflow_run_id, root_run_id = await _interrupt_active_workflow(
            owner,
            store,
            original,
            blocking.started,
        )
        blocking.release.set()
        if isinstance(store, SQLiteStore):
            await store.close()
            store = await SQLiteStore.open(path)

        worker_provider = _FinalCompletion("verified")
        reopened = AgentSDK.for_test(
            store=store,
            acompletion=worker_provider,
            permission_default="allow",
        )
        _register(reopened, include_worker=True, tool_handler=_forbidden_tool)
        request = await _admit_workflow_reconciliation(
            reopened,
            workflow_run_id,
            root_run_id,
        )
        evidence = {"provider_result": _provider_result("text")}
        actor = {"type": "operator", "id": "workflow-test"}
        resolved = await reopened.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=actor,
            evidence=evidence,
        )
        before_projection = await reopened.workflows.get(workflow_run_id)
        assert before_projection.nodes[0].status is WorkflowNodeStatus.RUNNING
        assert before_projection.nodes[1].status is WorkflowNodeStatus.PENDING

        result = await (
            await reopened.recovery.recover_workflow(workflow_run_id)
        ).result()

        assert result.status is WorkflowRunStatus.COMPLETED
        assert result.output_text == "verified"
        assert result.usage.total_tokens == 9
        assert worker_provider.calls == 1
        parent_id = result.nodes[0].run_id
        child_id = result.nodes[1].run_id
        assert parent_id == root_run_id
        assert child_id is not None
        child = await reopened.runs.get(child_id)
        assert child.parent_run_id == parent_id
        assert child.task_envelope is not None
        assert child.user_input == render_task_envelope(child.task_envelope)
        assert child.workflow_run_id == workflow_run_id
        assert child.workflow_node_id == "verify"
        tree = await reopened.queries.execution_tree(root_run_id)
        assert [node.snapshot.run_id for node in tree.nodes] == [root_run_id, child_id]
        assert [node.parent_run_id for node in tree.nodes] == [None, root_run_id]
        events = await store.read_events(after_cursor=0, session_id=session.session_id)
        assert sum(item.event.type == "workflow.node.completed" for item in events) == 2
        assert sum(item.event.type == "workflow.node.started" for item in events) == 2
        assert sum(item.event.type == "workflow.completed" for item in events) == 1

        cursor = await store.latest_cursor()
        assert (
            await reopened.recovery.resolve(
                request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor=actor,
                evidence=evidence,
            )
            == resolved
        )
        assert await store.latest_cursor() == cursor
    finally:
        blocking.release.set()
        if reopened is not None:
            await reopened.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_workflow_recovery_rejects_corrupt_confirmed_terminal_run(
    backend: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt-confirmed-terminal-run.db"
    store = await _open_store(backend, path)
    blocking = _BlockingCompletion()
    owner = AgentSDK.for_test(store=store, acompletion=blocking)
    _register(owner)
    session = await owner.sessions.create(workspaces=[])
    original = await owner.workflows.start(session.session_id, _workflow_yaml())
    reopened: AgentSDK | None = None
    try:
        workflow_run_id, run_id = await _interrupt_active_workflow(
            owner,
            store,
            original,
            blocking.started,
        )
        blocking.release.set()
        if isinstance(store, SQLiteStore):
            await store.close()
            store = await SQLiteStore.open(path)

        provider_calls = 0

        async def forbidden_provider(**_: Any) -> Any:
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("corrupt terminal Run must fail before Provider")

        reopened = AgentSDK.for_test(store=store, acompletion=forbidden_provider)
        _register(reopened)
        request = await _admit_workflow_reconciliation(
            reopened,
            workflow_run_id,
            run_id,
        )
        await reopened.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator", "id": "workflow-test"},
            evidence={"provider_result": _provider_result("text")},
        )
        raw_run = await store.get_snapshot("run", run_id)
        assert raw_run is not None
        corrupted = {**raw_run, "output_text": "forged-terminal-output"}
        if isinstance(store, InMemoryStore):
            key = ("run", run_id)
            store._snapshots[key] = store._snapshots[key]._replace(data=corrupted)
        else:
            await store._connection.execute(
                "UPDATE snapshots SET data_json = ? "
                "WHERE kind = 'run' AND entity_id = ?",
                (
                    json.dumps(
                        corrupted,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    run_id,
                ),
            )
            await store._connection.commit()
        workflow_before = await reopened.workflows.get(workflow_run_id)
        node_before = await store.get_snapshot(
            "workflow_node", workflow_before.nodes[0].entity_id
        )
        cursor_before = await store.latest_cursor()

        handle = await reopened.recovery.recover_workflow(workflow_run_id)
        with pytest.raises(AgentSDKError):
            await handle.result()

        assert await reopened.workflows.get(workflow_run_id) == workflow_before
        assert (
            await store.get_snapshot("workflow_node", workflow_before.nodes[0].entity_id)
            == node_before
        )
        assert await store.latest_cursor() == cursor_before
        assert provider_calls == 0
    finally:
        blocking.release.set()
        if reopened is not None:
            await reopened.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_tool_replay_survives_later_workflow_reconciliation(
    backend: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "later-workflow-reconciliation.db"
    store = await _open_store(backend, path)
    first_provider = _ToolCallCompletion()
    blocking_tool = _BlockingTool()
    owner = AgentSDK.for_test(
        store=store,
        acompletion=first_provider,
        permission_default="allow",
    )
    _register(owner, tool_handler=blocking_tool)
    session = await owner.sessions.create(workspaces=[])
    original = await owner.workflows.start(session.session_id, _workflow_yaml())
    model_owner: AgentSDK | None = None
    final_sdk: AgentSDK | None = None
    blocking_model = _BlockingCompletion()
    try:
        workflow_run_id, run_id = await _interrupt_active_workflow(
            owner,
            store,
            original,
            blocking_tool.started,
        )
        blocking_tool.release.set()
        if isinstance(store, SQLiteStore):
            await store.close()
            store = await SQLiteStore.open(path)

        model_owner = AgentSDK.for_test(
            store=store,
            acompletion=blocking_model,
            permission_default="allow",
        )
        _register(model_owner, tool_handler=_forbidden_tool)
        first_request = await _admit_workflow_reconciliation(
            model_owner,
            workflow_run_id,
            run_id,
        )
        first_actor = {"type": "operator", "id": "first-decision"}
        first_result = ToolResult.succeeded(
            "call_confirmed_workflow",
            TOOL.name,
            {"confirmed": True},
        ).model_dump(mode="json")
        first_evidence = {"tool_result": first_result}
        first_resolution = await model_owner.recovery.resolve(
            first_request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor=first_actor,
            evidence=first_evidence,
        )

        model_workflow = await model_owner.recovery.recover_workflow(workflow_run_id)
        second_workflow_run_id, second_run_id = await _interrupt_active_workflow(
            model_owner,
            store,
            model_workflow,
            blocking_model.started,
        )
        assert second_workflow_run_id == workflow_run_id
        assert second_run_id == run_id
        model_owner = None
        blocking_model.release.set()
        if isinstance(store, SQLiteStore):
            await store.close()
            store = await SQLiteStore.open(path)

        final_provider = _FinalCompletion("after-second-decision")
        final_sdk = AgentSDK.for_test(
            store=store,
            acompletion=final_provider,
            permission_default="allow",
        )
        _register(final_sdk, tool_handler=_forbidden_tool)
        second_request = await _admit_workflow_reconciliation(
            final_sdk,
            workflow_run_id,
            run_id,
        )
        assert second_request.request_id != first_request.request_id
        second_resolution = await final_sdk.recovery.resolve(
            second_request.request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator", "id": "second-decision"},
            evidence={"disposition": "not_executed"},
        )
        assert second_resolution.resolution is not None

        result = await (
            await final_sdk.recovery.recover_workflow(workflow_run_id)
        ).result()
        assert result.status is WorkflowRunStatus.COMPLETED
        assert result.output_text == "after-second-decision"
        assert final_provider.calls == 1
        assert blocking_tool.calls == 1
        assert final_provider.messages[-1][-1] == {
            "role": "tool",
            "tool_call_id": "call_confirmed_workflow",
            "name": TOOL.name,
            "content": first_result["content"],
        }

        cursor = await store.latest_cursor()
        assert (
            await final_sdk.recovery.resolve(
                first_request.request_id,
                ReconciliationAction.CONFIRM_COMPLETED,
                actor=first_actor,
                evidence=first_evidence,
            )
            == first_resolution
        )
        assert await store.latest_cursor() == cursor
        assert final_provider.calls == 1
        assert blocking_tool.calls == 1
    finally:
        blocking_tool.release.set()
        blocking_model.release.set()
        if model_owner is not None:
            await model_owner.close()
        if final_sdk is not None:
            await final_sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_two_sdks_project_confirmed_terminal_child_once(
    backend: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "confirmed-terminal-two-sdks.db"
    primary = await _open_store(backend, path)
    blocking = _BlockingCompletion()
    owner = AgentSDK.for_test(store=primary, acompletion=blocking)
    _register(owner)
    session = await owner.sessions.create(workspaces=[])
    original = await owner.workflows.start(session.session_id, _workflow_yaml())
    resolver: AgentSDK | None = None
    first: AgentSDK | None = None
    second: AgentSDK | None = None
    stores: tuple[SQLiteStore, ...] = ()
    barrier = _ProjectionBarrier()
    provider_calls = 0

    async def forbidden_provider(**_: Any) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("terminal child projection must not call Provider")

    try:
        workflow_run_id, run_id = await _interrupt_active_workflow(
            owner,
            primary,
            original,
            blocking.started,
        )
        blocking.release.set()
        if isinstance(primary, SQLiteStore):
            await primary.close()
            primary = await SQLiteStore.open(path)
        resolver = AgentSDK.for_test(store=primary, acompletion=forbidden_provider)
        _register(resolver)
        request = await _admit_workflow_reconciliation(
            resolver,
            workflow_run_id,
            run_id,
        )
        await resolver.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator", "id": "race"},
            evidence={"provider_result": _provider_result("text")},
        )
        await resolver.close()
        resolver = None

        if isinstance(primary, SQLiteStore):
            await primary.close()
            first_delegate = await SQLiteStore.open(path)
            second_delegate = await SQLiteStore.open(path)
            stores = (first_delegate, second_delegate)
            primary = first_delegate
        else:
            first_delegate = second_delegate = primary
        first_store = _ProjectionBarrierStore(
            first_delegate,
            barrier,
            winner=True,
        )
        second_store = _ProjectionBarrierStore(
            second_delegate,
            barrier,
            winner=False,
        )
        first = AgentSDK.for_test(store=first_store, acompletion=forbidden_provider)
        second = AgentSDK.for_test(store=second_store, acompletion=forbidden_provider)
        _register(first)
        _register(second)

        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow_run_id),
            second.recovery.recover_workflow(workflow_run_id),
        )
        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )

        assert first_result == second_result
        assert first_result.status is WorkflowRunStatus.COMPLETED
        assert barrier.arrivals == 2
        events = await primary.read_events(after_cursor=0, session_id=session.session_id)
        assert sum(item.event.type == "workflow.node.completed" for item in events) == 1
        assert sum(item.event.type == "workflow.completed" for item in events) == 1
        assert sum(item.event.type == "session.workflow.detached" for item in events) == 1
        assert provider_calls == 0
        assert blocking.calls == 1
    finally:
        blocking.release.set()
        barrier.ready.set()
        barrier.committed.set()
        if resolver is not None:
            await resolver.close()
        await asyncio.gather(
            *(sdk.close() for sdk in (first, second) if sdk is not None)
        )
        await asyncio.gather(*(store.close() for store in stores))
        if isinstance(primary, SQLiteStore) and primary not in stores:
            await primary.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_confirmed_terminal_projection_rejects_capability_drift(
    backend: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "confirmed-terminal-capability-drift.db"
    store = await _open_store(backend, path)
    blocking = _BlockingCompletion()
    owner = AgentSDK.for_test(store=store, acompletion=blocking)
    _register(owner)
    session = await owner.sessions.create(workspaces=[])
    original = await owner.workflows.start(session.session_id, _workflow_yaml())
    resolver: AgentSDK | None = None
    drifted: AgentSDK | None = None
    provider_calls = 0

    async def forbidden_provider(**_: Any) -> Any:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("capability drift must fail before Provider")

    try:
        workflow_run_id, run_id = await _interrupt_active_workflow(
            owner,
            store,
            original,
            blocking.started,
        )
        blocking.release.set()
        if isinstance(store, SQLiteStore):
            await store.close()
            store = await SQLiteStore.open(path)
        resolver = AgentSDK.for_test(store=store, acompletion=forbidden_provider)
        _register(resolver)
        request = await _admit_workflow_reconciliation(
            resolver,
            workflow_run_id,
            run_id,
        )
        await resolver.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator", "id": "drift"},
            evidence={"provider_result": _provider_result("text")},
        )
        await resolver.close()
        resolver = None

        drifted = AgentSDK.for_test(store=store, acompletion=forbidden_provider)
        drifted.agents.define(
            AgentSpec(name="planner", revision="1", model="fake/drifted")
        )
        workflow_before = await drifted.workflows.get(workflow_run_id)
        node_before = await store.get_snapshot(
            "workflow_node", workflow_before.nodes[0].entity_id
        )
        cursor_before = await store.latest_cursor()

        with pytest.raises(AgentSDKError) as mismatch:
            await drifted.recovery.recover_workflow(workflow_run_id)

        assert mismatch.value.code is ErrorCode.INVALID_STATE
        assert mismatch.value.message == "recovery capabilities unavailable"
        assert await drifted.workflows.get(workflow_run_id) == workflow_before
        assert (
            await store.get_snapshot("workflow_node", workflow_before.nodes[0].entity_id)
            == node_before
        )
        assert await store.latest_cursor() == cursor_before
        assert (await drifted.runs.get(run_id)).status is RunStatus.COMPLETED
        assert provider_calls == 0
        assert blocking.calls == 1
    finally:
        blocking.release.set()
        if resolver is not None:
            await resolver.close()
        if drifted is not None:
            await drifted.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_closing_session_waits_for_confirmed_workflow_projection_then_deletes(
    backend: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "confirmed-closing-session.db"
    store = await _open_store(backend, path)
    blocking = _BlockingCompletion()
    owner = AgentSDK.for_test(store=store, acompletion=blocking)
    _register(owner)
    session = await owner.sessions.create(workspaces=[])
    original = await owner.workflows.start(session.session_id, _workflow_yaml())
    reopened: AgentSDK | None = None
    try:
        workflow_run_id, run_id = await _interrupt_active_workflow(
            owner,
            store,
            original,
            blocking.started,
        )
        blocking.release.set()
        if isinstance(store, SQLiteStore):
            await store.close()
            store = await SQLiteStore.open(path)
        reopened = AgentSDK.for_test(store=store, acompletion=blocking)
        _register(reopened)
        request = await _admit_workflow_reconciliation(
            reopened,
            workflow_run_id,
            run_id,
        )
        closing = await reopened.sessions.close(session.session_id)
        assert closing.status is SessionStatus.CLOSING
        with pytest.raises(SessionBusyError):
            await reopened.sessions.delete(session.session_id)

        await reopened.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator", "id": "closing"},
            evidence={"provider_result": _provider_result("text")},
        )
        after_resolution = await reopened.sessions.get(session.session_id)
        assert after_resolution.status is SessionStatus.CLOSING
        assert after_resolution.active_run_ids == ()
        assert after_resolution.active_workflow_run_ids == (workflow_run_id,)
        with pytest.raises(SessionBusyError):
            await reopened.sessions.delete(session.session_id)

        result = await (
            await reopened.recovery.recover_workflow(workflow_run_id)
        ).result()
        assert result.status is WorkflowRunStatus.COMPLETED
        closed = await reopened.sessions.get(session.session_id)
        assert closed.status is SessionStatus.CLOSED
        assert closed.active_run_ids == ()
        assert closed.active_workflow_run_ids == ()
        node_id = (await reopened.workflows.get(workflow_run_id)).nodes[0].entity_id

        await reopened.sessions.delete(session.session_id)

        assert await store.get_snapshot("session", session.session_id) is None
        assert await store.get_snapshot("run", run_id) is None
        assert await store.get_snapshot("workflow", workflow_run_id) is None
        assert await store.get_snapshot("workflow_node", node_id) is None
        assert await store.get_run_checkpoint(run_id) is None
        if isinstance(store, InMemoryStore):
            assert store._external_operations == {}
            assert store._reconciliation_requests == {}
            assert store._idempotency == {}
        else:
            for table in ("external_operations", "reconciliation_requests"):
                async with store._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE run_id = ?",
                    (run_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                assert row is not None and row[0] == 0
            async with store._connection.execute(
                "SELECT COUNT(*) FROM idempotency_records WHERE session_id = ?",
                (session.session_id,),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None and row[0] == 0
        assert not await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        with pytest.raises(AgentSDKError) as missing:
            await reopened.recovery.recover_workflow(workflow_run_id)
        assert missing.value.code is ErrorCode.NOT_FOUND
    finally:
        blocking.release.set()
        if reopened is not None:
            await reopened.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    "event_type",
    ("workflow.node.completed", "workflow.completed"),
)
async def test_confirmed_terminal_projection_recovers_post_commit_ambiguity(
    backend: str,
    event_type: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"confirmed-ambiguity-{event_type}.db"
    store = await _open_store(backend, path)
    blocking = _BlockingCompletion()
    owner = AgentSDK.for_test(store=store, acompletion=blocking)
    _register(owner)
    session = await owner.sessions.create(workspaces=[])
    original = await owner.workflows.start(session.session_id, _workflow_yaml())
    resolver: AgentSDK | None = None
    crashing_sdk: AgentSDK | None = None
    recovered_sdk: AgentSDK | None = None
    try:
        workflow_run_id, run_id = await _interrupt_active_workflow(
            owner,
            store,
            original,
            blocking.started,
        )
        blocking.release.set()
        if isinstance(store, SQLiteStore):
            await store.close()
            store = await SQLiteStore.open(path)
        resolver = AgentSDK.for_test(store=store, acompletion=blocking)
        _register(resolver)
        request = await _admit_workflow_reconciliation(
            resolver,
            workflow_run_id,
            run_id,
        )
        await resolver.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator", "id": "ambiguity"},
            evidence={"provider_result": _provider_result("text")},
        )
        await resolver.close()
        resolver = None

        crashing_store = _CancelAfterWorkflowCommitStore(store, event_type)
        crashing_sdk = AgentSDK.for_test(store=crashing_store, acompletion=blocking)
        _register(crashing_sdk)
        crashing_handle = await crashing_sdk.recovery.recover_workflow(workflow_run_id)
        with pytest.raises(asyncio.CancelledError):
            await crashing_handle.result()
        assert crashing_store.fired is True
        await crashing_sdk.close()
        crashing_sdk = None

        if isinstance(store, SQLiteStore):
            await store.close()
            store = await SQLiteStore.open(path)
        recovered_sdk = AgentSDK.for_test(store=store, acompletion=blocking)
        _register(recovered_sdk)
        result = await (
            await recovered_sdk.recovery.recover_workflow(workflow_run_id)
        ).result()
        assert result.status is WorkflowRunStatus.COMPLETED
        assert result.output_text == "operator-confirmed"
        events = await store.read_events(after_cursor=0, session_id=session.session_id)
        assert sum(item.event.type == "workflow.node.completed" for item in events) == 1
        assert sum(item.event.type == "workflow.completed" for item in events) == 1
        assert sum(item.event.type == "session.workflow.detached" for item in events) == 1
        assert blocking.calls == 1
    finally:
        blocking.release.set()
        if resolver is not None:
            await resolver.close()
        if crashing_sdk is not None:
            await crashing_sdk.close()
        if recovered_sdk is not None:
            await recovered_sdk.close()
        if isinstance(store, SQLiteStore):
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("memory", "sqlite"))
async def test_two_sdks_resume_confirmed_tool_child_once(
    backend: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "confirmed-tool-two-sdks.db"
    primary = await _open_store(backend, path)
    first_provider = _ToolCallCompletion()
    blocking_tool = _BlockingTool()
    owner = AgentSDK.for_test(
        store=primary,
        acompletion=first_provider,
        permission_default="allow",
    )
    _register(owner, tool_handler=blocking_tool)
    session = await owner.sessions.create(workspaces=[])
    original = await owner.workflows.start(session.session_id, _workflow_yaml())
    resolver: AgentSDK | None = None
    first: AgentSDK | None = None
    second: AgentSDK | None = None
    stores: tuple[SQLiteStore, ...] = ()
    barrier = _LeaseBarrier()
    final_provider = _FinalCompletion("raced-finish")
    try:
        workflow_run_id, run_id = await _interrupt_active_workflow(
            owner,
            primary,
            original,
            blocking_tool.started,
        )
        blocking_tool.release.set()
        if isinstance(primary, SQLiteStore):
            await primary.close()
            primary = await SQLiteStore.open(path)
        resolver = AgentSDK.for_test(
            store=primary,
            acompletion=final_provider,
            permission_default="allow",
        )
        _register(resolver, tool_handler=_forbidden_tool)
        request = await _admit_workflow_reconciliation(
            resolver,
            workflow_run_id,
            run_id,
        )
        await resolver.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator", "id": "race"},
            evidence={
                "tool_result": ToolResult.succeeded(
                    "call_confirmed_workflow",
                    TOOL.name,
                    {"confirmed": True},
                ).model_dump(mode="json")
            },
        )
        await resolver.close()
        resolver = None

        if isinstance(primary, SQLiteStore):
            await primary.close()
            first_delegate = await SQLiteStore.open(path)
            second_delegate = await SQLiteStore.open(path)
            stores = (first_delegate, second_delegate)
            primary = first_delegate
        else:
            first_delegate = second_delegate = primary
        first_store = _LeaseBarrierStore(first_delegate, barrier)
        second_store = _LeaseBarrierStore(second_delegate, barrier)
        first = AgentSDK.for_test(
            store=first_store,
            acompletion=final_provider,
            permission_default="allow",
        )
        second = AgentSDK.for_test(
            store=second_store,
            acompletion=final_provider,
            permission_default="allow",
        )
        _register(first, tool_handler=_forbidden_tool)
        _register(second, tool_handler=_forbidden_tool)

        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow_run_id),
            second.recovery.recover_workflow(workflow_run_id),
        )
        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )

        assert first_result == second_result
        assert first_result.output_text == "raced-finish"
        assert barrier.arrivals == 2
        assert final_provider.calls == 1
        assert blocking_tool.calls == 1
        events = await primary.read_events(after_cursor=0, session_id=session.session_id)
        assert sum(item.event.type == "workflow.node.completed" for item in events) == 1
        assert sum(item.event.type == "workflow.completed" for item in events) == 1
    finally:
        blocking_tool.release.set()
        barrier.ready.set()
        if resolver is not None:
            await resolver.close()
        await asyncio.gather(
            *(sdk.close() for sdk in (first, second) if sdk is not None)
        )
        await asyncio.gather(*(store.close() for store in stores))
        if isinstance(primary, SQLiteStore) and primary not in stores:
            await primary.close()
