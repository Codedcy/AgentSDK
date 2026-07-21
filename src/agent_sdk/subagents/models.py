from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from agent_sdk.runtime.failures import RunFailure


class TaskEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    objective: str
    success_criteria: tuple[str, ...] = ()
    instructions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] | None = None
    workspace_scopes: tuple[str, ...] | None = None


class ChildUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ChildResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: Literal["completed", "failed"]
    output_text: str
    evidence_refs: tuple[str, ...]
    usage: ChildUsage


class ChildLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_depth: int = Field(default=3, ge=0)
    max_children_per_parent: int = Field(default=8, ge=0)
    max_children_per_session: int = Field(default=32, ge=0)
    max_concurrent_children: int = Field(default=4, ge=1)
    max_wait_seconds: float = Field(default=30.0, gt=0, le=300.0)


class ChildProgress(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    parent_run_id: str = Field(min_length=1)
    status: Literal[
        "queued",
        "running",
        "waiting",
        "interrupted",
        "completed",
        "failed",
    ]
    objective: str
    depth: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _normalize_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("child progress timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_timestamps(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("child progress timestamps are invalid")
        return self


class ChildWaitResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    child_run_id: str = Field(min_length=1)
    status: Literal["pending", "completed", "failed", "interrupted"]
    result: ChildResult | None = None
    error: RunFailure | None = None

    @model_validator(mode="after")
    def _validate_terminal_value(self) -> Self:
        if self.status == "completed" and self.result is None:
            raise ValueError("completed child wait result requires a result")
        if self.status != "completed" and self.result is not None:
            raise ValueError("non-completed child wait result cannot contain a result")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed child wait result requires an error")
        if self.status != "failed" and self.error is not None:
            raise ValueError("non-failed child wait result cannot contain an error")
        return self


class AgentMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1)
    sender_run_id: str = Field(min_length=1)
    recipient_run_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=32_768)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("message timestamp must be timezone-aware")
        return value.astimezone(UTC)


class MailboxSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recipient_run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    messages: tuple[AgentMessage, ...] = ()

    @model_validator(mode="after")
    def _validate_messages(self) -> Self:
        message_ids: set[str] = set()
        for sequence, message in enumerate(self.messages, start=1):
            if (
                message.session_id != self.session_id
                or message.recipient_run_id != self.recipient_run_id
                or message.sequence != sequence
                or message.message_id in message_ids
            ):
                raise ValueError("mailbox message identity is invalid")
            message_ids.add(message.message_id)
        return self


class MailboxCursorSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recipient_run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    last_consumed_sequence: int = Field(default=0, ge=0)
    version: int = Field(default=1, ge=1)
