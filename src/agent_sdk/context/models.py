from __future__ import annotations

import json
import math
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from agent_sdk.tools.models import freeze_json, thaw_json

type JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | tuple[JsonValue, ...]
    | Mapping[str, JsonValue]
)

_SOURCE_MESSAGE_MAX_DEPTH = 32
_SOURCE_MESSAGE_MAX_ENTRIES = 20_000
_SOURCE_MESSAGE_MAX_BYTES = 256 * 1024


def _bounded_json(
    value: Any,
    *,
    depth: int,
    entries: list[int],
    active: set[int],
) -> JsonValue:
    if isinstance(value, (Mapping, list, tuple)):
        if depth > _SOURCE_MESSAGE_MAX_DEPTH:
            raise ValueError("message nesting exceeds 32")
        identity = id(value)
        if identity in active:
            raise ValueError("message contains a cycle")
        active.add(identity)
        try:
            entries[0] += len(value)
            if entries[0] > _SOURCE_MESSAGE_MAX_ENTRIES:
                raise ValueError("message exceeds 20000 container entries")
            if isinstance(value, Mapping):
                frozen: dict[str, JsonValue] = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError("JSON object keys must be strings")
                    frozen[key] = _bounded_json(
                        item,
                        depth=depth + 1,
                        entries=entries,
                        active=active,
                    )
                return MappingProxyType(frozen)
            return tuple(
                _bounded_json(
                    item,
                    depth=depth + 1,
                    entries=entries,
                    active=active,
                )
                for item in value
            )
        finally:
            active.remove(identity)
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    raise ValueError("value must be JSON-compatible")


def _valid_tool_calls(value: JsonValue) -> bool:
    if not isinstance(value, tuple) or not value:
        return False
    for call in value:
        if not isinstance(call, Mapping) or set(call) != {
            "id",
            "type",
            "function",
        }:
            return False
        call_id = call["id"]
        function = call["function"]
        if (
            not isinstance(call_id, str)
            or not call_id
            or call["type"] != "function"
            or not isinstance(function, Mapping)
            or set(function) != {"name", "arguments"}
        ):
            return False
        name = function["name"]
        arguments = function["arguments"]
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(arguments, str)
        ):
            return False
    return True


class _DetachedModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

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


class CompactionLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class CompactionPolicy(_DetachedModel):
    l1_reference: StrictFloat = Field(default=0.70, gt=0, lt=1)
    l2_selective: StrictFloat = Field(default=0.80, gt=0, lt=1)
    l3_summary: StrictFloat = Field(default=0.90, gt=0, lt=1)
    l4_rebase: StrictFloat = Field(default=0.96, gt=0, lt=1)
    recovery_target: StrictFloat = Field(default=0.75, gt=0, lt=1)

    @model_validator(mode="after")
    def _validate_threshold_order(self) -> CompactionPolicy:
        if not (
            self.l1_reference
            < self.l2_selective
            < self.l3_summary
            < self.l4_rebase
        ):
            raise ValueError("compaction thresholds must be strictly increasing")
        if self.recovery_target >= self.l2_selective:
            raise ValueError("recovery target must be below L2")
        return self

    def recommend(self, watermark_ratio: float) -> CompactionLevel:
        if (
            isinstance(watermark_ratio, bool)
            or not isinstance(watermark_ratio, (int, float))
            or not math.isfinite(watermark_ratio)
            or watermark_ratio < 0
        ):
            raise ValueError("watermark ratio must be a finite non-negative number")
        if watermark_ratio >= self.l4_rebase:
            return CompactionLevel.L4
        if watermark_ratio >= self.l3_summary:
            return CompactionLevel.L3
        if watermark_ratio >= self.l2_selective:
            return CompactionLevel.L2
        if watermark_ratio >= self.l1_reference:
            return CompactionLevel.L1
        return CompactionLevel.L0


class ContextItem(_DetachedModel):
    event_id: StrictStr = Field(min_length=1)
    cursor: StrictInt = Field(ge=1)
    event_type: StrictStr = Field(min_length=1)
    role: Literal["system", "user", "assistant", "tool"]
    content: StrictStr


