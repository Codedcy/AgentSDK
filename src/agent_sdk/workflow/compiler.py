from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.events import AliasEvent, DocumentStartEvent, NodeEvent

from agent_sdk.workflow.models import (
    AgentNode,
    ConditionStep,
    InstructionOp,
    LoopStep,
    WorkflowDefinition,
    WorkflowExpression,
    WorkflowIR,
    WorkflowInstruction,
    WorkflowStep,
)


@dataclass
class _InstructionDraft:
    id: str
    op: InstructionOp
    agent_node_id: str | None = None
    expression: WorkflowExpression | None = None
    true_label: str | None = None
    false_label: str | None = None
    target_label: str | None = None
    loop_id: str | None = None
    max_iterations: int | None = None


class _ProgramBuilder:
    def __init__(self, reserved_ids: set[str]) -> None:
        self._labels: dict[str, int] = {}
        self._drafts: list[_InstructionDraft] = []
        self._nodes: list[AgentNode] = []
        self._instruction_ids: set[str] = set()
        self._reserved_ids = reserved_ids

    @property
    def nodes(self) -> tuple[AgentNode, ...]:
        return tuple(self._nodes)

    def mark(self, label: str) -> None:
        if label in self._labels:
            raise ValueError("workflow compiler label must be unique")
        self._labels[label] = len(self._drafts)

    def emit(self, instruction: _InstructionDraft) -> None:
        if instruction.id in self._instruction_ids:
            raise ValueError("workflow instruction ids must be unique")
        self._instruction_ids.add(instruction.id)
        self._drafts.append(instruction)

    def _synthetic_id(self, preferred: str) -> str:
        candidate = preferred
        suffix = 2
        while (
            candidate in self._instruction_ids
            or candidate in self._reserved_ids
        ):
            candidate = f"{preferred}:{suffix}"
            suffix += 1
        return candidate

    def compile_steps(
        self,
        steps: tuple[WorkflowStep, ...],
        depth: int,
    ) -> None:
        for step in steps:
            if isinstance(step, AgentNode):
                self._nodes.append(step)
                self.emit(
                    _InstructionDraft(
                        id=step.id,
                        op="agent",
                        agent_node_id=step.id,
                    )
                )
                continue
            if isinstance(step, ConditionStep):
                then_label = f"{step.id}:then"
                else_label = f"{step.id}:else"
                join_label = f"{step.id}:join"
                self.emit(
                    _InstructionDraft(
                        id=step.id,
                        op="branch",
                        expression=step.when,
                        true_label=then_label,
                        false_label=else_label,
                    )
                )
                self.mark(then_label)
                self.compile_steps(step.then_steps, depth + 1)
                self.emit(
                    _InstructionDraft(
                        id=self._synthetic_id(f"{step.id}:then:join"),
                        op="jump",
                        target_label=join_label,
                    )
                )
                self.mark(else_label)
                self.compile_steps(step.else_steps, depth + 1)
                self.mark(join_label)
                continue
            if isinstance(step, LoopStep):
                check_label = f"{step.id}:check"
                body_label = f"{step.id}:body"
                exit_label = f"{step.id}:exit"
                self.mark(check_label)
                self.emit(
                    _InstructionDraft(
                        id=step.id,
                        op="loop_check",
                        expression=step.until,
                        true_label=exit_label,
                        false_label=body_label,
                        loop_id=step.id,
                        max_iterations=step.max_iterations,
                    )
                )
                self.mark(body_label)
                self.compile_steps(step.body, depth + 1)
                self.emit(
                    _InstructionDraft(
                        id=self._synthetic_id(f"{step.id}:body:back"),
                        op="jump",
                        target_label=check_label,
                    )
                )
                self.mark(exit_label)
                continue
            raise ValueError("unsupported workflow step")

    def emit_complete(self) -> None:
        self.emit(
            _InstructionDraft(
                id=self._synthetic_id("complete"),
                op="complete",
            )
        )

    def freeze(self) -> tuple[WorkflowInstruction, ...]:
        def resolve(label: str | None) -> int | None:
            if label is None:
                return None
            try:
                return self._labels[label]
            except KeyError as error:
                raise ValueError("workflow compiler target label is missing") from error

        instructions = tuple(
            WorkflowInstruction(
                id=draft.id,
                op=draft.op,
                agent_node_id=draft.agent_node_id,
                expression=draft.expression,
                true_pc=resolve(draft.true_label),
                false_pc=resolve(draft.false_label),
                target_pc=resolve(draft.target_label),
                loop_id=draft.loop_id,
                max_iterations=draft.max_iterations,
            )
            for draft in self._drafts
        )
        instruction_count = len(instructions)
        if any(
            target >= instruction_count
            for instruction in instructions
            for target in (
                instruction.true_pc,
                instruction.false_pc,
                instruction.target_pc,
            )
            if target is not None
        ):
            raise ValueError("workflow compiler target is out of range")
        return instructions


