import json
from datetime import UTC, datetime, timedelta, timezone
from importlib.util import find_spec
from typing import Any

import agent_sdk.runtime.reconciliation as reconciliation
import pytest
from pydantic import ValidationError

from agent_sdk.errors import AgentSDKError
from agent_sdk.models.litellm_gateway import ModelRequest
from agent_sdk.runtime.models import TokenUsage
from agent_sdk.tools.models import ToolResult


def test_reconciliation_module_exists() -> None:
    assert find_spec("agent_sdk.runtime.reconciliation") is not None


def test_recovery_enums_have_the_persisted_values() -> None:
    assert tuple(item.value for item in reconciliation.ExternalOperationKind) == (
        "model_call",
        "tool_call",
    )
    assert tuple(item.value for item in reconciliation.ExternalOperationStatus) == (
        "started",
        "completed",
        "failed",
    )
    assert tuple(item.value for item in reconciliation.RunCheckpointPhase) == (
        "ready_for_model",
        "model_in_flight",
        "ready_for_tool",
        "tool_in_flight",
        "waiting",
        "terminal",
    )
    assert tuple(item.value for item in reconciliation.ReconciliationStatus) == (
        "pending",
        "resolved",
    )
    assert tuple(item.value for item in reconciliation.ReconciliationAction) == (
        "confirm_completed",
        "confirm_not_executed",
        "retry",
        "terminate",
    )


def test_recovery_conflict_error_is_constant_and_retryable() -> None:
    error = reconciliation.RecoveryStateConflictError()

    assert error.to_dict() == {
        "code": "conflict",
        "message": "recovery state conflict",
        "retryable": True,
    }


def _model_operation(**updates: Any) -> Any:
    values: dict[str, Any] = {
        "operation_id": "op_model",
        "session_id": "ses_1",
        "run_id": "run_1",
        "turn": 0,
        "request_fingerprint": "sha256:model",
        "lease_generation": 1,
        "status": reconciliation.ExternalOperationStatus.STARTED,
        "provider_identity": "provider:model",
    }
    values.update(updates)
    return reconciliation.ModelCallOperation(**values)


def _tool_operation(**updates: Any) -> Any:
    values: dict[str, Any] = {
        "operation_id": "op_tool",
        "session_id": "ses_1",
        "run_id": "run_1",
        "turn": 1,
        "request_fingerprint": "sha256:tool",
        "lease_generation": 1,
        "status": reconciliation.ExternalOperationStatus.STARTED,
        "tool_identity": "tool:search",
    }
    values.update(updates)
    return reconciliation.ToolCallOperation(**values)


