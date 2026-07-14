from __future__ import annotations

import json
import math
from collections.abc import Mapping
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from agent_sdk.tools.models import ToolSpec


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
    policy_hash: str

    @classmethod
    def create(cls, *, permission_default: Literal["allow", "deny", "ask"]) -> Self:
        content = {"permission_default": permission_default}
        return cls(**content, policy_hash=_hash(content))

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.policy_hash != _hash({"permission_default": self.permission_default}):
            raise ValueError("execution policy hash mismatch")
        return self


class ExecutionDescriptor(_RevalidatedDescriptor):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    agent: Mapping[str, Any]
    agent_hash: str
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[ToolCapabilityDescriptor, ...]
    policy: ExecutionPolicyDescriptor
    descriptor_hash: str

    @field_validator("agent", mode="after")
    @classmethod
    def _agent(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        expected = {"name", "model", "model_params", "revision"}
        if set(value) != expected:
            raise ValueError("execution agent shape is invalid")
        return cast(Mapping[str, Any], _freeze_json(value))

    @field_validator("messages", mode="after")
    @classmethod
    def _messages(
        cls, value: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(cast(Mapping[str, Any], _freeze_json(message)) for message in value)

    @field_serializer("agent")
    def _serialize_agent(self, value: Mapping[str, Any]) -> dict[str, Any]:
        result = _thaw_json(value)
        assert isinstance(result, dict)
        return result

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
        policy: ExecutionPolicyDescriptor,
    ) -> Self:
        agent_data = _model_json(agent)
        canonical_tools = tuple(sorted(tools, key=lambda item: item.spec.name))
        values: dict[str, Any] = {
            "agent": agent_data,
            "agent_hash": _hash(agent_data),
            "messages": messages,
            "tools": canonical_tools,
            "policy": policy,
        }
        content = {
            "agent": agent_data,
            "agent_hash": values["agent_hash"],
            "messages": list(messages),
            "tools": [tool.model_dump(mode="json") for tool in canonical_tools],
            "policy": policy.model_dump(mode="json"),
        }
        return cls(**values, descriptor_hash=_hash(content))

    def _content(self) -> dict[str, Any]:
        return {
            "agent": _thaw_json(self.agent),
            "agent_hash": self.agent_hash,
            "messages": [_thaw_json(message) for message in self.messages],
            "tools": [tool.model_dump(mode="json") for tool in self.tools],
            "policy": self.policy.model_dump(mode="json"),
        }

    @model_validator(mode="after")
    def _validate_hashes(self) -> Self:
        tool_names = tuple(tool.spec.name for tool in self.tools)
        if tool_names != tuple(sorted(set(tool_names))):
            raise ValueError("execution tools must be sorted and unique")
        if self.agent_hash != _hash(self.agent):
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
        if f"{agent['name']}:{agent['revision']}" != self.revision:
            raise ValueError("workflow agent revision mismatch")
        if self.descriptor_hash != _hash(content):
            raise ValueError("workflow agent descriptor hash mismatch")
        return self


class WorkflowExecutionDescriptor(_RevalidatedDescriptor):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    workflow: Mapping[str, Any]
    workflow_definition_hash: str
    agents: tuple[WorkflowAgentDescriptor, ...]
    tools: tuple[ToolCapabilityDescriptor, ...]
    policy: ExecutionPolicyDescriptor
    descriptor_hash: str

    @field_validator("workflow", mode="after")
    @classmethod
    def _workflow(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], _freeze_json(value))

    @field_serializer("workflow")
    def _serialize_workflow(self, value: Mapping[str, Any]) -> dict[str, Any]:
        result = _thaw_json(value)
        assert isinstance(result, dict)
        return result

    @classmethod
    def create(
        cls,
        *,
        workflow: object,
        agents: tuple[WorkflowAgentDescriptor, ...],
        tools: tuple[ToolCapabilityDescriptor, ...],
        policy: ExecutionPolicyDescriptor,
    ) -> Self:
        workflow_data = _model_json(workflow)
        definition_hash = workflow_data.get("definition_hash")
        if not isinstance(definition_hash, str):
            raise ValueError("workflow definition hash is missing")
        canonical_agents = tuple(sorted(agents, key=lambda item: item.revision))
        canonical_tools = tuple(sorted(tools, key=lambda item: item.spec.name))
        content = {
            "workflow": workflow_data,
            "workflow_definition_hash": definition_hash,
            "agents": [agent.model_dump(mode="json") for agent in canonical_agents],
            "tools": [tool.model_dump(mode="json") for tool in canonical_tools],
            "policy": policy.model_dump(mode="json"),
        }
        return cls(
            workflow=workflow_data,
            workflow_definition_hash=definition_hash,
            agents=canonical_agents,
            tools=canonical_tools,
            policy=policy,
            descriptor_hash=_hash(content),
        )

    def _content(self) -> dict[str, Any]:
        return {
            "workflow": _thaw_json(self.workflow),
            "workflow_definition_hash": self.workflow_definition_hash,
            "agents": [agent.model_dump(mode="json") for agent in self.agents],
            "tools": [tool.model_dump(mode="json") for tool in self.tools],
            "policy": self.policy.model_dump(mode="json"),
        }

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        agent_revisions = tuple(agent.revision for agent in self.agents)
        tool_names = tuple(tool.spec.name for tool in self.tools)
        workflow_nodes = self.workflow.get("nodes")
        if not isinstance(workflow_nodes, tuple):
            raise ValueError("workflow execution nodes are invalid")
        referenced_revisions = {
            node.get("agent_revision")
            for node in workflow_nodes
            if isinstance(node, Mapping)
        }
        if (
            agent_revisions != tuple(sorted(set(agent_revisions)))
            or set(agent_revisions) != referenced_revisions
        ):
            raise ValueError("workflow execution agents are invalid")
        if tool_names != tuple(sorted(set(tool_names))):
            raise ValueError("workflow execution tools must be sorted and unique")
        if self.workflow.get("definition_hash") != self.workflow_definition_hash:
            raise ValueError("workflow definition hash mismatch")
        if self.descriptor_hash != _hash(self._content()):
            raise ValueError("workflow execution descriptor hash mismatch")
        return self
