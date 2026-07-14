from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import Any

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.ids import new_id
from agent_sdk.models.litellm_gateway import ModelRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
)
from agent_sdk.runtime.models import (
    AgentSpec,
    RunResult,
    RunSnapshot,
    RunStatus,
    TokenUsage,
    mutable_model_params,
)
from agent_sdk.storage.base import StateStore
from agent_sdk.subagents.models import TaskEnvelope
from agent_sdk.subagents.service import SubagentService, render_task_envelope
from agent_sdk.tools.models import ToolSpec
from agent_sdk.workflow.handles import WorkflowHandle
from agent_sdk.workflow.models import (
    AgentNode,
    WorkflowFailure,
    WorkflowIR,
    WorkflowNodeStatus,
    WorkflowResult,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from agent_sdk.workflow.state import WorkflowState


@dataclass(frozen=True)
class _RunFailure:
    code: ErrorCode
    message: str


class _RunLoadFailure(Enum):
    MISSING = "missing"
    INVALID = "invalid"


class _IRValidationFailure(Enum):
    INVALID = "invalid"


class WorkflowExecutor:
    def __init__(
        self,
        store: StateStore,
        commands: RuntimeCommands,
        engine: RunEngine,
        agents: AgentRegistry,
        *,
        tool_schemas: Callable[[], tuple[dict[str, Any], ...]] | None = None,
        tool_specs: Callable[[], tuple[ToolSpec, ...]] | None = None,
        policy: PolicyEngine | None = None,
        track_run_task: Callable[[asyncio.Task[RunResult]], None] | None = None,
        track_workflow_task: Callable[[asyncio.Task[WorkflowResult]], None] | None = None,
    ) -> None:
        self._store = store
        self._commands = commands
        self._engine = engine
        self._agents = agents
        self._state = WorkflowState(store)
        self._tool_schemas = tool_schemas or (lambda: ())
        self._tool_specs = tool_specs or (lambda: ())
        self._policy = policy or PolicyEngine()
        self._subagents = SubagentService(
            store,
            commands,
            engine,
            agents,
            tool_schemas=self._tool_schemas,
            tool_specs=self._tool_specs,
            policy=self._policy,
            track_task=track_run_task,
        )
        self._track_workflow_task = track_workflow_task
        self._active: dict[str, asyncio.Task[WorkflowResult]] = {}

    async def start(self, session_id: str, workflow: WorkflowIR) -> WorkflowHandle:
        validated = _validated_ir(workflow)
        self._validate_agents(validated)
        snapshot = await self._state.create(session_id, validated)
        return self._start_task(snapshot.workflow_run_id)

    async def resume(
        self,
        workflow_run_id: str,
        *,
        expected_workflow: WorkflowIR | None = None,
    ) -> WorkflowHandle:
        snapshot = await self._state.load(workflow_run_id)
        if expected_workflow is not None:
            try:
                expected = _validated_ir(expected_workflow)
            except AgentSDKError:
                raise AgentSDKError(
                    ErrorCode.CONFLICT,
                    "workflow definition does not match persisted run",
                    retryable=False,
                ) from None
            if expected.definition_hash != snapshot.workflow.definition_hash:
                raise AgentSDKError(
                    ErrorCode.CONFLICT,
                    "workflow definition does not match persisted run",
                    retryable=False,
                )
        if snapshot.status is WorkflowRunStatus.RUNNING:
            self._validate_agents(snapshot.workflow)
        active = self._active.get(workflow_run_id)
        if active is not None and not active.done():
            return WorkflowHandle(workflow_run_id, self._store, active)
        return self._start_task(workflow_run_id)

    async def get(self, workflow_run_id: str) -> WorkflowRunSnapshot:
        return await self._state.load(workflow_run_id)

    def _start_task(self, workflow_run_id: str) -> WorkflowHandle:
        task = asyncio.create_task(self._drive(workflow_run_id))
        self._active[workflow_run_id] = task
        task.add_done_callback(partial(self._task_finished, workflow_run_id))
        if self._track_workflow_task is not None:
            self._track_workflow_task(task)
        return WorkflowHandle(workflow_run_id, self._store, task)

    def _task_finished(
        self,
        workflow_run_id: str,
        task: asyncio.Task[WorkflowResult],
    ) -> None:
        if self._active.get(workflow_run_id) is task:
            self._active.pop(workflow_run_id, None)
        if not task.cancelled():
            task.exception()

    def _validate_agents(self, workflow: WorkflowIR) -> None:
        for node in workflow.nodes:
            self._agents.resolve(node.agent_revision)

    async def _drive(self, workflow_run_id: str) -> WorkflowResult:
        while True:
            snapshot = await self._state.load(workflow_run_id)
            if snapshot.status is WorkflowRunStatus.COMPLETED:
                return _result(snapshot)
            if snapshot.status is WorkflowRunStatus.FAILED:
                raise _failure_error(snapshot.error)

            index = _next_node_index(snapshot)
            if index is None:
                completed = await self._state.complete_workflow(snapshot)
                return _result(completed)
            node_snapshot = snapshot.nodes[index]
            node = snapshot.workflow.nodes[index]
            if node_snapshot.status is WorkflowNodeStatus.FAILED:
                failed = await self._state.fail_workflow(
                    snapshot,
                    node_snapshot.error or _generic_failure("workflow node failed"),
                )
                raise _failure_error(failed.error)
            if node_snapshot.status is WorkflowNodeStatus.PENDING:
                snapshot = await self._state.start_node(
                    snapshot,
                    index,
                    new_id("run"),
                )
                node_snapshot = snapshot.nodes[index]

            run_id = node_snapshot.run_id
            if run_id is None:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "workflow node has no selected run",
                    retryable=False,
                )
            run = await _load_run(self._store, run_id)
            if run is _RunLoadFailure.INVALID:
                failure = _generic_failure("related run state is invalid")
                await self._persist_failure(snapshot, index, failure)
                raise _failure_error(failure)
            if isinstance(run, RunSnapshot) and not _related_run_matches(
                snapshot, index, node, run
            ):
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "related run does not match workflow node",
                    retryable=False,
                )
            if run is _RunLoadFailure.MISSING:
                result = await self._create_and_execute(snapshot, index, node, run_id)
            elif run.status is RunStatus.CREATED:
                result = await self._execute_created(node, run)
            elif run.status is RunStatus.COMPLETED:
                result = _run_result(run)
            elif run.status is RunStatus.FAILED:
                failure = _generic_failure("related run failed")
                await self._persist_failure(snapshot, index, failure)
                raise _failure_error(failure)
            else:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "workflow has an interrupted in-flight run; replay is disabled",
                    retryable=False,
                )

            if isinstance(result, _RunFailure):
                failure = WorkflowFailure(
                    code=result.code.value,
                    message=result.message,
                    retryable=False,
                )
                await self._persist_failure(snapshot, index, failure)
                raise _failure_error(failure)
            await self._state.complete_node(snapshot, index, result)

    async def _create_and_execute(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        node: AgentNode,
        run_id: str,
    ) -> RunResult | _RunFailure:
        agent = self._agents.resolve(node.agent_revision)
        if node.run_as == "child":
            parent_run_id = _parent_run_id(snapshot, index)
            task_envelope = _task_envelope(node)
            outcome = await _spawn_child(
                self._subagents,
                session_id=snapshot.session_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
                workflow_run_id=snapshot.workflow_run_id,
                workflow_node_id=node.id,
                agent_revision=node.agent_revision,
                task=task_envelope,
            )
            if isinstance(outcome, _RunFailure):
                return outcome
            return RunResult(
                run_id=outcome.run_id,
                output_text=outcome.output_text,
                usage=TokenUsage.model_validate(outcome.usage.model_dump()),
            )

        created = await _create_run(
            self._commands,
            session_id=snapshot.session_id,
            run_id=run_id,
            agent_revision=node.agent_revision,
            user_input=node.input,
            workflow_run_id=snapshot.workflow_run_id,
            workflow_node_id=node.id,
            execution_descriptor=self._execution_descriptor(agent, node.input),
        )
        if isinstance(created, _RunFailure):
            return created
        return await self._execute_created(node, created, agent=agent)

    def _execution_descriptor(
        self,
        agent: AgentSpec,
        user_input: str,
    ) -> ExecutionDescriptor:
        config = self._policy.execution_config()
        return ExecutionDescriptor.create(
            agent=agent,
            messages=({"role": "user", "content": user_input},),
            tools=tuple(
                ToolCapabilityDescriptor.from_spec(spec)
                for spec in self._tool_specs()
            ),
            policy=ExecutionPolicyDescriptor.create(
                permission_default=config["permission_default"]
            ),
        )

    async def _execute_created(
        self,
        node: AgentNode,
        run: RunSnapshot,
        *,
        agent: AgentSpec | None = None,
    ) -> RunResult | _RunFailure:
        resolved = agent or self._agents.resolve(node.agent_revision)
        if node.run_as == "child":
            envelope = run.task_envelope or _task_envelope(node)
            user_input = render_task_envelope(envelope)
        else:
            user_input = node.input
        request = ModelRequest(
            model=resolved.model,
            messages=({"role": "user", "content": user_input},),
            tools=self._tool_schemas(),
            params=mutable_model_params(resolved.model_params),
        )
        return await _execute_run(self._engine, run.run_id, request)

    async def _persist_failure(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        failure: WorkflowFailure,
    ) -> None:
        node_failed = await self._state.fail_node(snapshot, index, failure)
        await self._state.fail_workflow(node_failed, failure)


