from __future__ import annotations

import asyncio
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import types as mcp_types

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    PermissionDecision,
    ToolContext,
)
from agent_sdk.events.models import EventEnvelope
from agent_sdk.mcp import MCPManager, MCPServerConfig, StdioMCPTransport
from agent_sdk.permissions.broker import InProcessPermissionBridge
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionStatus
from agent_sdk.runtime.recovery import RecoveryPlan
from agent_sdk.runtime.session_lifecycle import exact_session_precondition, session_write
from agent_sdk.storage.base import CommitBatch, RunProgressBatch, SnapshotWrite
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
import agent_sdk.workflow.executor as workflow_executor_module


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


async def _seed_preconfigured_sdk(sdk: AgentSDK, store: Any) -> Any:
    session = await sdk.sessions.create(workspaces=[])
    workflow = WorkflowCompiler().compile_yaml(_workflow_yaml())
    descriptor = sdk.workflows._executor._workflow_execution_descriptor(  # type: ignore[attr-defined]
        workflow
    )
    return (
        await WorkflowState(store).create(
            session.session_id,
            workflow,
            execution_descriptor=descriptor,
        )
    ).value


async def _open_backend_pair(
    backend: str,
    database: Path,
) -> tuple[Any, Any, tuple[SQLiteStore, ...]]:
    if backend == "memory":
        store = InMemoryStore()
        return store, store, ()
    first = await SQLiteStore.open(database)
    second = await SQLiteStore.open(database)
    return first, second, (first, second)


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


class _HiddenRunStore:
    def __init__(self) -> None:
        self.delegate = InMemoryStore()
        self.hidden_run_id: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        if kind == "run" and entity_id == self.hidden_run_id:
            return None
        return await self.delegate.get_snapshot(kind, entity_id)


class _RunReadBarrierStore:
    def __init__(self, delegate: InMemoryStore, *, block_on_read: int) -> None:
        self.delegate = delegate
        self.block_on_read = block_on_read
        self.run_reads = 0
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        value = await self.delegate.get_snapshot(kind, entity_id)
        if kind == "run":
            self.run_reads += 1
            if self.run_reads == self.block_on_read:
                self.blocked.set()
                await self.release.wait()
        return value


class _ProjectionBarrier:
    def __init__(self) -> None:
        self.arrivals = 0
        self.both_arrived = asyncio.Event()
        self.recovery_committed = asyncio.Event()


class _ProjectionBarrierStore:
    def __init__(
        self,
        delegate: Any,
        barrier: _ProjectionBarrier,
        *,
        recovery_winner: bool,
        event_type: str = "workflow.node.completed",
    ) -> None:
        self.delegate = delegate
        self.barrier = barrier
        self.recovery_winner = recovery_winner
        self.event_type = event_type

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: CommitBatch) -> Any:
        if any(event.type == self.event_type for event in batch.events):
            self.barrier.arrivals += 1
            if self.barrier.arrivals == 2:
                self.barrier.both_arrived.set()
            await self.barrier.both_arrived.wait()
            if self.recovery_winner:
                result = await self.delegate.commit(batch)
                self.barrier.recovery_committed.set()
                return result
            await self.barrier.recovery_committed.wait()
        return await self.delegate.commit(batch)


class _TwoSDKWorkflowRaceStore:
    def __init__(
        self,
        *,
        block_node_selection: bool = False,
        block_run_lease: bool = False,
    ) -> None:
        self.delegate = InMemoryStore()
        self.block_node_selection = block_node_selection
        self.block_run_lease = block_run_lease
        self.node_selection_arrivals = 0
        self.run_lease_arrivals = 0
        self.node_selection_ready = asyncio.Event()
        self.run_lease_ready = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: CommitBatch) -> Any:
        if self.block_node_selection and any(
            event.type == "workflow.node.started" for event in batch.events
        ):
            self.node_selection_arrivals += 1
            if self.node_selection_arrivals == 2:
                self.node_selection_ready.set()
            await asyncio.wait_for(self.node_selection_ready.wait(), timeout=1)
        return await self.delegate.commit(batch)

    async def acquire_lease(self, **values: Any) -> Any:
        if self.block_run_lease:
            self.run_lease_arrivals += 1
            if self.run_lease_arrivals == 2:
                self.run_lease_ready.set()
            await asyncio.wait_for(self.run_lease_ready.wait(), timeout=1)
        return await self.delegate.acquire_lease(**values)


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
        await asyncio.wait_for(self.barrier.ready.wait(), timeout=1)
        return await self.delegate.acquire_lease(**values)


class _ToolCompletion:
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.calls = 0

    async def __call__(self, **_: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        call_number = self.calls

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if call_number == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_phase4b",
                                        "function": {
                                            "name": self.tool_name,
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
            else:
                yield {
                    "choices": [
                        {"delta": {"content": "done"}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }

        return chunks()


class _BlockingToolCompletion(_ToolCompletion):
    def __init__(self, tool_name: str) -> None:
        super().__init__(tool_name)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, **values: Any) -> AsyncIterator[dict[str, object]]:
        if self.calls == 0:
            self.started.set()
            await self.release.wait()
        return await super().__call__(**values)


class _CountingMCPSession:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    async def initialize(self) -> Any:
        return type("InitializeResult", (), {"protocolVersion": "2025-11-25"})()

    async def list_tools(self, cursor: str | None = None) -> Any:
        assert cursor is None
        return mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(
                    name="echo",
                    description="Echo one value",
                    inputSchema={
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                )
            ]
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        self.calls.append((name, dict(arguments or {})))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="remote-ok")]
        )

    def connector(self, _: MCPServerConfig) -> Any:
        session = self

        @asynccontextmanager
        async def connected() -> AsyncIterator[_CountingMCPSession]:
            yield session

        return connected()