def test_model_request_payload_is_canonical_and_round_trips_exactly() -> None:
    request = ModelRequest(
        model="provider:model",
        messages=(
            {"role": "system", "content": "general"},
            {"role": "user", "content": "ship"},
        ),
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object"},
                },
            },
        ),
        params={"temperature": 0, "metadata": {"labels": ["release"]}},
        purpose="agent_loop",
    )

    payload = reconciliation.serialize_model_request(request)

    assert payload == {
        "model": "provider:model",
        "messages": [
            {"role": "system", "content": "general"},
            {"role": "user", "content": "ship"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object"},
                },
            }
        ],
        "params": {
            "temperature": 0,
            "metadata": {"labels": ["release"]},
        },
        "purpose": "agent_loop",
    }
    assert reconciliation.deserialize_model_request(payload) == request
    assert (
        reconciliation.model_request_fingerprint(request)
        == reconciliation.model_request_fingerprint(
            reconciliation.deserialize_model_request(payload)
        )
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "model": "provider:model",
            "messages": [],
            "tools": [],
            "params": {},
            "purpose": None,
            "extra": True,
        },
        {
            "model": "provider:model",
            "messages": {},
            "tools": [],
            "params": {},
            "purpose": None,
        },
        {
            "model": "provider:model",
            "messages": [],
            "tools": [],
            "params": {"temperature": float("nan")},
            "purpose": None,
        },
    ],
)
def test_stored_model_request_rejects_noncanonical_payloads(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(AgentSDKError, match="stored model request is invalid"):
        reconciliation.deserialize_model_request(payload)


def test_model_operation_accepts_legacy_records_and_rejects_prepared_mismatch() -> None:
    legacy = {
        "operation_id": "op_model",
        "operation_kind": "model_call",
        "session_id": "ses_1",
        "run_id": "run_1",
        "turn": 0,
        "request_fingerprint": "sha256:model",
        "lease_generation": 1,
        "status": "started",
        "provider_identity": "provider:model",
        "tool_identity": None,
        "outcome": None,
        "recovery_metadata": {},
    }
    assert reconciliation.ModelCallOperation.model_validate_json(
        json.dumps(legacy)
    ) == _model_operation()

    request = ModelRequest(
        model="provider:model",
        messages=({"role": "user", "content": "ship"},),
    )
    prepared = reconciliation.serialize_model_request(request)
    with pytest.raises(ValidationError, match="fingerprint mismatch"):
        _model_operation(
            request_fingerprint="wrong",
            context_view_id="view_1",
            prompt_manifest_id="pmf_1",
            prepared_request=prepared,
        )


def test_external_operation_models_are_strict_frozen_detached_and_exact() -> None:
    outcome = {"response": {"parts": ["one"]}}
    metadata = {"query": {"supported": True}}
    operation = _model_operation(
        status=reconciliation.ExternalOperationStatus.COMPLETED,
        outcome=outcome,
        recovery_metadata=metadata,
    )
    outcome["response"]["parts"].append("caller mutation")
    metadata["query"]["supported"] = False

    assert operation.operation_kind is reconciliation.ExternalOperationKind.MODEL_CALL
    assert operation.tool_identity is None
    assert operation.outcome == {"response": {"parts": ("one",)}}
    assert operation.recovery_metadata == {"query": {"supported": True}}
    with pytest.raises(TypeError):
        operation.outcome["new"] = True  # type: ignore[index]
    with pytest.raises(ValidationError):
        operation.status = reconciliation.ExternalOperationStatus.FAILED  # type: ignore[misc]
    with pytest.raises(ValidationError):
        reconciliation.ModelCallOperation.model_validate(
            {**operation.model_dump(), "turn": "0"}
        )
    with pytest.raises(ValidationError):
        reconciliation.ModelCallOperation.model_validate(
            {**operation.model_dump(), "unexpected": True}
        )

    reconstructed = reconciliation.ModelCallOperation.model_validate_json(
        operation.model_dump_json()
    )
    assert reconstructed == operation
    assert reconstructed is not operation
    assert reconstructed.outcome is not operation.outcome


@pytest.mark.parametrize(
    ("factory", "updates"),
    [
        (_model_operation, {"operation_id": " "}),
        (_model_operation, {"session_id": ""}),
        (_model_operation, {"run_id": "\t"}),
        (_model_operation, {"request_fingerprint": " "}),
        (_model_operation, {"provider_identity": " "}),
        (_model_operation, {"provider_identity": None}),
        (_model_operation, {"tool_identity": "tool:wrong"}),
        (_model_operation, {"turn": -1}),
        (_model_operation, {"lease_generation": 0}),
        (_model_operation, {"status": reconciliation.ExternalOperationStatus.COMPLETED}),
        (_model_operation, {"status": reconciliation.ExternalOperationStatus.FAILED}),
        (
            _model_operation,
            {
                "status": reconciliation.ExternalOperationStatus.STARTED,
                "outcome": {},
            },
        ),
        (_tool_operation, {"tool_identity": " "}),
        (_tool_operation, {"tool_identity": None}),
        (_tool_operation, {"provider_identity": "provider:wrong"}),
    ],
)
def test_external_operation_invariants_reject_invalid_values(
    factory: Any, updates: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        factory(**updates)


def test_tool_operation_has_one_unambiguous_persisted_representation() -> None:
    operation = _tool_operation(
        status=reconciliation.ExternalOperationStatus.FAILED,
        outcome={},
    )

    assert operation.operation_kind is reconciliation.ExternalOperationKind.TOOL_CALL
    assert operation.provider_identity is None
    assert reconciliation.ToolCallOperation.model_validate_json(
        operation.model_dump_json()
    ) == operation
    with pytest.raises(ValidationError):
        operation.model_copy(update={"operation_kind": "model_call"})


def _checkpoint(**updates: Any) -> Any:
    values: dict[str, Any] = {
        "run_id": "run_1",
        "session_id": "ses_1",
        "checkpoint_version": 1,
        "turn": 0,
        "phase": reconciliation.RunCheckpointPhase.READY_FOR_MODEL,
        "messages": ({"role": "user", "content": {"parts": ["hello"]}},),
    }
    values.update(updates)
    return reconciliation.RunCheckpoint(**values)


def test_checkpoint_is_strict_frozen_deep_detached_and_round_trips() -> None:
    messages = ({"role": "user", "content": {"parts": ["hello"]}},)
    usage = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    tool_result = ToolResult.succeeded("call_1", "search", {"hits": [1]})
    checkpoint = _checkpoint(
        messages=messages,
        output_parts=("first",),
        usage=usage,
        tool_results=(tool_result,),
    )
    messages[0]["content"]["parts"].append("caller mutation")

    assert checkpoint.messages == (
        {"role": "user", "content": {"parts": ("hello",)}},
    )
    assert checkpoint.usage == usage
    assert checkpoint.usage is not usage
    assert checkpoint.tool_results == (tool_result,)
    assert checkpoint.tool_results[0] is not tool_result
    with pytest.raises(TypeError):
        checkpoint.messages[0]["role"] = "assistant"  # type: ignore[index]
    with pytest.raises(ValidationError):
        checkpoint.turn = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        reconciliation.RunCheckpoint.model_validate(
            {**checkpoint.model_dump(), "turn": "0"}
        )
    with pytest.raises(ValidationError):
        reconciliation.RunCheckpoint.model_validate(
            {**checkpoint.model_dump(), "extra": True}
        )

    reconstructed = reconciliation.RunCheckpoint.model_validate_json(
        checkpoint.model_dump_json()
    )
    assert reconstructed == checkpoint
    assert reconstructed.messages is not checkpoint.messages


@pytest.mark.parametrize(
    "updates",
    [
        {"run_id": " "},
        {"session_id": ""},
        {"checkpoint_version": 0},
        {"turn": -1},
        {"messages": ()},
        {
            "phase": reconciliation.RunCheckpointPhase.MODEL_IN_FLIGHT,
            "operation_id": None,
        },
        {
            "phase": reconciliation.RunCheckpointPhase.TOOL_IN_FLIGHT,
            "operation_id": " ",
        },
        {
            "phase": reconciliation.RunCheckpointPhase.WAITING,
            "operation_id": "op_wrong",
        },
        {
            "phase": reconciliation.RunCheckpointPhase.TERMINAL,
            "operation_id": "op_wrong",
        },
    ],
)
def test_checkpoint_phase_and_identity_invariants(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _checkpoint(**updates)


def _resolution(**updates: Any) -> Any:
    values: dict[str, Any] = {
        "action": reconciliation.ReconciliationAction.TERMINATE,
        "actor": {"type": "user", "id": "operator"},
        "evidence": {"reason": ["provider status unknown"]},
        "decided_at": datetime(2026, 7, 14, 8, tzinfo=UTC),
        "event_id": "evt_resolution",
    }
    values.update(updates)
    return reconciliation.ReconciliationResolution(**values)


def _request(**updates: Any) -> Any:
    values: dict[str, Any] = {
        "request_id": "rec_1",
        "session_id": "ses_1",
        "run_id": "run_1",
        "reason": "operation outcome is unknown",
        "details": {"attempts": [1]},
    }
    values.update(updates)
    return reconciliation.ReconciliationRequest(**values)


def test_reconciliation_models_are_strict_frozen_detached_and_normalize_utc() -> None:
    actor = {"type": "user", "roles": ["operator"]}
    evidence = {"provider": {"status": "unknown"}}
    resolution = _resolution(
        action=reconciliation.ReconciliationAction.RETRY,
        actor=actor,
        evidence=evidence,
        decided_at=datetime(2026, 7, 14, 16, tzinfo=timezone_eight()),
    )
    request = _request(
        status=reconciliation.ReconciliationStatus.RESOLVED,
        resolution=resolution,
    )
    actor["roles"].append("caller mutation")
    evidence["provider"]["status"] = "completed"

    assert resolution.decided_at == datetime(2026, 7, 14, 8, tzinfo=UTC)
    assert resolution.actor == {"type": "user", "roles": ("operator",)}
    assert resolution.evidence == {"provider": {"status": "unknown"}}
    assert request.details == {"attempts": (1,)}
    with pytest.raises(TypeError):
        resolution.actor["new"] = True  # type: ignore[index]
    with pytest.raises(ValidationError):
        request.status = reconciliation.ReconciliationStatus.PENDING  # type: ignore[misc]

    reconstructed = reconciliation.ReconciliationRequest.model_validate_json(
        request.model_dump_json()
    )
    assert reconstructed == request
    assert reconstructed.resolution is not resolution


def timezone_eight() -> Any:
    return timezone(timedelta(hours=8))


@pytest.mark.parametrize(
    "updates",
    [
        {"event_id": " "},
        {"actor": {}},
        {"evidence": {}},
        {"decided_at": datetime(2026, 7, 14, 8)},
    ],
)
def test_reconciliation_resolution_invariants(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _resolution(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"request_id": " "},
        {"session_id": ""},
        {"run_id": "\t"},
        {"operation_id": " "},
        {"reason": " "},
        {"status": reconciliation.ReconciliationStatus.RESOLVED},
    ],
)
def test_reconciliation_request_invariants(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _request(**updates)


def test_pending_reconciliation_request_forbids_resolution() -> None:
    with pytest.raises(ValidationError):
        _request(
            status=reconciliation.ReconciliationStatus.PENDING,
            resolution=_resolution(),
        )
