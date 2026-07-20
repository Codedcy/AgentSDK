from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from agent_sdk.tools.models import freeze_json, thaw_json
from agent_sdk.workflow.expressions import WorkflowExpressionError, evaluate_expression
from agent_sdk.workflow.models import (
    AgentNode,
    JsonValue,
    WorkflowControlState,
    WorkflowFailure,
    WorkflowExpression,
    WorkflowIR,
    WorkflowNodeSnapshot,
    WorkflowNodeStatus,
)


@dataclass(frozen=True)
class ExecuteAgent:
    node: AgentNode


@dataclass(frozen=True)
class PersistControl:
    control: WorkflowControlState
    event_type: str
    event_payload: Mapping[str, JsonValue]


@dataclass(frozen=True)
class CompleteWorkflow:
    output_text: str


@dataclass(frozen=True)
class FailWorkflow:
    failure: WorkflowFailure


type ProgramAction = (
    ExecuteAgent | PersistControl | CompleteWorkflow | FailWorkflow
)


def next_action(
    workflow: WorkflowIR,
    control: WorkflowControlState,
    *,
    completed_nodes: Mapping[str, WorkflowNodeSnapshot],
) -> ProgramAction:
    if workflow.schema_version != 2:
        raise ValueError("workflow program reducer requires schema version 2")
    if control.program_counter >= len(workflow.instructions):
        raise ValueError("workflow program counter is out of range")

    instruction = workflow.instructions[control.program_counter]
    if instruction.op == "agent":
        node_id = cast(str, instruction.agent_node_id)
        node = next(node for node in workflow.nodes if node.id == node_id)
        completed = completed_nodes.get(node_id)
        if completed is None:
            return ExecuteAgent(node=node)
        if completed.status is not WorkflowNodeStatus.COMPLETED:
            raise ValueError("completed node map contains a non-completed node")
        consumed = control.node_execution_counts.get(node_id, 0)
        if completed.execution_count > 0 and completed.execution_count == consumed:
            return ExecuteAgent(node=node)
        if (
            completed.execution_count > 0
            and completed.execution_count != consumed + 1
        ):
            raise ValueError("workflow node execution count is not reducible")
        output = _parse_node_output(cast(str, completed.output_text))
        outputs = dict(control.outputs)
        first_merge = node_id not in outputs
        outputs[node_id] = output
        execution_counts = dict(control.node_execution_counts)
        if completed.execution_count > 0:
            execution_counts[node_id] = completed.execution_count
        updated = _updated_control(
            control,
            program_counter=control.program_counter + 1,
            outputs=outputs,
            node_execution_counts=execution_counts,
            last_output_node_id=(
                node_id
                if completed.execution_count > 0 or first_merge
                else control.last_output_node_id
            ),
            last_output_run_id=(
                cast(str, completed.run_id)
                if completed.execution_count > 0
                else control.last_output_run_id
            ),
            last_output_node_execution=(
                completed.execution_count
                if completed.execution_count > 0
                else control.last_output_node_execution
            ),
        )
        payload: dict[str, JsonValue] = {
            "node_id": node_id,
            "output": output,
            "program_counter": updated.program_counter,
        }
        if completed.execution_count > 0:
            payload["execution_index"] = completed.execution_count
        return PersistControl(
            control=updated,
            event_type="workflow.node.output.recorded",
            event_payload=payload,
        )

    if instruction.op == "branch":
        selected = _evaluate(
            cast(WorkflowExpression, instruction.expression),
            workflow,
            control,
        )
        if isinstance(selected, FailWorkflow):
            return selected
        branch: Literal["then", "else"] = "then" if selected else "else"
        target = instruction.true_pc if selected else instruction.false_pc
        assert target is not None
        selected_branches = dict(control.selected_branches)
        selected_branches[instruction.id] = branch
        updated = _updated_control(
            control,
            program_counter=target,
            selected_branches=selected_branches,
        )
        return PersistControl(
            control=updated,
            event_type="workflow.condition.selected",
            event_payload={
                "condition_id": instruction.id,
                "branch": branch,
                "program_counter": target,
            },
        )

    if instruction.op == "loop_check":
        should_exit = _evaluate(
            cast(WorkflowExpression, instruction.expression),
            workflow,
            control,
        )
        if isinstance(should_exit, FailWorkflow):
            return should_exit
        loop_id = cast(str, instruction.loop_id)
        if should_exit:
            target = cast(int, instruction.true_pc)
            updated = _updated_control(control, program_counter=target)
            return PersistControl(
                control=updated,
                event_type="workflow.loop.exited",
                event_payload={
                    "loop_id": loop_id,
                    "iterations": control.loop_iterations.get(loop_id, 0),
                    "program_counter": target,
                },
            )
        iteration = control.loop_iterations.get(loop_id, 0)
        limit = cast(int, instruction.max_iterations)
        if iteration >= limit:
            return FailWorkflow(
                failure=WorkflowFailure(
                    code="workflow_loop_limit",
                    message=(
                        f"workflow loop '{loop_id}' reached its iteration limit"
                    ),
                    retryable=False,
                )
            )
        iteration += 1
        loop_iterations = dict(control.loop_iterations)
        loop_iterations[loop_id] = iteration
        target = cast(int, instruction.false_pc)
        updated = _updated_control(
            control,
            program_counter=target,
            loop_iterations=loop_iterations,
        )
        return PersistControl(
            control=updated,
            event_type="workflow.loop.iteration",
            event_payload={
                "loop_id": loop_id,
                "iteration": iteration,
                "program_counter": target,
            },
        )

    if instruction.op == "jump":
        target = cast(int, instruction.target_pc)
        updated = _updated_control(control, program_counter=target)
        return PersistControl(
            control=updated,
            event_type="workflow.control.jumped",
            event_payload={
                "instruction_id": instruction.id,
                "program_counter": target,
            },
        )

    if instruction.op == "complete":
        if control.last_output_node_id is not None:
            completed = completed_nodes.get(control.last_output_node_id)
            if (
                completed is None
                or completed.status is not WorkflowNodeStatus.COMPLETED
            ):
                raise ValueError(
                    "workflow last output node is not completed"
                )
            return CompleteWorkflow(output_text=cast(str, completed.output_text))
        for node in reversed(workflow.nodes):
            completed = completed_nodes.get(node.id)
            if (
                completed is not None
                and completed.status is WorkflowNodeStatus.COMPLETED
            ):
                return CompleteWorkflow(output_text=cast(str, completed.output_text))
        return CompleteWorkflow(
            output_text=json.dumps(
                thaw_json(workflow.inputs),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    raise ValueError("workflow instruction is unsupported")


def _evaluate(
    expression: WorkflowExpression,
    workflow: WorkflowIR,
    control: WorkflowControlState,
) -> bool | FailWorkflow:
    try:
        return evaluate_expression(
            expression,
            {"inputs": workflow.inputs, "outputs": control.outputs},
        )
    except WorkflowExpressionError as error:
        return FailWorkflow(
            failure=WorkflowFailure(
                code="workflow_expression_error",
                message=str(error),
                retryable=False,
            )
        )


def _updated_control(
    current: WorkflowControlState,
    *,
    program_counter: int,
    selected_branches: Mapping[str, str] | None = None,
    loop_iterations: Mapping[str, int] | None = None,
    outputs: Mapping[str, JsonValue] | None = None,
    node_execution_counts: Mapping[str, int] | None = None,
    last_output_node_id: str | None = None,
    last_output_run_id: str | None = None,
    last_output_node_execution: int | None = None,
) -> WorkflowControlState:
    return WorkflowControlState.model_validate(
        {
            "program_counter": program_counter,
            "revision": current.revision + 1,
            "selected_branches": (
                current.selected_branches
                if selected_branches is None
                else selected_branches
            ),
            "loop_iterations": (
                current.loop_iterations
                if loop_iterations is None
                else loop_iterations
            ),
            "outputs": current.outputs if outputs is None else outputs,
            "node_execution_counts": (
                current.node_execution_counts
                if node_execution_counts is None
                else node_execution_counts
            ),
            "last_output_node_id": (
                current.last_output_node_id
                if last_output_node_id is None
                else last_output_node_id
            ),
            "last_output_run_id": (
                current.last_output_run_id
                if last_output_run_id is None
                else last_output_run_id
            ),
            "last_output_node_execution": (
                current.last_output_node_execution
                if last_output_node_execution is None
                else last_output_node_execution
            ),
        }
    )

def _parse_node_output(output_text: str) -> JsonValue:
    try:
        decoded = json.loads(output_text)
        return cast(JsonValue, freeze_json(decoded))
    except (TypeError, ValueError):
        return cast(JsonValue, freeze_json({"text": output_text}))
