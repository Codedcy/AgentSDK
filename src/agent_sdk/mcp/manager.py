from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, cast

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, InitializeResult, ListToolsResult
from pydantic import ValidationError as PydanticValidationError

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.mcp.config import (
    MCPServerConfig,
    StdioMCPTransport,
    StreamableHTTPMCPTransport,
)
from agent_sdk.mcp.normalize import normalize_tool
from agent_sdk.tools.models import ToolSpec
from agent_sdk.tools.registry import RegisteredTool, ToolRegistry

_PROTOCOL_VERSION = "2025-11-25"


class _Session(Protocol):
    async def initialize(self) -> InitializeResult: ...

    async def list_tools(self, cursor: str | None = None) -> ListToolsResult: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> CallToolResult: ...


_SessionContext = AbstractAsyncContextManager[_Session]
_SessionConnector = Callable[[MCPServerConfig], _SessionContext]


@dataclass
class _Connection:
    stack: AsyncExitStack
    session: _Session
    tools: tuple[RegisteredTool, ...]


@asynccontextmanager
async def _official_session(config: MCPServerConfig) -> AsyncIterator[_Session]:
    async with AsyncExitStack() as stack:
        transport = config.transport
        if isinstance(transport, StdioMCPTransport):
            parameters = StdioServerParameters(
                command=transport.command,
                args=list(transport.args),
                env=dict(transport.env) or None,
                cwd=transport.cwd,
            )
            streams = await stack.enter_async_context(stdio_client(parameters))
        elif isinstance(transport, StreamableHTTPMCPTransport):
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=dict(transport.headers),
                    timeout=httpx.Timeout(config.request_timeout),
                )
            )
            streams = await stack.enter_async_context(
                streamable_http_client(
                    transport.url,
                    http_client=http_client,
                    terminate_on_close=transport.terminate_on_close,
                )
            )
        else:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "unsupported MCP transport",
                retryable=False,
            )
        read_stream, write_stream = streams[0], streams[1]
        session = ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=config.request_timeout),
        )
        await stack.enter_async_context(session)
        yield cast(_Session, session)


class MCPManager:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._connector: _SessionConnector = _official_session
        self._connections: dict[str, _Connection] = {}
        self._closed = False
        self._lock = asyncio.Lock()

    @classmethod
    def _for_test(
        cls,
        registry: ToolRegistry,
        connector: _SessionConnector,
    ) -> MCPManager:
        manager = cls(registry)
        manager._connector = connector
        return manager

    async def connect(self, config: MCPServerConfig) -> tuple[ToolSpec, ...]:
        async with self._lock:
            self._ensure_open()
            if config.name in self._connections:
                raise AgentSDKError(
                    ErrorCode.CONFLICT,
                    "MCP server already connected",
                    retryable=False,
                )

            stack = AsyncExitStack()
            registered: list[RegisteredTool] = []
            try:
                async with asyncio.timeout(config.startup_timeout):
                    session = await stack.enter_async_context(self._connector(config))
                    initialized = await session.initialize()
                    if initialized.protocolVersion != _PROTOCOL_VERSION:
                        raise AgentSDKError(
                            ErrorCode.INVALID_STATE,
                            "unsupported MCP protocol version",
                            retryable=False,
                        )
                    normalized = await self._discover_tools(config.name, session)
                self._validate_registration(normalized)
                for tool in normalized:
                    registered.append(self._registry.register(tool.spec, tool.handler))
                connection = _Connection(stack, session, tuple(registered))
                self._connections[config.name] = connection
                return tuple(tool.spec for tool in registered)
            except asyncio.CancelledError:
                self._rollback(registered)
                await self._close_stack_safely(stack)
                raise
            except AgentSDKError:
                self._rollback(registered)
                await self._close_stack_safely(stack)
                raise
            except Exception as error:
                self._rollback(registered)
                await self._close_stack_safely(stack)
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "failed to connect MCP server",
                    retryable=False,
                ) from error

    async def register_tools(self, server: str) -> tuple[ToolSpec, ...]:
        """Return the atomically registered catalog for an active server."""
        async with self._lock:
            self._ensure_open()
            try:
                connection = self._connections[server]
            except KeyError as error:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "MCP server not connected",
                    retryable=False,
                ) from error
            return tuple(tool.spec for tool in connection.tools)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            connections = self._connections
            self._connections = {}
            close_error: BaseException | None = None
            for name in sorted(connections):
                connection = connections[name]
                for tool in connection.tools:
                    self._registry.unregister(tool.spec.name, expected=tool)
                try:
                    await connection.stack.aclose()
                except BaseException as error:
                    if close_error is None:
                        close_error = error
            if close_error is not None:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "failed to close MCP manager",
                    retryable=False,
                ) from close_error

    async def _discover_tools(
        self,
        server: str,
        session: _Session,
    ) -> tuple[RegisteredTool, ...]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        normalized: list[RegisteredTool] = []
        names: set[str] = set()
        while True:
            page = await session.list_tools(cursor=cursor)
            for remote in page.tools:
                try:
                    tool = normalize_tool(server, remote, session)
                except (PydanticValidationError, TypeError, ValueError) as error:
                    raise AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "MCP tool catalog is invalid",
                        retryable=False,
                    ) from error
                if tool.spec.name in names:
                    raise AgentSDKError(
                        ErrorCode.CONFLICT,
                        "duplicate MCP tool name",
                        retryable=False,
                    )
                names.add(tool.spec.name)
                normalized.append(tool)
            next_cursor = page.nextCursor
            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "MCP tool pagination cursor repeated",
                    retryable=False,
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return tuple(sorted(normalized, key=lambda tool: tool.spec.name))

    def _validate_registration(self, tools: tuple[RegisteredTool, ...]) -> None:
        temporary = ToolRegistry()
        for tool in tools:
            temporary.register(tool.spec, tool.handler)
        registered_names = {spec.name for spec in self._registry.list()}
        if any(tool.spec.name in registered_names for tool in tools):
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "tool already registered",
                retryable=False,
            )

    def _rollback(self, registered: list[RegisteredTool]) -> None:
        for tool in reversed(registered):
            self._registry.unregister(tool.spec.name, expected=tool)

    def _ensure_open(self) -> None:
        if self._closed:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "MCP manager is closed",
                retryable=False,
            )

    @staticmethod
    async def _close_stack_safely(stack: AsyncExitStack) -> None:
        try:
            await stack.aclose()
        except BaseException:
            pass


__all__ = ["MCPManager"]
