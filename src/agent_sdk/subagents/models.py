from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class TaskEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    objective: str
    success_criteria: tuple[str, ...] = ()
    instructions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    workspace_scopes: tuple[str, ...] = ()


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
