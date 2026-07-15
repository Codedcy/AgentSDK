from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.ids import new_id
from agent_sdk.models.litellm_gateway import ModelRequest, ToolCallCompleted
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.runtime._recovery_observability import hashed_identity
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.engine import (
    RunEngine,
    _model_request_fingerprint,
    _tool_request_fingerprint,
)
from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
)
from agent_sdk.runtime.leases import (
    Lease,
    LeaseHeldError,
    LeaseLostError,
    LeaseManager,
)
from agent_sdk.runtime.models import (
    RunResult,
    RunSnapshot,
    RunStatus,
    SessionSnapshot,
    TokenUsage,
    mutable_model_params,
)
from agent_sdk.runtime.provider_recovery import (
    ProviderRecoveryAdapter,
    ProviderRecoveryDisposition,
    ProviderRecoveryRegistry,
    ProviderRecoveryRequest,
    ProviderRecoveryResult,
)
from agent_sdk.runtime.reconciliation import (
    ExternalOperation,
    ExternalOperationKind,
    ExternalOperationStatus,
    ModelCallOperation,
    ReconciliationRequest,
    RecoveryStateConflictError,
    RunCheckpoint,
    RunCheckpointPhase,
    ToolCallOperation,
)
from agent_sdk.runtime.session_lifecycle import (
    exact_run_precondition,
    exact_session_precondition,
)
from agent_sdk.storage.base import (
    CommitResult,
    ExternalOperationWrite,
    ReconciliationRequestWrite,
    RunProgressBatch,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.tools.models import ToolResult, ToolResultStatus, ToolRetryPolicy, thaw_json
from agent_sdk.tools.registry import RegisteredTool, ToolRegistry


_SCANNER_LEASE_TTL = timedelta(seconds=30)

_CERTIFIED_RUN_EVENT_TYPES = frozenset(
    {
        "run.created",
        "run.started",
        "run.recovery.started",
        "run.interrupted",
        "step.started",
        "step.completed",
        "model.call.started",
        "model.text.delta",
        "model.usage.reported",
        "model.call.completed",
        "tool.call.proposed",
        "permission.requested",
        "permission.resolved",
        "tool.call.authorized",
        "tool.call.started",
        "tool.call.completed",
        "model.recovery.query.started",
        "model.recovery.resend.started",
        "tool.recovery.retry.started",
    }
)


@dataclass(frozen=True)
class RecoveryPlan:
    kind: Literal[
        "detached",
        "execute",
        "resume",
        "reconcile",
        "provider_recovery",
        "tool_recovery",
        "follow",
    ]
    run_id: str
    request: ModelRequest | None = None
    checkpoint: RunCheckpoint | None = None
    reason: str | None = None
    operation_id: str | None = None
    details: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _RecoveryEvidence:
    run: RunSnapshot
    session: SessionSnapshot
    checkpoint: RunCheckpoint | None
    operations: tuple[ExternalOperation, ...]
    pending: tuple[ReconciliationRequest, ...]
    run_events: tuple[EventEnvelope, ...]
    run_event_cursors: tuple[int, ...]
    run_event_ids_unique: bool


@dataclass(frozen=True)
class _CertifiedToolRecovery:
    call: ToolCallCompleted
    registered: RegisteredTool


@dataclass(frozen=True)
class _CertifiedTurnEvidence:
    call: ToolCallCompleted
    finish_reason: str | None
    text: str
    usage: TokenUsage
    result: ToolResult | None
    operation: ToolCallOperation | None
    permission_allowed: bool | None


class RunRecoveryService:
    def __init__(
        self,
        store: StateStore,
        engine: RunEngine,
        agents: AgentRegistry,
        tools: ToolRegistry,
        policy: PolicyEngine,
        provider_recovery: ProviderRecoveryRegistry | None = None,
        *,
        lease_manager: LeaseManager | None = None,
        _clock: Callable[[], datetime] | None = None,
        _yield: Callable[[], Awaitable[None]] | None = None,
        _stopping: Callable[[], bool] | None = None,
        _wait_stopping: Callable[[], Awaitable[object]] | None = None,
        _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        _heartbeat_interval: float = _SCANNER_LEASE_TTL.total_seconds() / 3,
        _adapter_timeout: float = 30.0,
    ) -> None:
        self._store = store
        self._engine = engine
        self._agents = agents
        self._tools = tools
        self._policy = policy
        self._provider_recovery = provider_recovery or ProviderRecoveryRegistry()
        self._leases = lease_manager or LeaseManager(
            store,
            ttl=_SCANNER_LEASE_TTL,
        )
        self._clock = _clock or (lambda: datetime.now(UTC))
        self._yield = _yield or _yield_once
        self._stopping = _stopping or (lambda: False)
        self._wait_stopping = _wait_stopping
        self._sleep = _sleep
        self._heartbeat_interval = _heartbeat_interval
        self._adapter_timeout = _adapter_timeout

    async def plan(self, run_id: str) -> RecoveryPlan:
        public_error: tuple[ErrorCode, str, bool] | None = None
        try:
            return await self._plan_private(run_id)
        except AgentSDKError as error:
            public_error = (error.code, error.message, error.retryable)
        except Exception:
            public_error = (
                ErrorCode.INTERNAL,
                "failed to recover run",
                False,
            )
        del self, run_id
        assert public_error is not None
        raise AgentSDKError(
            public_error[0],
            public_error[1],
            retryable=public_error[2],
        ) from None

    async def _plan_private(self, run_id: str) -> RecoveryPlan:
        run = await self._load_run(run_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            return RecoveryPlan("detached", run_id)
        if run.status is RunStatus.WAITING_RECONCILIATION:
            await self._validated_pending_requests(run)
            return RecoveryPlan("detached", run_id)
        evidence = await self._load_evidence(run)
        checkpoint = evidence.checkpoint
        try:
            request = await self._validated_request(evidence)
        except AgentSDKError:
            if (
                run.status is RunStatus.INTERRUPTED
                and checkpoint is not None
                and checkpoint.phase is RunCheckpointPhase.TOOL_IN_FLIGHT
            ):
                return RecoveryPlan(
                    "reconcile",
                    run_id,
                    checkpoint=checkpoint,
                    reason="recovery_state_invalid",
                    operation_id=checkpoint.operation_id,
                    details=(("checkpoint_phase", checkpoint.phase.value),),
                )
            raise
        if run.execution_compatibility == "legacy_unknown":
            return RecoveryPlan(
                "reconcile",
                run_id,
                checkpoint=checkpoint,
                reason="legacy_unknown",
                details=(("run_status", run.status.value),),
            )
        assert request is not None
        if run.status is RunStatus.CREATED:
            if self._is_pristine_created(evidence):
                return RecoveryPlan("execute", run_id, request=request)
            return RecoveryPlan(
                "reconcile",
                run_id,
                checkpoint=checkpoint,
                reason="created_not_pristine",
                details=(("run_status", run.status.value),),
            )
        if run.status in {RunStatus.RUNNING, RunStatus.WAITING_PERMISSION}:
            return RecoveryPlan("follow", run_id)
        if run.status is not RunStatus.INTERRUPTED:
            return RecoveryPlan(
                "reconcile",
                run_id,
                checkpoint=checkpoint,
                reason="recovery_state_invalid",
                details=(("run_status", run.status.value),),
            )
        if checkpoint is None:
            return RecoveryPlan(
                "reconcile",
                run_id,
                reason="legacy_checkpoint_missing",
                details=(("run_status", run.status.value),),
            )
        if checkpoint.phase is RunCheckpointPhase.WAITING:
            return RecoveryPlan(
                "reconcile",
                run_id,
                checkpoint=checkpoint,
                reason="permission_wait_lost",
                details=(("checkpoint_phase", checkpoint.phase.value),),
            )
        if checkpoint.phase in {
            RunCheckpointPhase.MODEL_IN_FLIGHT,
            RunCheckpointPhase.TOOL_IN_FLIGHT,
        }:
            linked = self._matching_in_flight_operation(evidence)
            if linked is None:
                return RecoveryPlan(
                    "reconcile",
                    run_id,
                    checkpoint=checkpoint,
                    reason="recovery_state_invalid",
                    details=(("checkpoint_phase", checkpoint.phase.value),),
                )
            if checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT:
                assert isinstance(linked, ModelCallOperation)
                provider_request = self._certified_provider_request(
                    evidence,
                    request,
                    linked,
                )
                if provider_request is not None:
                    return RecoveryPlan(
                        "provider_recovery",
                        run_id,
                        request=provider_request.model_request,
                        checkpoint=checkpoint,
                        operation_id=linked.operation_id,
                    )
                reason = "model_call_unknown_outcome"
            else:
                assert isinstance(linked, ToolCallOperation)
                if self._certified_tool_call(evidence, request, linked) is not None:
                    return RecoveryPlan(
                        "tool_recovery",
                        run_id,
                        request=request,
                        checkpoint=checkpoint,
                        operation_id=linked.operation_id,
                    )
                reason = "tool_call_unknown_outcome"
            return RecoveryPlan(
                "reconcile",
                run_id,
                checkpoint=checkpoint,
                reason=reason,
                operation_id=linked.operation_id,
                details=(("checkpoint_phase", checkpoint.phase.value),),
            )
        completed_terminal_gap = self._completed_model_terminalization_gap(evidence)
        if completed_terminal_gap is not None:
            return RecoveryPlan(
                "reconcile",
                run_id,
                checkpoint=checkpoint,
                reason="model_call_completed_terminalization_unknown",
                operation_id=completed_terminal_gap.operation_id,
                details=(
                    ("checkpoint_phase", checkpoint.phase.value),
                    ("operation_status", completed_terminal_gap.status.value),
                ),
            )
        if self._is_safe_checkpoint(evidence):
            try:
                self._engine.validate_resume_checkpoint(checkpoint)
            except AgentSDKError:
                return RecoveryPlan(
                    "reconcile",
                    run_id,
                    checkpoint=checkpoint,
                    reason="recovery_state_invalid",
                    details=(("checkpoint_phase", checkpoint.phase.value),),
                )
            return RecoveryPlan(
                "resume",
                run_id,
                request=request,
                checkpoint=checkpoint,
            )
        return RecoveryPlan(
            "reconcile",
            run_id,
            checkpoint=checkpoint,
            reason="recovery_state_invalid",
            details=(("checkpoint_phase", checkpoint.phase.value),),
        )

    async def pending_requests(
        self,
        run_id: str,
    ) -> tuple[ReconciliationRequest, ...]:
        public_error: tuple[ErrorCode, str, bool] | None = None
        run: RunSnapshot | None = None
        try:
            run = await self._load_run(run_id)
            return await self._validated_pending_requests(run)
        except AgentSDKError as error:
            public_error = (error.code, error.message, error.retryable)
        except Exception:
            public_error = (
                ErrorCode.INTERNAL,
                "failed to load recovery requests",
                False,
            )
        del self, run_id, run
        assert public_error is not None
        raise AgentSDKError(
            public_error[0],
            public_error[1],
            retryable=public_error[2],
        ) from None

    async def execute(self, plan: RecoveryPlan) -> RunResult:
        public_error: tuple[ErrorCode, str, bool] | None = None
        try:
            return await self._execute_private(plan)
        except asyncio.CancelledError:
            del self, plan
            raise
        except AgentSDKError as error:
            public_error = (error.code, error.message, error.retryable)
        except Exception:
            public_error = (
                ErrorCode.INTERNAL,
                "failed to recover run",
                False,
            )
        del self, plan
        assert public_error is not None
        raise AgentSDKError(
            public_error[0],
            public_error[1],
            retryable=public_error[2],
        ) from None

    async def _execute_private(self, plan: RecoveryPlan) -> RunResult:
        follow = False
        run_id = plan.run_id
        try:
            if plan.kind == "execute":
                assert plan.request is not None
                return await self._engine.execute(plan.run_id, plan.request)
            if plan.kind == "resume":
                assert plan.request is not None
                assert plan.checkpoint is not None
                return await self._engine.resume(
                    plan.run_id,
                    plan.checkpoint,
                    plan.request,
                )
            if plan.kind == "reconcile":
                assert plan.reason is not None
                reason = plan.reason
                operation_id = plan.operation_id
                details = dict(plan.details)
                del plan
                return await self._coordinate_reconciliation(
                    run_id,
                    reason=reason,
                    operation_id=operation_id,
                    details=details,
                )
            if plan.kind == "provider_recovery":
                assert plan.request is not None
                assert plan.checkpoint is not None
                assert plan.operation_id is not None
                return await self._coordinate_provider_recovery(plan)
            if plan.kind == "tool_recovery":
                assert plan.request is not None
                assert plan.checkpoint is not None
                assert plan.operation_id is not None
                return await self._coordinate_tool_recovery(plan)
            if plan.kind == "follow":
                return await self._follow_durable_run(plan.run_id)
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not recoverable",
                retryable=False,
            ) from None
        except LeaseHeldError:
            follow = True
        except RecoveryStateConflictError:
            follow = True
        except AgentSDKError as error:
            if error.code in {ErrorCode.CONFLICT, ErrorCode.INVALID_STATE}:
                follow = True
            else:
                raise
        if follow:
            return await self._follow_durable_run(run_id)
        raise AssertionError("unreachable")

    async def _follow_durable_run(self, run_id: str) -> RunResult:
        while True:
            if self._stopping():
                raise self._recovery_required() from None
            run = await self._load_run(run_id)
            terminal = self._terminal_result(run)
            if terminal is not None:
                return terminal
            if run.status is RunStatus.WAITING_RECONCILIATION:
                raise self._recovery_required() from None

            lease = await self._store.get_run_lease(run_id)
            if lease is None or lease.expires_at <= self._clock():
                confirmed_run = await self._load_run(run_id)
                terminal = self._terminal_result(confirmed_run)
                if terminal is not None:
                    return terminal
                if confirmed_run.status is RunStatus.WAITING_RECONCILIATION:
                    raise self._recovery_required() from None
                confirmed_lease = await self._store.get_run_lease(run_id)
                if (
                    confirmed_lease is None
                    or confirmed_lease.expires_at <= self._clock()
                ):
                    raise self._recovery_required() from None
            await self._yield()

    @staticmethod
    def _terminal_result(run: RunSnapshot) -> RunResult | None:
        if run.status is RunStatus.COMPLETED:
            assert run.output_text is not None
            assert run.usage is not None
            return RunResult(
                run_id=run.run_id,
                output_text=run.output_text,
                usage=run.usage,
                tool_results=run.tool_results,
            )
        if run.status is RunStatus.FAILED:
            failure = run.error
            assert failure is not None
            try:
                code = ErrorCode(failure.code)
            except ValueError:
                code = ErrorCode.INTERNAL
            raise AgentSDKError(
                code,
                failure.message,
                retryable=failure.retryable,
            ) from None
        return None

    async def _load_run(self, run_id: str) -> RunSnapshot:
        data = await self._store.get_snapshot("run", run_id)
        if data is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "run not found",
                retryable=False,
            ) from None
        try:
            run = RunSnapshot.model_validate(data)
        except Exception:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "recovery state is invalid",
                retryable=False,
            ) from None
        if run.run_id != run_id:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "recovery state is invalid",
                retryable=False,
            ) from None
        return run

    async def _load_evidence(self, run: RunSnapshot) -> _RecoveryEvidence:
        session_data = await self._store.get_snapshot("session", run.session_id)
        try:
            session = SessionSnapshot.model_validate(session_data)
        except Exception:
            raise self._state_error() from None
        if run.run_id not in session.active_run_ids:
            raise self._state_error() from None
        checkpoint = await self._store.get_run_checkpoint(run.run_id)
        operations = await self._store.list_external_operations(run.run_id)
        pending = await self._store.list_pending_reconciliation_requests(run.run_id)
        up_to_cursor = await self._store.latest_cursor()
        events = await self._store.read_events(
            after_cursor=0,
            up_to_cursor=up_to_cursor,
        )
        run_records = tuple(
            stored for stored in events if stored.event.run_id == run.run_id
        )
        event_ids = tuple(stored.event.event_id for stored in events)
        return _RecoveryEvidence(
            run=run,
            session=session,
            checkpoint=checkpoint,
            operations=operations,
            pending=pending,
            run_events=tuple(stored.event for stored in run_records),
            run_event_cursors=tuple(stored.cursor for stored in run_records),
            run_event_ids_unique=len(event_ids) == len(set(event_ids)),
        )

    @staticmethod
    def _is_valid_run_event_envelope(evidence: _RecoveryEvidence) -> bool:
        events = evidence.run_events
        cursors = evidence.run_event_cursors
        run = evidence.run
        if (
            not events
            or len(events) != len(cursors)
            or not evidence.run_event_ids_unique
            or any(type(cursor) is not int or cursor <= 0 for cursor in cursors)
            or any(left >= right for left, right in zip(cursors, cursors[1:]))
            or tuple(event.sequence for event in events)
            != tuple(range(1, len(events) + 1))
            or any(event.type not in _CERTIFIED_RUN_EVENT_TYPES for event in events)
            or sum(event.type == "run.created" for event in events) != 1
            or sum(event.type == "run.started" for event in events) != 1
        ):
            return False
        if any(
            not isinstance(event.event_id, str)
            or not event.event_id.strip()
            or type(event.schema_version) is not int
            or event.schema_version != 1
            or not isinstance(event.type, str)
            or not event.type.strip()
            or event.session_id != run.session_id
            or event.run_id != run.run_id
            or type(event.sequence) is not int
            or not isinstance(event.payload, dict)
            or not isinstance(event.occurred_at, datetime)
            or event.occurred_at.tzinfo is None
            or event.occurred_at.utcoffset() is None
            for event in events
        ):
            return False
        recovery_started = tuple(
            event for event in events if event.type == "run.recovery.started"
        )
        interrupted = tuple(
            event for event in events if event.type == "run.interrupted"
        )
        if (
            len(interrupted) != len(recovery_started) + 1
            or any(
                event.payload != {"status": RunStatus.RUNNING.value}
                for event in recovery_started
            )
            or any(
                event.payload != {"status": RunStatus.INTERRUPTED.value}
                for event in interrupted
            )
            or any(
                not RunRecoveryService._is_valid_model_recovery_audit(
                    event,
                    evidence.operations,
                )
                for event in events
                if event.type.startswith("model.recovery.")
            )
            or any(
                not RunRecoveryService._is_valid_tool_recovery_audit(
                    event,
                    evidence.operations,
                )
                for event in events
                if event.type == "tool.recovery.retry.started"
            )
        ):
            return False
        created = RunSnapshot(
            run_id=run.run_id,
            session_id=run.session_id,
            agent_revision=run.agent_revision,
            status=RunStatus.CREATED,
            user_input=run.user_input,
            parent_run_id=run.parent_run_id,
            workflow_run_id=run.workflow_run_id,
            workflow_node_id=run.workflow_node_id,
            task_envelope=run.task_envelope,
            execution_compatibility=run.execution_compatibility,
            execution_descriptor=run.execution_descriptor,
        )
        return (
            events[0].type == "run.created"
            and events[0].payload == created.model_dump(mode="json")
            and events[1].type == "run.started"
            and events[1].payload == {"status": RunStatus.RUNNING.value}
        )

    @staticmethod
    def _is_valid_model_recovery_audit(
        event: EventEnvelope,
        operations: tuple[ExternalOperation, ...],
    ) -> bool:
        action = event.type.removeprefix("model.recovery.").removesuffix(".started")
        payload = event.payload
        if action not in {"query", "resend"} or set(payload) != {
            "adapter_id",
            "adapter_version",
            "operation_id",
            "action",
        }:
            return False
        operation = next(
            (
                item
                for item in operations
                if isinstance(item, ModelCallOperation)
                and item.operation_id == payload["operation_id"]
            ),
            None,
        )
        if operation is None:
            return False
        metadata = operation.recovery_metadata
        capability = (
            "authoritative_status" if action == "query" else "same_operation_id_resend"
        )
        return (
            payload["action"] == action
            and payload["adapter_id"] == metadata.get("adapter_id")
            and payload["adapter_version"] == metadata.get("adapter_version")
            and metadata.get(capability) is True
        )

    @staticmethod
    def _is_valid_tool_recovery_audit(
        event: EventEnvelope,
        operations: tuple[ExternalOperation, ...],
    ) -> bool:
        payload = event.payload
        if set(payload) != {"operation", "call", "tool", "retry_class"}:
            return False
        return (
            RunRecoveryService._is_hashed_identity(payload["call"])
            and RunRecoveryService._is_hashed_identity(payload["tool"])
            and any(
                isinstance(operation, ToolCallOperation)
                and payload["operation"] == hashed_identity(operation.operation_id)
                and payload["retry_class"]
                == operation.recovery_metadata.get("retry_class")
                for operation in operations
            )
        )

    @staticmethod
    def _is_valid_certified_provider_history(
        evidence: _RecoveryEvidence,
    ) -> bool:
        checkpoint = evidence.checkpoint
        descriptor = evidence.run.execution_descriptor
        if checkpoint is None or descriptor is None:
            return False
        events = evidence.run_events
        model_operations = tuple(
            operation
            for operation in evidence.operations
            if isinstance(operation, ModelCallOperation)
        )
        tool_operations = tuple(
            operation
            for operation in evidence.operations
            if isinstance(operation, ToolCallOperation)
        )
        if tuple(sorted(operation.turn for operation in model_operations)) != tuple(
            range(checkpoint.turn + 1)
        ):
            return False
        expected_counts = {
            "step.started": len(model_operations),
            "model.call.started": len(model_operations),
            "model.call.completed": sum(
                operation.status is ExternalOperationStatus.COMPLETED
                for operation in model_operations
            ),
            "tool.call.proposed": len(checkpoint.tool_results),
            "tool.call.authorized": len(tool_operations),
            "tool.call.started": len(tool_operations),
            "tool.call.completed": len(checkpoint.tool_results),
            "step.completed": checkpoint.turn,
        }
        if any(
            sum(event.type == event_type for event in events) != expected
            for event_type, expected in expected_counts.items()
        ):
            return False
        if (
            any(event.payload != {} for event in events if event.type == "step.started")
            or any(
                event.payload != {"model": descriptor.agent.model}
                for event in events
                if event.type == "model.call.started"
            )
            or sum(event.type == "permission.requested" for event in events)
            != sum(event.type == "permission.resolved" for event in events)
        ):
            return False
        last_interrupted = max(
            index for index, event in enumerate(events) if event.type == "run.interrupted"
        )
        return all(
            event.type
            in {"model.recovery.query.started", "model.recovery.resend.started"}
            for event in events[last_interrupted + 1 :]
        )

    async def _validated_request(
        self,
        evidence: _RecoveryEvidence,
    ) -> ModelRequest | None:
        run = evidence.run
        descriptor = run.execution_descriptor
        if run.execution_compatibility != "current" or descriptor is None:
            return None
        try:
            registered_agent = self._agents.resolve(run.agent_revision)
        except AgentSDKError:
            raise self._capability_error() from None
        live_policy = ExecutionPolicyDescriptor.create(
            permission_default=self._policy.execution_config()["permission_default"]
        )
        live_tools = tuple(
            ToolCapabilityDescriptor.from_spec(spec) for spec in self._tools.list()
        )
        descriptor_data = descriptor.model_dump(mode="json")
        descriptor_messages = tuple(descriptor_data["messages"])
        live_descriptor = ExecutionDescriptor.create(
            agent=registered_agent,
            messages=descriptor_messages,
            tools=live_tools,
            policy=live_policy,
        )
        if live_descriptor != descriptor:
            raise self._capability_error() from None
        request = ModelRequest(
            model=registered_agent.model,
            messages=descriptor_messages,
            tools=self._tools.schemas(),
            params=mutable_model_params(registered_agent.model_params),
        )
        return request

    @staticmethod
    def _is_pristine_created(evidence: _RecoveryEvidence) -> bool:
        run = evidence.run
        return (
            run.status is RunStatus.CREATED
            and run.version == 1
            and evidence.checkpoint is None
            and not evidence.operations
            and not evidence.pending
            and len(evidence.run_events) == 1
            and evidence.run_events[0].type == "run.created"
            and evidence.run_events[0].sequence == 1
            and evidence.run_events[0].session_id == run.session_id
            and evidence.run_events[0].run_id == run.run_id
            and evidence.run_events[0].payload == run.model_dump(mode="json")
        )

    @staticmethod
    def _is_safe_checkpoint(evidence: _RecoveryEvidence) -> bool:
        checkpoint = evidence.checkpoint
        if (
            checkpoint is None
            or checkpoint.run_id != evidence.run.run_id
            or checkpoint.session_id != evidence.run.session_id
            or checkpoint.phase
            not in {
                RunCheckpointPhase.READY_FOR_MODEL,
                RunCheckpointPhase.READY_FOR_TOOL,
            }
            or checkpoint.operation_id is not None
            or evidence.pending
            or any(
                operation.status is ExternalOperationStatus.STARTED
                for operation in evidence.operations
            )
        ):
            return False
        if checkpoint.phase is RunCheckpointPhase.READY_FOR_TOOL:
            return RunRecoveryService._is_exact_ready_tool_relation(evidence)
        return not any(
            operation.turn >= checkpoint.turn
            for operation in evidence.operations
        )

    @staticmethod
    def _is_exact_ready_tool_relation(evidence: _RecoveryEvidence) -> bool:
        checkpoint = evidence.checkpoint
        assert checkpoint is not None
        current_operations = tuple(
            operation
            for operation in evidence.operations
            if operation.turn == checkpoint.turn
        )
        if (
            len(current_operations) != 1
            or not isinstance(current_operations[0], ModelCallOperation)
            or any(
                operation.turn > checkpoint.turn
                for operation in evidence.operations
            )
        ):
            return False

        model_operations = tuple(
            operation
            for operation in evidence.operations
            if isinstance(operation, ModelCallOperation)
        )
        if tuple(operation.turn for operation in model_operations) != tuple(
            range(checkpoint.turn + 1)
        ):
            return False
        outcomes = tuple(
            RunRecoveryService._completed_model_outcome(operation)
            for operation in model_operations
        )
        if any(outcome is None for outcome in outcomes):
            return False
        completed_outcomes = tuple(outcome for outcome in outcomes if outcome is not None)
        if any(len(outcome[2]) != 1 for outcome in completed_outcomes):
            return False
        finish_reason, text, calls, _usage = completed_outcomes[-1]
        call = calls[0]

        messages = checkpoint.model_dump(mode="json")["messages"]
        if not messages or messages[-1] != {
            "role": "assistant",
            "content": text or None,
            "tool_calls": [
                {
                    "id": call["call_id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": call["arguments_json"],
                    },
                }
            ],
        }:
            return False
        if not "".join(checkpoint.output_parts).endswith(text):
            return False

        cumulative: dict[str, int | None] = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        for _finish_reason, _text, _calls, usage in completed_outcomes:
            for field in cumulative:
                value = getattr(usage, field)
                if value is not None:
                    cumulative[field] = (cumulative[field] or 0) + value
        if checkpoint.usage != TokenUsage(**cumulative):
            return False

        started_events = tuple(
            event for event in evidence.run_events if event.type == "model.call.started"
        )
        completed_events = tuple(
            event
            for event in evidence.run_events
            if event.type == "model.call.completed"
        )
        failed_events = tuple(
            event for event in evidence.run_events if event.type == "model.call.failed"
        )
        if (
            len(started_events) != len(model_operations)
            or len(completed_events) != len(model_operations)
            or failed_events
            or completed_events[-1].payload != {"finish_reason": finish_reason}
        ):
            return False
        completion_index = evidence.run_events.index(completed_events[-1])
        return tuple(
            event.type for event in evidence.run_events[completion_index + 1 :]
        ) == ("run.interrupted",)

    @staticmethod
    def _completed_model_outcome(
        operation: ModelCallOperation,
    ) -> tuple[str | None, str, tuple[Mapping[str, object], ...], TokenUsage] | None:
        if (
            operation.status is not ExternalOperationStatus.COMPLETED
            or operation.outcome is None
            or set(operation.outcome)
            != {"finish_reason", "text", "tool_calls", "usage"}
        ):
            return None
        finish_reason = operation.outcome["finish_reason"]
        text = operation.outcome["text"]
        calls = operation.outcome["tool_calls"]
        if (
            (finish_reason is not None and not isinstance(finish_reason, str))
            or not isinstance(text, str)
            or not isinstance(calls, tuple)
        ):
            return None
        validated_calls: list[Mapping[str, object]] = []
        for call in calls:
            if (
                not isinstance(call, Mapping)
                or set(call)
                != {"index", "call_id", "name", "arguments_json"}
                or type(call["index"]) is not int
                or call["index"] != len(validated_calls)
                or not all(
                    isinstance(call[field], str) and bool(call[field])
                    for field in ("call_id", "name", "arguments_json")
                )
            ):
                return None
            validated_calls.append(call)
        try:
            usage = TokenUsage.model_validate(operation.outcome["usage"])
        except Exception:
            return None
        return finish_reason, text, tuple(validated_calls), usage

    @staticmethod
    def _completed_model_terminalization_gap(
        evidence: _RecoveryEvidence,
    ) -> ModelCallOperation | None:
        checkpoint = evidence.checkpoint
        if (
            checkpoint is None
            or checkpoint.phase is not RunCheckpointPhase.READY_FOR_MODEL
            or checkpoint.operation_id is not None
            or evidence.pending
        ):
            return None
        current_operations = tuple(
            operation
            for operation in evidence.operations
            if operation.turn == checkpoint.turn
        )
        if len(current_operations) != 1:
            return None
        operation = current_operations[0]
        if (
            not isinstance(operation, ModelCallOperation)
            or operation.status is not ExternalOperationStatus.COMPLETED
            or operation.outcome is None
            or any(item.turn > checkpoint.turn for item in evidence.operations)
        ):
            return None
        outcome = operation.outcome
        if set(outcome) != {"finish_reason", "text", "tool_calls", "usage"}:
            return None
        finish_reason = outcome["finish_reason"]
        text = outcome["text"]
        if (
            (finish_reason is not None and not isinstance(finish_reason, str))
            or not isinstance(text, str)
            or outcome["tool_calls"] != ()
        ):
            return None
        try:
            operation_usage = TokenUsage.model_validate(outcome["usage"])
        except Exception:
            return None
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            operation_value = getattr(operation_usage, field)
            checkpoint_value = getattr(checkpoint.usage, field)
            if operation_value is not None and (
                checkpoint_value is None or checkpoint_value < operation_value
            ):
                return None
        messages = checkpoint.model_dump(mode="json")["messages"]
        if not messages or messages[-1] != {
            "role": "assistant",
            "content": text or None,
        }:
            return None
        if not "".join(checkpoint.output_parts).endswith(text):
            return None

        model_operations = tuple(
            item
            for item in evidence.operations
            if isinstance(item, ModelCallOperation)
        )
        started_events = tuple(
            event for event in evidence.run_events if event.type == "model.call.started"
        )
        completed_events = tuple(
            event
            for event in evidence.run_events
            if event.type == "model.call.completed"
        )
        failed_events = tuple(
            event for event in evidence.run_events if event.type == "model.call.failed"
        )
        if (
            len(started_events) != len(model_operations)
            or len(completed_events)
            != sum(
                item.status is ExternalOperationStatus.COMPLETED
                for item in model_operations
            )
            or len(failed_events)
            != sum(
                item.status is ExternalOperationStatus.FAILED
                for item in model_operations
            )
            or not completed_events
            or completed_events[-1].payload != {"finish_reason": finish_reason}
        ):
            return None
        completion_index = evidence.run_events.index(completed_events[-1])
        trailing_types = tuple(
            event.type for event in evidence.run_events[completion_index + 1 :]
        )
        if trailing_types != ("step.completed", "run.interrupted"):
            return None
        return operation

    @staticmethod
    def _matching_in_flight_operation(
        evidence: _RecoveryEvidence,
    ) -> ExternalOperation | None:
        checkpoint = evidence.checkpoint
        assert checkpoint is not None
        expected_kind = (
            ExternalOperationKind.MODEL_CALL
            if checkpoint.phase is RunCheckpointPhase.MODEL_IN_FLIGHT
            else ExternalOperationKind.TOOL_CALL
        )
        started = tuple(
            operation
            for operation in evidence.operations
            if operation.status is ExternalOperationStatus.STARTED
        )
        if len(started) != 1:
            return None
        operation = started[0]
        if (
            checkpoint.operation_id != operation.operation_id
            or checkpoint.turn != operation.turn
            or operation.operation_kind is not expected_kind
        ):
            return None
        return operation

    def _certified_provider_request(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        operation: ModelCallOperation,
    ) -> ProviderRecoveryRequest | None:
        checkpoint = evidence.checkpoint
        if (
            not self._is_valid_run_event_envelope(evidence)
            or not self._is_valid_certified_provider_history(evidence)
            or checkpoint is None
            or evidence.pending
            or operation.provider_identity != base_request.model
        ):
            return None
        adapter = self._provider_recovery.resolve(operation.provider_identity)
        if adapter is None:
            return None
        metadata = operation.recovery_metadata
        expected_metadata = {
            "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.version,
            "authoritative_status": adapter.authoritative_status,
            "same_operation_id_resend": adapter.same_operation_id_resend,
        }
        if (
            dict(metadata) != expected_metadata
            or type(metadata.get("authoritative_status")) is not bool
            or type(metadata.get("same_operation_id_resend")) is not bool
            or not (adapter.authoritative_status or adapter.same_operation_id_resend)
        ):
            return None
        checkpoint_data = checkpoint.model_dump(mode="json")
        reconstructed = ModelRequest(
            model=base_request.model,
            messages=tuple(checkpoint_data["messages"]),
            tools=base_request.tools,
            params=base_request.params,
            purpose=base_request.purpose,
        )
        try:
            if _model_request_fingerprint(reconstructed) != operation.request_fingerprint:
                return None
            return ProviderRecoveryRequest(
                session_id=operation.session_id,
                run_id=operation.run_id,
                turn=operation.turn,
                operation_id=operation.operation_id,
                provider_identity=operation.provider_identity,
                request_fingerprint=operation.request_fingerprint,
                model_request=reconstructed,
            )
        except Exception:
            return None

    def _certified_tool_call(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        operation: ToolCallOperation,
    ) -> _CertifiedToolRecovery | None:
        checkpoint = evidence.checkpoint
        descriptor = evidence.run.execution_descriptor
        if (
            not self._is_valid_run_event_envelope(evidence)
            or checkpoint is None
            or descriptor is None
            or evidence.pending
            or checkpoint.phase is not RunCheckpointPhase.TOOL_IN_FLIGHT
            or checkpoint.operation_id != operation.operation_id
            or checkpoint.turn != operation.turn
        ):
            return None
        try:
            descriptor_data = descriptor.model_dump(mode="json")
            reconstructed_messages = list(descriptor_data["messages"])
            reconstructed_results: list[ToolResult] = []
            reconstructed_output: list[str] = []
            cumulative: dict[str, int | None] = {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
            }
            turns: list[_CertifiedTurnEvidence] = []
            certified: _CertifiedToolRecovery | None = None
            initial_interrupt = next(
                index
                for index, event in enumerate(evidence.run_events)
                if event.type == "run.interrupted"
            )
            historical_completion_events = tuple(
                event
                for event in evidence.run_events[:initial_interrupt]
                if event.type == "tool.call.completed"
            )
            if len(historical_completion_events) != checkpoint.turn:
                return None

            for turn in range(checkpoint.turn + 1):
                current = tuple(
                    item for item in evidence.operations if item.turn == turn
                )
                if not current or not isinstance(current[0], ModelCallOperation):
                    return None
                model_operation = current[0]
                tool_operation = (
                    current[1]
                    if len(current) == 2
                    and isinstance(current[1], ToolCallOperation)
                    else None
                )
                if (
                    len(current) not in {1, 2}
                    or (len(current) == 2 and tool_operation is None)
                    or (turn == checkpoint.turn and tool_operation is None)
                ):
                    return None
                if (
                    model_operation.provider_identity != base_request.model
                    or model_operation.run_id != evidence.run.run_id
                    or model_operation.session_id != evidence.run.session_id
                    or (
                        tool_operation is not None
                        and (
                            tool_operation.run_id != evidence.run.run_id
                            or tool_operation.session_id != evidence.run.session_id
                        )
                    )
                ):
                    return None
                request = ModelRequest(
                    model=base_request.model,
                    messages=tuple(reconstructed_messages),
                    tools=base_request.tools,
                    params=dict(base_request.params),
                    purpose=base_request.purpose,
                )
                if (
                    _model_request_fingerprint(request)
                    != model_operation.request_fingerprint
                ):
                    return None
                completed = self._completed_model_outcome(model_operation)
                if completed is None:
                    return None
                finish_reason, text, raw_calls, operation_usage = completed
                if len(raw_calls) != 1:
                    return None
                raw_call = raw_calls[0]
                call = ToolCallCompleted(
                    index=0,
                    call_id=str(raw_call["call_id"]),
                    name=str(raw_call["name"]),
                    arguments_json=str(raw_call["arguments_json"]),
                )
                if raw_call["index"] != 0:
                    return None
                assistant = {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments_json,
                            },
                        }
                    ],
                }
                reconstructed_messages.append(assistant)
                reconstructed_output.append(text)
                for field in cumulative:
                    value = getattr(operation_usage, field)
                    if value is not None:
                        cumulative[field] = (cumulative[field] or 0) + value

                descriptor_capabilities = tuple(
                    item for item in descriptor.tools if item.spec.name == call.name
                )
                registered: RegisteredTool | None = None
                capability: ToolCapabilityDescriptor | None = None
                decoded: dict[str, Any] | None = None
                arguments_valid = False
                if len(descriptor_capabilities) == 1:
                    registered = self._tools.get(call.name)
                    capability = ToolCapabilityDescriptor.from_spec(registered.spec)
                    if descriptor_capabilities != (capability,):
                        return None
                    try:
                        candidate = json.loads(
                            call.arguments_json,
                            parse_constant=_reject_json_constant,
                        )
                        if not isinstance(candidate, dict):
                            raise ValueError
                        Draft202012Validator(
                            thaw_json(registered.spec.input_schema)
                        ).validate(candidate)
                        decoded = candidate
                        arguments_valid = True
                    except (
                        JSONSchemaValidationError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        pass
                elif descriptor_capabilities:
                    return None

                if tool_operation is not None:
                    if (
                        registered is None
                        or capability is None
                        or decoded is None
                        or not arguments_valid
                    ):
                        return None
                    expected_metadata = (
                        {"safe_retry": False, "retry_class": "unsafe"}
                        if registered.spec.retry_policy is ToolRetryPolicy.NEVER
                        else {
                            "safe_retry": True,
                            "retry_class": registered.spec.retry_policy.value,
                        }
                    )
                    if (
                        tool_operation.tool_identity != capability.capability_hash
                        or dict(tool_operation.recovery_metadata) != expected_metadata
                        or _tool_request_fingerprint(call, capability, decoded)
                        != tool_operation.request_fingerprint
                    ):
                        return None

                result: ToolResult | None = None
                permission_allowed: bool | None = None
                if turn == checkpoint.turn:
                    assert tool_operation is not None
                    assert registered is not None
                    if (
                        tool_operation != operation
                        or tool_operation.status is not ExternalOperationStatus.STARTED
                        or registered.spec.retry_policy is ToolRetryPolicy.NEVER
                    ):
                        return None
                    certified = _CertifiedToolRecovery(
                        call=call,
                        registered=registered,
                    )
                    if descriptor.policy.permission_default == "ask":
                        permission_allowed = True
                else:
                    result = ToolResult.model_validate(
                        historical_completion_events[turn].payload
                    )
                    if result.call_id != call.call_id or result.tool_name != call.name:
                        return None
                    if tool_operation is None:
                        if result.status is ToolResultStatus.FAILED:
                            expected_result = ToolResult.normalized_error(
                                call.call_id,
                                call.name,
                                ToolResultStatus.FAILED,
                                "tool not found",
                            )
                            if descriptor_capabilities or result != expected_result:
                                return None
                        elif result.status is ToolResultStatus.INVALID_ARGUMENTS:
                            expected_result = ToolResult.normalized_error(
                                call.call_id,
                                call.name,
                                ToolResultStatus.INVALID_ARGUMENTS,
                                "invalid tool arguments",
                            )
                            if (
                                len(descriptor_capabilities) != 1
                                or arguments_valid
                                or result != expected_result
                            ):
                                return None
                        elif result.status is ToolResultStatus.DENIED:
                            if (
                                len(descriptor_capabilities) != 1
                                or not arguments_valid
                            ):
                                return None
                            if descriptor.policy.permission_default == "ask":
                                permission_allowed = False
                            elif (
                                descriptor.policy.permission_default != "deny"
                                or result
                                != ToolResult.normalized_error(
                                    call.call_id,
                                    call.name,
                                    ToolResultStatus.DENIED,
                                    "permission denied",
                                )
                            ):
                                return None
                        else:
                            return None
                    else:
                        if (
                            tool_operation.status is ExternalOperationStatus.STARTED
                            or tool_operation.outcome is None
                            or result.status
                            in {
                                ToolResultStatus.DENIED,
                                ToolResultStatus.INVALID_ARGUMENTS,
                            }
                        ):
                            return None
                        expected_status = (
                            ExternalOperationStatus.COMPLETED
                            if result.status is ToolResultStatus.SUCCEEDED
                            else ExternalOperationStatus.FAILED
                        )
                        if (
                            tool_operation.status is not expected_status
                            or result.model_dump(mode="json")
                            != tool_operation.model_dump(mode="json")["outcome"]
                        ):
                            return None
                        if descriptor.policy.permission_default == "ask":
                            permission_allowed = True
                    reconstructed_results.append(result)
                    reconstructed_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "name": call.name,
                            "content": result.content,
                        }
                    )
                turns.append(
                    _CertifiedTurnEvidence(
                        call=call,
                        finish_reason=finish_reason,
                        text=text,
                        usage=operation_usage,
                        result=result,
                        operation=tool_operation,
                        permission_allowed=permission_allowed,
                    )
                )
        except (
            AgentSDKError,
            JSONSchemaValidationError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None
        if (
            certified is None
            or any(item.turn > checkpoint.turn for item in evidence.operations)
            or checkpoint.model_dump(mode="json")["messages"]
            != reconstructed_messages
            or checkpoint.tool_results != tuple(reconstructed_results)
            or checkpoint.usage != TokenUsage(**cumulative)
            or "".join(checkpoint.output_parts) != "".join(reconstructed_output)
            or not self._is_valid_certified_tool_history(evidence, tuple(turns))
        ):
            return None
        initial_interrupt = next(
            (
                index
                for index, event in enumerate(evidence.run_events)
                if event.type == "run.interrupted"
            ),
            -1,
        )
        if initial_interrupt < 0:
            return None
        if not self._is_valid_tool_recovery_tail(
            evidence.run_events[initial_interrupt:],
            operation=operation,
            call=certified.call,
        ):
            return None
        return certified

    def _is_valid_certified_tool_history(
        self,
        evidence: _RecoveryEvidence,
        turns: tuple[_CertifiedTurnEvidence, ...],
    ) -> bool:
        descriptor = evidence.run.execution_descriptor
        if descriptor is None:
            return False
        try:
            interrupted = next(
                index
                for index, event in enumerate(evidence.run_events)
                if event.type == "run.interrupted"
            )
        except StopIteration:
            return False
        events = evidence.run_events[:interrupted]
        expected_operations = sum(turn.operation is not None for turn in turns)
        expected_permissions = sum(
            turn.permission_allowed is not None for turn in turns
        )
        expected_historical = len(turns) - 1
        run_started = tuple(event for event in events if event.type == "run.started")
        expected_counts = {
            "step.started": len(turns),
            "model.call.started": len(turns),
            "model.call.completed": len(turns),
            "tool.call.proposed": len(turns),
            "tool.call.authorized": expected_operations,
            "tool.call.started": expected_operations,
            "tool.call.completed": expected_historical,
            "step.completed": expected_historical,
            "permission.requested": expected_permissions,
            "permission.resolved": expected_permissions,
        }
        if (
            len(run_started) != 1
            or run_started[0].payload != {"status": RunStatus.RUNNING.value}
            or any(
                sum(event.type == event_type for event in events) != expected
                for event_type, expected in expected_counts.items()
            )
        ):
            return False

        step_positions = tuple(
            index for index, event in enumerate(events) if event.type == "step.started"
        )
        if (
            len(step_positions) != len(turns)
            or any(event.type == "model.call.failed" for event in events)
        ):
            return False

        for index, turn in enumerate(turns):
            start = step_positions[index]
            end = (
                step_positions[index + 1]
                if index + 1 < len(step_positions)
                else len(events)
            )
            turn_events = tuple(enumerate(events[start:end], start=start))

            def selected(event_type: str) -> tuple[tuple[int, EventEnvelope], ...]:
                return tuple(
                    item for item in turn_events if item[1].type == event_type
                )

            model_started = selected("model.call.started")
            model_completed = selected("model.call.completed")
            proposed = selected("tool.call.proposed")
            authorized = selected("tool.call.authorized")
            tool_started = selected("tool.call.started")
            tool_completed = selected("tool.call.completed")
            step_completed = selected("step.completed")
            permission_requested = selected("permission.requested")
            permission_resolved = selected("permission.resolved")
            historical = turn.result is not None
            has_operation = turn.operation is not None
            if (
                len(model_started) != 1
                or len(model_completed) != 1
                or len(proposed) != 1
                or len(authorized) != (1 if has_operation else 0)
                or len(tool_started) != (1 if has_operation else 0)
                or len(tool_completed) != (1 if historical else 0)
                or len(step_completed) != (1 if historical else 0)
                or len(permission_requested)
                != (1 if turn.permission_allowed is not None else 0)
                or len(permission_resolved)
                != (1 if turn.permission_allowed is not None else 0)
            ):
                return False
            expected_identity = {
                "call_id": turn.call.call_id,
                "tool_name": turn.call.name,
            }
            base_positions = (
                start,
                model_started[0][0],
                model_completed[0][0],
                proposed[0][0],
            )
            if base_positions != tuple(sorted(base_positions)) or len(
                set(base_positions)
            ) != 4:
                return False
            if (
                events[start].payload != {}
                or model_started[0][1].payload != {"model": descriptor.agent.model}
                or model_completed[0][1].payload
                != {"finish_reason": turn.finish_reason}
                or proposed[0][1].payload != expected_identity
            ):
                return False
            deltas = tuple(
                event.payload.get("text")
                for event in events[model_started[0][0] + 1 : model_completed[0][0]]
                if event.type == "model.text.delta"
            )
            if any(not isinstance(delta, str) for delta in deltas) or "".join(
                delta for delta in deltas if isinstance(delta, str)
            ) != turn.text:
                return False
            usage_events = tuple(
                event
                for event in events[model_started[0][0] + 1 : model_completed[0][0]]
                if event.type == "model.usage.reported"
            )
            expected_usage = turn.usage.model_dump(mode="json")
            if (
                (any(value is not None for value in expected_usage.values()))
                != (len(usage_events) == 1)
                or (usage_events and usage_events[0].payload != expected_usage)
            ):
                return False
            preceding_position = proposed[0][0]
            if turn.permission_allowed is not None:
                requested_position, requested_event = permission_requested[0]
                resolved_position, resolved_event = permission_resolved[0]
                if not (
                    preceding_position < requested_position < resolved_position
                ):
                    return False
                try:
                    requested_payload = dict(requested_event.payload)
                    resolved_payload = dict(resolved_event.payload)
                    if set(requested_payload) != {"request"} or set(
                        resolved_payload
                    ) != {"request", "decision"}:
                        return False
                    if requested_payload["request"] != resolved_payload["request"]:
                        return False
                    request_payload = requested_payload["request"]
                    decision_payload = resolved_payload["decision"]
                    request = PermissionRequest.model_validate(request_payload)
                    decision = PermissionDecision.model_validate(decision_payload)
                    if (
                        request.model_dump(mode="json") != request_payload
                        or decision.model_dump(mode="json") != decision_payload
                        or not request.request_id.strip()
                        or request.run_id != evidence.run.run_id
                        or request.session_id != evidence.run.session_id
                        or request.tool_name != turn.call.name
                        or request.arguments
                        != json.loads(turn.call.arguments_json)
                        or request.effects
                        != descriptor.tools[
                            next(
                                tool_index
                                for tool_index, tool in enumerate(descriptor.tools)
                                if tool.spec.name == turn.call.name
                            )
                        ].spec.effects
                        or decision.action not in {"allow", "deny"}
                        or decision.allowed is not turn.permission_allowed
                    ):
                        return False
                    if not turn.permission_allowed:
                        assert turn.result is not None
                        denial = decision.reason or "permission denied"
                        if turn.result != ToolResult.normalized_error(
                            turn.call.call_id,
                            turn.call.name,
                            ToolResultStatus.DENIED,
                            denial,
                        ):
                            return False
                except (KeyError, StopIteration, TypeError, ValueError):
                    return False
                preceding_position = resolved_position
            if has_operation:
                if (
                    authorized[0][1].payload != expected_identity
                    or tool_started[0][1].payload != expected_identity
                    or not (
                        preceding_position
                        < authorized[0][0]
                        < tool_started[0][0]
                    )
                ):
                    return False
                preceding_position = tool_started[0][0]
            if historical:
                assert turn.result is not None
                if not (
                    preceding_position
                    < tool_completed[0][0]
                    < step_completed[0][0]
                ):
                    return False
                if tool_completed[0][1].payload != turn.result.model_dump(
                    mode="json"
                ):
                    return False
                if step_completed[0][0] >= end:
                    return False
        return True

    @staticmethod
    def _is_valid_tool_recovery_tail(
        events: tuple[EventEnvelope, ...],
        *,
        operation: ToolCallOperation,
        call: ToolCallCompleted,
    ) -> bool:
        if not events or events[0].type != "run.interrupted":
            return False
        index = 1
        expected_audit = {
            "operation": hashed_identity(operation.operation_id),
            "call": hashed_identity(call.call_id),
            "tool": hashed_identity(call.name),
            "retry_class": operation.recovery_metadata["retry_class"],
        }
        while index < len(events):
            audit = events[index]
            if (
                audit.type != "tool.recovery.retry.started"
                or audit.payload != expected_audit
            ):
                return False
            index += 1
            if index == len(events):
                return True
            if index < len(events) and events[index].type == "run.recovery.started":
                if events[index].payload != {"status": RunStatus.RUNNING.value}:
                    return False
                index += 1
                if index < len(events) and events[index].type == "permission.requested":
                    requested = events[index].payload
                    if (
                        set(requested) != {"request", "tool"}
                        or requested["tool"] != hashed_identity(call.name)
                        or not RunRecoveryService._is_hashed_identity(
                            requested["request"]
                        )
                    ):
                        return False
                    index += 1
                    permission_resolved = False
                    if (
                        index < len(events)
                        and events[index].type == "permission.resolved"
                    ):
                        resolved = events[index].payload
                        if (
                            set(resolved) != {"request", "tool", "allowed"}
                            or resolved["request"] != requested["request"]
                            or resolved["tool"] != requested["tool"]
                            or type(resolved["allowed"]) is not bool
                        ):
                            return False
                        index += 1
                        permission_resolved = True
                    if (
                        not permission_resolved
                        and index < len(events)
                        and events[index].type == "tool.call.authorized"
                    ):
                        return False
                if index < len(events) and events[index].type == "tool.call.authorized":
                    if events[index].payload != {
                        "call": hashed_identity(call.call_id),
                        "tool": hashed_identity(call.name),
                    }:
                        return False
                    index += 1
            if index >= len(events) or events[index].type != "run.interrupted":
                return False
            index += 1
        return True

    @staticmethod
    def _is_hashed_identity(value: Any) -> bool:
        return (
            isinstance(value, Mapping)
            and set(value) == {"sha256"}
            and isinstance(value["sha256"], str)
            and len(value["sha256"]) == 64
            and all(character in "0123456789abcdef" for character in value["sha256"])
        )

    async def _validated_pending_requests(
        self,
        run: RunSnapshot,
    ) -> tuple[ReconciliationRequest, ...]:
        session_data = await self._store.get_snapshot("session", run.session_id)
        try:
            session = SessionSnapshot.model_validate(session_data)
        except Exception:
            raise self._state_error() from None
        requests = await self._store.list_pending_reconciliation_requests(run.run_id)
        if not requests:
            if run.status is RunStatus.WAITING_RECONCILIATION:
                raise self._state_error() from None
            return ()
        if (
            run.status is not RunStatus.WAITING_RECONCILIATION
            or len(requests) != 1
            or run.run_id not in session.active_run_ids
        ):
            raise self._state_error() from None
        request = requests[0]
        if request.run_id != run.run_id or request.session_id != run.session_id:
            raise self._state_error() from None
        if request.operation_id is not None:
            operation = await self._store.get_external_operation(request.operation_id)
            if (
                operation is None
                or operation.run_id != run.run_id
                or operation.session_id != run.session_id
            ):
                raise self._state_error() from None
        return tuple(
            ReconciliationRequest.model_validate_json(item.model_dump_json())
            for item in requests
        )

    async def _coordinate_reconciliation(
        self,
        run_id: str,
        *,
        reason: str,
        operation_id: str | None,
        details: dict[str, str],
    ) -> RunResult:
        now = self._clock()
        lease = await self._leases.acquire(run_id, new_id("coord"), now=now)
        try:
            run = await self._load_run(run_id)
            session_data = await self._store.get_snapshot("session", run.session_id)
            try:
                session = SessionSnapshot.model_validate(session_data)
            except Exception:
                raise self._state_error() from None
            if run.run_id not in session.active_run_ids:
                raise self._state_error() from None
            pending = await self._store.list_pending_reconciliation_requests(run_id)
            if pending:
                await self._validated_pending_requests(run)
                raise self._recovery_required() from None
            sequence = await self._latest_reconciliation_sequence(run_id)
            request = ReconciliationRequest(
                request_id=new_id("rec"),
                session_id=run.session_id,
                run_id=run.run_id,
                operation_id=operation_id,
                reason=reason,
                details=details,
            )
            waiting = run.model_copy(
                update={
                    "status": RunStatus.WAITING_RECONCILIATION,
                    "version": max(run.version + 1, 3),
                }
            )
            event = EventEnvelope(
                event_id=new_id("evt"),
                type="reconciliation.requested",
                session_id=run.session_id,
                run_id=run.run_id,
                sequence=sequence + 1,
                payload={
                    "request_id": request.request_id,
                    "operation_id": operation_id,
                    "reason": reason,
                },
                occurred_at=now,
            )
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=lease,
                    now=now,
                    events=(event,),
                    snapshots=(
                        SnapshotWrite(
                            "run",
                            waiting.run_id,
                            waiting.session_id,
                            waiting.version,
                            waiting.model_dump(mode="json"),
                        ),
                    ),
                    preconditions=(
                        exact_session_precondition(session),
                        exact_run_precondition(run),
                    ),
                    reconciliation=ReconciliationRequestWrite(None, request),
                ),
            )
        finally:
            active_error = sys.exception()
            release = asyncio.create_task(self._leases.release(lease))
            cancellation = await _settle_task(release)
            if active_error is None and cancellation is not None:
                raise cancellation from None
        raise self._recovery_required() from None

    async def _coordinate_provider_recovery(
        self,
        plan: RecoveryPlan,
    ) -> RunResult:
        now = self._clock()
        lease = await self._leases.acquire(plan.run_id, new_id("coord"), now=now)
        owner = asyncio.current_task()
        assert owner is not None
        heartbeat_error: BaseException | None = None
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        shutdown: asyncio.Future[object] | None = (
            None
            if self._wait_stopping is None
            else asyncio.ensure_future(self._wait_stopping())
        )

        def heartbeat_finished(task: asyncio.Task[None]) -> None:
            nonlocal heartbeat_error
            if task.cancelled():
                return
            heartbeat_error = task.exception()
            if heartbeat_error is not None and not owner.done():
                owner.cancel()

        heartbeat.add_done_callback(heartbeat_finished)

        def shutdown_finished(task: asyncio.Future[object]) -> None:
            if not task.cancelled() and task.exception() is None and not owner.done():
                owner.cancel()

        if shutdown is not None:
            shutdown.add_done_callback(shutdown_finished)
        try:
            run = await self._load_run(plan.run_id)
            if run.status is not RunStatus.INTERRUPTED:
                raise RecoveryStateConflictError
            evidence = await self._load_evidence(run)
            base_request = await self._validated_request(evidence)
            if base_request is None:
                raise RecoveryStateConflictError
            linked = self._matching_in_flight_operation(evidence)
            checkpoint = plan.checkpoint
            if (
                not isinstance(linked, ModelCallOperation)
                or linked.operation_id != plan.operation_id
                or checkpoint is None
                or evidence.checkpoint != checkpoint
            ):
                raise RecoveryStateConflictError
            provider_request = self._certified_provider_request(
                evidence,
                base_request,
                linked,
            )
            if provider_request is None:
                raise RecoveryStateConflictError
            adapter = self._provider_recovery.resolve(linked.provider_identity)
            if adapter is None:
                raise RecoveryStateConflictError
            action: Literal["query", "resend"] = (
                "query" if adapter.authoritative_status else "resend"
            )
            refenced = linked.model_copy(
                update={"lease_generation": lease.generation}
            )
            sequence = await self._store.latest_run_event_sequence(run.run_id)
            if sequence is None:
                raise RecoveryStateConflictError
            await self._commit_provider_audit_start(
                lease=lease,
                run=run,
                session=evidence.session,
                checkpoint=checkpoint,
                expected_operation=linked,
                refenced_operation=refenced,
                adapter=adapter,
                action=action,
                sequence=sequence + 1,
            )
            await self._leases.assert_current(lease, now=self._clock())
            result, error_category = await self._invoke_provider_adapter(
                adapter,
                provider_request,
                action=action,
            )
            if (
                action == "query"
                and result is not None
                and result.disposition is ProviderRecoveryDisposition.NOT_EXECUTED
                and adapter.same_operation_id_resend
            ):
                sequence += 1
                await self._commit_provider_audit_start(
                    lease=lease,
                    run=run,
                    session=evidence.session,
                    checkpoint=checkpoint,
                    expected_operation=None,
                    refenced_operation=None,
                    adapter=adapter,
                    action="resend",
                    sequence=sequence + 1,
                )
                await self._leases.assert_current(lease, now=self._clock())
                action = "resend"
                result, error_category = await self._invoke_provider_adapter(
                    adapter,
                    provider_request,
                    action=action,
                )
            if result is not None and result.disposition is ProviderRecoveryDisposition.COMPLETED:
                sequence = await self._store.latest_run_event_sequence(run.run_id)
                if sequence is None:
                    raise RecoveryStateConflictError
                return await self._engine.resume_recovered_model(
                    run,
                    evidence.session,
                    checkpoint,
                    refenced,
                    provider_request.model_request,
                    result,
                    lease,
                    sequence=sequence + 1,
                )
            if result is not None and result.disposition is ProviderRecoveryDisposition.FAILED:
                sequence = await self._store.latest_run_event_sequence(run.run_id)
                if sequence is None:
                    raise RecoveryStateConflictError
                return await self._engine.fail_recovered_model(
                    run,
                    checkpoint,
                    refenced,
                    result,
                    lease,
                    sequence=sequence + 1,
                )
            disposition = (
                result.disposition.value if result is not None else "invalid"
            )
            details = {"action": action, "disposition": disposition}
            if error_category is not None:
                details["error_category"] = error_category
            return await self._request_reconciliation_owned(
                lease,
                run,
                evidence.session,
                reason="provider_recovery_unresolved",
                operation_id=refenced.operation_id,
                details=details,
                checkpoint=checkpoint,
            )
        except asyncio.CancelledError:
            if heartbeat_error is not None:
                raise LeaseLostError from None
            raise
        finally:
            active_error = sys.exception()
            heartbeat.cancel()
            heartbeat_cancellation = await _settle_task(heartbeat)
            shutdown_cancellation = None
            if shutdown is not None:
                shutdown.cancel()
                shutdown_cancellation = await _settle_task(shutdown)
            release = asyncio.create_task(self._leases.release(lease))
            cancellation = await _settle_task(release)
            if cancellation is None:
                cancellation = heartbeat_cancellation
            if cancellation is None:
                cancellation = shutdown_cancellation
            if active_error is None and cancellation is not None:
                raise cancellation from None

    async def _coordinate_tool_recovery(
        self,
        plan: RecoveryPlan,
    ) -> RunResult:
        now = self._clock()
        lease = await self._leases.acquire(plan.run_id, new_id("coord"), now=now)
        owner = asyncio.current_task()
        assert owner is not None
        heartbeat_error: BaseException | None = None
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        shutdown: asyncio.Future[object] | None = (
            None
            if self._wait_stopping is None
            else asyncio.ensure_future(self._wait_stopping())
        )

        def heartbeat_finished(task: asyncio.Task[None]) -> None:
            nonlocal heartbeat_error
            if task.cancelled():
                return
            heartbeat_error = task.exception()
            if heartbeat_error is not None and not owner.done():
                owner.cancel()

        heartbeat.add_done_callback(heartbeat_finished)

        def shutdown_finished(task: asyncio.Future[object]) -> None:
            if not task.cancelled() and task.exception() is None and not owner.done():
                owner.cancel()

        if shutdown is not None:
            shutdown.add_done_callback(shutdown_finished)
        try:
            run = await self._load_run(plan.run_id)
            if run.status is not RunStatus.INTERRUPTED:
                raise RecoveryStateConflictError
            evidence = await self._load_evidence(run)
            checkpoint = plan.checkpoint
            if (
                checkpoint is None
                or evidence.checkpoint != checkpoint
            ):
                raise RecoveryStateConflictError
            linked = self._matching_in_flight_operation(evidence)
            if (
                not isinstance(linked, ToolCallOperation)
                or linked.operation_id != plan.operation_id
            ):
                raise RecoveryStateConflictError
            try:
                base_request = await self._validated_request(evidence)
            except AgentSDKError:
                return await self._request_reconciliation_owned(
                    lease,
                    run,
                    evidence.session,
                    reason="recovery_state_invalid",
                    operation_id=linked.operation_id,
                    details={"checkpoint_phase": checkpoint.phase.value},
                    checkpoint=checkpoint,
                )
            if base_request is None:
                return await self._request_reconciliation_owned(
                    lease,
                    run,
                    evidence.session,
                    reason="recovery_state_invalid",
                    operation_id=linked.operation_id,
                    details={"checkpoint_phase": checkpoint.phase.value},
                    checkpoint=checkpoint,
                )
            certified = self._certified_tool_call(evidence, base_request, linked)
            if certified is None:
                return await self._request_reconciliation_owned(
                    lease,
                    run,
                    evidence.session,
                    reason="recovery_state_invalid",
                    operation_id=linked.operation_id,
                    details={"checkpoint_phase": checkpoint.phase.value},
                    checkpoint=checkpoint,
                )
            refenced = linked.model_copy(
                update={"lease_generation": lease.generation}
            )
            sequence = await self._store.latest_run_event_sequence(run.run_id)
            if sequence is None:
                raise RecoveryStateConflictError
            event = EventEnvelope(
                event_id=new_id("evt"),
                type="tool.recovery.retry.started",
                session_id=run.session_id,
                run_id=run.run_id,
                sequence=sequence + 1,
                payload={
                    "operation": hashed_identity(linked.operation_id),
                    "call": hashed_identity(certified.call.call_id),
                    "tool": hashed_identity(certified.call.name),
                    "retry_class": linked.recovery_metadata["retry_class"],
                },
                occurred_at=self._clock(),
            )
            await _commit_progress(
                self._store,
                RunProgressBatch(
                    lease=lease,
                    now=self._clock(),
                    events=(event,),
                    preconditions=(
                        exact_session_precondition(evidence.session),
                        exact_run_precondition(run),
                    ),
                    operation=ExternalOperationWrite(linked, refenced),
                    checkpoint_precondition=checkpoint,
                ),
            )
            await self._leases.assert_current(lease, now=self._clock())
            try:
                current_registered = self._tools.get(certified.call.name)
            except AgentSDKError:
                return await self._reconcile_tool_conflict_owned(
                    lease,
                    run.run_id,
                    linked.operation_id,
                )
            if current_registered is not certified.registered:
                return await self._reconcile_tool_conflict_owned(
                    lease,
                    run.run_id,
                    linked.operation_id,
                )
            sequence = await self._store.latest_run_event_sequence(run.run_id)
            if sequence is None:
                raise RecoveryStateConflictError
            try:
                return await self._engine.resume_recovered_tool(
                    run,
                    evidence.session,
                    checkpoint,
                    refenced,
                    certified.call,
                    certified.registered,
                    base_request,
                    lease,
                    sequence=sequence + 1,
                )
            except RecoveryStateConflictError:
                return await self._reconcile_tool_conflict_owned(
                    lease,
                    run.run_id,
                    linked.operation_id,
                )
        except asyncio.CancelledError:
            if heartbeat_error is not None:
                raise LeaseLostError from None
            raise
        finally:
            active_error = sys.exception()
            heartbeat.cancel()
            heartbeat_cancellation = await _settle_task(heartbeat)
            shutdown_cancellation = None
            if shutdown is not None:
                shutdown.cancel()
                shutdown_cancellation = await _settle_task(shutdown)
            release = asyncio.create_task(self._leases.release(lease))
            cancellation = await _settle_task(release)
            if cancellation is None:
                cancellation = heartbeat_cancellation
            if cancellation is None:
                cancellation = shutdown_cancellation
            if active_error is None and cancellation is not None:
                raise cancellation from None

    async def _reconcile_tool_conflict_owned(
        self,
        lease: Lease,
        run_id: str,
        operation_id: str,
    ) -> RunResult:
        await self._leases.assert_current(lease, now=self._clock())
        run = await self._load_run(run_id)
        evidence = await self._load_evidence(run)
        checkpoint = evidence.checkpoint
        details = {
            "checkpoint_phase": (
                "missing" if checkpoint is None else checkpoint.phase.value
            )
        }
        return await self._request_reconciliation_owned(
            lease,
            run,
            evidence.session,
            reason="recovery_state_invalid",
            operation_id=operation_id,
            details=details,
            checkpoint=checkpoint,
        )

    async def _heartbeat(self, lease: Lease) -> None:
        while True:
            await self._sleep(self._heartbeat_interval)
            lease = await self._leases.renew(lease, now=self._clock())

    async def _commit_provider_audit_start(
        self,
        *,
        lease: Lease,
        run: RunSnapshot,
        session: SessionSnapshot,
        checkpoint: RunCheckpoint,
        expected_operation: ModelCallOperation | None,
        refenced_operation: ModelCallOperation | None,
        adapter: ProviderRecoveryAdapter,
        action: Literal["query", "resend"],
        sequence: int,
    ) -> None:
        now = self._clock()
        event = EventEnvelope(
            event_id=new_id("evt"),
            type=f"model.recovery.{action}.started",
            session_id=run.session_id,
            run_id=run.run_id,
            sequence=sequence,
            payload={
                "adapter_id": adapter.adapter_id,
                "adapter_version": adapter.version,
                "operation_id": checkpoint.operation_id,
                "action": action,
            },
            occurred_at=now,
        )
        operation_write = None
        if expected_operation is not None and refenced_operation is not None:
            operation_write = ExternalOperationWrite(
                expected_operation,
                refenced_operation,
            )
        await _commit_progress(
            self._store,
            RunProgressBatch(
                lease=lease,
                now=now,
                events=(event,),
                preconditions=(
                    exact_session_precondition(session),
                    exact_run_precondition(run),
                ),
                operation=operation_write,
                checkpoint_precondition=checkpoint,
            ),
        )

    async def _invoke_provider_adapter(
        self,
        adapter: ProviderRecoveryAdapter,
        request: ProviderRecoveryRequest,
        *,
        action: Literal["query", "resend"],
    ) -> tuple[ProviderRecoveryResult | None, str | None]:
        callback = adapter.query_status if action == "query" else adapter.resend
        assert callback is not None
        awaitable: Awaitable[ProviderRecoveryResult] | None = None
        value: object | None = None
        failed = False
        task: asyncio.Future[ProviderRecoveryResult] | None = None
        try:
            awaitable = callback(request)
            task = asyncio.ensure_future(awaitable)
            async with asyncio.timeout(self._adapter_timeout):
                value = await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            if task is not None:
                task.cancel()
                await _settle_task(task)
            del callback, request, adapter, awaitable, value, task
            raise cancellation from None
        except TimeoutError:
            if task is not None:
                task.cancel()
                await _settle_task(task)
            failed = True
            error_category = "timeout"
        except Exception:
            if task is not None:
                await _settle_task(task)
            failed = True
            error_category = "adapter_failure"
        if failed:
            del callback, request, adapter, awaitable, value, task
            return None, error_category
        result: ProviderRecoveryResult | None = None
        detached: ProviderRecoveryResult | None = None
        try:
            if type(value) is not ProviderRecoveryResult:
                raise ValueError("provider recovery result is invalid")
            result = value
            detached = ProviderRecoveryResult(
                disposition=result.disposition,
                finish_reason=result.finish_reason,
                text=result.text,
                tool_call=result.tool_call,
                usage=result.usage,
                error_code=result.error_code,
                retryable=result.retryable,
            )
        except Exception:
            del callback, request, adapter, awaitable, value, result, detached, task
            return None, "invalid_result"
        assert detached is not None
        del callback, request, adapter, awaitable, result, value, task
        return detached, None

    async def _request_reconciliation_owned(
        self,
        lease: Lease,
        run: RunSnapshot,
        session: SessionSnapshot,
        *,
        reason: str,
        operation_id: str | None,
        details: dict[str, str],
        checkpoint: RunCheckpoint | None,
    ) -> RunResult:
        pending = await self._store.list_pending_reconciliation_requests(run.run_id)
        if pending:
            await self._validated_pending_requests(run)
            raise self._recovery_required() from None
        now = self._clock()
        sequence = await self._latest_reconciliation_sequence(run.run_id)
        request = ReconciliationRequest(
            request_id=new_id("rec"),
            session_id=run.session_id,
            run_id=run.run_id,
            operation_id=operation_id,
            reason=reason,
            details=details,
        )
        waiting = run.model_copy(
            update={
                "status": RunStatus.WAITING_RECONCILIATION,
                "version": max(run.version + 1, 3),
            }
        )
        event = EventEnvelope(
            event_id=new_id("evt"),
            type="reconciliation.requested",
            session_id=run.session_id,
            run_id=run.run_id,
            sequence=sequence + 1,
            payload={
                "request_id": request.request_id,
                "operation_id": operation_id,
                "reason": reason,
            },
            occurred_at=now,
        )
        await _commit_progress(
            self._store,
            RunProgressBatch(
                lease=lease,
                now=now,
                events=(event,),
                snapshots=(
                    SnapshotWrite(
                        "run",
                        waiting.run_id,
                        waiting.session_id,
                        waiting.version,
                        waiting.model_dump(mode="json"),
                    ),
                ),
                preconditions=(
                    exact_session_precondition(session),
                    exact_run_precondition(run),
                ),
                reconciliation=ReconciliationRequestWrite(None, request),
                checkpoint_precondition=checkpoint,
            ),
        )
        raise self._recovery_required() from None

    async def _latest_reconciliation_sequence(self, run_id: str) -> int:
        try:
            return await self._store.latest_run_event_sequence(run_id) or 0
        except RecoveryStateConflictError:
            up_to_cursor = await self._store.latest_cursor()
            events = await self._store.read_events(
                after_cursor=0,
                up_to_cursor=up_to_cursor,
            )
            return max(
                (
                    stored.event.sequence
                    for stored in events
                    if stored.event.run_id == run_id
                    and type(stored.event.sequence) is int
                    and stored.event.sequence > 0
                ),
                default=0,
            )

    @staticmethod
    def _capability_error() -> AgentSDKError:
        return AgentSDKError(
            ErrorCode.INVALID_STATE,
            "recovery capabilities unavailable",
            retryable=False,
        )

    @staticmethod
    def _state_error() -> AgentSDKError:
        return AgentSDKError(
            ErrorCode.INTERNAL,
            "recovery state is invalid",
            retryable=False,
        )

    @staticmethod
    def _recovery_required() -> AgentSDKError:
        return AgentSDKError(
            ErrorCode.CONFLICT,
            "recovery required",
            retryable=True,
        )


