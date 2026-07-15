from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_sdk import AgentSDK, AgentSDKError, AgentSpec, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.models import RunSnapshot, RunStatus
from agent_sdk.runtime.recovery import RecoveryPlan
from agent_sdk.runtime.session_lifecycle import exact_session_precondition, session_write
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.subagents.models import TaskEnvelope
from agent_sdk.subagents.service import render_task_envelope
from agent_sdk.tools.models import ToolRetryPolicy, ToolSpec
from agent_sdk.workflow import (
    WorkflowCompiler,
    WorkflowNodeStatus,
    WorkflowRunStatus,
)
from agent_sdk.workflow.state import WorkflowState


DEFINITION = {
    "api_version": "agent-sdk/v1",
    "kind": "Workflow",
    "name": "recoverable",
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
AGENT = AgentSpec(name="planner", revision="1", model="fake/planner")
WORKER = AgentSpec(name="worker", revision="1", model="fake/worker")
CHILD_DEFINITION = {
    "api_version": "agent-sdk/v1",
    "kind": "Workflow",
    "name": "recoverable-child",
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
TOOL = ToolSpec(
    name="inspect",
    description="Inspect state",
    input_schema={"type": "object", "properties": {}},
    version="1",
    source="application",
    effects=("read",),
    timeout_seconds=2.0,
    retry_policy=ToolRetryPolicy.IDEMPOTENT,
)


class _CountingCompletion:
    def __init__(self, *, block: bool = False, fail: bool = False) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = block
        self.fail = fail

    async def __call__(self, **params: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("RAW_PROVIDER_SECRET")
        text = "verified" if params.get("model") == "fake/worker" else "planned"

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [{"delta": {"content": text}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

        return chunks()


async def _tool_handler(**_: Any) -> dict[str, bool]:
    return {"ok": True}


async def _seed_current_workflow(
    store: InMemoryStore,
    completion: Any,
    *,
    definition: dict[str, Any] = DEFINITION,
    agents: tuple[AgentSpec, ...] = (AGENT,),
    tool: ToolSpec | None = None,
    permission_default: str = "ask",
) -> tuple[AgentSDK, Any]:
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default=permission_default,  # type: ignore[arg-type]
    )
    for agent in agents:
        sdk.agents.define(agent)
    if tool is not None:
        sdk.tools.register(tool, _tool_handler)
    session = await sdk.sessions.create(workspaces=[])
    workflow = WorkflowCompiler().compile_yaml(
        yaml.safe_dump(definition, sort_keys=False)
    )
    executor = sdk.workflows._executor  # type: ignore[attr-defined]
    descriptor = executor._workflow_execution_descriptor(workflow)
    created = await WorkflowState(store).create(
        session.session_id,
        workflow,
        execution_descriptor=descriptor,
    )
    return sdk, created.value


async def _select_root(
    store: InMemoryStore,
    workflow: Any,
    run_id: str = "run_selected",
) -> Any:
    return await WorkflowState(store).start_node(workflow, 0, run_id)


async def _create_selected_run(sdk: AgentSDK, workflow: Any) -> RunSnapshot:
    node = workflow.workflow.nodes[0]
    executor = sdk.workflows._executor  # type: ignore[attr-defined]
    descriptor = executor._node_execution_descriptor(node)
    outcome = await sdk.runs._commands.start_run(  # type: ignore[attr-defined]
        workflow.session_id,
        run_id=workflow.nodes[0].run_id,
        agent_revision=node.agent_revision,
        user_input=node.input,
        workflow_run_id=workflow.workflow_run_id,
        workflow_node_id=node.id,
        execution_descriptor=descriptor,
        idempotency_key=f"workflow-node:{workflow.workflow_run_id}:{node.id}",
    )
    return outcome.value


async def _completion(**_: Any) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [{"delta": {"content": "planned"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    return chunks()


def _workflow_yaml() -> str:
    return yaml.safe_dump(DEFINITION, sort_keys=False)


@pytest.mark.asyncio
async def test_recover_terminal_workflow_is_detached_without_live_capabilities() -> None:
    store = InMemoryStore()
    first = AgentSDK.for_test(store=store, acompletion=_completion)
    first.agents.define(AGENT)
    session = await first.sessions.create(workspaces=[])
    original = await first.workflows.start(session.session_id, _workflow_yaml())
    original_result = await original.result()
    assert original_result.status is WorkflowRunStatus.COMPLETED
    await first.close()

    reopened = AgentSDK.for_test(store=store, acompletion=_completion)
    try:
        recovered = await reopened.recovery.recover_workflow(original.workflow_run_id)

        assert recovered.attached is False
        assert await recovered.result() == original_result
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_recover_legacy_workflow_requires_resolution_without_mutation() -> None:
    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=_completion)
    session = await sdk.sessions.create(workspaces=[])
    workflow = WorkflowCompiler().compile_yaml(_workflow_yaml())
    created = await WorkflowState(store).create(session.session_id, workflow)
    cursor_before = await store.latest_cursor()

    try:
        with pytest.raises(AgentSDKError) as recovery:
            await sdk.recovery.recover_workflow(created.workflow_run_id)

        assert recovery.value.code is ErrorCode.CONFLICT
        assert recovery.value.message == "recovery required"
        assert recovery.value.retryable is True
        assert await store.latest_cursor() == cursor_before
        assert await WorkflowState(store).load(created.workflow_run_id) == created.value
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recover_pending_root_selects_creates_executes_and_completes() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        result = await handle.result()

        assert result.status is WorkflowRunStatus.COMPLETED
        assert result.nodes[0].status is WorkflowNodeStatus.COMPLETED
        assert result.nodes[0].run_id is not None
        assert completion.calls == 1
        run = await sdk.runs.get(result.nodes[0].run_id)
        assert run.execution_compatibility == "current"
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recover_selected_missing_run_repairs_exact_id() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    selected = await _select_root(store, workflow, "run_exact_missing")
    try:
        result = await (
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        ).result()

        assert result.nodes[0].run_id == "run_exact_missing"
        assert (await sdk.runs.get("run_exact_missing")).status is RunStatus.COMPLETED
        assert completion.calls == 1
        assert selected.nodes[0].status is WorkflowNodeStatus.RUNNING
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recover_selected_created_run_uses_run_recovery() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    selected = await _select_root(store, workflow)
    created = await _create_selected_run(sdk, selected)
    assert created.status is RunStatus.CREATED
    try:
        result = await (
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        ).result()

        assert result.status is WorkflowRunStatus.COMPLETED
        assert result.nodes[0].run_id == created.run_id
        assert completion.calls == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recover_selected_completed_run_projects_without_reexecution() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    selected = await _select_root(store, workflow)
    created = await _create_selected_run(sdk, selected)
    run_handle = await sdk.recovery.recover_run(created.run_id)
    await run_handle.result()
    assert completion.calls == 1
    try:
        result = await (
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        ).result()

        assert result.status is WorkflowRunStatus.COMPLETED
        assert completion.calls == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recover_selected_failed_run_projects_sanitized_failure() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion(fail=True)
    sdk, workflow = await _seed_current_workflow(store, completion)
    selected = await _select_root(store, workflow)
    created = await _create_selected_run(sdk, selected)
    with pytest.raises(AgentSDKError):
        await (await sdk.recovery.recover_run(created.run_id)).result()
    assert (await sdk.runs.get(created.run_id)).status is RunStatus.FAILED
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        with pytest.raises(AgentSDKError) as failed:
            await handle.result()

        assert failed.value.code is ErrorCode.INTERNAL
        assert "RAW_PROVIDER_SECRET" not in failed.value.message
        durable = await sdk.workflows.get(workflow.workflow_run_id)
        assert durable.status is WorkflowRunStatus.FAILED
        assert durable.nodes[0].status is WorkflowNodeStatus.FAILED
        assert completion.calls == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recover_selected_waiting_reconciliation_leaves_workflow_active() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    selected = await _select_root(store, workflow)
    created = await _create_selected_run(sdk, selected)
    interrupted = created.model_copy(
        update={"status": RunStatus.INTERRUPTED, "version": 3}
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    interrupted.run_id,
                    interrupted.session_id,
                    interrupted.version,
                    interrupted.model_dump(mode="json"),
                ),
            ),
        )
    )
    with pytest.raises(AgentSDKError) as run_recovery:
        await (await sdk.recovery.recover_run(created.run_id)).result()
    assert run_recovery.value.message == "recovery required"
    assert (await sdk.runs.get(created.run_id)).status is RunStatus.WAITING_RECONCILIATION
    cursor_before = await store.latest_cursor()
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        with pytest.raises(AgentSDKError) as recovery:
            await handle.result()

        assert recovery.value.code is ErrorCode.CONFLICT
        assert recovery.value.message == "recovery required"
        durable = await sdk.workflows.get(workflow.workflow_run_id)
        assert durable.status is WorkflowRunStatus.RUNNING
        assert durable.nodes[0].status is WorkflowNodeStatus.RUNNING
        assert await store.latest_cursor() == cursor_before
        assert completion.calls == 0
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recover_child_creates_exact_parent_envelope_and_descriptor() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(
        store,
        completion,
        definition=CHILD_DEFINITION,
        agents=(AGENT, WORKER),
        tool=TOOL,
    )
    try:
        result = await (
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        ).result()

        assert result.status is WorkflowRunStatus.COMPLETED
        assert completion.calls == 2
        parent_id = result.nodes[0].run_id
        child_id = result.nodes[1].run_id
        assert parent_id is not None
        assert child_id is not None
        child = await sdk.runs.get(child_id)
        node = workflow.workflow.nodes[1]
        envelope = sdk.workflows._executor._node_execution_descriptor(  # type: ignore[attr-defined]
            node
        ).messages[0]["content"]
        assert child.parent_run_id == parent_id
        assert child.task_envelope is not None
        assert child.user_input == render_task_envelope(child.task_envelope)
        assert child.user_input == envelope
        assert child.execution_descriptor is not None
        assert child.execution_descriptor.messages[0]["content"] == envelope
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recovery_revalidates_capabilities_before_next_node_create() -> None:
    store = InMemoryStore()
    sdk: AgentSDK | None = None
    calls = 0

    async def mutate_capability(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        assert sdk is not None
        if calls == 1:
            assert sdk.tools.unregister("inspect") is True

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

        del params
        return chunks()

    sdk, workflow = await _seed_current_workflow(
        store,
        mutate_capability,
        definition=CHILD_DEFINITION,
        agents=(AGENT, WORKER),
        tool=TOOL,
    )
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        with pytest.raises(AgentSDKError) as capability:
            await handle.result()

        assert capability.value.code is ErrorCode.INVALID_STATE
        assert capability.value.message == "recovery capabilities unavailable"
        durable = await sdk.workflows.get(workflow.workflow_run_id)
        assert durable.nodes[0].status is WorkflowNodeStatus.COMPLETED
        assert durable.nodes[1].status is WorkflowNodeStatus.PENDING
        assert durable.nodes[1].run_id is None
        assert calls == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recovery_rejects_workflow_missing_from_session_ownership() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    session = await sdk.sessions.get(workflow.session_id)
    detached = session.model_copy(
        update={"active_workflow_run_ids": (), "version": session.version + 1}
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="test.session.workflow.detached",
                    session_id=session.session_id,
                    run_id=None,
                    sequence=detached.version,
                    payload={},
                ),
            ),
            snapshots=(session_write(detached),),
            preconditions=(exact_session_precondition(session),),
        )
    )
    cursor_before = await store.latest_cursor()
    try:
        with pytest.raises(AgentSDKError) as ownership:
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)

        assert ownership.value.code in {ErrorCode.CONFLICT, ErrorCode.INVALID_STATE}
        assert await store.latest_cursor() == cursor_before
        assert completion.calls == 0
        assert (await sdk.workflows.get(workflow.workflow_run_id)).nodes[0].run_id is None
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_recover_terminal_failed_workflow_is_detached_without_capabilities() -> None:
    store = InMemoryStore()
    failing = _CountingCompletion(fail=True)
    first = AgentSDK.for_test(store=store, acompletion=failing)
    first.agents.define(AGENT)
    session = await first.sessions.create(workspaces=[])
    original = await first.workflows.start(session.session_id, _workflow_yaml())
    with pytest.raises(AgentSDKError):
        await original.result()
    await first.close()

    reopened = AgentSDK.for_test(store=store, acompletion=_completion)
    try:
        recovered = await reopened.recovery.recover_workflow(original.workflow_run_id)

        assert recovered.attached is False
        with pytest.raises(AgentSDKError) as failed:
            await recovered.result()
        assert failed.value.code is ErrorCode.INTERNAL
        assert "RAW_PROVIDER_SECRET" not in failed.value.message
    finally:
        await reopened.close()