class _SecretMCPSession(_CountingMCPSession):
    def __init__(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        secret: str,
    ) -> None:
        super().__init__(calls)
        self.secret = secret

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **values: Any,
    ) -> Any:
        del name, arguments, values
        raise RuntimeError(self.secret)


class _CancelAfterDurableCommitStore:
    def __init__(
        self,
        delegate: Any,
        event_type: str,
        *,
        run_progress: bool = False,
    ) -> None:
        self.delegate = delegate
        self.event_type = event_type
        self.run_progress = run_progress
        self.fired = False
        self.run_progress_event_types: list[tuple[str, ...]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: CommitBatch) -> Any:
        result = await self.delegate.commit(batch)
        if not self.run_progress:
            self._cancel_after_target(batch.events)
        return result

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        self.run_progress_event_types.append(tuple(event.type for event in batch.events))
        result = await self.delegate.commit_run_progress(batch)
        if self.run_progress:
            self._cancel_after_target(batch.events)
        return result

    def _cancel_after_target(self, events: tuple[EventEnvelope, ...]) -> None:
        if not self.fired and any(event.type == self.event_type for event in events):
            self.fired = True
            raise asyncio.CancelledError


class _SecretWorkflowReadStore:
    def __init__(self, delegate: InMemoryStore, secret: str) -> None:
        self.delegate = delegate
        self.secret = secret
        self.armed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        if self.armed and kind == "workflow":
            raise RuntimeError(self.secret)
        return await self.delegate.get_snapshot(kind, entity_id)


class _SecretPermissionBridge(InProcessPermissionBridge):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    async def wait(self, request: Any) -> Any:
        del request
        raise RuntimeError(self.secret)


def _assert_public_error_is_secret_free(error: BaseException, secret: str) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in str(error)
    assert secret not in "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    current = error.__traceback__
    sdk_frames = 0
    while current is not None:
        filename = current.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in filename:
            sdk_frames += 1
            assert secret not in repr(current.tb_frame.f_locals)
        current = current.tb_next
    assert sdk_frames > 0


@pytest.mark.asyncio
async def test_capability_mutation_during_session_admission_is_zero_mutation() -> None:
    store = InMemoryStore()
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(
        store,
        completion,
        tool=TOOL,
    )
    original_load_session = workflow_executor_module.load_session
    load_count = 0
    ownership_blocked = asyncio.Event()
    release_ownership = asyncio.Event()

    async def blocked_load_session(store_value: Any, session_id: str) -> Any:
        nonlocal load_count
        load_count += 1
        if load_count == 2:
            ownership_blocked.set()
            await release_ownership.wait()
        return await original_load_session(store_value, session_id)

    workflow_executor_module.load_session = blocked_load_session
    before_workflow = await sdk.workflows.get(workflow.workflow_run_id)
    before_session = await sdk.sessions.get(workflow.session_id)
    cursor_before = await store.latest_cursor()
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        await asyncio.wait_for(ownership_blocked.wait(), timeout=1)
        assert sdk.tools.unregister(TOOL.name) is True
        release_ownership.set()

        with pytest.raises(AgentSDKError) as capability:
            await handle.result()

        assert capability.value.code is ErrorCode.INVALID_STATE
        assert capability.value.message == "recovery capabilities unavailable"
        assert await sdk.workflows.get(workflow.workflow_run_id) == before_workflow
        assert await sdk.sessions.get(workflow.session_id) == before_session
        assert await store.latest_cursor() == cursor_before
        assert before_workflow.nodes[0].run_id is None
        assert completion.calls == 0
    finally:
        workflow_executor_module.load_session = original_load_session
        release_ownership.set()
        await sdk.close()


async def _seed_selected_child_boundary(
    store: Any,
    completion: _CountingCompletion,
) -> tuple[AgentSDK, Any, RunSnapshot]:
    sdk, workflow = await _seed_current_workflow(
        store,
        completion,
        definition=CHILD_DEFINITION,
        agents=(AGENT, WORKER),
        tool=TOOL,
    )
    root_selected = await WorkflowState(store).start_node(
        workflow,
        0,
        "run_parent_exact",
    )
    parent_created = await _create_selected_run(sdk, root_selected)
    parent_result = await (await sdk.recovery.recover_run(parent_created.run_id)).result()
    parent = await sdk.runs.get(parent_created.run_id)
    root_completed = await WorkflowState(store).complete_node(
        root_selected,
        0,
        parent_result,
    )
    child_selected = await WorkflowState(store).start_node(
        root_completed,
        1,
        "run_child_selected",
    )
    completion.calls = 0
    return sdk, child_selected, parent


