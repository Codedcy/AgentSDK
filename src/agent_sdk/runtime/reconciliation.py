"""Durable recovery records shared by runtime and storage."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from typing import Any, Literal, ParamSpec, Protocol, Self, TypeAlias, TypeVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.models.litellm_gateway import ModelRequest
from agent_sdk.runtime.model_params import (
    is_credential_key,
    validate_model_params_for_durability,
)
from agent_sdk.runtime.models import (
    RunFailure,
    RunSnapshot,
    RunStatus,
    SessionSnapshot,
    SessionStatus,
    TokenUsage,
)
from agent_sdk.runtime.provider_recovery import (
    ProviderRecoveryDisposition,
    ProviderRecoveryResult,
)
from agent_sdk.tools.models import ToolResult, ToolResultStatus, freeze_json, thaw_json


_TERMINATION_REASON_MAX_UTF8_BYTES = 256
_TERMINATION_ACTOR_MAX_UTF8_BYTES = 1024
_ADDITIONAL_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "authorization",
        "credential",
        "secret",
        "token",
    }
)
_BEARER_SECRET = re.compile(
    r"(?i)(\bauthorization\s*:\s*bearer)\s+[^\s,;]+"
)
_ASSIGNED_METADATA = re.compile(
    r"(?i)(?<![a-z0-9_-])(?P<quote>['\"]?)(?P<key>[a-z][a-z0-9_-]*)"
    r"(?P=quote)\s*[:=]\s*"
    r"(?P<value>(?:bearer\s+)?[^\s,;]+)"
)


def _normalize_metadata_key(key: str) -> str:
    return key.casefold().replace("_", "").replace("-", "")


def _contains_sensitive_assignment(value: str) -> bool:
    for match in _ASSIGNED_METADATA.finditer(value):
        key = match.group("key")
        normalized = _normalize_metadata_key(key)
        if is_credential_key(key):
            return True
        if normalized not in _ADDITIONAL_SENSITIVE_METADATA_KEYS:
            continue
        assignment = match.group("value")
        if normalized == "authorization" and assignment.casefold().startswith(
            "bearer "
        ):
            continue
        return True
    return False


def _contains_sensitive_metadata(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                return True
            normalized = _normalize_metadata_key(key)
            if (
                is_credential_key(key)
                or normalized in _ADDITIONAL_SENSITIVE_METADATA_KEYS
            ):
                return True
            if _contains_sensitive_metadata(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_metadata(item) for item in value)
    elif isinstance(value, str):
        return _contains_sensitive_assignment(value)
    return False


def _redact_metadata_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _BEARER_SECRET.sub(r"\1 [REDACTED]", value)
    if isinstance(value, dict):
        return {key: _redact_metadata_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_metadata_strings(item) for item in value]
    return value


def _sanitize_termination_resolution(
    actor: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return bounded, canonical metadata for an application abort decision."""
    if not isinstance(actor, Mapping) or not actor or not isinstance(evidence, Mapping):
        raise ValueError("termination resolution metadata is invalid")
    if set(evidence) != {"reason"} or type(evidence.get("reason")) is not str:
        raise ValueError("termination resolution reason is invalid")
    reason = " ".join(cast(str, evidence["reason"]).split())
    if not reason:
        raise ValueError("termination resolution reason is empty")
    if _contains_sensitive_assignment(reason):
        raise ValueError("termination resolution reason is unsafe")
    reason = cast(str, _redact_metadata_strings(reason))
    if len(reason.encode("utf-8")) > _TERMINATION_REASON_MAX_UTF8_BYTES:
        raise ValueError("termination resolution reason is too large")
    try:
        validate_model_params_for_durability(actor)
        canonical_actor = thaw_json(freeze_json(actor))
        if not isinstance(canonical_actor, dict) or _contains_sensitive_metadata(
            canonical_actor
        ):
            raise ValueError("termination resolution actor is unsafe")
        canonical_actor = _redact_metadata_strings(canonical_actor)
        assert isinstance(canonical_actor, dict)
        encoded_actor = json.dumps(
            canonical_actor,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except Exception as error:
        raise ValueError("termination resolution actor is invalid") from error
    if len(encoded_actor) > _TERMINATION_ACTOR_MAX_UTF8_BYTES:
        raise ValueError("termination resolution actor is too large")
    return canonical_actor, {"reason": reason}


def _is_sanitized_termination_resolution(
    actor: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> bool:
    try:
        thawed_actor = thaw_json(actor)
        thawed_evidence = thaw_json(evidence)
        if not isinstance(thawed_actor, dict) or not isinstance(
            thawed_evidence, dict
        ):
            return False
        expected_actor, expected_evidence = _sanitize_termination_resolution(
            thawed_actor,
            thawed_evidence,
        )
    except ValueError:
        return False
    return thawed_actor == expected_actor and thawed_evidence == expected_evidence


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


def _validate_prepared_tool_call(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "id",
        "type",
        "function",
    }:
        raise ValueError("prepared assistant Tool call shape is invalid")
    function = value["function"]
    if (
        not isinstance(value["id"], str)
        or not value["id"]
        or value["type"] != "function"
        or not isinstance(function, Mapping)
        or set(function) != {"name", "arguments"}
        or not isinstance(function["name"], str)
        or not function["name"]
        or not isinstance(function["arguments"], str)
    ):
        raise ValueError("prepared assistant Tool call fields are invalid")


def _validate_prepared_message(value: Mapping[str, Any]) -> None:
    role = value.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError("prepared message role is invalid")
    allowed = {
        "system": {"role", "content", "name"},
        "user": {"role", "content", "name"},
        "assistant": {"role", "content", "name", "tool_calls"},
        "tool": {"role", "content", "name", "tool_call_id"},
    }[role]
    if not {"role", "content"} <= set(value) or not set(value) <= allowed:
        raise ValueError("prepared message fields are invalid")
    name = value.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        raise ValueError("prepared message name is invalid")
    content = value["content"]
    if role in {"system", "user"}:
        if not isinstance(content, str):
            raise ValueError("prepared message content is invalid")
        return
    if role == "tool":
        call_id = value.get("tool_call_id")
        if (
            not isinstance(content, str)
            or not isinstance(call_id, str)
            or not call_id
        ):
            raise ValueError("prepared Tool result fields are invalid")
        return
    if content is not None and not isinstance(content, str):
        raise ValueError("prepared assistant content is invalid")
    calls = value.get("tool_calls")
    if calls is None:
        if content is None:
            raise ValueError("prepared assistant content is missing")
        return
    if not isinstance(calls, (list, tuple)) or not calls:
        raise ValueError("prepared assistant Tool calls are invalid")
    for call in calls:
        _validate_prepared_tool_call(call)


def _validate_prepared_tool(value: Mapping[str, Any]) -> None:
    if set(value) != {"type", "function"} or value.get("type") != "function":
        raise ValueError("prepared Tool schema shape is invalid")
    function = value.get("function")
    if not isinstance(function, Mapping):
        raise ValueError("prepared Tool function is invalid")
    allowed = {"name", "description", "parameters"}
    if (
        not {"name", "parameters"} <= set(function)
        or not set(function) <= allowed
    ):
        raise ValueError("prepared Tool function fields are invalid")
    name = function["name"]
    description = function.get("description")
    if (
        not isinstance(name, str)
        or not name
        or (description is not None and not isinstance(description, str))
        or not isinstance(function["parameters"], Mapping)
    ):
        raise ValueError("prepared Tool function values are invalid")


class _ModelRequestPayload(_RecoveryModel):
    model: StrictStr = Field(min_length=1)
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...] = ()
    params: Mapping[str, Any] = Field(default_factory=dict)
    purpose: StrictStr | None = None

    @field_validator("messages", mode="after")
    @classmethod
    def _validate_messages(
        cls,
        value: tuple[Mapping[str, Any], ...],
    ) -> tuple[Mapping[str, Any], ...]:
        if not value:
            raise ValueError("prepared model request messages are empty")
        for message in value:
            _validate_prepared_message(message)
        return value

    @field_validator("tools", mode="after")
    @classmethod
    def _validate_tools(
        cls,
        value: tuple[Mapping[str, Any], ...],
    ) -> tuple[Mapping[str, Any], ...]:
        for tool in value:
            _validate_prepared_tool(tool)
        return value

    @field_validator("messages", "tools", mode="after")
    @classmethod
    def _freeze_sequence(
        cls,
        value: tuple[Mapping[str, Any], ...],
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(_frozen_mapping(item) for item in value)

    @field_validator("params", mode="after")
    @classmethod
    def _freeze_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _frozen_mapping(value)

    @field_serializer("messages", "tools")
    def _serialize_sequence(
        self,
        value: tuple[Mapping[str, Any], ...],
    ) -> list[dict[str, Any]]:
        return [thaw_json(item) for item in value]

    @field_serializer("params")
    def _serialize_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], thaw_json(value))


