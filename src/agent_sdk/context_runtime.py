from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, model_validator


class _ContextRuntimeModel(BaseModel):
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


class CompactionPolicy(_ContextRuntimeModel):
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


class ContextRuntimeConfig(_ContextRuntimeModel):
    model_window: int = Field(default=128_000, gt=0)
    output_reserve: int = Field(default=4_096, ge=0)
    safety_reserve: int = Field(default=1_024, ge=0)
    policy: CompactionPolicy = Field(default_factory=CompactionPolicy)
    force_level: CompactionLevel | None = None
    allow_lossy: bool = True
    recent_messages: int = Field(default=12, ge=2)
    tool_preview_bytes: int = Field(default=4_096, ge=256)


__all__ = ["CompactionLevel", "CompactionPolicy", "ContextRuntimeConfig"]