@pytest.mark.parametrize(
    "parent_case",
    [
        "missing",
        "foreign_session",
        "wrong_workflow",
        "wrong_node",
        "wrong_agent",
        "wrong_root_relation",
        "legacy",
        "descriptor",
        "noncompleted",
        "output_projection",
        "usage_projection",
    ],
)
@pytest.mark.asyncio
async def test_child_recovery_rejects_unauthenticated_durable_parent(
    parent_case: str,
) -> None:
    store = _HiddenRunStore()
    completion = _CountingCompletion()
    sdk, workflow, parent = await _seed_selected_child_boundary(store, completion)
    if parent_case == "missing":
        store.hidden_run_id = parent.run_id
    else:
        updates: dict[str, Any] = {"version": parent.version + 1}
        executor = sdk.workflows._executor  # type: ignore[attr-defined]
        parent_node = workflow.workflow.nodes[0]
        if parent_case == "foreign_session":
            updates["session_id"] = "ses_foreign"
        elif parent_case == "wrong_workflow":
            updates["workflow_run_id"] = "wfr_foreign"
        elif parent_case == "wrong_node":
            updates["workflow_node_id"] = "foreign-node"
        elif parent_case == "wrong_agent":
            updates["agent_revision"] = "worker:1"
            updates["execution_descriptor"] = executor._execution_descriptor(
                WORKER,
                parent_node.input,
            )
        elif parent_case == "wrong_root_relation":
            updates["parent_run_id"] = "run_foreign_parent"
        elif parent_case == "legacy":
            updates["execution_compatibility"] = "legacy_unknown"
            updates["execution_descriptor"] = None
        elif parent_case == "descriptor":
            changed = AGENT.model_copy(
                update={"model_params": {"temperature": 0.5}}
            )
            updates["execution_descriptor"] = executor._execution_descriptor(
                changed,
                parent_node.input,
            )
        elif parent_case == "noncompleted":
            updates.update(
                {
                    "status": RunStatus.INTERRUPTED,
                    "output_text": None,
                    "usage": None,
                    "error": None,
                }
            )
        elif parent_case == "output_projection":
            updates["output_text"] = "substituted output"
        elif parent_case == "usage_projection":
            assert parent.usage is not None
            updates["usage"] = parent.usage.model_copy(
                update={"total_tokens": (parent.usage.total_tokens or 0) + 100}
            )
        substituted = parent.model_copy(update=updates)
        await store.delegate.commit(
            CommitBatch(
                events=(),
                snapshots=(
                    SnapshotWrite(
                        "run",
                        substituted.run_id,
                        substituted.session_id,
                        substituted.version,
                        substituted.model_dump(mode="json"),
                    ),
                ),
            )
        )

    cursor_before = await store.latest_cursor()
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        with pytest.raises(AgentSDKError) as invalid_parent:
            await handle.result()

        assert invalid_parent.value.code is ErrorCode.INVALID_STATE
        assert invalid_parent.value.message == "related parent run is invalid"
        assert await store.delegate.get_snapshot("run", "run_child_selected") is None
        assert await store.latest_cursor() == cursor_before
        assert completion.calls == 0
    finally:
        store.hidden_run_id = None
        await sdk.close()


@pytest.mark.asyncio
async def test_normal_and_recovery_converge_when_selected_run_is_missing() -> None:
    delegate = InMemoryStore()
    normal_store = _RunReadBarrierStore(delegate, block_on_read=2)
    recovery_store = _RunReadBarrierStore(delegate, block_on_read=1)
    completion = _CountingCompletion(block=True)
    normal = AgentSDK.for_test(store=normal_store, acompletion=completion)
    normal.agents.define(AGENT)
    session = await normal.sessions.create(workspaces=[])
    normal_handle = await normal.workflows.start(session.session_id, _workflow_yaml())
    recovery: AgentSDK | None = None
    try:
        await asyncio.wait_for(normal_store.blocked.wait(), timeout=1)
        selected = await normal.workflows.get(normal_handle.workflow_run_id)
        selected_run_id = selected.nodes[0].run_id
        assert selected_run_id is not None
        assert await delegate.get_snapshot("run", selected_run_id) is None

        recovery = AgentSDK.for_test(store=recovery_store, acompletion=completion)
        recovery.agents.define(AGENT)
        recovery_handle = await recovery.recovery.recover_workflow(
            normal_handle.workflow_run_id
        )
        await asyncio.wait_for(recovery_store.blocked.wait(), timeout=1)
        normal_store.release.set()
        recovery_store.release.set()
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        completion.release.set()

        normal_result, recovery_result = await asyncio.wait_for(
            asyncio.gather(normal_handle.result(), recovery_handle.result()),
            timeout=2,
        )
        assert normal_result == recovery_result
        assert normal_result.status is WorkflowRunStatus.COMPLETED
        assert normal_result.nodes[0].run_id == selected_run_id
        assert completion.calls == 1
        run_created = [
            stored
            for stored in await delegate.read_events(after_cursor=0)
            if stored.event.type == "run.created"
            and stored.event.run_id == selected_run_id
        ]
        assert len(run_created) == 1
    finally:
        normal_store.release.set()
        recovery_store.release.set()
        completion.release.set()
        if recovery is not None:
            await recovery.close()
        await normal.close()


