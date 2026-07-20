from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from agent_sdk.runtime.execution import DurableWorkflowIR
from agent_sdk.workflow.compiler import WorkflowCompiler
from agent_sdk.workflow.models import (
    AgentNode,
    WorkflowDefinition,
    WorkflowIR,
    WorkflowInstruction,
)


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


def _hash_content(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _rehash(payload: dict[str, object]) -> dict[str, object]:
    if payload.get("schema_version") == 2:
        content = {
            key: payload[key]
            for key in (
                "schema_version",
                "name",
                "inputs",
                "nodes",
                "instructions",
            )
        }
    else:
        content = {
            key: value
            for key, value in payload.items()
            if key != "definition_hash"
        }
    payload["definition_hash"] = _hash_content(content)
    return payload


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


@pytest.mark.parametrize("coercive_value", (True, 1.0, "2"))
def test_loop_iteration_limit_rejects_coercive_integers(
    coercive_value: object,
) -> None:
    candidate = json.loads(json.dumps(CONTROL_DATA))
    candidate["steps"][1]["max_iterations"] = coercive_value

    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(candidate)


@pytest.mark.parametrize("coercive_value", (True, 1.0, "2"))
@pytest.mark.parametrize(
    ("field", "instruction"),
    (
        (
            "true_pc",
            {
                "id": "branch",
                "op": "branch",
                "expression": {"path": "inputs.ready", "op": "exists"},
                "true_pc": 1,
                "false_pc": 2,
            },
        ),
        (
            "false_pc",
            {
                "id": "branch",
                "op": "branch",
                "expression": {"path": "inputs.ready", "op": "exists"},
                "true_pc": 1,
                "false_pc": 2,
            },
        ),
        (
            "target_pc",
            {"id": "jump", "op": "jump", "target_pc": 1},
        ),
        (
            "max_iterations",
            {
                "id": "loop",
                "op": "loop_check",
                "expression": {"path": "outputs.done", "op": "exists"},
                "true_pc": 3,
                "false_pc": 1,
                "loop_id": "loop",
                "max_iterations": 2,
            },
        ),
    ),
)
def test_instruction_control_numbers_reject_coercive_integers(
    field: str,
    instruction: dict[str, object],
    coercive_value: object,
) -> None:
    candidate = dict(instruction)
    candidate[field] = coercive_value

    with pytest.raises(ValidationError):
        WorkflowInstruction.model_validate(candidate)


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


def test_omitted_inputs_are_deeply_frozen_across_public_round_trips() -> None:
    definition = WorkflowDefinition.model_validate(
        {
            "api_version": "agent-sdk/v1",
            "kind": "Workflow",
            "name": "omitted",
            "nodes": [
                {
                    "id": "work",
                    "kind": "agent",
                    "agent_revision": "writer@1",
                    "input": "work",
                }
            ],
        }
    )
    schema_v1 = WorkflowIR.create(
        name="persisted",
        nodes=(
            AgentNode(
                id="work",
                agent_revision="writer@1",
                input="work",
            ),
        ),
        edges=(),
    )
    schema_v2 = WorkflowCompiler().compile(definition)
    restored_v1 = WorkflowIR.model_validate(schema_v1.model_dump(mode="json"))
    restored_v2 = WorkflowIR.model_validate(schema_v2.model_dump(mode="json"))

    for inputs in (
        definition.inputs,
        schema_v1.inputs,
        restored_v1.inputs,
        schema_v2.inputs,
        restored_v2.inputs,
    ):
        with pytest.raises(TypeError):
            inputs["tampered"] = "yes"  # type: ignore[index]

    assert schema_v1.definition_hash == restored_v1.definition_hash
    assert schema_v2.definition_hash == restored_v2.definition_hash


def _insert_early_complete(instructions: list[dict[str, object]]) -> None:
    early = dict(instructions[-1])
    early["id"] = "early-complete"
    instructions.insert(0, early)


def _make_branch_self_target(instructions: list[dict[str, object]]) -> None:
    instructions[0]["true_pc"] = 0


def _make_loop_back_edge_target_agent(
    instructions: list[dict[str, object]],
) -> None:
    instructions[6]["target_pc"] = 5


def _insert_orphan_jump(instructions: list[dict[str, object]]) -> None:
    orphan = dict(instructions[2])
    orphan["id"] = "orphan"
    orphan["target_pc"] = len(instructions)
    instructions.insert(len(instructions) - 1, orphan)


@pytest.mark.parametrize(
    ("corrupt", "message"),
    (
        (_insert_early_complete, "one final complete"),
        (_make_branch_self_target, "target itself"),
        (_make_loop_back_edge_target_agent, "back-edge"),
        (_insert_orphan_jump, "orphan"),
    ),
)
def test_rehashed_noncanonical_v2_program_is_rejected_public_and_durable(
    corrupt: Callable[[list[dict[str, object]]], None],
    message: str,
) -> None:
    valid = WorkflowCompiler().compile(
        WorkflowDefinition.model_validate(CONTROL_DATA)
    )
    payload = json.loads(json.dumps(valid.model_dump(mode="json")))
    corrupt(payload["instructions"])
    _rehash(payload)

    with pytest.raises(ValidationError, match=message):
        WorkflowIR.model_validate(payload)
    with pytest.raises(ValidationError, match=message):
        DurableWorkflowIR.model_validate(payload)
