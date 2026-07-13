from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import types as mcp_types
from pydantic import ValidationError

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    PermissionDecision,
    RunStatus,
    ToolContext,
    ToolRegistry,
    ToolResultStatus,
    ToolSpec,
)
from agent_sdk.mcp import (
    MCPManager,
    MCPServerConfig,
    StdioMCPTransport,
    StreamableHTTPMCPTransport,
)
from agent_sdk.storage.memory import InMemoryStore
import agent_sdk.mcp.manager as manager_module


PROTOCOL_VERSION = "2025-11-25"


def _remote_tool(
    name: str,
    *,
    schema: dict[str, Any] | None = None,
    description: str | None = None,
) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description=description,
        inputSchema=schema
        or {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )


@dataclass
class FakeMCPSession:
    pages: Mapping[str | None, mcp_types.ListToolsResult]
    protocol_version: str = PROTOCOL_VERSION
    result_factory: Callable[[], mcp_types.CallToolResult] = field(
        default=lambda: mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="ok")]
        )
    )
    initialize_calls: int = 0
    cursors: list[str | None] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    closed: bool = False
    registry_at_close: tuple[str, ...] | None = None
    close_registry: ToolRegistry | None = None

    async def initialize(self) -> Any:
        self.initialize_calls += 1
        return type("InitializeResult", (), {"protocolVersion": self.protocol_version})()

    async def list_tools(self, cursor: str | None = None) -> mcp_types.ListToolsResult:
        self.cursors.append(cursor)
        return self.pages[cursor]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **_: Any,
    ) -> mcp_types.CallToolResult:
        self.calls.append((name, dict(arguments or {})))
        return self.result_factory()

    def connector(self, _: MCPServerConfig) -> Any:
        session = self

        @asynccontextmanager
        async def connected() -> AsyncIterator[FakeMCPSession]:
            try:
                yield session
            finally:
                if session.close_registry is not None:
                    session.registry_at_close = tuple(
                        spec.name for spec in session.close_registry.list()
                    )
                session.closed = True

        return connected()


def _one_page(*tools: mcp_types.Tool) -> dict[None, mcp_types.ListToolsResult]:
    return {None: mcp_types.ListToolsResult(tools=list(tools))}


async def _application_handler(_: ToolContext, **__: Any) -> str:
    return "application"


def test_mcp_configs_are_strict_frozen_and_detach_nested_values() -> None:
    args = ["--flag"]
    env = {"TOKEN": "secret"}
    headers = {"Authorization": "Bearer token"}
    stdio = StdioMCPTransport(command="server", args=args, env=env)
    http = StreamableHTTPMCPTransport(url="https://example.test/mcp", headers=headers)
    config = MCPServerConfig(name="demo-server", transport=stdio)

    args.append("external")
    env["TOKEN"] = "changed"
    headers["Authorization"] = "changed"

    assert stdio.args == ("--flag",)
    assert stdio.env == {"TOKEN": "secret"}
    assert http.headers == {"Authorization": "Bearer token"}
    assert config.transport.type == "stdio"
    with pytest.raises(ValidationError):
        MCPServerConfig.model_validate(
            {"name": "demo", "transport": {"type": "sse", "url": "https://x"}}
        )
    with pytest.raises(ValidationError):
        MCPServerConfig(name="Bad.Name", transport=stdio)
    with pytest.raises(ValidationError):
        MCPServerConfig(name="demo", transport=stdio, request_timeout=0)
    with pytest.raises(ValidationError):
        StdioMCPTransport(command="server", unexpected=True)
    with pytest.raises(ValidationError):
        StreamableHTTPMCPTransport(url="file:///tmp/socket")