def serialize_model_request(request: ModelRequest) -> dict[str, Any]:
    try:
        payload = _ModelRequestPayload(
            model=request.model,
            messages=request.messages,
            tools=request.tools,
            params=request.params,
            purpose=request.purpose,
        ).model_dump(mode="json")
        frozen = freeze_json(payload)
        thawed = thaw_json(frozen)
    except Exception as error:
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "model request must be canonical JSON",
            retryable=False,
        ) from error
    assert isinstance(thawed, dict)
    return thawed


def deserialize_model_request(value: Mapping[str, Any]) -> ModelRequest:
    try:
        raw = thaw_json(freeze_json(value))
        if not isinstance(raw, dict):
            raise ValueError("model request payload must be an object")
        messages = raw.get("messages")
        tools = raw.get("tools")
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise ValueError("model request sequences are invalid")
        raw["messages"] = tuple(messages)
        raw["tools"] = tuple(tools)
        payload = _ModelRequestPayload.model_validate(raw)
        data = payload.model_dump(mode="json")
    except Exception as error:
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "stored model request is invalid",
            retryable=False,
        ) from error
    return ModelRequest(
        model=payload.model,
        messages=tuple(dict(message) for message in data["messages"]),
        tools=tuple(dict(tool) for tool in data["tools"]),
        params=dict(data["params"]),
        purpose=payload.purpose,
    )


