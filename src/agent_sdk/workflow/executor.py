from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import Any, NoReturn

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
    WorkflowAgentDescriptor,
    WorkflowExecutionDescriptor,
)
from agent_sdk.runtime.idempotency import _idempotency_public_error
from agent_sdk.runtime.handles import RunHandle
from agent_sdk.runtime.models import (
    AgentSpec,
    RunResult,
    RunSnapshot,
    RunStatus,
    TokenUsage,
    mutable_model_params,
)
from agent_sdk.runtime.session_lifecycle import exact_session_precondition, load_session
from agent_sdk.storage.base import (
    RunRecoveryEvidencePrecondition,
    SnapshotPrecondition,
    StateStore,
)
from agent_sdk.storage.idempotency import IdempotencyError
from agent_sdk.subagents.models import TaskEnvelope
from agent_sdk.subagents.coordinator import ChildCoordinator
from agent_sdk.subagents.service import render_task_envelope
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
from agent_sdk.workflow.program import (
    CompleteWorkflow,
    ExecuteAgent,
    FailWorkflow,
    PersistControl,
    next_action,
)
from agent_sdk.workflow.state import WorkflowState


@dataclass(frozen=True)
class _RunFailure:
    code: ErrorCode
    message: str


class _RunLoadFailure(Enum):
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True)
class _ParentExecutionIdentity:
    node_index: int
    run_id: str
    node_execution: int | None


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
        child_coordinator: ChildCoordinator | None = None,
    ) -> None:
        self._store = store
        self._commands = commands
        self._engine = engine
        self._agents = agents
        self._state = WorkflowState(store)
        self._tool_schemas = tool_schemas or (lambda: ())
        self._tool_specs = tool_specs or (lambda: ())
        self._policy = policy or PolicyEngine()
        self._children = child_coordinator or ChildCoordinator(
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
        self._start_lock = asyncio.Lock()
        self._recover_run: Callable[[str], Awaitable[RunHandle]] | None = None
        self._certify_terminal_run: (
            Callable[
                [str],
                Awaitable[tuple[RunSnapshot, RunRecoveryEvidencePrecondition]],
            ]
            | None
        ) = None

    def _set_run_recovery(
        self,
        recover_run: Callable[[str], Awaitable[RunHandle]],
        certify_terminal_run: Callable[
            [str],
            Awaitable[tuple[RunSnapshot, RunRecoveryEvidencePrecondition]],
        ],
    ) -> None:
        self._recover_run = recover_run
        self._certify_terminal_run = certify_terminal_run

    async def start(
        self,
        session_id: str,
        workflow: WorkflowIR,
        *,
        idempotency_key: str | None = None,
    ) -> WorkflowHandle:
        validated = _validated_ir(workflow)
        descriptor = self._workflow_execution_descriptor(validated)
        coordinator = asyncio.create_task(
            self._coordinate_start(
                session_id,
                validated,
                descriptor,
                idempotency_key=idempotency_key,
            )
        )
        try:
            return await self._await_start_coordinator(coordinator)
        finally:
            del idempotency_key
            del validated
            del descriptor
            del coordinator

    async def _coordinate_start(
        self,
        session_id: str,
        workflow: WorkflowIR,
        execution_descriptor: WorkflowExecutionDescriptor,
        *,
        idempotency_key: str | None,
    ) -> WorkflowHandle:
        try:
            async with self._start_lock:
                public_error: AgentSDKError | None = None
                try:
                    outcome = await self._state.create(
                        session_id,
                        workflow,
                        execution_descriptor=execution_descriptor,
                        idempotency_key=idempotency_key,
                    )
                except IdempotencyError as error:
                    public_error = _idempotency_public_error(error)
                if public_error is not None:
                    raise public_error from None
                if outcome.replayed:
                    active = self._active.get(outcome.value.workflow_run_id)
                    if active is not None and not active.done():
                        return WorkflowHandle(
                            outcome.value.workflow_run_id,
                            self._store,
                            active,
                        )
                    return WorkflowHandle(
                        outcome.value.workflow_run_id,
                        self._store,
                        None,
                    )
                return self._start_task(outcome.value.workflow_run_id)
        finally:
            del idempotency_key
            del execution_descriptor
            del workflow

    @staticmethod
    async def _await_start_coordinator(
        coordinator: asyncio.Task[WorkflowHandle],
    ) -> WorkflowHandle:
        cancellation: asyncio.CancelledError | None = None
        try:
            return await asyncio.shield(coordinator)
        except asyncio.CancelledError as error:
            cancellation = error

        while not coordinator.done():
            try:
                await asyncio.shield(coordinator)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if coordinator.done() and not coordinator.cancelled():
            coordinator.exception()
        assert cancellation is not None
        raise cancellation from None

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
        active = self._active.get(workflow_run_id)
        if active is not None and not active.done():
            return WorkflowHandle(workflow_run_id, self._store, active)
        if snapshot.status in {
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
        }:
            return WorkflowHandle(workflow_run_id, self._store, None)
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "recovery required",
            retryable=True,
        ) from None

    async def _recover(
        self,
        workflow_run_id: str,
        recover_run: Callable[[str], Awaitable[RunHandle]],
    ) -> WorkflowHandle:
        coordinator = asyncio.create_task(
            self._coordinate_recovery(workflow_run_id, recover_run)
        )
        try:
            return await self._await_start_coordinator(coordinator)
        finally:
            del coordinator
            del recover_run

    async def _coordinate_recovery(
        self,
        workflow_run_id: str,
        recover_run: Callable[[str], Awaitable[RunHandle]],
    ) -> WorkflowHandle:
        async with self._start_lock:
            snapshot = await self._state.load(workflow_run_id)
            if snapshot.status in {
                WorkflowRunStatus.COMPLETED,
                WorkflowRunStatus.FAILED,
            }:
                return WorkflowHandle(workflow_run_id, self._store, None)
            active = self._active.get(workflow_run_id)
            if active is not None and not active.done():
                return WorkflowHandle(workflow_run_id, self._store, active)
            await self._validate_recovery_preflight(snapshot)
            return self._start_recovery_task(workflow_run_id, recover_run)

    async def get(self, workflow_run_id: str) -> WorkflowRunSnapshot:
        return await self._state.load(workflow_run_id)

    def _start_task(self, workflow_run_id: str) -> WorkflowHandle:
        task = asyncio.create_task(self._drive(workflow_run_id))
        self._active[workflow_run_id] = task
        task.add_done_callback(partial(self._task_finished, workflow_run_id))
        if self._track_workflow_task is not None:
            self._track_workflow_task(task)
        return WorkflowHandle(workflow_run_id, self._store, task)

    def _start_recovery_task(
        self,
        workflow_run_id: str,
        recover_run: Callable[[str], Awaitable[RunHandle]],
    ) -> WorkflowHandle:
        task = asyncio.create_task(
            self._drive_recovery_public(workflow_run_id, recover_run)
        )
        self._active[workflow_run_id] = task
        task.add_done_callback(partial(self._task_finished, workflow_run_id))
        if self._track_workflow_task is not None:
            self._track_workflow_task(task)
        return WorkflowHandle(workflow_run_id, self._store, task)

    def _validate_recovery_descriptor(
        self,
        snapshot: WorkflowRunSnapshot,
    ) -> None:
        if (
            snapshot.execution_compatibility != "current"
            or snapshot.execution_descriptor is None
        ):
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "recovery required",
                retryable=True,
            ) from None
        capability_error = False
        live: WorkflowExecutionDescriptor | None = None
        try:
            live = self._workflow_execution_descriptor(snapshot.workflow)
        except Exception:
            capability_error = True
        if capability_error or live != snapshot.execution_descriptor:
            live = None
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "recovery capabilities unavailable",
                retryable=False,
            ) from None

    async def _validate_recovery_preflight(
        self,
        snapshot: WorkflowRunSnapshot,
    ) -> None:
        self._validate_recovery_descriptor(snapshot)
        ownership_error = False
        session = None
        try:
            session = await load_session(self._store, snapshot.session_id)
        except Exception:
            ownership_error = True
        if (
            ownership_error
            or session is None
            or snapshot.workflow_run_id not in session.active_workflow_run_ids
        ):
            session = None
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "workflow recovery ownership unavailable",
                retryable=False,
            ) from None
        self._validate_recovery_descriptor(snapshot)

    async def _drive_recovery_public(
        self,
        workflow_run_id: str,
        recover_run: Callable[[str], Awaitable[RunHandle]],
    ) -> WorkflowResult:
        public_error: tuple[ErrorCode, str, bool] | None = None
        try:
            return await self._drive_recovery(workflow_run_id, recover_run)
        except asyncio.CancelledError:
            raise
        except AgentSDKError as error:
            public_error = (error.code, error.message, error.retryable)
        except Exception:
            public_error = (
                ErrorCode.INTERNAL,
                "failed to recover workflow",
                False,
            )
        del recover_run
        assert public_error is not None
        raise AgentSDKError(
            public_error[0],
            public_error[1],
            retryable=public_error[2],
        ) from None

    async def _drive_recovery(
        self,
        workflow_run_id: str,
        recover_run: Callable[[str], Awaitable[RunHandle]],
    ) -> WorkflowResult:
        initial = await self._state.load(workflow_run_id)
        if (
            initial.workflow.schema_version == 2
            and not _is_linear_program(initial.workflow)
        ):
            return await self._drive_v2(
                workflow_run_id,
                recover_run=recover_run,
            )
        while True:
            snapshot = await self._state.load(workflow_run_id)
            if snapshot.status is WorkflowRunStatus.COMPLETED:
                return _result(snapshot)
            if snapshot.status is WorkflowRunStatus.FAILED:
                raise _failure_error(snapshot.error)
            await self._validate_recovery_preflight(snapshot)

            index = _next_node_index(snapshot)
            if index is None:
                try:
                    self._validate_recovery_descriptor(snapshot)
                    completed = await self._state.complete_workflow(snapshot)
                except AgentSDKError as error:
                    if error.code is ErrorCode.CONFLICT:
                        continue
                    raise
                return _result(completed)

            node_snapshot = snapshot.nodes[index]
            node = snapshot.workflow.nodes[index]
            if node_snapshot.status is WorkflowNodeStatus.FAILED:
                failure = node_snapshot.error or _generic_failure(
                    "workflow node failed"
                )
                try:
                    self._validate_recovery_descriptor(snapshot)
                    failed = await self._state.fail_workflow(snapshot, failure)
                except AgentSDKError as error:
                    if error.code is ErrorCode.CONFLICT:
                        continue
                    raise
                raise _failure_error(failed.error)

            if node_snapshot.status is WorkflowNodeStatus.PENDING:
                try:
                    self._validate_recovery_descriptor(snapshot)
                    snapshot = await self._state.start_node(
                        snapshot,
                        index,
                        new_id("run"),
                    )
                except AgentSDKError as error:
                    if error.code is ErrorCode.CONFLICT:
                        continue
                    raise
                node_snapshot = snapshot.nodes[index]

            run_id = node_snapshot.run_id
            if run_id is None:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "workflow node has no selected run",
                    retryable=False,
                ) from None

            expected_descriptor = self._node_execution_descriptor(node)
            run = await self._ensure_selected_run(
                snapshot,
                index,
                node,
                run_id,
                expected_descriptor,
            )
            expected_descriptor = self._selected_execution_descriptor(
                node,
                run,
                expected_descriptor,
            )
            if not _related_run_matches(
                snapshot,
                index,
                node,
                run,
                expected_descriptor=expected_descriptor,
            ):
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "related run does not match workflow node",
                    retryable=False,
                ) from None

            if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                try:
                    if node.run_as == "child":
                        await self._children.await_result(run_id)
                    else:
                        await (await recover_run(run_id)).result()
                except AgentSDKError as error:
                    run = await self._load_selected_run(run_id)
                    if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                        if (
                            run.status is RunStatus.WAITING_RECONCILIATION
                            or error.code is ErrorCode.CONFLICT
                        ):
                            raise AgentSDKError(
                                ErrorCode.CONFLICT,
                                "recovery required",
                                retryable=True,
                            ) from None
                        raise
                run = await self._load_selected_run(run_id)
                if not _related_run_matches(
                    snapshot,
                    index,
                    node,
                    run,
                    expected_descriptor=expected_descriptor,
                ):
                    raise AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "related run does not match workflow node",
                        retryable=False,
                    ) from None

            if run.status is RunStatus.COMPLETED:
                try:
                    run, projection_preconditions, evidence_precondition = (
                        await self._certified_terminal_selected_run(
                            snapshot,
                            index,
                            node,
                            run_id,
                            expected_descriptor,
                        )
                    )
                    self._validate_recovery_descriptor(snapshot)
                    await self._state.complete_node(
                        snapshot,
                        index,
                        _run_result(run),
                        related_preconditions=projection_preconditions,
                        recovery_evidence_precondition=evidence_precondition,
                    )
                except AgentSDKError as error:
                    if error.code is ErrorCode.CONFLICT:
                        continue
                    raise
                continue

            if run.status is RunStatus.FAILED:
                try:
                    run, projection_preconditions, evidence_precondition = (
                        await self._certified_terminal_selected_run(
                            snapshot,
                            index,
                            node,
                            run_id,
                            expected_descriptor,
                        )
                    )
                    failure = _run_workflow_failure(run)
                    self._validate_recovery_descriptor(snapshot)
                    node_failed = await self._state.fail_node(
                        snapshot,
                        index,
                        failure,
                        related_preconditions=projection_preconditions,
                        recovery_evidence_precondition=evidence_precondition,
                    )
                    self._validate_recovery_descriptor(node_failed)
                    failed = await self._state.fail_workflow(node_failed, failure)
                except AgentSDKError as error:
                    if error.code is ErrorCode.CONFLICT:
                        continue
                    raise
                raise _failure_error(failed.error)

            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "recovery required",
                retryable=True,
            ) from None

    def _node_execution_descriptor(self, node: AgentNode) -> ExecutionDescriptor:
        agent = self._agents.resolve(node.agent_revision)
        message = (
            render_task_envelope(_task_envelope(node))
            if node.run_as == "child"
            else node.input
        )
        return self._execution_descriptor(agent, message)

    @staticmethod
    def _selected_execution_descriptor(
        node: AgentNode,
        run: RunSnapshot,
        default: ExecutionDescriptor,
    ) -> ExecutionDescriptor:
        if node.run_as != "child":
            return default
        descriptor = run.execution_descriptor
        if descriptor is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "child run execution descriptor is missing",
                retryable=False,
            )
        return descriptor

    async def _ensure_selected_run(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        node: AgentNode,
        run_id: str,
        execution_descriptor: ExecutionDescriptor,
        *,
        use_idempotency: bool = True,
    ) -> RunSnapshot:
        loaded = await _load_run(self._store, run_id)
        if loaded is _RunLoadFailure.INVALID:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "related run state is invalid",
                retryable=False,
            ) from None
        if loaded is _RunLoadFailure.MISSING:
            await self._validate_recovery_preflight(snapshot)
            parent = await self._validated_child_parent(snapshot, index, node)
            self._validate_recovery_descriptor(snapshot)
            envelope = _task_envelope(node) if node.run_as == "child" else None
            parent_run_id = None if parent is None else parent.run_id
            user_input = (
                render_task_envelope(envelope)
                if envelope is not None
                else node.input
            )
            creation_error: tuple[ErrorCode, str, bool] | None = None
            try:
                if envelope is not None:
                    assert parent_run_id is not None
                    await self._children.spawn(
                        parent_run_id=parent_run_id,
                        agent_revision=node.agent_revision,
                        task=envelope,
                        session_id=snapshot.session_id,
                        run_id=run_id,
                        workflow_run_id=snapshot.workflow_run_id,
                        workflow_node_id=node.id,
                        workflow_node_execution=(
                            snapshot.nodes[index].execution_count
                            if snapshot.workflow.schema_version == 2
                            else None
                        ),
                    )
                else:
                    await self._commands.start_run(
                        snapshot.session_id,
                        run_id=run_id,
                        agent_revision=node.agent_revision,
                        user_input=user_input,
                        parent_run_id=parent_run_id,
                        workflow_run_id=snapshot.workflow_run_id,
                        workflow_node_id=node.id,
                        workflow_node_execution=(
                            snapshot.nodes[index].execution_count
                            if snapshot.workflow.schema_version == 2
                            else None
                        ),
                        task_envelope=envelope,
                        execution_descriptor=execution_descriptor,
                        idempotency_key=(
                            (
                                "workflow-node:"
                                f"{snapshot.workflow_run_id}:{node.id}:"
                                f"{snapshot.nodes[index].execution_count}"
                                if snapshot.workflow.schema_version == 2
                                else (
                                    "workflow-node:"
                                    f"{snapshot.workflow_run_id}:{node.id}"
                                )
                            )
                            if use_idempotency
                            else None
                        ),
                        related_preconditions=(
                            ()
                            if parent is None
                            else (_exact_run_precondition(parent),)
                        ),
                    )
            except AgentSDKError as error:
                creation_error = (error.code, error.message, error.retryable)
            except Exception:
                creation_error = (
                    ErrorCode.INTERNAL,
                    "failed to start run",
                    False,
                )
            if creation_error is not None:
                authoritative = await _load_run(self._store, run_id)
                if not (
                    isinstance(authoritative, RunSnapshot)
                    and _related_run_matches(
                        snapshot,
                        index,
                        node,
                        authoritative,
                        expected_descriptor=execution_descriptor,
                    )
                ):
                    raise AgentSDKError(
                        creation_error[0],
                        creation_error[1],
                        retryable=creation_error[2],
                    ) from None
        selected = await self._load_selected_run(run_id)
        await self._validated_child_parent(snapshot, index, node)
        self._validate_recovery_descriptor(snapshot)
        return selected

    async def _validated_child_parent(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        node: AgentNode,
    ) -> RunSnapshot | None:
        if node.run_as != "child":
            return None
        identity = _parent_execution_identity(snapshot, index)
        previous_node = snapshot.workflow.nodes[identity.node_index]
        previous_projection = snapshot.nodes[identity.node_index]
        parent = await _load_run(self._store, identity.run_id)
        if not isinstance(parent, RunSnapshot) or parent.status is not RunStatus.COMPLETED:
            raise _invalid_parent_run()
        expected_descriptor = self._selected_execution_descriptor(
            previous_node,
            parent,
            self._node_execution_descriptor(previous_node),
        )
        if not _historical_parent_run_matches(
                snapshot,
                identity,
                previous_node,
                parent,
                expected_descriptor=expected_descriptor,
            ):
            raise _invalid_parent_run()
        if (
            previous_projection.run_id == identity.run_id
            and (
                previous_projection.status is not WorkflowNodeStatus.COMPLETED
                or parent.output_text != previous_projection.output_text
                or parent.usage != previous_projection.usage
            )
        ):
            raise _invalid_parent_run()
        try:
            _run_result(parent)
        except AgentSDKError:
            raise _invalid_parent_run() from None
        return parent

    async def _load_selected_run(self, run_id: str) -> RunSnapshot:
        loaded = await _load_run(self._store, run_id)
        if loaded is _RunLoadFailure.MISSING:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "recovery required",
                retryable=True,
            ) from None
        if loaded is _RunLoadFailure.INVALID:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "related run state is invalid",
                retryable=False,
            ) from None
        return loaded

    async def _certified_terminal_selected_run(
        self,
        workflow: WorkflowRunSnapshot,
        index: int,
        node: AgentNode,
        run_id: str,
        expected_descriptor: ExecutionDescriptor,
    ) -> tuple[
        RunSnapshot,
        tuple[SnapshotPrecondition, ...],
        RunRecoveryEvidencePrecondition,
    ]:
        if self._certify_terminal_run is None:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "terminal run certification is unavailable",
                retryable=False,
            ) from None
        certified, evidence_precondition = await self._certify_terminal_run(run_id)
        run = await self._load_selected_run(run_id)
        if run != certified:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "related terminal run changed after certification",
                retryable=False,
            ) from None
        if not _related_run_matches(
            workflow,
            index,
            node,
            run,
            expected_descriptor=expected_descriptor,
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "related run does not match workflow node",
                retryable=False,
            ) from None
        try:
            session = await load_session(self._store, workflow.session_id)
        except Exception:
            await self._raise_terminal_projection_ownership_error(workflow)
        if workflow.workflow_run_id not in session.active_workflow_run_ids:
            await self._raise_terminal_projection_ownership_error(workflow)
        parent = await self._validated_child_parent(workflow, index, node)
        self._validate_recovery_descriptor(workflow)
        related_preconditions = (
            exact_session_precondition(session),
            _exact_run_precondition(run),
            *(() if parent is None else (_exact_run_precondition(parent),)),
        )
        return run, related_preconditions, evidence_precondition

    async def _raise_terminal_projection_ownership_error(
        self,
        previous: WorkflowRunSnapshot,
    ) -> NoReturn:
        current = await self._state.load(previous.workflow_run_id)
        if current != previous:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "workflow state changed concurrently",
                retryable=True,
            ) from None
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "workflow recovery ownership unavailable",
            retryable=False,
        ) from None

    async def _workflow_changed_after_conflict(
        self,
        previous: WorkflowRunSnapshot,
        error: AgentSDKError,
    ) -> bool:
        if error.code is not ErrorCode.CONFLICT:
            return False
        current = await self._state.load(previous.workflow_run_id)
        return current != previous

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

    def _workflow_execution_descriptor(
        self,
        workflow: WorkflowIR,
    ) -> WorkflowExecutionDescriptor:
        tools = tuple(
            ToolCapabilityDescriptor.from_spec(spec)
            for spec in self._tool_specs()
        )
        config = self._policy.execution_config()
        policy = ExecutionPolicyDescriptor.create(
            permission_default=config["permission_default"],
            permission_rules=config["permission_rules"],
        )
        agents: list[WorkflowAgentDescriptor] = []
        seen: set[str] = set()
        for node in workflow.nodes:
            if node.agent_revision in seen:
                continue
            seen.add(node.agent_revision)
            agent = self._agents.resolve(node.agent_revision)
            execution = ExecutionDescriptor.create(
                agent=agent,
                messages=({"role": "user", "content": node.input},),
                tools=tools,
                policy=policy,
            )
            agents.append(
                WorkflowAgentDescriptor.create(node.agent_revision, execution)
            )
        return WorkflowExecutionDescriptor.create(
            workflow=workflow,
            agents=tuple(agents),
            tools=tools,
            policy=policy,
        )

    async def _drive_v2(
        self,
        workflow_run_id: str,
        *,
        recover_run: Callable[[str], Awaitable[RunHandle]] | None = None,
    ) -> WorkflowResult:
        while True:
            snapshot = await self._state.load(workflow_run_id)
            if snapshot.status is WorkflowRunStatus.COMPLETED:
                return _result(snapshot)
            if snapshot.status is WorkflowRunStatus.FAILED:
                raise _failure_error(snapshot.error)
            if recover_run is not None:
                await self._validate_recovery_preflight(snapshot)
            control = snapshot.control
            if control is None:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "schema-v2 workflow control state is missing",
                    retryable=False,
                )
            completed_nodes = {
                node.node_id: node
                for node in snapshot.nodes
                if node.status is WorkflowNodeStatus.COMPLETED
            }
            action = next_action(
                snapshot.workflow,
                control,
                completed_nodes=completed_nodes,
            )
            if isinstance(action, PersistControl):
                try:
                    await self._state.advance_control(
                        snapshot,
                        action.control,
                        event_type=action.event_type,
                        event_payload=action.event_payload,
                    )
                except AgentSDKError as error:
                    if await self._workflow_changed_after_conflict(snapshot, error):
                        continue
                    raise
                continue
            if isinstance(action, CompleteWorkflow):
                try:
                    if recover_run is not None:
                        self._validate_recovery_descriptor(snapshot)
                    completed = await self._state.complete_workflow(
                        snapshot,
                        output_text=action.output_text,
                    )
                except AgentSDKError as error:
                    if await self._workflow_changed_after_conflict(snapshot, error):
                        continue
                    raise
                return _result(completed)
            if isinstance(action, FailWorkflow):
                try:
                    if recover_run is not None:
                        self._validate_recovery_descriptor(snapshot)
                    failed = await self._state.fail_workflow(
                        snapshot,
                        action.failure,
                    )
                except AgentSDKError as error:
                    if await self._workflow_changed_after_conflict(snapshot, error):
                        continue
                    raise
                raise _failure_error(failed.error)
            assert isinstance(action, ExecuteAgent)
            index = next(
                index
                for index, node in enumerate(snapshot.workflow.nodes)
                if node.id == action.node.id
            )
            if recover_run is None:
                await self._execute_agent_instruction(snapshot, index, action.node)
            else:
                await self._recover_agent_instruction(
                    snapshot,
                    index,
                    action.node,
                    recover_run,
                )

    async def _execute_agent_instruction(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        node: AgentNode,
    ) -> None:
        node_snapshot = snapshot.nodes[index]
        if node_snapshot.status is WorkflowNodeStatus.FAILED:
            failure = node_snapshot.error or _generic_failure(
                "workflow node failed"
            )
            await self._state.fail_workflow(snapshot, failure)
            raise _failure_error(failure)
        if node_snapshot.status in {
            WorkflowNodeStatus.PENDING,
            WorkflowNodeStatus.COMPLETED,
        }:
            try:
                snapshot = await self._state.start_node(
                    snapshot,
                    index,
                    new_id("run"),
                )
            except AgentSDKError as error:
                if await self._workflow_changed_after_conflict(snapshot, error):
                    return
                raise
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
            snapshot,
            index,
            node,
            run,
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "related run does not match workflow node",
                retryable=False,
            )
        if run is _RunLoadFailure.MISSING:
            result = await self._create_and_execute(snapshot, index, node, run_id)
        elif run.status is RunStatus.CREATED:
            if self._recover_run is None:
                result = await self._execute_created(node, run)
            else:
                result = await self._recover_normal_run(run_id)
        elif run.status is RunStatus.COMPLETED:
            result = _run_result(run)
        elif run.status is RunStatus.FAILED:
            failure = _run_workflow_failure(run)
            await self._persist_failure(snapshot, index, failure)
            raise _failure_error(failure)
        elif self._recover_run is not None:
            result = await self._recover_normal_run(run_id)
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
        try:
            await self._state.complete_node(snapshot, index, result)
        except AgentSDKError as error:
            if await self._workflow_changed_after_conflict(snapshot, error):
                return
            raise

    async def _recover_agent_instruction(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        node: AgentNode,
        recover_run: Callable[[str], Awaitable[RunHandle]],
    ) -> None:
        node_snapshot = snapshot.nodes[index]
        if node_snapshot.status is WorkflowNodeStatus.FAILED:
            failure = node_snapshot.error or _generic_failure(
                "workflow node failed"
            )
            self._validate_recovery_descriptor(snapshot)
            await self._state.fail_workflow(snapshot, failure)
            raise _failure_error(failure)
        if node_snapshot.status in {
            WorkflowNodeStatus.PENDING,
            WorkflowNodeStatus.COMPLETED,
        }:
            try:
                self._validate_recovery_descriptor(snapshot)
                snapshot = await self._state.start_node(
                    snapshot,
                    index,
                    new_id("run"),
                )
            except AgentSDKError as error:
                if error.code is ErrorCode.CONFLICT:
                    return
                raise
            node_snapshot = snapshot.nodes[index]
        run_id = node_snapshot.run_id
        if run_id is None:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "workflow node has no selected run",
                retryable=False,
            )
        descriptor = self._node_execution_descriptor(node)
        run = await self._ensure_selected_run(
            snapshot,
            index,
            node,
            run_id,
            descriptor,
        )
        descriptor = self._selected_execution_descriptor(node, run, descriptor)
        if not _related_run_matches(
            snapshot,
            index,
            node,
            run,
            expected_descriptor=descriptor,
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "related run does not match workflow node",
                retryable=False,
            )
        if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
            try:
                if node.run_as == "child":
                    await self._children.await_result(run_id)
                else:
                    await (await recover_run(run_id)).result()
            except AgentSDKError as error:
                run = await self._load_selected_run(run_id)
                if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                    if (
                        run.status is RunStatus.WAITING_RECONCILIATION
                        or error.code is ErrorCode.CONFLICT
                    ):
                        raise AgentSDKError(
                            ErrorCode.CONFLICT,
                            "recovery required",
                            retryable=True,
                        ) from None
                    raise
            run = await self._load_selected_run(run_id)
        if run.status is RunStatus.COMPLETED:
            run, preconditions, evidence = (
                await self._certified_terminal_selected_run(
                    snapshot,
                    index,
                    node,
                    run_id,
                    descriptor,
                )
            )
            self._validate_recovery_descriptor(snapshot)
            try:
                await self._state.complete_node(
                    snapshot,
                    index,
                    _run_result(run),
                    related_preconditions=preconditions,
                    recovery_evidence_precondition=evidence,
                )
            except AgentSDKError as error:
                if error.code is ErrorCode.CONFLICT:
                    return
                raise
            return
        if run.status is RunStatus.FAILED:
            run, preconditions, evidence = (
                await self._certified_terminal_selected_run(
                    snapshot,
                    index,
                    node,
                    run_id,
                    descriptor,
                )
            )
            failure = _run_workflow_failure(run)
            self._validate_recovery_descriptor(snapshot)
            failed_node = await self._state.fail_node(
                snapshot,
                index,
                failure,
                related_preconditions=preconditions,
                recovery_evidence_precondition=evidence,
            )
            self._validate_recovery_descriptor(failed_node)
            await self._state.fail_workflow(failed_node, failure)
            raise _failure_error(failure)
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "recovery required",
            retryable=True,
        )

    async def _drive(self, workflow_run_id: str) -> WorkflowResult:
        initial = await self._state.load(workflow_run_id)
        if (
            initial.workflow.schema_version == 2
            and not _is_linear_program(initial.workflow)
        ):
            return await self._drive_v2(workflow_run_id)
        while True:
            snapshot = await self._state.load(workflow_run_id)
            if snapshot.status is WorkflowRunStatus.COMPLETED:
                return _result(snapshot)
            if snapshot.status is WorkflowRunStatus.FAILED:
                raise _failure_error(snapshot.error)

            index = _next_node_index(snapshot)
            if index is None:
                try:
                    completed = await self._state.complete_workflow(snapshot)
                except AgentSDKError as error:
                    if await self._workflow_changed_after_conflict(snapshot, error):
                        continue
                    raise
                return _result(completed)
            node_snapshot = snapshot.nodes[index]
            node = snapshot.workflow.nodes[index]
            if node_snapshot.status is WorkflowNodeStatus.FAILED:
                try:
                    failed = await self._state.fail_workflow(
                        snapshot,
                        node_snapshot.error
                        or _generic_failure("workflow node failed"),
                    )
                except AgentSDKError as error:
                    if await self._workflow_changed_after_conflict(snapshot, error):
                        continue
                    raise
                raise _failure_error(failed.error)
            if node_snapshot.status is WorkflowNodeStatus.PENDING:
                try:
                    snapshot = await self._state.start_node(
                        snapshot,
                        index,
                        new_id("run"),
                    )
                except AgentSDKError as error:
                    if await self._workflow_changed_after_conflict(snapshot, error):
                        continue
                    raise
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
                if self._recover_run is None:
                    result = await self._execute_created(node, run)
                else:
                    result = await self._recover_normal_run(run_id)
            elif run.status is RunStatus.COMPLETED:
                result = _run_result(run)
            elif run.status is RunStatus.FAILED:
                failure = _generic_failure("related run failed")
                await self._persist_failure(snapshot, index, failure)
                raise _failure_error(failure)
            elif self._recover_run is not None:
                result = await self._recover_normal_run(run_id)
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
                try:
                    await self._persist_failure(snapshot, index, failure)
                except AgentSDKError as error:
                    if await self._workflow_changed_after_conflict(snapshot, error):
                        continue
                    raise
                raise _failure_error(failure)
            try:
                await self._state.complete_node(snapshot, index, result)
            except AgentSDKError as error:
                if await self._workflow_changed_after_conflict(snapshot, error):
                    continue
                raise

    async def _create_and_execute(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        node: AgentNode,
        run_id: str,
    ) -> RunResult | _RunFailure:
        if self._recover_run is not None:
            return await self._create_and_recover(
                snapshot,
                index,
                node,
                run_id,
            )
        agent = self._agents.resolve(node.agent_revision)
        if node.run_as == "child":
            parent_run_id = _parent_run_id(snapshot, index)
            task_envelope = _task_envelope(node)
            outcome = await _spawn_child(
                self._children,
                session_id=snapshot.session_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
                workflow_run_id=snapshot.workflow_run_id,
                workflow_node_id=node.id,
                workflow_node_execution=(
                    snapshot.nodes[index].execution_count
                    if snapshot.workflow.schema_version == 2
                    else None
                ),
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
            workflow_node_execution=(
                snapshot.nodes[index].execution_count
                if snapshot.workflow.schema_version == 2
                else None
            ),
            execution_descriptor=self._execution_descriptor(agent, node.input),
        )
        if isinstance(created, _RunFailure):
            return created
        return await self._execute_created(node, created, agent=agent)

    async def _create_and_recover(
        self,
        snapshot: WorkflowRunSnapshot,
        index: int,
        node: AgentNode,
        run_id: str,
    ) -> RunResult | _RunFailure:
        descriptor = self._node_execution_descriptor(node)
        try:
            run = await self._ensure_selected_run(
                snapshot,
                index,
                node,
                run_id,
                descriptor,
                use_idempotency=False,
            )
            descriptor = self._selected_execution_descriptor(node, run, descriptor)
        except AgentSDKError as failure:
            return _RunFailure(failure.code, failure.message)
        except Exception:
            return _RunFailure(ErrorCode.INTERNAL, "run recovery failed")
        if not _related_run_matches(
            snapshot,
            index,
            node,
            run,
            expected_descriptor=descriptor,
        ):
            return _RunFailure(
                ErrorCode.INVALID_STATE,
                "related run does not match workflow node",
            )
        if node.run_as == "child":
            try:
                child = await self._children.await_result(run_id)
                return RunResult(
                    run_id=child.run_id,
                    output_text=child.output_text,
                    usage=TokenUsage.model_validate(child.usage.model_dump()),
                )
            except AgentSDKError as failure:
                return _RunFailure(failure.code, failure.message)
            except Exception:
                return _RunFailure(ErrorCode.INTERNAL, "child execution failed")
        return await self._recover_normal_run(run_id)

    async def _recover_normal_run(
        self,
        run_id: str,
    ) -> RunResult | _RunFailure:
        recover_run = self._recover_run
        if recover_run is None:
            return _RunFailure(ErrorCode.INTERNAL, "run recovery unavailable")
        try:
            return await (await recover_run(run_id)).result()
        except AgentSDKError as failure:
            if (
                failure.code is ErrorCode.CONFLICT
                and failure.message == "recovery required"
                and failure.retryable
            ):
                raise AgentSDKError(
                    ErrorCode.CONFLICT,
                    "recovery required",
                    retryable=True,
                ) from None
            return _RunFailure(failure.code, failure.message)
        except Exception:
            return _RunFailure(ErrorCode.INTERNAL, "run recovery failed")

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
                permission_default=config["permission_default"],
                permission_rules=config["permission_rules"],
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
    service: ChildCoordinator,
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


