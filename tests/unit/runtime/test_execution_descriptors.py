from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from agent_sdk.runtime.execution import (
    DurableAgentSpec,
    DurableWorkflowIR,
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
    WorkflowAgentDescriptor,
    WorkflowExecutionDescriptor,
)
from agent_sdk.runtime.models import (
    AgentSpec,
    RunSnapshot,
    RunStatus,
    SessionSnapshot,
    SessionStatus,
    TokenUsage,
    run_created_event_matches,
)
from agent_sdk.context import ContextRuntimeConfig
from agent_sdk.tools.models import ToolSpec
from agent_sdk.workflow.models import (
    AgentNode,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowIR,
    WorkflowNodeSnapshot,
    WorkflowNodeStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from agent_sdk.workflow.compiler import WorkflowCompiler


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


def test_policy_hash_covers_detached_permission_rules() -> None:
    source_rule = {
        "outcome": "allow",
        "tool": "bash",
        "path_prefix": "workspace",
        "command_prefix": ["git", "status"],
    }
    with_rules = ExecutionPolicyDescriptor.create(
        permission_default="deny",
        permission_rules=(source_rule,),
    )
    without_rules = ExecutionPolicyDescriptor.create(permission_default="deny")

    source_rule["tool"] = "write"

    assert with_rules.policy_hash != without_rules.policy_hash
    assert with_rules.permission_rules[0]["tool"] == "bash"
    with pytest.raises(TypeError):
        with_rules.permission_rules[0]["tool"] = "read"  # type: ignore[index]


def test_persisted_policy_without_rules_defaults_to_empty_tuple() -> None:
    legacy = {
        "permission_default": "ask",
        "policy_hash": _canonical_hash({"permission_default": "ask"}),
    }

    restored = ExecutionPolicyDescriptor.model_validate(legacy)

    assert restored.permission_rules == ()
    assert "permission_rules" not in restored.model_dump(mode="json")


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


def test_agent_prompt_and_context_fields_are_defaulted_and_validated() -> None:
    agent = AgentSpec(name="coder", model="openai/test")

    assert agent.prompt_profile == "general"
    assert agent.system_prompt is None
    assert agent.skills == ()
    assert agent.context == ContextRuntimeConfig()
    with pytest.raises(ValidationError, match="skills"):
        AgentSpec(name="coder", model="openai/test", skills=("",))
    with pytest.raises(ValidationError, match="skills"):
        AgentSpec(name="coder", model="openai/test", skills=("demo", "demo"))


def test_execution_descriptor_hash_covers_prompt_skills_and_context() -> None:
    def descriptor(agent: AgentSpec) -> ExecutionDescriptor:
        return ExecutionDescriptor.create(
            agent=agent,
            messages=({"role": "user", "content": "hello"},),
            tools=(),
            policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
        )

    base = descriptor(AgentSpec(name="coder", model="openai/test"))
    changed = (
        AgentSpec(name="coder", model="openai/test", prompt_profile="coding"),
        AgentSpec(
            name="coder",
            model="openai/test",
            system_prompt="Application constraint.",
        ),
        AgentSpec(name="coder", model="openai/test", skills=("coding-demo",)),
        AgentSpec(
            name="coder",
            model="openai/test",
            context=ContextRuntimeConfig(model_window=64_000),
        ),
    )

    for agent in changed:
        current = descriptor(agent)
        assert current.agent_hash != base.agent_hash
        assert current.descriptor_hash != base.descriptor_hash


def test_legacy_durable_agent_and_descriptor_load_prompt_defaults() -> None:
    descriptor = ExecutionDescriptor.create(
        agent=AgentSpec(name="coder", model="openai/test"),
        messages=({"role": "user", "content": "hello"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    legacy = descriptor.model_dump(mode="json")
    for field in ("prompt_profile", "system_prompt", "skills", "context"):
        legacy["agent"].pop(field)
    legacy["agent_hash"] = _canonical_hash(legacy["agent"])
    legacy["descriptor_hash"] = _canonical_hash(
        {key: value for key, value in legacy.items() if key != "descriptor_hash"}
    )

    restored_agent = DurableAgentSpec.model_validate(legacy["agent"])
    restored = ExecutionDescriptor.model_validate(legacy)

    assert restored_agent.prompt_profile == "general"
    assert restored_agent.system_prompt is None
    assert restored_agent.skills == ()
    assert restored_agent.context == ContextRuntimeConfig()
    assert restored.agent == restored_agent
    assert restored.agent_hash == _canonical_hash(
        restored.agent.model_dump(mode="json")
    )
    assert restored.descriptor_hash == _canonical_hash(
        {
            key: value
            for key, value in restored.model_dump(mode="json").items()
            if key != "descriptor_hash"
        }
    )


def test_schema_v1_run_creation_authenticates_genuine_legacy_descriptor_hashes() -> None:
    descriptor = ExecutionDescriptor.create(
        agent=AgentSpec(name="coder", model="openai/test"),
        messages=({"role": "user", "content": "hello"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    raw_descriptor = descriptor.model_dump(mode="json")
    for field in ("prompt_profile", "system_prompt", "skills", "context"):
        raw_descriptor["agent"].pop(field)
    raw_descriptor["agent_hash"] = _canonical_hash(raw_descriptor["agent"])
    raw_descriptor["descriptor_hash"] = _canonical_hash(
        {
            key: value
            for key, value in raw_descriptor.items()
            if key != "descriptor_hash"
        }
    )
    raw_v1 = RunSnapshot(
        run_id="run_r2",
        session_id="ses_r2",
        agent_revision="coder:1",
        status=RunStatus.CREATED,
        user_input="hello",
        execution_compatibility="current",
        execution_descriptor=descriptor,
    ).model_dump(mode="json")
    raw_v1["execution_descriptor"] = raw_descriptor
    upgraded = RunSnapshot.model_validate(raw_v1)

    assert run_created_event_matches(
        upgraded,
        raw_v1,
        schema_version=1,
    )

    wrong_agent_hash = json.loads(json.dumps(raw_v1))
    wrong_agent_hash["execution_descriptor"]["agent_hash"] = "a" * 64
    assert not run_created_event_matches(
        upgraded,
        wrong_agent_hash,
        schema_version=1,
    )
    wrong_descriptor_hash = json.loads(json.dumps(raw_v1))
    wrong_descriptor_hash["execution_descriptor"]["descriptor_hash"] = "d" * 64
    assert not run_created_event_matches(
        upgraded,
        wrong_descriptor_hash,
        schema_version=1,
    )


def test_execution_descriptor_rejects_rehashed_noncanonical_agent() -> None:
    descriptor = ExecutionDescriptor.create(
        agent=AgentSpec(name="coder", model="openai/test"),
        messages=({"role": "user", "content": "hello"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    tampered = descriptor.model_dump(mode="json")
    tampered["agent"]["revision"] = 2
    tampered["agent_hash"] = _canonical_hash(tampered["agent"])
    tampered["descriptor_hash"] = _canonical_hash(
        {key: value for key, value in tampered.items() if key != "descriptor_hash"}
    )

    with pytest.raises(ValidationError):
        ExecutionDescriptor.model_validate(tampered)


def test_execution_tool_order_is_preserved_and_hash_is_order_sensitive() -> None:
    bash = ToolCapabilityDescriptor.from_spec(_tool(name="bash"))
    write = ToolCapabilityDescriptor.from_spec(_tool(name="write"))
    kwargs = {
        "agent": AgentSpec(name="coder", model="openai/test"),
        "messages": ({"role": "user", "content": "hello"},),
        "policy": ExecutionPolicyDescriptor.create(permission_default="ask"),
    }

    forward = ExecutionDescriptor.create(tools=(bash, write), **kwargs)
    reverse = ExecutionDescriptor.create(tools=(write, bash), **kwargs)

    assert tuple(tool.spec.name for tool in reverse.tools) == ("write", "bash")
    assert reverse.descriptor_hash != forward.descriptor_hash


def test_execution_tools_require_order_preserving_uniqueness() -> None:
    bash = ToolCapabilityDescriptor.from_spec(_tool(name="bash"))
    with pytest.raises(ValidationError, match="unique"):
        ExecutionDescriptor.create(
            agent=AgentSpec(name="coder", model="openai/test"),
            messages=({"role": "user", "content": "hello"},),
            tools=(bash, bash),
            policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
        )


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


def test_workflow_descriptor_round_trips_schema_v2_program() -> None:
    workflow = WorkflowCompiler().compile(
        WorkflowDefinition.model_validate(
            {
                "api_version": "agent-sdk/v1",
                "kind": "Workflow",
                "name": "controlled",
                "inputs": {"enabled": True},
                "steps": [
                    {
                        "id": "work",
                        "kind": "agent",
                        "agent_revision": "coder:1",
                        "input": "work",
                    }
                ],
            }
        )
    )
    execution = ExecutionDescriptor.create(
        agent=AgentSpec(name="coder", model="openai/test"),
        messages=({"role": "user", "content": "work"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )

    descriptor = WorkflowExecutionDescriptor.create(
        workflow=workflow,
        agents=(WorkflowAgentDescriptor.create("coder:1", execution),),
        tools=(),
        policy=execution.policy,
    )
    restored = WorkflowExecutionDescriptor.model_validate(
        descriptor.model_dump(mode="json")
    )

    assert restored == descriptor
    assert restored.workflow.schema_version == 2
    assert tuple(item.op for item in restored.workflow.instructions) == (
        "agent",
        "complete",
    )
    for inputs in (
        descriptor.workflow.inputs,
        restored.workflow.inputs,
    ):
        with pytest.raises(TypeError):
            inputs["tampered"] = "yes"  # type: ignore[index]


def test_durable_workflow_defaults_omitted_version_to_legacy_schema_v1() -> None:
    node = AgentNode(id="one", agent_revision="coder:1", input="work")
    content = {
        "schema_version": 1,
        "name": "legacy",
        "nodes": [node.model_dump(mode="json")],
        "edges": [],
    }
    payload = {
        "name": "legacy",
        "nodes": content["nodes"],
        "edges": [],
        "definition_hash": _canonical_hash(content),
    }

    restored = DurableWorkflowIR.model_validate(payload)
    round_tripped = DurableWorkflowIR.model_validate(
        restored.model_dump(mode="json")
    )

    assert restored.schema_version == 1
    for inputs in (restored.inputs, round_tripped.inputs):
        with pytest.raises(TypeError):
            inputs["tampered"] = "yes"  # type: ignore[index]


def test_legacy_workflow_descriptor_accepts_omitted_nested_schema_version() -> None:
    workflow = WorkflowIR.create(
        name="legacy",
        nodes=(AgentNode(id="one", agent_revision="coder:1", input="work"),),
        edges=(),
    )
    execution = ExecutionDescriptor.create(
        agent=AgentSpec(name="coder", model="openai/test"),
        messages=({"role": "user", "content": "work"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    descriptor = WorkflowExecutionDescriptor.create(
        workflow=workflow,
        agents=(WorkflowAgentDescriptor.create("coder:1", execution),),
        tools=(),
        policy=execution.policy,
    )
    payload = descriptor.model_dump(mode="json")
    del payload["workflow"]["schema_version"]

    restored = WorkflowExecutionDescriptor.model_validate(payload)

    assert restored.workflow.schema_version == 1
    assert restored.workflow.definition_hash == workflow.definition_hash
    assert restored.descriptor_hash == descriptor.descriptor_hash
    with pytest.raises(TypeError):
        restored.workflow.inputs["tampered"] = "yes"  # type: ignore[index]


def test_unversioned_durable_v2_program_is_rejected() -> None:
    workflow = WorkflowCompiler().compile(
        WorkflowDefinition.model_validate(
            {
                "api_version": "agent-sdk/v1",
                "kind": "Workflow",
                "name": "controlled",
                "steps": [
                    {
                        "id": "work",
                        "kind": "agent",
                        "agent_revision": "coder:1",
                        "input": "work",
                    }
                ],
            }
        )
    )
    payload = workflow.model_dump(mode="json")
    del payload["schema_version"]

    with pytest.raises(ValidationError):
        DurableWorkflowIR.model_validate(payload)


def test_workflow_descriptor_rejects_rehashed_noncanonical_workflow() -> None:
    workflow = WorkflowIR.create(
        name="single",
        nodes=(AgentNode(id="one", agent_revision="coder:1", input="work"),),
        edges=(),
    )
    run_descriptor = ExecutionDescriptor.create(
        agent=AgentSpec(name="coder", model="openai/test"),
        messages=({"role": "user", "content": "work"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    descriptor = WorkflowExecutionDescriptor.create(
        workflow=workflow,
        agents=(WorkflowAgentDescriptor.create("coder:1", run_descriptor),),
        tools=(),
        policy=run_descriptor.policy,
    )
    tampered = descriptor.model_dump(mode="json")
    tampered["workflow"]["nodes"][0]["run_as"] = "sideways"
    workflow_content = {
        key: value
        for key, value in tampered["workflow"].items()
        if key != "definition_hash"
    }
    tampered["workflow"]["definition_hash"] = _canonical_hash(workflow_content)
    tampered["workflow_definition_hash"] = tampered["workflow"]["definition_hash"]
    tampered["descriptor_hash"] = _canonical_hash(
        {key: value for key, value in tampered.items() if key != "descriptor_hash"}
    )

    with pytest.raises(ValidationError):
        WorkflowExecutionDescriptor.model_validate(tampered)


def test_workflow_agent_order_uses_first_node_reference_and_tools_keep_order() -> None:
    workflow = WorkflowIR.create(
        name="two",
        nodes=(
            AgentNode(id="first", agent_revision="zeta:1", input="first"),
            AgentNode(id="second", agent_revision="alpha:1", input="second"),
        ),
        edges=(WorkflowEdge(source="first", target="second"),),
    )
    bash = ToolCapabilityDescriptor.from_spec(_tool(name="bash"))
    write = ToolCapabilityDescriptor.from_spec(_tool(name="write"))
    policy = ExecutionPolicyDescriptor.create(permission_default="ask")

    def agent(name: str, message: str) -> WorkflowAgentDescriptor:
        execution = ExecutionDescriptor.create(
            agent=AgentSpec(name=name, model="openai/test"),
            messages=({"role": "user", "content": message},),
            tools=(write, bash),
            policy=policy,
        )
        return WorkflowAgentDescriptor.create(f"{name}:1", execution)

    descriptor = WorkflowExecutionDescriptor.create(
        workflow=workflow,
        agents=(agent("alpha", "second"), agent("zeta", "first")),
        tools=(write, bash),
        policy=policy,
    )

    assert tuple(item.revision for item in descriptor.agents) == ("zeta:1", "alpha:1")
    assert tuple(item.spec.name for item in descriptor.tools) == ("write", "bash")


def test_workflow_descriptor_requires_agent_policy_and_tools_consistency() -> None:
    workflow = WorkflowIR.create(
        name="single",
        nodes=(AgentNode(id="one", agent_revision="coder:1", input="work"),),
        edges=(),
    )
    execution = ExecutionDescriptor.create(
        agent=AgentSpec(name="coder", model="openai/test"),
        messages=({"role": "user", "content": "work"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    )
    with pytest.raises(ValidationError, match="policy"):
        WorkflowExecutionDescriptor.create(
            workflow=workflow,
            agents=(WorkflowAgentDescriptor.create("coder:1", execution),),
            tools=(),
            policy=ExecutionPolicyDescriptor.create(permission_default="deny"),
        )


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
    with pytest.raises(ValidationError, match="agent"):
        RunSnapshot.model_validate(
            {
                **legacy.model_dump(mode="json"),
                "agent_revision": "other:1",
                "execution_compatibility": "current",
                "execution_descriptor": descriptor.model_dump(mode="json"),
            }
        )
    with pytest.raises(ValidationError, match="input|message"):
        RunSnapshot.model_validate(
            {
                **legacy.model_dump(mode="json"),
                "user_input": "different",
                "execution_compatibility": "current",
                "execution_descriptor": descriptor.model_dump(mode="json"),
            }
        )
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

    execution = ExecutionDescriptor.create(
        agent=AgentSpec(name="coder", model="openai/test"),
        messages=({"role": "user", "content": "work"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    descriptor = WorkflowExecutionDescriptor.create(
        workflow=workflow,
        agents=(WorkflowAgentDescriptor.create("coder:1", execution),),
        tools=(),
        policy=execution.policy,
    )
    current = WorkflowRunSnapshot.model_validate(
        {
            **legacy.model_dump(mode="json"),
            "execution_compatibility": "current",
            "execution_descriptor": descriptor.model_dump(mode="json"),
        }
    )
    assert current.execution_descriptor == descriptor

    different_workflow = WorkflowIR.create(
        name="different",
        nodes=workflow.nodes,
        edges=workflow.edges,
    )
    with pytest.raises(ValidationError, match="workflow"):
        WorkflowRunSnapshot.model_validate(
            {
                **legacy.model_dump(mode="json"),
                "workflow": different_workflow.model_dump(mode="json"),
                "execution_compatibility": "current",
                "execution_descriptor": descriptor.model_dump(mode="json"),
            }
        )


def test_workflow_snapshots_model_copy_revalidates_invariants() -> None:
    workflow = WorkflowIR.create(
        name="single",
        nodes=(AgentNode(id="one", agent_revision="coder:1", input="work"),),
        edges=(),
    )
    pending = WorkflowNodeSnapshot(
        entity_id="wf_1:one",
        workflow_run_id="wf_1",
        session_id="ses_1",
        node_id="one",
        status=WorkflowNodeStatus.PENDING,
    )
    with pytest.raises(ValidationError, match="running|version"):
        pending.model_copy(update={"status": "running", "version": 1})

    running = pending.model_copy(
        update={"status": "running", "version": 2, "run_id": "run_1"}
    )
    completed = running.model_copy(
        update={
            "status": "completed",
            "version": 3,
            "output_text": "done",
            "usage": TokenUsage(total_tokens=1),
        }
    )
    with pytest.raises(ValidationError, match="completed"):
        completed.model_copy(update={"output_text": None})

    execution = ExecutionDescriptor.create(
        agent=AgentSpec(name="coder", model="openai/test"),
        messages=({"role": "user", "content": "work"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    descriptor = WorkflowExecutionDescriptor.create(
        workflow=workflow,
        agents=(WorkflowAgentDescriptor.create("coder:1", execution),),
        tools=(),
        policy=execution.policy,
    )
    current = WorkflowRunSnapshot(
        workflow_run_id="wf_1",
        session_id="ses_1",
        status=WorkflowRunStatus.RUNNING,
        workflow=workflow,
        nodes=(pending,),
        execution_compatibility="current",
        execution_descriptor=descriptor,
    )
    with pytest.raises(ValidationError, match="compatibility"):
        current.model_copy(update={"execution_descriptor": None})
    with pytest.raises(ValidationError, match="completed"):
        current.model_copy(update={"status": "completed", "version": 2})
