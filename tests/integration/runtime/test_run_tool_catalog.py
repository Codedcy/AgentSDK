from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import AgentSDK, AgentSDKError, AgentSpec, ErrorCode
from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.tools.models import ToolContext, ToolSpec
from agent_sdk.tools.registry import ToolRegistry


def _text_stream(text: str) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {"delta": {"content": text}, "finish_reason": "stop"}
            ]
        }

    return generate()


def _tool(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object", "additionalProperties": False},
        source="test",
        effects=(),
    )


def _tool_stream(name: str) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_denied_catalog",
                                "function": {"name": name, "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    return generate()


async def _handler(_: ToolContext) -> dict[str, bool]:
    return {"ok": True}


@pytest.mark.asyncio
async def test_run_persists_and_uses_only_its_effective_tool_catalog(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> AsyncIterator[dict[str, object]]:
        requests.append(kwargs)
        return _text_stream("done")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        enable_builtin_tools=False,
    )
    sdk.tools.register(_tool("write"), _handler)
    sdk.tools.register(_tool("read"), _handler)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        session = await sdk.sessions.create(workspaces=(workspace,))
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(
                name="restricted",
                model="test/model",
                tool_allowlist=("read", "read"),
                workspace_allowlist=(str(workspace),),
            ),
            "go",
        )

        assert (await handle.result()).output_text == "done"
        run = await sdk.runs.get(handle.run_id)
        assert run.execution_descriptor is not None
        assert tuple(tool.spec.name for tool in run.execution_descriptor.tools) == ("read",)
        assert run.execution_descriptor.workspace_scopes == (str(workspace.resolve()),)
        assert [tool["function"]["name"] for tool in requests[0]["tools"]] == ["read"]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_run_creation_rejects_unknown_explicit_tool_capability() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _text_stream("unused")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        enable_builtin_tools=False,
    )
    sdk.tools.register(_tool("read"), _handler)
    try:
        session = await sdk.sessions.create(workspaces=())
        with pytest.raises(AgentSDKError) as error:
            await sdk.runs.start(
                session.session_id,
                AgentSpec(
                    name="unknown-tool",
                    model="test/model",
                    tool_allowlist=("missing",),
                ),
                "go",
            )
        assert error.value.code is ErrorCode.NOT_FOUND
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_descriptor_selected_catalog_blocks_a_model_requested_global_tool() -> None:
    calls = 0

    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        calls += 1
        return _tool_stream("write") if calls == 1 else _text_stream("done")

    invoked = 0

    async def write_handler(_: ToolContext) -> dict[str, bool]:
        nonlocal invoked
        invoked += 1
        return {"ok": True}

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    sdk.tools.register(_tool("read"), _handler)
    sdk.tools.register(_tool("write"), write_handler)
    try:
        session = await sdk.sessions.create(workspaces=())
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(
                name="selected-only",
                model="test/model",
                tool_allowlist=("read",),
            ),
            "go",
        )

        result = await handle.result()

        assert result.output_text == "done"
        assert invoked == 0
        assert result.tool_results[0].tool_name == "write"
        assert result.tool_results[0].status.value == "failed"
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_unrelated_tool_registered_after_run_creation_does_not_break_execution() -> None:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _text_stream("done")

    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=())
    registry = ToolRegistry()
    registry.register(_tool("read"), _handler)
    selected = registry.select(("read",))
    agent = AgentSpec(name="stable-catalog", model="test/model")
    descriptor = ExecutionDescriptor.create(
        agent=agent,
        messages=({"role": "user", "content": "go"},),
        tools=tuple(ToolCapabilityDescriptor.from_spec(spec) for spec in selected.list()),
        workspace_scopes=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    )
    run = await commands.start_run(
        session.session_id,
        agent_revision="stable-catalog:1",
        user_input="go",
        execution_descriptor=descriptor,
    )
    registry.register(_tool("write"), _handler)

    result = await RunEngine(
        store,
        LiteLLMGateway._for_test(provider),
        tools=registry,
        policy=PolicyEngine("allow"),
    ).execute(
        run.value.run_id,
        ModelRequest(
            model="test/model",
            messages=({"role": "user", "content": "go"},),
            tools=selected.schemas(),
        ),
    )

    assert result.output_text == "done"


@pytest.mark.asyncio
async def test_explicit_empty_capabilities_are_persisted_not_inherited(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> AsyncIterator[dict[str, object]]:
        requests.append(kwargs)
        return _text_stream("done")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        enable_builtin_tools=False,
    )
    sdk.tools.register(_tool("read"), _handler)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        session = await sdk.sessions.create(workspaces=(workspace,))
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(
                name="none",
                model="test/model",
                tool_allowlist=(),
                workspace_allowlist=(),
            ),
            "go",
        )

        await handle.result()
        run = await sdk.runs.get(handle.run_id)
        assert run.execution_descriptor is not None
        assert run.execution_descriptor.tools == ()
        assert run.execution_descriptor.workspace_scopes == ()
        assert requests[0]["tools"] == []
    finally:
        await sdk.close()
