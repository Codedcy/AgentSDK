from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_sdk.events.models import EventEnvelope
from agent_sdk.observability import (
    AttributionContributor,
    ObservedEvent,
    TraceStageKind,
    TraceTimeline,
    project_attribution,
    project_stages,
)
from agent_sdk.runtime.models import RunStatus


BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _event(
    cursor: int,
    event_type: str,
    payload: dict[str, object],
    *,
    run_id: str = "run_root",
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
            occurred_at=BASE + timedelta(seconds=cursor),
        ),
    )


def _context(
    cursor: int,
    view_id: str,
    *,
    source_refs: tuple[str, ...] = (),
    message_refs: tuple[str, ...] = (),
    consumed_message_ids: tuple[str, ...] = (),
    fallback_from: str | None = None,
) -> ObservedEvent:
    return _event(
        cursor,
        "context.view.created",
        {
            "view_id": view_id,
            "capsule_id": None,
            "recommended_level": fallback_from or "L0",
            "applied_level": "L2" if fallback_from is not None else "L0",
            "fallback_from": fallback_from,
            "estimated_tokens": 10,
            "budget": None,
            "message_refs": list(message_refs),
            "source_refs": list(source_refs),
            "transformations": [],
            "consumed_message_ids": list(consumed_message_ids),
            "compaction_usage": None,
        },
        run_id=view_id,
    )


def _summary(
    events: tuple[ObservedEvent, ...],
    status: RunStatus,
) -> object:
    timeline = TraceTimeline(
        root_id="run_root",
        stages=project_stages(events),
        as_of_cursor=max(event.cursor for event in events),
    )
    return project_attribution(
        root_run_id="run_root",
        terminal_status=status,
        timeline=timeline,
        events=events,
    )


def _contributor(
    contributors: tuple[AttributionContributor, ...],
    entity_id: str,
) -> AttributionContributor:
    return next(item for item in contributors if item.entity_id == entity_id)


def test_successful_run_marks_consumed_and_unused_tool_results_from_later_context() -> None:
    events = (
        _event(1, "run.started", {"run_id": "run_root"}),
        _event(
            2,
            "model.call.started",
            {"operation_id": "model-1", "context_view_id": "view-1"},
            schema_version=2,
        ),
        _event(
            3,
            "model.call.completed",
            {"operation_id": "model-1", "finish_reason": "tool_calls"},
            schema_version=2,
        ),
        _event(4, "tool.call.started", {"call_id": "tool-call-1", "tool_name": "lookup"}),
        _event(
            5,
            "tool.call.completed",
            {
                "call_id": "tool-call-1",
                "tool_name": "lookup",
                "status": "succeeded",
                "content": "{}",
                "value": {},
                "error": None,
            },
        ),
        _event(6, "tool.call.started", {"call_id": "tool-call-2", "tool_name": "lookup"}),
        _event(
            7,
            "tool.call.completed",
            {
                "call_id": "tool-call-2",
                "tool_name": "lookup",
                "status": "succeeded",
                "content": "{}",
                "value": {},
                "error": None,
            },
        ),
        _context(8, "view-2", source_refs=("evt_5",)),
        _event(
            9,
            "model.call.started",
            {"operation_id": "model-2", "context_view_id": "view-2"},
            schema_version=2,
        ),
        _event(
            10,
            "model.call.completed",
            {"operation_id": "model-2", "finish_reason": "stop"},
            schema_version=2,
        ),
        _event(11, "run.completed", {"run_id": "run_root"}),
    )

    summary = _summary(events, RunStatus.COMPLETED)

    assert summary.failure is None
    assert _contributor(summary.contributors, "tool-call-1").disposition == "consumed"
    assert _contributor(summary.contributors, "tool-call-2").disposition == "unused"
    assert _contributor(summary.contributors, "model-1").disposition == "consumed"
    assert _contributor(summary.contributors, "model-2").disposition == "terminal"
    assert "unused_tool_output" in {hint.code for hint in summary.hints}
    assert [item.entity_id for item in summary.contributors] == [
        "model-1",
        "tool-call-1",
        "tool-call-2",
        "view-2",
        "model-2",
    ]
    evidence = {event.event.event_id for event in events}
    assert all(set(item.evidence_ids) <= evidence for item in summary.contributors)
    assert all(set(item.evidence_ids) <= evidence for item in summary.hints)