def _validated_ir(workflow: WorkflowIR) -> WorkflowIR:
    result = _validate_ir(workflow)
    if result is _IRValidationFailure.INVALID:
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "workflow IR is invalid",
            retryable=False,
        )
    return result


def _validate_ir(workflow: WorkflowIR) -> WorkflowIR | _IRValidationFailure:
    try:
        return WorkflowIR.model_validate(workflow.model_dump(mode="json"))
    except Exception:
        return _IRValidationFailure.INVALID


async def _load_run(
    store: StateStore,
    run_id: str,
) -> RunSnapshot | _RunLoadFailure:
    try:
        data = await store.get_snapshot("run", run_id)
        if data is None:
            return _RunLoadFailure.MISSING
        return RunSnapshot.model_validate(data)
    except Exception:
        return _RunLoadFailure.INVALID


async def _execute_run(
    engine: RunEngine,
    run_id: str,
    request: ModelRequest,
) -> RunResult | _RunFailure:
    try:
        return await engine.execute(run_id, request)
    except AgentSDKError as failure:
        return _RunFailure(failure.code, failure.message)
    except Exception:
        return _RunFailure(ErrorCode.INTERNAL, "run execution failed")


async def _create_run(
    commands: RuntimeCommands,
    **values: Any,
) -> RunSnapshot | _RunFailure:
    try:
        return (await commands.start_run(**values)).value
    except AgentSDKError as failure:
        return _RunFailure(failure.code, failure.message)
    except Exception:
        return _RunFailure(ErrorCode.INTERNAL, "run creation failed")


