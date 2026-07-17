from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_sdk.api import AgentSDK
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.runtime.models import RunResult, TokenUsage
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.workflow.compiler import WorkflowCompiler
from agent_sdk.workflow.models import (
    WorkflowControlState,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNodeSnapshot,
    WorkflowNodeStatus,
    WorkflowRunSnapshot,
)
from agent_sdk.workflow.program import CompleteWorkflow, next_action
from agent_sdk.workflow.program import PersistControl
from agent_sdk.workflow.state import WorkflowState


async def _provider(**_: Any) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        if False:
            yield {}

    return chunks()


def _workflow():
    return WorkflowCompiler().compile(
        WorkflowDefinition.model_validate(
            {
                "api_version": "agent-sdk/v1",
                "kind": "Workflow",
                "name": "control-state",
                "inputs": {"enabled": True},
                "steps": [
                    {
                        "id": "choose",
                        "kind": "condition",
                        "when": {
                            "path": "inputs.enabled",
                            "op": "eq",
                            "value": True,
                        },
                        "then_steps": [
                            {
                                "id": "selected",
                                "kind": "agent",
                                "agent_revision": "worker@1",
                                "input": "selected",
                            }
                        ],
                        "else_steps": [
                            {
                                "id": "unselected",
                                "kind": "agent",
                                "agent_revision": "worker@1",
                                "input": "unselected",
                            }
                        ],
                    }
                ],
            }
        )
    )


@pytest.mark.asyncio
async def test_schema_v2_create_and_control_advance_are_atomic() -> None:
    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=_provider)
    session = await sdk.sessions.create(workspaces=[])
    state = WorkflowState(store)
    created = (await state.create(session.session_id, _workflow())).value
    assert created.control == WorkflowControlState()
    node_before = {
        node.entity_id: await store.get_snapshot("workflow_node", node.entity_id)
        for node in created.nodes
    }
    updated_control = WorkflowControlState(
        program_counter=1,
        revision=2,
        selected_branches={"choose": "then"},
    )

    updated = await state.advance_control(
        created,
        updated_control,
        event_type="workflow.condition.selected",
        event_payload={
            "condition_id": "choose",
            "branch": "then",
            "program_counter": 1,
        },
    )

    assert updated.control == updated_control
    assert updated.version == 2
    assert await state.load(created.workflow_run_id) == updated
    assert {
        node.entity_id: await store.get_snapshot("workflow_node", node.entity_id)
        for node in created.nodes
    } == node_before
    events = await store.read_events(after_cursor=0, session_id=session.session_id)
    assert events[-1].event.type == "workflow.condition.selected"
    assert events[-1].event.sequence == updated.version

    with pytest.raises(AgentSDKError) as conflict:
        await state.advance_control(
            created,
            updated_control,
            event_type="workflow.condition.selected",
            event_payload={
                "condition_id": "choose",
                "branch": "then",
                "program_counter": 1,
            },
        )
    assert conflict.value.code is ErrorCode.CONFLICT
    assert conflict.value.retryable is True
    assert await state.load(created.workflow_run_id) == updated
    await sdk.close()


def test_control_state_is_deeply_immutable_and_json_bounded() -> None:
    source = {
        "selected_branches": {"choose": "then"},
        "loop_iterations": {"retry": 1},
        "node_execution_counts": {"node": 1},
        "outputs": {"node": {"items": [1, 2]}},
    }
    control = WorkflowControlState.model_validate(source)
    source["selected_branches"]["choose"] = "else"
    source["loop_iterations"]["retry"] = 99
    source["node_execution_counts"]["node"] = 99
    source["outputs"]["node"]["items"].append(3)

    assert control.selected_branches == {"choose": "then"}
    assert control.loop_iterations == {"retry": 1}
    assert control.node_execution_counts == {"node": 1}
    assert control.outputs == {"node": {"items": (1, 2)}}
    with pytest.raises(TypeError):
        control.outputs["node"] = {"changed": True}  # type: ignore[index]
    with pytest.raises(TypeError):
        control.node_execution_counts["node"] = 2  # type: ignore[index]
    with pytest.raises(ValidationError):
        control.model_copy(update={"program_counter": -1})

    for invalid in (
        {"program_counter": -1},
        {"program_counter": True},
        {"revision": 0},
        {"revision": True},
        {"selected_branches": {"": "then"}},
        {"loop_iterations": {"loop": -1}},
        {"loop_iterations": {"loop": True}},
        {"loop_iterations": {"": 1}},
        {"node_execution_counts": {"node": -1}},
        {"node_execution_counts": {"node": True}},
        {"node_execution_counts": {"": 1}},
        {"outputs": {"": 1}},
        {"outputs": {"node": float("nan")}},
    ):
        with pytest.raises(ValidationError):
            WorkflowControlState.model_validate(invalid)


