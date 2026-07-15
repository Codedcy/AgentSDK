"""Application-certified provider recovery contracts and registry."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from enum import StrEnum
from typing import Any, Self, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.models.litellm_gateway import (
    ModelRequest,
    ToolCallCompleted,
)
from agent_sdk.runtime.models import TokenUsage

_MAX_IDENTITY_BYTES = 256
_MAX_FINISH_REASON_BYTES = 128
_MAX_TEXT_BYTES = 64 * 1024
_MAX_TOOL_ARGUMENTS_BYTES = 64 * 1024


class _ProviderRecoveryModel(BaseModel):
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
        values = {
            field_name: getattr(self, field_name)
            for field_name in type(self).model_fields
        }
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


def _bounded_identity(value: str) -> str:
    if not value.strip():
        raise ValueError("provider recovery identity must be nonempty")
    if len(value.encode("utf-8")) > _MAX_IDENTITY_BYTES:
        raise ValueError("provider recovery identity must be bounded")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


class ProviderRecoveryDisposition(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_EXECUTED = "not_executed"
    PENDING = "pending"
    UNKNOWN = "unknown"


class ProviderRecoveryRequest(_ProviderRecoveryModel):
    session_id: str
    run_id: str
    turn: int = Field(ge=0)
    operation_id: str
    provider_identity: str
    request_fingerprint: str
    model_request: ModelRequest

    @field_validator(
        "session_id",
        "run_id",
        "operation_id",
        "provider_identity",
    )
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _bounded_identity(value)

    @field_validator("request_fingerprint")
    @classmethod
    def _validate_fingerprint(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("provider recovery fingerprint is invalid")
        return value

    @field_validator("model_request", mode="before")
    @classmethod
    def _detach_model_request(cls, value: object) -> ModelRequest:
        if type(value) is not ModelRequest:
            raise ValueError("provider recovery model request is invalid")
        request = value
        if (
            type(request.model) is not str
            or not request.model.strip()
            or type(request.messages) is not tuple
            or not request.messages
            or any(type(message) is not dict for message in request.messages)
            or type(request.tools) is not tuple
            or any(type(tool) is not dict for tool in request.tools)
            or type(request.params) is not dict
            or (request.purpose is not None and type(request.purpose) is not str)
        ):
            raise ValueError("provider recovery model request is invalid")
        try:
            detached = ModelRequest(
                model=request.model,
                messages=deepcopy(request.messages),
                tools=deepcopy(request.tools),
                params=deepcopy(request.params),
                purpose=request.purpose,
            )
        except Exception:
            raise ValueError("provider recovery model request is invalid") from None
        return detached

    @model_validator(mode="after")
    def _validate_provider_match(self) -> Self:
        if self.provider_identity != self.model_request.model:
            raise ValueError("provider recovery request identity mismatch")
        return self

    @property
    def request(self) -> ModelRequest:
        """Compatibility view of the detached reconstructed request."""
        return self.model_request


ProviderRecoveryCallable: TypeAlias = Callable[
    [ProviderRecoveryRequest], Awaitable["ProviderRecoveryResult"]
]


class ProviderRecoveryAdapter(_ProviderRecoveryModel):
    provider_identity: str
    adapter_id: str
    version: str
    authoritative_status: bool
    same_operation_id_resend: bool
    query_status: ProviderRecoveryCallable | None = None
    resend: ProviderRecoveryCallable | None = None

    @field_validator("provider_identity", "adapter_id", "version")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _bounded_identity(value)

    @model_validator(mode="after")
    def _validate_certification(self) -> Self:
        if self.authoritative_status != (self.query_status is not None):
            raise ValueError("authoritative status callable certification mismatch")
        if self.same_operation_id_resend != (self.resend is not None):
            raise ValueError("same operation id resend callable certification mismatch")
        return self


class ProviderRecoveryResult(_ProviderRecoveryModel):
    disposition: ProviderRecoveryDisposition
    finish_reason: str | None = None
    text: str | None = None
    tool_call: ToolCallCompleted | None = None
    usage: TokenUsage | None = None
    error_code: ErrorCode | None = None
    retryable: bool | None = None

    @field_validator("finish_reason")
    @classmethod
    def _validate_finish_reason(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > _MAX_FINISH_REASON_BYTES:
            raise ValueError("provider recovery finish reason must be bounded")
        return value

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise ValueError("provider recovery text must be bounded")
        return value

    @field_validator("tool_call", mode="after")
    @classmethod
    def _detach_tool_call(
        cls, value: ToolCallCompleted | None
    ) -> ToolCallCompleted | None:
        if value is None:
            return None
        if (
            type(value) is not ToolCallCompleted
            or type(value.index) is not int
            or value.index != 0
            or any(
                type(field) is not str or not field
                for field in (value.call_id, value.name, value.arguments_json)
            )
            or any(
                len(field.encode("utf-8")) > _MAX_IDENTITY_BYTES
                for field in (value.call_id, value.name)
            )
            or len(value.arguments_json.encode("utf-8")) > _MAX_TOOL_ARGUMENTS_BYTES
        ):
            raise ValueError("provider recovery Tool call is invalid")
        try:
            arguments = json.loads(
                value.arguments_json,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, ValueError):
            raise ValueError("provider recovery Tool call is invalid") from None
        if type(arguments) is not dict:
            raise ValueError("provider recovery Tool call is invalid")
        return ToolCallCompleted(
            index=value.index,
            call_id=value.call_id,
            name=value.name,
            arguments_json=value.arguments_json,
        )

    @field_validator("usage", mode="after")
    @classmethod
    def _detach_usage(cls, value: TokenUsage | None) -> TokenUsage | None:
        if value is None:
            return None
        for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            token_count = getattr(value, field_name)
            if token_count is not None and (
                type(token_count) is not int or token_count < 0
            ):
                raise ValueError("provider recovery usage is invalid")
        return TokenUsage.model_validate_json(value.model_dump_json())

    @model_validator(mode="after")
    def _validate_disposition_fields(self) -> Self:
        completed = self.disposition is ProviderRecoveryDisposition.COMPLETED
        failed = self.disposition is ProviderRecoveryDisposition.FAILED
        if completed:
            if (
                self.text is None
                or self.usage is None
                or self.error_code is not None
                or self.retryable is not None
            ):
                raise ValueError("completed provider recovery result is invalid")
        elif failed:
            if (
                self.error_code is None
                or self.retryable is None
                or self.finish_reason is not None
                or self.text is not None
                or self.tool_call is not None
                or self.usage is not None
            ):
                raise ValueError("failed provider recovery result is invalid")
        elif any(
            value is not None
            for value in (
                self.finish_reason,
                self.text,
                self.tool_call,
                self.usage,
                self.error_code,
                self.retryable,
            )
        ):
            raise ValueError("nonterminal provider recovery result has outcome")
        return self


class ProviderRecoveryRegistry:
    def __init__(self) -> None:
        self._registered: dict[str, ProviderRecoveryAdapter] = {}

    def register(self, adapter: ProviderRecoveryAdapter) -> ProviderRecoveryAdapter:
        if type(adapter) is not ProviderRecoveryAdapter:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "provider recovery adapter is invalid",
                retryable=False,
            )
        if adapter.provider_identity in self._registered:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "provider recovery adapter already registered",
                retryable=False,
            )
        detached = ProviderRecoveryAdapter(
            provider_identity=adapter.provider_identity,
            adapter_id=adapter.adapter_id,
            version=adapter.version,
            authoritative_status=adapter.authoritative_status,
            same_operation_id_resend=adapter.same_operation_id_resend,
            query_status=adapter.query_status,
            resend=adapter.resend,
        )
        self._registered[detached.provider_identity] = detached
        return detached

    def unregister(
        self,
        provider_identity: str,
        *,
        expected: ProviderRecoveryAdapter | None = None,
    ) -> bool:
        registered = self._registered.get(provider_identity)
        if registered is None or (
            expected is not None and registered is not expected
        ):
            return False
        del self._registered[provider_identity]
        return True

    def get(self, provider_identity: str) -> ProviderRecoveryAdapter:
        try:
            return self._registered[provider_identity]
        except KeyError:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "provider recovery adapter not found",
                retryable=False,
            ) from None

    def resolve(self, provider_identity: str) -> ProviderRecoveryAdapter | None:
        return self._registered.get(provider_identity)

    def list(self) -> tuple[ProviderRecoveryAdapter, ...]:
        return tuple(
            self._registered[provider_identity]
            for provider_identity in sorted(self._registered)
        )


__all__ = [
    "ProviderRecoveryAdapter",
    "ProviderRecoveryDisposition",
    "ProviderRecoveryRequest",
    "ProviderRecoveryResult",
]