@pytest.mark.asyncio
async def test_normal_and_recovery_converge_from_precreated_selected_run() -> None:
    delegate = InMemoryStore()
    projection = _ProjectionBarrier()
    normal_store = _ProjectionBarrierStore(
        delegate,
        projection,
        recovery_winner=False,
    )
    recovery_store = _ProjectionBarrierStore(
        delegate,
        projection,
        recovery_winner=True,
    )
    completion = _CountingCompletion(block=True)
    normal = AgentSDK.for_test(store=normal_store, acompletion=completion)
    normal.agents.define(AGENT)
    original_plan = normal.recovery._service.plan  # type: ignore[attr-defined]
    plan_blocked = asyncio.Event()
    release_plan = asyncio.Event()
    block_once = True

    async def blocked_plan(run_id: str) -> Any:
        nonlocal block_once
        if block_once:
            block_once = False
            plan_blocked.set()
            await release_plan.wait()
        return await original_plan(run_id)

    normal.recovery._service.plan = blocked_plan  # type: ignore[attr-defined,method-assign]
    session = await normal.sessions.create(workspaces=[])
    normal_handle = await normal.workflows.start(session.session_id, _workflow_yaml())
    recovery: AgentSDK | None = None
    try:
        await asyncio.wait_for(plan_blocked.wait(), timeout=1)
        selected = await normal.workflows.get(normal_handle.workflow_run_id)
        selected_run_id = selected.nodes[0].run_id
        assert selected_run_id is not None
        assert (await normal.runs.get(selected_run_id)).status is RunStatus.CREATED

        recovery = AgentSDK.for_test(store=recovery_store, acompletion=completion)
        recovery.agents.define(AGENT)
        recovery_handle = await recovery.recovery.recover_workflow(
            normal_handle.workflow_run_id
        )
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        release_plan.set()
        completion.release.set()
        await asyncio.wait_for(projection.both_arrived.wait(), timeout=1)

        normal_result, recovery_result = await asyncio.wait_for(
            asyncio.gather(normal_handle.result(), recovery_handle.result()),
            timeout=2,
        )
        assert normal_result == recovery_result
        assert normal_result.status is WorkflowRunStatus.COMPLETED
        assert normal_result.nodes[0].run_id == selected_run_id
        assert completion.calls == 1
        assert (await normal.runs.get(selected_run_id)).status is RunStatus.COMPLETED
    finally:
        release_plan.set()
        completion.release.set()
        projection.recovery_committed.set()
        if recovery is not None:
            await recovery.close()
        await normal.close()


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
async def test_two_sdks_pending_node_select_one_run_and_converge() -> None:
    store = _TwoSDKWorkflowRaceStore(block_node_selection=True)
    completion = _CountingCompletion()
    first, workflow = await _seed_current_workflow(store, completion)  # type: ignore[arg-type]
    second = AgentSDK.for_test(store=store, acompletion=completion)
    second.agents.define(AGENT)
    try:
        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow.workflow_run_id),
            second.recovery.recover_workflow(workflow.workflow_run_id),
        )
        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )

        selected_run_id = first_result.nodes[0].run_id
        assert selected_run_id is not None
        assert second_result.nodes[0].run_id == selected_run_id
        events = await store.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert sum(item.event.type == "workflow.node.started" for item in events) == 1
        assert sum(item.event.type == "run.created" for item in events) == 1
        assert completion.calls == 1
    finally:
        await asyncio.gather(first.close(), second.close())


