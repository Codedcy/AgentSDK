from __future__ import annotations

import json
import math
from collections.abc import Mapping
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from agent_sdk.context_runtime import ContextRuntimeConfig
from agent_sdk.runtime.model_params import (
    freeze_model_params,
    validate_model_params_for_durability,
)
from agent_sdk.tools.models import ToolSpec
from agent_sdk._workflow_validation import validate_canonical_workflow_program


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("execution descriptor values must be JSON-compatible")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _hash(value: object) -> str:
    encoded = json.dumps(
        _thaw_json(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _model_json(value: object) -> dict[str, Any]:
    dump = getattr(value, "model_dump", None)
    if dump is None:
        raise ValueError("descriptor input must be a model")
    result = dump(mode="json")
    if not isinstance(result, dict):
        raise ValueError("descriptor model must serialize to an object")
    return result


class _RevalidatedDescriptor(BaseModel):
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


class DurableAgentSpec(_RevalidatedDescriptor):
    """Cycle-free, strict durable representation of ``AgentSpec``."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: str
    model: str
    model_params: Mapping[str, Any] = Field(default_factory=dict)
    revision: str = "1"
    prompt_profile: Literal["general", "coding"] = "general"
    system_prompt: str | None = None
    skills: tuple[str, ...] = ()
    context: ContextRuntimeConfig = Field(default_factory=ContextRuntimeConfig)
    tool_allowlist: tuple[str, ...] | None = None
    workspace_allowlist: tuple[str, ...] | None = None

    @field_validator("model_params", mode="before")
    @classmethod
    def _reject_credentials(cls, value: Any) -> Any:
        validate_model_params_for_durability(value)
        return value

    @field_validator("model_params", mode="after")
    @classmethod
    def _model_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_model_params(value)

    @field_serializer("model_params")
    def _serialize_model_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
        result = _thaw_json(value)
        assert isinstance(result, dict)
        return result

    @field_validator("skills")
    @classmethod
    def _validate_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name.strip() for name in value):
            raise ValueError("skills must contain nonempty names")
        if len(set(value)) != len(value):
            raise ValueError("skills must be unique")
        return value

    @field_validator("tool_allowlist", "workspace_allowlist")
    @classmethod
    def _validate_capability_allowlist(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is not None and any(not item.strip() for item in value):
            raise ValueError("capability allowlists must contain nonempty values")
        return value


class DurableAgentNode(_RevalidatedDescriptor):
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


class DurableWorkflowEdge(_RevalidatedDescriptor):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)


class DurableWorkflowExpression(_RevalidatedDescriptor):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    path: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "exists"]
    value: Any = None

    @field_validator("value", mode="after")
    @classmethod
    def _value(cls, value: Any) -> Any:
        return _freeze_json(value)

    @field_serializer("value")
    def _serialize_value(self, value: Any) -> Any:
        return _thaw_json(value)


class DurableWorkflowInstruction(_RevalidatedDescriptor):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=256)
    op: Literal["agent", "branch", "loop_check", "jump", "complete"]
    agent_node_id: str | None = None
    expression: DurableWorkflowExpression | None = None
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
        required = {
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


class DurableWorkflowIR(_RevalidatedDescriptor):
    """Cycle-free, strict durable representation of ``WorkflowIR``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1, 2] = 1
    name: str
    nodes: tuple[DurableAgentNode, ...]
    edges: tuple[DurableWorkflowEdge, ...] = ()
    inputs: Mapping[str, Any] = Field(
        default_factory=dict,
        exclude_if=lambda value: not value,
        validate_default=True,
    )
    instructions: tuple[DurableWorkflowInstruction, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    definition_hash: str

    @field_validator("inputs", mode="after")
    @classmethod
    def _inputs(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen = _freeze_json(value)
        assert isinstance(frozen, Mapping)
        return frozen

    @field_serializer("inputs")
    def _serialize_inputs(self, value: Mapping[str, Any]) -> dict[str, Any]:
        result = _thaw_json(value)
        assert isinstance(result, dict)
        return result

    def _content(self) -> dict[str, object]:
        if self.schema_version == 2:
            return {
                "schema_version": self.schema_version,
                "name": self.name,
                "inputs": _thaw_json(self.inputs),
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

    @model_validator(mode="after")
    def _validate_canonical_ir(self) -> Self:
        if self.schema_version == 1:
            if self.inputs or self.instructions:
                raise ValueError(
                    "schema-v1 workflow IR cannot contain inputs or instructions"
                )
            self._validate_schema_v1()
        else:
            self._validate_schema_v2()
        if self.definition_hash != _hash(self._content()):
            raise ValueError("workflow definition hash mismatch")
        return self

    def _validate_schema_v1(self) -> None:
        if not self.nodes:
            raise ValueError("workflow IR must contain at least one node")
        node_ids = tuple(node.id for node in self.nodes)
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("workflow IR node ids must be unique")
        if self.nodes[0].run_as == "child":
            raise ValueError("workflow IR root cannot be a child")
        expected_edges = tuple(
            (left.id, right.id) for left, right in zip(self.nodes, self.nodes[1:])
        )
        actual_edges = tuple((edge.source, edge.target) for edge in self.edges)
        if actual_edges != expected_edges:
            raise ValueError("workflow IR must be a canonical sequential chain")

    def _validate_schema_v2(self) -> None:
        if self.edges:
            raise ValueError("schema-v2 workflow IR cannot contain edges")
        node_ids = tuple(node.id for node in self.nodes)
        validate_canonical_workflow_program(node_ids, self.instructions)


class ToolCapabilityDescriptor(_RevalidatedDescriptor):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spec: ToolSpec
    capability_hash: str

    @classmethod
    def from_spec(cls, spec: ToolSpec) -> Self:
        detached = ToolSpec.model_validate(spec.model_dump(mode="json"))
        return cls(spec=detached, capability_hash=_hash(detached.model_dump(mode="json")))

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.capability_hash != _hash(self.spec.model_dump(mode="json")):
            raise ValueError("tool capability hash mismatch")
        return self


class ExecutionPolicyDescriptor(_RevalidatedDescriptor):
    model_config = ConfigDict(frozen=True, extra="forbid")

    permission_default: Literal["allow", "deny", "ask"]
    permission_rules: tuple[Mapping[str, Any], ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    policy_hash: str

    @classmethod
    def create(
        cls,
        *,
        permission_default: Literal["allow", "deny", "ask"],
        permission_rules: tuple[Mapping[str, Any], ...] = (),
    ) -> Self:
        canonical_rules = tuple(
            cast(dict[str, Any], _thaw_json(_freeze_json(rule)))
            for rule in permission_rules
        )
        content: dict[str, Any] = {"permission_default": permission_default}
        if canonical_rules:
            content["permission_rules"] = list(canonical_rules)
        return cls(
            permission_default=permission_default,
            permission_rules=canonical_rules,
            policy_hash=_hash(content),
        )

    @field_validator("permission_rules", mode="after")
    @classmethod
    def _permission_rules(
        cls,
        value: tuple[Mapping[str, Any], ...],
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(cast(Mapping[str, Any], _freeze_json(rule)) for rule in value)

    @field_serializer("permission_rules")
    def _serialize_permission_rules(
        self,
        value: tuple[Mapping[str, Any], ...],
    ) -> list[dict[str, Any]]:
        return [_thaw_json(rule) for rule in value]

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        content: dict[str, Any] = {
            "permission_default": self.permission_default,
        }
        if self.permission_rules:
            content["permission_rules"] = [
                _thaw_json(rule) for rule in self.permission_rules
            ]
        if self.policy_hash != _hash(content):
            raise ValueError("execution policy hash mismatch")
        return self


class ExecutionDescriptor(_RevalidatedDescriptor):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    agent: DurableAgentSpec
    agent_hash: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[ToolCapabilityDescriptor, ...]
    workspace_scopes: tuple[str, ...] | None = None
    policy: ExecutionPolicyDescriptor
    descriptor_hash: str

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_agent_fields(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        agent = value.get("agent")
        if not isinstance(agent, Mapping):
            return value
        new_agent_fields = {
            "prompt_profile",
            "system_prompt",
            "skills",
            "context",
            "tool_allowlist",
            "workspace_allowlist",
        }
        if new_agent_fields <= set(agent) and "workspace_scopes" in value:
            return value
        raw_agent = dict(agent)
        if value.get("agent_hash") != _hash(raw_agent):
            return value
        raw_content = {
            key: _thaw_json(item)
            for key, item in value.items()
            if key != "descriptor_hash"
        }
        if value.get("descriptor_hash") != _hash(raw_content):
            return value
        upgraded_agent = DurableAgentSpec.model_validate(raw_agent).model_dump(mode="json")
        upgraded = {key: _thaw_json(item) for key, item in value.items()}
        upgraded["agent"] = upgraded_agent
        upgraded.setdefault("workspace_scopes", None)
        upgraded["agent_hash"] = _hash(upgraded_agent)
        upgraded["descriptor_hash"] = _hash(
            {
                key: item
                for key, item in upgraded.items()
                if key != "descriptor_hash"
            }
        )
        return upgraded

    @field_validator("messages", mode="after")
    @classmethod
    def _messages(
        cls, value: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(cast(Mapping[str, Any], _freeze_json(message)) for message in value)

    @field_serializer("messages")
    def _serialize_messages(
        self, value: tuple[Mapping[str, Any], ...]
    ) -> list[dict[str, Any]]:
        return [_thaw_json(message) for message in value]

    @classmethod
    def create(
        cls,
        *,
        agent: object,
        messages: tuple[Mapping[str, Any], ...],
        tools: tuple[ToolCapabilityDescriptor, ...],
        workspace_scopes: tuple[str, ...] | None = None,
        policy: ExecutionPolicyDescriptor,
    ) -> Self:
        validate_model_params_for_durability(getattr(agent, "model_params", None))
        agent_data = DurableAgentSpec.model_validate(_model_json(agent))
        values: dict[str, Any] = {
            "agent": agent_data,
            "agent_hash": _hash(agent_data.model_dump(mode="json")),
            "messages": messages,
            "tools": tools,
            "workspace_scopes": workspace_scopes,
            "policy": policy,
        }
        content = {
            "agent": agent_data.model_dump(mode="json"),
            "agent_hash": values["agent_hash"],
            "messages": list(messages),
            "tools": [tool.model_dump(mode="json") for tool in tools],
            "workspace_scopes": workspace_scopes,
            "policy": policy.model_dump(mode="json"),
        }
        return cls(**values, descriptor_hash=_hash(content))

    def _content(self) -> dict[str, Any]:
        return {
            "agent": self.agent.model_dump(mode="json"),
            "agent_hash": self.agent_hash,
            "messages": [_thaw_json(message) for message in self.messages],
            "tools": [tool.model_dump(mode="json") for tool in self.tools],
            "workspace_scopes": self.workspace_scopes,
            "policy": self.policy.model_dump(mode="json"),
        }

    @model_validator(mode="after")
    def _validate_hashes(self) -> Self:
        tool_names = tuple(tool.spec.name for tool in self.tools)
        if len(set(tool_names)) != len(tool_names):
            raise ValueError("execution tools must be unique")
        if self.agent_hash != _hash(self.agent.model_dump(mode="json")):
            raise ValueError("agent hash mismatch")
        if self.descriptor_hash != _hash(self._content()):
            raise ValueError("execution descriptor hash mismatch")
        return self


class WorkflowAgentDescriptor(_RevalidatedDescriptor):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: str = Field(min_length=1)
    execution: ExecutionDescriptor
    descriptor_hash: str

    @classmethod
    def create(cls, revision: str, execution: ExecutionDescriptor) -> Self:
        content = {"revision": revision, "execution": execution.model_dump(mode="json")}
        return cls(
            revision=revision,
            execution=execution,
            descriptor_hash=_hash(content),
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        content = {"revision": self.revision, "execution": self.execution.model_dump(mode="json")}
        agent = self.execution.agent
        if f"{agent.name}:{agent.revision}" != self.revision:
            raise ValueError("workflow agent revision mismatch")
        if self.descriptor_hash != _hash(content):
            raise ValueError("workflow agent descriptor hash mismatch")
        return self


class WorkflowExecutionDescriptor(_RevalidatedDescriptor):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: DurableWorkflowIR
    workflow_definition_hash: str
    agents: tuple[WorkflowAgentDescriptor, ...]
    tools: tuple[ToolCapabilityDescriptor, ...]
    policy: ExecutionPolicyDescriptor
    descriptor_hash: str

    @classmethod
    def create(
        cls,
        *,
        workflow: object,
        agents: tuple[WorkflowAgentDescriptor, ...],
        tools: tuple[ToolCapabilityDescriptor, ...],
        policy: ExecutionPolicyDescriptor,
    ) -> Self:
        workflow_data = DurableWorkflowIR.model_validate(_model_json(workflow))
        definition_hash = workflow_data.definition_hash
        referenced_revisions = tuple(
            dict.fromkeys(node.agent_revision for node in workflow_data.nodes)
        )
        if len({agent.revision for agent in agents}) != len(agents):
            raise ValueError("workflow execution agents must be unique")
        agents_by_revision = {agent.revision: agent for agent in agents}
        if set(agents_by_revision) != set(referenced_revisions):
            raise ValueError("workflow execution agents are invalid")
        canonical_agents = tuple(
            agents_by_revision[revision] for revision in referenced_revisions
        )
        content = {
            "workflow": workflow_data.model_dump(mode="json"),
            "workflow_definition_hash": definition_hash,
            "agents": [agent.model_dump(mode="json") for agent in canonical_agents],
            "tools": [tool.model_dump(mode="json") for tool in tools],
            "policy": policy.model_dump(mode="json"),
        }
        return cls(
            workflow=workflow_data,
            workflow_definition_hash=definition_hash,
            agents=canonical_agents,
            tools=tools,
            policy=policy,
            descriptor_hash=_hash(content),
        )

    def _content(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow.model_dump(mode="json"),
            "workflow_definition_hash": self.workflow_definition_hash,
            "agents": [agent.model_dump(mode="json") for agent in self.agents],
            "tools": [tool.model_dump(mode="json") for tool in self.tools],
            "policy": self.policy.model_dump(mode="json"),
        }

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        agent_revisions = tuple(agent.revision for agent in self.agents)
        tool_names = tuple(tool.spec.name for tool in self.tools)
        referenced_revisions = tuple(
            dict.fromkeys(node.agent_revision for node in self.workflow.nodes)
        )
        if agent_revisions != referenced_revisions:
            raise ValueError("workflow execution agents are invalid")
        if len(set(tool_names)) != len(tool_names):
            raise ValueError("workflow execution tools must be unique")
        if any(
            agent.execution.tools != self.tools for agent in self.agents
        ):
            raise ValueError("workflow agent tools do not match workflow tools")
        if any(
            agent.execution.policy != self.policy for agent in self.agents
        ):
            raise ValueError("workflow agent policy does not match workflow policy")
        if self.workflow.definition_hash != self.workflow_definition_hash:
            raise ValueError("workflow definition hash mismatch")
        if self.descriptor_hash != _hash(self._content()):
            raise ValueError("workflow execution descriptor hash mismatch")
        return self
