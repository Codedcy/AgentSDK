from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_sdk.workflow.compiler import WorkflowCompiler
from agent_sdk.workflow.models import WorkflowDefinition


CONTROL_DATA: dict[str, object] = {
    "api_version": "agent-sdk/v1",
    "kind": "Workflow",
    "name": "refine",
    "inputs": {"threshold": 2},
    "steps": [
        {
            "id": "choose",
            "kind": "condition",
            "when": {"path": "inputs.threshold", "op": "gte", "value": 2},
            "then_steps": [
                {
                    "id": "draft",
                    "kind": "agent",
                    "agent_revision": "writer@1",
                    "input": "draft",
                }
            ],
            "else_steps": [
                {
                    "id": "reject",
                    "kind": "agent",
                    "agent_revision": "writer@1",
                    "input": "reject",
                }
            ],
        },
        {
            "id": "improve",
            "kind": "loop",
            "until": {
                "path": "outputs.review.done",
                "op": "eq",
                "value": True,
            },
            "max_iterations": 3,
            "body": [
                {
                    "id": "review",
                    "kind": "agent",
                    "agent_revision": "reviewer@1",
                    "input": "review",
                }
            ],
        },
        {
            "id": "finish",
            "kind": "agent",
            "agent_revision": "writer@1",
            "input": "finish",
        },
    ],
}

CONTROL_YAML = """
api_version: agent-sdk/v1
kind: Workflow
name: refine
inputs:
  threshold: 2
steps:
  - id: choose
    kind: condition
    when: {path: inputs.threshold, op: gte, value: 2}
    then_steps:
      - {id: draft, kind: agent, agent_revision: writer@1, input: draft}
    else_steps:
      - {id: reject, kind: agent, agent_revision: writer@1, input: reject}
  - id: improve
    kind: loop
    until: {path: outputs.review.done, op: eq, value: true}
    max_iterations: 3
    body:
      - {id: review, kind: agent, agent_revision: reviewer@1, input: review}
  - {id: finish, kind: agent, agent_revision: writer@1, input: finish}
"""


def test_nested_control_compiles_to_exact_stable_program() -> None:
    ir = WorkflowCompiler().compile(
        WorkflowDefinition.model_validate(CONTROL_DATA)
    )

    assert ir.schema_version == 2
    assert tuple(node.id for node in ir.nodes) == (
        "draft",
        "reject",
        "review",
        "finish",
    )
    assert tuple(
        (
            instruction.id,
            instruction.op,
            instruction.agent_node_id,
            instruction.true_pc,
            instruction.false_pc,
            instruction.target_pc,
            instruction.loop_id,
            instruction.max_iterations,
        )
        for instruction in ir.instructions
    ) == (
        ("choose", "branch", None, 1, 3, None, None, None),
        ("draft", "agent", "draft", None, None, None, None, None),
        ("choose:then:join", "jump", None, None, None, 4, None, None),
        ("reject", "agent", "reject", None, None, None, None, None),
        ("improve", "loop_check", None, 7, 5, None, "improve", 3),
        ("review", "agent", "review", None, None, None, None, None),
        ("improve:body:back", "jump", None, None, None, 4, None, None),
        ("finish", "agent", "finish", None, None, None, None, None),
        ("complete", "complete", None, None, None, None, None, None),
    )
    assert ir.edges == ()


def test_yaml_json_and_python_models_have_identical_program_hashes() -> None:
    compiler = WorkflowCompiler()
    source = json.loads(json.dumps(CONTROL_DATA))
    definition = WorkflowDefinition.model_validate(source)
    source["inputs"]["threshold"] = 99
    from_python = compiler.compile(definition)
    from_yaml = compiler.compile_yaml(CONTROL_YAML)
    from_json = compiler.compile_yaml(json.dumps(CONTROL_DATA))

    assert from_python.inputs["threshold"] == 2
    assert from_python.canonical_bytes() == from_yaml.canonical_bytes()
    assert from_python.canonical_bytes() == from_json.canonical_bytes()
    assert from_python.definition_hash == from_yaml.definition_hash
    assert from_python.definition_hash == from_json.definition_hash
    with pytest.raises(TypeError):
        from_python.inputs["threshold"] = 99  # type: ignore[index]


