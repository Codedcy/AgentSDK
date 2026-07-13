from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from agent_sdk.subagents import ChildResult, TaskEnvelope
from agent_sdk.workflow import (
    WorkflowCompiler,
    WorkflowDefinition,
    WorkflowFailure,
    WorkflowNodeSnapshot,
    WorkflowNodeStatus,
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
