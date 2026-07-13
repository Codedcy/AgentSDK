from __future__ import annotations

import json
import math
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            frozen[key] = freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("value must be JSON-compatible")


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def json_text(value: Any, *, max_bytes: int) -> tuple[Any, str]:
    frozen = freeze_json(value)
    text = json.dumps(
        thaw_json(frozen),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("JSON value exceeds size limit")
    return frozen, text


def bounded_text(value: str, *, max_bytes: int) -> str:
    sanitized = value.encode("utf-8", errors="replace").decode("utf-8")
    encoded = sanitized.encode("utf-8")
    if len(encoded) <= max_bytes:
        return sanitized
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: str = Field(min_length=1)
    description: str
    input_schema: Mapping[str, Any]
    version: str = "1"
    source: str = "application"
    effects: tuple[str, ...] = ()
    timeout_seconds: float | None = Field(default=None, gt=0)

    @field_validator("input_schema", mode="after")
    @classmethod
    def _freeze_schema(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen = freeze_json(value)
        assert isinstance(frozen, Mapping)
        return frozen

    @field_serializer("input_schema")
    def _serialize_schema(self, value: Mapping[str, Any]) -> dict[str, Any]:
        thawed = thaw_json(value)
        assert isinstance(thawed, dict)
        return thawed

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        del deep
        data = self.model_dump(mode="json")
        if update is not None:
            data.update(update)
        return type(self).model_validate(data)


class ToolContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    session_id: str


class ToolResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INVALID_ARGUMENTS = "invalid_arguments"


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    call_id: str
    tool_name: str
    status: ToolResultStatus
    content: str
    value: Any = None
    error: str | None = None

    @field_validator("content", mode="after")
    @classmethod
    def _bound_content(cls, value: str) -> str:
        return bounded_text(value, max_bytes=16 * 1024)

    @field_validator("error", mode="after")
    @classmethod
    def _bound_error(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return bounded_text(value, max_bytes=512)

    @field_validator("value", mode="after")
    @classmethod
    def _freeze_value(cls, value: Any) -> Any:
        return freeze_json(value)

    @field_serializer("value")
    def _serialize_value(self, value: Any) -> Any:
        return thaw_json(value)

    @classmethod
    def succeeded(cls, call_id: str, tool_name: str, value: Any) -> ToolResult:
        frozen, content = json_text(value, max_bytes=16 * 1024)
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolResultStatus.SUCCEEDED,
            content=content,
            value=frozen,
        )

    @classmethod
    def normalized_error(
        cls,
        call_id: str,
        tool_name: str,
        status: ToolResultStatus,
        message: str,
    ) -> ToolResult:
        bounded = bounded_text(message, max_bytes=512)
        _, content = json_text(
            {"status": status.value, "error": bounded},
            max_bytes=4 * 1024,
        )
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=status,
            content=content,
            error=bounded,
        )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        del deep
        data = self.model_dump(mode="json")
        if update is not None:
            data.update(update)
        return type(self).model_validate(data)