@pytest.mark.parametrize(
    "capability_case",
    [
        "missing_agent",
        "agent_model_params",
        "missing_tool",
        "tool_effects",
        "tool_timeout",
        "tool_source",
        "tool_version",
        "tool_retry",
        "policy",
    ],
)
@pytest.mark.asyncio
async def test_nonterminal_capability_mismatch_is_zero_mutation(
    capability_case: str,
) -> None:
    store = InMemoryStore()
    seed, workflow = await _seed_current_workflow(
        store,
        _completion,
        tool=TOOL,
    )
    await seed.close()

    completion = _CountingCompletion()
    permission_default = "deny" if capability_case == "policy" else "ask"
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default=permission_default,
    )
    if capability_case != "missing_agent":
        agent = AGENT
        if capability_case == "agent_model_params":
            agent = AGENT.model_copy(update={"model_params": {"temperature": 0.25}})
        sdk.agents.define(agent)
    if capability_case != "missing_tool":
        tool = TOOL
        updates: dict[str, Any] = {
            "tool_effects": {"effects": ("write",)},
            "tool_timeout": {"timeout_seconds": 9.0},
            "tool_source": {"source": "mcp:test"},
            "tool_version": {"version": "2"},
            "tool_retry": {"retry_policy": ToolRetryPolicy.SAFE_RETRY},
        }
        if capability_case in updates:
            tool = TOOL.model_copy(update=updates[capability_case])
        sdk.tools.register(tool, _tool_handler)
    cursor_before = await store.latest_cursor()
    before = await sdk.workflows.get(workflow.workflow_run_id)
    try:
        with pytest.raises(AgentSDKError) as mismatch:
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)

        assert mismatch.value.code is ErrorCode.INVALID_STATE
        assert mismatch.value.message == "recovery capabilities unavailable"
        assert mismatch.value.retryable is False
        assert await store.latest_cursor() == cursor_before
        assert await sdk.workflows.get(workflow.workflow_run_id) == before
        assert completion.calls == 0
    finally:
        await sdk.close()


