from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from agent_sdk.runtime.models import TokenUsage
from agent_sdk.runtime.execution import WorkflowExecutionDescriptor
from agent_sdk.tools.models import freeze_json, thaw_json
from agent_sdk._workflow_validation import validate_canonical_workflow_program


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


class ConditionStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    kind: Literal["condition"] = "condition"
    when: WorkflowExpression
    then_steps: tuple[WorkflowStep, ...] = Field(min_length=1)
    else_steps: tuple[WorkflowStep, ...] = ()


class LoopStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    kind: Literal["loop"] = "loop"
    until: WorkflowExpression
    max_iterations: int = Field(ge=1)
    body: tuple[WorkflowStep, ...] = Field(min_length=1)


type WorkflowStep = Annotated[
    AgentNode | ConditionStep | LoopStep,
    Field(discriminator="kind"),
]


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: Literal["agent-sdk/v1"]
    kind: Literal["Workflow"]
    name: str = Field(min_length=1, max_length=256)
    inputs: Mapping[str, JsonValue] = Field(
        default_factory=dict,
        validate_default=True,
    )
    steps: tuple[WorkflowStep, ...] = ()
    nodes: tuple[AgentNode, ...] = ()
    edges: tuple[WorkflowEdge, ...] = ()

    @field_validator("inputs", mode="before")
    @classmethod
    def _validate_inputs(cls, value: Any) -> Mapping[str, JsonValue]:
        frozen = freeze_json(value)
        if not isinstance(frozen, Mapping):
            raise ValueError("workflow inputs must be an object")
        return cast(Mapping[str, JsonValue], frozen)

    @field_validator("inputs", mode="after")
    @classmethod
    def _freeze_inputs(
        cls,
        value: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        return cast(Mapping[str, JsonValue], freeze_json(value))

    @field_serializer("inputs")
    def _serialize_inputs(self, value: Mapping[str, JsonValue]) -> Any:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_definition_shape(self) -> Self:
        if bool(self.steps) == bool(self.nodes):
            raise ValueError(
                "workflow definition must contain exactly one of steps or nodes"
            )
        if self.steps and self.edges:
            raise ValueError("workflow steps cannot contain legacy edges")
        return self


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


type InstructionOp = Literal["agent", "branch", "loop_check", "jump", "complete"]


class WorkflowInstruction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=256)
    op: InstructionOp
    agent_node_id: str | None = None
    expression: WorkflowExpression | None = None
    true_pc: int | None = None
    false_pc: int | None = None
    target_pc: int | None = None
    loop_id: str | None = None
    max_iterations: int | None = None

    @model_validator(mode="after")
    def _validate_operation_shape(self) -> Self:
        values = {
            "agent_node_id": self.agent_node_id,
            "expression": self.expression,
            "true_pc": self.true_pc,
            "false_pc": self.false_pc,
            "target_pc": self.target_pc,
            "loop_id": self.loop_id,
            "max_iterations": self.max_iterations,
        }
        required: dict[InstructionOp, frozenset[str]] = {
            "agent": frozenset({"agent_node_id"}),
            "branch": frozenset({"expression", "true_pc", "false_pc"}),
            "loop_check": frozenset(
                {
                    "expression",
                    "true_pc",
                    "false_pc",
                    "loop_id",
                    "max_iterations",
                }
            ),
            "jump": frozenset({"target_pc"}),
            "complete": frozenset(),
        }
        expected = required[self.op]
        present = frozenset(key for key, value in values.items() if value is not None)
        if present != expected:
            raise ValueError(f"workflow {self.op} instruction fields are invalid")
        for field in ("true_pc", "false_pc", "target_pc"):
            value = values[field]
            if isinstance(value, int) and value < 0:
                raise ValueError("workflow instruction target must be non-negative")
        if self.max_iterations is not None and self.max_iterations < 1:
            raise ValueError("workflow loop limit must be positive")
        return self