def test_mcp_config_model_copy_revalidates_and_detaches_updates() -> None:
    args = ["--one"]
    env = {"A": "one"}
    headers = {"X-Test": "one"}
    stdio = StdioMCPTransport(command="server").model_copy(
        update={"args": args, "env": env}
    )
    http = StreamableHTTPMCPTransport(url="https://example.test/mcp").model_copy(
        update={"headers": headers}
    )
    config = MCPServerConfig(
        name="demo", transport=StdioMCPTransport(command="server")
    )

    args.append("--external")
    env["A"] = "external"
    headers["X-Test"] = "external"

    assert stdio.args == ("--one",)
    assert stdio.env == {"A": "one"}
    assert http.headers == {"X-Test": "one"}
    with pytest.raises(ValidationError):
        config.model_copy(update={"request_timeout": 0})


@pytest.mark.asyncio
async def test_mcp_tool_is_namespaced_paginated_and_registered_in_order() -> None:
    registry = ToolRegistry()
    session = FakeMCPSession(
        {
            None: mcp_types.ListToolsResult(
                tools=[_remote_tool("zeta")], nextCursor="page-2"
            ),
            "page-2": mcp_types.ListToolsResult(tools=[_remote_tool("alpha")]),
        }
    )
    manager = MCPManager._for_test(registry, session.connector)

    specs = await manager.connect(
        MCPServerConfig(
            name="demo",
            transport=StdioMCPTransport(command="ignored"),
        )
    )

    assert session.initialize_calls == 1
    assert session.cursors == [None, "page-2"]
    assert [spec.name for spec in specs] == ["mcp.demo.alpha", "mcp.demo.zeta"]
    assert [spec.name for spec in registry.list()] == ["mcp.demo.alpha", "mcp.demo.zeta"]
    assert registry.get("mcp.demo.alpha").spec.source == "mcp:demo"
    await manager.close()


@pytest.mark.asyncio
async def test_protocol_mismatch_rolls_back_connection_and_manager_can_retry() -> None:
    registry = ToolRegistry()
    bad = FakeMCPSession(_one_page(_remote_tool("echo")), protocol_version="2025-06-18")
    manager = MCPManager._for_test(registry, bad.connector)

    with pytest.raises(AgentSDKError) as raised:
        await manager.connect(
            MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
        )

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "unsupported MCP protocol version"
    assert bad.closed is True
    assert registry.list() == ()

    good = FakeMCPSession(_one_page(_remote_tool("echo")))
    manager._connector = good.connector  # type: ignore[attr-defined]
    await manager.connect(
        MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
    )
    assert registry.get("mcp.demo.echo").spec.source == "mcp:demo"
    await manager.close()


@pytest.mark.parametrize(
    "tools",
    [
        (_remote_tool("echo"), _remote_tool("echo")),
        (
            _remote_tool("valid"),
            _remote_tool(
                "invalid",
                schema={"type": "object", "properties": {"x": {"type": "nope"}}},
            ),
        ),
    ],
    ids=["duplicate-remote-name", "invalid-remote-schema"],
)
@pytest.mark.asyncio
async def test_invalid_remote_catalog_is_failure_atomic(
    tools: tuple[mcp_types.Tool, ...],
) -> None:
    registry = ToolRegistry()
    session = FakeMCPSession(_one_page(*tools))
    manager = MCPManager._for_test(registry, session.connector)

    with pytest.raises(AgentSDKError):
        await manager.connect(
            MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
        )

    assert registry.list() == ()
    assert session.closed is True


@pytest.mark.asyncio
async def test_non_json_remote_schema_is_a_stable_invalid_catalog_failure() -> None:
    registry = ToolRegistry()
    remote = _remote_tool("invalid", schema={"type": "object", "default": object()})
    session = FakeMCPSession(_one_page(remote))
    manager = MCPManager._for_test(registry, session.connector)

    with pytest.raises(AgentSDKError) as raised:
        await manager.connect(
            MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
        )

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "MCP tool catalog is invalid"
    assert registry.list() == ()
    assert session.closed is True


