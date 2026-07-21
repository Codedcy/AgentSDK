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
