from __future__ import annotations

import ast
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSpec,
    PermissionDecision,
    PermissionRequest,
)
from agent_sdk.permissions import PermissionRule
from agent_sdk.storage.memory import InMemoryStore
from examples.quickstart_agent import (
    build_parser,
    define_agent,
    execute_turn,
    select_session,
    summarize_run,
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


def _text_stream(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [{"delta": {"content": text}, "finish_reason": "stop"}]
        }
        yield {
            "choices": [],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        }

    return chunks()


def _tool_stream(
    name: str,
    arguments: dict[str, object],
) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "quickstart-call",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    return chunks()


class WriteThenAnswerProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_: Any) -> object:
        self.calls += 1
        if self.calls == 1:
            return _tool_stream(
                "write",
                {"path": "note.txt", "content": "hello"},
            )
        return _text_stream("finished")


@pytest.mark.asyncio
async def test_execute_turn_resolves_asked_write_and_summarizes_trace(
    tmp_path: Path,
) -> None:
    provider = WriteThenAnswerProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
        permission_rules=(
            PermissionRule(
                outcome="ask",
                tool="write",
                path_prefix=tmp_path.resolve(),
            ),
        ),
    )
    requests: list[PermissionRequest] = []

    async def allow(request: PermissionRequest) -> PermissionDecision:
        requests.append(request)
        return PermissionDecision.allow_once()

    try:
        session = await sdk.sessions.create(workspaces=(tmp_path,))
        agent = sdk.agents.define(
            AgentSpec(
                name="quickstart",
                model="fake/general",
                tool_allowlist=("read", "write", "bash"),
            )
        )

        result = await execute_turn(
            sdk,
            session.session_id,
            agent,
            "write a note",
            resolve_permission=allow,
        )
        summary = await summarize_run(sdk, result)

        assert result.output_text == "finished"
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello"
        assert [request.tool_name for request in requests] == ["write"]
        assert summary.run_id == result.run_id
        assert summary.total_tokens == 3
        assert summary.tools == ("write",)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_execute_turn_can_deny_write(tmp_path: Path) -> None:
    provider = WriteThenAnswerProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
    )

    async def deny(_: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.deny("not this time")

    try:
        session = await sdk.sessions.create(workspaces=(tmp_path,))
        agent = define_agent(sdk, "fake/general")
        result = await execute_turn(
            sdk,
            session.session_id,
            agent,
            "write a note",
            resolve_permission=deny,
        )

        assert result.output_text == "finished"
        assert not (tmp_path / "note.txt").exists()
        assert result.tool_results[0].status.value == "denied"
    finally:
        await sdk.close()
