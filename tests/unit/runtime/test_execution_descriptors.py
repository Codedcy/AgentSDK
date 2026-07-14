from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
    WorkflowAgentDescriptor,
    WorkflowExecutionDescriptor,
)
from agent_sdk.runtime.models import AgentSpec, RunSnapshot, RunStatus, SessionSnapshot, SessionStatus
from agent_sdk.tools.models import ToolSpec
from agent_sdk.workflow.models import (
    AgentNode,
    WorkflowIR,
    WorkflowNodeSnapshot,
    WorkflowNodeStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)


def _tool(**updates: object) -> ToolSpec:
    data: dict[str, object] = {
        "name": "bash",
        "description": "Run a command",
        "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        "version": "handler-v1",
        "source": "builtin",
        "effects": ("process",),
        "timeout_seconds": 30.0,
    }
    data.update(updates)
    return ToolSpec.model_validate(data)


def test_tool_capability_hash_covers_full_spec() -> None:
    base = ToolCapabilityDescriptor.from_spec(_tool())
    for update in (
        {"name": "shell"},
        {"description": "Different behavior"},
        {"input_schema": {"type": "object", "required": ["cmd"]}},
        {"version": "handler-v2"},
        {"source": "mcp/server"},
        {"effects": ("filesystem",)},
        {"timeout_seconds": 31.0},
    ):
        changed = ToolCapabilityDescriptor.from_spec(_tool(**update))
        assert changed.capability_hash != base.capability_hash
    with pytest.raises(ValidationError, match="hash"):
        ToolCapabilityDescriptor.model_validate(
            {**base.model_dump(mode="json"), "capability_hash": "0" * 64}
        )
    with pytest.raises(ValidationError, match="hash"):
        base.model_copy(update={"capability_hash": "0" * 64})


def test_serialized_capabilities_forbid_handlers_and_credentials() -> None:
    descriptor = ToolCapabilityDescriptor.from_spec(_tool())
    with pytest.raises(ValidationError):
        ToolCapabilityDescriptor.model_validate(
            {**descriptor.model_dump(mode="json"), "handler": "callable"}
        )
    policy = ExecutionPolicyDescriptor.create(permission_default="ask")
    with pytest.raises(ValidationError):
        ExecutionPolicyDescriptor.model_validate(
            {**policy.model_dump(mode="json"), "credentials": "secret"}
        )


def test_tool_version_and_source_are_nonempty() -> None:
    with pytest.raises(ValidationError):
        _tool(version="")
    with pytest.raises(ValidationError):
        _tool(source="")


def test_policy_hash_covers_permission_default() -> None:
    allow = ExecutionPolicyDescriptor.create(permission_default="allow")
    deny = ExecutionPolicyDescriptor.create(permission_default="deny")
    assert allow.policy_hash != deny.policy_hash