@pytest.mark.asyncio
async def test_two_sdks_selected_created_run_have_one_lease_owner() -> None:
    store = _TwoSDKWorkflowRaceStore(block_run_lease=True)
    completion = _CountingCompletion()
    first, workflow = await _seed_current_workflow(store, completion)  # type: ignore[arg-type]
    selected = await _select_root(store, workflow)  # type: ignore[arg-type]
    created = await _create_selected_run(first, selected)
    second = AgentSDK.for_test(store=store, acompletion=completion)
    second.agents.define(AGENT)
    try:
        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow.workflow_run_id),
            second.recovery.recover_workflow(workflow.workflow_run_id),
        )
        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )

        assert first_result.nodes[0].run_id == created.run_id
        assert second_result.nodes[0].run_id == created.run_id
        assert store.run_lease_arrivals == 2
        events = await store.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert sum(item.event.type == "run.created" for item in events) == 1
        assert completion.calls == 1
    finally:
        await asyncio.gather(first.close(), second.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_two_sdks_terminal_run_project_node_once(
    backend: str,
    tmp_path: Path,
) -> None:
    sqlite_stores: tuple[SQLiteStore, SQLiteStore] | None = None
    if backend == "memory":
        primary: Any = InMemoryStore()
        first_delegate = second_delegate = primary
    else:
        database = tmp_path / "terminal-run-node-projection.db"
        sqlite_stores = (
            await SQLiteStore.open(database),
            await SQLiteStore.open(database),
        )
        first_delegate, second_delegate = sqlite_stores
        primary = first_delegate
    barrier = _ProjectionBarrier()
    first_store = _ProjectionBarrierStore(
        first_delegate,
        barrier,
        recovery_winner=True,
    )
    second_store = _ProjectionBarrierStore(
        second_delegate,
        barrier,
        recovery_winner=False,
    )
    completion = _CountingCompletion()
    first, workflow = await _seed_current_workflow(  # type: ignore[arg-type]
        first_store,
        completion,
    )
    selected = await _select_root(first_store, workflow)  # type: ignore[arg-type]
    created = await _create_selected_run(first, selected)
    run_result = await (await first.recovery.recover_run(created.run_id)).result()
    second = AgentSDK.for_test(store=second_store, acompletion=completion)
    second.agents.define(AGENT)
    try:
        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow.workflow_run_id),
            second.recovery.recover_workflow(workflow.workflow_run_id),
        )
        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )

        assert first_result == second_result
        assert first_result.nodes[0].run_id == run_result.run_id
        assert barrier.arrivals == 2
        events = await primary.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert sum(item.event.type == "workflow.node.completed" for item in events) == 1
        assert sum(item.event.type == "workflow.completed" for item in events) == 1
        assert completion.calls == 1
    finally:
        await asyncio.gather(first.close(), second.close())
        if sqlite_stores is not None:
            await asyncio.gather(*(store.close() for store in sqlite_stores))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_two_sdks_terminal_node_project_workflow_once(
    backend: str,
    tmp_path: Path,
) -> None:
    sqlite_stores: tuple[SQLiteStore, SQLiteStore] | None = None
    if backend == "memory":
        primary: Any = InMemoryStore()
        first_delegate = second_delegate = primary
    else:
        database = tmp_path / "terminal-node-workflow-projection.db"
        sqlite_stores = (
            await SQLiteStore.open(database),
            await SQLiteStore.open(database),
        )
        first_delegate, second_delegate = sqlite_stores
        primary = first_delegate
    barrier = _ProjectionBarrier()
    first_store = _ProjectionBarrierStore(
        first_delegate,
        barrier,
        recovery_winner=True,
        event_type="workflow.completed",
    )
    second_store = _ProjectionBarrierStore(
        second_delegate,
        barrier,
        recovery_winner=False,
        event_type="workflow.completed",
    )
    completion = _CountingCompletion()
    first, workflow = await _seed_current_workflow(  # type: ignore[arg-type]
        first_store,
        completion,
    )
    selected = await _select_root(first_store, workflow)  # type: ignore[arg-type]
    created = await _create_selected_run(first, selected)
    run_result = await (await first.recovery.recover_run(created.run_id)).result()
    projected = await WorkflowState(first_store).complete_node(selected, 0, run_result)
    assert projected.nodes[0].status is WorkflowNodeStatus.COMPLETED
    second = AgentSDK.for_test(store=second_store, acompletion=completion)
    second.agents.define(AGENT)
    try:
        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow.workflow_run_id),
            second.recovery.recover_workflow(workflow.workflow_run_id),
        )
        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )

        assert first_result == second_result
        assert first_result.status is WorkflowRunStatus.COMPLETED
        assert barrier.arrivals == 2
        events = await primary.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert sum(item.event.type == "workflow.completed" for item in events) == 1
        assert sum(
            item.event.type == "session.workflow.detached" for item in events
        ) == 1
        assert completion.calls == 1
    finally:
        await asyncio.gather(first.close(), second.close())
        if sqlite_stores is not None:
            await asyncio.gather(*(store.close() for store in sqlite_stores))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_two_sdks_provider_tool_provider_side_effects_execute_once(
    backend: str,
    tmp_path: Path,
) -> None:
    first_delegate, second_delegate, sqlite_stores = await _open_backend_pair(
        backend,
        tmp_path / "provider-tool-provider.db",
    )
    lease_barrier = _LeaseBarrier()
    first_store = _LeaseBarrierStore(first_delegate, lease_barrier)
    second_store = _LeaseBarrierStore(second_delegate, lease_barrier)
    completion = _ToolCompletion("calculate")
    tool = ToolSpec(
        name="calculate",
        description="Calculate a value",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        version="1",
        source="application",
        effects=("execute",),
        timeout_seconds=2.0,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
    )
    handler_calls: list[tuple[str, int]] = []

    def handler(owner: str) -> Any:
        async def execute(_: ToolContext, value: int) -> dict[str, int]:
            handler_calls.append((owner, value))
            return {"value": value}

        return execute

    first = AgentSDK.for_test(
        store=first_store,
        acompletion=completion,
        permission_default="allow",
    )
    first.agents.define(AGENT)
    first.tools.register(tool, handler("first"))
    workflow = await _seed_preconfigured_sdk(first, first_store)
    selected = await _select_root(first_store, workflow)  # type: ignore[arg-type]
    created = await _create_selected_run(first, selected)
    second = AgentSDK.for_test(
        store=second_store,
        acompletion=completion,
        permission_default="allow",
    )
    second.agents.define(AGENT)
    second.tools.register(tool, handler("second"))
    try:
        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow.workflow_run_id),
            second.recovery.recover_workflow(workflow.workflow_run_id),
        )
        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )

        assert first_result == second_result
        assert first_result.nodes[0].run_id == created.run_id
        assert lease_barrier.arrivals == 2
        assert completion.calls == 2
        assert handler_calls in [[("first", 7)], [("second", 7)]]
    finally:
        await asyncio.gather(first.close(), second.close())
        await asyncio.gather(*(store.close() for store in sqlite_stores))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_two_sdks_mcp_backed_tool_transport_executes_once(
    backend: str,
    tmp_path: Path,
) -> None:
    first_delegate, second_delegate, sqlite_stores = await _open_backend_pair(
        backend,
        tmp_path / "mcp-tool.db",
    )
    lease_barrier = _LeaseBarrier()
    first_store = _LeaseBarrierStore(first_delegate, lease_barrier)
    second_store = _LeaseBarrierStore(second_delegate, lease_barrier)
    completion = _ToolCompletion("mcp.demo.echo")
    transport_calls: list[tuple[str, dict[str, Any]]] = []
    first = AgentSDK.for_test(
        store=first_store,
        acompletion=completion,
        permission_default="allow",
    )
    first.agents.define(AGENT)
    first_manager = MCPManager._for_test(
        first.tools,
        _CountingMCPSession(transport_calls).connector,
    )
    second = AgentSDK.for_test(
        store=second_store,
        acompletion=completion,
        permission_default="allow",
    )
    second.agents.define(AGENT)
    second_manager = MCPManager._for_test(
        second.tools,
        _CountingMCPSession(transport_calls).connector,
    )
    config = MCPServerConfig(
        name="demo",
        transport=StdioMCPTransport(command="ignored"),
    )
    await asyncio.gather(
        first_manager.connect(config),
        second_manager.connect(config),
    )
    workflow = await _seed_preconfigured_sdk(first, first_store)
    selected = await _select_root(first_store, workflow)  # type: ignore[arg-type]
    created = await _create_selected_run(first, selected)
    try:
        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow.workflow_run_id),
            second.recovery.recover_workflow(workflow.workflow_run_id),
        )
        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )

        assert first_result == second_result
        assert first_result.nodes[0].run_id == created.run_id
        assert lease_barrier.arrivals == 2
        assert completion.calls == 2
        assert transport_calls == [("echo", {"value": 7})]
    finally:
        await asyncio.gather(first_manager.close(), second_manager.close())
        await asyncio.gather(first.close(), second.close())
        await asyncio.gather(*(store.close() for store in sqlite_stores))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_two_sdks_permission_ask_has_one_request_decision_and_tool_call(
    backend: str,
    tmp_path: Path,
) -> None:
    first_delegate, second_delegate, sqlite_stores = await _open_backend_pair(
        backend,
        tmp_path / "permission-ask.db",
    )
    lease_barrier = _LeaseBarrier()
    first_store = _LeaseBarrierStore(first_delegate, lease_barrier)
    second_store = _LeaseBarrierStore(second_delegate, lease_barrier)
    completion = _ToolCompletion("calculate")
    tool = ToolSpec(
        name="calculate",
        description="Calculate a value",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        version="1",
        source="application",
        effects=("execute",),
        timeout_seconds=2.0,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
    )
    handler_calls: list[tuple[str, int]] = []

    def handler(owner: str) -> Any:
        async def execute(_: ToolContext, value: int) -> dict[str, int]:
            handler_calls.append((owner, value))
            return {"value": value}

        return execute

    first = AgentSDK.for_test(store=first_store, acompletion=completion)
    first.agents.define(AGENT)
    first.tools.register(tool, handler("first"))
    workflow = await _seed_preconfigured_sdk(first, first_store)
    selected = await _select_root(first_store, workflow)  # type: ignore[arg-type]
    created = await _create_selected_run(first, selected)
    second = AgentSDK.for_test(store=second_store, acompletion=completion)
    second.agents.define(AGENT)
    second.tools.register(tool, handler("second"))
    permission_tasks: list[asyncio.Task[Any]] = []
    try:
        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow.workflow_run_id),
            second.recovery.recover_workflow(workflow.workflow_run_id),
        )
        permission_tasks = [
            asyncio.create_task(first.permissions.next_request(created.run_id)),
            asyncio.create_task(second.permissions.next_request(created.run_id)),
        ]
        done, pending = await asyncio.wait(
            permission_tasks,
            timeout=1,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert len(done) == 1
        request_task = done.pop()
        request = request_task.result()
        owner_index = permission_tasks.index(request_task)
        owner = (first, second)[owner_index]
        await owner.permissions.resolve(
            request.request_id,
            PermissionDecision.allow_once(),
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        first_result, second_result = await asyncio.gather(
            first_handle.result(),
            second_handle.result(),
        )
        assert first_result == second_result
        assert lease_barrier.arrivals == 2
        assert completion.calls == 2
        assert handler_calls in [[("first", 7)], [("second", 7)]]
        events = await first_delegate.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert sum(item.event.type == "permission.requested" for item in events) == 1
        assert sum(item.event.type == "permission.resolved" for item in events) == 1
    finally:
        for task in permission_tasks:
            task.cancel()
        await asyncio.gather(*permission_tasks, return_exceptions=True)
        await asyncio.gather(first.close(), second.close())
        await asyncio.gather(*(store.close() for store in sqlite_stores))


_WORKFLOW_AMBIGUOUS_COMMIT_CASES = (
    pytest.param("memory", "workflow.node.started", False, False, id="node-started"),
    pytest.param("memory", "run.created", False, False, id="run-created"),
    pytest.param("memory", "run.completed", True, False, id="run-terminal"),
    pytest.param(
        "memory",
        "workflow.node.completed",
        False,
        False,
        id="node-completed",
    ),
    pytest.param(
        "memory",
        "workflow.node.failed",
        False,
        True,
        id="node-failed",
    ),
    pytest.param("memory", "workflow.completed", False, False, id="workflow-completed"),
    pytest.param("memory", "workflow.failed", False, True, id="workflow-failed"),
    pytest.param(
        "sqlite",
        "run.completed",
        True,
        False,
        id="sqlite-run-terminal-reopen",
    ),
    pytest.param(
        "sqlite",
        "workflow.completed",
        False,
        False,
        id="sqlite-workflow-terminal-reopen",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "event_type", "run_progress", "provider_fails"),
    _WORKFLOW_AMBIGUOUS_COMMIT_CASES,
)
async def test_workflow_recovery_reuses_ambiguous_durable_commit(
    backend: str,
    event_type: str,
    run_progress: bool,
    provider_fails: bool,
    tmp_path: Path,
) -> None:
    database = tmp_path / f"ambiguous-{event_type.replace('.', '-')}.db"
    raw_store: Any = (
        InMemoryStore() if backend == "memory" else await SQLiteStore.open(database)
    )
    crashing_store = _CancelAfterDurableCommitStore(
        raw_store,
        event_type,
        run_progress=run_progress,
    )
    completion = _CountingCompletion(fail=provider_fails)
    first, workflow = await _seed_current_workflow(  # type: ignore[arg-type]
        crashing_store,
        completion,
    )
    first_handle = await first.recovery.recover_workflow(workflow.workflow_run_id)
    if run_progress:
        assert (await first_handle.result()).status is WorkflowRunStatus.COMPLETED
    else:
        with pytest.raises(asyncio.CancelledError):
            await first_handle.result()
    assert crashing_store.fired is True
    await first.close()

    if backend == "sqlite":
        await raw_store.close()
        recovered_store: Any = await SQLiteStore.open(database)
    else:
        recovered_store = raw_store
    reopened = AgentSDK.for_test(store=recovered_store, acompletion=completion)
    reopened.agents.define(AGENT)
    try:
        recovered_handle = await reopened.recovery.recover_workflow(
            workflow.workflow_run_id
        )
        if provider_fails:
            with pytest.raises(AgentSDKError):
                await recovered_handle.result()
            recovered = await reopened.workflows.get(workflow.workflow_run_id)
            assert recovered.status is WorkflowRunStatus.FAILED
        else:
            recovered_result = await recovered_handle.result()
            assert recovered_result.status is WorkflowRunStatus.COMPLETED

        events = await recovered_store.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert sum(item.event.type == event_type for item in events) == 1
        assert completion.calls == 1
    finally:
        await reopened.close()
        if backend == "sqlite":
            await recovered_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_selected_run_substitution_is_zero_external_work_on_both_stores(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "selected-run-substitution.db")
    )
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    selected = await _select_root(store, workflow, "run_substituted")
    node = workflow.workflow.nodes[0]
    descriptor = sdk.workflows._executor._node_execution_descriptor(  # type: ignore[attr-defined]
        node
    )
    await sdk.runs._commands.start_run(  # type: ignore[attr-defined]
        workflow.session_id,
        run_id="run_substituted",
        agent_revision=node.agent_revision,
        user_input=node.input,
        workflow_run_id="wfr_substituted",
        workflow_node_id=node.id,
        execution_descriptor=descriptor,
    )
    cursor_before = await store.latest_cursor()
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        with pytest.raises(AgentSDKError) as rejected:
            await handle.result()

        assert rejected.value.code is ErrorCode.INVALID_STATE
        assert rejected.value.message == "related run does not match workflow node"
        assert await store.latest_cursor() == cursor_before
        assert completion.calls == 0
        assert selected.nodes[0].status is WorkflowNodeStatus.RUNNING
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_forged_child_parent_is_zero_external_work_on_both_stores(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "forged-child-parent.db")
    )
    completion = _CountingCompletion()
    sdk, workflow, parent = await _seed_selected_child_boundary(store, completion)
    forged = parent.model_copy(
        update={
            "version": parent.version + 1,
            "output_text": "forged parent projection",
        }
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    forged.run_id,
                    forged.session_id,
                    forged.version,
                    forged.model_dump(mode="json"),
                ),
            ),
        )
    )
    cursor_before = await store.latest_cursor()
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        with pytest.raises(AgentSDKError) as rejected:
            await handle.result()

        assert rejected.value.code is ErrorCode.INVALID_STATE
        assert rejected.value.message == "related parent run is invalid"
        assert await store.get_snapshot("run", "run_child_selected") is None
        assert await store.latest_cursor() == cursor_before
        assert completion.calls == 0
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_closing_session_pending_workflow_creates_no_new_run(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "closing-pending-workflow.db")
    )
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    assert (await sdk.sessions.close(workflow.session_id)).status is SessionStatus.CLOSING
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        with pytest.raises(AgentSDKError) as rejected:
            await handle.result()

        assert rejected.value.code is ErrorCode.INVALID_STATE
        durable = await sdk.workflows.get(workflow.workflow_run_id)
        selected_run_id = durable.nodes[0].run_id
        assert selected_run_id is not None
        assert await store.get_snapshot("run", selected_run_id) is None
        events = await store.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert not any(item.event.type == "run.created" for item in events)
        assert completion.calls == 0
        assert (await sdk.sessions.get(workflow.session_id)).status is SessionStatus.CLOSING
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_closing_session_allows_exact_terminal_run_projection(
    backend: str,
    tmp_path: Path,
) -> None:
    store: Any = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(tmp_path / "closing-terminal-run.db")
    )
    completion = _CountingCompletion()
    sdk, workflow = await _seed_current_workflow(store, completion)
    selected = await _select_root(store, workflow)
    created = await _create_selected_run(sdk, selected)
    await (await sdk.recovery.recover_run(created.run_id)).result()
    assert completion.calls == 1
    assert (await sdk.sessions.close(workflow.session_id)).status is SessionStatus.CLOSING
    try:
        result = await (
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        ).result()

        assert result.status is WorkflowRunStatus.COMPLETED
        assert result.nodes[0].run_id == created.run_id
        session = await sdk.sessions.get(workflow.session_id)
        assert session.status is SessionStatus.CLOSED
        assert session.active_run_ids == ()
        assert session.active_workflow_run_ids == ()
        assert completion.calls == 1
    finally:
        await sdk.close()
        if backend == "sqlite":
            await store.close()


