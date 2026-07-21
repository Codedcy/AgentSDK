from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any

from agent_sdk.errors import AgentSDKError, ErrorCode
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
    intersect_names,
    intersect_workspaces,
    mutable_model_params,
)
from agent_sdk.storage.base import StateStore
from agent_sdk.subagents.models import ChildResult, ChildUsage, TaskEnvelope
from agent_sdk.tools.models import ToolSpec
from agent_sdk.tools.registry import ToolRegistry


class _ChildTaskFailure(Enum):
    FAILED = "failed"


class SubagentService:
    def __init__(
        self,
        store: StateStore,
        commands: RuntimeCommands,
        engine: RunEngine,
        agents: AgentRegistry,
        *,
        tools: ToolRegistry | None = None,
        tool_schemas: Callable[[], tuple[dict[str, object], ...]] | None = None,
        tool_specs: Callable[[], tuple[ToolSpec, ...]] | None = None,
        policy: PolicyEngine | None = None,
        track_task: Callable[[asyncio.Task[RunResult]], None] | None = None,
        execution_runner: (
            Callable[
                [RunSnapshot, ModelRequest],
                Coroutine[Any, Any, RunResult],
            ]
            | None
        ) = None,
    ) -> None:
        self._store = store
        self._commands = commands
        self._engine = engine
        self._agents = agents
        self._tools = tools
        self._tool_schemas = tool_schemas or (lambda: ())
        self._tool_specs = tool_specs or (lambda: ())
        self._policy = policy or PolicyEngine()
        self._track_task = track_task
        self._execution_runner = execution_runner or self._execute_immediately
        self._tasks: dict[str, asyncio.Task[RunResult]] = {}

    async def spawn(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        parent_run_id: str,
        workflow_run_id: str | None = None,
        workflow_node_id: str | None = None,
        workflow_node_execution: int | None = None,
        agent_revision: str,
        task: TaskEnvelope,
    ) -> RunSnapshot:
        if (workflow_run_id is None) != (workflow_node_id is None):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "workflow run and node identities must be provided together",
                retryable=False,
            )
        if workflow_node_execution is not None and workflow_run_id is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "workflow node execution requires workflow identity",
                retryable=False,
            )
        try:
            resolved_agent = self._agents.resolve(agent_revision)
            agent = AgentSpec.model_validate(
                resolved_agent.model_dump(mode="json")
            )
        except AgentSDKError:
            raise
        except Exception:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "agent revision is invalid",
                retryable=False,
            ) from None
        parent = await self._load_run(parent_run_id, missing_message="parent run not found")
        if parent.session_id != session_id:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "parent run not found",
                retryable=False,
            )
        session = await self._commands.get_session(session_id)
        available_specs = self._available_tool_specs()
        available_names = tuple(spec.name for spec in available_specs)
        ancestor_tools, ancestor_workspaces = await self._ancestor_capabilities(
            parent,
            session_id=session_id,
        )
        for allowlist in (
            *ancestor_tools,
            task.allowed_tools,
            agent.tool_allowlist,
        ):
            if allowlist is not None:
                self._select_tool_specs(allowlist)
        effective_tool_names = intersect_names(
            available_names,
            *ancestor_tools,
            task.allowed_tools,
            agent.tool_allowlist,
        )
        effective_specs = self._select_tool_specs(effective_tool_names)
        workspace_scopes = tuple(
            str(scope)
            for scope in intersect_workspaces(
                tuple(Path(root) for root in session.workspaces),
                *ancestor_workspaces,
                task.workspace_scopes,
                agent.workspace_allowlist,
            )
        )
        rendered = render_task_envelope(task)
        config = self._policy.execution_config()
        descriptor = ExecutionDescriptor.create(
            agent=agent,
            messages=({"role": "user", "content": rendered},),
            tools=tuple(
                ToolCapabilityDescriptor.from_spec(spec)
                for spec in effective_specs
            ),
            workspace_scopes=workspace_scopes,
            policy=ExecutionPolicyDescriptor.create(
                permission_default=config["permission_default"],
                permission_rules=config["permission_rules"],
            ),
        )
        outcome = await self._commands.start_run(
            session_id,
            run_id=run_id,
            agent_revision=agent_revision,
            user_input=rendered,
            parent_run_id=parent_run_id,
            workflow_run_id=workflow_run_id,
            workflow_node_id=workflow_node_id,
            workflow_node_execution=workflow_node_execution,
            task_envelope=task,
            execution_descriptor=descriptor,
        )
        created = outcome.value
        request = ModelRequest(
            model=agent.model,
            messages=({"role": "user", "content": rendered},),
            tools=self._select_tool_schemas(effective_tool_names),
            params=mutable_model_params(agent.model_params),
        )
        execution_awaitable = self._execution_runner(created, request)
        try:
            execution: asyncio.Task[RunResult] = asyncio.create_task(
                execution_awaitable
            )
        except Exception:
            if isinstance(execution_awaitable, Coroutine):
                execution_awaitable.close()
            return created
        self._tasks[created.run_id] = execution
        execution.add_done_callback(partial(self._task_finished, created.run_id))
        if self._track_task is not None:
            self._track_task(execution)
        return created

    async def _ancestor_capabilities(
        self,
        parent: RunSnapshot,
        *,
        session_id: str,
    ) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...]]:
        tool_scopes: list[tuple[str, ...]] = []
        workspace_scopes: list[tuple[str, ...]] = []
        current = parent
        visited: set[str] = set()
        while True:
            if current.session_id != session_id or current.run_id in visited:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "stored child relation is invalid",
                    retryable=False,
                )
            visited.add(current.run_id)
            descriptor = current.execution_descriptor
            if descriptor is not None:
                tool_scopes.append(
                    tuple(capability.spec.name for capability in descriptor.tools)
                )
                if descriptor.workspace_scopes is not None:
                    workspace_scopes.append(descriptor.workspace_scopes)
            if current.parent_run_id is None:
                break
            current = await self._load_run(
                current.parent_run_id,
                missing_message="ancestor run not found",
            )
        return tuple(reversed(tool_scopes)), tuple(reversed(workspace_scopes))

    async def _execute_immediately(
        self,
        child: RunSnapshot,
        request: ModelRequest,
    ) -> RunResult:
        return await self._engine.execute(child.run_id, request)

    def _available_tool_specs(self) -> tuple[ToolSpec, ...]:
        if self._tools is not None:
            return self._tools.list()
        return self._tool_specs()

    def _select_tool_specs(self, names: tuple[str, ...]) -> tuple[ToolSpec, ...]:
        if self._tools is not None:
            return self._tools.select(names).list()
        by_name = {spec.name: spec for spec in self._tool_specs()}
        missing = set(names).difference(by_name)
        if missing:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "tool not found",
                retryable=False,
            )
        return tuple(by_name[name] for name in sorted(set(names)))

    def _select_tool_schemas(
        self,
        names: tuple[str, ...],
    ) -> tuple[dict[str, object], ...]:
        if self._tools is not None:
            return self._tools.select(names).schemas()
        selected = set(names)
        return tuple(
            schema
            for schema in self._tool_schemas()
            if _schema_tool_name(schema) in selected
        )

    async def await_result(self, run_id: str) -> ChildResult:
        task = self._tasks.get(run_id)
        if task is not None:
            outcome = await _settle_child_task(task)
            task = None
            snapshot = await self._load_run(run_id, missing_message="child run not found")
            if outcome is _ChildTaskFailure.FAILED:
                raise _child_failure(snapshot)
            return self._child_result(snapshot, outcome)

        snapshot = await self._load_run(run_id, missing_message="child run not found")
        if snapshot.status is RunStatus.FAILED:
            raise _child_failure(snapshot)
        if snapshot.status is not RunStatus.COMPLETED:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "child run is not completed",
                retryable=False,
            )
        if snapshot.usage is None or snapshot.output_text is None:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "child run terminal state is invalid",
                retryable=False,
            )
        result = RunResult(
            run_id=run_id,
            output_text=snapshot.output_text,
            usage=snapshot.usage,
        )
        return self._child_result(snapshot, result)

    def task_for(self, run_id: str) -> asyncio.Task[RunResult] | None:
        return self._tasks.get(run_id)

    async def _load_run(self, run_id: str, *, missing_message: str) -> RunSnapshot:
        try:
            data = await self._store.get_snapshot("run", run_id)
            if data is None:
                raise AgentSDKError(ErrorCode.NOT_FOUND, missing_message, retryable=False)
            return RunSnapshot.model_validate(data)
        except AgentSDKError:
            raise
        except Exception:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to load child run",
                retryable=False,
            ) from None

    @staticmethod
    def _child_result(snapshot: RunSnapshot, result: RunResult) -> ChildResult:
        envelope = snapshot.task_envelope
        if envelope is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not a child",
                retryable=False,
            )
        return ChildResult(
            run_id=result.run_id,
            status="completed",
            output_text=result.output_text,
            evidence_refs=envelope.evidence_refs,
            usage=ChildUsage.model_validate(result.usage.model_dump()),
        )

    def _task_finished(self, run_id: str, task: asyncio.Task[RunResult]) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)
        if not task.cancelled():
            task.exception()


def render_task_envelope(task: TaskEnvelope) -> str:
    return "Child task envelope:\n" + json.dumps(
        task.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _schema_tool_name(schema: dict[str, object]) -> str | None:
    function = schema.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) else None


async def _settle_child_task(
    task: asyncio.Task[RunResult],
) -> RunResult | _ChildTaskFailure:
    try:
        return await task
    except Exception:
        return _ChildTaskFailure.FAILED


def _child_failure(snapshot: RunSnapshot) -> AgentSDKError:
    failure = snapshot.error
    if failure is None:
        return AgentSDKError(
            ErrorCode.INTERNAL,
            "child run failed",
            retryable=False,
        )
    try:
        code = ErrorCode(failure.code)
    except ValueError:
        code = ErrorCode.INTERNAL
    return AgentSDKError(code, failure.message, retryable=failure.retryable)