def _is_linear_program(workflow: WorkflowIR) -> bool:
    return workflow.schema_version == 2 and all(
        instruction.op in {"agent", "complete"}
        for instruction in workflow.instructions
    )


def _parent_run_id(snapshot: WorkflowRunSnapshot, index: int) -> str:
    return _parent_execution_identity(snapshot, index).run_id


def _parent_execution_identity(
    snapshot: WorkflowRunSnapshot,
    index: int,
) -> _ParentExecutionIdentity:
    if index == 0:
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "root workflow node cannot be a child",
            retryable=False,
        )
    if (
        snapshot.workflow.schema_version == 1
        or _is_linear_program(snapshot.workflow)
    ):
        parent_index = index - 1
        parent = snapshot.nodes[parent_index]
        if parent.run_id is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "child workflow node has no parent run",
                retryable=False,
            )
        return _ParentExecutionIdentity(
            node_index=parent_index,
            run_id=parent.run_id,
            node_execution=(
                parent.execution_count
                if snapshot.workflow.schema_version == 2
                else None
            ),
        )
    control = snapshot.control
    if control is None or control.last_output_node_id is None:
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "child workflow node has no parent run",
            retryable=False,
        )
    parent_node_id = control.last_output_node_id
    for parent_index, candidate in enumerate(snapshot.workflow.nodes):
        if candidate.id == parent_node_id:
            projection = snapshot.nodes[parent_index]
            parent_run_id = (
                control.last_output_run_id
                if control.last_output_run_id is not None
                else projection.run_id
            )
            parent_execution = (
                control.last_output_node_execution
                if control.last_output_node_execution is not None
                else projection.execution_count
            )
            if parent_run_id is None:
                break
            return _ParentExecutionIdentity(
                node_index=parent_index,
                run_id=parent_run_id,
                node_execution=parent_execution,
            )
    raise AgentSDKError(
        ErrorCode.INVALID_STATE,
        "child workflow node has no parent run",
        retryable=False,
    )


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
    *,
    expected_descriptor: ExecutionDescriptor | None = None,
) -> bool:
    if not _workflow_execution_run_matches(
        workflow,
        node,
        run,
        expected_run_id=workflow.nodes[index].run_id,
        expected_node_execution=(
            workflow.nodes[index].execution_count
            if workflow.workflow.schema_version == 2
            else None
        ),
        expected_descriptor=expected_descriptor,
    ):
        return False
    if node.run_as == "parent":
        return (
            run.parent_run_id is None
            and run.task_envelope is None
            and run.user_input == node.input
        )
    expected_parent = _parent_run_id(workflow, index)
    expected_envelope = _task_envelope(node)
    return (
        run.parent_run_id == expected_parent
        and run.task_envelope == expected_envelope
        and run.user_input == render_task_envelope(expected_envelope)
    )


