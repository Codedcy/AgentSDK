from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.models import RunSnapshot


EVIDENCE_ID_MAX_BYTES = 256


def is_public_evidence_id(value: str) -> bool:
    return bool(value) and len(value.encode("utf-8")) <= EVIDENCE_ID_MAX_BYTES


def _evidence_id(value: str) -> str:
    if not is_public_evidence_id(value):
        raise ValueError("evidence id exceeds the public bound")
    return value


EvidenceId = Annotated[str, AfterValidator(_evidence_id)]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def detached_event(event: EventEnvelope) -> EventEnvelope:
    data = event.model_dump(mode="python")
    data["payload"] = _freeze(data["payload"])
    return EventEnvelope.model_construct(**data)


class EventFilter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str | None = None
    run_id: str | None = None
    event_types: tuple[str, ...] = ()


class ObservedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cursor: int = Field(ge=1)
    event: EventEnvelope

    @field_validator("event", mode="after")
    @classmethod
    def _detach_event(cls, value: EventEnvelope) -> EventEnvelope:
        return detached_event(value)


class ObservedRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: RunSnapshot
    as_of_cursor: int = Field(ge=0)


class RunTimeline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    events: tuple[ObservedEvent, ...]
    as_of_cursor: int = Field(ge=0)


class EventQueryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ObservedEvent, ...]
    next_cursor: int = Field(ge=0)
    as_of_cursor: int = Field(ge=0)


class ExecutionTreeNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: RunSnapshot
    parent_run_id: str | None
    created_cursor: int = Field(ge=1)


class ExecutionTree(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root_run_id: str
    nodes: tuple[ExecutionTreeNode, ...]
    as_of_cursor: int = Field(ge=0)


class TraceStageKind(StrEnum):
    RUN = "run"
    STEP = "step"
    CONTEXT = "context"
    MODEL = "model"
    TOOL = "tool"
    PERMISSION = "permission"
    WORKFLOW = "workflow"
    WORKFLOW_NODE = "workflow_node"
    CHILD = "child"
    MESSAGE = "message"
    EVALUATION = "evaluation"
    RECOVERY = "recovery"


class TraceStageStatus(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


class TraceStage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage_id: str = Field(min_length=1, max_length=128)
    kind: TraceStageKind
    status: TraceStageStatus
    entity_id: str = Field(min_length=1, max_length=256)
    run_id: str | None = Field(default=None, max_length=256)
    parent_stage_id: str | None = Field(default=None, max_length=128)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    first_cursor: int = Field(ge=1)
    last_cursor: int = Field(ge=1)
    usage: "TokenUsage | None" = None
    evidence_event_ids: tuple[EvidenceId, ...] = ()
    evidence_cursors: tuple[int, ...] = ()


class TraceTimeline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root_id: str = Field(min_length=1, max_length=256)
    stages: tuple[TraceStage, ...]
    as_of_cursor: int = Field(ge=0)


class AttributionContributor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["model", "tool", "context", "workflow", "child", "evaluation"]
    entity_id: str = Field(min_length=1, max_length=256)
    status: str = Field(min_length=1, max_length=128)
    disposition: Literal["consumed", "unused", "terminal", "supporting"]
    evidence_ids: tuple[EvidenceId, ...] = ()


class FailureAttribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage_id: str = Field(min_length=1, max_length=128)
    stage_kind: TraceStageKind
    code: str = Field(min_length=1, max_length=128)
    retryable: bool
    evidence_ids: tuple[EvidenceId, ...] = ()


ImprovementHintCode = Literal[
    "repeated_tool_failure",
    "unused_tool_output",
    "context_fallback",
    "workflow_loop_limit",
    "child_failure",
    "permission_denied",
    "interrupted_external_work",
]


class ImprovementHint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ImprovementHintCode
    summary: str = Field(min_length=1, max_length=256)
    evidence_ids: tuple[EvidenceId, ...] = ()


class AttributionSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root_run_id: str = Field(min_length=1, max_length=256)
    terminal_status: "RunStatus"
    failure: FailureAttribution | None
    contributors: tuple[AttributionContributor, ...]
    evaluation_ids: tuple[str, ...]
    hints: tuple[ImprovementHint, ...]
    method: Literal["deterministic_event_evidence_v1"] = "deterministic_event_evidence_v1"
    as_of_cursor: int = Field(ge=0)


from agent_sdk.runtime.models import RunStatus, TokenUsage  # noqa: E402

TraceStage.model_rebuild()
AttributionSummary.model_rebuild()
