from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_sdk import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.observability import (
    ObservedEvent,
    TraceStageKind,
    TraceStageStatus,
    project_stages,
)


BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _event(
    cursor: int,
    event_type: str,
    payload: dict[str, object],
    *,
    run_id: str = "run_1",
    seconds: int | None = None,
    schema_version: int = 1,
) -> ObservedEvent:
    return ObservedEvent(
        cursor=cursor,
        event=EventEnvelope(
            event_id=f"evt_{cursor}",
            type=event_type,
            session_id="ses_1",
            run_id=run_id,
            sequence=cursor,
            schema_version=schema_version,
            payload=payload,
            occurred_at=BASE + timedelta(seconds=seconds or 0),
        ),
    )


def test_interrupted_run_and_child_can_reach_a_later_terminal_status() -> None:
    stages = project_stages(
        (
            _event(1, "run.started", {"run_id": "run_1"}),
            _event(2, "run.interrupted", {"run_id": "run_1"}),
            _event(3, "run.failed", {"run_id": "run_1"}),
            _event(4, "child.created", {"child_run_id": "run_child"}),
            _event(5, "child.interrupted", {"child_run_id": "run_child"}),
            _event(6, "child.failed", {"child_run_id": "run_child"}),
        )
    )

    assert {
        (stage.kind, stage.entity_id, stage.status) for stage in stages
    } >= {
        (TraceStageKind.RUN, "run_1", TraceStageStatus.FAILED),
        (TraceStageKind.CHILD, "run_child", TraceStageStatus.FAILED),
    }


