from __future__ import annotations

from collections import Counter
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.events import AliasEvent, DocumentStartEvent, NodeEvent

from agent_sdk.workflow.models import (
    AgentNode,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowIR,
)


class WorkflowCompiler:
    def __init__(
        self,
        *,
        max_yaml_bytes: int = 64 * 1024,
        max_depth: int = 16,
        max_items: int = 512,
    ) -> None:
        if min(max_yaml_bytes, max_depth, max_items) < 1:
            raise ValueError("workflow YAML limits must be positive")
        self._max_yaml_bytes = max_yaml_bytes
        self._max_depth = max_depth
        self._max_items = max_items

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
        nodes = definition.nodes
        if not nodes:
            raise ValueError("workflow must contain at least one node")
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

        normalized_edges = tuple(
            WorkflowEdge(source=left.id, target=right.id)
            for left, right in zip(ordered, ordered[1:])
        )
        return WorkflowIR.create(
            name=definition.name,
            nodes=tuple(ordered),
            edges=normalized_edges,
        )

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