@pytest.mark.asyncio
async def test_sdk_close_settles_workflow_recovery_before_rejecting_new_work() -> None:
    store = InMemoryStore()
    completion = _BlockingToolCompletion("calculate")
    tool = ToolSpec(
        name="calculate",
        description="Calculate a value",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        version="1",
        source="application",
        effects=("execute",),
        timeout_seconds=2.0,
        retry_policy=ToolRetryPolicy.IDEMPOTENT,
    )
    handler_calls = 0

    async def handler(_: ToolContext, value: int) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        return {"value": value}

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(AGENT)
    sdk.tools.register(tool, handler)
    workflow = await _seed_preconfigured_sdk(sdk, store)
    selected = await _select_root(store, workflow)
    created = await _create_selected_run(sdk, selected)
    handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
    await asyncio.wait_for(completion.started.wait(), timeout=1)
    closing = asyncio.create_task(sdk.close())
    try:
        await asyncio.sleep(0)
        assert closing.done() is False
        completion.release.set()
        await asyncio.wait_for(closing, timeout=2)

        result = await handle.result()
        assert result.status is WorkflowRunStatus.COMPLETED
        assert result.nodes[0].run_id == created.run_id
        assert completion.calls == 2
        assert handler_calls == 1
        calls_after_close = completion.calls
        handlers_after_close = handler_calls
        with pytest.raises(AgentSDKError) as rejected:
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        assert rejected.value.code is ErrorCode.INVALID_STATE
        await asyncio.sleep(0)
        assert completion.calls == calls_after_close
        assert handler_calls == handlers_after_close
        assert sdk.workflows._executor._active == {}  # type: ignore[attr-defined]
        assert sdk.recovery._tasks == {}  # type: ignore[attr-defined]
    finally:
        completion.release.set()
        await closing