class WorkflowIR(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1, 2] = 2
    name: str
    nodes: tuple[AgentNode, ...]
    edges: tuple[WorkflowEdge, ...] = ()
    inputs: Mapping[str, JsonValue] = Field(
        default_factory=dict,
        exclude_if=lambda value: not value,
        validate_default=True,
    )
    instructions: tuple[WorkflowInstruction, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    definition_hash: str

    def _content(self) -> dict[str, object]:
        if self.schema_version == 2:
            return {
                "schema_version": self.schema_version,
                "name": self.name,
                "inputs": thaw_json(self.inputs),
                "nodes": [node.model_dump(mode="json") for node in self.nodes],
                "instructions": [
                    instruction.model_dump(mode="json")
                    for instruction in self.instructions
                ],
            }
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

    @classmethod
    def create_program(
        cls,
        *,
        name: str,
        inputs: Mapping[str, JsonValue],
        nodes: tuple[AgentNode, ...],
        instructions: tuple[WorkflowInstruction, ...],
    ) -> Self:
        frozen_inputs = freeze_json(inputs)
        if not isinstance(frozen_inputs, Mapping):
            raise ValueError("workflow inputs must be an object")
        content: dict[str, object] = {
            "schema_version": 2,
            "name": name,
            "inputs": thaw_json(frozen_inputs),
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "instructions": [
                instruction.model_dump(mode="json")
                for instruction in instructions
            ],
        }
        definition_hash = hashlib.sha256(
            _canonical_json(content).encode("utf-8")
        ).hexdigest()
        return cls(
            schema_version=2,
            name=name,
            nodes=nodes,
            edges=(),
            inputs=cast(Mapping[str, JsonValue], frozen_inputs),
            instructions=instructions,
            definition_hash=definition_hash,
        )

    @field_validator("inputs", mode="before")
    @classmethod
    def _validate_inputs(cls, value: Any) -> Mapping[str, JsonValue]:
        frozen = freeze_json(value)
        if not isinstance(frozen, Mapping):
            raise ValueError("workflow inputs must be an object")
        return cast(Mapping[str, JsonValue], frozen)

    @field_validator("inputs", mode="after")
    @classmethod
    def _freeze_inputs(
        cls,
        value: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        return cast(Mapping[str, JsonValue], freeze_json(value))

    @field_serializer("inputs")
    def _serialize_inputs(self, value: Mapping[str, JsonValue]) -> Any:
        return thaw_json(value)

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.schema_version == 1:
            if self.inputs or self.instructions:
                raise ValueError(
                    "schema-v1 workflow IR cannot contain inputs or instructions"
                )
            _validate_canonical_graph(self.nodes, self.edges)
        else:
            _validate_canonical_program(self.nodes, self.edges, self.instructions)
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


def _validate_canonical_program(
    nodes: tuple[AgentNode, ...],
    edges: tuple[WorkflowEdge, ...],
    instructions: tuple[WorkflowInstruction, ...],
) -> None:
    if edges:
        raise ValueError("schema-v2 workflow IR cannot contain edges")
    node_ids = tuple(node.id for node in nodes)
    validate_canonical_workflow_program(node_ids, instructions)


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


class WorkflowControlState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    program_counter: int = Field(default=0, ge=0)
    revision: int = Field(default=1, ge=1)
    selected_branches: Mapping[str, Literal["then", "else"]] = Field(
        default_factory=dict,
        validate_default=True,
    )
    loop_iterations: Mapping[str, int] = Field(
        default_factory=dict,
        validate_default=True,
    )
    outputs: Mapping[str, JsonValue] = Field(
        default_factory=dict,
        validate_default=True,
    )
    last_output_node_id: str | None = Field(
        default=None,
        max_length=128,
        exclude_if=lambda value: value is None,
    )

    @field_validator("program_counter", "revision", mode="before")
    @classmethod
    def _validate_counter_type(cls, value: Any) -> int:
        if type(value) is not int:
            raise ValueError("workflow control counters must be integers")
        return value

    @field_validator("last_output_node_id", mode="before")
    @classmethod
    def _validate_last_output_node_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("workflow last output node id must be a nonempty string")
        return value

    @field_validator(
        "selected_branches",
        "loop_iterations",
        "outputs",
        mode="before",
    )
    @classmethod
    def _validate_mapping(
        cls,
        value: Any,
        info: ValidationInfo,
    ) -> Mapping[str, Any]:
        if (
            info.field_name == "loop_iterations"
            and isinstance(value, Mapping)
            and any(type(iteration) is not int for iteration in value.values())
        ):
            raise ValueError("workflow loop iterations must be integers")
        frozen = freeze_json(value)
        if not isinstance(frozen, Mapping):
            raise ValueError("workflow control field must be an object")
        return frozen

    @field_validator("selected_branches", "loop_iterations", "outputs", mode="after")
    @classmethod
    def _freeze_mapping(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        for key in value:
            if not key or len(key) > 128:
                raise ValueError("workflow control ids must be nonempty and bounded")
        return cast(Mapping[str, Any], freeze_json(value))

    @field_validator("loop_iterations", mode="after")
    @classmethod
    def _validate_loop_iterations(
        cls,
        value: Mapping[str, int],
    ) -> Mapping[str, int]:
        if any(iteration < 0 for iteration in value.values()):
            raise ValueError("workflow loop iterations must be non-negative")
        return value

    @field_serializer("selected_branches", "loop_iterations", "outputs")
    def _serialize_mapping(self, value: Mapping[str, Any]) -> Any:
        return thaw_json(value)

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
    control: WorkflowControlState | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

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
        control_revision = 0
        if self.workflow.schema_version == 1:
            if self.control is not None:
                raise ValueError("schema-v1 workflow cannot contain control state")
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
                raise ValueError(
                    "workflow snapshot statuses are not a legal sequential prefix"
                )
        else:
            if self.control is None:
                raise ValueError("schema-v2 workflow requires control state")
            _validate_control_state(self.workflow, self.nodes, self.control)
            control_revision = self.control.revision - 1
            if statuses.count(WorkflowNodeStatus.RUNNING) > 1:
                raise ValueError("workflow snapshot has multiple running nodes")
            if statuses.count(WorkflowNodeStatus.FAILED) > 1:
                raise ValueError("workflow snapshot has multiple failed nodes")

        if self.status is WorkflowRunStatus.RUNNING:
            if any(
                value is not None for value in (self.output_text, self.usage, self.error)
            ):
                raise ValueError("running workflow contains terminal fields")
        elif self.status is WorkflowRunStatus.COMPLETED:
            if self.workflow.schema_version == 1:
                completed_shape_valid = (
                    all(
                        status is WorkflowNodeStatus.COMPLETED
                        for status in statuses
                    )
                    and self.output_text == self.nodes[-1].output_text
                )
            else:
                completed_shape_valid = all(
                    status
                    not in {WorkflowNodeStatus.RUNNING, WorkflowNodeStatus.FAILED}
                    for status in statuses
                )
            if (
                not completed_shape_valid
                or self.output_text is None
                or self.usage is None
                or self.error is not None
                or self.usage != _sum_node_usage(self.nodes)
            ):
                raise ValueError("completed workflow state is invalid")
        else:
            failed_nodes = tuple(
                node
                for node in self.nodes
                if node.status is WorkflowNodeStatus.FAILED
            )
            failed_shape_valid = (
                len(failed_nodes) == 1 and self.error == failed_nodes[0].error
            )
            if self.workflow.schema_version == 2 and not failed_nodes:
                failed_shape_valid = all(
                    status is not WorkflowNodeStatus.RUNNING for status in statuses
                )
            if (
                not failed_shape_valid
                or self.error is None
                or self.output_text is not None
                or self.usage is not None
            ):
                raise ValueError("failed workflow state is invalid")
        base_version = (
            1
            + sum(node.version - 1 for node in self.nodes)
            + control_revision
        )
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


def _validate_control_state(
    workflow: WorkflowIR,
    nodes: tuple[WorkflowNodeSnapshot, ...],
    control: WorkflowControlState,
) -> None:
    if control.program_counter >= len(workflow.instructions):
        raise ValueError("workflow program counter is out of range")

    branch_ids = {
        instruction.id
        for instruction in workflow.instructions
        if instruction.op == "branch"
    }
    loop_limits = {
        instruction.loop_id: instruction.max_iterations
        for instruction in workflow.instructions
        if instruction.op == "loop_check"
        and instruction.loop_id is not None
        and instruction.max_iterations is not None
    }
    node_statuses = {node.node_id: node.status for node in nodes}
    if not set(control.selected_branches).issubset(branch_ids):
        raise ValueError("workflow control contains an unknown branch id")
    if not set(control.loop_iterations).issubset(loop_limits):
        raise ValueError("workflow control contains an unknown loop id")
    if any(
        iterations > loop_limits[loop_id]
        for loop_id, iterations in control.loop_iterations.items()
    ):
        raise ValueError("workflow control loop counter exceeds its limit")
    if not set(control.outputs).issubset(node_statuses):
        raise ValueError("workflow control contains an unknown output id")
    if any(
        node_statuses[node_id] is not WorkflowNodeStatus.COMPLETED
        for node_id in control.outputs
    ):
        raise ValueError("workflow control output does not belong to a completed node")
    if (
        control.last_output_node_id is not None
        and control.last_output_node_id not in control.outputs
    ):
        raise ValueError("workflow last output node id is not a recorded output")


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
