from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import ValidationError as PydanticValidationError

from agent_sdk.errors import AgentSDKError
from agent_sdk.ids import new_id
from agent_sdk.models.litellm_gateway import ToolCallCompleted
from agent_sdk.permissions.broker import InProcessPermissionBridge, PermissionBroker
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.tools.errors import ToolAccessDenied, ToolExecutionTimedOut
from agent_sdk.tools.models import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    thaw_json,
)
from agent_sdk.tools.registry import RegisteredTool, ToolRegistry

_Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
_PermissionTransition = Callable[[PermissionRequest, PermissionDecision | None], Awaitable[None]]
_BeforeHandler = Callable[[ToolCallCompleted, RegisteredTool, Mapping[str, Any]], Awaitable[None]]
_CompleteCall = Callable[[ToolCallCompleted, ToolResult], Awaitable[None]]
_Preflight = Callable[[], Awaitable[None]]


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        bridge: InProcessPermissionBridge | None,
    ) -> None:
        self._registry = registry
        self._permissions = PermissionBroker(policy, bridge)

    async def execute(
        self,
        call: ToolCallCompleted,
        context: ToolContext,
        *,
        emit: _Emit,
        on_permission_requested: _PermissionTransition,
        on_permission_resolved: _PermissionTransition,
        on_before_handler: _BeforeHandler | None = None,
        on_call_completed: _CompleteCall | None = None,
        on_preflight: _Preflight | None = None,
        sanitize_permission_denial: bool = False,
    ) -> ToolResult:
        if on_preflight is not None:
            await on_preflight()
        try:
            registered = self._registry.get(call.name)
        except AgentSDKError:
            return await self._complete_error(
                call,
                ToolResultStatus.FAILED,
                "tool not found",
                emit,
                on_call_completed,
                on_preflight,
            )

        try:
            decoded = json.loads(
                call.arguments_json,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(decoded, dict):
                raise ValueError("tool arguments must be an object")
            arguments = cast(dict[str, Any], decoded)
            Draft202012Validator(thaw_json(registered.spec.input_schema)).validate(arguments)
        except (
            json.JSONDecodeError,
            ValueError,
            ValidationError,
            PydanticValidationError,
        ):
            return await self._complete_error(
                call,
                ToolResultStatus.INVALID_ARGUMENTS,
                "invalid tool arguments",
                emit,
                on_call_completed,
                on_preflight,
            )

        if on_preflight is not None:
            await on_preflight()

        try:
            permission_arguments: Mapping[str, Any] = arguments
            if registered.permission_arguments is not None:
                permission_arguments = await registered.permission_arguments(
                    context,
                    arguments,
                )
            request = PermissionRequest(
                request_id=new_id("prm"),
                run_id=context.run_id,
                session_id=context.session_id,
                tool_name=call.name,
                arguments=permission_arguments,
                effects=registered.spec.effects,
            )
        except ToolAccessDenied:
            return await self._complete_error(
                call,
                ToolResultStatus.DENIED,
                "tool access denied",
                emit,
                on_call_completed,
                on_preflight,
            )
        except Exception:
            return await self._complete_error(
                call,
                ToolResultStatus.FAILED,
                "tool permission preflight failed",
                emit,
                on_call_completed,
                on_preflight,
            )

        if on_preflight is not None:
            await on_preflight()

        decision = await self._permissions.authorize(
            request,
            on_requested=on_permission_requested,
            on_resolved=on_permission_resolved,
        )
        if on_preflight is not None:
            await on_preflight()
        if not decision.allowed:
            return await self._complete_error(
                call,
                ToolResultStatus.DENIED,
                (
                    "permission denied"
                    if sanitize_permission_denial
                    else decision.reason or "permission denied"
                ),
                emit,
                on_call_completed,
                on_preflight,
            )

        await emit(
            "tool.call.authorized",
            {"call_id": call.call_id, "tool_name": call.name},
        )
        if on_before_handler is None:
            await emit(
                "tool.call.started",
                {"call_id": call.call_id, "tool_name": call.name},
            )
        else:
            await on_before_handler(call, registered, arguments)
        try:
            invocation = registered.handler(
                context,
                **cast(dict[str, Any], thaw_json(arguments)),
            )
            handler_task = asyncio.ensure_future(invocation)
            handler_task.add_done_callback(_consume_handler_completion)
            try:
                if registered.spec.timeout_seconds is None:
                    value = await asyncio.shield(handler_task)
                else:
                    try:
                        value = await asyncio.wait_for(
                            asyncio.shield(handler_task),
                            timeout=registered.spec.timeout_seconds,
                        )
                    except TimeoutError:
                        handler_task.cancel()
                        value = _HANDLER_TIMED_OUT
            except asyncio.CancelledError:
                handler_task.cancel()
                raise
            if value is _HANDLER_TIMED_OUT:
                result = ToolResult.normalized_error(
                    call.call_id,
                    call.name,
                    ToolResultStatus.TIMED_OUT,
                    "tool execution timed out",
                )
            else:
                try:
                    result = ToolResult.succeeded(call.call_id, call.name, value)
                except ValueError:
                    result = ToolResult.normalized_error(
                        call.call_id,
                        call.name,
                        ToolResultStatus.FAILED,
                        "tool result is not JSON-compatible or exceeds size limit",
                    )
        except asyncio.CancelledError:
            raise
        except ToolAccessDenied:
            result = ToolResult.normalized_error(
                call.call_id,
                call.name,
                ToolResultStatus.DENIED,
                "tool access denied",
            )
        except ToolExecutionTimedOut:
            result = ToolResult.normalized_error(
                call.call_id,
                call.name,
                ToolResultStatus.TIMED_OUT,
                "tool execution timed out",
            )
        except Exception:
            result = ToolResult.normalized_error(
                call.call_id,
                call.name,
                ToolResultStatus.FAILED,
                "tool handler failed",
            )

        if on_preflight is not None:
            await on_preflight()
        if on_call_completed is None:
            await emit("tool.call.completed", self._result_payload(result))
        else:
            await on_call_completed(call, result)
        return result

    @staticmethod
    async def _complete_error(
        call: ToolCallCompleted,
        status: ToolResultStatus,
        message: str,
        emit: _Emit,
        on_call_completed: _CompleteCall | None = None,
        on_preflight: _Preflight | None = None,
    ) -> ToolResult:
        if on_preflight is not None:
            await on_preflight()
        result = ToolResult.normalized_error(
            call.call_id,
            call.name,
            status,
            message,
        )
        if on_call_completed is None:
            await emit("tool.call.completed", ToolExecutor._result_payload(result))
        else:
            await on_call_completed(call, result)
        return result

    @staticmethod
    def _result_payload(result: ToolResult) -> dict[str, Any]:
        return result.model_dump(mode="json")


_HANDLER_TIMED_OUT = object()


def _consume_handler_completion(handler: asyncio.Future[Any]) -> None:
    if not handler.cancelled():
        handler.exception()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
