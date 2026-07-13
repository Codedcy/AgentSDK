from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from agent_sdk.observability import RunTimeline
from agent_sdk.runtime.models import RunSnapshot


class EvaluationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


def validate_metadata_string(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 128
    ):
        raise ValueError("invalid evaluator metadata")
    return value


def _metrics(value: Any) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("evaluation metrics must be a mapping")
    result: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError("evaluation metric names must be nonempty strings")
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("evaluation metric values must be numbers")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError("evaluation metric values must be finite")
        result[key] = number
    return MappingProxyType(result)


class EvaluationDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EvaluationVerdict
    metrics: Mapping[str, float] = Field(default_factory=dict)
    reason: str
    confidence: float = Field(ge=0, le=1)
    evidence_event_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("metrics", mode="after")
    @classmethod
    def _validate_metrics(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        return _metrics(value)

    @field_validator("evidence_event_ids", mode="after")
    @classmethod
    def _unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(set(value)) != len(value):
            raise ValueError("evaluation evidence ids must be unique and nonempty")
        return value

    @field_serializer("metrics")
    def _serialize_metrics(self, value: Mapping[str, float]) -> dict[str, float]:
        return dict(value)


class EvaluationSubject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: RunSnapshot
    timeline: RunTimeline
    as_of_cursor: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_identity(self) -> Self:
        if (
            self.timeline.run_id != self.snapshot.run_id
            or self.timeline.as_of_cursor != self.as_of_cursor
        ):
            raise ValueError("evaluation subject identity is inconsistent")
        return self


class EvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: str
    session_id: str
    subject_run_id: str
    subject_type: Literal["run"] = "run"
    evaluator_id: str
    evaluator_version: str
    method: str
    verdict: EvaluationVerdict
    metrics: Mapping[str, float] = Field(default_factory=dict)
    reason: str
    confidence: float = Field(ge=0, le=1)
    evidence_event_ids: tuple[str, ...] = Field(min_length=1)
    created_at: datetime
    subject_cursor: int = Field(ge=0)
    schema_version: Literal[1] = 1
    record_version: Literal[1] = 1

    @field_validator("evaluator_id", "evaluator_version", "method", mode="before")
    @classmethod
    def _validate_metadata(cls, value: object) -> str:
        return validate_metadata_string(value)

    @field_validator("metrics", mode="after")
    @classmethod
    def _validate_metrics(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        return _metrics(value)

    @field_validator("evidence_event_ids", mode="after")
    @classmethod
    def _unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(set(value)) != len(value):
            raise ValueError("evaluation evidence ids must be unique and nonempty")
        return value

    @field_serializer("metrics")
    def _serialize_metrics(self, value: Mapping[str, float]) -> dict[str, float]:
        return dict(value)