def test_legacy_chain_is_promoted_to_v2_sequential_program() -> None:
    definition = WorkflowDefinition.model_validate(
        {
            "api_version": "agent-sdk/v1",
            "kind": "Workflow",
            "name": "legacy",
            "nodes": [
                {
                    "id": "first",
                    "kind": "agent",
                    "agent_revision": "writer@1",
                    "input": "first",
                },
                {
                    "id": "second",
                    "kind": "agent",
                    "agent_revision": "writer@1",
                    "input": "second",
                },
            ],
            "edges": [{"source": "first", "target": "second"}],
        }
    )

    ir = WorkflowCompiler().compile(definition)

    assert ir.schema_version == 2
    assert ir.edges == ()
    assert tuple(node.id for node in ir.nodes) == ("first", "second")
    assert tuple(
        (instruction.id, instruction.op, instruction.agent_node_id)
        for instruction in ir.instructions
    ) == (
        ("first", "agent", "first"),
        ("second", "agent", "second"),
        ("complete", "complete", None),
    )


def test_duplicate_ids_are_rejected_across_nested_steps() -> None:
    candidate = json.loads(json.dumps(CONTROL_DATA))
    candidate["steps"][1]["body"][0]["id"] = "draft"

    with pytest.raises(ValueError, match="unique"):
        WorkflowCompiler().compile(WorkflowDefinition.model_validate(candidate))


@pytest.mark.parametrize(
    "candidate",
    [
        {
            "api_version": "agent-sdk/v1",
            "kind": "Workflow",
            "name": "empty-condition",
            "steps": [
                {
                    "id": "choose",
                    "kind": "condition",
                    "when": {"path": "inputs.value", "op": "exists"},
                    "then_steps": [],
                }
            ],
        },
        {
            "api_version": "agent-sdk/v1",
            "kind": "Workflow",
            "name": "empty-loop",
            "steps": [
                {
                    "id": "repeat",
                    "kind": "loop",
                    "until": {"path": "outputs.done", "op": "exists"},
                    "max_iterations": 1,
                    "body": [],
                }
            ],
        },
    ],
)
def test_control_bodies_must_not_be_empty(candidate: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(candidate)


def test_control_depth_is_statically_bounded() -> None:
    candidate = {
        "api_version": "agent-sdk/v1",
        "kind": "Workflow",
        "name": "deep",
        "steps": [
            {
                "id": "outer",
                "kind": "condition",
                "when": {"path": "inputs.enabled", "op": "exists"},
                "then_steps": [
                    {
                        "id": "inner",
                        "kind": "condition",
                        "when": {"path": "inputs.enabled", "op": "exists"},
                        "then_steps": [
                            {
                                "id": "work",
                                "kind": "agent",
                                "agent_revision": "writer@1",
                                "input": "work",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    with pytest.raises(ValueError, match="control depth"):
        WorkflowCompiler(max_control_depth=1).compile(
            WorkflowDefinition.model_validate(candidate)
        )


def test_loop_iteration_limit_is_statically_bounded() -> None:
    with pytest.raises(ValueError, match="loop iteration"):
        WorkflowCompiler(max_loop_iterations=2).compile(
            WorkflowDefinition.model_validate(CONTROL_DATA)
        )


def test_unknown_operator_and_arbitrary_yaml_tags_are_rejected() -> None:
    candidate = json.loads(json.dumps(CONTROL_DATA))
    candidate["steps"][0]["when"]["op"] = "evaluate"
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(candidate)

    with pytest.raises(ValueError):
        WorkflowCompiler().compile_yaml("value: !python/object unsafe")


def test_definition_requires_exactly_one_definition_shape() -> None:
    both = json.loads(json.dumps(CONTROL_DATA))
    both["nodes"] = [
        {
            "id": "legacy",
            "kind": "agent",
            "agent_revision": "writer@1",
            "input": "legacy",
        }
    ]
    neither = {
        "api_version": "agent-sdk/v1",
        "kind": "Workflow",
        "name": "empty",
    }

    for candidate in (both, neither):
        with pytest.raises(ValidationError, match="exactly one"):
            WorkflowDefinition.model_validate(candidate)
