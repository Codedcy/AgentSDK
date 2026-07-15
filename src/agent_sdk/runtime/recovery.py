from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.ids import new_id
from agent_sdk.models.litellm_gateway import ModelRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.engine import RunEngine, _model_request_fingerprint
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
from agent_sdk.tools.registry import ToolRegistry


_SCANNER_LEASE_TTL = timedelta(seconds=30)


@dataclass(frozen=True)
class RecoveryPlan:
    kind: Literal[
        "detached",
        "execute",
        "resume",
        "reconcile",
        "provider_recovery",
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
        request = await self._validated_request(evidence)
        checkpoint = evidence.checkpoint
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
        follow = False
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
                return await self._coordinate_reconciliation(
                    plan.run_id,
                    reason=plan.reason,
                    operation_id=plan.operation_id,
                    details=dict(plan.details),
                )
            if plan.kind == "provider_recovery":
                assert plan.request is not None
                assert plan.checkpoint is not None
                assert plan.operation_id is not None
                return await self._coordinate_provider_recovery(plan)
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
            return await self._follow_durable_run(plan.run_id)
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
            session_id=run.session_id,
            up_to_cursor=up_to_cursor,
        )
        run_events = tuple(
            stored.event for stored in events if stored.event.run_id == run.run_id
        )
        return _RecoveryEvidence(
            run=run,
            session=session,
            checkpoint=checkpoint,
            operations=operations,
            pending=pending,
            run_events=run_events,
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
            checkpoint is None
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
            sequence = await self._store.latest_run_event_sequence(run_id) or 0
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
            del callback, request, adapter, value, task
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
            del callback, request, adapter, value, task
            return None, error_category
        if type(value) is not ProviderRecoveryResult:
            del callback, request, adapter, value, task
            return None, "invalid_result"
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
        del callback, request, adapter, result, value, task
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
        sequence = await self._store.latest_run_event_sequence(run.run_id) or 0
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
