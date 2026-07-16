from __future__ import annotations

import asyncio
import json
import math
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
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
    _add_usage,
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
    RunFailure,
    RunResult,
    RunSnapshot,
    RunStatus,
    SessionSnapshot,
    SessionStatus,
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
    ReconciliationAction,
    ReconciliationRequest,
    ReconciliationResolution,
    ReconciliationStatus,
    RecoveryStateConflictError,
    RunCheckpoint,
    RunCheckpointPhase,
    ToolCallOperation,
)
from agent_sdk.runtime.session_lifecycle import (
    detach_run_transition,
    exact_run_precondition,
    exact_session_precondition,
    session_write,
)
from agent_sdk.storage.base import (
    canonical_snapshot_data,
    CommitResult,
    EventPrecondition,
    ExternalOperationWrite,
    ReconciliationRequestWrite,
    RunCheckpointWrite,
    RunProgressBatch,
    RunRecoveryEvidencePrecondition,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.tools.models import ToolResult, ToolResultStatus, ToolRetryPolicy, thaw_json
from agent_sdk.tools.registry import RegisteredTool, ToolRegistry


_SCANNER_LEASE_TTL = timedelta(seconds=30)
_FOLLOWER_POLL_INTERVAL_SECONDS = 0.05

_CERTIFIED_RUN_EVENT_TYPES = frozenset(
    {
        "run.created",
        "run.started",
        "run.recovery.started",
        "run.interrupted",
        "step.started",
        "step.completed",
        "step.failed",
        "model.call.started",
        "model.text.delta",
        "model.usage.reported",
        "model.call.completed",
        "model.call.failed",
        "tool.call.proposed",
        "permission.requested",
        "permission.resolved",
        "tool.call.authorized",
        "tool.call.started",
        "tool.call.completed",
        "model.recovery.query.started",
        "model.recovery.resend.started",
        "tool.recovery.retry.started",
        "reconciliation.requested",
        "reconciliation.resolved",
        "run.completed",
        "run.failed",
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
    provider_adapter: ProviderRecoveryAdapter | None = None


@dataclass(frozen=True)
class _RecoveryEvidence:
    run: RunSnapshot
    session: SessionSnapshot
    checkpoint: RunCheckpoint | None
    operations: tuple[ExternalOperation, ...]
    pending: tuple[ReconciliationRequest, ...]
    reconciliations: tuple[ReconciliationRequest, ...]
    run_events: tuple[EventEnvelope, ...]
    run_event_cursors: tuple[int, ...]
    session_lifecycle_events: tuple[EventEnvelope, ...]
    session_lifecycle_event_cursors: tuple[int, ...]
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
                planned_adapter = self._provider_recovery.resolve(
                    linked.provider_identity
                )
                if provider_request is not None and planned_adapter is not None:
                    return RecoveryPlan(
                        "provider_recovery",
                        run_id,
                        request=provider_request.model_request,
                        checkpoint=checkpoint,
                        operation_id=linked.operation_id,
                        provider_adapter=planned_adapter,
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
        if self._is_safe_checkpoint(evidence, request):
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

    async def _certify_terminal_run_for_workflow(
        self,
        run_id: str,
    ) -> tuple[RunSnapshot, RunRecoveryEvidencePrecondition]:
        public_error: tuple[ErrorCode, str, bool] | None = None
        try:
            run = await self._load_run(run_id)
            if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                raise self._state_error() from None
            evidence = await self._load_evidence(
                run,
                allow_terminal_detached=True,
            )
            checkpoint = evidence.checkpoint
            base_request = await self._validated_request(evidence)
            effective = (
                None
                if base_request is None
                else self._effective_resolved_evidence(evidence, base_request)
            )
            confirmed_requests = tuple(
                request
                for request in evidence.reconciliations
                if request.resolution is not None
                and request.resolution.action
                is ReconciliationAction.CONFIRM_COMPLETED
            )
            operations_by_id = {
                operation.operation_id: operation for operation in evidence.operations
            }

            def confirmed_request_is_certified(
                request: ReconciliationRequest,
            ) -> bool:
                if base_request is None:
                    return False
                operation_id = request.operation_id
                if operation_id is None:
                    return False
                operation = operations_by_id.get(operation_id)
                if isinstance(operation, ModelCallOperation):
                    return self._is_exact_confirmed_model_replay(
                        evidence,
                        base_request,
                        request,
                        operation,
                    ) and self._is_confirmed_replay_closed_world(
                        evidence,
                        base_request,
                        request,
                        operation,
                    )
                if isinstance(operation, ToolCallOperation):
                    return self._is_confirmed_tool_replay_closed_world(
                        evidence,
                        base_request,
                        request,
                        operation,
                    )
                return False

            confirmed_history = (
                base_request is not None
                and bool(confirmed_requests)
                and any(map(confirmed_request_is_certified, confirmed_requests))
            )
            ordinary_history = (
                not confirmed_requests
                and base_request is not None
                and effective is not None
                and self._is_valid_certified_provider_history(
                    effective,
                    base_request=base_request,
                    terminal_status=run.status,
                )
            )
            if (
                checkpoint is None
                or checkpoint.phase is not RunCheckpointPhase.TERMINAL
                or checkpoint.operation_id is not None
                or evidence.pending
                or base_request is None
                or any(
                    operation.status is ExternalOperationStatus.STARTED
                    for operation in evidence.operations
                )
                or not (confirmed_history or ordinary_history)
            ):
                raise self._state_error() from None
            return run, self._recovery_evidence_precondition(evidence)
        except AgentSDKError as error:
            public_error = (error.code, error.message, error.retryable)
        except Exception:
            public_error = (
                ErrorCode.INTERNAL,
                "recovery state is invalid",
                False,
            )
        assert public_error is not None
        raise AgentSDKError(
            public_error[0],
            public_error[1],
            retryable=public_error[2],
        ) from None

    @staticmethod
    def _recovery_evidence_precondition(
        evidence: _RecoveryEvidence,
    ) -> RunRecoveryEvidencePrecondition:
        checkpoint = evidence.checkpoint
        return RunRecoveryEvidencePrecondition(
            run_id=evidence.run.run_id,
            checkpoint_json=(
                None
                if checkpoint is None
                else canonical_snapshot_data(checkpoint.model_dump(mode="json"))
            ),
            operation_jsons=tuple(
                canonical_snapshot_data(operation.model_dump(mode="json"))
                for operation in evidence.operations
            ),
            reconciliation_jsons=tuple(
                canonical_snapshot_data(request.model_dump(mode="json"))
                for request in evidence.reconciliations
            ),
            run_events=tuple(
                (
                    evidence.run_event_cursors[index],
                    canonical_snapshot_data(event.model_dump(mode="json")),
                )
                for index, event in enumerate(evidence.run_events)
            ),
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

    async def resolve(
        self,
        request_id: str,
        action: ReconciliationAction,
        *,
        actor: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> ReconciliationRequest:
        public_error: tuple[ErrorCode, str, bool] | None = None
        cancelled = False
        try:
            return await self._resolve_private(
                request_id,
                action,
                actor=actor,
                evidence=evidence,
            )
        except asyncio.CancelledError:
            cancelled = True
        except AgentSDKError as error:
            public_error = (error.code, error.message, error.retryable)
        except Exception:
            public_error = (
                ErrorCode.INTERNAL,
                "failed to resolve reconciliation request",
                False,
            )
        del self, request_id, action, actor, evidence
        if cancelled:
            raise asyncio.CancelledError from None
        assert public_error is not None
        raise AgentSDKError(
            public_error[0],
            public_error[1],
            retryable=public_error[2],
        ) from None

    async def _resolve_private(
        self,
        request_id: str,
        action: ReconciliationAction,
        *,
        actor: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> ReconciliationRequest:
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or type(action) is not ReconciliationAction
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "reconciliation decision is invalid",
                retryable=False,
            ) from None
        if action is ReconciliationAction.TERMINATE:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "reconciliation action is not supported",
                retryable=False,
            ) from None
        expected_evidence: dict[str, object]
        provider_result: ProviderRecoveryResult | None = None
        tool_result: ToolResult | None = None
        if action is ReconciliationAction.CONFIRM_NOT_EXECUTED:
            expected_evidence = {"disposition": "not_executed"}
        elif action is ReconciliationAction.RETRY:
            expected_evidence = {"acknowledge_duplicate_side_effect_risk": True}
        else:
            assert action is ReconciliationAction.CONFIRM_COMPLETED
            if not isinstance(evidence, Mapping):
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "reconciliation decision is invalid",
                    retryable=False,
                ) from None
            if set(evidence) == {"provider_result"}:
                try:
                    raw_result = evidence["provider_result"]
                    if not isinstance(raw_result, Mapping):
                        raise ValueError
                    encoded_result = json.dumps(
                        dict(raw_result),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    provider_result = ProviderRecoveryResult.model_validate_json(
                        encoded_result
                    )
                    if provider_result.disposition not in {
                        ProviderRecoveryDisposition.COMPLETED,
                        ProviderRecoveryDisposition.FAILED,
                    }:
                        raise ValueError
                except Exception:
                    provider_result = None
            elif set(evidence) == {"tool_result"}:
                tool_result = _strict_tool_result(evidence["tool_result"])
            if provider_result is None and tool_result is None:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "reconciliation decision is invalid",
                    retryable=False,
                ) from None
            expected_evidence = dict(evidence)
        if (
            not isinstance(actor, Mapping)
            or not actor
            or not isinstance(evidence, Mapping)
            or dict(evidence) != expected_evidence
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "reconciliation decision is invalid",
                retryable=False,
            ) from None
        try:
            validated_metadata = ReconciliationResolution(
                action=action,
                actor=actor,
                evidence=evidence,
                decided_at=self._clock(),
                event_id="evt_validation",
            )
        except Exception:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "reconciliation decision is invalid",
                retryable=False,
            ) from None
        request = await self._store.get_reconciliation_request(request_id)
        if request is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "reconciliation request not found",
                retryable=False,
            ) from None
        if request.status is ReconciliationStatus.RESOLVED:
            return await self._validated_exact_resolution_replay(
                request,
                action=action,
                actor=validated_metadata.actor,
                evidence=validated_metadata.evidence,
            )

        lease: Lease | None = None
        try:
            lease = await self._leases.acquire(
                request.run_id,
                new_id("coord"),
                now=self._clock(),
            )
            current = await self._store.get_reconciliation_request(request_id)
            if current is None:
                raise RecoveryStateConflictError
            if current.status is ReconciliationStatus.RESOLVED:
                return await self._validated_exact_resolution_replay(
                    current,
                    action=action,
                    actor=validated_metadata.actor,
                    evidence=validated_metadata.evidence,
                )
            if current != request:
                raise RecoveryStateConflictError
            run = await self._load_run(current.run_id)
            if run.status is not RunStatus.WAITING_RECONCILIATION:
                raise RecoveryStateConflictError
            recovery_evidence = await self._load_evidence(run)
            if recovery_evidence.pending != (current,):
                raise RecoveryStateConflictError
            base_request = await self._validated_request(recovery_evidence)
            checkpoint = recovery_evidence.checkpoint
            operation = next(
                (
                    item
                    for item in recovery_evidence.operations
                    if item.operation_id == current.operation_id
                ),
                None,
            )
            requested = tuple(
                (cursor, event)
                for cursor, event in zip(
                    recovery_evidence.run_event_cursors,
                    recovery_evidence.run_events,
                )
                if event.type == "reconciliation.requested"
                and event.payload.get("request_id") == current.request_id
            )
            confirm_completed = action is ReconciliationAction.CONFIRM_COMPLETED
            confirm_model = confirm_completed and provider_result is not None
            confirm_tool = confirm_completed and tool_result is not None
            terminalization_gap = (
                confirm_model
                and current.reason == "model_call_completed_terminalization_unknown"
            )
            if (
                base_request is None
                or checkpoint is None
                or operation is None
                or not self._is_valid_run_event_envelope(recovery_evidence)
                or (confirm_model and not isinstance(operation, ModelCallOperation))
                or (confirm_tool and not isinstance(operation, ToolCallOperation))
            ):
                raise RecoveryStateConflictError
            if (
                checkpoint.turn != operation.turn
                or current.operation_id != operation.operation_id
                or len(requested) != 1
                or requested[0][1] != recovery_evidence.run_events[-1]
                or requested[0][1].payload
                != {
                    "request_id": current.request_id,
                    "operation_id": current.operation_id,
                    "reason": current.reason,
                }
            ):
                raise RecoveryStateConflictError
            if terminalization_gap:
                pre_request_evidence = replace(
                    recovery_evidence,
                    pending=(),
                    run_events=recovery_evidence.run_events[:-1],
                    run_event_cursors=recovery_evidence.run_event_cursors[:-1],
                )
                if (
                    self._completed_model_terminalization_gap(pre_request_evidence)
                    != operation
                    or checkpoint.operation_id is not None
                    or dict(current.details)
                    != {
                        "checkpoint_phase": RunCheckpointPhase.READY_FOR_MODEL.value,
                        "operation_status": ExternalOperationStatus.COMPLETED.value,
                    }
                ):
                    raise RecoveryStateConflictError
            elif (
                operation.status is not ExternalOperationStatus.STARTED
                or checkpoint.operation_id != operation.operation_id
                or current.reason
                != (
                    "model_call_unknown_outcome"
                    if isinstance(operation, ModelCallOperation)
                    else "tool_call_unknown_outcome"
                )
                or dict(current.details)
                != {"checkpoint_phase": checkpoint.phase.value}
            ):
                raise RecoveryStateConflictError
            if not terminalization_gap:
                effective_evidence = self._effective_resolved_evidence(
                    recovery_evidence,
                    base_request,
                )
                if effective_evidence is None:
                    raise RecoveryStateConflictError
                certified_events = tuple(
                    (cursor, event)
                    for cursor, event in zip(
                        effective_evidence.run_event_cursors,
                        effective_evidence.run_events,
                    )
                    if event.type
                    not in {"reconciliation.requested", "reconciliation.resolved"}
                )
                certified = replace(
                    effective_evidence,
                    pending=(),
                    run_events=tuple(
                        event.model_copy(update={"sequence": index})
                        for index, (_, event) in enumerate(certified_events, start=1)
                    ),
                    run_event_cursors=tuple(range(1, len(certified_events) + 1)),
                )
                if not self._is_resolution_operation_certified(
                    certified,
                    base_request,
                    operation,
                ):
                    raise RecoveryStateConflictError
                if confirm_tool:
                    assert isinstance(operation, ToolCallOperation)
                    assert tool_result is not None
                    certified_tool = self._certified_tool_call(
                        certified,
                        base_request,
                        operation,
                        allow_unsafe=True,
                    )
                    if certified_tool is None:
                        raise RecoveryStateConflictError
                    if (
                        tool_result.call_id,
                        tool_result.tool_name,
                    ) != (
                        certified_tool.call.call_id,
                        certified_tool.call.name,
                    ):
                        raise AgentSDKError(
                            ErrorCode.INVALID_STATE,
                            "reconciliation decision is invalid",
                            retryable=False,
                        ) from None
                if (
                    confirm_model
                    and provider_result is not None
                    and provider_result.disposition
                    is ProviderRecoveryDisposition.COMPLETED
                ):
                    assert isinstance(operation, ModelCallOperation)
                    assert provider_result.text is not None
                    durable_deltas = self._current_model_deltas_before_interrupt(
                        certified,
                        operation,
                    )
                    if (
                        durable_deltas is None
                        or not self._is_exact_durable_text_prefix(
                            durable_deltas,
                            provider_result.text,
                        )
                    ):
                        raise RecoveryStateConflictError

            now = self._clock()
            resolution = ReconciliationResolution(
                action=action,
                actor=validated_metadata.actor,
                evidence=validated_metadata.evidence,
                decided_at=now,
                event_id=new_id("evt"),
            )
            resolved = current.model_copy(
                update={
                    "status": ReconciliationStatus.RESOLVED,
                    "resolution": resolution,
                }
            )
            if confirm_model:
                assert provider_result is not None
                assert isinstance(operation, ModelCallOperation)
                batch_builder = (
                    self._confirmed_model_terminalization_batch
                    if terminalization_gap
                    else self._confirmed_model_resolution_batch
                )
                batch = batch_builder(
                    lease=lease,
                    now=now,
                    run=run,
                    session=recovery_evidence.session,
                    checkpoint=checkpoint,
                    operation=operation,
                    request=current,
                    resolved=resolved,
                    resolution=resolution,
                    requested_cursor=requested[0][0],
                    requested_event=requested[0][1],
                    result=provider_result,
                )
                await _commit_progress(self._store, batch)
                return ReconciliationRequest.model_validate_json(
                    resolved.model_dump_json()
                )
            if confirm_tool:
                assert tool_result is not None
                assert isinstance(operation, ToolCallOperation)
                batch = self._confirmed_tool_resolution_batch(
                    lease=lease,
                    now=now,
                    run=run,
                    session=recovery_evidence.session,
                    checkpoint=checkpoint,
                    operation=operation,
                    request=current,
                    resolved=resolved,
                    resolution=resolution,
                    requested_cursor=requested[0][0],
                    requested_event=requested[0][1],
                    result=tool_result,
                )
                await _commit_progress(self._store, batch)
                return ReconciliationRequest.model_validate_json(
                    resolved.model_dump_json()
                )

            terminalized = operation.model_copy(
                update={
                    "status": ExternalOperationStatus.FAILED,
                    "outcome": {
                        "reconciliation": {
                            "request_id": current.request_id,
                            "action": action.value,
                        }
                    },
                }
            )
            safe_phase = (
                RunCheckpointPhase.READY_FOR_MODEL
                if isinstance(operation, ModelCallOperation)
                else RunCheckpointPhase.READY_FOR_TOOL
            )
            safe_checkpoint = checkpoint.model_copy(
                update={
                    "checkpoint_version": checkpoint.checkpoint_version + 1,
                    "phase": safe_phase,
                    "operation_id": None,
                }
            )
            interrupted = run.model_copy(
                update={
                    "status": RunStatus.INTERRUPTED,
                    "version": run.version + 1,
                }
            )
            event = EventEnvelope(
                event_id=resolution.event_id,
                type="reconciliation.resolved",
                session_id=run.session_id,
                run_id=run.run_id,
                sequence=requested[0][1].sequence + 1,
                payload={
                    "request_id": current.request_id,
                    "operation_id": current.operation_id,
                    "action": action.value,
                    "actor": thaw_json(resolution.actor),
                    "evidence": thaw_json(resolution.evidence),
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
                            interrupted.run_id,
                            interrupted.session_id,
                            interrupted.version,
                            interrupted.model_dump(mode="json"),
                        ),
                    ),
                    preconditions=(
                        exact_session_precondition(recovery_evidence.session),
                        exact_run_precondition(run),
                    ),
                    event_preconditions=(
                        EventPrecondition(
                            requested[0][1].event_id,
                            requested[0][0],
                            requested[0][1].session_id,
                            requested[0][1].run_id,
                            requested[0][1].type,
                            requested[0][1].sequence,
                        ),
                    ),
                    operation=ExternalOperationWrite(operation, terminalized),
                    checkpoint=RunCheckpointWrite(checkpoint, safe_checkpoint),
                    reconciliation=ReconciliationRequestWrite(current, resolved),
                ),
            )
            return ReconciliationRequest.model_validate_json(
                resolved.model_dump_json()
            )
        except LeaseHeldError:
            return await self._follow_resolution(
                request_id,
                action=action,
                actor=validated_metadata.actor,
                evidence=validated_metadata.evidence,
            )
        finally:
            if lease is not None:
                active_error = sys.exception()
                release = asyncio.create_task(self._leases.release(lease))
                cancellation = await _settle_task(release)
                if active_error is None and cancellation is not None:
                    raise cancellation from None

    @staticmethod
    def _confirmed_tool_resolution_batch(
        *,
        lease: Lease,
        now: datetime,
        run: RunSnapshot,
        session: SessionSnapshot,
        checkpoint: RunCheckpoint,
        operation: ToolCallOperation,
        request: ReconciliationRequest,
        resolved: ReconciliationRequest,
        resolution: ReconciliationResolution,
        requested_cursor: int,
        requested_event: EventEnvelope,
        result: ToolResult,
    ) -> RunProgressBatch:
        projected_operation = operation.model_copy(
            update={
                "status": (
                    ExternalOperationStatus.COMPLETED
                    if result.status is ToolResultStatus.SUCCEEDED
                    else ExternalOperationStatus.FAILED
                ),
                "outcome": result.model_dump(mode="json"),
            }
        )
        tool_message = {
            "role": "tool",
            "tool_call_id": result.call_id,
            "name": result.tool_name,
            "content": result.content,
        }
        projected_checkpoint = checkpoint.model_copy(
            update={
                "checkpoint_version": checkpoint.checkpoint_version + 1,
                "turn": checkpoint.turn + 1,
                "phase": RunCheckpointPhase.READY_FOR_MODEL,
                "operation_id": None,
                "messages": (*checkpoint.messages, tool_message),
                "tool_results": (*checkpoint.tool_results, result),
            }
        )
        projected_run = run.model_copy(
            update={
                "status": RunStatus.INTERRUPTED,
                "version": run.version + 1,
            }
        )
        resolution_payload = {
            "request_id": request.request_id,
            "operation_id": request.operation_id,
            "action": resolution.action.value,
            "actor": thaw_json(resolution.actor),
            "evidence": thaw_json(resolution.evidence),
        }
        events = tuple(
            EventEnvelope(
                event_id=resolution.event_id if offset == 1 else new_id("evt"),
                type=event_type,
                session_id=run.session_id,
                run_id=run.run_id,
                sequence=requested_event.sequence + offset,
                payload=payload,
                occurred_at=now,
            )
            for offset, (event_type, payload) in enumerate(
                (
                    ("reconciliation.resolved", resolution_payload),
                    ("tool.call.completed", result.model_dump(mode="json")),
                    ("step.completed", {}),
                ),
                start=1,
            )
        )
        return RunProgressBatch(
            lease=lease,
            now=now,
            events=events,
            snapshots=(
                SnapshotWrite(
                    "run",
                    projected_run.run_id,
                    projected_run.session_id,
                    projected_run.version,
                    projected_run.model_dump(mode="json"),
                ),
            ),
            preconditions=(
                exact_session_precondition(session),
                exact_run_precondition(run),
            ),
            event_preconditions=(
                EventPrecondition(
                    requested_event.event_id,
                    requested_cursor,
                    requested_event.session_id,
                    requested_event.run_id,
                    requested_event.type,
                    requested_event.sequence,
                ),
            ),
            operation=ExternalOperationWrite(operation, projected_operation),
            checkpoint=RunCheckpointWrite(checkpoint, projected_checkpoint),
            reconciliation=ReconciliationRequestWrite(request, resolved),
        )

    @staticmethod
    def _confirmed_model_terminalization_batch(
        *,
        lease: Lease,
        now: datetime,
        run: RunSnapshot,
        session: SessionSnapshot,
        checkpoint: RunCheckpoint,
        operation: ModelCallOperation,
        request: ReconciliationRequest,
        resolved: ReconciliationRequest,
        resolution: ReconciliationResolution,
        requested_cursor: int,
        requested_event: EventEnvelope,
        result: ProviderRecoveryResult,
    ) -> RunProgressBatch:
        if (
            result.disposition is not ProviderRecoveryDisposition.COMPLETED
            or result.text is None
            or result.usage is None
            or result.tool_call is not None
            or operation.outcome is None
            or operation.model_dump(mode="json")["outcome"]
            != {
                "finish_reason": result.finish_reason,
                "text": result.text,
                "tool_calls": [],
                "usage": result.usage.model_dump(mode="json"),
            }
        ):
            raise RecoveryStateConflictError
        terminal_checkpoint = checkpoint.model_copy(
            update={
                "checkpoint_version": checkpoint.checkpoint_version + 1,
                "phase": RunCheckpointPhase.TERMINAL,
            }
        )
        output_text = "".join(checkpoint.output_parts)
        completed_run = run.model_copy(
            update={
                "status": RunStatus.COMPLETED,
                "version": run.version + 1,
                "output_text": output_text,
                "usage": checkpoint.usage,
                "tool_results": checkpoint.tool_results,
            }
        )
        resolved_event = EventEnvelope(
            event_id=resolution.event_id,
            type="reconciliation.resolved",
            session_id=run.session_id,
            run_id=run.run_id,
            sequence=requested_event.sequence + 1,
            payload={
                "request_id": request.request_id,
                "operation_id": request.operation_id,
                "action": resolution.action.value,
                "actor": thaw_json(resolution.actor),
                "evidence": thaw_json(resolution.evidence),
            },
            occurred_at=now,
        )
        terminal_payload: dict[str, Any] = {
            "output_text": output_text,
            "usage": checkpoint.usage.model_dump(mode="json"),
        }
        if checkpoint.tool_results:
            terminal_payload["tool_results"] = [
                item.model_dump(mode="json") for item in checkpoint.tool_results
            ]
        run_event = EventEnvelope(
            event_id=new_id("evt"),
            type="run.completed",
            session_id=run.session_id,
            run_id=run.run_id,
            sequence=requested_event.sequence + 2,
            payload=terminal_payload,
            occurred_at=now,
        )
        updated_session, session_event_type = detach_run_transition(session, run.run_id)
        session_event = EventEnvelope(
            event_id=new_id("evt"),
            type=session_event_type,
            session_id=session.session_id,
            run_id=None,
            sequence=updated_session.version,
            payload={
                "run_id": run.run_id,
                "status": updated_session.status.value,
            },
            occurred_at=now,
        )
        return RunProgressBatch(
            lease=lease,
            now=now,
            events=(resolved_event, run_event, session_event),
            snapshots=(
                SnapshotWrite(
                    "run",
                    completed_run.run_id,
                    completed_run.session_id,
                    completed_run.version,
                    completed_run.model_dump(mode="json"),
                ),
                session_write(updated_session),
            ),
            preconditions=(
                exact_session_precondition(session),
                exact_run_precondition(run),
            ),
            event_preconditions=(
                EventPrecondition(
                    requested_event.event_id,
                    requested_cursor,
                    requested_event.session_id,
                    requested_event.run_id,
                    requested_event.type,
                    requested_event.sequence,
                ),
            ),
            checkpoint=RunCheckpointWrite(checkpoint, terminal_checkpoint),
            reconciliation=ReconciliationRequestWrite(request, resolved),
            operation_precondition=operation,
        )

    @staticmethod
    def _confirmed_model_resolution_batch(
        *,
        lease: Lease,
        now: datetime,
        run: RunSnapshot,
        session: SessionSnapshot,
        checkpoint: RunCheckpoint,
        operation: ModelCallOperation,
        request: ReconciliationRequest,
        resolved: ReconciliationRequest,
        resolution: ReconciliationResolution,
        requested_cursor: int,
        requested_event: EventEnvelope,
        result: ProviderRecoveryResult,
    ) -> RunProgressBatch:
        sequence = requested_event.sequence + 1

        def run_event(event_type: str, payload: dict[str, Any]) -> EventEnvelope:
            nonlocal sequence
            event = EventEnvelope(
                event_id=(resolution.event_id if sequence == requested_event.sequence + 1 else new_id("evt")),
                type=event_type,
                session_id=run.session_id,
                run_id=run.run_id,
                sequence=sequence,
                payload=payload,
                occurred_at=now,
            )
            sequence += 1
            return event

        events: list[EventEnvelope] = [
            run_event(
                "reconciliation.resolved",
                {
                    "request_id": request.request_id,
                    "operation_id": request.operation_id,
                    "action": resolution.action.value,
                    "actor": thaw_json(resolution.actor),
                    "evidence": thaw_json(resolution.evidence),
                },
            )
        ]
        snapshots: list[SnapshotWrite]
        if result.disposition is ProviderRecoveryDisposition.COMPLETED:
            assert result.text is not None
            assert result.usage is not None
            calls = () if result.tool_call is None else (result.tool_call,)
            operation_outcome = {
                "finish_reason": result.finish_reason,
                "text": result.text,
                "tool_calls": [
                    {
                        "index": call.index,
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments_json": call.arguments_json,
                    }
                    for call in calls
                ],
                "usage": result.usage.model_dump(mode="json"),
            }
            projected_operation = operation.model_copy(
                update={
                    "status": ExternalOperationStatus.COMPLETED,
                    "outcome": operation_outcome,
                }
            )
            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": result.text or None,
            }
            if calls:
                assistant["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments_json,
                        },
                    }
                    for call in calls
                ]
            cumulative_usage = _add_usage(checkpoint.usage, result.usage)
            output_parts = (*checkpoint.output_parts, result.text)
            events.extend(
                (
                    run_event(
                        "model.usage.reported",
                        result.usage.model_dump(mode="json"),
                    ),
                    run_event(
                        "model.call.completed",
                        {"finish_reason": result.finish_reason},
                    ),
                )
            )
            if calls:
                projected_checkpoint = checkpoint.model_copy(
                    update={
                        "checkpoint_version": checkpoint.checkpoint_version + 1,
                        "phase": RunCheckpointPhase.READY_FOR_TOOL,
                        "operation_id": None,
                        "messages": (*checkpoint.messages, assistant),
                        "output_parts": output_parts,
                        "usage": cumulative_usage,
                    }
                )
                projected_run = run.model_copy(
                    update={
                        "status": RunStatus.INTERRUPTED,
                        "version": run.version + 1,
                    }
                )
                snapshots = [
                    SnapshotWrite(
                        "run",
                        projected_run.run_id,
                        projected_run.session_id,
                        projected_run.version,
                        projected_run.model_dump(mode="json"),
                    )
                ]
            else:
                projected_checkpoint = checkpoint.model_copy(
                    update={
                        "checkpoint_version": checkpoint.checkpoint_version + 1,
                        "phase": RunCheckpointPhase.TERMINAL,
                        "operation_id": None,
                        "messages": (*checkpoint.messages, assistant),
                        "output_parts": output_parts,
                        "usage": cumulative_usage,
                    }
                )
                output_text = "".join(output_parts)
                projected_run = run.model_copy(
                    update={
                        "status": RunStatus.COMPLETED,
                        "version": run.version + 1,
                        "output_text": output_text,
                        "usage": cumulative_usage,
                        "tool_results": checkpoint.tool_results,
                    }
                )
                events.append(run_event("step.completed", {}))
                terminal_payload: dict[str, Any] = {
                    "output_text": output_text,
                    "usage": cumulative_usage.model_dump(mode="json"),
                }
                if checkpoint.tool_results:
                    terminal_payload["tool_results"] = [
                        item.model_dump(mode="json")
                        for item in checkpoint.tool_results
                    ]
                events.append(run_event("run.completed", terminal_payload))
                updated_session, session_event_type = detach_run_transition(
                    session, run.run_id
                )
                events.append(
                    EventEnvelope(
                        event_id=new_id("evt"),
                        type=session_event_type,
                        session_id=session.session_id,
                        run_id=None,
                        sequence=updated_session.version,
                        payload={
                            "run_id": run.run_id,
                            "status": updated_session.status.value,
                        },
                        occurred_at=now,
                    )
                )
                snapshots = [
                    SnapshotWrite(
                        "run",
                        projected_run.run_id,
                        projected_run.session_id,
                        projected_run.version,
                        projected_run.model_dump(mode="json"),
                    ),
                    session_write(updated_session),
                ]
        else:
            assert result.disposition is ProviderRecoveryDisposition.FAILED
            assert result.error_code is not None
            assert result.retryable is not None
            public_error = {
                "code": result.error_code.value,
                "message": "model call failed",
                "retryable": result.retryable,
            }
            projected_operation = operation.model_copy(
                update={
                    "status": ExternalOperationStatus.FAILED,
                    "outcome": {
                        "error": {
                            "code": result.error_code.value,
                            "message": "model call failed",
                        }
                    },
                }
            )
            projected_checkpoint = checkpoint.model_copy(
                update={
                    "checkpoint_version": checkpoint.checkpoint_version + 1,
                    "phase": RunCheckpointPhase.TERMINAL,
                    "operation_id": None,
                }
            )
            projected_run = run.model_copy(
                update={
                    "status": RunStatus.FAILED,
                    "version": run.version + 1,
                    "output_text": "".join(checkpoint.output_parts),
                    "usage": checkpoint.usage,
                    "tool_results": checkpoint.tool_results,
                    "error": RunFailure(
                        code=result.error_code.value,
                        message="model call failed",
                        retryable=result.retryable,
                    ),
                }
            )
            payload = {"error": public_error}
            events.extend(
                (
                    run_event("model.call.failed", payload),
                    run_event("step.failed", payload),
                    run_event("run.failed", payload),
                )
            )
            updated_session, session_event_type = detach_run_transition(
                session, run.run_id
            )
            events.append(
                EventEnvelope(
                    event_id=new_id("evt"),
                    type=session_event_type,
                    session_id=session.session_id,
                    run_id=None,
                    sequence=updated_session.version,
                    payload={
                        "run_id": run.run_id,
                        "status": updated_session.status.value,
                    },
                    occurred_at=now,
                )
            )
            snapshots = [
                SnapshotWrite(
                    "run",
                    projected_run.run_id,
                    projected_run.session_id,
                    projected_run.version,
                    projected_run.model_dump(mode="json"),
                ),
                session_write(updated_session),
            ]

        return RunProgressBatch(
            lease=lease,
            now=now,
            events=tuple(events),
            snapshots=tuple(snapshots),
            preconditions=(
                exact_session_precondition(session),
                exact_run_precondition(run),
            ),
            event_preconditions=(
                EventPrecondition(
                    requested_event.event_id,
                    requested_cursor,
                    requested_event.session_id,
                    requested_event.run_id,
                    requested_event.type,
                    requested_event.sequence,
                ),
            ),
            operation=ExternalOperationWrite(operation, projected_operation),
            checkpoint=RunCheckpointWrite(checkpoint, projected_checkpoint),
            reconciliation=ReconciliationRequestWrite(request, resolved),
        )

    async def _follow_resolution(
        self,
        request_id: str,
        *,
        action: ReconciliationAction,
        actor: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> ReconciliationRequest:
        deadline = monotonic() + 10.0
        while monotonic() < deadline:
            if self._stopping():
                raise RecoveryStateConflictError
            current = await self._store.get_reconciliation_request(request_id)
            if current is None:
                raise RecoveryStateConflictError
            if current.status is ReconciliationStatus.RESOLVED:
                return await self._validated_exact_resolution_replay(
                    current,
                    action=action,
                    actor=actor,
                    evidence=evidence,
                )
            lease = await self._store.get_run_lease(current.run_id)
            if lease is None or lease.expires_at <= self._clock():
                raise RecoveryStateConflictError
            await self._yield()
        raise RecoveryStateConflictError

    async def _validated_exact_resolution_replay(
        self,
        request: ReconciliationRequest,
        *,
        action: ReconciliationAction,
        actor: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> ReconciliationRequest:
        replay = self._exact_resolution_replay(
            request,
            action=action,
            actor=actor,
            evidence=evidence,
        )
        run = await self._load_run(request.run_id)
        recovery_evidence = await self._load_evidence(
            run,
            allow_terminal_detached=True,
        )
        base_request = await self._validated_request(recovery_evidence)
        matching = tuple(
            item
            for item in recovery_evidence.reconciliations
            if item.request_id == request.request_id
        )
        resolution = request.resolution
        operation = next(
            (
                item
                for item in recovery_evidence.operations
                if item.operation_id == request.operation_id
            ),
            None,
        )
        if resolution is not None and resolution.action is ReconciliationAction.CONFIRM_COMPLETED:
            model_replay = (
                isinstance(operation, ModelCallOperation)
                and self._is_exact_confirmed_model_replay(
                    recovery_evidence,
                    base_request,
                    request,
                    operation,
                )
                and base_request is not None
                and self._is_confirmed_replay_closed_world(
                    recovery_evidence,
                    base_request,
                    request,
                    operation,
                )
            )
            tool_replay = (
                isinstance(operation, ToolCallOperation)
                and base_request is not None
                and self._is_confirmed_tool_replay_closed_world(
                    recovery_evidence,
                    base_request,
                    request,
                    operation,
                )
            )
            if matching != (request,) or not (model_replay or tool_replay):
                raise RecoveryStateConflictError
            return replay

        effective = (
            None
            if base_request is None
            else self._effective_resolved_evidence(
                recovery_evidence,
                base_request,
            )
        )
        if (
            matching != (request,)
            or effective is None
            or resolution is None
            or operation is None
        ):
            raise RecoveryStateConflictError

        if (
            recovery_evidence.run_events
            and recovery_evidence.run_events[-1].event_id == resolution.event_id
        ):
            checkpoint = recovery_evidence.checkpoint
            expected_phase = (
                RunCheckpointPhase.READY_FOR_MODEL
                if isinstance(operation, ModelCallOperation)
                else RunCheckpointPhase.READY_FOR_TOOL
            )
            if (
                run.status is not RunStatus.INTERRUPTED
                or checkpoint is None
                or checkpoint.run_id != run.run_id
                or checkpoint.session_id != run.session_id
                or checkpoint.turn != operation.turn
                or checkpoint.phase is not expected_phase
                or checkpoint.operation_id is not None
            ):
                raise RecoveryStateConflictError
        return replay

    @staticmethod
    def _exact_resolution_replay(
        request: ReconciliationRequest,
        *,
        action: ReconciliationAction,
        actor: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> ReconciliationRequest:
        resolution = request.resolution
        if (
            resolution is None
            or resolution.action is not action
            or resolution.actor != actor
            or resolution.evidence != evidence
        ):
            raise RecoveryStateConflictError
        return ReconciliationRequest.model_validate_json(request.model_dump_json())

    def _is_exact_confirmed_model_replay(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest | None,
        request: ReconciliationRequest,
        operation: ModelCallOperation,
    ) -> bool:
        resolution = request.resolution
        checkpoint = evidence.checkpoint
        if (
            resolution is None
            or base_request is None
            or checkpoint is None
            or resolution.action is not ReconciliationAction.CONFIRM_COMPLETED
            or request.operation_id != operation.operation_id
            or request.run_id != evidence.run.run_id
            or request.session_id != evidence.run.session_id
            or operation.run_id != evidence.run.run_id
            or operation.session_id != evidence.run.session_id
            or checkpoint.run_id != evidence.run.run_id
            or checkpoint.session_id != evidence.run.session_id
            or not self._is_valid_run_event_envelope(
                evidence,
                allow_recovery_closed=True,
            )
        ):
            return False
        try:
            raw_evidence = thaw_json(resolution.evidence)
            if set(raw_evidence) != {"provider_result"}:
                return False
            result = ProviderRecoveryResult.model_validate_json(
                json.dumps(
                    raw_evidence["provider_result"],
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except Exception:
            return False
        requested_positions = tuple(
            index
            for index, event in enumerate(evidence.run_events)
            if event.type == "reconciliation.requested"
            and event.payload
            == {
                "request_id": request.request_id,
                "operation_id": request.operation_id,
                "reason": request.reason,
            }
        )
        resolved_positions = tuple(
            index
            for index, event in enumerate(evidence.run_events)
            if event.type == "reconciliation.resolved"
            and event.event_id == resolution.event_id
            and event.occurred_at == resolution.decided_at
            and event.payload
            == {
                "request_id": request.request_id,
                "operation_id": request.operation_id,
                "action": resolution.action.value,
                "actor": thaw_json(resolution.actor),
                "evidence": raw_evidence,
            }
        )
        if (
            len(requested_positions) != 1
            or len(resolved_positions) != 1
            or resolved_positions[0] != requested_positions[0] + 1
            or requested_positions[0] == 0
            or evidence.run_events[requested_positions[0] - 1].type
            != "run.interrupted"
        ):
            return False
        requested_index = requested_positions[0]
        resolved_index = resolved_positions[0]
        messages_before = self._messages_before_turn(
            evidence,
            base_request,
            operation.turn,
        )
        if messages_before is None:
            return False
        try:
            reconstructed = ModelRequest(
                model=base_request.model,
                messages=messages_before,
                tools=base_request.tools,
                params=dict(base_request.params),
                purpose=base_request.purpose,
            )
        except Exception:
            return False
        metadata = dict(operation.recovery_metadata)
        metadata_valid = metadata == {
            "authoritative_status": False,
            "same_operation_id_resend": False,
        } or (
            set(metadata)
            == {
                "adapter_id",
                "adapter_version",
                "authoritative_status",
                "same_operation_id_resend",
            }
            and all(
                isinstance(metadata[field], str) and bool(metadata[field])
                for field in ("adapter_id", "adapter_version")
            )
            and type(metadata["authoritative_status"]) is bool
            and type(metadata["same_operation_id_resend"]) is bool
        )
        if (
            not metadata_valid
            or operation.provider_identity != base_request.model
            or operation.request_fingerprint
            != _model_request_fingerprint(reconstructed)
        ):
            return False

        prior_output: list[str] = []
        prior_usage = TokenUsage()
        for turn in range(operation.turn):
            matches = tuple(
                item
                for item in evidence.operations
                if isinstance(item, ModelCallOperation)
                and item.turn == turn
                and item.status is ExternalOperationStatus.COMPLETED
            )
            if len(matches) != 1:
                return False
            completed = self._completed_model_outcome(matches[0])
            if completed is None:
                return False
            if completed[1]:
                prior_output.append(completed[1])
            prior_usage = _add_usage(prior_usage, completed[3])
        trailing = evidence.run_events[resolved_index + 1 :]
        if request.reason == "model_call_completed_terminalization_unknown":
            if (
                result.disposition is not ProviderRecoveryDisposition.COMPLETED
                or result.text is None
                or result.usage is None
                or result.tool_call is not None
                or operation.model_dump(mode="json")["outcome"]
                != {
                    "finish_reason": result.finish_reason,
                    "text": result.text,
                    "tool_calls": [],
                    "usage": result.usage.model_dump(mode="json"),
                }
                or checkpoint.turn != operation.turn
                or len(checkpoint.tool_results) != operation.turn
                or tuple(event.type for event in trailing) != ("run.completed",)
            ):
                return False
            output_parts = (*prior_output, result.text)
            usage = _add_usage(prior_usage, result.usage)
            terminal_payload: dict[str, Any] = {
                "output_text": "".join(output_parts),
                "usage": usage.model_dump(mode="json"),
            }
            if checkpoint.tool_results:
                terminal_payload["tool_results"] = [
                    item.model_dump(mode="json")
                    for item in checkpoint.tool_results
                ]
            if (
                trailing[0].payload != terminal_payload
                or not self._is_exact_resolution_event_batch(
                    evidence,
                    first_run_index=resolved_index,
                    last_run_index=resolved_index + 1,
                    terminal_session_transition=True,
                )
            ):
                return False
            return self._is_exact_confirmed_terminal_state(
                evidence,
                operation,
                result,
                prior_output=prior_output,
                prior_usage=prior_usage,
                messages_before=messages_before,
                gap=True,
            )
        if request.reason != "model_call_unknown_outcome" or dict(request.details) != {
            "checkpoint_phase": RunCheckpointPhase.MODEL_IN_FLIGHT.value
        }:
            return False
        if result.disposition is ProviderRecoveryDisposition.COMPLETED:
            assert result.text is not None and result.usage is not None
            expected_operation = operation.model_copy(
                update={
                    "status": ExternalOperationStatus.COMPLETED,
                    "outcome": {
                        "finish_reason": result.finish_reason,
                        "text": result.text,
                        "tool_calls": []
                        if result.tool_call is None
                        else [
                            {
                                "index": result.tool_call.index,
                                "call_id": result.tool_call.call_id,
                                "name": result.tool_call.name,
                                "arguments_json": result.tool_call.arguments_json,
                            }
                        ],
                        "usage": result.usage.model_dump(mode="json"),
                    },
                }
            )
            if operation != expected_operation:
                return False
            if (
                len(trailing) < 2
                or tuple(event.type for event in trailing[:2])
                != ("model.usage.reported", "model.call.completed")
                or trailing[0].payload != result.usage.model_dump(mode="json")
                or trailing[1].payload
                != {"finish_reason": result.finish_reason}
                or not self._is_exact_resolution_event_batch(
                    evidence,
                    first_run_index=resolved_index,
                    last_run_index=resolved_index + 2,
                    terminal_session_transition=False,
                )
            ):
                return False
            if result.tool_call is not None:
                interrupt = evidence.run_events[requested_index - 1]
                later = trailing[2:]
                retained = (
                    *evidence.run_events[: requested_index - 1],
                    evidence.run_events[resolved_index],
                    *trailing[:2],
                    interrupt,
                    *later,
                )
                normalized = replace(
                    evidence,
                    pending=(),
                    reconciliations=(),
                    run_events=tuple(
                        event.model_copy(update={"sequence": index})
                        for index, event in enumerate(retained, start=1)
                    ),
                    run_event_cursors=tuple(range(1, len(retained) + 1)),
                )
                if later:
                    return self._is_valid_run_event_envelope(
                        normalized,
                        allow_recovery_closed=True,
                    )
                return (
                    checkpoint.turn == operation.turn
                    and len(checkpoint.tool_results) == operation.turn
                    and self._is_exact_ready_tool_relation(
                        normalized,
                        base_request,
                    )
                )
            if (
                checkpoint.turn != operation.turn
                or len(checkpoint.tool_results) != operation.turn
                or tuple(event.type for event in trailing[2:])
                != ("step.completed", "run.completed")
                or trailing[2].payload != {}
            ):
                return False
            output_parts = (*prior_output, result.text)
            usage = _add_usage(prior_usage, result.usage)
            terminal_payload = {
                "output_text": "".join(output_parts),
                "usage": usage.model_dump(mode="json"),
            }
            if checkpoint.tool_results:
                terminal_payload["tool_results"] = [
                    item.model_dump(mode="json")
                    for item in checkpoint.tool_results
                ]
            if (
                trailing[3].payload != terminal_payload
                or not self._is_exact_resolution_event_batch(
                    evidence,
                    first_run_index=resolved_index,
                    last_run_index=resolved_index + 4,
                    terminal_session_transition=True,
                )
            ):
                return False
            return self._is_exact_confirmed_terminal_state(
                evidence,
                operation,
                result,
                prior_output=prior_output,
                prior_usage=prior_usage,
                messages_before=messages_before,
                gap=False,
            )
        if (
            result.disposition is not ProviderRecoveryDisposition.FAILED
            or result.error_code is None
            or result.retryable is None
            or tuple(event.type for event in trailing)
            != ("model.call.failed", "step.failed", "run.failed")
        ):
            return False
        expected_error = {
            "error": {
                "code": result.error_code.value,
                "message": "model call failed",
                "retryable": result.retryable,
            }
        }
        return (
            operation.status is ExternalOperationStatus.FAILED
            and operation.model_dump(mode="json")["outcome"]
            == {
                "error": {
                    "code": result.error_code.value,
                    "message": "model call failed",
                }
            }
            and all(event.payload == expected_error for event in trailing)
            and checkpoint.turn == operation.turn
            and len(checkpoint.tool_results) == operation.turn
            and self._is_exact_resolution_event_batch(
                evidence,
                first_run_index=resolved_index,
                last_run_index=resolved_index + 3,
                terminal_session_transition=True,
            )
            and checkpoint.phase is RunCheckpointPhase.TERMINAL
            and checkpoint.operation_id is None
            and checkpoint.model_dump(mode="json")["messages"]
            == list(messages_before)
            and checkpoint.output_parts == tuple(prior_output)
            and checkpoint.usage == prior_usage
            and evidence.run
            == evidence.run.model_copy(
                update={
                    "status": RunStatus.FAILED,
                    "version": evidence.run.version,
                    "output_text": "".join(prior_output),
                    "usage": prior_usage,
                    "tool_results": checkpoint.tool_results,
                    "error": RunFailure(
                        code=result.error_code.value,
                        message="model call failed",
                        retryable=result.retryable,
                    ),
                }
            )
        )

    def _is_confirmed_replay_closed_world(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        request: ReconciliationRequest,
        operation: ModelCallOperation,
    ) -> bool:
        checkpoint = evidence.checkpoint
        if (
            checkpoint is None
            or not self._has_closed_reconciliation_markers(evidence)
        ):
            return False
        completed = self._completed_model_outcome(operation)
        confirmed_tool_call = completed is not None and len(completed[2]) == 1
        if confirmed_tool_call and evidence.run.status is RunStatus.WAITING_RECONCILIATION:
            return self._is_exact_confirmed_later_model_pending(
                evidence,
                base_request,
                request,
                operation,
            )
        if evidence.run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            return self._is_exact_confirmed_terminal_history(
                evidence,
                base_request,
                request,
                operation,
            )
        closed_world = (
            evidence.reconciliations == (request,)
            and not evidence.pending
            and self._has_exact_model_operation_turns(
                evidence,
                through_turn=checkpoint.turn,
                required=operation,
            )
        )
        if not closed_world:
            return False
        return True

    def _is_exact_confirmed_tool_replay(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        request: ReconciliationRequest,
        operation: ToolCallOperation,
        *,
        removed_event_indexes: set[int] | None = None,
        removed_operation_ids: set[str] | None = None,
        deferred_events: Mapping[int, list[EventEnvelope]] | None = None,
    ) -> bool:
        resolution = request.resolution
        checkpoint = evidence.checkpoint
        if (
            resolution is None
            or checkpoint is None
            or resolution.action is not ReconciliationAction.CONFIRM_COMPLETED
            or request.operation_id != operation.operation_id
            or request.run_id != evidence.run.run_id
            or request.session_id != evidence.run.session_id
            or request.reason != "tool_call_unknown_outcome"
            or dict(request.details)
            != {"checkpoint_phase": RunCheckpointPhase.TOOL_IN_FLIGHT.value}
            or operation.run_id != evidence.run.run_id
            or operation.session_id != evidence.run.session_id
            or checkpoint.run_id != evidence.run.run_id
            or checkpoint.session_id != evidence.run.session_id
            or not self._is_valid_run_event_envelope(
                evidence,
                allow_recovery_closed=True,
            )
        ):
            return False
        raw_evidence = thaw_json(resolution.evidence)
        if set(raw_evidence) != {"tool_result"}:
            return False
        result = _strict_tool_result(raw_evidence["tool_result"])
        if result is None:
            return False
        expected_status = (
            ExternalOperationStatus.COMPLETED
            if result.status is ToolResultStatus.SUCCEEDED
            else ExternalOperationStatus.FAILED
        )
        if (
            operation.status is not expected_status
            or operation.model_dump(mode="json")["outcome"]
            != result.model_dump(mode="json")
            or operation.turn >= len(checkpoint.tool_results)
            or checkpoint.tool_results[operation.turn] != result
        ):
            return False
        requested = tuple(
            index
            for index, event in enumerate(evidence.run_events)
            if event.type == "reconciliation.requested"
            and event.payload
            == {
                "request_id": request.request_id,
                "operation_id": request.operation_id,
                "reason": request.reason,
            }
        )
        resolved = tuple(
            index
            for index, event in enumerate(evidence.run_events)
            if event.type == "reconciliation.resolved"
            and event.event_id == resolution.event_id
            and event.occurred_at == resolution.decided_at
            and event.payload
            == {
                "request_id": request.request_id,
                "operation_id": request.operation_id,
                "action": resolution.action.value,
                "actor": thaw_json(resolution.actor),
                "evidence": raw_evidence,
            }
        )
        if (
            len(requested) != 1
            or len(resolved) != 1
            or requested[0] == 0
            or resolved[0] != requested[0] + 1
            or evidence.run_events[requested[0] - 1].type != "run.interrupted"
            or resolved[0] + 2 >= len(evidence.run_events)
            or tuple(
                event.type
                for event in evidence.run_events[resolved[0] + 1 : resolved[0] + 3]
            )
            != ("tool.call.completed", "step.completed")
            or evidence.run_events[resolved[0] + 1].payload
            != result.model_dump(mode="json")
            or evidence.run_events[resolved[0] + 2].payload != {}
            or not self._is_exact_resolution_event_batch(
                evidence,
                first_run_index=resolved[0],
                last_run_index=resolved[0] + 2,
                terminal_session_transition=False,
            )
        ):
            return False

        excluded_operation_ids = removed_operation_ids or set()
        model_operations = tuple(
            item
            for item in evidence.operations
            if (
                isinstance(item, ModelCallOperation)
                and item.turn == operation.turn
                and item.operation_id not in excluded_operation_ids
            )
        )
        if len(model_operations) != 1:
            return False
        completed = self._completed_model_outcome(model_operations[0])
        if completed is None or len(completed[2]) != 1:
            return False
        raw_call = completed[2][0]
        if (result.call_id, result.tool_name) != (
            raw_call["call_id"],
            raw_call["name"],
        ):
            return False
        return self._is_valid_resolved_attempt_lifecycle(
            evidence,
            base_request,
            operation,
            interrupt_index=requested[0] - 1,
            removed_event_indexes=removed_event_indexes or set(),
            removed_operation_ids=frozenset(excluded_operation_ids),
            deferred_events=deferred_events,
        )

    def _is_confirmed_tool_replay_closed_world(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        request: ReconciliationRequest,
        operation: ToolCallOperation,
    ) -> bool:
        checkpoint = evidence.checkpoint
        if (
            checkpoint is None
            or not self._has_closed_reconciliation_markers(evidence)
        ):
            return False
        effective = self._effective_resolved_evidence(evidence, base_request)
        if effective is None:
            return False
        if evidence.run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            if evidence.pending or checkpoint.phase is not RunCheckpointPhase.TERMINAL:
                return False
            terminal_index = next(
                (
                    index
                    for index, event in enumerate(evidence.run_events)
                    if event.type
                    == (
                        "run.completed"
                        if evidence.run.status is RunStatus.COMPLETED
                        else "run.failed"
                    )
                ),
                -1,
            )
            return (
                terminal_index >= 0
                and self._is_exact_resolution_event_batch(
                    evidence,
                    first_run_index=terminal_index,
                    last_run_index=terminal_index,
                    terminal_session_transition=True,
                    atomic_session_timestamp=False,
                )
                and self._is_valid_normalized_terminal_history(
                    evidence,
                    base_request,
                )
            )
        if evidence.pending:
            if (
                len(evidence.pending) != 1
                or evidence.run.status is not RunStatus.WAITING_RECONCILIATION
            ):
                return False
            pending = evidence.pending[0]
            if pending == request:
                return False
            current = self._matching_in_flight_operation(evidence)
            if current is None or pending.operation_id != current.operation_id:
                return False
            retained = tuple(
                event
                for event in effective.run_events
                if event.type
                not in {"reconciliation.requested", "reconciliation.resolved"}
            )
            certified = replace(
                effective,
                pending=(),
                run_events=tuple(
                    event.model_copy(update={"sequence": index})
                    for index, event in enumerate(retained, start=1)
                ),
                run_event_cursors=tuple(range(1, len(retained) + 1)),
            )
            return self._is_resolution_operation_certified(
                certified,
                base_request,
                current,
            )
        immediate_projection = (
            effective.run.status is RunStatus.INTERRUPTED
            and effective.run.run_id in effective.session.active_run_ids
            and checkpoint.phase is RunCheckpointPhase.READY_FOR_MODEL
            and checkpoint.operation_id is None
            and checkpoint.turn == operation.turn + 1
            and effective.run_events[-1].type == "run.interrupted"
            and self._is_exact_ready_model_relation(
                effective,
                base_request,
            )
        )
        if immediate_projection:
            return True
        if not any(
            record.request_id != request.request_id
            and record.status is ReconciliationStatus.RESOLVED
            for record in evidence.reconciliations
        ) or not self._is_certified_safe_checkpoint(effective, base_request):
            return False
        try:
            self._engine.validate_resume_checkpoint(checkpoint)
        except AgentSDKError:
            return False
        return True

    @staticmethod
    def _has_closed_reconciliation_markers(
        evidence: _RecoveryEvidence,
    ) -> bool:
        records = evidence.reconciliations
        requested_events = tuple(
            event
            for event in evidence.run_events
            if event.type == "reconciliation.requested"
        )
        resolved_events = tuple(
            event
            for event in evidence.run_events
            if event.type == "reconciliation.resolved"
        )
        resolved_records = tuple(
            record
            for record in records
            if record.status is ReconciliationStatus.RESOLVED
        )
        if (
            len({record.request_id for record in records}) != len(records)
            or len(requested_events) != len(records)
            or len(resolved_events) != len(resolved_records)
        ):
            return False
        for record in records:
            requested = tuple(
                event
                for event in requested_events
                if event.payload
                == {
                    "request_id": record.request_id,
                    "operation_id": record.operation_id,
                    "reason": record.reason,
                }
            )
            if len(requested) != 1:
                return False
            resolution = record.resolution
            matching_resolved = tuple(
                event
                for event in resolved_events
                if resolution is not None
                and event.event_id == resolution.event_id
                and event.occurred_at == resolution.decided_at
                and event.payload
                == {
                    "request_id": record.request_id,
                    "operation_id": record.operation_id,
                    "action": resolution.action.value,
                    "actor": thaw_json(resolution.actor),
                    "evidence": thaw_json(resolution.evidence),
                }
            )
            if (
                record.status is ReconciliationStatus.RESOLVED
                and len(matching_resolved) != 1
            ) or (
                record.status is ReconciliationStatus.PENDING
                and (resolution is not None or matching_resolved)
            ):
                return False
        return True

    @staticmethod
    def _has_exact_model_operation_turns(
        evidence: _RecoveryEvidence,
        *,
        through_turn: int,
        required: ModelCallOperation,
    ) -> bool:
        model_operations = tuple(
            operation
            for operation in evidence.operations
            if isinstance(operation, ModelCallOperation)
        )
        by_turn = {operation.turn: operation for operation in model_operations}
        return (
            len(by_turn) == len(model_operations)
            and tuple(sorted(by_turn)) == tuple(range(through_turn + 1))
            and by_turn.get(required.turn) == required
        )

    def _is_exact_confirmed_later_model_pending(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        original_request: ReconciliationRequest,
        original_operation: ModelCallOperation,
    ) -> bool:
        checkpoint = evidence.checkpoint
        if (
            checkpoint is None
            or evidence.run.status is not RunStatus.WAITING_RECONCILIATION
            or checkpoint.phase is not RunCheckpointPhase.MODEL_IN_FLIGHT
            or len(evidence.pending) != 1
        ):
            return False
        pending = evidence.pending[0]
        resolved_records = tuple(
            record
            for record in evidence.reconciliations
            if record.status is ReconciliationStatus.RESOLVED
        )
        if (
            resolved_records != (original_request,)
            or len(evidence.reconciliations) != 2
            or pending.request_id == original_request.request_id
            or pending.run_id != evidence.run.run_id
            or pending.session_id != evidence.run.session_id
            or pending.operation_id != checkpoint.operation_id
            or pending.reason != "model_call_unknown_outcome"
            or dict(pending.details)
            != {"checkpoint_phase": RunCheckpointPhase.MODEL_IN_FLIGHT.value}
        ):
            return False
        requested = tuple(
            event
            for event in evidence.run_events
            if event.type == "reconciliation.requested"
            and event.payload
            == {
                "request_id": pending.request_id,
                "operation_id": pending.operation_id,
                "reason": pending.reason,
            }
        )
        current_operation = self._matching_in_flight_operation(evidence)
        if (
            len(requested) != 1
            or requested[0] != evidence.run_events[-1]
            or not isinstance(current_operation, ModelCallOperation)
            or current_operation.status is not ExternalOperationStatus.STARTED
            or not self._has_exact_model_operation_turns(
                evidence,
                through_turn=checkpoint.turn,
                required=original_operation,
            )
        ):
            return False
        effective = self._effective_resolved_evidence(evidence, base_request)
        if effective is None:
            return False
        retained_events = tuple(
            event
            for event in effective.run_events
            if event.type
            not in {"reconciliation.requested", "reconciliation.resolved"}
        )
        certified = replace(
            effective,
            reconciliations=(),
            run_events=tuple(
                event.model_copy(update={"sequence": index})
                for index, event in enumerate(retained_events, start=1)
            ),
            run_event_cursors=tuple(range(1, len(retained_events) + 1)),
        )
        return self._is_resolution_operation_certified(
            certified,
            base_request,
            current_operation,
        )

    def _is_exact_confirmed_terminal_history(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        request: ReconciliationRequest,
        operation: ModelCallOperation,
    ) -> bool:
        checkpoint = evidence.checkpoint
        resolution = request.resolution
        if (
            checkpoint is None
            or resolution is None
            or checkpoint.phase is not RunCheckpointPhase.TERMINAL
            or checkpoint.operation_id is not None
        ):
            return False
        return self._is_valid_normalized_terminal_history(
            evidence,
            base_request,
        )

    def _is_valid_normalized_terminal_history(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
    ) -> bool:
        if evidence.run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
            return False
        effective = self._effective_resolved_evidence(evidence, base_request)
        if effective is None:
            return False
        retained = tuple(
            event
            for event in effective.run_events
            if event.type
            not in {"reconciliation.requested", "reconciliation.resolved"}
        )
        normalized = replace(
            effective,
            run_events=tuple(
                event.model_copy(update={"sequence": index})
                for index, event in enumerate(retained, start=1)
            ),
            run_event_cursors=tuple(range(1, len(retained) + 1)),
        )
        operations_by_id = {
            item.operation_id: item for item in evidence.operations
        }
        confirmed_operation_ids: set[str] = set()
        for record in evidence.reconciliations:
            operation_id = record.operation_id
            confirmed_operation = (
                None
                if operation_id is None
                else operations_by_id.get(operation_id)
            )
            if (
                record.reason == "model_call_unknown_outcome"
                and record.resolution is not None
                and record.resolution.action
                is ReconciliationAction.CONFIRM_COMPLETED
                and isinstance(confirmed_operation, ModelCallOperation)
                and self._is_exact_confirmed_model_replay(
                    evidence,
                    base_request,
                    record,
                    confirmed_operation,
                )
            ):
                confirmed_operation_ids.add(confirmed_operation.operation_id)
        return self._is_valid_certified_provider_history(
            normalized,
            base_request=base_request,
            terminal_status=evidence.run.status,
            confirmed_operation_ids=frozenset(confirmed_operation_ids),
        )

    @staticmethod
    def _is_exact_confirmed_terminal_state(
        evidence: _RecoveryEvidence,
        operation: ModelCallOperation,
        result: ProviderRecoveryResult,
        *,
        prior_output: list[str],
        prior_usage: TokenUsage,
        messages_before: tuple[dict[str, Any], ...],
        gap: bool,
    ) -> bool:
        checkpoint = evidence.checkpoint
        if (
            checkpoint is None
            or result.text is None
            or result.usage is None
            or result.tool_call is not None
        ):
            return False
        output_parts = (*prior_output, result.text)
        usage = _add_usage(prior_usage, result.usage)
        assistant = {"role": "assistant", "content": result.text or None}
        return (
            operation.status is ExternalOperationStatus.COMPLETED
            and checkpoint.phase is RunCheckpointPhase.TERMINAL
            and checkpoint.operation_id is None
            and checkpoint.model_dump(mode="json")["messages"]
            == [*messages_before, assistant]
            and checkpoint.output_parts == output_parts
            and checkpoint.usage == usage
            and evidence.run.status is RunStatus.COMPLETED
            and evidence.run.output_text == "".join(output_parts)
            and evidence.run.usage == usage
            and evidence.run.tool_results == checkpoint.tool_results
            and (not gap or operation.model_dump(mode="json")["outcome"] == {
                "finish_reason": result.finish_reason,
                "text": result.text,
                "tool_calls": [],
                "usage": result.usage.model_dump(mode="json"),
            })
        )

    @staticmethod
    def _is_exact_resolution_event_batch(
        evidence: _RecoveryEvidence,
        *,
        first_run_index: int,
        last_run_index: int,
        terminal_session_transition: bool,
        atomic_session_timestamp: bool = True,
    ) -> bool:
        if (
            first_run_index < 0
            or last_run_index < first_run_index
            or last_run_index >= len(evidence.run_events)
        ):
            return False
        run_events = evidence.run_events[first_run_index : last_run_index + 1]
        run_cursors = evidence.run_event_cursors[
            first_run_index : last_run_index + 1
        ]
        if (
            not run_cursors
            or run_cursors
            != tuple(range(run_cursors[0], run_cursors[0] + len(run_cursors)))
            or any(event.occurred_at != run_events[0].occurred_at for event in run_events)
        ):
            return False
        if not terminal_session_transition:
            return True
        if (
            len(evidence.session_lifecycle_events) != 1
            or len(evidence.session_lifecycle_event_cursors) != 1
        ):
            return False
        session_event = evidence.session_lifecycle_events[0]
        if set(session_event.payload) != {"run_id", "status"}:
            return False
        try:
            projected_status = SessionStatus(session_event.payload["status"])
        except (TypeError, ValueError):
            return False
        event_matches_projection = (
            session_event.type == "session.closed"
            and projected_status is SessionStatus.CLOSED
        ) or (
            session_event.type == "session.run.detached"
            and projected_status in {SessionStatus.ACTIVE, SessionStatus.CLOSING}
        )
        legal_current_statuses = {
            SessionStatus.ACTIVE: {
                SessionStatus.ACTIVE,
                SessionStatus.CLOSING,
                SessionStatus.CLOSED,
            },
            SessionStatus.CLOSING: {
                SessionStatus.CLOSING,
                SessionStatus.CLOSED,
            },
            SessionStatus.CLOSED: {SessionStatus.CLOSED},
        }
        exact_current = evidence.session.version == session_event.sequence
        later_current = evidence.session.version > session_event.sequence
        legal_session_successor = (
            exact_current and evidence.session.status is projected_status
        ) or (
            later_current
            and projected_status is not SessionStatus.CLOSED
            and evidence.session.status in legal_current_statuses[projected_status]
        )
        return (
            type(evidence.session_lifecycle_event_cursors[0]) is int
            and evidence.session_lifecycle_event_cursors[0] == run_cursors[-1] + 1
            and isinstance(session_event.event_id, str)
            and bool(session_event.event_id.strip())
            and type(session_event.schema_version) is int
            and session_event.schema_version == 1
            and event_matches_projection
            and session_event.session_id == evidence.session.session_id
            and session_event.run_id is None
            and type(session_event.sequence) is int
            and session_event.sequence >= 1
            and legal_session_successor
            and (
                session_event.occurred_at == run_events[0].occurred_at
                if atomic_session_timestamp
                else session_event.occurred_at >= run_events[-1].occurred_at
            )
            and session_event.payload
            == {
                "run_id": evidence.run.run_id,
                "status": projected_status.value,
            }
        )

    def _is_resolution_operation_certified(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        operation: ExternalOperation,
    ) -> bool:
        checkpoint = evidence.checkpoint
        if (
            checkpoint is None
            or not self._is_valid_run_event_envelope(evidence)
            or not self._is_valid_certified_lifecycle_positions(
                evidence,
                current_kind=operation.operation_kind,
            )
        ):
            return False
        if isinstance(operation, ModelCallOperation):
            if not self._is_valid_certified_provider_history(evidence):
                return False
            try:
                reconstructed = ModelRequest(
                    model=base_request.model,
                    messages=tuple(checkpoint.model_dump(mode="json")["messages"]),
                    tools=base_request.tools,
                    params=base_request.params,
                    purpose=base_request.purpose,
                )
            except Exception:
                return False
            metadata = dict(operation.recovery_metadata)
            metadata_valid = metadata == {
                "authoritative_status": False,
                "same_operation_id_resend": False,
            } or (
                set(metadata)
                == {
                    "adapter_id",
                    "adapter_version",
                    "authoritative_status",
                    "same_operation_id_resend",
                }
                and all(
                    isinstance(metadata[field], str) and bool(metadata[field])
                    for field in ("adapter_id", "adapter_version")
                )
                and type(metadata["authoritative_status"]) is bool
                and type(metadata["same_operation_id_resend"]) is bool
            )
            return (
                metadata_valid
                and operation.provider_identity == base_request.model
                and operation.request_fingerprint
                == _model_request_fingerprint(reconstructed)
            )
        assert isinstance(operation, ToolCallOperation)
        return self._certified_tool_call(
            evidence,
            base_request,
            operation,
            allow_unsafe=True,
        ) is not None

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

    async def _load_evidence(
        self,
        run: RunSnapshot,
        *,
        allow_terminal_detached: bool = False,
    ) -> _RecoveryEvidence:
        session_data = await self._store.get_snapshot("session", run.session_id)
        try:
            session = SessionSnapshot.model_validate(session_data)
        except Exception:
            raise self._state_error() from None
        terminal = run.status in {RunStatus.COMPLETED, RunStatus.FAILED}
        if (
            run.run_id not in session.active_run_ids
            and not (allow_terminal_detached and terminal)
        ) or (terminal and run.run_id in session.active_run_ids):
            raise self._state_error() from None
        checkpoint = await self._store.get_run_checkpoint(run.run_id)
        operations = await self._store.list_external_operations(run.run_id)
        reconciliations = await self._store.list_reconciliation_requests(run.run_id)
        pending = tuple(
            request
            for request in reconciliations
            if request.status is ReconciliationStatus.PENDING
        )
        up_to_cursor = await self._store.latest_cursor()
        events = await self._store.read_events(
            after_cursor=0,
            up_to_cursor=up_to_cursor,
        )
        run_records = tuple(
            stored for stored in events if stored.event.run_id == run.run_id
        )
        session_lifecycle_records = tuple(
            stored
            for stored in events
            if stored.event.run_id is None
            and stored.event.session_id == run.session_id
            and stored.event.type in {"session.run.detached", "session.closed"}
            and stored.event.payload.get("run_id") == run.run_id
        )
        event_ids = tuple(stored.event.event_id for stored in events)
        return _RecoveryEvidence(
            run=run,
            session=session,
            checkpoint=checkpoint,
            operations=operations,
            pending=pending,
            reconciliations=reconciliations,
            run_events=tuple(stored.event for stored in run_records),
            run_event_cursors=tuple(stored.cursor for stored in run_records),
            session_lifecycle_events=tuple(
                stored.event for stored in session_lifecycle_records
            ),
            session_lifecycle_event_cursors=tuple(
                stored.cursor for stored in session_lifecycle_records
            ),
            run_event_ids_unique=len(event_ids) == len(set(event_ids)),
        )

    @staticmethod
    def _is_valid_run_event_envelope(
        evidence: _RecoveryEvidence,
        *,
        allow_recovery_closed: bool = False,
    ) -> bool:
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
        control_types = tuple(
            event.type
            for event in events
            if event.type in {"run.interrupted", "run.recovery.started"}
        )
        closed_after_recovery = (
            allow_recovery_closed
            and run.status in {RunStatus.COMPLETED, RunStatus.FAILED}
            and len(interrupted) == len(recovery_started)
        )
        expected_control_types = tuple(
            event_type
            for _ in range(len(recovery_started))
            for event_type in ("run.interrupted", "run.recovery.started")
        ) + (() if closed_after_recovery else ("run.interrupted",))
        if (
            control_types != expected_control_types
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
    def _validated_permission_request(
        evidence: _RecoveryEvidence,
        call: ToolCallCompleted,
        payload: Mapping[str, Any],
    ) -> PermissionRequest | None:
        descriptor = evidence.run.execution_descriptor
        if descriptor is None or set(payload) != {"request"}:
            return None
        request_payload = payload["request"]
        if not isinstance(request_payload, Mapping):
            return None
        try:
            request = PermissionRequest.model_validate(request_payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        expected = RunRecoveryService._recorded_permission_request(
            evidence,
            call,
            request_id=request.request_id,
        )
        if (
            request.model_dump(mode="json") != request_payload
            or expected is None
            or request != expected
        ):
            return None
        return request

    @staticmethod
    def _recorded_permission_request(
        evidence: _RecoveryEvidence,
        call: ToolCallCompleted,
        *,
        request_id: str,
    ) -> PermissionRequest | None:
        descriptor = evidence.run.execution_descriptor
        if descriptor is None or not request_id.strip():
            return None
        capabilities = tuple(
            tool for tool in descriptor.tools if tool.spec.name == call.name
        )
        if len(capabilities) != 1:
            return None
        try:
            arguments = json.loads(
                call.arguments_json,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(arguments, dict):
                return None
            Draft202012Validator(
                thaw_json(capabilities[0].spec.input_schema)
            ).validate(arguments)
            return PermissionRequest(
                request_id=request_id,
                run_id=evidence.run.run_id,
                session_id=evidence.run.session_id,
                tool_name=call.name,
                arguments=arguments,
                effects=capabilities[0].spec.effects,
            )
        except (
            JSONSchemaValidationError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None

    @staticmethod
    def _recorded_permission_decision(
        evidence: _RecoveryEvidence,
        request: PermissionRequest,
    ) -> PermissionDecision | None:
        descriptor = evidence.run.execution_descriptor
        if descriptor is None:
            return None
        try:
            return PolicyEngine(descriptor.policy.permission_default).evaluate(request)
        except AgentSDKError:
            return None

    @staticmethod
    def _validated_permission_decision(
        request_payload: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> PermissionDecision | None:
        if (
            set(payload) != {"request", "decision"}
            or payload["request"] != request_payload
            or not isinstance(payload["decision"], Mapping)
        ):
            return None
        try:
            decision = PermissionDecision.model_validate(payload["decision"])
        except (TypeError, ValueError):
            return None
        if (
            decision.model_dump(mode="json") != payload["decision"]
            or decision.action not in {"allow", "deny"}
        ):
            return None
        return decision

    @staticmethod
    def _is_recovery_control_event(event: EventEnvelope) -> bool:
        if event.type in {
            "run.interrupted",
            "run.recovery.started",
            "model.recovery.query.started",
            "model.recovery.resend.started",
            "tool.recovery.retry.started",
        }:
            return True
        payload_keys = set(event.payload)
        return (
            event.type == "permission.requested"
            and payload_keys == {"request", "tool"}
        ) or (
            event.type == "permission.resolved"
            and payload_keys == {"request", "tool", "allowed"}
        ) or (
            event.type == "tool.call.authorized"
            and payload_keys == {"call", "tool"}
        )

    @staticmethod
    def _is_confirmed_tool_operation(
        evidence: _RecoveryEvidence,
        operation: ToolCallOperation,
        result: ToolResult,
    ) -> bool:
        expected_status = (
            ExternalOperationStatus.COMPLETED
            if result.status is ToolResultStatus.SUCCEEDED
            else ExternalOperationStatus.FAILED
        )
        if (
            operation.status is not expected_status
            or operation.model_dump(mode="json")["outcome"]
            != result.model_dump(mode="json")
        ):
            return False
        matching = tuple(
            request
            for request in evidence.reconciliations
            if request.operation_id == operation.operation_id
            and request.resolution is not None
            and request.resolution.action is ReconciliationAction.CONFIRM_COMPLETED
        )
        if len(matching) != 1:
            return False
        resolution = matching[0].resolution
        if resolution is None:
            return False
        raw_evidence = thaw_json(resolution.evidence)
        return (
            set(raw_evidence) == {"tool_result"}
            and _strict_tool_result(raw_evidence["tool_result"]) == result
        )

    @staticmethod
    def _is_authoritative_historical_tool_result(
        evidence: _RecoveryEvidence,
        *,
        turn: int,
        result_index: int,
        call: ToolCallCompleted,
        operation: ToolCallOperation | None,
        result: ToolResult,
        permission_decision: PermissionDecision | None,
        initial_permission_decision: PermissionDecision | None,
        permission_recovery: bool,
        permission_allowed: bool | None,
    ) -> bool:
        checkpoint = evidence.checkpoint
        descriptor = evidence.run.execution_descriptor
        if (
            checkpoint is None
            or descriptor is None
            or result_index >= len(checkpoint.tool_results)
            or checkpoint.tool_results[result_index] != result
        ):
            return False
        tool_messages = tuple(
            message
            for message in checkpoint.messages
            if message.get("role") == "tool"
        )
        if (
            len(tool_messages) != len(checkpoint.tool_results)
            or result_index >= len(tool_messages)
            or tool_messages[result_index]
            != {
                "role": "tool",
                "tool_call_id": call.call_id,
                "name": call.name,
                "content": result.content,
            }
        ):
            return False

        capabilities = tuple(
            tool for tool in descriptor.tools if tool.spec.name == call.name
        )
        capability = capabilities[0] if len(capabilities) == 1 else None
        arguments: dict[str, Any] | None = None
        if capability is not None:
            try:
                candidate = json.loads(
                    call.arguments_json,
                    parse_constant=_reject_json_constant,
                )
                if not isinstance(candidate, dict):
                    raise ValueError
                Draft202012Validator(thaw_json(capability.spec.input_schema)).validate(
                    candidate
                )
                arguments = candidate
            except (
                JSONSchemaValidationError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                pass

        if operation is not None:
            if capability is None or arguments is None:
                return False
            expected_metadata = (
                {"safe_retry": False, "retry_class": "unsafe"}
                if capability.spec.retry_policy is ToolRetryPolicy.NEVER
                else {
                    "safe_retry": True,
                    "retry_class": capability.spec.retry_policy.value,
                }
            )
            expected_status = (
                ExternalOperationStatus.COMPLETED
                if result.status is ToolResultStatus.SUCCEEDED
                else ExternalOperationStatus.FAILED
            )
            operator_confirmed = (
                RunRecoveryService._is_confirmed_tool_operation(
                    evidence,
                    operation,
                    result,
                )
            )
            normalized_result: ToolResult | None = result if operator_confirmed else None
            if not operator_confirmed and result.status is ToolResultStatus.SUCCEEDED:
                try:
                    normalized_result = ToolResult.succeeded(
                        call.call_id,
                        call.name,
                        thaw_json(result.value),
                    )
                except ValueError:
                    return False
            elif not operator_confirmed and result.status is ToolResultStatus.FAILED:
                candidates = (
                    ToolResult.normalized_error(
                        call.call_id,
                        call.name,
                        ToolResultStatus.FAILED,
                        message,
                    )
                    for message in (
                        "tool handler failed",
                        "tool result is not JSON-compatible or exceeds size limit",
                    )
                )
                if result not in candidates:
                    return False
                normalized_result = result
            elif not operator_confirmed and result.status is ToolResultStatus.TIMED_OUT:
                normalized_result = ToolResult.normalized_error(
                    call.call_id,
                    call.name,
                    ToolResultStatus.TIMED_OUT,
                    "tool execution timed out",
                )
            elif (
                not operator_confirmed
                and result.status is ToolResultStatus.DENIED
                and permission_recovery
                and (
                    permission_allowed is False
                    or (
                        initial_permission_decision is not None
                        and initial_permission_decision.action == "deny"
                    )
                )
            ):
                normalized_result = ToolResult.normalized_error(
                    call.call_id,
                    call.name,
                    ToolResultStatus.DENIED,
                    "permission denied",
                )
            if normalized_result != result:
                return False
            return (
                operation.run_id == evidence.run.run_id
                and operation.session_id == evidence.run.session_id
                and operation.turn == turn
                and operation.status is expected_status
                and operation.tool_identity == capability.capability_hash
                and dict(operation.recovery_metadata) == expected_metadata
                and operation.request_fingerprint
                == _tool_request_fingerprint(call, capability, arguments)
                and operation.model_dump(mode="json")["outcome"]
                == result.model_dump(mode="json")
            )

        if len(capabilities) > 1:
            return False
        if capability is None:
            expected = ToolResult.normalized_error(
                call.call_id,
                call.name,
                ToolResultStatus.FAILED,
                "tool not found",
            )
        elif arguments is None:
            expected = ToolResult.normalized_error(
                call.call_id,
                call.name,
                ToolResultStatus.INVALID_ARGUMENTS,
                "invalid tool arguments",
            )
        elif (
            initial_permission_decision is not None
            and initial_permission_decision.action == "deny"
        ):
            expected = ToolResult.normalized_error(
                call.call_id,
                call.name,
                ToolResultStatus.DENIED,
                "permission denied",
            )
        elif (
            initial_permission_decision is not None
            and initial_permission_decision.action == "ask"
            and permission_decision is not None
            and not permission_decision.allowed
        ):
            expected = ToolResult.normalized_error(
                call.call_id,
                call.name,
                ToolResultStatus.DENIED,
                permission_decision.reason or "permission denied",
            )
        else:
            return False
        return result == expected

    @staticmethod
    def _is_valid_certified_lifecycle_positions(
        evidence: _RecoveryEvidence,
        *,
        current_kind: ExternalOperationKind | None,
        terminal_status: RunStatus | None = None,
        confirmed_operation_ids: frozenset[str] = frozenset(),
    ) -> bool:
        checkpoint = evidence.checkpoint
        descriptor = evidence.run.execution_descriptor
        if (
            checkpoint is None
            or descriptor is None
            or terminal_status
            not in {None, RunStatus.COMPLETED, RunStatus.FAILED}
        ):
            return False
        model_operations = {
            operation.turn: operation
            for operation in evidence.operations
            if isinstance(operation, ModelCallOperation)
        }
        tool_operations = {
            operation.turn: operation
            for operation in evidence.operations
            if isinstance(operation, ToolCallOperation)
        }
        if len(model_operations) != sum(
            isinstance(operation, ModelCallOperation)
            for operation in evidence.operations
        ) or len(tool_operations) != sum(
            isinstance(operation, ToolCallOperation)
            for operation in evidence.operations
        ):
            return False

        state = "ready_for_step"
        interrupted_state: str | None = None
        audit_kind: ExternalOperationKind | None = None
        turn = -1
        current_model: ModelCallOperation | None = None
        current_tool: ToolCallOperation | None = None
        current_call: ToolCallCompleted | None = None
        pending_permission: Mapping[str, Any] | None = None
        permission_decision: PermissionDecision | None = None
        initial_permission_decision: PermissionDecision | None = None
        permission_recovery = False
        permission_allowed: bool | None = None
        completed_result_count = 0
        terminal_failure_payload: Mapping[str, Any] | None = None
        model_deltas: list[str] = []
        model_usage: TokenUsage | None = None
        recovered_model_operation_ids = {
            str(event.payload["operation_id"])
            for event in evidence.run_events
            if event.type
            in {"model.recovery.query.started", "model.recovery.resend.started"}
            and isinstance(event.payload.get("operation_id"), str)
        }
        recovered_model_operation_ids.update(confirmed_operation_ids)

        for event in evidence.run_events[2:]:
            event_type = event.type
            payload = event.payload
            if event_type == "run.interrupted":
                if state not in {
                    "ready_for_step",
                    "model_in_flight",
                    "model_completed",
                    "tool_proposed",
                    "permission_pending",
                    "permission_resolved",
                    "tool_authorized",
                    "tool_in_flight",
                    "tool_recovering",
                    "tool_completed",
                }:
                    return False
                interrupted_state = state
                state = "interrupted"
                audit_kind = None
                continue
            if event_type in {
                "model.recovery.query.started",
                "model.recovery.resend.started",
            }:
                if (
                    state != "interrupted"
                    or interrupted_state != "model_in_flight"
                    or current_model is None
                    or payload.get("operation_id") != current_model.operation_id
                    or audit_kind not in {None, ExternalOperationKind.MODEL_CALL}
                ):
                    return False
                audit_kind = ExternalOperationKind.MODEL_CALL
                continue
            if event_type == "tool.recovery.retry.started":
                if (
                    state != "interrupted"
                    or current_tool is None
                    or current_call is None
                    or interrupted_state
                    not in {
                        "tool_in_flight",
                        "tool_recovering",
                        "permission_pending",
                        "permission_resolved",
                    }
                    or payload.get("operation")
                    != hashed_identity(current_tool.operation_id)
                    or payload.get("call") != hashed_identity(current_call.call_id)
                    or payload.get("tool") != hashed_identity(current_call.name)
                    or audit_kind not in {None, ExternalOperationKind.TOOL_CALL}
                ):
                    return False
                audit_kind = ExternalOperationKind.TOOL_CALL
                continue
            if event_type == "reconciliation.resolved":
                if (
                    state != "model_in_flight"
                    or current_model is None
                    or payload.get("operation_id") != current_model.operation_id
                    or payload.get("action")
                    != ReconciliationAction.CONFIRM_COMPLETED.value
                    or set(payload)
                    != {"request_id", "operation_id", "action", "actor", "evidence"}
                ):
                    return False
                continue
            if event_type == "run.recovery.started":
                if state != "interrupted" or interrupted_state is None:
                    return False
                if interrupted_state == "model_in_flight":
                    if (
                        current_model is None
                        or audit_kind is not ExternalOperationKind.MODEL_CALL
                    ):
                        return False
                    state = "model_in_flight"
                elif current_tool is not None and interrupted_state in {
                    "tool_in_flight",
                    "tool_recovering",
                    "permission_pending",
                    "permission_resolved",
                }:
                    if audit_kind is not ExternalOperationKind.TOOL_CALL:
                        return False
                    state = "tool_recovering"
                    pending_permission = None
                    permission_allowed = None
                    permission_decision = None
                    permission_recovery = True
                elif audit_kind is not None:
                    return False
                elif interrupted_state in {"ready_for_step", "tool_completed"}:
                    state = "ready_for_step"
                else:
                    state = "model_completed"
                    current_tool = None
                interrupted_state = None
                audit_kind = None
                continue
            if state == "interrupted":
                return False

            if event_type == "step.started":
                if state != "ready_for_step" or payload != {}:
                    return False
                turn += 1
                current_model = None
                current_tool = None
                current_call = None
                permission_decision = None
                initial_permission_decision = None
                permission_recovery = False
                permission_allowed = None
                model_deltas = []
                model_usage = None
                state = "model_starting"
                continue
            if event_type == "model.call.started":
                current_model = model_operations.get(turn)
                if (
                    state != "model_starting"
                    or current_model is None
                    or payload != {"model": descriptor.agent.model}
                ):
                    return False
                state = "model_in_flight"
                continue
            if event_type == "model.text.delta":
                if (
                    state != "model_in_flight"
                    or set(payload) != {"text"}
                    or not isinstance(payload["text"], str)
                ):
                    return False
                if terminal_status is not None:
                    model_deltas.append(payload["text"])
                continue
            if event_type == "model.usage.reported":
                if state != "model_in_flight":
                    return False
                try:
                    usage = TokenUsage.model_validate(payload)
                except Exception:
                    return False
                if usage.model_dump(mode="json") != payload:
                    return False
                if terminal_status is not None:
                    model_usage = usage
                state = "model_usage_reported"
                continue
            if event_type == "model.call.completed":
                if (
                    state not in {"model_in_flight", "model_usage_reported"}
                    or set(payload) != {"finish_reason"}
                    or (
                        payload["finish_reason"] is not None
                        and not isinstance(payload["finish_reason"], str)
                    )
                    or current_model is None
                    or current_model.status is not ExternalOperationStatus.COMPLETED
                    or current_model.outcome is None
                    or current_model.outcome.get("finish_reason")
                    != payload["finish_reason"]
                ):
                    return False
                if terminal_status is not None:
                    completed = RunRecoveryService._completed_model_outcome(
                        current_model
                    )
                    if completed is None:
                        return False
                    recovered = (
                        current_model.operation_id
                        in recovered_model_operation_ids
                    )
                    expected_usage = completed[3]
                    usage_required = recovered or any(
                        value is not None
                        for value in expected_usage.model_dump(
                            mode="json"
                        ).values()
                    )
                    if (
                        (
                            recovered
                            and not RunRecoveryService._is_exact_durable_text_prefix(
                                tuple(model_deltas),
                                completed[1],
                            )
                        )
                        or (not recovered and "".join(model_deltas) != completed[1])
                        or usage_required != (model_usage is not None)
                        or (
                            model_usage is not None
                            and model_usage != expected_usage
                        )
                    ):
                        return False
                state = "model_completed"
                continue
            if event_type == "model.call.failed":
                error = payload.get("error")
                if (
                    state != "model_in_flight"
                    or set(payload) != {"error"}
                    or not isinstance(error, dict)
                    or set(error) != {"code", "message", "retryable"}
                    or not isinstance(error["code"], str)
                    or not isinstance(error["message"], str)
                    or type(error["retryable"]) is not bool
                    or current_model is None
                    or current_model.status is not ExternalOperationStatus.FAILED
                    or current_model.model_dump(mode="json")["outcome"]
                    != {
                        "error": {
                            "code": error["code"],
                            "message": error["message"],
                        }
                    }
                ):
                    return False
                terminal_failure_payload = payload
                state = "model_failed"
                continue
            if event_type == "tool.call.proposed":
                completed = (
                    RunRecoveryService._completed_model_outcome(current_model)
                    if current_model is not None
                    else None
                )
                if (
                    state != "model_completed"
                    or set(payload) != {"call_id", "tool_name"}
                    or not all(
                        isinstance(payload[field], str) and bool(payload[field])
                        for field in ("call_id", "tool_name")
                    )
                    or completed is None
                    or len(completed[2]) != 1
                ):
                    return False
                raw_call = completed[2][0]
                if (
                    raw_call["index"] != 0
                    or raw_call["call_id"] != payload["call_id"]
                    or raw_call["name"] != payload["tool_name"]
                ):
                    return False
                current_call = ToolCallCompleted(
                    index=0,
                    call_id=str(raw_call["call_id"]),
                    name=str(raw_call["name"]),
                    arguments_json=str(raw_call["arguments_json"]),
                )
                current_tool = None
                pending_permission = None
                permission_allowed = None
                permission_decision = None
                permission_recovery = False
                replay_request = RunRecoveryService._recorded_permission_request(
                    evidence,
                    current_call,
                    request_id="prm_recovery_replay",
                )
                initial_permission_decision = (
                    RunRecoveryService._recorded_permission_decision(
                        evidence,
                        replay_request,
                    )
                    if replay_request is not None
                    else None
                )
                state = "tool_proposed"
                continue
            if event_type == "permission.requested":
                if (
                    state not in {"tool_proposed", "tool_recovering"}
                    or current_call is None
                    or initial_permission_decision is None
                    or initial_permission_decision.action != "ask"
                ):
                    return False
                permission_recovery = state == "tool_recovering"
                if permission_recovery:
                    if (
                        set(payload) != {"request", "tool"}
                        or not RunRecoveryService._is_hashed_identity(
                            payload["request"]
                        )
                        or payload["tool"] != hashed_identity(current_call.name)
                    ):
                        return False
                else:
                    request = RunRecoveryService._validated_permission_request(
                        evidence,
                        current_call,
                        payload,
                    )
                    if request is None:
                        return False
                    recorded_decision = (
                        RunRecoveryService._recorded_permission_decision(
                            evidence,
                            request,
                        )
                    )
                    if recorded_decision is None or recorded_decision.action != "ask":
                        return False
                pending_permission = payload
                permission_allowed = None
                permission_decision = None
                state = "permission_pending"
                continue
            if event_type == "permission.resolved":
                if state != "permission_pending" or pending_permission is None:
                    return False
                if permission_recovery:
                    if (
                        set(payload) != {"request", "tool", "allowed"}
                        or payload["request"] != pending_permission["request"]
                        or payload["tool"] != pending_permission["tool"]
                        or type(payload["allowed"]) is not bool
                    ):
                        return False
                    permission_allowed = payload["allowed"]
                else:
                    decision = RunRecoveryService._validated_permission_decision(
                        pending_permission["request"],
                        payload,
                    )
                    if decision is None:
                        return False
                    permission_decision = decision
                    permission_allowed = decision.allowed
                state = "permission_resolved"
                continue
            if event_type == "tool.call.authorized":
                recovering = state == "tool_recovering" or (
                    state == "permission_resolved" and permission_recovery
                )
                if (
                    current_call is None
                    or state
                    not in {"tool_proposed", "tool_recovering", "permission_resolved"}
                    or (state == "permission_resolved" and permission_allowed is not True)
                    or (
                        state in {"tool_proposed", "tool_recovering"}
                        and (
                            initial_permission_decision is None
                            or initial_permission_decision.action != "allow"
                        )
                    )
                    or (
                        state == "permission_resolved"
                        and (
                            initial_permission_decision is None
                            or initial_permission_decision.action != "ask"
                        )
                    )
                ):
                    return False
                expected = (
                    {
                        "call": hashed_identity(current_call.call_id),
                        "tool": hashed_identity(current_call.name),
                    }
                    if recovering
                    else {
                        "call_id": current_call.call_id,
                        "tool_name": current_call.name,
                    }
                )
                if payload != expected:
                    return False
                state = "tool_in_flight" if recovering else "tool_authorized"
                continue
            if event_type == "tool.call.started":
                current_tool = tool_operations.get(turn)
                if (
                    state != "tool_authorized"
                    or current_call is None
                    or current_tool is None
                    or payload
                    != {
                        "call_id": current_call.call_id,
                        "tool_name": current_call.name,
                    }
                ):
                    return False
                state = "tool_in_flight"
                continue
            if event_type == "tool.call.completed":
                if state not in {
                    "tool_proposed",
                    "tool_recovering",
                    "permission_resolved",
                    "tool_in_flight",
                }:
                    return False
                if state == "permission_resolved" and (
                    permission_allowed is not False
                    or initial_permission_decision is None
                    or initial_permission_decision.action != "ask"
                ):
                    return False
                if state in {"tool_proposed", "tool_recovering"} and (
                    initial_permission_decision is not None
                    and initial_permission_decision.action != "deny"
                ):
                    return False
                if (
                    state == "tool_recovering"
                    and initial_permission_decision is None
                ):
                    return False
                try:
                    result = ToolResult.model_validate(payload)
                except Exception:
                    return False
                if (
                    result.model_dump(mode="json") != payload
                    or current_call is None
                    or (result.call_id, result.tool_name)
                    != (current_call.call_id, current_call.name)
                    or (
                        permission_decision is not None
                        and not permission_decision.allowed
                        and result
                        != ToolResult.normalized_error(
                            current_call.call_id,
                            current_call.name,
                            ToolResultStatus.DENIED,
                            permission_decision.reason or "permission denied",
                        )
                    )
                    or not RunRecoveryService._is_authoritative_historical_tool_result(
                        evidence,
                        turn=turn,
                        result_index=completed_result_count,
                        call=current_call,
                        operation=current_tool,
                        result=result,
                        permission_decision=permission_decision,
                        initial_permission_decision=initial_permission_decision,
                        permission_recovery=permission_recovery,
                        permission_allowed=permission_allowed,
                    )
                ):
                    return False
                completed_result_count += 1
                state = "tool_completed"
                continue
            if event_type == "step.completed":
                completed_model = (
                    RunRecoveryService._completed_model_outcome(current_model)
                    if current_model is not None
                    else None
                )
                terminal_model_step = (
                    terminal_status is RunStatus.COMPLETED
                    and state == "model_completed"
                    and completed_model is not None
                    and not completed_model[2]
                )
                if (
                    state != "tool_completed"
                    and not terminal_model_step
                ) or payload != {}:
                    return False
                state = "ready_for_step"
                continue
            if event_type == "step.failed":
                if (
                    terminal_status is not RunStatus.FAILED
                    or state != "model_failed"
                    or payload != terminal_failure_payload
                ):
                    return False
                state = "step_failed"
                continue
            if event_type == "run.completed":
                if (
                    terminal_status is not RunStatus.COMPLETED
                    or state != "ready_for_step"
                ):
                    return False
                state = "terminal_completed"
                continue
            if event_type == "run.failed":
                if (
                    terminal_status is not RunStatus.FAILED
                    or state != "step_failed"
                    or payload != terminal_failure_payload
                ):
                    return False
                state = "terminal_failed"
                continue
            return False

        if completed_result_count != len(checkpoint.tool_results):
            return False
        if terminal_status is not None:
            expected_state = (
                "terminal_completed"
                if terminal_status is RunStatus.COMPLETED
                else "terminal_failed"
            )
            expected_operation_status = (
                ExternalOperationStatus.COMPLETED
                if terminal_status is RunStatus.COMPLETED
                else ExternalOperationStatus.FAILED
            )
            return (
                evidence.run.status is terminal_status
                and state == expected_state
                and turn == checkpoint.turn
                and checkpoint.phase is RunCheckpointPhase.TERMINAL
                and checkpoint.operation_id is None
                and current_model is not None
                and current_model.turn == checkpoint.turn
                and current_model.status is expected_operation_status
            )
        if state != "interrupted":
            return False
        if current_kind is None:
            if checkpoint.operation_id is not None:
                return False
            if checkpoint.phase is RunCheckpointPhase.READY_FOR_TOOL:
                return (
                    interrupted_state == "model_completed"
                    and current_model is not None
                    and current_model.turn == checkpoint.turn
                    and current_model.status is ExternalOperationStatus.COMPLETED
                )
            return (
                checkpoint.phase is RunCheckpointPhase.READY_FOR_MODEL
                and interrupted_state in {"ready_for_step", "tool_completed"}
                and turn == checkpoint.turn - 1
                and (
                    (checkpoint.turn == 0 and current_model is None)
                    or (
                        current_model is not None
                        and current_model.turn == checkpoint.turn - 1
                        and current_model.status
                        is ExternalOperationStatus.COMPLETED
                    )
                )
            )
        if checkpoint.operation_id is None:
            return False
        current = current_model if current_kind is ExternalOperationKind.MODEL_CALL else current_tool
        return current is not None and current.operation_id == checkpoint.operation_id

    @staticmethod
    def _is_valid_certified_provider_history(
        evidence: _RecoveryEvidence,
        *,
        base_request: ModelRequest | None = None,
        terminal_status: RunStatus | None = None,
        confirmed_operation_ids: frozenset[str] = frozenset(),
    ) -> bool:
        checkpoint = evidence.checkpoint
        descriptor = evidence.run.execution_descriptor
        if (
            checkpoint is None
            or descriptor is None
            or (
                terminal_status is not None
                and not RunRecoveryService._is_valid_run_event_envelope(
                    evidence,
                    allow_recovery_closed=True,
                )
            )
        ):
            return False
        if not RunRecoveryService._is_valid_certified_lifecycle_positions(
            evidence,
            current_kind=ExternalOperationKind.MODEL_CALL,
            terminal_status=terminal_status,
            confirmed_operation_ids=confirmed_operation_ids,
        ):
            return False
        if terminal_status is not None:
            if base_request is None:
                return False
            return RunRecoveryService._is_valid_certified_terminal_provider_turns(
                evidence,
                base_request=base_request,
                terminal_status=terminal_status,
            )
        events = evidence.run_events
        first_interrupted = next(
            index for index, event in enumerate(events) if event.type == "run.interrupted"
        )
        if any(
            event.type
            in {
                "run.recovery.started",
                "model.recovery.query.started",
                "model.recovery.resend.started",
                "tool.recovery.retry.started",
            }
            for event in events[:first_interrupted]
        ):
            return False
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
        logical_events = tuple(
            event
            for event in events
            if not RunRecoveryService._is_recovery_control_event(event)
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
            sum(event.type == event_type for event in logical_events) != expected
            for event_type, expected in expected_counts.items()
        ):
            return False
        if (
            any(
                event.payload != {}
                for event in logical_events
                if event.type == "step.started"
            )
            or any(
                event.payload != {"model": descriptor.agent.model}
                for event in logical_events
                if event.type == "model.call.started"
            )
            or sum(
                event.type == "permission.requested" for event in logical_events
            )
            != sum(event.type == "permission.resolved" for event in logical_events)
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

    @staticmethod
    def _is_valid_certified_terminal_provider_turns(
        evidence: _RecoveryEvidence,
        *,
        base_request: ModelRequest,
        terminal_status: RunStatus,
    ) -> bool:
        checkpoint = evidence.checkpoint
        if checkpoint is None or evidence.run.execution_descriptor is None:
            return False
        model_operations = {
            operation.turn: operation
            for operation in evidence.operations
            if isinstance(operation, ModelCallOperation)
        }
        if (
            len(model_operations)
            != sum(
                isinstance(operation, ModelCallOperation)
                for operation in evidence.operations
            )
            or tuple(sorted(model_operations))
            != tuple(range(checkpoint.turn + 1))
            or any(
                operation.run_id != evidence.run.run_id
                or operation.session_id != evidence.run.session_id
                for operation in model_operations.values()
            )
        ):
            return False
        messages_before = RunRecoveryService._messages_before_turn(
            evidence,
            base_request,
            checkpoint.turn,
        )
        if messages_before is None:
            return False
        final_operation = model_operations[checkpoint.turn]
        try:
            final_request = ModelRequest(
                model=base_request.model,
                messages=messages_before,
                tools=base_request.tools,
                params=dict(base_request.params),
                purpose=base_request.purpose,
            )
        except Exception:
            return False
        if (
            final_operation.provider_identity != base_request.model
            or final_operation.request_fingerprint
            != _model_request_fingerprint(final_request)
        ):
            return False

        output_parts: list[str] = []
        usage = TokenUsage()
        for turn in range(checkpoint.turn):
            completed = RunRecoveryService._completed_model_outcome(
                model_operations[turn]
            )
            if completed is None or len(completed[2]) != 1:
                return False
            output_parts.append(completed[1])
            usage = _add_usage(usage, completed[3])

        expected_messages = list(messages_before)
        if terminal_status is RunStatus.COMPLETED:
            completed = RunRecoveryService._completed_model_outcome(final_operation)
            if completed is None or completed[2]:
                return False
            output_parts.append(completed[1])
            usage = _add_usage(usage, completed[3])
            expected_messages.append(
                {"role": "assistant", "content": completed[1] or None}
            )
        elif (
            final_operation.status is not ExternalOperationStatus.FAILED
            or final_operation.outcome is None
            or set(final_operation.outcome) != {"error"}
        ):
            return False
        output_text = "".join(output_parts)
        if (
            checkpoint.model_dump(mode="json")["messages"]
            != expected_messages
            or "".join(checkpoint.output_parts) != output_text
            or checkpoint.usage != usage
            or evidence.run.output_text != output_text
            or evidence.run.usage != usage
            or evidence.run.tool_results != checkpoint.tool_results
        ):
            return False
        terminal_events = tuple(
            event
            for event in evidence.run_events
            if event.type
            == (
                "run.completed"
                if terminal_status is RunStatus.COMPLETED
                else "run.failed"
            )
        )
        if len(terminal_events) != 1:
            return False
        terminal_event = terminal_events[0]
        if terminal_status is RunStatus.COMPLETED:
            expected_payload: dict[str, Any] = {
                "output_text": output_text,
                "usage": usage.model_dump(mode="json"),
            }
            if checkpoint.tool_results:
                expected_payload["tool_results"] = [
                    result.model_dump(mode="json")
                    for result in checkpoint.tool_results
                ]
            return evidence.run.error is None and terminal_event.payload == expected_payload
        final_error = (
            final_operation.outcome.get("error")
            if final_operation.outcome is not None
            else None
        )
        return (
            isinstance(final_error, Mapping)
            and evidence.run.error is not None
            and evidence.run.error.model_dump(mode="json")
            == terminal_event.payload.get("error")
            and terminal_event.payload.get("error", {}).get("code")
            == final_error.get("code")
            and terminal_event.payload.get("error", {}).get("message")
            == final_error.get("message")
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

    def _effective_resolved_evidence(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
    ) -> _RecoveryEvidence | None:
        resolved = tuple(
            request
            for request in evidence.reconciliations
            if request.status is ReconciliationStatus.RESOLVED
        )
        resolution_events = tuple(
            event
            for event in evidence.run_events
            if event.type == "reconciliation.resolved"
        )
        if not resolved:
            return evidence if not resolution_events else None
        confirmed_history = any(
            request.resolution is not None
            and request.resolution.action is ReconciliationAction.CONFIRM_COMPLETED
            for request in resolved
        )
        if (
            len(resolved) != len(resolution_events)
            or not self._is_valid_run_event_envelope(
                evidence,
                allow_recovery_closed=confirmed_history,
            )
        ):
            return None

        requested_positions = {
            request.request_id: tuple(
                index
                for index, event in enumerate(evidence.run_events)
                if event.type == "reconciliation.requested"
                and event.payload.get("request_id") == request.request_id
            )
            for request in resolved
        }
        if any(len(indexes) != 1 for indexes in requested_positions.values()):
            return None
        resolved = tuple(
            sorted(
                resolved,
                key=lambda request: requested_positions[request.request_id][0],
            )
        )
        removed_event_indexes: set[int] = set()
        removed_operation_ids: set[str] = set()
        deferred_events: dict[int, list[EventEnvelope]] = {}
        for request in resolved:
            resolution = request.resolution
            if resolution is None or request.operation_id is None:
                return None
            operation = next(
                (
                    item
                    for item in evidence.operations
                    if item.operation_id == request.operation_id
                ),
                None,
            )
            requested_indexes = tuple(
                index
                for index, event in enumerate(evidence.run_events)
                if event.type == "reconciliation.requested"
                and event.payload
                == {
                    "request_id": request.request_id,
                    "operation_id": request.operation_id,
                    "reason": request.reason,
                }
            )
            resolved_indexes = tuple(
                index
                for index, event in enumerate(evidence.run_events)
                if event.type == "reconciliation.resolved"
                and event.event_id == resolution.event_id
                and event.occurred_at == resolution.decided_at
                and event.payload
                == {
                    "request_id": request.request_id,
                    "operation_id": request.operation_id,
                    "action": resolution.action.value,
                    "actor": thaw_json(resolution.actor),
                    "evidence": thaw_json(resolution.evidence),
                }
            )
            if resolution.action is ReconciliationAction.CONFIRM_COMPLETED:
                if isinstance(operation, ToolCallOperation):
                    if (
                        not self._is_exact_confirmed_tool_replay(
                            evidence,
                            base_request,
                            request,
                            operation,
                            removed_event_indexes=removed_event_indexes,
                            removed_operation_ids=removed_operation_ids,
                            deferred_events=deferred_events,
                        )
                        or len(requested_indexes) != 1
                        or len(resolved_indexes) != 1
                        or requested_indexes[0] == 0
                    ):
                        return None
                    interrupt_index = requested_indexes[0] - 1
                    decision_end_index = resolved_indexes[0] + 2
                    if decision_end_index >= len(evidence.run_events):
                        return None
                    deferred_events.setdefault(decision_end_index, []).append(
                        evidence.run_events[interrupt_index]
                    )
                    removed_event_indexes.add(interrupt_index)
                    removed_event_indexes.update(requested_indexes)
                    removed_event_indexes.update(resolved_indexes)
                    continue
                if (
                    not isinstance(operation, ModelCallOperation)
                    or not self._is_exact_confirmed_model_replay(
                        evidence,
                        base_request,
                        request,
                        operation,
                    )
                    or len(requested_indexes) != 1
                    or len(resolved_indexes) != 1
                ):
                    return None
                try:
                    confirmed = ProviderRecoveryResult.model_validate_json(
                        json.dumps(
                            thaw_json(resolution.evidence)["provider_result"],
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                except Exception:
                    return None
                if requested_indexes[0] == 0:
                    return None
                interrupt_index = requested_indexes[0] - 1
                if confirmed.disposition is ProviderRecoveryDisposition.FAILED:
                    if evidence.run.status is not RunStatus.FAILED:
                        return None
                    removed_event_indexes.add(interrupt_index)
                    removed_event_indexes.update(requested_indexes)
                    continue
                if (
                    confirmed.disposition
                    is not ProviderRecoveryDisposition.COMPLETED
                ):
                    return None
                if confirmed.tool_call is None:
                    if evidence.run.status not in {
                        RunStatus.COMPLETED,
                        RunStatus.FAILED,
                    }:
                        return None
                    removed_event_indexes.add(interrupt_index)
                    removed_event_indexes.update(requested_indexes)
                    continue
                decision_end_index = resolved_indexes[0] + 2
                deferred_events.setdefault(decision_end_index, []).append(
                    evidence.run_events[interrupt_index]
                )
                removed_event_indexes.add(interrupt_index)
                removed_event_indexes.update(requested_indexes)
                continue
            expected_outcome = {
                "reconciliation": {
                    "request_id": request.request_id,
                    "action": resolution.action.value,
                }
            }
            if (
                operation is None
                or operation.status is not ExternalOperationStatus.FAILED
                or operation.model_dump(mode="json")["outcome"]
                != expected_outcome
                or resolution.action
                not in {
                    ReconciliationAction.CONFIRM_NOT_EXECUTED,
                    ReconciliationAction.RETRY,
                }
                or len(requested_indexes) != 1
                or len(resolved_indexes) != 1
                or resolved_indexes[0] != requested_indexes[0] + 1
                or requested_indexes[0] == 0
                or evidence.run_events[requested_indexes[0] - 1].type
                != "run.interrupted"
            ):
                return None
            interrupt_index = requested_indexes[0] - 1
            lower_bound = max(
                (
                    index
                    for index in range(interrupt_index)
                    if evidence.run_events[index].type
                    in {"run.interrupted", "run.recovery.started"}
                ),
                default=1,
            )
            if isinstance(operation, ModelCallOperation):
                starts = tuple(
                    index
                    for index in range(lower_bound + 1, interrupt_index)
                    if evidence.run_events[index].type == "step.started"
                    and sum(
                        event.type == "step.completed"
                        for event in evidence.run_events[:index]
                    )
                    == operation.turn
                )
            else:
                starts = tuple(
                    index
                    for index in range(lower_bound + 1, interrupt_index)
                    if evidence.run_events[index].type == "tool.call.proposed"
                    and sum(
                        event.type == "step.completed"
                        for event in evidence.run_events[:index]
                    )
                    == operation.turn
                )
            if len(starts) != 1:
                return None
            attempt_start = starts[0]
            if not self._is_exact_resolved_attempt(
                evidence,
                base_request,
                request,
                operation,
                attempt_start=attempt_start,
            ):
                return None
            if not self._is_valid_resolved_attempt_lifecycle(
                evidence,
                base_request,
                operation,
                interrupt_index=interrupt_index,
                removed_event_indexes=removed_event_indexes,
                removed_operation_ids=frozenset(removed_operation_ids),
                deferred_events=deferred_events,
            ):
                return None
            removed_event_indexes.update(range(attempt_start, interrupt_index))
            removed_event_indexes.update(requested_indexes)
            removed_event_indexes.update(resolved_indexes)
            removed_operation_ids.add(operation.operation_id)

        retained_events: list[EventEnvelope] = []
        for index, event in enumerate(evidence.run_events):
            if index not in removed_event_indexes:
                retained_events.append(event)
            retained_events.extend(deferred_events.get(index, ()))
        normalized_events = tuple(
            event.model_copy(update={"sequence": index})
            for index, event in enumerate(retained_events, start=1)
        )
        normalized = replace(
            evidence,
            operations=tuple(
                operation
                for operation in evidence.operations
                if operation.operation_id not in removed_operation_ids
            ),
            pending=(),
            run_events=normalized_events,
            run_event_cursors=tuple(range(1, len(normalized_events) + 1)),
        )
        if not self._is_valid_run_event_envelope(
            normalized,
            allow_recovery_closed=confirmed_history,
        ):
            return None
        checkpoint = normalized.checkpoint
        if (
            checkpoint is not None
            and checkpoint.phase is RunCheckpointPhase.READY_FOR_MODEL
            and not self._is_exact_ready_model_relation(
                normalized,
                base_request,
            )
        ):
            return None
        return normalized

    def _is_valid_resolved_attempt_lifecycle(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        operation: ExternalOperation,
        *,
        interrupt_index: int,
        removed_event_indexes: set[int],
        removed_operation_ids: frozenset[str],
        deferred_events: Mapping[int, list[EventEnvelope]] | None = None,
    ) -> bool:
        retained_list: list[tuple[int, EventEnvelope]] = []
        for index, (cursor, event) in enumerate(
            zip(evidence.run_event_cursors, evidence.run_events)
        ):
            if index > interrupt_index:
                break
            if index not in removed_event_indexes:
                retained_list.append((cursor, event))
            if deferred_events is not None:
                retained_list.extend(
                    (0, deferred) for deferred in deferred_events.get(index, ())
                )
        retained = tuple(retained_list)
        projected_events = tuple(
            event.model_copy(update={"sequence": index})
            for index, (_, event) in enumerate(retained, start=1)
        )
        started_operation = operation.model_copy(
            update={
                "status": ExternalOperationStatus.STARTED,
                "outcome": None,
            }
        )
        projected_operations = tuple(
            started_operation
            if item.operation_id == operation.operation_id
            else item
            for item in evidence.operations
            if item.operation_id == operation.operation_id
            or (
                item.operation_id not in removed_operation_ids
                and (
                    item.turn < operation.turn
                    or (
                        isinstance(operation, ToolCallOperation)
                        and item.turn == operation.turn
                        and isinstance(item, ModelCallOperation)
                        and item.status is ExternalOperationStatus.COMPLETED
                    )
                )
            )
        )
        checkpoint = evidence.checkpoint
        descriptor = evidence.run.execution_descriptor
        messages = self._messages_before_turn(
            evidence,
            base_request,
            operation.turn,
        )
        if checkpoint is None or descriptor is None or messages is None:
            return False

        completed_turns = operation.turn + (
            1 if isinstance(operation, ToolCallOperation) else 0
        )
        output_parts: list[str] = []
        cumulative: dict[str, int | None] = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        current_completed: tuple[
            str | None,
            str,
            tuple[Mapping[str, object], ...],
            TokenUsage,
        ] | None = None
        for turn in range(completed_turns):
            matches = tuple(
                item
                for item in projected_operations
                if isinstance(item, ModelCallOperation)
                and item.turn == turn
                and item.status is ExternalOperationStatus.COMPLETED
            )
            if len(matches) != 1:
                return False
            completed = self._completed_model_outcome(matches[0])
            if completed is None:
                return False
            current_completed = completed
            output_parts.append(completed[1])
            usage = completed[3]
            for field in cumulative:
                value = getattr(usage, field)
                if value is not None:
                    cumulative[field] = (cumulative[field] or 0) + value

        projected_messages = list(messages)
        if isinstance(operation, ToolCallOperation):
            if current_completed is None or len(current_completed[2]) != 1:
                return False
            raw_call = current_completed[2][0]
            projected_messages.append(
                {
                    "role": "assistant",
                    "content": current_completed[1] or None,
                    "tool_calls": [
                        {
                            "id": raw_call["call_id"],
                            "type": "function",
                            "function": {
                                "name": raw_call["name"],
                                "arguments": raw_call["arguments_json"],
                            },
                        }
                    ],
                }
            )

        projected_checkpoint = checkpoint.model_copy(
            update={
                "turn": operation.turn,
                "phase": (
                    RunCheckpointPhase.MODEL_IN_FLIGHT
                    if isinstance(operation, ModelCallOperation)
                    else RunCheckpointPhase.TOOL_IN_FLIGHT
                ),
                "operation_id": operation.operation_id,
                "messages": tuple(projected_messages),
                "output_parts": tuple(output_parts),
                "usage": TokenUsage(**cumulative),
                "tool_results": checkpoint.tool_results[: operation.turn],
            }
        )
        projected = replace(
            evidence,
            checkpoint=projected_checkpoint,
            operations=projected_operations,
            pending=(),
            reconciliations=(),
            run_events=projected_events,
            run_event_cursors=(
                tuple(range(1, len(retained) + 1))
                if deferred_events
                else tuple(cursor for cursor, _ in retained)
            ),
        )
        return self._is_resolution_operation_certified(
            projected,
            base_request,
            started_operation,
        )

    @staticmethod
    def _is_exact_resolved_attempt(
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        request: ReconciliationRequest,
        operation: ExternalOperation,
        *,
        attempt_start: int,
    ) -> bool:
        resolution = request.resolution
        checkpoint = evidence.checkpoint
        if resolution is None or checkpoint is None:
            return False
        expected_resolution_evidence: dict[str, object]
        if resolution.action is ReconciliationAction.CONFIRM_NOT_EXECUTED:
            expected_resolution_evidence = {"disposition": "not_executed"}
        elif resolution.action is ReconciliationAction.RETRY:
            expected_resolution_evidence = {
                "acknowledge_duplicate_side_effect_risk": True
            }
        else:
            return False
        is_model = isinstance(operation, ModelCallOperation)
        expected_reason = (
            "model_call_unknown_outcome"
            if is_model
            else "tool_call_unknown_outcome"
        )
        expected_phase = (
            RunCheckpointPhase.MODEL_IN_FLIGHT
            if is_model
            else RunCheckpointPhase.TOOL_IN_FLIGHT
        )
        expected_turn = (
            sum(
                event.type == "step.completed"
                for event in evidence.run_events[:attempt_start]
            )
        )
        if (
            request.run_id != evidence.run.run_id
            or request.session_id != evidence.run.session_id
            or operation.run_id != evidence.run.run_id
            or operation.session_id != evidence.run.session_id
            or operation.turn != expected_turn
            or operation.operation_id != request.operation_id
            or request.reason != expected_reason
            or dict(request.details)
            != {"checkpoint_phase": expected_phase.value}
            or thaw_json(resolution.evidence) != expected_resolution_evidence
        ):
            return False
        messages = RunRecoveryService._messages_before_turn(
            evidence,
            base_request,
            expected_turn,
        )
        if messages is None:
            return False
        if is_model:
            assert isinstance(operation, ModelCallOperation)
            metadata = dict(operation.recovery_metadata)
            metadata_valid = metadata == {
                "authoritative_status": False,
                "same_operation_id_resend": False,
            } or (
                set(metadata)
                == {
                    "adapter_id",
                    "adapter_version",
                    "authoritative_status",
                    "same_operation_id_resend",
                }
                and all(
                    isinstance(metadata[field], str) and bool(metadata[field])
                    for field in ("adapter_id", "adapter_version")
                )
                and type(metadata["authoritative_status"]) is bool
                and type(metadata["same_operation_id_resend"]) is bool
            )
            try:
                expected_fingerprint = _model_request_fingerprint(
                    ModelRequest(
                        model=base_request.model,
                        messages=messages,
                        tools=base_request.tools,
                        params=dict(base_request.params),
                        purpose=base_request.purpose,
                    )
                )
            except Exception:
                return False
            return (
                metadata_valid
                and operation.provider_identity == base_request.model
                and operation.request_fingerprint == expected_fingerprint
            )

        assert isinstance(operation, ToolCallOperation)
        proposed = evidence.run_events[attempt_start]
        if (
            proposed.type != "tool.call.proposed"
            or set(proposed.payload) != {"call_id", "tool_name"}
        ):
            return False
        model_operations = tuple(
            item
            for item in evidence.operations
            if isinstance(item, ModelCallOperation)
            and item.turn == expected_turn
            and item.status is ExternalOperationStatus.COMPLETED
        )
        if len(model_operations) != 1:
            return False
        completed = RunRecoveryService._completed_model_outcome(model_operations[0])
        if completed is None:
            return False
        raw_calls = tuple(
            call
            for call in completed[2]
            if call["call_id"] == proposed.payload["call_id"]
            and call["name"] == proposed.payload["tool_name"]
        )
        if len(raw_calls) != 1:
            return False
        raw_call = raw_calls[0]
        capabilities = tuple(
            capability
            for capability in evidence.run.execution_descriptor.tools
            if capability.spec.name == raw_call["name"]
        ) if evidence.run.execution_descriptor is not None else ()
        if len(capabilities) != 1:
            return False
        capability = capabilities[0]
        try:
            arguments = json.loads(
                str(raw_call["arguments_json"]),
                parse_constant=_reject_json_constant,
            )
            if not isinstance(arguments, dict):
                return False
            Draft202012Validator(
                thaw_json(capability.spec.input_schema)
            ).validate(arguments)
            raw_index = raw_call["index"]
            if type(raw_index) is not int:
                return False
            call = ToolCallCompleted(
                index=raw_index,
                call_id=str(raw_call["call_id"]),
                name=str(raw_call["name"]),
                arguments_json=str(raw_call["arguments_json"]),
            )
        except (
            JSONSchemaValidationError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return False
        expected_metadata = (
            {"safe_retry": False, "retry_class": "unsafe"}
            if capability.spec.retry_policy is ToolRetryPolicy.NEVER
            else {
                "safe_retry": True,
                "retry_class": capability.spec.retry_policy.value,
            }
        )
        return (
            operation.tool_identity == capability.capability_hash
            and dict(operation.recovery_metadata) == expected_metadata
            and operation.request_fingerprint
            == _tool_request_fingerprint(call, capability, arguments)
        )

    @staticmethod
    def _messages_before_turn(
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        target_turn: int,
    ) -> tuple[dict[str, Any], ...] | None:
        descriptor = evidence.run.execution_descriptor
        checkpoint = evidence.checkpoint
        if descriptor is None or checkpoint is None or target_turn > checkpoint.turn:
            return None
        messages: list[dict[str, Any]] = list(
            descriptor.model_dump(mode="json")["messages"]
        )
        try:
            for turn in range(target_turn):
                completed_operations = tuple(
                    operation
                    for operation in evidence.operations
                    if isinstance(operation, ModelCallOperation)
                    and operation.turn == turn
                    and operation.status is ExternalOperationStatus.COMPLETED
                )
                if len(completed_operations) != 1:
                    return None
                operation = completed_operations[0]
                request = ModelRequest(
                    model=base_request.model,
                    messages=tuple(messages),
                    tools=base_request.tools,
                    params=dict(base_request.params),
                    purpose=base_request.purpose,
                )
                if (
                    operation.provider_identity != base_request.model
                    or operation.request_fingerprint
                    != _model_request_fingerprint(request)
                ):
                    return None
                completed = RunRecoveryService._completed_model_outcome(operation)
                if completed is None or len(completed[2]) != 1:
                    return None
                _finish_reason, text, calls, _usage = completed
                call = calls[0]
                messages.append(
                    {
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
                    }
                )
                if turn >= len(checkpoint.tool_results):
                    return None
                result = checkpoint.tool_results[turn]
                if (result.call_id, result.tool_name) != (
                    call["call_id"],
                    call["name"],
                ):
                    return None
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "name": result.tool_name,
                        "content": result.content,
                    }
                )
        except (KeyError, TypeError, ValueError):
            return None
        return tuple(messages)

    @staticmethod
    def _is_exact_durable_text_prefix(
        deltas: tuple[str, ...],
        full_text: str,
    ) -> bool:
        return full_text.startswith("".join(deltas))

    @staticmethod
    def _current_model_deltas_before_interrupt(
        evidence: _RecoveryEvidence,
        operation: ModelCallOperation,
    ) -> tuple[str, ...] | None:
        checkpoint = evidence.checkpoint
        events = evidence.run_events
        if (
            checkpoint is None
            or operation.status is not ExternalOperationStatus.STARTED
            or checkpoint.operation_id != operation.operation_id
            or not events
            or events[-1].type != "run.interrupted"
        ):
            return None
        starts = tuple(
            index
            for index, event in enumerate(events[:-1])
            if event.type == "model.call.started"
        )
        if not starts:
            return None
        segment = events[starts[-1] + 1 : -1]
        if any(
            event.type
            in {
                "step.started",
                "model.call.started",
                "model.call.completed",
                "model.call.failed",
            }
            for event in segment
        ):
            return None
        deltas = tuple(
            event.payload.get("text")
            for event in segment
            if event.type == "model.text.delta"
        )
        return (
            tuple(delta for delta in deltas if isinstance(delta, str))
            if all(isinstance(delta, str) for delta in deltas)
            else None
        )

    def _is_safe_checkpoint(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
    ) -> bool:
        effective = self._effective_resolved_evidence(
            evidence,
            base_request,
        )
        if effective is None:
            return False
        return self._is_certified_safe_checkpoint(effective, base_request)

    def _is_certified_safe_checkpoint(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
    ) -> bool:
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
            return RunRecoveryService._is_exact_ready_tool_relation(
                evidence,
                base_request,
            )
        return RunRecoveryService._is_exact_ready_model_relation(
            evidence,
            base_request,
        )

    @staticmethod
    def _is_exact_ready_tool_relation(
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
    ) -> bool:
        checkpoint = evidence.checkpoint
        assert checkpoint is not None
        descriptor = evidence.run.execution_descriptor
        if (
            descriptor is None
            or checkpoint.phase is not RunCheckpointPhase.READY_FOR_TOOL
            or checkpoint.operation_id is not None
            or len(checkpoint.tool_results) != checkpoint.turn
            or not RunRecoveryService._is_valid_run_event_envelope(evidence)
        ):
            return False

        model_operations = {
            operation.turn: operation
            for operation in evidence.operations
            if isinstance(operation, ModelCallOperation)
        }
        if len(model_operations) != sum(
            isinstance(operation, ModelCallOperation)
            for operation in evidence.operations
        ) or tuple(sorted(model_operations)) != tuple(range(checkpoint.turn + 1)):
            return False
        if any(operation.turn > checkpoint.turn for operation in evidence.operations):
            return False
        current_operations = tuple(
            operation
            for operation in evidence.operations
            if operation.turn == checkpoint.turn
        )
        if current_operations != (model_operations[checkpoint.turn],):
            return False

        reconstructed_messages = list(
            descriptor.model_dump(mode="json")["messages"]
        )
        reconstructed_output: list[str] = []
        cumulative: dict[str, int | None] = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        completed: list[
            tuple[
                ModelCallOperation,
                str | None,
                str,
                TokenUsage,
            ]
        ] = []
        try:
            for turn in range(checkpoint.turn + 1):
                operation = model_operations[turn]
                turn_operations = tuple(
                    item for item in evidence.operations if item.turn == turn
                )
                tool_operations = tuple(
                    item
                    for item in turn_operations
                    if isinstance(item, ToolCallOperation)
                )
                if (
                    operation.run_id != evidence.run.run_id
                    or operation.session_id != evidence.run.session_id
                    or operation.provider_identity != base_request.model
                    or len(tool_operations) > (0 if turn == checkpoint.turn else 1)
                    or len(turn_operations) != 1 + len(tool_operations)
                ):
                    return False
                request = ModelRequest(
                    model=base_request.model,
                    messages=tuple(reconstructed_messages),
                    tools=base_request.tools,
                    params=dict(base_request.params),
                    purpose=base_request.purpose,
                )
                if (
                    _model_request_fingerprint(request)
                    != operation.request_fingerprint
                ):
                    return False
                outcome = RunRecoveryService._completed_model_outcome(operation)
                if outcome is None or len(outcome[2]) != 1:
                    return False
                finish_reason, text, calls, usage = outcome
                call = calls[0]
                reconstructed_messages.append(
                    {
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
                    }
                )
                reconstructed_output.append(text)
                for field in cumulative:
                    value = getattr(usage, field)
                    if value is not None:
                        cumulative[field] = (cumulative[field] or 0) + value
                if turn < checkpoint.turn:
                    result = checkpoint.tool_results[turn]
                    if (result.call_id, result.tool_name) != (
                        call["call_id"],
                        call["name"],
                    ):
                        return False
                    reconstructed_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.call_id,
                            "name": result.tool_name,
                            "content": result.content,
                        }
                    )
                completed.append(
                    (operation, finish_reason, text, usage)
                )
        except (KeyError, TypeError, ValueError):
            return False

        if (
            checkpoint.model_dump(mode="json")["messages"]
            != reconstructed_messages
            or "".join(checkpoint.output_parts) != "".join(reconstructed_output)
            or checkpoint.usage != TokenUsage(**cumulative)
            or not RunRecoveryService._is_valid_certified_lifecycle_positions(
                evidence,
                current_kind=None,
            )
        ):
            return False

        step_positions = tuple(
            index
            for index, event in enumerate(evidence.run_events)
            if event.type == "step.started"
        )
        if len(step_positions) != checkpoint.turn + 1:
            return False
        for turn, (operation, finish_reason, text, usage) in enumerate(completed):
            start = step_positions[turn]
            end = (
                step_positions[turn + 1]
                if turn < checkpoint.turn
                else len(evidence.run_events)
            )
            segment = evidence.run_events[start:end]
            started = tuple(
                index
                for index, event in enumerate(segment)
                if event.type == "model.call.started"
            )
            terminal = tuple(
                index
                for index, event in enumerate(segment)
                if event.type == "model.call.completed"
            )
            if (
                len(started) != 1
                or len(terminal) != 1
                or started[0] >= terminal[0]
                or segment[started[0]].payload != {"model": base_request.model}
                or segment[terminal[0]].payload
                != {"finish_reason": finish_reason}
            ):
                return False
            between = segment[started[0] + 1 : terminal[0]]
            deltas = tuple(
                event.payload.get("text")
                for event in between
                if event.type == "model.text.delta"
            )
            recovered = any(
                (
                    event.type
                    in {"model.recovery.query.started", "model.recovery.resend.started"}
                    or (
                        event.type == "reconciliation.resolved"
                        and event.payload.get("action")
                        == ReconciliationAction.CONFIRM_COMPLETED.value
                    )
                )
                and event.payload.get("operation_id") == operation.operation_id
                for event in segment
            )
            if any(not isinstance(delta, str) for delta in deltas):
                return False
            text_deltas = tuple(
                delta for delta in deltas if isinstance(delta, str)
            )
            if (
                recovered
                and not RunRecoveryService._is_exact_durable_text_prefix(
                    text_deltas,
                    text,
                )
            ) or (not recovered and "".join(text_deltas) != text):
                return False
            usage_events = tuple(
                event for event in between if event.type == "model.usage.reported"
            )
            expected_usage = usage.model_dump(mode="json")
            usage_event_required = recovered or any(
                value is not None for value in expected_usage.values()
            )
            if (
                usage_event_required != (len(usage_events) == 1)
                or (usage_events and usage_events[0].payload != expected_usage)
            ):
                return False
        return True

    @staticmethod
    def _is_exact_ready_model_relation(
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
    ) -> bool:
        checkpoint = evidence.checkpoint
        descriptor = evidence.run.execution_descriptor
        if (
            checkpoint is None
            or descriptor is None
            or evidence.run.status is not RunStatus.INTERRUPTED
            or checkpoint.run_id != evidence.run.run_id
            or checkpoint.session_id != evidence.run.session_id
            or checkpoint.phase is not RunCheckpointPhase.READY_FOR_MODEL
            or checkpoint.operation_id is not None
            or len(checkpoint.tool_results) != checkpoint.turn
            or evidence.pending
            or not RunRecoveryService._is_valid_run_event_envelope(evidence)
        ):
            return False

        model_operations = {
            operation.turn: operation
            for operation in evidence.operations
            if isinstance(operation, ModelCallOperation)
        }
        tool_operations = {
            operation.turn: operation
            for operation in evidence.operations
            if isinstance(operation, ToolCallOperation)
        }
        if (
            len(model_operations)
            != sum(
                isinstance(operation, ModelCallOperation)
                for operation in evidence.operations
            )
            or len(tool_operations)
            != sum(
                isinstance(operation, ToolCallOperation)
                for operation in evidence.operations
            )
            or tuple(sorted(model_operations)) != tuple(range(checkpoint.turn))
            or any(turn >= checkpoint.turn for turn in tool_operations)
            or any(
                operation.status is ExternalOperationStatus.STARTED
                for operation in evidence.operations
            )
        ):
            return False

        reconstructed_messages = list(
            descriptor.model_dump(mode="json")["messages"]
        )
        reconstructed_output: list[str] = []
        cumulative = TokenUsage()
        completed: list[
            tuple[ModelCallOperation, str | None, str, TokenUsage]
        ] = []
        try:
            for turn in range(checkpoint.turn):
                operation = model_operations[turn]
                turn_operations = tuple(
                    item for item in evidence.operations if item.turn == turn
                )
                tool_operation = tool_operations.get(turn)
                if (
                    operation.run_id != evidence.run.run_id
                    or operation.session_id != evidence.run.session_id
                    or operation.provider_identity != base_request.model
                    or turn_operations
                    != (
                        (operation,)
                        if tool_operation is None
                        else (operation, tool_operation)
                    )
                ):
                    return False
                request = ModelRequest(
                    model=base_request.model,
                    messages=tuple(reconstructed_messages),
                    tools=base_request.tools,
                    params=dict(base_request.params),
                    purpose=base_request.purpose,
                )
                if (
                    operation.request_fingerprint
                    != _model_request_fingerprint(request)
                ):
                    return False
                outcome = RunRecoveryService._completed_model_outcome(operation)
                if outcome is None or len(outcome[2]) != 1:
                    return False
                finish_reason, text, calls, usage = outcome
                call = calls[0]
                reconstructed_messages.append(
                    {
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
                    }
                )
                cumulative = _add_usage(cumulative, usage)
                result = checkpoint.tool_results[turn]
                if (result.call_id, result.tool_name) != (
                    call["call_id"],
                    call["name"],
                ):
                    return False
                reconstructed_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "name": result.tool_name,
                        "content": result.content,
                    }
                )
                completed.append((operation, finish_reason, text, usage))
        except (KeyError, TypeError, ValueError):
            return False

        checkpoint_transitions = sum(
            event.type
            in {
                "model.call.started",
                "model.call.completed",
                "permission.requested",
                "permission.resolved",
                "tool.call.started",
                "tool.call.completed",
            }
            for event in evidence.run_events
        )
        discarded_attempt_transitions = 2 * sum(
            request.resolution is not None
            and request.resolution.action
            in {
                ReconciliationAction.CONFIRM_NOT_EXECUTED,
                ReconciliationAction.RETRY,
            }
            for request in evidence.reconciliations
        )
        if (
            checkpoint.checkpoint_version
            != 1 + checkpoint_transitions + discarded_attempt_transitions
            or checkpoint.model_dump(mode="json")["messages"]
            != reconstructed_messages
            or checkpoint.usage != cumulative
            or not RunRecoveryService._is_valid_certified_lifecycle_positions(
                evidence,
                current_kind=None,
            )
        ):
            return False

        step_positions = tuple(
            index
            for index, event in enumerate(evidence.run_events)
            if event.type == "step.started"
        )
        if len(step_positions) != checkpoint.turn:
            return False
        for turn, (operation, finish_reason, text, usage) in enumerate(completed):
            start = step_positions[turn]
            end = (
                step_positions[turn + 1]
                if turn + 1 < checkpoint.turn
                else len(evidence.run_events)
            )
            segment = evidence.run_events[start:end]
            started = tuple(
                index
                for index, event in enumerate(segment)
                if event.type == "model.call.started"
            )
            terminal = tuple(
                index
                for index, event in enumerate(segment)
                if event.type == "model.call.completed"
            )
            if (
                len(started) != 1
                or len(terminal) != 1
                or started[0] >= terminal[0]
                or segment[started[0]].payload != {"model": base_request.model}
                or segment[terminal[0]].payload
                != {"finish_reason": finish_reason}
            ):
                return False
            between = segment[started[0] + 1 : terminal[0]]
            deltas = tuple(
                event.payload.get("text")
                for event in between
                if event.type == "model.text.delta"
            )
            recovered = any(
                (
                    event.type
                    in {
                        "model.recovery.query.started",
                        "model.recovery.resend.started",
                    }
                    or (
                        event.type == "reconciliation.resolved"
                        and event.payload.get("action")
                        == ReconciliationAction.CONFIRM_COMPLETED.value
                    )
                )
                and event.payload.get("operation_id") == operation.operation_id
                for event in segment
            )
            if any(not isinstance(delta, str) for delta in deltas):
                return False
            text_deltas = tuple(
                delta for delta in deltas if isinstance(delta, str)
            )
            if (
                recovered
                and not RunRecoveryService._is_exact_durable_text_prefix(
                    text_deltas,
                    text,
                )
            ) or (not recovered and "".join(text_deltas) != text):
                return False
            reconstructed_output.extend(text_deltas)
            if recovered:
                reconstructed_output.append(text)
            usage_events = tuple(
                event
                for event in between
                if event.type == "model.usage.reported"
            )
            expected_usage = usage.model_dump(mode="json")
            usage_required = recovered or any(
                value is not None for value in expected_usage.values()
            )
            if (
                usage_required != (len(usage_events) == 1)
                or (usage_events and usage_events[0].payload != expected_usage)
            ):
                return False
        return checkpoint.output_parts == tuple(reconstructed_output)

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

    @staticmethod
    def _is_exact_provider_adapter_registration(
        operation: ModelCallOperation,
        *,
        planned: ProviderRecoveryAdapter | None,
        current: ProviderRecoveryAdapter | None,
    ) -> bool:
        if planned is None or current is not planned:
            return False
        metadata = operation.recovery_metadata
        return dict(metadata) == {
            "adapter_id": current.adapter_id,
            "adapter_version": current.version,
            "authoritative_status": current.authoritative_status,
            "same_operation_id_resend": current.same_operation_id_resend,
        } and (current.authoritative_status or current.same_operation_id_resend)

    def _certified_tool_call(
        self,
        evidence: _RecoveryEvidence,
        base_request: ModelRequest,
        operation: ToolCallOperation,
        *,
        allow_unsafe: bool = False,
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
            step_positions = tuple(
                index
                for index, event in enumerate(evidence.run_events)
                if event.type == "step.started"
            )
            if checkpoint.turn >= len(step_positions):
                return None
            current_step = step_positions[checkpoint.turn]
            current_tool_starts = tuple(
                index
                for index, event in enumerate(evidence.run_events)
                if index > current_step and event.type == "tool.call.started"
            )
            if len(current_tool_starts) != 1:
                return None
            initial_interrupt = next(
                (
                    index
                    for index, event in enumerate(evidence.run_events)
                    if index > current_tool_starts[0]
                    and event.type == "run.interrupted"
                ),
                -1,
            )
            if initial_interrupt < 0:
                return None
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

                recorded_permission_request = self._recorded_permission_request(
                    evidence,
                    call,
                    request_id="prm_tool_history_replay",
                )
                initial_permission = (
                    self._recorded_permission_decision(
                        evidence,
                        recorded_permission_request,
                    )
                    if recorded_permission_request is not None
                    else None
                )
                result: ToolResult | None = None
                permission_allowed: bool | None = None
                if turn == checkpoint.turn:
                    assert tool_operation is not None
                    assert registered is not None
                    if (
                        tool_operation != operation
                        or tool_operation.status is not ExternalOperationStatus.STARTED
                        or (
                            registered.spec.retry_policy is ToolRetryPolicy.NEVER
                            and not allow_unsafe
                        )
                    ):
                        return None
                    certified = _CertifiedToolRecovery(
                        call=call,
                        registered=registered,
                    )
                    if (
                        initial_permission is not None
                        and initial_permission.action == "ask"
                    ):
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
                            if (
                                initial_permission is not None
                                and initial_permission.action == "ask"
                            ):
                                permission_allowed = False
                            elif (
                                initial_permission is None
                                or initial_permission.action != "deny"
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
                        operator_confirmed = (
                            RunRecoveryService._is_confirmed_tool_operation(
                                evidence,
                                tool_operation,
                                result,
                            )
                        )
                        if (
                            tool_operation.status is ExternalOperationStatus.STARTED
                            or tool_operation.outcome is None
                            or (
                                result.status
                                in {
                                    ToolResultStatus.DENIED,
                                    ToolResultStatus.INVALID_ARGUMENTS,
                                }
                                and not operator_confirmed
                            )
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
                        if (
                            initial_permission is not None
                            and initial_permission.action == "ask"
                        ):
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
            or not self._is_valid_certified_tool_history(
                evidence,
                tuple(turns),
                history_end=initial_interrupt,
            )
            or not self._is_valid_certified_lifecycle_positions(
                evidence,
                current_kind=ExternalOperationKind.TOOL_CALL,
            )
        ):
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
        *,
        history_end: int,
    ) -> bool:
        descriptor = evidence.run.execution_descriptor
        if descriptor is None:
            return False
        if not (0 < history_end < len(evidence.run_events)):
            return False
        events = tuple(
            event
            for event in evidence.run_events[:history_end]
            if not self._is_recovery_control_event(event)
        )
        recovered_model_operation_ids = frozenset(
            str(event.payload["operation_id"])
            for event in evidence.run_events[:history_end]
            if event.type
            in {"model.recovery.query.started", "model.recovery.resend.started"}
        ) | frozenset(
            request.operation_id
            for request in evidence.reconciliations
            if request.operation_id is not None
            and request.resolution is not None
            and request.resolution.action
            is ReconciliationAction.CONFIRM_COMPLETED
        )
        allowed_types = {
            "run.created",
            "run.started",
            "step.started",
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
            "step.completed",
        }
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
            or any(event.type not in allowed_types for event in events)
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
            model_operation = next(
                (
                    operation
                    for operation in evidence.operations
                    if isinstance(operation, ModelCallOperation)
                    and operation.turn == index
                ),
                None,
            )
            recovered_model = (
                model_operation is not None
                and model_operation.operation_id in recovered_model_operation_ids
            )
            if any(not isinstance(delta, str) for delta in deltas):
                return False
            text_deltas = tuple(
                delta for delta in deltas if isinstance(delta, str)
            )
            if (
                recovered_model
                and not self._is_exact_durable_text_prefix(
                    text_deltas,
                    turn.text,
                )
            ) or (not recovered_model and "".join(text_deltas) != turn.text):
                return False
            usage_events = tuple(
                event
                for event in events[model_started[0][0] + 1 : model_completed[0][0]]
                if event.type == "model.usage.reported"
            )
            expected_usage = turn.usage.model_dump(mode="json")
            if (
                (
                    recovered_model
                    or any(value is not None for value in expected_usage.values())
                )
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
                requested_payload = dict(requested_event.payload)
                resolved_payload = dict(resolved_event.payload)
                request = self._validated_permission_request(
                    evidence,
                    turn.call,
                    requested_payload,
                )
                decision = self._validated_permission_decision(
                    requested_payload.get("request", {}),
                    resolved_payload,
                )
                if (
                    request is None
                    or decision is None
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
            planned_adapter = plan.provider_adapter
            adapter = self._provider_recovery.resolve(linked.provider_identity)
            if not self._is_exact_provider_adapter_registration(
                linked,
                planned=planned_adapter,
                current=adapter,
            ):
                return await self._reconcile_provider_conflict_owned(
                    lease,
                    run,
                    evidence.session,
                    checkpoint,
                    linked.operation_id,
                )
            assert adapter is not None
            provider_request = self._certified_provider_request(
                evidence,
                base_request,
                linked,
            )
            if provider_request is None:
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
            if not self._is_exact_provider_adapter_registration(
                linked,
                planned=planned_adapter,
                current=self._provider_recovery.resolve(linked.provider_identity),
            ):
                return await self._reconcile_provider_conflict_owned(
                    lease,
                    run,
                    evidence.session,
                    checkpoint,
                    linked.operation_id,
                )
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
                if not self._is_exact_provider_adapter_registration(
                    linked,
                    planned=planned_adapter,
                    current=self._provider_recovery.resolve(
                        linked.provider_identity
                    ),
                ):
                    return await self._reconcile_provider_conflict_owned(
                        lease,
                        run,
                        evidence.session,
                        checkpoint,
                        linked.operation_id,
                    )
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

    async def _reconcile_provider_conflict_owned(
        self,
        lease: Lease,
        run: RunSnapshot,
        session: SessionSnapshot,
        checkpoint: RunCheckpoint,
        operation_id: str,
    ) -> RunResult:
        return await self._request_reconciliation_owned(
            lease,
            run,
            session,
            reason="recovery_state_invalid",
            operation_id=operation_id,
            details={"checkpoint_phase": checkpoint.phase.value},
            checkpoint=checkpoint,
        )

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
    await asyncio.sleep(_FOLLOWER_POLL_INTERVAL_SECONDS)


_STRICT_TOOL_JSON_MAX_DEPTH = 64
_STRICT_TOOL_JSON_MAX_NODES = 4096
_STRICT_TOOL_JSON_MAX_BYTES = 16 * 1024
_INVALID_STRICT_JSON = object()


def _strict_tool_result(value: object) -> ToolResult | None:
    if type(value) is not dict:
        return None
    try:
        if set(value) != {
            "call_id",
            "tool_name",
            "status",
            "content",
            "value",
            "error",
        }:
            return None
        call_id = value["call_id"]
        tool_name = value["tool_name"]
        status = value["status"]
        content = value["content"]
        error = value["error"]
        if (
            type(call_id) is not str
            or type(tool_name) is not str
            or type(status) is not str
            or status not in {item.value for item in ToolResultStatus}
            or type(content) is not str
            or (error is not None and type(error) is not str)
        ):
            return None
        if len(content.encode("utf-8")) > 16 * 1024:
            return None
        if error is not None and len(error.encode("utf-8")) > 512:
            return None
        detached_value = _detach_strict_json_value(value["value"])
        if detached_value is _INVALID_STRICT_JSON:
            return None
        raw = {
            "call_id": call_id,
            "tool_name": tool_name,
            "status": status,
            "content": content,
            "value": detached_value,
            "error": error,
        }
        encoded = json.dumps(
            raw,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = ToolResult.model_validate_json(encoded)
        detached_result = result.model_dump(mode="json")
    except Exception:
        return None
    return result if detached_result == raw else None


def _detach_strict_json_value(value: object) -> object:
    holder: list[object] = [None]
    active_container_ids: set[int] = set()
    stack: list[
        tuple[
            Literal["visit", "leave"],
            object,
            list[object] | dict[str, object] | None,
            int | str | None,
            int,
        ]
    ] = [("visit", value, holder, 0, 0)]
    scheduled_nodes = 1
    encoded_bytes = 0

    try:
        while stack:
            action, source, parent, position, depth = stack.pop()
            if action == "leave":
                active_container_ids.remove(id(source))
                continue
            if depth > _STRICT_TOOL_JSON_MAX_DEPTH:
                return _INVALID_STRICT_JSON

            detached: object
            if source is None or type(source) in {bool, int, str}:
                scalar = json.dumps(
                    source,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                encoded_bytes += len(scalar.encode("utf-8"))
                detached = source
            elif type(source) is float:
                if not math.isfinite(source):
                    return _INVALID_STRICT_JSON
                scalar = json.dumps(
                    source,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                encoded_bytes += len(scalar.encode("utf-8"))
                detached = source
            elif type(source) is list:
                if id(source) in active_container_ids:
                    return _INVALID_STRICT_JSON
                child_count = len(source)
                if scheduled_nodes + child_count > _STRICT_TOOL_JSON_MAX_NODES:
                    return _INVALID_STRICT_JSON
                scheduled_nodes += child_count
                encoded_bytes += 2 + max(0, child_count - 1)
                detached_list: list[object] = [None] * child_count
                detached = detached_list
                active_container_ids.add(id(source))
                stack.append(("leave", source, None, None, depth))
                for index in range(child_count - 1, -1, -1):
                    stack.append(
                        ("visit", source[index], detached_list, index, depth + 1)
                    )
            elif type(source) is dict:
                if id(source) in active_container_ids:
                    return _INVALID_STRICT_JSON
                child_count = len(source)
                if scheduled_nodes + child_count > _STRICT_TOOL_JSON_MAX_NODES:
                    return _INVALID_STRICT_JSON
                scheduled_nodes += child_count
                encoded_bytes += 2 + max(0, child_count - 1) + child_count
                source_items = tuple(source.items())
                for key, _ in source_items:
                    if type(key) is not str:
                        return _INVALID_STRICT_JSON
                    encoded_key = json.dumps(
                        key,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    encoded_bytes += len(encoded_key.encode("utf-8"))
                detached_dict: dict[str, object] = {}
                detached = detached_dict
                active_container_ids.add(id(source))
                stack.append(("leave", source, None, None, depth))
                for key, item in reversed(source_items):
                    stack.append(("visit", item, detached_dict, key, depth + 1))
            else:
                return _INVALID_STRICT_JSON

            if encoded_bytes > _STRICT_TOOL_JSON_MAX_BYTES:
                return _INVALID_STRICT_JSON
            if parent is not None:
                if type(parent) is list and type(position) is int:
                    parent[position] = detached
                elif type(parent) is dict and type(position) is str:
                    parent[position] = detached
                else:
                    return _INVALID_STRICT_JSON
    except Exception:
        return _INVALID_STRICT_JSON
    return holder[0]


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
