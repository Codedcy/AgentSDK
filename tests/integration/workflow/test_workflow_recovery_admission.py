from __future__ import annotations

import asyncio
import inspect
import traceback
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
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
    RecoveryAPI,
    ToolContext,
)
from agent_sdk.errors import SessionBusyError
from agent_sdk.events.models import EventEnvelope
from agent_sdk.mcp import MCPManager, MCPServerConfig, StdioMCPTransport
from agent_sdk.permissions.broker import InProcessPermissionBridge
from agent_sdk.runtime.leases import Lease, LeaseLostError
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionStatus
from agent_sdk.runtime.recovery import RecoveryPlan
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
    RunCheckpointPhase,
)
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
_DIAGNOSTIC_TIMEOUT_SECONDS = 10.0


async def _wait_for_event(
    event: asyncio.Event,
    *,
    diagnostic: Callable[[], object],
) -> None:
    try:
        await asyncio.wait_for(
            event.wait(),
            timeout=_DIAGNOSTIC_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise AssertionError(f"coordination timeout: {diagnostic()!r}") from None


async def _cancel_sdk_active_tasks(sdk: AgentSDK) -> None:
    tasks = tuple(sdk._active_tasks)  # type: ignore[attr-defined]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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
            await _wait_for_event(
                self.release,
                diagnostic=lambda: {"phase": "provider_release", "calls": self.calls},
            )
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
    idempotency_key: str | None = None,
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
        idempotency_key=idempotency_key,
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
                await _wait_for_event(
                    self.release,
                    diagnostic=lambda: {
                        "phase": "run_read_release",
                        "run_reads": self.run_reads,
                        "block_on_read": self.block_on_read,
                    },
                )
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
            await _wait_for_event(
                self.barrier.both_arrived,
                diagnostic=lambda: {
                    "phase": "projection_arrivals",
                    "arrivals": self.barrier.arrivals,
                    "event_type": self.event_type,
                },
            )
            if self.recovery_winner:
                result = await self.delegate.commit(batch)
                self.barrier.recovery_committed.set()
                return result
            await _wait_for_event(
                self.barrier.recovery_committed,
                diagnostic=lambda: {
                    "phase": "projection_winner_commit",
                    "arrivals": self.barrier.arrivals,
                    "event_type": self.event_type,
                },
            )
        return await self.delegate.commit(batch)


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
        await _wait_for_event(
            self.barrier.ready,
            diagnostic=lambda: {
                "phase": "lease_arrivals",
                "arrivals": self.barrier.arrivals,
            },
        )
        return await self.delegate.acquire_lease(**values)


class _LeaseRecordingStore:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.acquired: list[Lease] = []
        self.released: list[Lease] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def acquire_lease(self, **values: Any) -> Lease:
        lease = await self.delegate.acquire_lease(**values)
        self.acquired.append(lease)
        return lease

    async def release_lease(self, lease: Lease) -> None:
        await self.delegate.release_lease(lease)
        self.released.append(lease)


class _BusyDeleteBarrier:
    def __init__(self) -> None:
        self.arrivals = 0
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        self.run_ids: list[str] = []
        self.operation_ids: set[str] = set()


class _BusyDeleteBarrierStore:
    def __init__(self, delegate: Any, barrier: _BusyDeleteBarrier) -> None:
        self.delegate = delegate
        self.barrier = barrier

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def acquire_lease(self, **values: Any) -> Lease:
        run_id = values.get("run_id")
        assert isinstance(run_id, str)
        self.barrier.run_ids.append(run_id)
        self.barrier.arrivals += 1
        if self.barrier.arrivals == 2:
            self.barrier.ready.set()
        await _wait_for_event(
            self.barrier.release,
            diagnostic=lambda: {
                "phase": "busy_delete_lease_release",
                "arrivals": self.barrier.arrivals,
                "run_ids": tuple(self.barrier.run_ids),
            },
        )
        return await self.delegate.acquire_lease(**values)

    async def commit_run_progress(self, batch: RunProgressBatch) -> Any:
        result = await self.delegate.commit_run_progress(batch)
        if batch.operation is not None:
            self.barrier.operation_ids.add(batch.operation.updated.operation_id)
        return result


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
            await _wait_for_event(
                self.release,
                diagnostic=lambda: {
                    "phase": "blocking_tool_provider_release",
                    "calls": self.calls,
                },
            )
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


def test_workflow_recovery_has_one_public_entry_point() -> None:
    assert "recover" not in workflow_executor_module.WorkflowExecutor.__dict__
    signature = inspect.signature(RecoveryAPI.recover_workflow)
    assert tuple(signature.parameters) == ("self", "workflow_run_id")
    assert signature.parameters["workflow_run_id"].annotation == "str"
    assert signature.return_annotation == "WorkflowHandle"


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
            await _wait_for_event(
                release_ownership,
                diagnostic=lambda: {
                    "phase": "capability_mutation_release",
                    "load_count": load_count,
                },
            )
        return await original_load_session(store_value, session_id)

    workflow_executor_module.load_session = blocked_load_session
    before_workflow = await sdk.workflows.get(workflow.workflow_run_id)
    before_session = await sdk.sessions.get(workflow.session_id)
    cursor_before = await store.latest_cursor()
    try:
        handle = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        await _wait_for_event(
            ownership_blocked,
            diagnostic=lambda: {
                "phase": "capability_mutation_arrival",
                "load_count": load_count,
            },
        )
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
        await _wait_for_event(
            normal_store.blocked,
            diagnostic=lambda: {
                "phase": "normal_run_read",
                "normal_reads": normal_store.run_reads,
                "recovery_reads": recovery_store.run_reads,
            },
        )
        selected = await normal.workflows.get(normal_handle.workflow_run_id)
        selected_run_id = selected.nodes[0].run_id
        assert selected_run_id is not None
        assert await delegate.get_snapshot("run", selected_run_id) is None

        recovery = AgentSDK.for_test(store=recovery_store, acompletion=completion)
        recovery.agents.define(AGENT)
        recovery_handle = await recovery.recovery.recover_workflow(
            normal_handle.workflow_run_id
        )
        await _wait_for_event(
            recovery_store.blocked,
            diagnostic=lambda: {
                "phase": "recovery_run_read",
                "normal_reads": normal_store.run_reads,
                "recovery_reads": recovery_store.run_reads,
            },
        )
        normal_store.release.set()
        recovery_store.release.set()
        await _wait_for_event(
            completion.started,
            diagnostic=lambda: {
                "phase": "normal_recovery_provider_owner",
                "calls": completion.calls,
            },
        )
        completion.release.set()

        normal_result, recovery_result = await asyncio.wait_for(
            asyncio.gather(normal_handle.result(), recovery_handle.result()),
            timeout=_DIAGNOSTIC_TIMEOUT_SECONDS,
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
            await _wait_for_event(
                release_plan,
                diagnostic=lambda: {"phase": "normal_plan_release"},
            )
        return await original_plan(run_id)

    normal.recovery._service.plan = blocked_plan  # type: ignore[attr-defined,method-assign]
    session = await normal.sessions.create(workspaces=[])
    normal_handle = await normal.workflows.start(session.session_id, _workflow_yaml())
    recovery: AgentSDK | None = None
    try:
        await _wait_for_event(
            plan_blocked,
            diagnostic=lambda: {"phase": "normal_plan_arrival"},
        )
        selected = await normal.workflows.get(normal_handle.workflow_run_id)
        selected_run_id = selected.nodes[0].run_id
        assert selected_run_id is not None
        assert (await normal.runs.get(selected_run_id)).status is RunStatus.CREATED

        recovery = AgentSDK.for_test(store=recovery_store, acompletion=completion)
        recovery.agents.define(AGENT)
        recovery_handle = await recovery.recovery.recover_workflow(
            normal_handle.workflow_run_id
        )
        await _wait_for_event(
            completion.started,
            diagnostic=lambda: {
                "phase": "precreated_provider_owner",
                "calls": completion.calls,
            },
        )
        release_plan.set()
        completion.release.set()
        await _wait_for_event(
            projection.both_arrived,
            diagnostic=lambda: {
                "phase": "precreated_projection_arrivals",
                "arrivals": projection.arrivals,
            },
        )

        normal_result, recovery_result = await asyncio.wait_for(
            asyncio.gather(normal_handle.result(), recovery_handle.result()),
            timeout=_DIAGNOSTIC_TIMEOUT_SECONDS,
        )
        assert normal_result == recovery_result
        assert normal_result.status is WorkflowRunStatus.COMPLETED
        assert normal_result.nodes[0].run_id == selected_run_id
        assert completion.calls == 1
        assert (await normal.runs.get(selected_run_id)).status is RunStatus.COMPLETED
    finally:
        release_plan.set()
        completion.release.set()
        projection.both_arrived.set()
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
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_two_sdks_pending_node_select_one_run_and_converge(
    backend: str,
    tmp_path: Path,
) -> None:
    first_delegate, second_delegate, sqlite_stores = await _open_backend_pair(
        backend,
        tmp_path / "pending-node-race.db",
    )
    barrier = _ProjectionBarrier()
    first_store = _ProjectionBarrierStore(
        first_delegate,
        barrier,
        recovery_winner=True,
        event_type="workflow.node.started",
    )
    second_store = _ProjectionBarrierStore(
        second_delegate,
        barrier,
        recovery_winner=False,
        event_type="workflow.node.started",
    )
    completion = _CountingCompletion()
    first, workflow = await _seed_current_workflow(first_store, completion)  # type: ignore[arg-type]
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

        selected_run_id = first_result.nodes[0].run_id
        assert selected_run_id is not None
        assert second_result.nodes[0].run_id == selected_run_id
        events = await first_delegate.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert barrier.arrivals == 2
        assert sum(item.event.type == "workflow.node.started" for item in events) == 1
        assert sum(item.event.type == "run.created" for item in events) == 1
        assert sum(item.event.type == "session.run.attached" for item in events) == 1
        assert completion.calls == 1
    finally:
        barrier.both_arrived.set()
        barrier.recovery_committed.set()
        await asyncio.gather(first.close(), second.close())
        await asyncio.gather(*(store.close() for store in sqlite_stores))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_two_sdks_selected_missing_run_recreate_exactly_once(
    backend: str,
    tmp_path: Path,
) -> None:
    first_delegate, second_delegate, sqlite_stores = await _open_backend_pair(
        backend,
        tmp_path / "selected-missing-race.db",
    )
    barrier = _ProjectionBarrier()
    first_store = _ProjectionBarrierStore(
        first_delegate,
        barrier,
        recovery_winner=True,
        event_type="run.created",
    )
    second_store = _ProjectionBarrierStore(
        second_delegate,
        barrier,
        recovery_winner=False,
        event_type="run.created",
    )
    completion = _CountingCompletion()
    first, workflow = await _seed_current_workflow(first_store, completion)  # type: ignore[arg-type]
    selected = await _select_root(
        first_store,  # type: ignore[arg-type]
        workflow,
        "run_two_sdk_exact_missing",
    )
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
        assert first_result.nodes[0].run_id == "run_two_sdk_exact_missing"
        assert selected.nodes[0].run_id == "run_two_sdk_exact_missing"
        assert barrier.arrivals == 2
        run = await first.runs.get("run_two_sdk_exact_missing")
        assert run.status is RunStatus.COMPLETED
        events = await first_delegate.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert sum(item.event.type == "run.created" for item in events) == 1
        assert sum(item.event.type == "session.run.attached" for item in events) == 1
        assert completion.calls == 1
    finally:
        barrier.both_arrived.set()
        barrier.recovery_committed.set()
        await asyncio.gather(first.close(), second.close())
        await asyncio.gather(*(store.close() for store in sqlite_stores))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_two_sdks_selected_created_run_have_one_lease_owner(
    backend: str,
    tmp_path: Path,
) -> None:
    first_delegate, second_delegate, sqlite_stores = await _open_backend_pair(
        backend,
        tmp_path / "selected-created-race.db",
    )
    barrier = _LeaseBarrier()
    first_store = _LeaseBarrierStore(first_delegate, barrier)
    second_store = _LeaseBarrierStore(second_delegate, barrier)
    completion = _CountingCompletion()
    first, workflow = await _seed_current_workflow(first_store, completion)  # type: ignore[arg-type]
    selected = await _select_root(first_store, workflow)  # type: ignore[arg-type]
    created = await _create_selected_run(first, selected)
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

        assert first_result.nodes[0].run_id == created.run_id
        assert second_result.nodes[0].run_id == created.run_id
        assert barrier.arrivals == 2
        events = await first_delegate.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert sum(item.event.type == "run.created" for item in events) == 1
        assert completion.calls == 1
    finally:
        barrier.ready.set()
        await asyncio.gather(first.close(), second.close())
        await asyncio.gather(*(store.close() for store in sqlite_stores))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_two_sdks_follow_live_selected_run_owner_without_failure(
    backend: str,
    tmp_path: Path,
) -> None:
    first_store, second_store, sqlite_stores = await _open_backend_pair(
        backend,
        tmp_path / "selected-live-owner.db",
    )
    completion = _CountingCompletion(block=True)
    first, workflow = await _seed_current_workflow(first_store, completion)
    selected = await _select_root(first_store, workflow)
    created = await _create_selected_run(first, selected)
    second = AgentSDK.for_test(store=second_store, acompletion=completion)
    second.agents.define(AGENT)
    first_handle = await first.recovery.recover_workflow(workflow.workflow_run_id)
    try:
        await _wait_for_event(
            completion.started,
            diagnostic=lambda: {
                "phase": "live_run_owner",
                "provider_calls": completion.calls,
                "run_id": created.run_id,
            },
        )
        live = await first.runs.get(created.run_id)
        lease = await first_store.get_run_lease(created.run_id)
        assert live.status is RunStatus.RUNNING
        assert lease is not None
        second_handle = await second.recovery.recover_workflow(
            workflow.workflow_run_id
        )
        completion.release.set()
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first_handle.result(), second_handle.result()),
            timeout=_DIAGNOSTIC_TIMEOUT_SECONDS,
        )

        assert first_result == second_result
        assert first_result.nodes[0].run_id == created.run_id
        assert completion.calls == 1
        events = await first_store.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert not any(
            item.event.type in {"workflow.node.failed", "workflow.failed"}
            for item in events
        )
    finally:
        completion.release.set()
        await asyncio.gather(first.close(), second.close())
        await asyncio.gather(*(store.close() for store in sqlite_stores))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_two_sdks_expired_unreconciled_run_stays_active_and_bounded(
    backend: str,
    tmp_path: Path,
) -> None:
    database = tmp_path / "expired-unreconciled.db"
    sqlite_stores: list[SQLiteStore] = []
    if backend == "memory":
        initial_delegate: Any = InMemoryStore()
    else:
        initial_delegate = await SQLiteStore.open(database)
        sqlite_stores.append(initial_delegate)
    recording_store = _LeaseRecordingStore(initial_delegate)
    barrier = _LeaseBarrier()
    completion = _CountingCompletion(block=True)
    owner: AgentSDK | None = None
    first: AgentSDK | None = None
    second: AgentSDK | None = None
    owner_handle: Any = None
    try:
        owner, workflow = await _seed_current_workflow(  # type: ignore[arg-type]
            recording_store,
            completion,
        )
        selected = await _select_root(recording_store, workflow)  # type: ignore[arg-type]
        created = await _create_selected_run(owner, selected)
        owner_handle = await owner.recovery.recover_workflow(
            workflow.workflow_run_id
        )
        await _wait_for_event(
            completion.started,
            diagnostic=lambda: {
                "phase": "real_running_provider",
                "provider_calls": completion.calls,
                "run_id": created.run_id,
            },
        )

        running = await owner.runs.get(created.run_id)
        checkpoint = await initial_delegate.get_run_checkpoint(created.run_id)
        operations = await initial_delegate.list_external_operations(created.run_id)
        initial_lease = await initial_delegate.get_run_lease(created.run_id)
        assert running.status is RunStatus.RUNNING
        assert running.version == 2
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
        assert len(operations) == 1
        operation = operations[0]
        assert isinstance(operation, ModelCallOperation)
        assert operation.status is ExternalOperationStatus.STARTED
        assert initial_lease is not None
        assert operation.lease_generation == initial_lease.generation
        assert recording_store.acquired == [initial_lease]

        clock = [initial_lease.expires_at + timedelta(seconds=1)]
        owner._recovery_scanner._clock = lambda: clock[0]  # type: ignore[attr-defined]
        await owner.recovery.scan()

        interrupted = await owner.runs.get(created.run_id)
        scanner_lease = recording_store.acquired[-1]
        assert interrupted.status is RunStatus.INTERRUPTED
        assert interrupted.version == running.version + 1
        assert len(recording_store.acquired) == 2
        assert scanner_lease.generation == initial_lease.generation + 1
        assert scanner_lease.owner.startswith("coord_")
        assert scanner_lease.acquired_at == clock[0]
        assert recording_store.released == [scanner_lease]
        assert await initial_delegate.get_run_lease(created.run_id) is None
        assert await initial_delegate.get_run_checkpoint(created.run_id) == checkpoint
        assert await initial_delegate.list_external_operations(created.run_id) == operations
        with pytest.raises(LeaseLostError):
            await initial_delegate.assert_current_lease(initial_lease, now=clock[0])
        events_after_scan = [
            item.event
            for item in await initial_delegate.read_events(
                after_cursor=0,
                session_id=workflow.session_id,
            )
            if item.event.run_id == created.run_id
        ]
        assert [event.type for event in events_after_scan] == [
            "run.created",
            "run.started",
            "step.started",
            "model.call.started",
            "run.interrupted",
        ]
        assert events_after_scan[-1].payload == {"status": "interrupted"}

        await _cancel_sdk_active_tasks(owner)
        await owner.close()
        owner = None
        await asyncio.gather(owner_handle.result(), return_exceptions=True)
        owner_handle = None
        if backend == "memory":
            first_delegate = second_delegate = initial_delegate
        else:
            await initial_delegate.close()
            sqlite_stores.remove(initial_delegate)
            first_delegate = await SQLiteStore.open(database)
            second_delegate = await SQLiteStore.open(database)
            sqlite_stores.extend((first_delegate, second_delegate))
        first_store = _LeaseBarrierStore(first_delegate, barrier)
        second_store = _LeaseBarrierStore(second_delegate, barrier)
        first = AgentSDK.for_test(store=first_store, acompletion=completion)
        first.agents.define(AGENT)
        second = AgentSDK.for_test(store=second_store, acompletion=completion)
        second.agents.define(AGENT)
        first.recovery._service._clock = lambda: clock[0]  # type: ignore[attr-defined]
        second.recovery._service._clock = lambda: clock[0]  # type: ignore[attr-defined]
        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow.workflow_run_id),
            second.recovery.recover_workflow(workflow.workflow_run_id),
        )
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                first_handle.result(),
                second_handle.result(),
                return_exceptions=True,
            ),
            timeout=_DIAGNOSTIC_TIMEOUT_SECONDS,
        )

        assert barrier.arrivals == 2
        assert len(outcomes) == 2
        for outcome in outcomes:
            assert isinstance(outcome, AgentSDKError)
            assert outcome.code is ErrorCode.CONFLICT
            assert outcome.message == "recovery required"
            assert outcome.retryable is True
        durable_run = await first.runs.get(created.run_id)
        durable_workflow = await first.workflows.get(workflow.workflow_run_id)
        session = await first.sessions.get(workflow.session_id)
        assert durable_run.status is RunStatus.WAITING_RECONCILIATION
        assert durable_run.version == interrupted.version + 1
        assert durable_workflow.status is WorkflowRunStatus.RUNNING
        assert durable_workflow.nodes[0].status is WorkflowNodeStatus.RUNNING
        assert session.active_run_ids == (created.run_id,)
        assert session.active_workflow_run_ids == (workflow.workflow_run_id,)
        assert completion.calls == 1
        assert await first_delegate.get_run_lease(created.run_id) is None
        assert await first_delegate.get_run_checkpoint(created.run_id) == checkpoint
        assert await first_delegate.list_external_operations(created.run_id) == operations
        requests = await first_delegate.list_pending_reconciliation_requests(
            created.run_id
        )
        assert len(requests) == 1
        assert requests[0].operation_id == operation.operation_id
        assert requests[0].reason == "model_call_unknown_outcome"
        assert requests[0].details == {
            "checkpoint_phase": RunCheckpointPhase.MODEL_IN_FLIGHT.value
        }
        events = await first_delegate.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        run_events = [
            item.event for item in events if item.event.run_id == created.run_id
        ]
        assert run_events[:-1] == events_after_scan
        assert run_events[-1].type == "reconciliation.requested"
        assert run_events[-1].payload == {
            "request_id": requests[0].request_id,
            "operation_id": operation.operation_id,
            "reason": "model_call_unknown_outcome",
        }
        assert not any(
            item.event.type
            in {
                "run.completed",
                "run.failed",
                "workflow.node.completed",
                "workflow.node.failed",
                "workflow.completed",
                "workflow.failed",
            }
            for item in events
        )
        await asyncio.sleep(0)
        assert first.workflows._executor._active == {}  # type: ignore[attr-defined]
        assert second.workflows._executor._active == {}  # type: ignore[attr-defined]
        assert first.recovery._tasks == {}  # type: ignore[attr-defined]
        assert second.recovery._tasks == {}  # type: ignore[attr-defined]
    finally:
        barrier.ready.set()
        completion.release.set()
        if owner is not None:
            await _cancel_sdk_active_tasks(owner)
            await owner.close()
        if owner_handle is not None:
            await asyncio.gather(owner_handle.result(), return_exceptions=True)
        await asyncio.gather(
            *(sdk.close() for sdk in (first, second) if sdk is not None)
        )
        await asyncio.gather(*(store.close() for store in sqlite_stores))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("lifecycle", ["active", "closing"])