class SourceMessage(_DetachedModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        arbitrary_types_allowed=True,
        strict=True,
    )

    ref: StrictStr = Field(min_length=1, max_length=64)
    role: Literal["system", "user", "assistant", "tool"]
    message: Mapping[str, JsonValue]
    event_type: StrictStr = Field(min_length=1, max_length=128)
    protected: StrictBool = False
    current: StrictBool = False

    @field_validator("ref", mode="before")
    @classmethod
    def _validate_ref_bytes(cls, value: Any) -> Any:
        if isinstance(value, str) and len(value.encode("utf-8")) > 64:
            raise ValueError("ref must not exceed 64 UTF-8 bytes")
        return value

    @field_validator("message", mode="before")
    @classmethod
    def _validate_message(cls, value: Any) -> Mapping[str, JsonValue]:
        if not isinstance(value, Mapping):
            raise ValueError("source message must be a JSON object")
        entries = [0]
        frozen = _bounded_json(
            value,
            depth=0,
            entries=entries,
            active=set(),
        )
        assert isinstance(frozen, Mapping)
        encoded = json.dumps(
            thaw_json(frozen),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _SOURCE_MESSAGE_MAX_BYTES:
            raise ValueError("serialized message exceeds 262144 bytes")
        return frozen

    @field_validator("message", mode="after")
    @classmethod
    def _freeze_message(
        cls,
        value: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        return cast(Mapping[str, JsonValue], freeze_json(value))

    @field_serializer("message")
    def _serialize_message(
        self,
        value: Mapping[str, JsonValue],
    ) -> dict[str, Any]:
        thawed = thaw_json(value)
        assert isinstance(thawed, dict)
        return thawed

    @model_validator(mode="after")
    def _validate_provider_message(self) -> SourceMessage:
        message_role = self.message.get("role")
        if message_role != self.role:
            raise ValueError("message role must match source role")
        content = self.message.get("content")
        if self.role in {"system", "user", "tool"}:
            if not isinstance(content, str):
                raise ValueError(f"{self.role} content must be a string")
        else:
            has_tool_calls = "tool_calls" in self.message
            tool_calls = self.message.get("tool_calls")
            if has_tool_calls and not _valid_tool_calls(tool_calls):
                raise ValueError(
                    "tool_calls must be a nonempty sequence of exact "
                    "function-call protocol objects"
                )
            if content is None and not has_tool_calls:
                raise ValueError(
                    "assistant null content requires tool-call protocol data"
                )
            if content is not None and not isinstance(content, str):
                raise ValueError("assistant content must be a string or null")
        return self


class _BudgetInputs(_DetachedModel):
    model_window: StrictInt = Field(gt=0)
    output_reserve: StrictInt = Field(ge=0)
    tool_schema_tokens: StrictInt = Field(ge=0)
    safety_reserve: StrictInt = Field(ge=0)
    projected_source_tokens: StrictInt = Field(ge=0)


class ContextBudget(_DetachedModel):
    model_window: StrictInt = Field(gt=0)
    output_reserve: StrictInt = Field(ge=0)
    tool_schema_tokens: StrictInt = Field(ge=0)
    safety_reserve: StrictInt = Field(ge=0)
    available_input_tokens: StrictInt = Field(ge=0)
    projected_source_tokens: StrictInt = Field(ge=0)
    watermark_ratio: StrictFloat = Field(ge=0)

    @classmethod
    def calculate(
        cls,
        *,
        model_window: int,
        output_reserve: int,
        tool_schema_tokens: int,
        safety_reserve: int,
        projected_source_tokens: int,
    ) -> ContextBudget:
        inputs = _BudgetInputs(
            model_window=model_window,
            output_reserve=output_reserve,
            tool_schema_tokens=tool_schema_tokens,
            safety_reserve=safety_reserve,
            projected_source_tokens=projected_source_tokens,
        )
        available = (
            inputs.model_window
            - inputs.output_reserve
            - inputs.tool_schema_tokens
            - inputs.safety_reserve
        )
        ratio = (
            inputs.projected_source_tokens / available if available > 0 else 0.0
        )
        return cls(
            model_window=inputs.model_window,
            output_reserve=inputs.output_reserve,
            tool_schema_tokens=inputs.tool_schema_tokens,
            safety_reserve=inputs.safety_reserve,
            available_input_tokens=max(available, 0),
            projected_source_tokens=inputs.projected_source_tokens,
            watermark_ratio=float(ratio),
        )

    @model_validator(mode="after")
    def _validate_arithmetic(self) -> ContextBudget:
        available = max(
            self.model_window
            - self.output_reserve
            - self.tool_schema_tokens
            - self.safety_reserve,
            0,
        )
        if self.available_input_tokens != available:
            raise ValueError("available input token arithmetic is inconsistent")
        expected_ratio = (
            self.projected_source_tokens / available if available > 0 else 0.0
        )
        if self.watermark_ratio != expected_ratio:
            raise ValueError("watermark ratio is inconsistent")
        return self


class ContextCapsule(_DetachedModel):
    objective: StrictStr
    constraints: tuple[StrictStr, ...]
    decisions: tuple[StrictStr, ...]
    facts: tuple[StrictStr, ...]
    next_actions: tuple[StrictStr, ...]
    artifact_refs: tuple[StrictStr, ...]
    source_event_ids: tuple[StrictStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_source_ids(self) -> ContextCapsule:
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("capsule source event ids must be unique")
        return self


class ContextView(_DetachedModel):
    view_id: StrictStr = Field(min_length=1)
    session_id: StrictStr = Field(min_length=1)
    message_refs: tuple[StrictStr, ...]
    capsule_id: StrictStr | None = Field(min_length=1)
    estimated_tokens: StrictInt = Field(ge=0)
    recommended_level: CompactionLevel = CompactionLevel.L0
    applied_level: CompactionLevel = CompactionLevel.L0
    budget: ContextBudget | None = None
    source_refs: tuple[StrictStr, ...] = ()
    transformations: tuple[StrictStr, ...] = ()
    fallback_from: CompactionLevel | None = None
    consumed_message_ids: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def _validate_unique_message_refs(self) -> ContextView:
        if len(set(self.message_refs)) != len(self.message_refs):
            raise ValueError("context message references must be unique")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("context source references must be unique")
        if len(set(self.consumed_message_ids)) != len(
            self.consumed_message_ids
        ):
            raise ValueError("consumed message ids must be unique")
        has_capsule = self.capsule_id is not None
        applied_capsule = self.applied_level in {
            CompactionLevel.L3,
            CompactionLevel.L4,
        }
        if has_capsule != applied_capsule:
            raise ValueError(
                "context capsule and applied level must describe the same state"
            )
        if self.fallback_from is not None and (
            self.fallback_from
            not in {CompactionLevel.L3, CompactionLevel.L4}
            or self.applied_level is not CompactionLevel.L2
        ):
            raise ValueError("context fallback must describe an L3/L4 to L2 path")
        return self

    @property
    def id(self) -> str:
        return self.view_id