def test_schema_v1_preserves_none_control_and_prefix_version_rules() -> None:
    legacy = WorkflowCompiler().compile(
        WorkflowDefinition.model_validate(
            {
                "api_version": "agent-sdk/v1",
                "kind": "Workflow",
                "name": "legacy",
                "nodes": [
                    {
                        "id": "one",
                        "kind": "agent",
                        "agent_revision": "worker@1",
                        "input": "one",
                    },
                    {
                        "id": "two",
                        "kind": "agent",
                        "agent_revision": "worker@1",
                        "input": "two",
                    },
                ],
                "edges": [{"source": "one", "target": "two"}],
            }
        )
    )
    # Compiler promotes legacy definitions to v2; reconstruct the persisted v1 IR.
    graph = legacy.create(
        name="legacy",
        nodes=legacy.nodes,
        edges=(WorkflowEdge(source="one", target="two"),),
    )
    nodes = tuple(
        WorkflowNodeSnapshot(
            entity_id=f"wfr_legacy:{node.id}",
            workflow_run_id="wfr_legacy",
            session_id="ses_legacy",
            node_id=node.id,
            status=WorkflowNodeStatus.PENDING,
        )
        for node in graph.nodes
    )
    snapshot = WorkflowRunSnapshot(
        workflow_run_id="wfr_legacy",
        session_id="ses_legacy",
        status="running",
        workflow=graph,
        nodes=nodes,
    )
    dumped = snapshot.model_dump(mode="json")

    assert snapshot.control is None
    assert "control" not in dumped
    with pytest.raises(ValidationError):
        snapshot.model_copy(update={"control": WorkflowControlState()})

    invalid = snapshot.model_dump(mode="json")
    invalid["nodes"][1].update(
        {
            "status": "completed",
            "version": 3,
            "run_id": "run_two",
            "output_text": "two",
            "usage": {},
        }
    )
    invalid["version"] = 3
    with pytest.raises(ValidationError):
        WorkflowRunSnapshot.model_validate(invalid)


def test_schema_v2_validates_control_references_and_allows_unselected_pending() -> None:
    workflow = _workflow()
    pending_nodes = tuple(
        WorkflowNodeSnapshot(
            entity_id=f"wfr_v2:{node.id}",
            workflow_run_id="wfr_v2",
            session_id="ses_v2",
            node_id=node.id,
            status=WorkflowNodeStatus.PENDING,
        )
        for node in workflow.nodes
    )
    nodes = (
        pending_nodes[0],
        pending_nodes[1].model_copy(
            update={
                "status": WorkflowNodeStatus.COMPLETED,
                "version": 3,
                "run_id": "run_unselected",
                "output_text": "no",
                "usage": TokenUsage(
                    prompt_tokens=1,
                    completion_tokens=1,
                    total_tokens=2,
                ),
            }
        ),
    )
    valid = WorkflowRunSnapshot(
        workflow_run_id="wfr_v2",
        session_id="ses_v2",
        status="running",
        workflow=workflow,
        nodes=nodes,
        control=WorkflowControlState(
            program_counter=4,
            revision=2,
            selected_branches={"choose": "else"},
            outputs={"unselected": {"text": "no"}},
        ),
        version=4,
    )
    assert valid.nodes[0].status is WorkflowNodeStatus.PENDING
    assert valid.nodes[1].status is WorkflowNodeStatus.COMPLETED

    invalid_controls = (
        WorkflowControlState(program_counter=len(workflow.instructions)),
        WorkflowControlState(selected_branches={"missing": "then"}),
        WorkflowControlState(loop_iterations={"missing": 1}),
        WorkflowControlState(
            program_counter=4,
            revision=2,
            selected_branches={"choose": "else"},
            outputs={"selected": {"ok": True}},
        ),
    )
    for control in invalid_controls:
        payload = valid.model_dump(mode="json")
        payload["control"] = control.model_dump(mode="json")
        payload["version"] = control.revision + 2
        with pytest.raises(ValidationError):
            WorkflowRunSnapshot.model_validate(payload)

    corrupted_marker = valid.model_dump(mode="json")
    corrupted_marker["control"]["last_output_node_id"] = "selected"
    with pytest.raises(ValidationError):
        WorkflowRunSnapshot.model_validate(corrupted_marker)

    future_consumption = valid.model_dump(mode="json")
    future_consumption["nodes"][1]["execution_count"] = 1
    future_consumption["control"]["node_execution_counts"] = {
        "unselected": 2
    }
    with pytest.raises(ValidationError):
        WorkflowRunSnapshot.model_validate(future_consumption)


