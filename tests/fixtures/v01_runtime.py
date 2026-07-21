from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_sdk import AgentSDK, AgentSpec, ToolContext, ToolSpec
from agent_sdk.permissions import PermissionRule


def _text_stream(text: str) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {"delta": {"content": text}, "finish_reason": "stop"}
            ]
        }
        yield {
            "choices": [],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        }

    return generate()


def _tool_stream(
    *,
    call_id: str,
    name: str,
    arguments: dict[str, object],
) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
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

    return generate()


@dataclass
class V01Harness:
    database_path: Path
    workspace: Path
    outside_file: Path
    acompletion: Callable[..., Awaitable[object]]

    def open(self) -> AgentSDK:
        return AgentSDK.for_test(
            database_path=self.database_path,
            acompletion=self.acompletion,
            permission_default="allow",
            permission_rules=(PermissionRule(outcome="ask", tool="read"),),
        )

    def reopen(
        self,
        acompletion: Callable[..., Awaitable[object]] | None = None,
    ) -> AgentSDK:
        return AgentSDK.for_test(
            database_path=self.database_path,
            acompletion=acompletion or self.acompletion,
            permission_default="allow",
            permission_rules=(PermissionRule(outcome="ask", tool="read"),),
        )


@pytest.fixture
def v01_harness(tmp_path: Path) -> V01Harness:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside fixture", encoding="utf-8")
    model_calls = 0

    async def acompletion(**_: object) -> object:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return _tool_stream(
                call_id="call-app-echo",
                name="app_echo",
                arguments={"text": "application tool complete"},
            )
        if model_calls == 2:
            return _tool_stream(
                call_id="call-write",
                name="write",
                arguments={
                    "path": "generated.txt",
                    "content": "created by builtin write",
                },
            )
        if model_calls == 3:
            return _tool_stream(
                call_id="call-read",
                name="read",
                arguments={"path": "keep.txt"},
            )
        if model_calls == 4:
            return _tool_stream(
                call_id="call-bash",
                name="bash",
                arguments={
                    "argv": [
                        sys.executable,
                        "-c",
                        "print('builtin bash complete')",
                    ]
                },
            )
        if model_calls == 5:
            return _tool_stream(
                call_id="call-outside-write",
                name="write",
                arguments={
                    "path": str(outside_file),
                    "content": "must not be written",
                    "overwrite": True,
                },
            )
        if model_calls == 6:
            return _tool_stream(
                call_id="call-mcp-echo",
                name="mcp.demo.echo",
                arguments={"text": "mcp tool complete"},
            )
        return _text_stream("baseline complete")

    return V01Harness(
        database_path=tmp_path / "agent-sdk.sqlite3",
        workspace=workspace,
        outside_file=outside_file,
        acompletion=acompletion,
    )


async def _seed_interrupted_tool(database_path: Path) -> None:
    async def provider(**_: object) -> object:
        return _tool_stream(
            call_id="call-external-effect",
            name="external_effect",
            arguments={"value": 7},
        )

    async def external_effect(context: ToolContext, *, value: int) -> object:
        print(
            json.dumps(
                {
                    "run_id": context.run_id,
                    "status": "tool_in_flight",
                    "value": value,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    sdk = AgentSDK.for_test(
        database_path=database_path,
        acompletion=provider,
        permission_default="allow",
    )
    sdk.tools.register(
        ToolSpec(
            name="external_effect",
            description="Block until the fixture process is terminated",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            version="1",
            source="application",
            effects=("external.write",),
        ),
        external_effect,
    )
    session = await sdk.sessions.create(workspaces=())
    agent = sdk.agents.define(
        AgentSpec(name="recovery", revision="1", model="test/recovery")
    )
    await sdk.runs.start(session.session_id, agent, "perform external effect")
    await asyncio.Event().wait()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--seed-interrupted-tool":
        raise SystemExit("usage: v01_runtime --seed-interrupted-tool DATABASE")
    asyncio.run(_seed_interrupted_tool(Path(sys.argv[2])))
