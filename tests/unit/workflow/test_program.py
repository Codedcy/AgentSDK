from __future__ import annotations

import json
from collections.abc import Mapping

from agent_sdk.runtime.models import TokenUsage
from agent_sdk.workflow.compiler import WorkflowCompiler
from agent_sdk.workflow.models import (
    WorkflowControlState,
    WorkflowDefinition,
    WorkflowFailure,
    WorkflowNodeSnapshot,
    WorkflowNodeStatus,
)
from agent_sdk.workflow.program import (
    CompleteWorkflow,
    ExecuteAgent,
    FailWorkflow,
    PersistControl,
    next_action,
)


def _program():
    return WorkflowCompiler().compile(
        WorkflowDefinition.model_validate(
            {
                "api_version": "agent-sdk/v1",
                "kind": "Workflow",
                "name": "refine",
                "inputs": {"threshold": 2},
                "steps": [
                    {
                        "id": "choose",
                        "kind": "condition",
                        "when": {
                            "path": "inputs.threshold",
                            "op": "gte",
                            "value": 2,
                        },
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
        )
    )


def _completed(node_id: str, output_text: str) -> WorkflowNodeSnapshot:
    return WorkflowNodeSnapshot(
        entity_id=f"wfr_test:{node_id}",
        workflow_run_id="wfr_test",
        session_id="ses_test",
        node_id=node_id,
        status=WorkflowNodeStatus.COMPLETED,
        version=3,
        run_id=f"run_{node_id}",
        output_text=output_text,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def test_branch_decision_is_recorded_and_advances() -> None:
    ir = _program()

    action = next_action(ir, WorkflowControlState(), completed_nodes={})

    assert action == PersistControl(
        control=WorkflowControlState(
            program_counter=1,
            revision=2,
            selected_branches={"choose": "then"},
            loop_iterations={},
            outputs={},
        ),
        event_type="workflow.condition.selected",
        event_payload={
            "condition_id": "choose",
            "branch": "then",
            "program_counter": 1,
        },
    )


def test_agent_instruction_dispatches_pending_node() -> None:
    ir = _program()
    control = WorkflowControlState(program_counter=1)

    action = next_action(ir, control, completed_nodes={})

    assert action == ExecuteAgent(node=ir.nodes[0])


def test_false_branch_is_recorded_and_advances_to_else() -> None:
    definition = WorkflowDefinition.model_validate(
        {
            "api_version": "agent-sdk/v1",
            "kind": "Workflow",
            "name": "false-branch",
            "inputs": {"enabled": False},
            "steps": [
                {
                    "id": "choose",
                    "kind": "condition",
                    "when": {"path": "inputs.enabled", "op": "eq", "value": True},
                    "then_steps": [
                        {
                            "id": "yes",
                            "kind": "agent",
                            "agent_revision": "worker@1",
                            "input": "yes",
                        }
                    ],
                    "else_steps": [
                        {
                            "id": "no",
                            "kind": "agent",
                            "agent_revision": "worker@1",
                            "input": "no",
                        }
                    ],
                }
            ],
        }
    )
    ir = WorkflowCompiler().compile(definition)

    action = next_action(ir, WorkflowControlState(), completed_nodes={})

    assert isinstance(action, PersistControl)
    assert action.control.program_counter == 3
    assert action.control.selected_branches == {"choose": "else"}
    assert action.event_payload["branch"] == "else"


def test_completed_agent_is_never_dispatched_and_parses_json_output() -> None:
    ir = _program()
    completed = _completed("draft", '{"score": 3, "items": ["a"]}')

    action = next_action(
        ir,
        WorkflowControlState(program_counter=1, revision=2),
        completed_nodes={"draft": completed},
    )

    assert isinstance(action, PersistControl)
    assert action.event_type == "workflow.node.output.recorded"
    assert action.control.program_counter == 2
    assert action.control.revision == 3
    assert action.control.outputs == {
        "draft": {"score": 3, "items": ("a",)}
    }


def test_completed_agent_uses_text_output_fallback() -> None:
    ir = _program()
    completed = _completed("reject", "not-json")

    action = next_action(
        ir,
        WorkflowControlState(program_counter=3),
        completed_nodes={"reject": completed},
    )

    assert isinstance(action, PersistControl)
    assert action.control.outputs == {"reject": {"text": "not-json"}}
    assert action.event_payload["output"] == {"text": "not-json"}


def test_jump_advances_to_target() -> None:
    ir = _program()

    action = next_action(
        ir,
        WorkflowControlState(program_counter=2),
        completed_nodes={},
    )

    assert action == PersistControl(
        control=WorkflowControlState(program_counter=4, revision=2),
        event_type="workflow.control.jumped",
        event_payload={
            "instruction_id": "choose:then:join",
            "program_counter": 4,
        },
    )


def test_loop_false_increments_iteration_and_enters_body() -> None:
    ir = _program()
    control = WorkflowControlState(
        program_counter=4,
        revision=5,
        outputs={"review": {"done": False}},
    )

    action = next_action(ir, control, completed_nodes={})

    assert action == PersistControl(
        control=WorkflowControlState(
            program_counter=5,
            revision=6,
            loop_iterations={"improve": 1},
            outputs={"review": {"done": False}},
        ),
        event_type="workflow.loop.iteration",
        event_payload={
            "loop_id": "improve",
            "iteration": 1,
            "program_counter": 5,
        },
    )


def test_loop_true_exits_without_incrementing() -> None:
    ir = _program()
    control = WorkflowControlState(
        program_counter=4,
        revision=5,
        loop_iterations={"improve": 2},
        outputs={"review": {"done": True}},
    )

    action = next_action(ir, control, completed_nodes={})

    assert isinstance(action, PersistControl)
    assert action.control.program_counter == 7
    assert action.control.loop_iterations == {"improve": 2}
    assert action.event_type == "workflow.loop.exited"


def test_loop_limit_returns_terminal_failure() -> None:
    ir = _program()
    control = WorkflowControlState(
        program_counter=4,
        revision=5,
        loop_iterations={"improve": 3},
        outputs={"review": {"done": False}},
    )

    action = next_action(ir, control, completed_nodes={})

    assert isinstance(action, FailWorkflow)
    assert action.failure == WorkflowFailure(
        code="workflow_loop_limit",
        message="workflow loop 'improve' reached its iteration limit",
        retryable=False,
    )


def test_complete_returns_last_completed_agent_output() -> None:
    ir = _program()
    completed = {
        "draft": _completed("draft", "drafted"),
        "finish": _completed("finish", "finished"),
    }

    action = next_action(
        ir,
        WorkflowControlState(program_counter=8),
        completed_nodes=completed,
    )

    assert action == CompleteWorkflow(output_text="finished")


def test_complete_without_agent_uses_canonical_inputs() -> None:
    ir = _program()

    action = next_action(
        ir,
        WorkflowControlState(program_counter=8),
        completed_nodes={},
    )

    assert action == CompleteWorkflow(
        output_text=json.dumps(
            {"threshold": 2},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def test_reducer_does_not_mutate_inputs_or_perform_hidden_io() -> None:
    ir = _program()
    control = WorkflowControlState()
    before = control.model_dump(mode="json")

    first = next_action(ir, control, completed_nodes={})
    second = next_action(ir, control, completed_nodes={})

    assert first == second
    assert control.model_dump(mode="json") == before
    assert isinstance(control.outputs, Mapping)