def test_failed_run_uses_first_terminal_denial_before_root_failure() -> None:
    events = (
        _event(1, "run.started", {"run_id": "run_root"}),
        _event(2, "tool.call.started", {"call_id": "call-denied", "tool_name": "write"}),
        _event(
            3,
            "permission.requested",
            {"request_id": "perm-1", "call_id": "call-denied"},
            schema_version=2,
        ),
        _event(
            4,
            "permission.resolved",
            {"request_id": "perm-1", "call_id": "call-denied", "allowed": False},
            schema_version=2,
        ),
        _event(
            5,
            "tool.call.completed",
            {
                "call_id": "call-denied",
                "tool_name": "write",
                "status": "denied",
                "content": "{}",
                "value": None,
                "error": "tool access denied",
            },
        ),
        _event(
            6,
            "run.failed",
            {
                "run_id": "run_root",
                "error": {"code": "run_failed", "message": "failed", "retryable": False},
            },
        ),
    )
    timeline = TraceTimeline(
        root_id="run_root",
        stages=project_stages(events),
        as_of_cursor=6,
    )
    first_terminal_failure = next(
        stage
        for stage in timeline.stages
        if stage.kind is TraceStageKind.PERMISSION
    )

    failed = project_attribution(
        root_run_id="run_root",
        terminal_status=RunStatus.FAILED,
        timeline=timeline,
        events=events,
    )

    assert failed.failure is not None
    assert failed.failure.stage_id == first_terminal_failure.stage_id
    assert failed.failure.code == "permission_denied"
    assert {hint.code for hint in failed.hints} >= {"permission_denied"}


def test_failed_child_is_a_hint_but_not_root_failure_when_parent_completes() -> None:
    events = (
        _event(1, "run.started", {"run_id": "run_root"}),
        _event(
            2,
            "run.created",
            {"run_id": "run_child", "parent_run_id": "run_root"},
            run_id="run_child",
        ),
        _event(3, "run.started", {"run_id": "run_child"}, run_id="run_child"),
        _event(
            4,
            "run.failed",
            {
                "run_id": "run_child",
                "error": {"code": "child_error", "message": "failed", "retryable": False},
            },
            run_id="run_child",
        ),
        _event(5, "run.completed", {"run_id": "run_root"}),
    )

    child_failure_on_success = _summary(events, RunStatus.COMPLETED)

    assert child_failure_on_success.failure is None
    assert "child_failure" in {hint.code for hint in child_failure_on_success.hints}
    assert _contributor(child_failure_on_success.contributors, "run_child").disposition == "unused"


def test_child_result_is_consumed_only_by_parent_context_reference() -> None:
    events = (
        _event(1, "run.started", {"run_id": "run_root"}),
        _event(
            2,
            "run.created",
            {"run_id": "run_child", "parent_run_id": "run_root"},
            run_id="run_child",
        ),
        _event(3, "run.started", {"run_id": "run_child"}, run_id="run_child"),
        _event(4, "run.completed", {"run_id": "run_child"}, run_id="run_child"),
        _event(
            5,
            "agent.message.sent",
            {
                "message_id": "msg-child",
                "sender_run_id": "run_child",
                "recipient_run_id": "run_root",
            },
            run_id="msg-child",
        ),
        _context(6, "view-parent", consumed_message_ids=("msg-child",)),
        _event(
            7,
            "model.call.started",
            {"operation_id": "model-parent", "context_view_id": "view-parent"},
            schema_version=2,
        ),
        _event(
            8,
            "model.call.completed",
            {"operation_id": "model-parent", "finish_reason": "stop"},
            schema_version=2,
        ),
        _event(9, "run.completed", {"run_id": "run_root"}),
    )

    summary = _summary(events, RunStatus.COMPLETED)

    assert _contributor(summary.contributors, "run_child").disposition == "consumed"


def test_context_l3_fallback_to_l2_emits_one_fixed_hint() -> None:
    events = (
        _event(1, "run.started", {"run_id": "run_root"}),
        _context(2, "view-fallback", fallback_from="L3"),
        _event(
            3,
            "model.call.started",
            {"operation_id": "model-1", "context_view_id": "view-fallback"},
            schema_version=2,
        ),
        _event(
            4,
            "model.call.completed",
            {"operation_id": "model-1", "finish_reason": "stop"},
            schema_version=2,
        ),
        _event(5, "run.completed", {"run_id": "run_root"}),
    )

    summary = _summary(events, RunStatus.COMPLETED)
    hints = [hint for hint in summary.hints if hint.code == "context_fallback"]

    assert len(hints) == 1
    assert hints[0].summary == "Context compaction fell back to a lower level."
    assert hints[0].evidence_ids == ("evt_2",)


