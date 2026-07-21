from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_sdk.events.models import EventEnvelope
from agent_sdk.permissions.models import PermissionRequest
from agent_sdk.tools.models import ToolResult

from .reconciliation import ExternalOperation, ModelCallOperation

STAGE_EVENT_SCHEMA_VERSION = 2

_V2_STAGE_EVENTS = frozenset(
    {
        "step.started",
        "step.completed",
        "step.failed",
        "model.call.started",
        "model.usage.reported",
        "model.call.completed",
        "model.call.failed",
        "tool.call.started",
        "permission.requested",
        "permission.resolved",
    }
)


def stage_event_schema_version(event_type: str) -> int:
    return STAGE_EVENT_SCHEMA_VERSION if event_type in _V2_STAGE_EVENTS else 1


def normalize_stage_events_for_recovery(
    events: tuple[EventEnvelope, ...],
    operations: tuple[ExternalOperation, ...],
) -> tuple[EventEnvelope, ...] | None:
    model_operations = {
        operation.operation_id: operation
        for operation in operations
        if isinstance(operation, ModelCallOperation)
    }
    model_operations_by_turn = {
        operation.turn: operation
        for operation in model_operations.values()
    }
    current_step: str | None = None
    current_call: str | None = None
    legacy_model_turn = 0
    legacy_step_open = False
    normalized: list[EventEnvelope] = []
    for event in events:
        if event.type == "run.created" and event.schema_version in {1, 2, 3}:
            normalized.append(event)
            continue
        if event.schema_version == 1:
            normalized.append(event)
            if event.type == "step.started":
                legacy_step_open = True
            elif event.type in {"step.completed", "step.failed"}:
                current_step = None
                current_call = None
                legacy_step_open = False
            elif event.type == "model.call.started":
                operation = model_operations_by_turn.get(legacy_model_turn)
                legacy_model_turn += 1
                if operation is not None and legacy_step_open:
                    current_step = operation.operation_id
            elif event.type == "tool.call.proposed":
                call_id = event.payload.get("call_id")
                current_call = call_id if isinstance(call_id, str) else None
            continue
        if event.schema_version != STAGE_EVENT_SCHEMA_VERSION:
            return None
        payload = event.payload
        legacy: dict[str, Any]
        if event.type == "step.started":
            step_id = payload.get("step_id")
            if (
                set(payload) != {"step_id"}
                or not isinstance(step_id, str)
                or step_id not in model_operations
            ):
                return None
            current_step = step_id
            current_call = None
            legacy = {}
        elif event.type == "step.completed":
            if payload != {"step_id": current_step} or current_step is None:
                return None
            legacy = {}
            current_step = None
            current_call = None
        elif event.type == "step.failed":
            if (
                current_step is None
                or payload.get("step_id") != current_step
                or set(payload) != {"step_id", "error"}
                or not isinstance(payload.get("error"), Mapping)
            ):
                return None
            legacy = {"error": payload["error"]}
            current_step = None
            current_call = None
        elif event.type == "model.call.started":
            operation = _model_operation(payload, model_operations, current_step)
            if operation is None:
                return None
            legacy = _legacy_model_started(operation)
            if payload != {**legacy, "operation_id": operation.operation_id, "step_id": operation.operation_id}:
                return None
        elif event.type == "model.usage.reported":
            operation = _model_operation(payload, model_operations, current_step)
            if operation is None or operation.outcome is None:
                return None
            usage = operation.outcome.get("usage")
            if not isinstance(usage, Mapping):
                return None
            legacy = dict(usage)
            if payload != {"operation_id": operation.operation_id, **legacy}:
                return None
        elif event.type == "model.call.completed":
            operation = _model_operation(payload, model_operations, current_step)
            if operation is None or operation.outcome is None:
                return None
            legacy = {"finish_reason": operation.outcome.get("finish_reason")}
            expected: dict[str, Any] = {
                "operation_id": operation.operation_id,
                "step_id": operation.operation_id,
                **legacy,
            }
            if operation.context_view_id is not None:
                expected["context_view_id"] = operation.context_view_id
            if operation.prompt_manifest_id is not None:
                expected["prompt_manifest_id"] = operation.prompt_manifest_id
            if payload != expected:
                return None
        elif event.type == "model.call.failed":
            operation = _model_operation(payload, model_operations, current_step)
            error = payload.get("error")
            if operation is None or not isinstance(error, Mapping):
                return None
            expected = {
                "operation_id": operation.operation_id,
                "step_id": operation.operation_id,
                "error": error,
            }
            if operation.context_view_id is not None:
                expected["context_view_id"] = operation.context_view_id
            if operation.prompt_manifest_id is not None:
                expected["prompt_manifest_id"] = operation.prompt_manifest_id
            if payload != expected:
                return None
            legacy = {"error": error}
        elif event.type == "tool.call.started":
            call_id = payload.get("call_id")
            tool_name = payload.get("tool_name")
            if (
                current_step is None
                or current_call is None
                or call_id != current_call
                or not isinstance(tool_name, str)
                or not tool_name
                or payload
                != {
                    "call_id": current_call,
                    "tool_name": tool_name,
                    "step_id": current_step,
                }
            ):
                return None
            legacy = {"call_id": current_call, "tool_name": tool_name}
        elif event.type == "tool.call.completed":
            if current_step is None or payload.get("step_id") != current_step:
                return None
            if payload.get("result_event_id") != event.event_id:
                return None
            raw_result = {
                key: value
                for key, value in payload.items()
                if key not in {"step_id", "result_event_id"}
            }
            try:
                result = ToolResult.model_validate(raw_result)
            except Exception:
                return None
            legacy = result.model_dump(mode="json")
            if payload != {**legacy, "step_id": current_step, "result_event_id": event.event_id}:
                return None
            if current_call is not None and result.call_id != current_call:
                return None
        elif event.type in {"permission.requested", "permission.resolved"}:
            request_data = payload.get("request")
            if not isinstance(request_data, Mapping):
                return None
            try:
                request = PermissionRequest.model_validate(request_data)
            except Exception:
                return None
            expected_keys = {"request", "request_id", "call_id"}
            if event.type == "permission.resolved":
                expected_keys.add("decision")
            if (
                set(payload) != expected_keys
                or payload.get("request_id") != request.request_id
                or payload.get("call_id") != current_call
                or request.run_id != event.run_id
            ):
                return None
            legacy = {"request": request.model_dump(mode="json")}
            if event.type == "permission.resolved":
                legacy["decision"] = payload["decision"]
        else:
            return None
        normalized.append(
            event.model_copy(
                update={"schema_version": 1, "payload": legacy}
            )
        )
    return tuple(normalized)


def _model_operation(
    payload: Mapping[str, Any],
    operations: Mapping[str, ModelCallOperation],
    current_step: str | None,
) -> ModelCallOperation | None:
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or operation_id != current_step:
        return None
    return operations.get(operation_id)


def _legacy_model_started(operation: ModelCallOperation) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": operation.provider_identity}
    if operation.prepared_request is not None:
        payload.update(
            {
                "context_view_id": operation.context_view_id,
                "prompt_manifest_id": operation.prompt_manifest_id,
                "request_fingerprint": operation.request_fingerprint,
            }
        )
    return payload