async def _spawn_child(
    service: SubagentService,
    **values: Any,
) -> Any:
    try:
        child = await service.spawn(**values)
        return await service.await_result(child.run_id)
    except AgentSDKError as failure:
        return _RunFailure(failure.code, failure.message)
    except Exception:
        return _RunFailure(ErrorCode.INTERNAL, "child execution failed")


def _next_node_index(snapshot: WorkflowRunSnapshot) -> int | None:
    for index, node in enumerate(snapshot.nodes):
        if node.status is not WorkflowNodeStatus.COMPLETED:
            return index
    return None


def _parent_run_id(snapshot: WorkflowRunSnapshot, index: int) -> str:
    if index == 0:
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "root workflow node cannot be a child",
            retryable=False,
        )
    parent = snapshot.nodes[index - 1].run_id
    if parent is None:
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "child workflow node has no parent run",
            retryable=False,
        )
    return parent


def _task_envelope(node: AgentNode) -> TaskEnvelope:
    return TaskEnvelope(
        objective=node.input,
        success_criteria=node.success_criteria,
        evidence_refs=node.evidence_refs,
        allowed_tools=node.allowed_tools,
        workspace_scopes=node.workspace_scopes,
    )


def _related_run_matches(
    workflow: WorkflowRunSnapshot,
    index: int,
    node: AgentNode,
    run: RunSnapshot,
) -> bool:
    if (
        run.run_id != workflow.nodes[index].run_id
        or run.session_id != workflow.session_id
        or run.workflow_run_id != workflow.workflow_run_id
        or run.workflow_node_id != node.id
        or run.agent_revision != node.agent_revision
    ):
        return False
    if node.run_as == "parent":
        return (
            run.parent_run_id is None
            and run.task_envelope is None
            and run.user_input == node.input
        )
    if index == 0:
        return False
    expected_parent = workflow.nodes[index - 1].run_id
    expected_envelope = _task_envelope(node)
    return (
        expected_parent is not None
        and run.parent_run_id == expected_parent
        and run.task_envelope == expected_envelope
        and run.user_input == render_task_envelope(expected_envelope)
    )


def _run_result(run: RunSnapshot) -> RunResult:
    if run.output_text is None or run.usage is None:
        raise AgentSDKError(
            ErrorCode.INTERNAL,
            "terminal run result is invalid",
            retryable=False,
        )
    return RunResult(run_id=run.run_id, output_text=run.output_text, usage=run.usage)


def _result(snapshot: WorkflowRunSnapshot) -> WorkflowResult:
    if snapshot.output_text is None or snapshot.usage is None:
        raise AgentSDKError(
            ErrorCode.INTERNAL,
            "terminal workflow result is invalid",
            retryable=False,
        )
    return WorkflowResult(
        workflow_run_id=snapshot.workflow_run_id,
        status=snapshot.status,
        nodes=snapshot.nodes,
        output_text=snapshot.output_text,
        usage=snapshot.usage,
    )


def _generic_failure(message: str) -> WorkflowFailure:
    return WorkflowFailure(code=ErrorCode.INTERNAL.value, message=message, retryable=False)


def _failure_error(failure: WorkflowFailure | None) -> AgentSDKError:
    if failure is None:
        return AgentSDKError(
            ErrorCode.INTERNAL,
            "workflow failed",
            retryable=False,
        )
    try:
        code = ErrorCode(failure.code)
    except ValueError:
        code = ErrorCode.INTERNAL
    return AgentSDKError(code, failure.message, retryable=failure.retryable)
