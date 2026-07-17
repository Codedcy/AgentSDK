from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from agent_sdk.runtime.models import TokenUsage
from agent_sdk.runtime.execution import WorkflowExecutionDescriptor
from agent_sdk.tools.models import freeze_json, thaw_json


type JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | tuple[JsonValue, ...]
    | Mapping[str, JsonValue]
)


class WorkflowExpression(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "exists"]
    value: JsonValue = None

    @field_validator("value", mode="before")
    @classmethod
    def _validate_json_value(cls, value: Any) -> JsonValue:
        return cast(JsonValue, freeze_json(value))

    @field_validator("value", mode="after")
    @classmethod
    def _freeze_value(cls, value: JsonValue) -> JsonValue:
        return cast(JsonValue, freeze_json(value))

    @field_serializer("value")
    def _serialize_value(self, value: JsonValue) -> Any:
        return thaw_json(value)


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
        _validate_canonical_graph(self.nodes, self.edges)
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


def _validate_canonical_graph(
    nodes: tuple[AgentNode, ...],
    edges: tuple[WorkflowEdge, ...],
) -> None:
    if not nodes:
        raise ValueError("workflow IR must contain at least one node")
    node_ids = tuple(node.id for node in nodes)
    if len(set(node_ids)) != len(node_ids):
        raise ValueError("workflow IR node ids must be unique")
    if nodes[0].run_as == "child":
        raise ValueError("workflow IR root cannot be a child")
    expected_edges = tuple(
        (left.id, right.id) for left, right in zip(nodes, nodes[1:])
    )
    actual_edges = tuple((edge.source, edge.target) for edge in edges)
    if actual_edges != expected_edges:
        raise ValueError("workflow IR must be a canonical sequential chain")


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

    @model_validator(mode="after")
    def _validate_status_fields(self) -> Self:
        if self.status is WorkflowNodeStatus.PENDING:
            if self.version != 1 or any(
                value is not None
                for value in (self.run_id, self.output_text, self.usage, self.error)
            ):
                raise ValueError("pending workflow node contains execution state")
        elif self.status is WorkflowNodeStatus.RUNNING:
            if (
                self.version != 2
                or self.run_id is None
                or any(
                    value is not None
                    for value in (self.output_text, self.usage, self.error)
                )
            ):
                raise ValueError("running workflow node state is invalid")
        elif self.status is WorkflowNodeStatus.COMPLETED:
            if (
                self.version != 3
                or self.run_id is None
                or self.output_text is None
                or self.usage is None
                or self.error is not None
            ):
                raise ValueError("completed workflow node state is invalid")
        elif (
            self.version != 3
            or self.run_id is None
            or self.error is None
            or self.output_text is not None
            or self.usage is not None
        ):
            raise ValueError("failed workflow node state is invalid")
        return self

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
    execution_compatibility: Literal["legacy_unknown", "current"] = "legacy_unknown"
    execution_descriptor: WorkflowExecutionDescriptor | None = None

    @model_validator(mode="after")
    def _validate_aggregate(self) -> Self:
        if (self.execution_compatibility == "current") != (
            self.execution_descriptor is not None
        ):
            raise ValueError("workflow execution compatibility is invalid")
        if (
            self.execution_descriptor is not None
            and self.execution_descriptor.workflow.model_dump(mode="json")
            != self.workflow.model_dump(mode="json")
        ):
            raise ValueError("workflow does not match execution descriptor")
        if len(self.nodes) != len(self.workflow.nodes):
            raise ValueError("workflow snapshot node count does not match definition")
        for node, definition_node in zip(self.nodes, self.workflow.nodes, strict=True):
            if (
                node.workflow_run_id != self.workflow_run_id
                or node.session_id != self.session_id
                or node.node_id != definition_node.id
                or node.entity_id != f"{self.workflow_run_id}:{definition_node.id}"
            ):
                raise ValueError("workflow snapshot node ownership is invalid")

        statuses = tuple(node.status for node in self.nodes)
        first_incomplete = next(
            (
                index
                for index, status in enumerate(statuses)
                if status is not WorkflowNodeStatus.COMPLETED
            ),
            len(statuses),
        )
        if any(
            status is not WorkflowNodeStatus.PENDING
            for status in statuses[first_incomplete + 1 :]
        ):
            raise ValueError("workflow snapshot statuses are not a legal sequential prefix")

        if self.status is WorkflowRunStatus.RUNNING:
            if any(
                value is not None for value in (self.output_text, self.usage, self.error)
            ):
                raise ValueError("running workflow contains terminal fields")
        elif self.status is WorkflowRunStatus.COMPLETED:
            if (
                any(status is not WorkflowNodeStatus.COMPLETED for status in statuses)
                or self.output_text is None
                or self.usage is None
                or self.error is not None
                or self.output_text != self.nodes[-1].output_text
                or self.usage != _sum_node_usage(self.nodes)
            ):
                raise ValueError("completed workflow state is invalid")
        elif (
            statuses.count(WorkflowNodeStatus.FAILED) != 1
            or self.error is None
            or self.output_text is not None
            or self.usage is not None
            or self.error
            != next(
                node.error
                for node in self.nodes
                if node.status is WorkflowNodeStatus.FAILED
            )
        ):
            raise ValueError("failed workflow state is invalid")
        base_version = 1 + sum(node.version - 1 for node in self.nodes)
        expected_version = (
            base_version
            if self.status is WorkflowRunStatus.RUNNING
            else base_version + 1
        )
        if self.version != expected_version:
            raise ValueError("workflow snapshot version does not match node state")
        return self

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


class WorkflowResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_run_id: str
    status: WorkflowRunStatus
    nodes: tuple[WorkflowNodeSnapshot, ...]
    output_text: str
    usage: TokenUsage


def _sum_node_usage(nodes: tuple[WorkflowNodeSnapshot, ...]) -> TokenUsage:
    def total(field: str) -> int | None:
        values = [
            getattr(node.usage, field)
            for node in nodes
            if node.usage is not None and getattr(node.usage, field) is not None
        ]
        return sum(values) if values else None

    return TokenUsage(
        prompt_tokens=total("prompt_tokens"),
        completion_tokens=total("completion_tokens"),
        total_tokens=total("total_tokens"),
    )
