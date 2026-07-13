from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_sdk.runtime.models import TokenUsage


class AgentNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    kind: Literal["agent"] = "agent"
    agent_revision: str = Field(min_length=1, max_length=256)
    input: str = Field(min_length=1, max_length=32_768)
    run_as: Literal["parent", "child"] = "parent"
    success_criteria: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    workspace_scopes: tuple[str, ...] = ()


class WorkflowEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: Literal["agent-sdk/v1"]
    kind: Literal["Workflow"]
    name: str = Field(min_length=1, max_length=256)
    nodes: tuple[AgentNode, ...]
    edges: tuple[WorkflowEdge, ...] = ()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


class WorkflowIR(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    name: str
    nodes: tuple[AgentNode, ...]
    edges: tuple[WorkflowEdge, ...]
    definition_hash: str

    def _content(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "nodes": [node.model_dump(mode="json") for node in self.nodes],
            "edges": [edge.model_dump(mode="json") for edge in self.edges],
        }

    @classmethod
    def create(
        cls,
        *,
        name: str,
        nodes: tuple[AgentNode, ...],
        edges: tuple[WorkflowEdge, ...],
    ) -> Self:
        content: dict[str, object] = {
            "schema_version": 1,
            "name": name,
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "edges": [edge.model_dump(mode="json") for edge in edges],
        }
        definition_hash = hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()
        return cls(
            schema_version=1,
            name=name,
            nodes=nodes,
            edges=edges,
            definition_hash=definition_hash,
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        expected = hashlib.sha256(
            _canonical_json(self._content()).encode("utf-8")
        ).hexdigest()
        if self.definition_hash != expected:
            raise ValueError("workflow definition hash mismatch")
        return self

    def canonical_json(self) -> str:
        return _canonical_json(
            {
                **self._content(),
                "definition_hash": self.definition_hash,
            }
        )

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")


class WorkflowRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowNodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    retryable: bool


class WorkflowNodeSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str
    workflow_run_id: str
    session_id: str
    node_id: str
    status: WorkflowNodeStatus
    version: int = 1
    run_id: str | None = None
    output_text: str | None = None
    usage: TokenUsage | None = None
    error: WorkflowFailure | None = None


class WorkflowRunSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_run_id: str
    session_id: str
    status: WorkflowRunStatus
    workflow: WorkflowIR
    nodes: tuple[WorkflowNodeSnapshot, ...]
    version: int = 1
    output_text: str | None = None
    usage: TokenUsage | None = None
    error: WorkflowFailure | None = None


class WorkflowResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_run_id: str
    status: WorkflowRunStatus
    nodes: tuple[WorkflowNodeSnapshot, ...]
    output_text: str
    usage: TokenUsage