def test_projects_supported_events_by_stable_ids_in_first_evidence_order() -> None:
    events = (
        _event(1, "run.started", {"run_id": "run_1"}),
        _event(2, "step.started", {"step_id": "step_1"}, seconds=1),
        _event(
            3,
            "model.call.started",
            {
                "operation_id": "op_1",
                "step_id": "step_1",
                "context_view_id": "view_1",
                "prompt_manifest_id": "prompt_1",
            },
            seconds=2,
        ),
        _event(
            4,
            "context.view.created",
            {"view_id": "view_1", "capsule_id": "capsule_1"},
            seconds=3,
        ),
        _event(
            5,
            "model.usage.reported",
            {
                "operation_id": "op_1",
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "cost_usd": 0.25,
            },
            seconds=4,
        ),
        _event(
            6,
            "model.call.completed",
            {"operation_id": "op_1", "step_id": "step_1"},
            seconds=5,
        ),
        _event(
            7,
            "tool.call.started",
            {"call_id": "call_1", "step_id": "step_1"},
            seconds=6,
        ),
        _event(
            8,
            "permission.requested",
            {"request_id": "perm_1", "call_id": "call_1"},
            seconds=7,
        ),
        _event(
            9,
            "permission.resolved",
            {"request_id": "perm_1", "call_id": "call_1", "allowed": False},
            seconds=8,
        ),
        _event(
            10,
            "tool.call.timed_out",
            {"call_id": "call_1", "step_id": "step_1"},
            seconds=9,
        ),
        _event(11, "step.completed", {"step_id": "step_1"}, seconds=10),
        _event(
            12,
            "agent.message.sent",
            {"message_id": "msg_1", "sender_run_id": "run_1"},
            seconds=11,
        ),
        _event(
            13,
            "evaluation.completed",
            {"evaluation_id": "evl_1", "subject_run_id": "run_1"},
            run_id="evl_1",
            seconds=12,
        ),
        _event(
            14,
            "tool.recovery.retry.started",
            {"operation": "hashed_operation_1"},
            seconds=13,
        ),
        _event(
            15,
            "run.completed",
            {
                "run_id": "run_1",
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                    "cost_usd": 0.25,
                },
                "output_text": "must-not-be-copied",
            },
            seconds=14,
        ),
        _event(
            16,
            "workflow.started",
            {"workflow_run_id": "wfr_1"},
            run_id="wfr_1",
            seconds=15,
        ),
        _event(
            17,
            "workflow.node.started",
            {"workflow_run_id": "wfr_1", "node_id": "node_1"},
            run_id="wfr_1",
            seconds=16,
        ),
        _event(
            18,
            "workflow.node.failed",
            {
                "workflow_run_id": "wfr_1",
                "node_id": "node_1",
                "error": {"message": "must-not-be-copied"},
            },
            run_id="wfr_1",
            seconds=17,
        ),
        _event(
            19,
            "workflow.failed",
            {
                "workflow_run_id": "wfr_1",
                "error": {"message": "must-not-be-copied"},
            },
            run_id="wfr_1",
            seconds=18,
        ),
        _event(
            20,
            "child.created",
            {"child_run_id": "run_child", "parent_run_id": "run_1"},
            seconds=19,
        ),
        _event(
            21,
            "child.interrupted",
            {"child_run_id": "run_child", "parent_run_id": "run_1"},
            seconds=20,
        ),
        _event(22, "unknown.future.event", {"secret": "ignored"}, seconds=21),
    )

    stages = project_stages(events)

    assert [stage.first_cursor for stage in stages] == sorted(
        stage.first_cursor for stage in stages
    )
    assert all(stage.kind is not None for stage in stages)
    assert all("must-not-be-copied" not in stage.model_dump_json() for stage in stages)
    assert not any("unknown.future.event" in stage.model_dump_json() for stage in stages)

    by_kind = {stage.kind: stage for stage in stages}
    assert by_kind[TraceStageKind.RUN].status is TraceStageStatus.COMPLETED
    assert by_kind[TraceStageKind.RUN].usage is not None
    assert by_kind[TraceStageKind.RUN].usage.cost_usd == 0.25
    assert by_kind[TraceStageKind.MODEL].duration_ms == 3000
    assert by_kind[TraceStageKind.MODEL].usage is not None
    assert by_kind[TraceStageKind.MODEL].usage.total_tokens == 5
    assert (
        by_kind[TraceStageKind.CONTEXT].parent_stage_id
        == by_kind[TraceStageKind.MODEL].stage_id
    )
    assert by_kind[TraceStageKind.PERMISSION].status is TraceStageStatus.DENIED
    assert by_kind[TraceStageKind.TOOL].status is TraceStageStatus.TIMED_OUT
    assert by_kind[TraceStageKind.WORKFLOW_NODE].status is TraceStageStatus.FAILED
    assert by_kind[TraceStageKind.CHILD].status is TraceStageStatus.INTERRUPTED

    ids = {stage.stage_id for stage in stages}
    assert all(stage.parent_stage_id is None or stage.parent_stage_id in ids for stage in stages)
    assert all(stage.duration_ms is None or stage.duration_ms >= 0 for stage in stages)


def test_terminal_without_start_and_running_start_remain_truthful() -> None:
    stages = project_stages(
        (
            _event(
                1,
                "model.call.completed",
                {"operation_id": "missing_start"},
                seconds=10,
            ),
            _event(
                2,
                "model.call.started",
                {"operation_id": "still_running"},
                seconds=20,
            ),
        )
    )

    missing, running = stages
    assert missing.started_at is None
    assert missing.ended_at == BASE + timedelta(seconds=10)
    assert missing.duration_ms is None
    assert missing.status is TraceStageStatus.COMPLETED
    assert running.status is TraceStageStatus.RUNNING
    assert running.ended_at is None


def test_negative_clock_skew_is_clamped_to_non_negative_duration() -> None:
    stages = project_stages(
        (
            _event(1, "tool.call.started", {"call_id": "call_1"}, seconds=10),
            _event(2, "tool.call.completed", {"call_id": "call_1"}, seconds=5),
        )
    )

    assert stages[0].duration_ms == 0


def test_malformed_related_events_fail_with_sanitized_internal_error() -> None:
    events = (
        _event(
            1,
            "model.call.started",
            {"operation_id": "duplicate", "secret": "must-not-leak"},
        ),
        _event(
            2,
            "model.call.started",
            {"operation_id": "duplicate", "secret": "must-not-leak"},
        ),
    )

    with pytest.raises(AgentSDKError) as captured:
        project_stages(events)

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.message == "failed to project trace stages"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "must-not-leak" not in str(captured.value)