@pytest.mark.asyncio
async def test_duplicate_server_or_application_tool_never_leaks_connection() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="mcp.demo.echo",
            description="owned by application",
            input_schema={"type": "object"},
        ),
        _application_handler,
    )
    conflicting = FakeMCPSession(_one_page(_remote_tool("echo")))
    manager = MCPManager._for_test(registry, conflicting.connector)

    with pytest.raises(AgentSDKError) as raised:
        await manager.connect(
            MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
        )
    assert raised.value.code is ErrorCode.CONFLICT
    assert registry.get("mcp.demo.echo").spec.source == "application"
    assert conflicting.closed is True

    clean_registry = ToolRegistry()
    first = FakeMCPSession(_one_page(_remote_tool("echo")))
    second = FakeMCPSession(_one_page(_remote_tool("other")))
    duplicate_manager = MCPManager._for_test(clean_registry, first.connector)
    config = MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
    await duplicate_manager.connect(config)
    duplicate_manager._connector = second.connector  # type: ignore[attr-defined]
    with pytest.raises(AgentSDKError) as duplicate:
        await duplicate_manager.connect(config)
    assert duplicate.value.code is ErrorCode.CONFLICT
    assert second.initialize_calls == 0
    await duplicate_manager.close()


@pytest.mark.asyncio
async def test_partial_shared_registration_failure_rolls_back_by_identity() -> None:
    class FailsSecondRegistration(ToolRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.remote_registration_calls = 0

        def register(self, spec: ToolSpec, handler: Any) -> Any:
            if spec.source.startswith("mcp:"):
                self.remote_registration_calls += 1
                if self.remote_registration_calls == 2:
                    raise AgentSDKError(
                        ErrorCode.INTERNAL,
                        "injected registry failure",
                        retryable=False,
                    )
            return super().register(spec, handler)

    registry = FailsSecondRegistration()
    session = FakeMCPSession(
        _one_page(_remote_tool("alpha"), _remote_tool("zeta"))
    )
    manager = MCPManager._for_test(registry, session.connector)

    with pytest.raises(AgentSDKError, match="injected registry failure"):
        await manager.connect(
            MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
        )

    assert registry.list() == ()
    assert session.closed is True


@pytest.mark.asyncio
async def test_mcp_result_is_detached_json_without_raw_meta() -> None:
    structured = {"answer": {"items": [1, 2]}, "_meta": {"secret": "raw"}}
    remote_result = mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(
                type="text",
                text="hello",
                _meta={"secret": "raw"},
            )
        ],
        structuredContent=structured,
    )
    registry = ToolRegistry()
    session = FakeMCPSession(
        _one_page(_remote_tool("echo")), result_factory=lambda: remote_result
    )
    manager = MCPManager._for_test(registry, session.connector)
    await manager.connect(
        MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
    )

    value = await registry.get("mcp.demo.echo").handler(
        ToolContext(run_id="run_test", session_id="ses_test"), text="hello"
    )
    remote_result.content[0].text = "mutated"
    assert remote_result.structuredContent is not None
    remote_result.structuredContent["answer"]["items"].append(3)

    assert value == {
        "content": [{"type": "text", "text": "hello"}],
        "structuredContent": {"answer": {"items": [1, 2]}},
    }
    assert session.calls == [("echo", {"text": "hello"})]
    await manager.close()


