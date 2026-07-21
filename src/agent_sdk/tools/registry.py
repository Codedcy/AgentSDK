from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.tools.models import ToolContext, ToolSpec, thaw_json

ToolHandler: TypeAlias = Callable[..., Awaitable[Any]]
PermissionArgumentsResolver: TypeAlias = Callable[
    [ToolContext, Mapping[str, Any]],
    Awaitable[Mapping[str, Any]],
]


def builtin_permission_argument_names(spec: ToolSpec) -> tuple[str, ...]:
    if spec.source != "builtin":
        return ()
    if spec.effects in {("filesystem.read",), ("filesystem.write",)}:
        return ("path",)
    if spec.effects == ("process.execute",):
        return ("cwd",)
    return ()


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler
    permission_arguments: PermissionArgumentsResolver | None = None
    permission_argument_names: tuple[str, ...] = ()


class ToolCatalog:
    """An immutable, per-run view of registered tools."""

    def __init__(self, registered: tuple[RegisteredTool, ...]) -> None:
        self._registered = {item.spec.name: item for item in registered}

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._registered[name]
        except KeyError as error:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "tool not found in run capability",
                retryable=False,
            ) from error

    def list(self) -> tuple[ToolSpec, ...]:
        return tuple(self._registered[name].spec for name in sorted(self._registered))

    def schemas(self) -> tuple[dict[str, Any], ...]:
        return _tool_schemas(self.list())


class ToolRegistry:
    def __init__(self) -> None:
        self._registered: dict[str, RegisteredTool] = {}

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        permission_arguments: PermissionArgumentsResolver | None = None,
        permission_argument_names: tuple[str, ...] = (),
    ) -> RegisteredTool:
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
        if bool(permission_arguments) != bool(permission_argument_names):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "tool permission binding is invalid",
                retryable=False,
            )
        if (
            len(set(permission_argument_names)) != len(permission_argument_names)
            or any(not name for name in permission_argument_names)
            or (
                permission_argument_names
                and permission_argument_names
                != builtin_permission_argument_names(spec)
            )
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "tool permission binding is invalid",
                retryable=False,
            )
        registered = RegisteredTool(
            spec,
            handler,
            permission_arguments,
            permission_argument_names,
        )
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

    def select(self, names: Iterable[str] | None) -> ToolCatalog:
        selected_names = (
            tuple(sorted(self._registered))
            if names is None
            else tuple(sorted(set(names)))
        )
        return ToolCatalog(tuple(self.get(name) for name in selected_names))

    def list(self) -> tuple[ToolSpec, ...]:
        return tuple(self._registered[name].spec for name in sorted(self._registered))

    def schemas(self) -> tuple[dict[str, Any], ...]:
        return _tool_schemas(self.list())


def _tool_schemas(specs: tuple[ToolSpec, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": thaw_json(spec.input_schema),
                },
            }
            for spec in specs
    )


__all__ = [
    "PermissionArgumentsResolver",
    "RegisteredTool",
    "ToolCatalog",
    "ToolHandler",
    "ToolRegistry",
    "ToolContext",
    "builtin_permission_argument_names",
]
