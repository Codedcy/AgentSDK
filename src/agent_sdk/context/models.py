from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from agent_sdk.tools.models import freeze_json, thaw_json


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
    )

    ref: StrictStr = Field(min_length=1)
    message: Mapping[str, Any]
    protected: bool = False
    current: bool = False

    @field_validator("message", mode="after")
    @classmethod
    def _freeze_message(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen = freeze_json(value)
        assert isinstance(frozen, Mapping)
        return frozen

    @field_serializer("message")
    def _serialize_message(self, value: Mapping[str, Any]) -> dict[str, Any]:
        thawed = thaw_json(value)
        assert isinstance(thawed, dict)
        return thawed


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

    @model_validator(mode="after")
    def _validate_unique_message_refs(self) -> ContextView:
        if len(set(self.message_refs)) != len(self.message_refs):
            raise ValueError("context message references must be unique")
        has_capsule = self.capsule_id is not None
        applied_capsule = self.applied_level in {
            CompactionLevel.L3,
            CompactionLevel.L4,
        }
        if has_capsule != applied_capsule:
            raise ValueError(
                "context capsule and applied level must describe the same state"
            )
        return self

    @property
    def id(self) -> str:
        return self.view_id