def _tool_call_chunks(name: str) -> tuple[dict[str, Any], ...]:
    return (
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_mcp",
                                "function": {"name": name, "arguments": '{"text":"hi"}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_mcp_tool_still_publishes_local_permission_before_remote_call() -> None:
    requests: list[dict[str, Any]] = []

    async def model(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        requests.append(kwargs)

        async def chunks() -> AsyncIterator[dict[str, Any]]:
            if len(requests) == 1:
                for chunk in _tool_call_chunks("mcp.demo.echo"):
                    yield chunk
            else:
                yield {
                    "choices": [
                        {"delta": {"content": "done"}, "finish_reason": "stop"}
                    ]
                }

        return chunks()

    sdk = AgentSDK.for_test(
        store=InMemoryStore(), acompletion=model, permission_default="ask"
    )
    session = FakeMCPSession(_one_page(_remote_tool("echo")))
    manager = MCPManager._for_test(sdk.tools, session.connector)
    try:
        await manager.connect(
            MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
        )
        created = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            created.session_id,
            AgentSpec(name="test", model="fake/model"),
            "use echo",
        )

        permission = await asyncio.wait_for(
            sdk.permissions.next_request(run.run_id), timeout=1
        )
        assert permission.tool_name == "mcp.demo.echo"
        assert session.calls == []
        assert (await sdk.runs.get(run.run_id)).status is RunStatus.WAITING_PERMISSION

        await sdk.permissions.resolve(permission.request_id, PermissionDecision.allow_once())
        result = await asyncio.wait_for(run.result(), timeout=1)
        assert session.calls == [("echo", {"text": "hi"})]
        assert result.tool_results[0].status is ToolResultStatus.SUCCEEDED
    finally:
        await manager.close()
        await sdk.close()


@pytest.mark.parametrize("outcome", ["is_error", "exception"])
@pytest.mark.asyncio
async def test_mcp_remote_failures_are_sanitized_by_local_executor(outcome: str) -> None:
    model_calls = 0

    async def model(**_: Any) -> AsyncIterator[dict[str, Any]]:
        nonlocal model_calls
        model_calls += 1

        async def chunks() -> AsyncIterator[dict[str, Any]]:
            if model_calls == 1:
                for chunk in _tool_call_chunks("mcp.demo.echo"):
                    yield chunk
            else:
                yield {
                    "choices": [
                        {"delta": {"content": "handled"}, "finish_reason": "stop"}
                    ]
                }

        return chunks()

    def result_factory() -> mcp_types.CallToolResult:
        if outcome == "exception":
            raise RuntimeError("transport bearer token secret")
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="remote secret")],
            isError=True,
        )

    sdk = AgentSDK.for_test(
        store=InMemoryStore(), acompletion=model, permission_default="allow"
    )
    session = FakeMCPSession(
        _one_page(_remote_tool("echo")), result_factory=result_factory
    )
    manager = MCPManager._for_test(sdk.tools, session.connector)
    try:
        await manager.connect(
            MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
        )
        created = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            created.session_id,
            AgentSpec(name="test", model="fake/model"),
            "use echo",
        )
        result = await asyncio.wait_for(run.result(), timeout=1)
        tool_result = result.tool_results[0]
        assert tool_result.status is ToolResultStatus.FAILED
        assert tool_result.error == "tool handler failed"
        assert "secret" not in tool_result.content
    finally:
        await manager.close()
        await sdk.close()


@pytest.mark.asyncio
async def test_close_unregisters_before_transport_close_is_identity_safe_and_idempotent() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="application", description="app", input_schema={"type": "object"}),
        _application_handler,
    )
    session = FakeMCPSession(_one_page(_remote_tool("echo")), close_registry=registry)
    manager = MCPManager._for_test(registry, session.connector)
    await manager.connect(
        MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
    )

    owned = registry.get("mcp.demo.echo")
    assert registry.unregister("mcp.demo.echo", expected=owned) is True
    registry.register(
        ToolSpec(
            name="mcp.demo.echo",
            description="replacement",
            input_schema={"type": "object"},
        ),
        _application_handler,
    )

    await asyncio.gather(manager.close(), manager.close())
    await manager.close()

    assert session.registry_at_close == ("application", "mcp.demo.echo")
    assert registry.get("mcp.demo.echo").spec.source == "application"
    assert registry.get("application").spec.source == "application"
    with pytest.raises(AgentSDKError) as closed:
        await manager.connect(
            MCPServerConfig(name="other", transport=StdioMCPTransport(command="ignored"))
        )
    assert closed.value.code is ErrorCode.INVALID_STATE
    assert closed.value.message == "MCP manager is closed"


@pytest.mark.asyncio
async def test_close_removes_owned_tools_before_transport_becomes_unusable() -> None:
    registry = ToolRegistry()
    session = FakeMCPSession(_one_page(_remote_tool("echo")), close_registry=registry)
    manager = MCPManager._for_test(registry, session.connector)
    await manager.connect(
        MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored"))
    )

    await manager.close()

    assert session.registry_at_close == ()
    assert registry.list() == ()


