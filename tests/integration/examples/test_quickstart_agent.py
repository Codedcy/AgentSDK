from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import AgentSDK
from agent_sdk.storage.memory import InMemoryStore
from examples.quickstart_agent import (
    build_parser,
    define_agent,
    select_session,
)


async def _unexpected_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
    raise AssertionError("provider must not be called")


def test_quickstart_uses_only_public_agent_sdk_imports() -> None:
    path = Path(__file__).parents[3] / "examples" / "quickstart_agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("agent_sdk")
    }
    assert modules <= {"agent_sdk", "agent_sdk.permissions"}


def test_parser_supplies_documented_defaults() -> None:
    args = build_parser().parse_args([])

    assert args.model == "openai/gpt-4o-mini"
    assert args.database == Path(".agent-sdk/quickstart.db")
    assert args.workspace == Path(".")
    assert args.session_id is None


@pytest.mark.asyncio
async def test_select_session_creates_then_reopens_same_session(
    tmp_path: Path,
) -> None:
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unexpected_provider,
    )
    try:
        created = await select_session(sdk, tmp_path, None)
        reopened = await select_session(sdk, tmp_path, created.session_id)

        assert reopened.session_id == created.session_id
        assert reopened.workspaces == (str(tmp_path.resolve()),)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_general_agent_exposes_only_workspace_tools(tmp_path: Path) -> None:
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unexpected_provider,
    )
    try:
        agent = define_agent(sdk, "fake/general")

        assert agent.name == "quickstart"
        assert agent.model == "fake/general"
        assert agent.system_prompt is None
        assert agent.tool_allowlist == ("read", "write", "bash")
    finally:
        await sdk.close()
