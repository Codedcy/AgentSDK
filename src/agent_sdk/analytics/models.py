from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class AnalyticsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    value: float | None
    sample_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    method: str
    filters: Mapping[str, str]
    evidence_event_ids: tuple[str, ...]
    as_of_cursor: int = Field(ge=0)

    @field_validator("filters", mode="after")
    @classmethod
    def _freeze_filters(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(value))

    @field_serializer("filters")
    def _serialize_filters(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)