def model_request_fingerprint(request: ModelRequest) -> str:
    encoded = json.dumps(
        serialize_model_request(request),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


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
    context_view_id: str | None = None
    prompt_manifest_id: str | None = None
    prepared_request: Mapping[str, Any] | None = None

    @field_validator("provider_identity")
    @classmethod
    def _validate_provider_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider identity must be nonempty")
        return value

    @field_validator("context_view_id", "prompt_manifest_id")
    @classmethod
    def _validate_context_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("model context identity must be nonempty")
        return value

    @field_validator("prepared_request", mode="after")
    @classmethod
    def _freeze_prepared_request(
        cls,
        value: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        if value is None:
            return None
        request = deserialize_model_request(value)
        return _frozen_mapping(serialize_model_request(request))

    @field_serializer("prepared_request")
    def _serialize_prepared_request(
        self,
        value: Mapping[str, Any] | None,
    ) -> Any:
        return None if value is None else thaw_json(value)

    @model_validator(mode="after")
    def _validate_prepared_request_identity(self) -> Self:
        populated = (
            self.context_view_id is not None,
            self.prompt_manifest_id is not None,
            self.prepared_request is not None,
        )
        if any(populated) and not all(populated):
            raise ValueError("prepared model request references are incomplete")
        if self.prepared_request is not None:
            request = deserialize_model_request(self.prepared_request)
            if (
                request.model != self.provider_identity
                or model_request_fingerprint(request) != self.request_fingerprint
            ):
                raise ValueError("prepared model request fingerprint mismatch")
        return self


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


def _valid_confirmed_model_resolution_batch(batch: Any) -> bool:
    operation_write = batch.operation
    checkpoint_write = batch.checkpoint
    request_write = batch.reconciliation
    if (
        operation_write is None
        or not isinstance(operation_write.expected, ModelCallOperation)
        or checkpoint_write is None
        or checkpoint_write.expected is None
        or request_write is None
        or request_write.expected is None
        or len(batch.preconditions) != 2
        or len(batch.event_preconditions) != 1
        or batch.checkpoint_precondition is not None
    ):
        return False
    operation = operation_write.expected
    projected_operation = operation_write.updated
    checkpoint = checkpoint_write.expected
    projected_checkpoint = checkpoint_write.updated
    request = request_write.expected
    resolved = request_write.updated
    resolution = resolved.resolution
    if (
        resolution is None
        or resolution.action is not ReconciliationAction.CONFIRM_COMPLETED
        or set(resolution.evidence) != {"provider_result"}
        or request.status is not ReconciliationStatus.PENDING
        or resolved.status is not ReconciliationStatus.RESOLVED
        or request.operation_id != operation.operation_id
        or request.reason != "model_call_unknown_outcome"
        or dict(request.details)
        != {"checkpoint_phase": RunCheckpointPhase.MODEL_IN_FLIGHT.value}
        or operation.status is not ExternalOperationStatus.STARTED
        or checkpoint.phase is not RunCheckpointPhase.MODEL_IN_FLIGHT
        or checkpoint.operation_id != operation.operation_id
        or checkpoint.turn != operation.turn
    ):
        return False
    try:
        result = ProviderRecoveryResult.model_validate_json(
            json.dumps(
                thaw_json(resolution.evidence)["provider_result"],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        preconditions = {
            precondition.kind: precondition for precondition in batch.preconditions
        }
        if set(preconditions) != {"session", "run"}:
            return False
        session_precondition = preconditions["session"]
        run_precondition = preconditions["run"]
        if session_precondition.data is None or run_precondition.data is None:
            return False
        session = SessionSnapshot.model_validate(session_precondition.data)
        run = RunSnapshot.model_validate(run_precondition.data)
    except Exception:
        return False
    if result.disposition not in {
        ProviderRecoveryDisposition.COMPLETED,
        ProviderRecoveryDisposition.FAILED,
    }:
        return False
    if (
        session_precondition.entity_id != session.session_id
        or session_precondition.version != session.version
        or session_precondition.session_id != session.session_id
        or run_precondition.entity_id != run.run_id
        or run_precondition.version != run.version
        or run_precondition.session_id != run.session_id
        or request.run_id != run.run_id
        or request.session_id != run.session_id
        or operation.run_id != run.run_id
        or operation.session_id != run.session_id
        or run.session_id != session.session_id
        or run.status is not RunStatus.WAITING_RECONCILIATION
        or run.run_id not in session.active_run_ids
    ):
        return False
    requested = batch.event_preconditions[0]
    if (
        requested.type != "reconciliation.requested"
        or requested.session_id != run.session_id
        or requested.run_id != run.run_id
    ):
        return False
    expected_resolution_payload = {
        "request_id": request.request_id,
        "operation_id": request.operation_id,
        "action": resolution.action.value,
        "actor": thaw_json(resolution.actor),
        "evidence": thaw_json(resolution.evidence),
    }
    if not batch.events:
        return False
    first = batch.events[0]
    if (
        first.event_id != resolution.event_id
        or first.type != "reconciliation.resolved"
        or first.session_id != run.session_id
        or first.run_id != run.run_id
        or first.sequence != requested.sequence + 1
        or first.occurred_at != batch.now
        or first.payload != expected_resolution_payload
        or resolved
        != request.model_copy(
            update={
                "status": ReconciliationStatus.RESOLVED,
                "resolution": resolution,
            }
        )
    ):
        return False

    expected_types: tuple[str, ...]
    expected_payloads: tuple[dict[str, Any], ...]
    projected_session: SessionSnapshot | None = None
    session_event_type: str | None = None
    if result.disposition is ProviderRecoveryDisposition.COMPLETED:
        assert result.text is not None and result.usage is not None
        calls = () if result.tool_call is None else (result.tool_call,)
        expected_operation = operation.model_copy(
            update={
                "status": ExternalOperationStatus.COMPLETED,
                "outcome": {
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
                },
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

        def add(first_value: int | None, second_value: int | None) -> int | None:
            if first_value is None:
                return second_value
            if second_value is None:
                return first_value
            return first_value + second_value

        cumulative = TokenUsage(
            prompt_tokens=add(
                checkpoint.usage.prompt_tokens, result.usage.prompt_tokens
            ),
            completion_tokens=add(
                checkpoint.usage.completion_tokens,
                result.usage.completion_tokens,
            ),
            total_tokens=add(
                checkpoint.usage.total_tokens, result.usage.total_tokens
            ),
        )
        output_parts = (*checkpoint.output_parts, result.text)
        if calls:
            expected_checkpoint = checkpoint.model_copy(
                update={
                    "checkpoint_version": checkpoint.checkpoint_version + 1,
                    "phase": RunCheckpointPhase.READY_FOR_TOOL,
                    "operation_id": None,
                    "messages": (*checkpoint.messages, assistant),
                    "output_parts": output_parts,
                    "usage": cumulative,
                }
            )
            expected_run = run.model_copy(
                update={
                    "status": RunStatus.INTERRUPTED,
                    "version": run.version + 1,
                }
            )
            expected_types = (
                "reconciliation.resolved",
                "model.usage.reported",
                "model.call.completed",
            )
            expected_payloads = (
                expected_resolution_payload,
                result.usage.model_dump(mode="json"),
                {"finish_reason": result.finish_reason},
            )
        else:
            expected_checkpoint = checkpoint.model_copy(
                update={
                    "checkpoint_version": checkpoint.checkpoint_version + 1,
                    "phase": RunCheckpointPhase.TERMINAL,
                    "operation_id": None,
                    "messages": (*checkpoint.messages, assistant),
                    "output_parts": output_parts,
                    "usage": cumulative,
                }
            )
            output_text = "".join(output_parts)
            expected_run = run.model_copy(
                update={
                    "status": RunStatus.COMPLETED,
                    "version": run.version + 1,
                    "output_text": output_text,
                    "usage": cumulative,
                    "tool_results": checkpoint.tool_results,
                }
            )
            terminal_payload: dict[str, Any] = {
                "output_text": output_text,
                "usage": cumulative.model_dump(mode="json"),
            }
            if checkpoint.tool_results:
                terminal_payload["tool_results"] = [
                    item.model_dump(mode="json") for item in checkpoint.tool_results
                ]
            expected_types = (
                "reconciliation.resolved",
                "model.usage.reported",
                "model.call.completed",
                "step.completed",
                "run.completed",
                "session.closed"
                if session.status is SessionStatus.CLOSING
                and not session.active_workflow_run_ids
                and session.active_run_ids == (run.run_id,)
                else "session.run.detached",
            )
            expected_payloads = (
                expected_resolution_payload,
                result.usage.model_dump(mode="json"),
                {"finish_reason": result.finish_reason},
                {},
                terminal_payload,
                {},
            )
    else:
        assert result.error_code is not None and result.retryable is not None
        expected_operation = operation.model_copy(
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
        expected_checkpoint = checkpoint.model_copy(
            update={
                "checkpoint_version": checkpoint.checkpoint_version + 1,
                "phase": RunCheckpointPhase.TERMINAL,
                "operation_id": None,
            }
        )
        expected_run = run.model_copy(
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
        failure_payload = {
            "error": {
                "code": result.error_code.value,
                "message": "model call failed",
                "retryable": result.retryable,
            }
        }
        expected_types = (
            "reconciliation.resolved",
            "model.call.failed",
            "step.failed",
            "run.failed",
            "session.closed"
            if session.status is SessionStatus.CLOSING
            and not session.active_workflow_run_ids
            and session.active_run_ids == (run.run_id,)
            else "session.run.detached",
        )
        expected_payloads = (
            expected_resolution_payload,
            failure_payload,
            failure_payload,
            failure_payload,
            {},
        )
    terminal = expected_run.status in {RunStatus.COMPLETED, RunStatus.FAILED}
    if terminal:
        remaining = tuple(
            run_id for run_id in session.active_run_ids if run_id != run.run_id
        )
        close_now = (
            session.status is SessionStatus.CLOSING
            and not remaining
            and not session.active_workflow_run_ids
        )
        projected_session = session.model_copy(
            update={
                "active_run_ids": remaining,
                "status": SessionStatus.CLOSED if close_now else session.status,
                "version": session.version + 1,
            }
        )
        session_event_type = "session.closed" if close_now else "session.run.detached"
        session_payload = {
            "run_id": run.run_id,
            "status": projected_session.status.value,
        }
        expected_payloads = (*expected_payloads[:-1], session_payload)
    if (
        projected_operation != expected_operation
        or projected_checkpoint != expected_checkpoint
        or tuple(event.type for event in batch.events) != expected_types
        or tuple(event.payload for event in batch.events) != expected_payloads
        or any(event.occurred_at != batch.now for event in batch.events)
    ):
        return False
    for offset, event in enumerate(batch.events[:-1] if terminal else batch.events):
        if (
            event.session_id != run.session_id
            or event.run_id != run.run_id
            or event.sequence != requested.sequence + 1 + offset
        ):
            return False
    if len(batch.snapshots) != (2 if terminal else 1):
        return False
    run_write = batch.snapshots[0]
    if (
        run_write.kind != "run"
        or run_write.entity_id != run.run_id
        or run_write.session_id != run.session_id
        or run_write.version != expected_run.version
        or run_write.data != expected_run.model_dump(mode="json")
    ):
        return False
    if terminal:
        assert projected_session is not None and session_event_type is not None
        session_write = batch.snapshots[1]
        session_event = batch.events[-1]
        if (
            session_write.kind != "session"
            or session_write.entity_id != session.session_id
            or session_write.session_id != session.session_id
            or session_write.version != projected_session.version
            or session_write.data != projected_session.model_dump(mode="json")
            or session_event.type != session_event_type
            or session_event.session_id != session.session_id
            or session_event.run_id is not None
            or session_event.sequence != projected_session.version
        ):
            return False
    return True


def _valid_confirmed_tool_resolution_batch(batch: Any) -> bool:
    operation_write = batch.operation
    checkpoint_write = batch.checkpoint
    request_write = batch.reconciliation
    if (
        operation_write is None
        or not isinstance(operation_write.expected, ToolCallOperation)
        or checkpoint_write is None
        or checkpoint_write.expected is None
        or request_write is None
        or request_write.expected is None
        or len(batch.events) != 3
        or len(batch.snapshots) != 1
        or len(batch.preconditions) != 2
        or len(batch.event_preconditions) != 1
        or batch.checkpoint_precondition is not None
        or batch.operation_precondition is not None
    ):
        return False
    operation = operation_write.expected
    projected_operation = operation_write.updated
    checkpoint = checkpoint_write.expected
    projected_checkpoint = checkpoint_write.updated
    request = request_write.expected
    resolved = request_write.updated
    resolution = resolved.resolution
    if (
        resolution is None
        or resolution.action is not ReconciliationAction.CONFIRM_COMPLETED
        or set(resolution.evidence) != {"tool_result"}
        or request.status is not ReconciliationStatus.PENDING
        or resolved.status is not ReconciliationStatus.RESOLVED
        or request.operation_id != operation.operation_id
        or request.reason != "tool_call_unknown_outcome"
        or dict(request.details)
        != {"checkpoint_phase": RunCheckpointPhase.TOOL_IN_FLIGHT.value}
        or operation.status is not ExternalOperationStatus.STARTED
        or checkpoint.phase is not RunCheckpointPhase.TOOL_IN_FLIGHT
        or checkpoint.operation_id != operation.operation_id
        or checkpoint.turn != operation.turn
        or len(checkpoint.tool_results) != operation.turn
    ):
        return False
    try:
        raw_result = thaw_json(resolution.evidence)["tool_result"]
        result = ToolResult.model_validate_json(
            json.dumps(
                raw_result,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if result.model_dump(mode="json") != raw_result:
            return False
        preconditions = {
            precondition.kind: precondition for precondition in batch.preconditions
        }
        if set(preconditions) != {"session", "run"}:
            return False
        session_precondition = preconditions["session"]
        run_precondition = preconditions["run"]
        if session_precondition.data is None or run_precondition.data is None:
            return False
        session = SessionSnapshot.model_validate(session_precondition.data)
        run = RunSnapshot.model_validate(run_precondition.data)
        messages = checkpoint.model_dump(mode="json")["messages"]
        assistant = messages[-1]
        calls = assistant["tool_calls"]
        call = calls[0]
        function = call["function"]
    except Exception:
        return False
    if (
        session_precondition.entity_id != session.session_id
        or session_precondition.version != session.version
        or session_precondition.session_id != session.session_id
        or run_precondition.entity_id != run.run_id
        or run_precondition.version != run.version
        or run_precondition.session_id != run.session_id
        or request.run_id != run.run_id
        or request.session_id != run.session_id
        or operation.run_id != run.run_id
        or operation.session_id != run.session_id
        or run.session_id != session.session_id
        or run.status is not RunStatus.WAITING_RECONCILIATION
        or run.run_id not in session.active_run_ids
        or not isinstance(assistant, dict)
        or assistant.get("role") != "assistant"
        or not isinstance(calls, list)
        or len(calls) != 1
        or not isinstance(call, dict)
        or set(call) != {"id", "type", "function"}
        or call["type"] != "function"
        or not isinstance(function, dict)
        or set(function) != {"name", "arguments"}
        or not isinstance(function["arguments"], str)
        or call["id"] != result.call_id
        or function["name"] != result.tool_name
    ):
        return False
    requested = batch.event_preconditions[0]
    resolution_payload = {
        "request_id": request.request_id,
        "operation_id": request.operation_id,
        "action": resolution.action.value,
        "actor": thaw_json(resolution.actor),
        "evidence": thaw_json(resolution.evidence),
    }
    expected_operation = operation.model_copy(
        update={
            "status": (
                ExternalOperationStatus.COMPLETED
                if result.status is ToolResultStatus.SUCCEEDED
                else ExternalOperationStatus.FAILED
            ),
            "outcome": result.model_dump(mode="json"),
        }
    )
    expected_checkpoint = checkpoint.model_copy(
        update={
            "checkpoint_version": checkpoint.checkpoint_version + 1,
            "turn": checkpoint.turn + 1,
            "phase": RunCheckpointPhase.READY_FOR_MODEL,
            "operation_id": None,
            "messages": (
                *checkpoint.messages,
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "name": result.tool_name,
                    "content": result.content,
                },
            ),
            "tool_results": (*checkpoint.tool_results, result),
        }
    )
    expected_run = run.model_copy(
        update={
            "status": RunStatus.INTERRUPTED,
            "version": run.version + 1,
        }
    )
    expected_types = (
        "reconciliation.resolved",
        "tool.call.completed",
        "step.completed",
    )
    expected_payloads: tuple[dict[str, Any], ...] = (
        resolution_payload,
        result.model_dump(mode="json"),
        {},
    )
    run_write = batch.snapshots[0]
    return (
        requested.type == "reconciliation.requested"
        and requested.session_id == run.session_id
        and requested.run_id == run.run_id
        and resolved
        == request.model_copy(
            update={
                "status": ReconciliationStatus.RESOLVED,
                "resolution": resolution,
            }
        )
        and projected_operation == expected_operation
        and projected_checkpoint == expected_checkpoint
        and tuple(event.type for event in batch.events) == expected_types
        and tuple(event.payload for event in batch.events) == expected_payloads
        and all(event.occurred_at == batch.now for event in batch.events)
        and all(
            event.session_id == run.session_id
            and event.run_id == run.run_id
            and event.sequence == requested.sequence + 1 + offset
            for offset, event in enumerate(batch.events)
        )
        and batch.events[0].event_id == resolution.event_id
        and run_write.kind == "run"
        and run_write.entity_id == run.run_id
        and run_write.session_id == run.session_id
        and run_write.version == expected_run.version
        and run_write.data == expected_run.model_dump(mode="json")
    )


def _valid_confirmed_model_terminalization_batch(batch: Any) -> bool:
    checkpoint_write = batch.checkpoint
    request_write = batch.reconciliation
    operation = batch.operation_precondition
    if (
        batch.operation is not None
        or not isinstance(operation, ModelCallOperation)
        or operation.status is not ExternalOperationStatus.COMPLETED
        or operation.outcome is None
        or checkpoint_write is None
        or checkpoint_write.expected is None
        or request_write is None
        or request_write.expected is None
        or len(batch.events) != 3
        or len(batch.snapshots) != 2
        or len(batch.preconditions) != 2
        or len(batch.event_preconditions) != 1
        or batch.checkpoint_precondition is not None
    ):
        return False
    checkpoint = checkpoint_write.expected
    terminal_checkpoint = checkpoint_write.updated
    request = request_write.expected
    resolved = request_write.updated
    resolution = resolved.resolution
    if (
        resolution is None
        or resolution.action is not ReconciliationAction.CONFIRM_COMPLETED
        or set(resolution.evidence) != {"provider_result"}
        or request.status is not ReconciliationStatus.PENDING
        or resolved.status is not ReconciliationStatus.RESOLVED
        or request.operation_id != operation.operation_id
        or request.reason != "model_call_completed_terminalization_unknown"
        or dict(request.details)
        != {
            "checkpoint_phase": RunCheckpointPhase.READY_FOR_MODEL.value,
            "operation_status": ExternalOperationStatus.COMPLETED.value,
        }
        or checkpoint.phase is not RunCheckpointPhase.READY_FOR_MODEL
        or checkpoint.operation_id is not None
        or checkpoint.turn != operation.turn
    ):
        return False
    try:
        result = ProviderRecoveryResult.model_validate_json(
            json.dumps(
                thaw_json(resolution.evidence)["provider_result"],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        preconditions = {
            precondition.kind: precondition for precondition in batch.preconditions
        }
        if set(preconditions) != {"session", "run"}:
            return False
        session_precondition = preconditions["session"]
        run_precondition = preconditions["run"]
        if session_precondition.data is None or run_precondition.data is None:
            return False
        session = SessionSnapshot.model_validate(session_precondition.data)
        run = RunSnapshot.model_validate(run_precondition.data)
    except Exception:
        return False
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
        or run.status is not RunStatus.WAITING_RECONCILIATION
        or run.run_id != operation.run_id
        or run.session_id != operation.session_id
        or run.session_id != session.session_id
        or run.run_id not in session.active_run_ids
        or request.run_id != run.run_id
        or request.session_id != run.session_id
        or session_precondition.entity_id != session.session_id
        or session_precondition.version != session.version
        or session_precondition.session_id != session.session_id
        or run_precondition.entity_id != run.run_id
        or run_precondition.version != run.version
        or run_precondition.session_id != run.session_id
    ):
        return False
    expected_checkpoint = checkpoint.model_copy(
        update={
            "checkpoint_version": checkpoint.checkpoint_version + 1,
            "phase": RunCheckpointPhase.TERMINAL,
        }
    )
    output_text = "".join(checkpoint.output_parts)
    expected_run = run.model_copy(
        update={
            "status": RunStatus.COMPLETED,
            "version": run.version + 1,
            "output_text": output_text,
            "usage": checkpoint.usage,
            "tool_results": checkpoint.tool_results,
        }
    )
    remaining = tuple(
        active for active in session.active_run_ids if active != run.run_id
    )
    close_now = (
        session.status is SessionStatus.CLOSING
        and not remaining
        and not session.active_workflow_run_ids
    )
    expected_session = session.model_copy(
        update={
            "active_run_ids": remaining,
            "status": SessionStatus.CLOSED if close_now else session.status,
            "version": session.version + 1,
        }
    )
    terminal_payload: dict[str, Any] = {
        "output_text": output_text,
        "usage": checkpoint.usage.model_dump(mode="json"),
    }
    if checkpoint.tool_results:
        terminal_payload["tool_results"] = [
            item.model_dump(mode="json") for item in checkpoint.tool_results
        ]
    resolution_payload = {
        "request_id": request.request_id,
        "operation_id": request.operation_id,
        "action": resolution.action.value,
        "actor": thaw_json(resolution.actor),
        "evidence": thaw_json(resolution.evidence),
    }
    requested = batch.event_preconditions[0]
    expected_types = (
        "reconciliation.resolved",
        "run.completed",
        "session.closed" if close_now else "session.run.detached",
    )
    expected_payloads = (
        resolution_payload,
        terminal_payload,
        {"run_id": run.run_id, "status": expected_session.status.value},
    )
    run_write, session_write = batch.snapshots
    first, terminal_event, session_event = batch.events
    return (
        terminal_checkpoint == expected_checkpoint
        and resolved
        == request.model_copy(
            update={
                "status": ReconciliationStatus.RESOLVED,
                "resolution": resolution,
            }
        )
        and requested.type == "reconciliation.requested"
        and requested.session_id == run.session_id
        and requested.run_id == run.run_id
        and tuple(event.type for event in batch.events) == expected_types
        and tuple(event.payload for event in batch.events) == expected_payloads
        and all(event.occurred_at == batch.now for event in batch.events)
        and first.event_id == resolution.event_id
        and first.sequence == requested.sequence + 1
        and terminal_event.sequence == requested.sequence + 2
        and first.session_id == terminal_event.session_id == run.session_id
        and first.run_id == terminal_event.run_id == run.run_id
        and session_event.session_id == session.session_id
        and session_event.run_id is None
        and session_event.sequence == expected_session.version
        and run_write.kind == "run"
        and run_write.entity_id == run.run_id
        and run_write.session_id == run.session_id
        and run_write.version == expected_run.version
        and run_write.data == expected_run.model_dump(mode="json")
        and session_write.kind == "session"
        and session_write.entity_id == session.session_id
        and session_write.session_id == session.session_id
        and session_write.version == expected_session.version
        and session_write.data == expected_session.model_dump(mode="json")
    )


@dataclass(frozen=True)
class _TerminationProjection:
    operation: ExternalOperation
    checkpoint: RunCheckpoint
    run: RunSnapshot
    session: SessionSnapshot
    session_event_type: str
    failure: dict[str, Any]
    event_types: tuple[str, ...]
    event_payloads: tuple[dict[str, Any], ...]


def _termination_projection(
    *,
    session: SessionSnapshot,
    run: RunSnapshot,
    checkpoint: RunCheckpoint,
    operation: ExternalOperation,
    request: ReconciliationRequest,
    resolution: ReconciliationResolution,
) -> _TerminationProjection:
    reason = cast(str, resolution.evidence["reason"])
    failure: dict[str, Any] = {
        "code": "application_resolution_aborted",
        "message": reason,
        "retryable": False,
    }
    projected_operation = operation.model_copy(
        update={
            "status": ExternalOperationStatus.FAILED,
            "outcome": {
                "reconciliation": {
                    "request_id": request.request_id,
                    "action": ReconciliationAction.TERMINATE.value,
                    "outcome_known": False,
                }
            },
        }
    )
    terminal_checkpoint = checkpoint.model_copy(
        update={
            "checkpoint_version": checkpoint.checkpoint_version + 1,
            "phase": RunCheckpointPhase.TERMINAL,
            "operation_id": None,
        }
    )
    failed_run = run.model_copy(
        update={
            "status": RunStatus.FAILED,
            "version": run.version + 1,
            "output_text": "".join(checkpoint.output_parts),
            "usage": checkpoint.usage,
            "tool_results": checkpoint.tool_results,
            "error": RunFailure(**failure),
        }
    )
    remaining = tuple(item for item in session.active_run_ids if item != run.run_id)
    close_now = (
        session.status is SessionStatus.CLOSING
        and not remaining
        and not session.active_workflow_run_ids
    )
    projected_session = session.model_copy(
        update={
            "active_run_ids": remaining,
            "status": SessionStatus.CLOSED if close_now else session.status,
            "version": session.version + 1,
        }
    )
    resolution_payload = {
        "request_id": request.request_id,
        "operation_id": request.operation_id,
        "action": resolution.action.value,
        "actor": thaw_json(resolution.actor),
        "evidence": thaw_json(resolution.evidence),
    }
    failure_payload = {"error": failure}
    return _TerminationProjection(
        operation=projected_operation,
        checkpoint=terminal_checkpoint,
        run=failed_run,
        session=projected_session,
        session_event_type=(
            "session.closed" if close_now else "session.run.detached"
        ),
        failure=failure,
        event_types=(
            "reconciliation.resolved",
            "step.failed",
            "run.failed",
        ),
        event_payloads=(
            resolution_payload,
            failure_payload,
            failure_payload,
        ),
    )


def _valid_terminate_resolution_batch(batch: Any) -> bool:
    operation_write, checkpoint_write, request_write = (
        batch.operation,
        batch.checkpoint,
        batch.reconciliation,
    )
    if any(
        write is None or write.expected is None
        for write in (operation_write, checkpoint_write, request_write)
    ) or (
        len(batch.events),
        len(batch.snapshots),
        len(batch.preconditions),
        len(batch.event_preconditions),
        batch.checkpoint_precondition,
        batch.operation_precondition,
    ) != (4, 2, 2, 1, None, None):
        return False
    assert operation_write is not None and operation_write.expected is not None
    assert checkpoint_write is not None and checkpoint_write.expected is not None
    assert request_write is not None and request_write.expected is not None
    operation, checkpoint, request = (
        operation_write.expected,
        checkpoint_write.expected,
        request_write.expected,
    )
    resolved = request_write.updated
    resolution = resolved.resolution
    phase = (
        RunCheckpointPhase.MODEL_IN_FLIGHT
        if isinstance(operation, ModelCallOperation)
        else RunCheckpointPhase.TOOL_IN_FLIGHT
    )
    reason = (
        "model_call_unknown_outcome"
        if isinstance(operation, ModelCallOperation)
        else "tool_call_unknown_outcome"
    )
    preconditions = {item.kind: item for item in batch.preconditions}
    try:
        if resolution is None:
            return False
        session = SessionSnapshot.model_validate(preconditions["session"].data)
        run = RunSnapshot.model_validate(preconditions["run"].data)
        projection = _termination_projection(
            session=session,
            run=run,
            checkpoint=checkpoint,
            operation=operation,
            request=request,
            resolution=resolution,
        )
    except (KeyError, TypeError, ValueError):
        return False
    requested = batch.event_preconditions[0]
    run_events, session_event = batch.events[:-1], batch.events[-1]
    run_write, session_write = batch.snapshots
    return (
        set(preconditions) == {"session", "run"}
        and resolution.action is ReconciliationAction.TERMINATE
        and _is_sanitized_termination_resolution(
            resolution.actor, resolution.evidence
        )
        and request.status is ReconciliationStatus.PENDING
        and resolved
        == request.model_copy(
            update={"status": ReconciliationStatus.RESOLVED, "resolution": resolution}
        )
        and run.status is RunStatus.WAITING_RECONCILIATION
        and run.run_id in session.active_run_ids
        and operation.status is ExternalOperationStatus.STARTED
        and (operation.run_id, operation.session_id)
        == (run.run_id, run.session_id)
        and request.operation_id == checkpoint.operation_id == operation.operation_id
        and request.reason == reason
        and dict(request.details) == {"checkpoint_phase": phase.value}
        and checkpoint.phase is phase
        and checkpoint.turn == operation.turn
        and operation_write.updated == projection.operation
        and checkpoint_write.updated == projection.checkpoint
        and run_write.data == projection.run.model_dump(mode="json")
        and session_write.data == projection.session.model_dump(mode="json")
        and tuple(event.type for event in run_events) == projection.event_types
        and tuple(event.payload for event in run_events) == projection.event_payloads
        and session_event.type == projection.session_event_type
        and session_event.payload
        == {"run_id": run.run_id, "status": projection.session.status.value}
        and all(event.occurred_at == batch.now for event in batch.events)
        and run_events[0].event_id == resolution.event_id
        and requested.type == "reconciliation.requested"
        and all(
            event.sequence == requested.sequence + offset
            for offset, event in enumerate(run_events, start=1)
        )
        and session_event.sequence == projection.session.version
    )