def test_execution_descriptor_is_immutable_and_revalidates_hashes() -> None:
    agent = AgentSpec(name="coder", model="openai/test", model_params={"temperature": 0})
    capability = ToolCapabilityDescriptor.from_spec(_tool())
    descriptor = ExecutionDescriptor.create(
        agent=agent,
        messages=({"role": "user", "content": "hello"},),
        tools=(capability,),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    assert descriptor.agent_hash
    assert descriptor.descriptor_hash
    with pytest.raises(ValidationError):
        ExecutionDescriptor.model_validate(
            {**descriptor.model_dump(mode="json"), "descriptor_hash": "f" * 64}
        )
    with pytest.raises(TypeError):
        descriptor.messages[0]["content"] = "changed"  # type: ignore[index]

    changed_agent = AgentSpec(
        name="coder",
        model="openai/test",
        model_params={"temperature": 1},
    )
    changed = ExecutionDescriptor.create(
        agent=changed_agent,
        messages=({"role": "user", "content": "hello"},),
        tools=(capability,),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    assert changed.agent_hash != descriptor.agent_hash
    assert changed.descriptor_hash != descriptor.descriptor_hash


def test_workflow_descriptor_covers_agents_workflow_tools_and_policy() -> None:
    workflow = WorkflowIR.create(
        name="single",
        nodes=(AgentNode(id="one", agent_revision="coder:1", input="work"),),
        edges=(),
    )
    agent = AgentSpec(name="coder", model="openai/test")
    run_descriptor = ExecutionDescriptor.create(
        agent=agent,
        messages=({"role": "user", "content": "work"},),
        tools=(ToolCapabilityDescriptor.from_spec(_tool()),),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    descriptor = WorkflowExecutionDescriptor.create(
        workflow=workflow,
        agents=(WorkflowAgentDescriptor.create("coder:1", run_descriptor),),
        tools=run_descriptor.tools,
        policy=run_descriptor.policy,
    )
    assert descriptor.workflow_definition_hash == workflow.definition_hash
    assert descriptor.descriptor_hash


def test_session_snapshot_is_strict_and_owns_sorted_unique_active_work() -> None:
    snapshot = SessionSnapshot(
        session_id="ses_1",
        status=SessionStatus.CLOSING,
        workspaces=("workspace",),
        active_run_ids=("run_a", "run_b"),
    )
    assert snapshot.model_dump(mode="json")["status"] == "closing"
    for invalid in (
        {**snapshot.model_dump(mode="json"), "active_run_ids": ["run_b", "run_a"]},
        {**snapshot.model_dump(mode="json"), "active_run_ids": ["run_a", "run_a"]},
        {**snapshot.model_dump(mode="json"), "version": 0},
        {**snapshot.model_dump(mode="json"), "unknown": True},
        {**snapshot.model_dump(mode="json"), "status": "closed"},
    ):
        with pytest.raises(ValidationError):
            SessionSnapshot.model_validate(invalid)
    with pytest.raises(ValidationError):
        snapshot.model_copy(update={"status": "closed"})


def test_run_compatibility_requires_descriptor_only_for_current() -> None:
    legacy = RunSnapshot(
        run_id="run_1",
        session_id="ses_1",
        agent_revision="coder:1",
        status=RunStatus.CREATED,
        user_input="hello",
    )
    assert legacy.execution_compatibility == "legacy_unknown"
    descriptor = ExecutionDescriptor.create(
        agent=AgentSpec(name="coder", model="openai/test"),
        messages=({"role": "user", "content": "hello"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    current = RunSnapshot.model_validate(
        {
            **legacy.model_dump(mode="json"),
            "execution_compatibility": "current",
            "execution_descriptor": descriptor.model_dump(mode="json"),
        }
    )
    assert current.execution_descriptor == descriptor
    with pytest.raises(ValidationError):
        RunSnapshot.model_validate(
            {**legacy.model_dump(mode="json"), "execution_descriptor": descriptor.model_dump(mode="json")}
        )
    with pytest.raises(ValidationError, match="version"):
        RunSnapshot.model_validate(
            {**legacy.model_dump(mode="json"), "status": "running", "version": 1}
        )
    with pytest.raises(ValidationError, match="version"):
        legacy.model_copy(update={"status": "running", "version": 1})


def test_workflow_compatibility_requires_descriptor_only_for_current() -> None:
    workflow = WorkflowIR.create(
        name="single",
        nodes=(AgentNode(id="one", agent_revision="coder:1", input="work"),),
        edges=(),
    )
    node = WorkflowNodeSnapshot(
        entity_id="wf_1:one",
        workflow_run_id="wf_1",
        session_id="ses_1",
        node_id="one",
        status=WorkflowNodeStatus.PENDING,
    )
    legacy = WorkflowRunSnapshot(
        workflow_run_id="wf_1",
        session_id="ses_1",
        status=WorkflowRunStatus.RUNNING,
        workflow=workflow,
        nodes=(node,),
    )
    assert legacy.execution_compatibility == "legacy_unknown"
    with pytest.raises(ValidationError):
        WorkflowRunSnapshot.model_validate(
            {**legacy.model_dump(mode="json"), "execution_compatibility": "current"}
        )
