from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.models import RunSnapshot


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
