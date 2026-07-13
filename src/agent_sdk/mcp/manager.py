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
    owner: asyncio.Task[None]
    stop: asyncio.Event
    tools: tuple[RegisteredTool, ...]


@dataclass(frozen=True)
class _PreparedConnection:
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
        self._close_task: asyncio.Task[None] | None = None

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

            ready: asyncio.Future[_PreparedConnection] = (
                asyncio.get_running_loop().create_future()
            )
            ready.add_done_callback(self._future_finished)
            stop = asyncio.Event()
            owner = asyncio.create_task(self._connection_owner(config, ready, stop))
            owner.add_done_callback(self._future_finished)
            registered: list[RegisteredTool] = []
            prepared = False
            try:
                async with asyncio.timeout(config.startup_timeout):
                    connection = await asyncio.shield(ready)
                prepared = True
                self._validate_registration(connection.tools)
                for tool in connection.tools:
                    registered.append(self._registry.register(tool.spec, tool.handler))
                self._connections[config.name] = _Connection(
                    owner=owner,
                    stop=stop,
                    tools=tuple(registered),
                )
                return tuple(tool.spec for tool in registered)
            except asyncio.CancelledError:
                self._rollback(registered)
                await self._settle_failed_owner(owner, stop, prepared=prepared)
                raise
            except AgentSDKError:
                self._rollback(registered)
                await self._settle_failed_owner(owner, stop, prepared=prepared)
                raise
            except Exception as error:
                self._rollback(registered)
                await self._settle_failed_owner(owner, stop, prepared=prepared)
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
            self._closed = True
            if self._close_task is None:
                connections = self._connections
                self._connections = {}
                self._close_task = asyncio.create_task(
                    self._close_connections(connections)
                )
                self._close_task.add_done_callback(self._future_finished)
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _connection_owner(
        self,
        config: MCPServerConfig,
        ready: asyncio.Future[_PreparedConnection],
        stop: asyncio.Event,
    ) -> None:
        try:
            async with self._connector(config) as session:
                initialized = await session.initialize()
                if initialized.protocolVersion != _PROTOCOL_VERSION:
                    raise AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "unsupported MCP protocol version",
                        retryable=False,
                    )
                tools = await self._discover_tools(config.name, session)
                if not ready.done():
                    ready.set_result(_PreparedConnection(tools=tools))
                await stop.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.set_exception(
                    AgentSDKError(
                        ErrorCode.INTERNAL,
                        "failed to connect MCP server",
                        retryable=False,
                    )
                )
            raise
        except Exception as error:
            if not ready.done():
                ready.set_exception(error)
                return
            raise

    async def _close_connections(self, connections: dict[str, _Connection]) -> None:
        ordered = tuple(connections[name] for name in sorted(connections))
        for connection in ordered:
            for tool in connection.tools:
                self._registry.unregister(tool.spec.name, expected=tool)
        for connection in ordered:
            connection.stop.set()

        close_error: Exception | None = None
        for connection in ordered:
            try:
                await connection.owner
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                if close_error is None:
                    close_error = RuntimeError("MCP connection owner was cancelled")
            except Exception as error:
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
    def _future_finished(future: asyncio.Future[Any]) -> None:
        if not future.cancelled():
            future.exception()

    @staticmethod
    async def _settle_failed_owner(
        owner: asyncio.Task[None],
        stop: asyncio.Event,
        *,
        prepared: bool,
    ) -> None:
        if not owner.done():
            if prepared:
                stop.set()
            else:
                owner.cancel()
        cancelled: asyncio.CancelledError | None = None
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError as error:
                if owner.done():
                    break
                if cancelled is None:
                    cancelled = error
            except Exception:
                break
        if not owner.cancelled():
            owner.exception()
        if cancelled is not None:
            raise cancelled


__all__ = ["MCPManager"]