class RecoveryScanner:
    def __init__(
        self,
        store: StateStore,
        *,
        lease_manager: LeaseManager | None = None,
        _clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._leases = lease_manager or LeaseManager(store, ttl=_SCANNER_LEASE_TTL)
        self._clock = _clock or (lambda: datetime.now(UTC))
        self._scan_lock = asyncio.Lock()

    async def scan(self) -> None:
        public_error: tuple[ErrorCode, str, bool] | None = None
        try:
            await self._scan_private()
            return
        except asyncio.CancelledError:
            raise
        except AgentSDKError as error:
            public_error = (error.code, error.message, error.retryable)
        except Exception:
            public_error = (
                ErrorCode.INTERNAL,
                "failed to scan abandoned runs",
                False,
            )
        del self
        assert public_error is not None
        raise AgentSDKError(
            public_error[0],
            public_error[1],
            retryable=public_error[2],
        ) from None

    async def _scan_private(self) -> None:
        async with self._scan_lock:
            now = self._clock()
            run_ids = await self._store.list_abandoned_run_ids(now=now)
            for run_id in run_ids:
                await self._scan_run(run_id, now=now)

    async def _scan_run(self, run_id: str, *, now: datetime) -> None:
        try:
            lease = await self._leases.acquire(
                run_id,
                new_id("coord"),
                now=now,
            )
        except LeaseHeldError:
            return
        try:
            await self._interrupt_if_still_abandoned(run_id, lease, now=now)
        finally:
            release = asyncio.create_task(self._leases.release(lease))
            cancellation = await _settle_task(release)
            if cancellation is not None:
                raise cancellation from None

    async def _interrupt_if_still_abandoned(
        self,
        run_id: str,
        lease: Lease,
        *,
        now: datetime,
    ) -> None:
        run_data = await self._store.get_snapshot("run", run_id)
        if run_data is None:
            return
        try:
            run = RunSnapshot.model_validate(run_data)
        except ValueError:
            raise RecoveryStateConflictError from None
        if run.run_id != run_id:
            raise RecoveryStateConflictError
        if run.status not in {
            RunStatus.RUNNING,
            RunStatus.WAITING_PERMISSION,
        }:
            return
        session_data = await self._store.get_snapshot("session", run.session_id)
        if session_data is None:
            return
        try:
            session = SessionSnapshot.model_validate(session_data)
        except ValueError:
            raise RecoveryStateConflictError from None
        if (
            session.session_id != run.session_id
            or run.run_id not in session.active_run_ids
        ):
            raise RecoveryStateConflictError
        sequence = await self._store.latest_run_event_sequence(run.run_id)
        interrupted = run.model_copy(
            update={
                "status": RunStatus.INTERRUPTED,
                "version": run.version + 1,
            }
        )
        event = EventEnvelope(
            event_id=new_id("evt"),
            type="run.interrupted",
            session_id=run.session_id,
            run_id=run.run_id,
            sequence=1 if sequence is None else sequence + 1,
            payload={"status": RunStatus.INTERRUPTED.value},
            occurred_at=now,
        )
        batch = RunProgressBatch(
            lease=lease,
            now=now,
            events=(event,),
            snapshots=(
                SnapshotWrite(
                    "run",
                    interrupted.run_id,
                    interrupted.session_id,
                    interrupted.version,
                    interrupted.model_dump(mode="json"),
                ),
            ),
            preconditions=(
                exact_session_precondition(session),
                exact_run_precondition(run),
            ),
        )
        try:
            await _commit_progress(self._store, batch)
        except RecoveryStateConflictError:
            return


async def _commit_progress(
    store: StateStore,
    batch: RunProgressBatch,
) -> CommitResult:
    first = asyncio.create_task(store.commit_run_progress(batch))
    try:
        return await asyncio.shield(first)
    except asyncio.CancelledError as cancellation:
        await _settle_task(first)
        if (
            first.done()
            and not first.cancelled()
            and first.exception() is not None
            and not isinstance(first.exception(), RecoveryStateConflictError)
        ):
            replay = asyncio.create_task(store.commit_run_progress(batch))
            await _settle_task(replay)
        raise cancellation from None
    except RecoveryStateConflictError:
        raise
    except Exception as first_error:
        del first_error

    replay = asyncio.create_task(store.commit_run_progress(batch))
    try:
        return await asyncio.shield(replay)
    except asyncio.CancelledError as cancellation:
        await _settle_task(replay)
        raise cancellation from None
    except RecoveryStateConflictError:
        raise
    except Exception as replay_error:
        del replay_error
    raise AgentSDKError(
        ErrorCode.INTERNAL,
        "failed to commit interrupted run",
        retryable=False,
    ) from None


async def _settle_task(
    task: asyncio.Future[Any],
) -> asyncio.CancelledError | None:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.done() and task.cancelled():
                break
            if cancellation is None:
                cancellation = error
        except Exception:
            break
    if task.done() and not task.cancelled():
        task.exception()
    return cancellation


async def _yield_once() -> None:
    await asyncio.sleep(0)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
