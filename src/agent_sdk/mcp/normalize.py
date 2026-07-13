from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from mcp import types as mcp_types
from pydantic import BaseModel

from agent_sdk.tools.models import ToolContext, ToolSpec, freeze_json, thaw_json
from agent_sdk.tools.registry import RegisteredTool


class _ToolSession(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> mcp_types.CallToolResult: ...


class MCPToolCallFailed(RuntimeError):
    """Internal marker normalized by the local ToolExecutor."""


def _without_mcp_meta(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_mcp_meta(item)
            for key, item in value.items()
            if key != "_meta"
        }
    if isinstance(value, (list, tuple)):
        return [_without_mcp_meta(item) for item in value]
    return value


def _detached_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    cleaned = _without_mcp_meta(value)
    return thaw_json(freeze_json(cleaned))


def normalize_mcp_content(
    content: Sequence[mcp_types.ContentBlock],
    structured_content: Mapping[str, Any] | None,
) -> dict[str, Any]:
    normalized_content = [_detached_json(item) for item in content]
    normalized_structured = (
        None if structured_content is None else _detached_json(structured_content)
    )
    return {
        "content": normalized_content,
        "structuredContent": normalized_structured,
    }


def normalize_tool(
    server: str,
    remote: mcp_types.Tool,
    session: _ToolSession,
) -> RegisteredTool:
    remote_name = remote.name

    async def invoke(_: ToolContext, **arguments: Any) -> Any:
        result = await session.call_tool(name=remote_name, arguments=dict(arguments))
        if result.isError:
            raise MCPToolCallFailed("remote MCP tool reported failure")
        return normalize_mcp_content(result.content, result.structuredContent)

    spec = ToolSpec(
        name=f"mcp.{server}.{remote_name}",
        description=remote.description or remote_name,
        input_schema=remote.inputSchema,
        source=f"mcp:{server}",
    )
    return RegisteredTool(spec=spec, handler=invoke)


__all__ = ["normalize_mcp_content", "normalize_tool"]