class WorkflowCompiler:
    def __init__(
        self,
        *,
        max_yaml_bytes: int = 64 * 1024,
        max_depth: int = 16,
        max_items: int = 512,
        max_control_depth: int = 8,
        max_loop_iterations: int = 100,
    ) -> None:
        if min(
            max_yaml_bytes,
            max_depth,
            max_items,
            max_control_depth,
            max_loop_iterations,
        ) < 1:
            raise ValueError("workflow compiler limits must be positive")
        self._max_yaml_bytes = max_yaml_bytes
        self._max_depth = max_depth
        self._max_items = max_items
        self._max_control_depth = max_control_depth
        self._max_loop_iterations = max_loop_iterations

    def compile_yaml(self, document: str) -> WorkflowIR:
        if not isinstance(document, str):
            raise ValueError("workflow YAML must be text")
        if len(document.encode("utf-8")) > self._max_yaml_bytes:
            raise ValueError("workflow YAML exceeds size limit")
        self._validate_yaml_syntax(document)
        try:
            decoded = yaml.safe_load(document)
        except yaml.YAMLError as error:
            raise ValueError("workflow YAML is invalid") from error
        self._validate_value_bounds(decoded)
        if not isinstance(decoded, dict):
            raise ValueError("workflow YAML root must be an object")
        try:
            definition = WorkflowDefinition.model_validate(decoded)
        except ValidationError as error:
            raise ValueError("workflow definition is invalid") from error
        return self.compile(definition)

    def compile(self, definition: WorkflowDefinition) -> WorkflowIR:
        if definition.steps:
            steps = definition.steps
        else:
            steps = self._normalize_legacy_chain(definition)
        step_ids: set[str] = set()
        self._validate_steps(steps, depth=1, seen=step_ids)
        builder = _ProgramBuilder(step_ids)
        builder.compile_steps(steps, depth=1)
        builder.emit_complete()
        return WorkflowIR.create_program(
            name=definition.name,
            inputs=definition.inputs,
            nodes=builder.nodes,
            instructions=builder.freeze(),
        )

    @staticmethod
    def _normalize_legacy_chain(
        definition: WorkflowDefinition,
    ) -> tuple[WorkflowStep, ...]:
        nodes = definition.nodes
        by_id = {node.id: node for node in nodes}
        if len(by_id) != len(nodes):
            raise ValueError("workflow node ids must be unique")

        incoming: Counter[str] = Counter()
        outgoing: dict[str, str] = {}
        for edge in definition.edges:
            if edge.source not in by_id or edge.target not in by_id:
                raise ValueError("workflow edge endpoint does not exist")
            if edge.source == edge.target:
                raise ValueError("workflow self edges are not supported")
            incoming[edge.target] += 1
            if incoming[edge.target] > 1 or edge.source in outgoing:
                raise ValueError("workflow must be a sequential chain")
            outgoing[edge.source] = edge.target

        roots = [node.id for node in nodes if incoming[node.id] == 0]
        if len(roots) != 1:
            raise ValueError("workflow must have exactly one root")
        if len(definition.edges) != len(nodes) - 1:
            raise ValueError("workflow must be connected")

        ordered: list[AgentNode] = []
        current: str | None = roots[0]
        visited: set[str] = set()
        while current is not None:
            if current in visited:
                raise ValueError("workflow cycles are not supported")
            visited.add(current)
            ordered.append(by_id[current])
            current = outgoing.get(current)
        if len(ordered) != len(nodes):
            raise ValueError("workflow must be a connected acyclic chain")
        if ordered[0].run_as == "child":
            raise ValueError("workflow root cannot be a child")

        return tuple(ordered)

    def _validate_steps(
        self,
        steps: tuple[WorkflowStep, ...],
        *,
        depth: int,
        seen: set[str],
    ) -> None:
        for step in steps:
            if step.id in seen:
                raise ValueError("workflow step ids must be unique across nesting")
            seen.add(step.id)
            if isinstance(step, AgentNode):
                continue
            if depth > self._max_control_depth:
                raise ValueError("workflow exceeds control depth limit")
            if isinstance(step, ConditionStep):
                self._validate_steps(
                    step.then_steps,
                    depth=depth + 1,
                    seen=seen,
                )
                self._validate_steps(
                    step.else_steps,
                    depth=depth + 1,
                    seen=seen,
                )
                continue
            if isinstance(step, LoopStep):
                if step.max_iterations > self._max_loop_iterations:
                    raise ValueError("workflow exceeds loop iteration limit")
                self._validate_steps(
                    step.body,
                    depth=depth + 1,
                    seen=seen,
                )
                continue
            raise ValueError("unsupported workflow step")

    @staticmethod
    def _validate_yaml_syntax(document: str) -> None:
        try:
            events = tuple(yaml.parse(document, Loader=yaml.SafeLoader))
        except yaml.YAMLError as error:
            raise ValueError("workflow YAML is invalid") from error
        if sum(isinstance(event, DocumentStartEvent) for event in events) != 1:
            raise ValueError("workflow YAML must contain exactly one document")
        for event in events:
            if isinstance(event, AliasEvent):
                raise ValueError("workflow YAML aliases are not supported")
            if isinstance(event, NodeEvent) and (
                event.anchor is not None or getattr(event, "tag", None) is not None
            ):
                raise ValueError("workflow YAML tags and anchors are not supported")

    def _validate_value_bounds(self, value: Any) -> None:
        item_count = 0

        def visit(item: Any, depth: int) -> None:
            nonlocal item_count
            if depth > self._max_depth:
                raise ValueError("workflow YAML exceeds depth limit")
            if isinstance(item, dict):
                item_count += len(item)
                if item_count > self._max_items:
                    raise ValueError("workflow YAML exceeds item limit")
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("workflow YAML object keys must be strings")
                    visit(child, depth + 1)
            elif isinstance(item, list):
                item_count += len(item)
                if item_count > self._max_items:
                    raise ValueError("workflow YAML exceeds item limit")
                for child in item:
                    visit(child, depth + 1)

        visit(value, 1)
