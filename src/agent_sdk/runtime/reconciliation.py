"""Durable recovery records shared by runtime and storage."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from typing import Any, Literal, ParamSpec, Protocol, Self, TypeAlias, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.runtime.models import TokenUsage
from agent_sdk.tools.models import ToolResult, freeze_json, thaw_json


class ExternalOperationKind(StrEnum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"


class ExternalOperationStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class RunCheckpointPhase(StrEnum):
    READY_FOR_MODEL = "ready_for_model"
    MODEL_IN_FLIGHT = "model_in_flight"
    READY_FOR_TOOL = "ready_for_tool"
    TOOL_IN_FLIGHT = "tool_in_flight"
    WAITING = "waiting"
    TERMINAL = "terminal"


class ReconciliationStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"


class ReconciliationAction(StrEnum):
    CONFIRM_COMPLETED = "confirm_completed"
    CONFIRM_NOT_EXECUTED = "confirm_not_executed"
    RETRY = "retry"
    TERMINATE = "terminate"


class RecoveryStateConflictError(AgentSDKError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CONFLICT,
            "recovery state conflict",
            retryable=True,
        )


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _context_free_recovery_errors(
    method: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    @wraps(method)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        conflict = False
        try:
            return await method(*args, **kwargs)
        except RecoveryStateConflictError:
            conflict = True
        if conflict:
            del args, kwargs
            raise RecoveryStateConflictError
        raise AssertionError("unreachable")

    return wrapped


class _RecoveryModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        arbitrary_types_allowed=True,
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        del deep
        data = {
            field_name: getattr(self, field_name)
            for field_name in type(self).model_fields
        }
        if update is not None:
            data.update(update)
        return type(self).model_validate(data)


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen = freeze_json(value)
    assert isinstance(frozen, Mapping)
    return frozen


class _ExternalOperationBase(_RecoveryModel):
    operation_id: str
    operation_kind: ExternalOperationKind
    session_id: str
    run_id: str
    turn: int = Field(ge=0)
    request_fingerprint: str
    lease_generation: int = Field(ge=1)
    status: ExternalOperationStatus
    provider_identity: str | None
    tool_identity: str | None
    outcome: Mapping[str, Any] | None = None
    recovery_metadata: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator(
        "operation_id",
        "session_id",
        "run_id",
        "request_fingerprint",
        "provider_identity",
        "tool_identity",
    )
    @classmethod
    def _validate_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("recovery identity must be nonempty")
        return value

    @field_validator("outcome", mode="after")
    @classmethod
    def _freeze_outcome(
        cls, value: Mapping[str, Any] | None
    ) -> Mapping[str, Any] | None:
        return None if value is None else _frozen_mapping(value)

    @field_validator("recovery_metadata", mode="after")
    @classmethod
    def _freeze_recovery_metadata(
        cls, value: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return _frozen_mapping(value)

    @field_serializer("outcome")
    def _serialize_outcome(self, value: Mapping[str, Any] | None) -> Any:
        return None if value is None else thaw_json(value)

    @field_serializer("recovery_metadata")
    def _serialize_recovery_metadata(self, value: Mapping[str, Any]) -> Any:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_outcome_status(self) -> Self:
        if self.status is ExternalOperationStatus.STARTED:
            if self.outcome is not None:
                raise ValueError("started operation cannot have an outcome")
        elif self.outcome is None:
            raise ValueError("terminal operation requires an outcome")
        return self


class ModelCallOperation(_ExternalOperationBase):
    operation_kind: Literal[ExternalOperationKind.MODEL_CALL] = (
        ExternalOperationKind.MODEL_CALL
    )
    provider_identity: str
    tool_identity: None = None

    @field_validator("provider_identity")
    @classmethod
    def _validate_provider_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider identity must be nonempty")
        return value


class ToolCallOperation(_ExternalOperationBase):
    operation_kind: Literal[ExternalOperationKind.TOOL_CALL] = (
        ExternalOperationKind.TOOL_CALL
    )
    provider_identity: None = None
    tool_identity: str

    @field_validator("tool_identity")
    @classmethod
    def _validate_tool_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool identity must be nonempty")
        return value


ExternalOperation: TypeAlias = ModelCallOperation | ToolCallOperation


class RunCheckpoint(_RecoveryModel):
    run_id: str
    session_id: str
    checkpoint_version: int = Field(ge=1)
    turn: int = Field(ge=0)
    phase: RunCheckpointPhase
    operation_id: str | None = None
    messages: tuple[Mapping[str, Any], ...] = Field(min_length=1)
    output_parts: tuple[str, ...] = ()
    usage: TokenUsage = Field(default_factory=TokenUsage)
    tool_results: tuple[ToolResult, ...] = ()

    @field_validator("run_id", "session_id", "operation_id")
    @classmethod
    def _validate_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("checkpoint identity must be nonempty")
        return value

    @field_validator("messages", mode="after")
    @classmethod
    def _freeze_messages(
        cls, value: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(_frozen_mapping(message) for message in value)

    @field_serializer("messages")
    def _serialize_messages(
        self, value: tuple[Mapping[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        return tuple(thaw_json(message) for message in value)

    @field_validator("usage", mode="after")
    @classmethod
    def _detach_usage(cls, value: TokenUsage) -> TokenUsage:
        return TokenUsage.model_validate_json(value.model_dump_json())

    @field_validator("tool_results", mode="after")
    @classmethod
    def _detach_tool_results(
        cls, value: tuple[ToolResult, ...]
    ) -> tuple[ToolResult, ...]:
        return tuple(
            ToolResult.model_validate_json(result.model_dump_json()) for result in value
        )

    @model_validator(mode="after")
    def _validate_phase_operation(self) -> Self:
        in_flight = self.phase in {
            RunCheckpointPhase.MODEL_IN_FLIGHT,
            RunCheckpointPhase.TOOL_IN_FLIGHT,
        }
        if in_flight != (self.operation_id is not None):
            raise ValueError("checkpoint operation does not match phase")
        return self


class ReconciliationResolution(_RecoveryModel):
    action: ReconciliationAction
    actor: Mapping[str, Any]
    evidence: Mapping[str, Any]
    decided_at: datetime
    event_id: str

    @field_validator("event_id")
    @classmethod
    def _validate_event_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("resolution event identity must be nonempty")
        return value

    @field_validator("actor", "evidence", mode="after")
    @classmethod
    def _freeze_nonempty_mapping(
        cls, value: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if not value:
            raise ValueError("resolution metadata must be nonempty")
        return _frozen_mapping(value)

    @field_serializer("actor", "evidence")
    def _serialize_mapping(self, value: Mapping[str, Any]) -> Any:
        return thaw_json(value)

    @field_validator("decided_at")
    @classmethod
    def _normalize_decided_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("resolution timestamp must be timezone-aware")
        return value.astimezone(UTC)


class ReconciliationRequest(_RecoveryModel):
    request_id: str
    session_id: str
    run_id: str
    operation_id: str | None = None
    status: ReconciliationStatus = ReconciliationStatus.PENDING
    reason: str
    details: Mapping[str, Any] = Field(default_factory=dict)
    resolution: ReconciliationResolution | None = None

    @field_validator("request_id", "session_id", "run_id", "operation_id", "reason")
    @classmethod
    def _validate_identity_or_reason(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("reconciliation identity and reason must be nonempty")
        return value

    @field_validator("details", mode="after")
    @classmethod
    def _freeze_details(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _frozen_mapping(value)

    @field_serializer("details")
    def _serialize_details(self, value: Mapping[str, Any]) -> Any:
        return thaw_json(value)

    @field_validator("resolution", mode="after")
    @classmethod
    def _detach_resolution(
        cls, value: ReconciliationResolution | None
    ) -> ReconciliationResolution | None:
        if value is None:
            return None
        return ReconciliationResolution.model_validate_json(value.model_dump_json())

    @model_validator(mode="after")
    def _validate_resolution_status(self) -> Self:
        if self.status is ReconciliationStatus.PENDING:
            if self.resolution is not None:
                raise ValueError("pending reconciliation cannot have a resolution")
        elif self.resolution is None:
            raise ValueError("resolved reconciliation requires a resolution")
        return self


class _ReconciliationResolver(Protocol):
    async def resolve(
        self,
        request_id: str,
        action: ReconciliationAction,
        *,
        actor: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> ReconciliationRequest: ...


class ReconciliationService:
    """Apply explicit operator decisions to durable reconciliation requests."""

    def __init__(self, resolver: _ReconciliationResolver) -> None:
        self._resolver = resolver

    async def resolve(
        self,
        request_id: str,
        action: ReconciliationAction,
        *,
        actor: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> ReconciliationRequest:
        try:
            return await self._resolver.resolve(
                request_id,
                action,
                actor=actor,
                evidence=evidence,
            )
        finally:
            del request_id, action, actor, evidence


def _canonical_record_json(record: _RecoveryModel) -> str:
    return json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _external_operation_from_json(value: str) -> ExternalOperation:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("external operation must be an object")
    kind = decoded.get("operation_kind")
    if kind == ExternalOperationKind.MODEL_CALL.value:
        return ModelCallOperation.model_validate_json(value)
    if kind == ExternalOperationKind.TOOL_CALL.value:
        return ToolCallOperation.model_validate_json(value)
    raise ValueError("external operation kind is invalid")


def _checkpoint_from_json(value: str) -> RunCheckpoint:
    return RunCheckpoint.model_validate_json(value)


def _reconciliation_request_from_json(value: str) -> ReconciliationRequest:
    return ReconciliationRequest.model_validate_json(value)


def _valid_checkpoint_replay_shape(
    checkpoint: RunCheckpoint, expected: RunCheckpoint | None
) -> bool:
    if expected is None:
        return checkpoint.checkpoint_version == 1
    return (
        checkpoint != expected
        and checkpoint.run_id == expected.run_id
        and checkpoint.session_id == expected.session_id
        and checkpoint.checkpoint_version == expected.checkpoint_version + 1
    )