class _OfficialSessionDouble(FakeMCPSession):
    instances: list[_OfficialSessionDouble] = []

    def __init__(self, read: object, write: object, **kwargs: Any) -> None:
        super().__init__(_one_page(_remote_tool("echo")))
        self.read = read
        self.write = write
        self.kwargs = kwargs
        self.entered = False
        type(self).instances.append(self)

    async def __aenter__(self) -> _OfficialSessionDouble:
        self.entered = True
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_default_stdio_connector_uses_official_sdk_contexts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport_calls: list[Any] = []
    transport_closed = False

    def fake_stdio(parameters: Any) -> Any:
        transport_calls.append(parameters)

        @asynccontextmanager
        async def connected() -> AsyncIterator[tuple[object, object]]:
            nonlocal transport_closed
            try:
                yield ("read", "write")
            finally:
                transport_closed = True

        return connected()

    _OfficialSessionDouble.instances.clear()
    monkeypatch.setattr(manager_module, "stdio_client", fake_stdio)
    monkeypatch.setattr(manager_module, "ClientSession", _OfficialSessionDouble)
    manager = MCPManager(ToolRegistry())
    await manager.connect(
        MCPServerConfig(
            name="demo",
            request_timeout=4,
            transport=StdioMCPTransport(
                command="server",
                args=["--flag"],
                env={"A": "B"},
                cwd=tmp_path,
            ),
        )
    )

    parameters = transport_calls[0]
    session = _OfficialSessionDouble.instances[0]
    assert parameters.command == "server"
    assert parameters.args == ["--flag"]
    assert parameters.env == {"A": "B"}
    assert parameters.cwd == tmp_path
    assert session.kwargs["read_timeout_seconds"] == timedelta(seconds=4)
    assert session.entered is True
    await manager.close()
    assert session.closed is True
    assert transport_closed is True


@pytest.mark.asyncio
async def test_default_http_connector_uses_official_streamable_http_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_options: list[dict[str, Any]] = []
    stream_options: list[tuple[str, object, bool]] = []

    class FakeHTTPClient:
        def __init__(self, **kwargs: Any) -> None:
            client_options.append(kwargs)

        async def __aenter__(self) -> FakeHTTPClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    def fake_stream(url: str, *, http_client: object, terminate_on_close: bool) -> Any:
        stream_options.append((url, http_client, terminate_on_close))

        @asynccontextmanager
        async def connected() -> AsyncIterator[tuple[object, object, Callable[[], None]]]:
            yield ("read", "write", lambda: None)

        return connected()

    _OfficialSessionDouble.instances.clear()
    monkeypatch.setattr(manager_module.httpx, "AsyncClient", FakeHTTPClient)
    monkeypatch.setattr(manager_module, "streamable_http_client", fake_stream)
    monkeypatch.setattr(manager_module, "ClientSession", _OfficialSessionDouble)
    manager = MCPManager(ToolRegistry())
    await manager.connect(
        MCPServerConfig(
            name="demo",
            request_timeout=7,
            transport=StreamableHTTPMCPTransport(
                url="https://example.test/mcp",
                headers={"Authorization": "Bearer token"},
                terminate_on_close=False,
            ),
        )
    )

    assert client_options[0]["headers"] == {"Authorization": "Bearer token"}
    assert client_options[0]["timeout"].connect == 7
    assert stream_options[0][0] == "https://example.test/mcp"
    assert stream_options[0][1].__class__ is FakeHTTPClient
    assert stream_options[0][2] is False
    await manager.close()


@pytest.mark.asyncio
async def test_default_connector_rejects_model_constructed_unknown_transport() -> None:
    config = MCPServerConfig.model_construct(
        name="demo",
        transport=object(),
        startup_timeout=1,
        request_timeout=1,
    )
    manager = MCPManager(ToolRegistry())

    with pytest.raises(AgentSDKError) as raised:
        await manager.connect(config)

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "unsupported MCP transport"