async def test_two_sdks_public_busy_delete_races_recovery_without_resurrection(
    backend: str,
    lifecycle: str,
    tmp_path: Path,
) -> None:
    first_delegate, second_delegate, sqlite_stores = await _open_backend_pair(
        backend,
        tmp_path / f"workflow-busy-delete-{lifecycle}.db",
    )
    barrier = _BusyDeleteBarrier()
    first_store = _BusyDeleteBarrierStore(first_delegate, barrier)
    second_store = _BusyDeleteBarrierStore(second_delegate, barrier)
    completion = _CountingCompletion()
    first: AgentSDK | None = None
    second: AgentSDK | None = None
    try:
        first, workflow = await _seed_current_workflow(  # type: ignore[arg-type]
            first_store,
            completion,
            idempotency_key="phase4-public-busy-delete",
        )
        selected = await _select_root(first_store, workflow)  # type: ignore[arg-type]
        created = await _create_selected_run(first, selected)
        if lifecycle == "closing":
            closing = await first.sessions.close(workflow.session_id)
            assert closing.status is SessionStatus.CLOSING
            assert closing.active_run_ids == (created.run_id,)
            assert closing.active_workflow_run_ids == (workflow.workflow_run_id,)
        second = AgentSDK.for_test(store=second_store, acompletion=completion)
        second.agents.define(AGENT)
        first_handle, second_handle = await asyncio.gather(
            first.recovery.recover_workflow(workflow.workflow_run_id),
            second.recovery.recover_workflow(workflow.workflow_run_id),
        )
        await _wait_for_event(
            barrier.ready,
            diagnostic=lambda: {
                "phase": "busy_delete_arrivals",
                "arrivals": barrier.arrivals,
                "run_ids": tuple(barrier.run_ids),
                "lifecycle": lifecycle,
            },
        )
        assert barrier.arrivals == 2
        assert barrier.run_ids == [created.run_id, created.run_id]
        before_delete = await first.sessions.get(workflow.session_id)
        assert before_delete.status is (
            SessionStatus.CLOSING
            if lifecycle == "closing"
            else SessionStatus.ACTIVE
        )
        cursor_before_delete = await first_delegate.latest_cursor()
        with pytest.raises(SessionBusyError) as busy:
            await second.sessions.delete(workflow.session_id)

        assert busy.value.code is ErrorCode.CONFLICT
        assert busy.value.message == "session has active work"
        assert busy.value.retryable is False
        assert await first_delegate.latest_cursor() == cursor_before_delete
        assert await first.sessions.get(workflow.session_id) == before_delete
        barrier.release.set()
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(
                first_handle.result(),
                second_handle.result(),
            ),
            timeout=_DIAGNOSTIC_TIMEOUT_SECONDS,
        )

        assert first_result == second_result
        assert first_result.status is WorkflowRunStatus.COMPLETED
        assert first_result.nodes[0].run_id == created.run_id
        assert completion.calls == 1
        assert (await first.runs.get(created.run_id)).status is RunStatus.COMPLETED
        assert (
            await first.workflows.get(workflow.workflow_run_id)
        ).status is WorkflowRunStatus.COMPLETED
        settled_session = await first.sessions.get(workflow.session_id)
        assert settled_session.active_run_ids == ()
        assert settled_session.active_workflow_run_ids == ()
        assert settled_session.status is (
            SessionStatus.CLOSED
            if lifecycle == "closing"
            else SessionStatus.ACTIVE
        )
        events = await first_delegate.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert sum(item.event.type == "run.created" for item in events) == 1
        assert sum(item.event.type == "run.completed" for item in events) == 1
        assert sum(
            item.event.type == "workflow.completed" for item in events
        ) == 1
        assert not any(
            item.event.type
            in {
                "permission.requested",
                "reconciliation.requested",
                "run.failed",
                "workflow.node.failed",
                "workflow.failed",
            }
            for item in events
        )
        assert len(barrier.operation_ids) == 1
        operation_id = next(iter(barrier.operation_ids))
        operation = await first_delegate.get_external_operation(operation_id)
        assert isinstance(operation, ModelCallOperation)
        assert operation.status is ExternalOperationStatus.COMPLETED

        if settled_session.status is SessionStatus.ACTIVE:
            closed = await first.sessions.close(workflow.session_id)
            assert closed.status is SessionStatus.CLOSED
        await second.sessions.delete(workflow.session_id)

        assert await first_delegate.get_snapshot("session", workflow.session_id) is None
        assert await first_delegate.get_snapshot("run", created.run_id) is None
        assert (
            await first_delegate.get_snapshot("workflow", workflow.workflow_run_id)
            is None
        )
        for node in workflow.nodes:
            assert await first_delegate.get_snapshot(
                "workflow_node",
                node.entity_id,
            ) is None
        assert await first_delegate.get_run_lease(created.run_id) is None
        assert await first_delegate.get_run_checkpoint(created.run_id) is None
        assert await first_delegate.get_external_operation(operation_id) is None
        assert (
            await first_delegate.list_unresolved_external_operations(created.run_id)
            == ()
        )
        assert (
            await first_delegate.list_pending_reconciliation_requests(created.run_id)
            == ()
        )
        assert (
            await first_delegate.read_events(
                after_cursor=0,
                session_id=workflow.session_id,
            )
            == []
        )
        assert (
            await first_delegate.get_idempotency(
                f"session/{workflow.session_id}/workflow.start",
                "phase4-public-busy-delete",
            )
            is None
        )
        for sdk in (first, second):
            with pytest.raises(AgentSDKError) as deleted:
                await sdk.recovery.recover_workflow(workflow.workflow_run_id)
            assert deleted.value.code is ErrorCode.NOT_FOUND
        assert await first_delegate.get_snapshot("session", workflow.session_id) is None
        assert await first_delegate.read_events(after_cursor=0) == []
        assert completion.calls == 1
        await asyncio.sleep(0)
        assert first.workflows._executor._active == {}  # type: ignore[attr-defined]
        assert second.workflows._executor._active == {}  # type: ignore[attr-defined]
        assert first.recovery._tasks == {}  # type: ignore[attr-defined]
        assert second.recovery._tasks == {}  # type: ignore[attr-defined]
    finally:
        barrier.release.set()
        barrier.ready.set()
        await asyncio.gather(
            *(sdk.close() for sdk in (first, second) if sdk is not None)
        )
        await asyncio.gather(*(store.close() for store in sqlite_stores))


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
        barrier.both_arrived.set()
        barrier.recovery_committed.set()
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
        barrier.both_arrived.set()
        barrier.recovery_committed.set()
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
        lease_barrier.ready.set()
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
        lease_barrier.ready.set()
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
            timeout=_DIAGNOSTIC_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            durable = await first.runs.get(created.run_id)
            raise AssertionError(
                "permission owner timeout: "
                f"lease_arrivals={lease_barrier.arrivals}, run={durable!r}"
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
        lease_barrier.ready.set()
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
    recovered_store: Any = None
    reopened: AgentSDK | None = None
    try:
        first_handle = await first.recovery.recover_workflow(
            workflow.workflow_run_id
        )
        if run_progress:
            assert (
                await first_handle.result()
            ).status is WorkflowRunStatus.COMPLETED
        else:
            with pytest.raises(asyncio.CancelledError):
                await first_handle.result()
        assert crashing_store.fired is True

        committed = await first.workflows.get(workflow.workflow_run_id)
        committed_session = await first.sessions.get(workflow.session_id)
        committed_events = await raw_store.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        selected_run_id = committed.nodes[0].run_id
        assert selected_run_id is not None
        started_events = [
            item.event
            for item in committed_events
            if item.event.type == "workflow.node.started"
        ]
        assert len(started_events) == 1
        assert started_events[0].payload["run_id"] == selected_run_id
        assert committed.nodes[0].run_id == selected_run_id

        if event_type == "workflow.node.started":
            assert committed.status is WorkflowRunStatus.RUNNING
            assert committed.nodes[0].status is WorkflowNodeStatus.RUNNING
            assert (
                await raw_store.get_snapshot("run", selected_run_id)
                is None
            )
            assert committed_session.active_run_ids == ()
            assert workflow.workflow_run_id in (
                committed_session.active_workflow_run_ids
            )
        elif event_type == "run.created":
            created = await first.runs.get(selected_run_id)
            assert created.status is RunStatus.CREATED
            assert created.workflow_run_id == workflow.workflow_run_id
            assert created.workflow_node_id == committed.nodes[0].node_id
            assert committed.nodes[0].status is WorkflowNodeStatus.RUNNING
            assert committed_session.active_run_ids == (selected_run_id,)
            assert sum(
                item.event.type == "run.created" for item in committed_events
            ) == 1
            assert sum(
                item.event.type == "session.run.attached"
                for item in committed_events
            ) == 1
        elif event_type in {"workflow.node.completed", "workflow.node.failed"}:
            expected_node_status = (
                WorkflowNodeStatus.FAILED
                if provider_fails
                else WorkflowNodeStatus.COMPLETED
            )
            assert committed.status is WorkflowRunStatus.RUNNING
            assert committed.nodes[0].status is expected_node_status
            assert committed_session.active_run_ids == ()
            assert committed_session.active_workflow_run_ids == (
                workflow.workflow_run_id,
            )
            node_write = await raw_store.get_snapshot(
                "workflow_node",
                committed.nodes[0].entity_id,
            )
            assert node_write is not None
            assert node_write == committed.nodes[0].model_dump(mode="json")
        else:
            expected_status = (
                WorkflowRunStatus.FAILED
                if provider_fails
                else WorkflowRunStatus.COMPLETED
            )
            assert committed.status is expected_status
            assert committed_session.status is SessionStatus.ACTIVE
            assert committed_session.active_run_ids == ()
            assert committed_session.active_workflow_run_ids == ()
            assert sum(
                item.event.type == "session.workflow.detached"
                for item in committed_events
            ) == 1

        await first.close()
        first = None  # type: ignore[assignment]
        if backend == "sqlite":
            await raw_store.close()
            raw_store = None
            recovered_store = await SQLiteStore.open(database)
        else:
            recovered_store = crashing_store.delegate
        reopened = AgentSDK.for_test(
            store=recovered_store,
            acompletion=completion,
        )
        reopened.agents.define(AGENT)
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

        recovered = await reopened.workflows.get(workflow.workflow_run_id)
        selected_run_id = recovered.nodes[0].run_id
        assert selected_run_id is not None
        recovered_run = await reopened.runs.get(selected_run_id)
        recovered_session = await reopened.sessions.get(workflow.session_id)
        expected_run_status = (
            RunStatus.FAILED if provider_fails else RunStatus.COMPLETED
        )
        expected_node_status = (
            WorkflowNodeStatus.FAILED
            if provider_fails
            else WorkflowNodeStatus.COMPLETED
        )
        expected_workflow_status = (
            WorkflowRunStatus.FAILED
            if provider_fails
            else WorkflowRunStatus.COMPLETED
        )
        assert recovered_run.status is expected_run_status
        assert recovered_run.workflow_run_id == recovered.workflow_run_id
        assert recovered_run.workflow_node_id == recovered.nodes[0].node_id
        assert recovered.nodes[0].run_id == recovered_run.run_id
        assert recovered.nodes[0].status is expected_node_status
        assert recovered.status is expected_workflow_status
        assert recovered_session.status is SessionStatus.ACTIVE
        assert recovered_session.active_run_ids == ()
        assert recovered_session.active_workflow_run_ids == ()
        node_write = await recovered_store.get_snapshot(
            "workflow_node",
            recovered.nodes[0].entity_id,
        )
        assert node_write is not None
        assert node_write == recovered.nodes[0].model_dump(mode="json")

        events = await recovered_store.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert sum(item.event.type == event_type for item in events) == 1
        assert sum(
            item.event.type == "session.workflow.attached" for item in events
        ) == 1
        assert sum(
            item.event.type == "workflow.node.started" for item in events
        ) == 1
        assert sum(item.event.type == "run.created" for item in events) == 1
        assert sum(
            item.event.type == "session.run.attached" for item in events
        ) == 1
        assert sum(
            item.event.type
            == ("run.failed" if provider_fails else "run.completed")
            for item in events
        ) == 1
        assert sum(
            item.event.type == "session.run.detached" for item in events
        ) == 1
        assert sum(
            item.event.type
            == (
                "workflow.node.failed"
                if provider_fails
                else "workflow.node.completed"
            )
            for item in events
        ) == 1
        assert sum(
            item.event.type
            == ("workflow.failed" if provider_fails else "workflow.completed")
            for item in events
        ) == 1
        assert sum(
            item.event.type == "session.workflow.detached" for item in events
        ) == 1
        assert (
            await recovered_store.list_unresolved_external_operations(
                selected_run_id
            )
            == ()
        )
        terminal_checkpoint = await recovered_store.get_run_checkpoint(
            selected_run_id
        )
        assert terminal_checkpoint is not None
        assert terminal_checkpoint.run_id == selected_run_id
        assert terminal_checkpoint.phase is RunCheckpointPhase.TERMINAL
        assert (
            await recovered_store.list_pending_reconciliation_requests(
                selected_run_id
            )
            == ()
        )

        repeated_handle = await reopened.recovery.recover_workflow(
            workflow.workflow_run_id
        )
        if provider_fails:
            with pytest.raises(AgentSDKError):
                await repeated_handle.result()
        else:
            repeated = await repeated_handle.result()
            assert repeated.workflow_run_id == recovered.workflow_run_id
            assert repeated.status is recovered.status
            assert repeated.nodes == recovered.nodes
            assert repeated.output_text == recovered.output_text
            assert repeated.usage == recovered.usage
        assert await reopened.sessions.get(workflow.session_id) == recovered_session
        assert await reopened.workflows.get(workflow.workflow_run_id) == recovered
        repeated_events = await recovered_store.read_events(
            after_cursor=0,
            session_id=workflow.session_id,
        )
        assert repeated_events == events
        assert completion.calls == 1
        await asyncio.sleep(0)
        assert reopened.workflows._executor._active == {}  # type: ignore[attr-defined]
        assert reopened.recovery._tasks == {}  # type: ignore[attr-defined]
    finally:
        if first is not None:
            await first.close()
        if reopened is not None:
            await reopened.close()
        if backend == "sqlite" and raw_store is not None:
            await raw_store.close()
        if backend == "sqlite" and recovered_store is not None:
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
    await _wait_for_event(
        completion.started,
        diagnostic=lambda: {
            "phase": "sdk_close_provider_owner",
            "calls": completion.calls,
        },
    )
    closing = asyncio.create_task(sdk.close())
    try:
        await asyncio.sleep(0)
        assert closing.done() is False
        completion.release.set()
        await asyncio.wait_for(closing, timeout=_DIAGNOSTIC_TIMEOUT_SECONDS)

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
        await _wait_for_event(
            completion.started,
            diagnostic=lambda: {
                "phase": "same_sdk_provider_owner",
                "calls": completion.calls,
            },
        )
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
            await _wait_for_event(
                release,
                diagnostic=lambda: {"phase": "cancelled_admission_release"},
            )
        return await original_load(workflow_run_id)

    executor._state.load = blocked_load
    caller = asyncio.create_task(
        sdk.recovery.recover_workflow(workflow.workflow_run_id)
    )
    try:
        await _wait_for_event(
            blocked,
            diagnostic=lambda: {"phase": "cancelled_admission_arrival"},
        )
        caller.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller

        executor._state.load = original_load
        attached = await sdk.recovery.recover_workflow(workflow.workflow_run_id)
        await _wait_for_event(
            completion.started,
            diagnostic=lambda: {
                "phase": "cancelled_admission_provider_owner",
                "calls": completion.calls,
            },
        )
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
        await _wait_for_event(
            completion.started,
            diagnostic=lambda: {
                "phase": "same_sdk_live_provider_owner",
                "calls": completion.calls,
            },
        )
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