@pytest.mark.parametrize(
    "relation",
    [
        "session",
        "workflow",
        "node",
        "agent",
        "input",
        "parent",
        "envelope",
        "descriptor",
    ],
)
@pytest.mark.asyncio
async def test_recovery_rejects_unrelated_selected_run_before_external_work(
    relation: str,
) -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    selected = await _select_root(store, workflow, f"run_foreign_{relation}")
    node = workflow.workflow.nodes[0]
    executor = sdk.workflows._executor  # type: ignore[attr-defined]
    descriptor = executor._node_execution_descriptor(node)
    session_id = workflow.session_id
    values: dict[str, Any] = {
        "agent_revision": node.agent_revision,
        "user_input": node.input,
        "parent_run_id": None,
        "workflow_run_id": workflow.workflow_run_id,
        "workflow_node_id": node.id,
        "task_envelope": None,
        "execution_descriptor": descriptor,
    }
    if relation == "session":
        session_id = (await sdk.sessions.create(workspaces=[])).session_id
    elif relation == "workflow":
        values["workflow_run_id"] = "wfr_foreign"
    elif relation == "node":
        values["workflow_node_id"] = "foreign-node"
    elif relation == "agent":
        values["agent_revision"] = "worker:1"
        values["execution_descriptor"] = executor._execution_descriptor(
            WORKER,
            node.input,
        )
    elif relation == "input":
        values["user_input"] = "foreign input"
        values["execution_descriptor"] = executor._execution_descriptor(
            AGENT,
            "foreign input",
        )
    elif relation == "parent":
        values["parent_run_id"] = "run_foreign_parent"
    elif relation == "envelope":
        values["task_envelope"] = TaskEnvelope(objective="foreign task")
    elif relation == "descriptor":
        changed = AGENT.model_copy(update={"model_params": {"temperature": 0.9}})
        values["execution_descriptor"] = executor._execution_descriptor(
            changed,
            node.input,
        )

    await sdk.runs._commands.start_run(  # type: ignore[attr-defined]
        session_id,
        run_id=selected.nodes[0].run_id,
        **values,
    )
    cursor_before = await store.latest_cursor()
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        with pytest.raises(AgentSDKError) as mismatch:
            await handle.result()

        assert mismatch.value.code is ErrorCode.INVALID_STATE
        assert mismatch.value.message == "related run does not match workflow node"
        assert await store.latest_cursor() == cursor_before
        assert completion.calls == 0
        assert (await sdk.workflows.get(workflow.workflow_run_id)).nodes[0].status is WorkflowNodeStatus.RUNNING
        await asyncio.sleep(0)
        assert sdk.workflows._executor._active == {}  # type: ignore[attr-defined]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_same_sdk_recovery_calls_attach_once_and_caller_cancel_is_shielded() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion(block=True)
    sdk, workflow = await _seed_current_workflow(store, completion)
    try:
        handles = await asyncio.gather(
            *(sdk.recovery.recover_workflow(workflow.workflow_run_id) for _ in range(20))
        )
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        tasks = {id(handle._task) for handle in handles}  # type: ignore[attr-defined]
        assert len(tasks) == 1

        cancelled_waiter = asyncio.create_task(handles[0].result())
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter

        completion.release.set()
        results = await asyncio.gather(*(handle.result() for handle in handles[1:]))
        assert {result.workflow_run_id for result in results} == {
            workflow.workflow_run_id
        }
        assert completion.calls == 1
        await asyncio.sleep(0)
        assert sdk.workflows._executor._active == {}  # type: ignore[attr-defined]
        assert sdk.recovery._tasks == {}  # type: ignore[attr-defined]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_cancelled_recovery_admission_still_registers_one_coordinator() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion(block=True)
    sdk, workflow = await _seed_current_workflow(store, completion)
    executor = sdk.workflows._executor  # type: ignore[attr-defined]
    original_load = executor._state.load
    blocked = asyncio.Event()
    release = asyncio.Event()
    block_once = True

    async def blocked_load(workflow_run_id: str) -> Any:
        nonlocal block_once
        if block_once:
            block_once = False
            blocked.set()
            await release.wait()
        return await original_load(workflow_run_id)

    executor._state.load = blocked_load
    caller = asyncio.create_task(
        sdk.recovery.recover_workflow(workflow.workflow_run_id)
    )
    try:
        await asyncio.wait_for(blocked.wait(), timeout=1)
        caller.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller

        executor._state.load = original_load
        attached = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        assert attached.attached is True
        assert len(executor._active) == 1
        completion.release.set()
        assert (await attached.result()).status is WorkflowRunStatus.COMPLETED
        await asyncio.sleep(0)
        assert executor._active == {}
    finally:
        executor._state.load = original_load
        release.set()
        completion.release.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_recovery_attaches_same_sdk_live_normal_workflow() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion(block=True)
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    sdk.agents.define(AGENT)
    session = await sdk.sessions.create(workspaces=[])
    normal = await sdk.workflows.start(session.session_id, _workflow_yaml())
    try:
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        recovered = await sdk.recovery.recover_workflow(normal.workflow_run_id)

        assert recovered._task is normal._task  # type: ignore[attr-defined]
        completion.release.set()
        assert (await recovered.result()).status is WorkflowRunStatus.COMPLETED
        assert completion.calls == 1
    finally:
        completion.release.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_normal_sdk_workflow_routes_selected_run_through_recovery_registry() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    sdk.agents.define(AGENT)
    planned: list[str] = []
    original_plan = sdk.recovery._service.plan  # type: ignore[attr-defined]

    async def recording_plan(run_id: str) -> Any:
        planned.append(run_id)
        return await original_plan(run_id)

    sdk.recovery._service.plan = recording_plan  # type: ignore[attr-defined,method-assign]
    session = await sdk.sessions.create(workspaces=[])
    try:
        result = await (
            await sdk.workflows.start(session.session_id, _workflow_yaml())
        ).result()

        assert result.status is WorkflowRunStatus.COMPLETED
        assert planned == [result.nodes[0].run_id]
        assert completion.calls == 1
        assert sdk.recovery._tasks == {}  # type: ignore[attr-defined]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_normal_workflow_recovery_required_diagnostic_stays_active() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    sdk.agents.define(AGENT)

    async def detached_plan(run_id: str) -> RecoveryPlan:
        return RecoveryPlan("detached", run_id)

    sdk.recovery._service.plan = detached_plan  # type: ignore[attr-defined,method-assign]
    session = await sdk.sessions.create(workspaces=[])
    try:
        handle = await sdk.workflows.start(session.session_id, _workflow_yaml())
        with pytest.raises(AgentSDKError) as diagnostic:
            await handle.result()

        assert diagnostic.value.code is ErrorCode.CONFLICT
        assert diagnostic.value.message == "recovery required"
        assert diagnostic.value.retryable is True
        durable = await sdk.workflows.get(handle.workflow_run_id)
        assert durable.status is WorkflowRunStatus.RUNNING
        assert durable.nodes[0].status is WorkflowNodeStatus.RUNNING
        assert durable.nodes[0].run_id is not None
        assert (await sdk.runs.get(durable.nodes[0].run_id)).status is RunStatus.CREATED
        assert completion.calls == 0
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_sqlite_selected_missing_run_recovery_uses_exact_id(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "workflow-recovery.db")
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)  # type: ignore[arg-type]
    await _select_root(store, workflow, "run_sqlite_exact")  # type: ignore[arg-type]
    try:
        result = await (
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        ).result()

        assert result.nodes[0].run_id == "run_sqlite_exact"
        assert completion.calls == 1
    finally:
        await sdk.close()
        await store.close()