def _historical_parent_run_matches(
    workflow: WorkflowRunSnapshot,
    identity: _ParentExecutionIdentity,
    node: AgentNode,
    run: RunSnapshot,
    *,
    expected_descriptor: ExecutionDescriptor,
) -> bool:
    if not _workflow_execution_run_matches(
        workflow,
        node,
        run,
        expected_run_id=identity.run_id,
        expected_node_execution=identity.node_execution,
        expected_descriptor=expected_descriptor,
    ):
        return False
    if node.run_as == "parent":
        return (
            run.parent_run_id is None
            and run.task_envelope is None
            and run.user_input == node.input
        )
    expected_envelope = _task_envelope(node)
    return (
        run.parent_run_id is not None
        and run.parent_run_id != run.run_id
        and run.task_envelope == expected_envelope
        and run.user_input == render_task_envelope(expected_envelope)
    )


def _workflow_execution_run_matches(
    workflow: WorkflowRunSnapshot,
    node: AgentNode,
    run: RunSnapshot,
    *,
    expected_run_id: str | None,
    expected_node_execution: int | None,
    expected_descriptor: ExecutionDescriptor | None,
) -> bool:
    return not (
        run.run_id != expected_run_id
        or run.session_id != workflow.session_id
        or run.workflow_run_id != workflow.workflow_run_id
        or run.workflow_node_id != node.id
        or run.workflow_node_execution != expected_node_execution
        or run.agent_revision != node.agent_revision
        or (
            expected_descriptor is not None
            and (
                run.execution_compatibility != "current"
                or run.execution_descriptor != expected_descriptor
            )
        )
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


def _run_workflow_failure(run: RunSnapshot) -> WorkflowFailure:
    failure = run.error
    if failure is None:
        return _generic_failure("related run terminal state is invalid")
    return WorkflowFailure(
        code=failure.code,
        message=failure.message,
        retryable=failure.retryable,
    )


def _exact_run_precondition(run: RunSnapshot) -> SnapshotPrecondition:
    return SnapshotPrecondition(
        "run",
        run.run_id,
        run.version,
        run.session_id,
        run.model_dump(mode="json"),
    )


def _invalid_parent_run() -> AgentSDKError:
    return AgentSDKError(
        ErrorCode.INVALID_STATE,
        "related parent run is invalid",
        retryable=False,
    )


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
