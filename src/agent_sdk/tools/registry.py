from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.tools.models import ToolContext, ToolSpec, thaw_json

ToolHandler: TypeAlias = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._registered: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> RegisteredTool:
        if spec.name in self._registered:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "tool already registered",
                retryable=False,
            )
        try:
            Draft202012Validator.check_schema(thaw_json(spec.input_schema))
        except SchemaError as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "tool schema is invalid",
                retryable=False,
            ) from error
        registered = RegisteredTool(spec, handler)
        self._registered[spec.name] = registered
        return registered

    def unregister(
        self,
        name: str,
        *,
        expected: RegisteredTool | None = None,
    ) -> bool:
        registered = self._registered.get(name)
        if registered is None or (expected is not None and registered is not expected):
            return False
        del self._registered[name]
        return True

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._registered[name]
        except KeyError as error:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "tool not found",
                retryable=False,
            ) from error

    def list(self) -> tuple[ToolSpec, ...]:
        return tuple(self._registered[name].spec for name in sorted(self._registered))

    def schemas(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": thaw_json(spec.input_schema),
                },
            }
            for spec in self.list()
        )


__all__ = ["RegisteredTool", "ToolHandler", "ToolRegistry", "ToolContext"]
