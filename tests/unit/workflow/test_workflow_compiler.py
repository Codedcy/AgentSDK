from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from agent_sdk.subagents import ChildResult, TaskEnvelope
from agent_sdk.workflow import (
    AgentNode,
    WorkflowCompiler,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowFailure,
    WorkflowIR,
    WorkflowNodeSnapshot,
    WorkflowNodeStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)


WORKFLOW_DATA = {
    "api_version": "agent-sdk/v1",
    "kind": "Workflow",
    "name": "parent-child",
    "nodes": [
        {
            "id": "plan",
            "kind": "agent",
            "agent_revision": "planner:1",
            "input": "plan",
        },
        {
            "id": "child",
            "kind": "agent",
            "agent_revision": "worker:1",
            "input": "verify",
            "run_as": "child",
            "success_criteria": ["return a verification result"],
            "evidence_refs": ["artifact:plan"],
        },
    ],
    "edges": [{"source": "plan", "target": "child"}],
}

WORKFLOW_YAML = """
api_version: agent-sdk/v1
kind: Workflow
name: parent-child
nodes:
  - id: plan
    kind: agent
    agent_revision: planner:1
    input: plan
  - id: child
    kind: agent
    agent_revision: worker:1
    input: verify
    run_as: child
    success_criteria:
      - return a verification result
    evidence_refs:
      - artifact:plan
edges:
  - source: plan
    target: child
"""