@pytest.mark.asyncio
async def test_workflow_recovery_store_error_traceback_is_secret_free() -> None:
    secret = "PHASE4B_STORE_SECRET_7f1c"
    store = _SecretWorkflowReadStore(InMemoryStore(), secret)
    sdk, workflow = await _seed_current_workflow(store, _CountingCompletion())  # type: ignore[arg-type]
    store.armed = True
    try:
        with pytest.raises(AgentSDKError) as caught:
            await sdk.recovery.recover_workflow(workflow.workflow_run_id)

        _assert_public_error_is_secret_free(caught.value, secret)
        assert caught.value.code is ErrorCode.INTERNAL
    finally:
        store.armed = False
        await sdk.close()


@pytest.mark.asyncio
async def test_workflow_recovery_provider_error_traceback_is_secret_free() -> None:
    secret = "PHASE4B_PROVIDER_SECRET_02ad"

    async def provider(**values: Any) -> AsyncIterator[dict[str, object]]:
        del values
        raise RuntimeError(secret)

    store = InMemoryStore()
    sdk, workflow = await _seed_current_workflow(store, provider)
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        with pytest.raises(AgentSDKError) as caught:
            await handle.result()

        _assert_public_error_is_secret_free(caught.value, secret)
        assert (await sdk.workflows.get(workflow.workflow_run_id)).status is WorkflowRunStatus.FAILED
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_workflow_recovery_tool_failure_result_is_secret_free() -> None:
    secret = "PHASE4B_TOOL_SECRET_164e"
    store = InMemoryStore()
    completion = _ToolCompletion("calculate")
    tool = ToolSpec(
        name="calculate",
        description="Calculate a value",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        effects=("execute",),
    )

    async def handler(_: ToolContext, value: int) -> None:
        del value
        raise RuntimeError(secret)

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(AGENT)
    sdk.tools.register(tool, handler)
    workflow = await _seed_preconfigured_sdk(sdk, store)
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        result = await handle.result()

        run_id = result.nodes[0].run_id
        assert run_id is not None
        run = await sdk.runs.get(run_id)
        events = await store.read_events(after_cursor=0)
        assert secret not in repr(result)
        assert secret not in repr(run)
        assert secret not in repr(events)
        assert completion.calls == 2
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_workflow_recovery_mcp_failure_result_is_secret_free() -> None:
    secret = "PHASE4B_MCP_SECRET_e81a"
    store = InMemoryStore()
    completion = _ToolCompletion("mcp.demo.echo")
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(AGENT)
    manager = MCPManager._for_test(
        sdk.tools,
        _SecretMCPSession([], secret).connector,
    )
    await manager.connect(
        MCPServerConfig(
            name="demo",
            transport=StdioMCPTransport(command="ignored"),
        )
    )
    workflow = await _seed_preconfigured_sdk(sdk, store)
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        result = await handle.result()

        run_id = result.nodes[0].run_id
        assert run_id is not None
        run = await sdk.runs.get(run_id)
        events = await store.read_events(after_cursor=0)
        assert secret not in repr(result)
        assert secret not in repr(run)
        assert secret not in repr(events)
        assert completion.calls == 2
    finally:
        await manager.close()
        await sdk.close()


@pytest.mark.asyncio
async def test_workflow_recovery_permission_error_traceback_is_secret_free() -> None:
    secret = "PHASE4B_PERMISSION_SECRET_b913"
    store = InMemoryStore()
    completion = _ToolCompletion("calculate")
    bridge = _SecretPermissionBridge(secret)
    tool = ToolSpec(
        name="calculate",
        description="Calculate a value",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        effects=("execute",),
    )
    handler_calls = 0

    async def handler(_: ToolContext, value: int) -> dict[str, int]:
        nonlocal handler_calls
        handler_calls += 1
        return {"value": value}

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_bridge=bridge,
    )
    sdk.agents.define(AGENT)
    sdk.tools.register(tool, handler)
    workflow = await _seed_preconfigured_sdk(sdk, store)
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        with pytest.raises(AgentSDKError) as caught:
            await handle.result()

        _assert_public_error_is_secret_free(caught.value, secret)
        assert completion.calls == 1
        assert handler_calls == 0
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
        assert durable.nodes[0].status is WorkflowNodeStatus.RUNNING
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
