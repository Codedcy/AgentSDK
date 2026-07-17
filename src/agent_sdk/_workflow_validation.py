from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class WorkflowProgramInstruction(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def op(self) -> str: ...

    @property
    def agent_node_id(self) -> str | None: ...

    @property
    def true_pc(self) -> int | None: ...

    @property
    def false_pc(self) -> int | None: ...

    @property
    def target_pc(self) -> int | None: ...

    @property
    def loop_id(self) -> str | None: ...


def validate_canonical_workflow_program(
    node_ids: tuple[str, ...],
    instructions: Sequence[WorkflowProgramInstruction],
) -> None:
    if not node_ids:
        raise ValueError("workflow IR must contain at least one node")
    if len(set(node_ids)) != len(node_ids):
        raise ValueError("workflow IR node ids must be unique")
    if not instructions:
        raise ValueError("schema-v2 workflow program must not be empty")

    instruction_ids = tuple(instruction.id for instruction in instructions)
    if len(set(instruction_ids)) != len(instruction_ids):
        raise ValueError("workflow instruction ids must be unique")
    complete_pcs = tuple(
        pc for pc, instruction in enumerate(instructions)
        if instruction.op == "complete"
    )
    if complete_pcs != (len(instructions) - 1,):
        raise ValueError(
            "schema-v2 workflow program must have one final complete"
        )

    agent_ids = tuple(
        instruction.agent_node_id
        for instruction in instructions
        if instruction.op == "agent"
    )
    if agent_ids != node_ids:
        raise ValueError(
            "workflow program must reference each agent node exactly once"
        )

    instruction_count = len(instructions)
    for pc, instruction in enumerate(instructions):
        for target in (
            instruction.true_pc,
            instruction.false_pc,
            instruction.target_pc,
        ):
            if target is not None and (
                target < 0 or target >= instruction_count
            ):
                raise ValueError("workflow instruction target is out of range")
            if target == pc:
                raise ValueError("workflow instruction cannot target itself")

    def parse_region(start: int, end: int) -> None:
        pc = start
        while pc < end:
            instruction = instructions[pc]
            if instruction.op == "agent":
                pc += 1
                continue
            if instruction.op == "branch":
                true_pc = instruction.true_pc
                false_pc = instruction.false_pc
                if (
                    true_pc != pc + 1
                    or false_pc is None
                    or false_pc <= true_pc
                    or false_pc > end
                ):
                    raise ValueError("workflow branch targets are not canonical")
                then_jump_pc = false_pc - 1
                if then_jump_pc <= true_pc:
                    raise ValueError("workflow branch then body must not be empty")
                then_jump = instructions[then_jump_pc]
                join_pc = then_jump.target_pc
                if (
                    then_jump.op != "jump"
                    or join_pc is None
                    or join_pc < false_pc
                    or join_pc > end
                ):
                    raise ValueError("workflow branch join is not canonical")
                parse_region(true_pc, then_jump_pc)
                parse_region(false_pc, join_pc)
                pc = join_pc
                continue
            if instruction.op == "loop_check":
                true_pc = instruction.true_pc
                false_pc = instruction.false_pc
                if (
                    instruction.loop_id != instruction.id
                    or false_pc != pc + 1
                    or true_pc is None
                    or true_pc <= false_pc
                    or true_pc > end
                ):
                    raise ValueError("workflow loop targets are not canonical")
                back_jump_pc = true_pc - 1
                if back_jump_pc <= false_pc:
                    raise ValueError("workflow loop body must not be empty")
                back_jump = instructions[back_jump_pc]
                if back_jump.op != "jump" or back_jump.target_pc != pc:
                    raise ValueError("workflow loop back-edge is not canonical")
                parse_region(false_pc, back_jump_pc)
                pc = true_pc
                continue
            raise ValueError("workflow program contains an orphan instruction")

    parse_region(0, instruction_count - 1)