def test_python_and_yaml_compile_to_same_frozen_canonical_ir() -> None:
    source = json.loads(json.dumps(WORKFLOW_DATA))
    definition = WorkflowDefinition.model_validate(source)

    source["nodes"][0]["input"] = "mutated"
    ir_from_python = WorkflowCompiler().compile(definition)
    ir_from_yaml = WorkflowCompiler().compile_yaml(WORKFLOW_YAML)

    assert ir_from_python.canonical_json() == ir_from_yaml.canonical_json()
    assert ir_from_python.canonical_bytes() == ir_from_yaml.canonical_bytes()
    assert tuple(node.id for node in ir_from_python.nodes) == ("plan", "child")
    assert ir_from_python.nodes[0].input == "plan"
    with pytest.raises(ValidationError):
        ir_from_python.nodes[0].input = "changed"  # type: ignore[misc]

    encoded = json.loads(ir_from_python.canonical_json())
    definition_hash = encoded.pop("definition_hash")
    expected = hashlib.sha256(
        json.dumps(encoded, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert definition_hash == expected


def test_canonical_hash_is_independent_of_mapping_insertion_order() -> None:
    reordered = {
        "nodes": WORKFLOW_DATA["nodes"],
        "name": "parent-child",
        "kind": "Workflow",
        "edges": WORKFLOW_DATA["edges"],
        "api_version": "agent-sdk/v1",
    }
    compiler = WorkflowCompiler()

    assert compiler.compile_yaml(WORKFLOW_YAML).definition_hash == compiler.compile(
        WorkflowDefinition.model_validate(reordered)
    ).definition_hash


@pytest.mark.parametrize(
    "update",
    [
        {"extra": True},
        {"api_version": "agent-sdk/v2"},
        {"kind": "Other"},
    ],
)
def test_definition_rejects_extra_or_unsupported_top_level_fields(
    update: dict[str, object],
) -> None:
    candidate = dict(WORKFLOW_DATA)
    candidate.update(update)

    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(candidate)


@pytest.mark.parametrize(
    "nodes,edges",
    [
        ([], []),
        ([WORKFLOW_DATA["nodes"][0], WORKFLOW_DATA["nodes"][0]], []),
        (WORKFLOW_DATA["nodes"], [{"source": "missing", "target": "child"}]),
        ([WORKFLOW_DATA["nodes"][0]], [{"source": "plan", "target": "plan"}]),
        (
            WORKFLOW_DATA["nodes"],
            [
                {"source": "plan", "target": "child"},
                {"source": "child", "target": "plan"},
            ],
        ),
        (WORKFLOW_DATA["nodes"], []),
        (
            [
                WORKFLOW_DATA["nodes"][0],
                WORKFLOW_DATA["nodes"][1],
                {
                    "id": "other",
                    "kind": "agent",
                    "agent_revision": "worker:1",
                    "input": "other",
                },
            ],
            [
                {"source": "plan", "target": "child"},
                {"source": "plan", "target": "other"},
            ],
        ),
    ],
)
def test_compile_rejects_non_sequential_graphs(
    nodes: list[object],
    edges: list[object],
) -> None:
    candidate = dict(WORKFLOW_DATA)
    candidate["nodes"] = nodes
    candidate["edges"] = edges

    with pytest.raises((ValidationError, ValueError)):
        WorkflowCompiler().compile(WorkflowDefinition.model_validate(candidate))


def test_compile_rejects_root_child() -> None:
    candidate = json.loads(json.dumps(WORKFLOW_DATA))
    candidate["nodes"][0]["run_as"] = "child"

    with pytest.raises(ValueError, match="root"):
        WorkflowCompiler().compile(WorkflowDefinition.model_validate(candidate))


@pytest.mark.parametrize(
    "document",
    [
        WORKFLOW_YAML + "\n---\n{}",
        "value: !unsafe tag",
        "value: &shared [1]\nother: *shared",
        "value: !!str hello",
    ],
)
def test_yaml_rejects_documents_tags_and_aliases(document: str) -> None:
    with pytest.raises(ValueError) as raised:
        WorkflowCompiler().compile_yaml(document)

    assert document not in str(raised.value)


def test_yaml_rejects_size_depth_and_item_limits_without_echoing_input() -> None:
    compiler = WorkflowCompiler(max_yaml_bytes=256, max_depth=4, max_items=8)
    oversized = "secret-value: " + "x" * 300
    deeply_nested = "a:\n  b:\n    c:\n      d:\n        e: value"
    too_many = "items:\n" + "".join(f"  - {index}\n" for index in range(12))

    for document in (oversized, deeply_nested, too_many):
        with pytest.raises(ValueError) as raised:
            compiler.compile_yaml(document)
        assert document not in str(raised.value)


def test_node_rejects_unknown_kinds_conditions_and_extra_fields() -> None:
    for field, value in (("kind", "parallel"), ("condition", "always"), ("extra", 1)):
        candidate = json.loads(json.dumps(WORKFLOW_DATA))
        candidate["nodes"][0][field] = value
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate(candidate)


def test_child_and_failure_models_are_recursively_detached_and_frozen() -> None:
    criteria = ["verify"]
    envelope = TaskEnvelope(objective="work", success_criteria=criteria)
    criteria.append("mutated")
    failure = WorkflowFailure(code="internal", message="safe", retryable=False)
    snapshot = WorkflowNodeSnapshot(
        entity_id="wfr_1:node",
        workflow_run_id="wfr_1",
        session_id="ses_1",
        node_id="node",
        status=WorkflowNodeStatus.FAILED,
        version=3,
        run_id="run_1",
        error=failure,
    )
    child = ChildResult(
        run_id="run_1",
        status="completed",
        output_text="done",
        evidence_refs=["artifact:1"],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )

    assert envelope.success_criteria == ("verify",)
    assert child.evidence_refs == ("artifact:1",)
    with pytest.raises(ValidationError):
        snapshot.error.message = "raw"  # type: ignore[union-attr,misc]


def _agent_node(node_id: str, *, run_as: str = "parent") -> AgentNode:
    return AgentNode.model_validate(
        {
            "id": node_id,
            "kind": "agent",
            "agent_revision": f"{node_id}:1",
            "input": node_id,
            "run_as": run_as,
        }
    )


@pytest.mark.parametrize(
    "nodes,edges",
    [
        (
            (_agent_node("root"), _agent_node("left"), _agent_node("right")),
            (
                WorkflowEdge(source="root", target="left"),
                WorkflowEdge(source="root", target="right"),
            ),
        ),
        (
            (_agent_node("root"), _agent_node("child")),
            (
                WorkflowEdge(source="root", target="child"),
                WorkflowEdge(source="child", target="root"),
            ),
        ),
        (
            (_agent_node("child", run_as="child"),),
            (),
        ),
        (
            (_agent_node("same"), _agent_node("same")),
            (WorkflowEdge(source="same", target="same"),),
        ),
        (
            (_agent_node("root"), _agent_node("middle"), _agent_node("leaf")),
            (
                WorkflowEdge(source="middle", target="leaf"),
                WorkflowEdge(source="root", target="middle"),
            ),
        ),
    ],
)
def test_workflow_ir_create_rejects_hashable_noncanonical_graphs(
    nodes: tuple[AgentNode, ...],
    edges: tuple[WorkflowEdge, ...],
) -> None:
    with pytest.raises(ValidationError):
        WorkflowIR.create(name="unsafe", nodes=nodes, edges=edges)


def test_workflow_ir_model_validate_rejects_self_hashed_branching_graph() -> None:
    nodes = (_agent_node("root"), _agent_node("left"), _agent_node("right"))
    edges = (
        WorkflowEdge(source="root", target="left"),
        WorkflowEdge(source="root", target="right"),
    )
    content = {
        "schema_version": 1,
        "name": "unsafe",
        "nodes": [node.model_dump(mode="json") for node in nodes],
        "edges": [edge.model_dump(mode="json") for edge in edges],
    }
    payload = {
        **content,
        "definition_hash": hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }

    with pytest.raises(ValidationError):
        WorkflowIR.model_validate(payload)


def _pending_workflow_snapshot() -> WorkflowRunSnapshot:
    workflow = WorkflowCompiler().compile(
        WorkflowDefinition.model_validate(WORKFLOW_DATA)
    )
    workflow_run_id = "wfr_owner"
    session_id = "ses_owner"
    nodes = tuple(
        WorkflowNodeSnapshot(
            entity_id=f"{workflow_run_id}:{node.id}",
            workflow_run_id=workflow_run_id,
            session_id=session_id,
            node_id=node.id,
            status=WorkflowNodeStatus.PENDING,
        )
        for node in workflow.nodes
    )
    return WorkflowRunSnapshot(
        workflow_run_id=workflow_run_id,
        session_id=session_id,
        status=WorkflowRunStatus.RUNNING,
        workflow=workflow,
        nodes=nodes,
    )


def test_workflow_snapshot_rejects_misaligned_owned_nodes_and_illegal_status_shape() -> None:
    valid = _pending_workflow_snapshot().model_dump(mode="json")
    corruptions: list[dict[str, object]] = []

    missing = json.loads(json.dumps(valid))
    missing["nodes"] = missing["nodes"][:-1]
    corruptions.append(missing)

    for field, value in (
        ("workflow_run_id", "wfr_foreign"),
        ("session_id", "ses_foreign"),
        ("node_id", "wrong"),
        ("entity_id", "wfr_owner:wrong"),
    ):
        corrupted = json.loads(json.dumps(valid))
        corrupted["nodes"][0][field] = value
        corruptions.append(corrupted)

    reordered = json.loads(json.dumps(valid))
    reordered["nodes"] = list(reversed(reordered["nodes"]))
    corruptions.append(reordered)

    illegal_prefix = json.loads(json.dumps(valid))
    illegal_prefix["nodes"][1].update(
        {
            "status": "completed",
            "run_id": "run_child",
            "output_text": "done",
            "usage": {},
            "version": 3,
        }
    )
    corruptions.append(illegal_prefix)

    invalid_terminal = json.loads(json.dumps(valid))
    invalid_terminal["status"] = "completed"
    invalid_terminal["output_text"] = "done"
    invalid_terminal["usage"] = {}
    corruptions.append(invalid_terminal)

    for corrupted in corruptions:
        with pytest.raises(ValidationError):
            WorkflowRunSnapshot.model_validate(corrupted)
