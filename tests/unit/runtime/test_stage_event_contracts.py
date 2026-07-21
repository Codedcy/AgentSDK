from __future__ import annotations

from datetime import UTC, datetime

from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.event_contracts import normalize_stage_events_for_recovery
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
)


def _event(
    sequence: int,
    event_type: str,
    payload: dict[str, object],
    *,
    schema_version: int,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt_{sequence}",
        schema_version=schema_version,
        type=event_type,
        session_id="ses_1",
        run_id="run_1",
        sequence=sequence,
        payload=payload,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _operation() -> ModelCallOperation:
    return ModelCallOperation(
        operation_id="op_1",
        session_id="ses_1",
        run_id="run_1",
        turn=0,
        request_fingerprint="fingerprint",
        lease_generation=1,
        status=ExternalOperationStatus.STARTED,
        provider_identity="provider/model",
    )


def test_legacy_v1_recovery_evidence_is_exactly_preserved() -> None:
    events = (
        _event(1, "step.started", {}, schema_version=1),
        _event(2, "model.call.started", {"model": "provider/model"}, schema_version=1),
    )

    assert normalize_stage_events_for_recovery(events, ()) == events


def test_v2_recovery_evidence_is_validated_and_normalized_to_v1() -> None:
    events = (
        _event(1, "step.started", {"step_id": "op_1"}, schema_version=2),
        _event(
            2,
            "model.call.started",
            {
                "model": "provider/model",
                "operation_id": "op_1",
                "step_id": "op_1",
            },
            schema_version=2,
        ),
    )

    normalized = normalize_stage_events_for_recovery(events, (_operation(),))

    assert normalized is not None
    assert [event.schema_version for event in normalized] == [1, 1]
    assert [dict(event.payload) for event in normalized] == [
        {},
        {"model": "provider/model"},
    ]


def test_v2_recovery_reference_tampering_is_rejected() -> None:
    events = (
        _event(1, "step.started", {"step_id": "op_forged"}, schema_version=2),
    )

    assert normalize_stage_events_for_recovery(events, (_operation(),)) is None


def test_v2_terminal_can_follow_a_certified_legacy_v1_step() -> None:
    events = (
        _event(1, "step.started", {}, schema_version=1),
        _event(2, "model.call.started", {"model": "provider/model"}, schema_version=1),
        _event(
            3,
            "model.call.failed",
            {
                "operation_id": "op_1",
                "step_id": "op_1",
                "error": {
                    "code": "internal_error",
                    "message": "model call failed",
                    "retryable": False,
                },
            },
            schema_version=2,
        ),
    )

    normalized = normalize_stage_events_for_recovery(events, (_operation(),))

    assert normalized is not None
    assert normalized[-1].schema_version == 1
    assert set(normalized[-1].payload) == {"error"}


def test_hashed_recovery_permission_events_are_v1_only() -> None:
    requested = _event(
        1,
        "permission.requested",
        {"request": {"sha256": "request"}, "tool": {"sha256": "tool"}},
        schema_version=1,
    )
    resolved = _event(
        2,
        "permission.resolved",
        {
            "request": {"sha256": "request"},
            "tool": {"sha256": "tool"},
            "allowed": True,
        },
        schema_version=1,
    )

    assert normalize_stage_events_for_recovery((requested, resolved), ()) == (
        requested,
        resolved,
    )
    assert (
        normalize_stage_events_for_recovery(
            (requested.model_copy(update={"schema_version": 2}),),
            (),
        )
        is None
    )