@pytest.mark.asyncio
async def test_last_output_marker_survives_sqlite_roundtrip(
    tmp_path: Path,
) -> None:
    workflow = WorkflowCompiler().compile(
        WorkflowDefinition.model_validate(
            {
                "api_version": "agent-sdk/v1",
                "kind": "Workflow",
                "name": "sqlite-output-order",
                "steps": [
                    {
                        "id": "repeat",
                        "kind": "loop",
                        "until": {
                            "path": "outputs.a",
                            "op": "exists",
                        },
                        "max_iterations": 2,
                        "body": [
                            {
                                "id": "choose",
                                "kind": "condition",
                                "when": {"path": "outputs.b", "op": "exists"},
                                "then_steps": [
                                    {
                                        "id": "a",
                                        "kind": "agent",
                                        "agent_revision": "worker@1",
                                        "input": "a",
                                    }
                                ],
                                "else_steps": [
                                    {
                                        "id": "b",
                                        "kind": "agent",
                                        "agent_revision": "worker@1",
                                        "input": "b",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    )
    database = tmp_path / "workflow-output-order.db"
    store = await SQLiteStore.open(database)
    sdk = AgentSDK.for_test(store=store, acompletion=_provider)
    session = await sdk.sessions.create(workspaces=[])
    state = WorkflowState(store)
    snapshot = (await state.create(session.session_id, workflow)).value
    snapshot = await state.start_node(snapshot, 1, "run_b")
    snapshot = await state.complete_node(
        snapshot,
        1,
        RunResult(
            run_id="run_b",
            output_text="B-first",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        ),
    )
    snapshot = await state.start_node(snapshot, 0, "run_a")
    snapshot = await state.complete_node(
        snapshot,
        0,
        RunResult(
            run_id="run_a",
            output_text="A-last",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        ),
    )
    completed = {node.node_id: node for node in snapshot.nodes}
    merged_b = next_action(
        workflow,
        snapshot.control.model_copy(update={"program_counter": 4}),
        completed_nodes=completed,
    )
    assert isinstance(merged_b, PersistControl)
    snapshot = await state.advance_control(
        snapshot,
        merged_b.control,
        event_type=merged_b.event_type,
        event_payload=merged_b.event_payload,
    )
    snapshot = await state.advance_control(
        snapshot,
        snapshot.control.model_copy(
            update={
                "program_counter": 2,
                "revision": snapshot.control.revision + 1,
            }
        ),
        event_type="workflow.condition.selected",
        event_payload={"condition_id": "choose", "branch": "then"},
    )
    merged_a = next_action(
        workflow,
        snapshot.control,
        completed_nodes=completed,
    )
    assert isinstance(merged_a, PersistControl)
    snapshot = await state.advance_control(
        snapshot,
        merged_a.control,
        event_type=merged_a.event_type,
        event_payload=merged_a.event_payload,
    )
    while snapshot.control.program_counter != len(workflow.instructions) - 1:
        action = next_action(
            workflow,
            snapshot.control,
            completed_nodes=completed,
        )
        assert isinstance(action, PersistControl)
        snapshot = await state.advance_control(
            snapshot,
            action.control,
            event_type=action.event_type,
            event_payload=action.event_payload,
        )

    await sdk.close()
    await store.close()

    reopened = await SQLiteStore.open(database)
    try:
        restored = await WorkflowState(reopened).load(snapshot.workflow_run_id)
        assert restored.control is not None
        assert restored.control.last_output_node_id == "a"
        action = next_action(
            workflow,
            restored.control,
            completed_nodes={node.node_id: node for node in restored.nodes},
        )
        assert action == CompleteWorkflow(output_text="A-last")
    finally:
        await reopened.close()