def test_workflow_loop_limit_failure_is_attributed_and_hinted() -> None:
    events = (
        _event(1, "run.started", {"run_id": "run_root"}),
        _event(
            2,
            "workflow.started",
            {"workflow_run_id": "workflow-1"},
            run_id="workflow-1",
        ),
        _event(
            3,
            "workflow.failed",
            {
                "workflow_run_id": "workflow-1",
                "error": {
                    "code": "workflow_loop_limit",
                    "message": "loop limit",
                    "retryable": False,
                },
            },
            run_id="workflow-1",
        ),
        _event(
            4,
            "run.failed",
            {
                "run_id": "run_root",
                "error": {
                    "code": "workflow_loop_limit",
                    "message": "loop limit",
                    "retryable": False,
                },
            },
        ),
    )

    summary = _summary(events, RunStatus.FAILED)

    assert summary.failure is not None
    assert summary.failure.stage_kind is TraceStageKind.WORKFLOW
    assert summary.failure.code == "workflow_loop_limit"
    assert "workflow_loop_limit" in {hint.code for hint in summary.hints}


def test_two_failures_of_same_tool_emit_one_repeated_failure_hint() -> None:
    events = (
        _event(1, "run.started", {"run_id": "run_root"}),
        _event(2, "tool.call.started", {"call_id": "call-1", "tool_name": "lookup"}),
        _event(
            3,
            "tool.call.completed",
            {
                "call_id": "call-1",
                "tool_name": "lookup",
                "status": "failed",
                "content": "{}",
                "value": None,
                "error": "failed",
            },
        ),
        _event(4, "tool.call.started", {"call_id": "call-2", "tool_name": "lookup"}),
        _event(
            5,
            "tool.call.completed",
            {
                "call_id": "call-2",
                "tool_name": "lookup",
                "status": "failed",
                "content": "{}",
                "value": None,
                "error": "failed",
            },
        ),
        _event(6, "run.completed", {"run_id": "run_root"}),
    )

    summary = _summary(events, RunStatus.COMPLETED)
    hints = [hint for hint in summary.hints if hint.code == "repeated_tool_failure"]

    assert len(hints) == 1
    assert hints[0].evidence_ids == ("evt_3", "evt_5")


def test_interrupted_unknown_external_operation_is_visible_as_fixed_hint() -> None:
    events = (
        _event(1, "run.started", {"run_id": "run_root"}),
        _event(
            2,
            "model.call.started",
            {"operation_id": "external-model", "context_view_id": "view-1"},
            schema_version=2,
        ),
        _event(3, "run.interrupted", {"run_id": "run_root", "status": "interrupted"}),
    )

    summary = _summary(events, RunStatus.INTERRUPTED)

    assert summary.failure is not None
    assert summary.failure.stage_kind is TraceStageKind.RUN
    assert "interrupted_external_work" in {hint.code for hint in summary.hints}


def test_explicit_evaluation_pass_fail_and_unknown_are_preserved() -> None:
    events = [
        _event(1, "run.started", {"run_id": "run_root"}),
        _event(2, "run.completed", {"run_id": "run_root"}),
    ]
    for cursor, verdict in enumerate(("pass", "fail", "unknown"), start=3):
        evaluation_id = f"evaluation-{verdict}"
        events.append(
            _event(
                cursor,
                "evaluation.completed",
                {
                    "evaluation_id": evaluation_id,
                    "session_id": "ses_1",
                    "subject_run_id": "run_root",
                    "subject_type": "run",
                    "evaluator_id": "explicit",
                    "evaluator_version": "1",
                    "method": "deterministic",
                    "verdict": verdict,
                    "metrics": {},
                    "reason": verdict,
                    "confidence": 1.0,
                    "evidence_event_ids": ["evt_2"],
                    "created_at": BASE.isoformat(),
                    "subject_cursor": 2,
                    "schema_version": 1,
                    "record_version": 1,
                },
                run_id=evaluation_id,
            )
        )

    summary = _summary(tuple(events), RunStatus.COMPLETED)

    evaluations = [item for item in summary.contributors if item.kind == "evaluation"]
    assert [(item.entity_id, item.status) for item in evaluations] == [
        ("evaluation-pass", "pass"),
        ("evaluation-fail", "fail"),
        ("evaluation-unknown", "unknown"),
    ]
    assert summary.evaluation_ids == (
        "evaluation-pass",
        "evaluation-fail",
        "evaluation-unknown",
    )
    assert summary.method == "deterministic_event_evidence_v1"