def test_missing_stable_id_is_malformed_instead_of_using_event_id() -> None:
    with pytest.raises(AgentSDKError) as captured:
        project_stages((_event(1, "tool.call.started", {}),))

    assert captured.value.code is ErrorCode.INTERNAL


def test_legacy_v1_step_and_model_events_pair_with_deterministic_ids() -> None:
    events = (
        _event(1, "run.started", {}),
        _event(2, "step.started", {}),
        _event(3, "model.call.started", {"model": "fake/model"}),
        _event(4, "model.usage.reported", {"total_tokens": 3}),
        _event(5, "model.call.completed", {"finish_reason": "stop"}),
        _event(6, "step.completed", {}),
        _event(7, "run.completed", {"usage": {"total_tokens": 3}}),
    )

    first = project_stages(events)
    second = project_stages(events)

    assert first == second
    assert [stage.kind for stage in first] == [
        TraceStageKind.RUN,
        TraceStageKind.STEP,
        TraceStageKind.MODEL,
    ]
    assert all(stage.status is TraceStageStatus.COMPLETED for stage in first)
    assert first[2].parent_stage_id == first[1].stage_id
    assert first[2].usage is not None
    assert first[2].usage.total_tokens == 3


def test_v2_step_event_without_stable_id_is_rejected() -> None:
    with pytest.raises(AgentSDKError) as captured:
        project_stages((_event(1, "step.started", {}, schema_version=2),))

    assert captured.value.code is ErrorCode.INTERNAL


def test_known_stage_event_with_unknown_schema_is_rejected() -> None:
    with pytest.raises(AgentSDKError) as captured:
        project_stages(
            (
                _event(
                    1,
                    "model.call.started",
                    {"operation_id": "op_1"},
                    schema_version=999,
                ),
            )
        )

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.message == "failed to project trace stages"


def test_model_usage_is_ordering_and_bounded_stage_evidence() -> None:
    stage = project_stages(
        (
            _event(1, "model.usage.reported", {"operation_id": "op_1", "total_tokens": 3}),
            _event(2, "model.call.started", {"operation_id": "op_1"}, seconds=1),
            _event(3, "model.call.completed", {"operation_id": "op_1"}, seconds=2),
        )
    )[0]

    assert stage.first_cursor == 1
    assert stage.last_cursor == 3
    assert stage.evidence_event_ids == ("evt_1", "evt_2", "evt_3")
    assert stage.evidence_cursors == (1, 2, 3)
    assert stage.started_at == BASE + timedelta(seconds=1)
    assert stage.duration_ms == 1000


def test_model_usage_is_first_evidence_when_start_is_missing() -> None:
    stage = project_stages(
        (
            _event(1, "model.usage.reported", {"operation_id": "op_1", "total_tokens": 3}),
            _event(2, "model.call.completed", {"operation_id": "op_1"}, seconds=2),
        )
    )[0]

    assert stage.first_cursor == 1
    assert stage.last_cursor == 2
    assert stage.evidence_event_ids == ("evt_1", "evt_2")
    assert stage.evidence_cursors == (1, 2)
    assert stage.started_at is None
    assert stage.ended_at == BASE + timedelta(seconds=2)
    assert stage.usage is not None
    assert stage.usage.total_tokens == 3


def test_tool_terminal_with_a_different_step_reference_is_rejected() -> None:
    with pytest.raises(AgentSDKError) as captured:
        project_stages(
            (
                _event(
                    1,
                    "tool.call.started",
                    {"call_id": "call_1", "step_id": "step_1"},
                    schema_version=2,
                ),
                _event(
                    2,
                    "tool.call.completed",
                    {"call_id": "call_1", "step_id": "step_2"},
                    schema_version=2,
                ),
            )
        )

    assert captured.value.code is ErrorCode.INTERNAL