@pytest.mark.asyncio
async def test_substituted_persisted_workflow_descriptor_is_zero_mutation() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    sdk.agents.define(AGENT)
    session = await sdk.sessions.create(workspaces=[])
    workflow_ir = WorkflowCompiler().compile_yaml(_workflow_yaml())

    alternate_store = InMemoryStore()
    alternate = AgentSDK.for_test(store=alternate_store, acompletion=_completion)
    alternate.agents.define(
        AGENT.model_copy(update={"model_params": {"temperature": 0.75}})
    )
    substituted_descriptor = (
        alternate.workflows._executor._workflow_execution_descriptor(  # type: ignore[attr-defined]
            workflow_ir
        )
    )
    await alternate.close()
    workflow = (
        await WorkflowState(store).create(
            session.session_id,
            workflow_ir,
            execution_descriptor=substituted_descriptor,
        )
    ).value
    cursor_before = await store.latest_cursor()
    try:
        with pytest.raises(AgentSDKError) as mismatch:
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)

        assert mismatch.value.code is ErrorCode.INVALID_STATE
        assert mismatch.value.message == "recovery capabilities unavailable"
        assert await store.latest_cursor() == cursor_before
        assert completion.calls == 0
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_malformed_persisted_workflow_descriptor_is_zero_mutation() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    malformed = workflow.model_dump(mode="json")
    malformed["version"] = workflow.version + 1
    malformed["execution_descriptor"]["descriptor_hash"] = "RAW_DESCRIPTOR_SECRET"
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "workflow",
                    workflow.workflow_run_id,
                    workflow.session_id,
                    workflow.version + 1,
                    malformed,
                ),
            ),
        )
    )
    cursor_before = await store.latest_cursor()
    try:
        with pytest.raises(AgentSDKError) as invalid:
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)

        assert invalid.value.code is ErrorCode.INTERNAL
        assert invalid.value.message == "failed to load workflow run"
        assert "RAW_DESCRIPTOR_SECRET" not in invalid.value.message
        assert await store.latest_cursor() == cursor_before
        assert completion.calls == 0
    finally:
        await sdk.close()
