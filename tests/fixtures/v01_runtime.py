from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_sdk import AgentSDK


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


@dataclass
class V01Harness:
    database_path: Path
    workspace: Path
    acompletion: Callable[..., Awaitable[object]]

    def open(self) -> AgentSDK:
        return AgentSDK.for_test(
            database_path=self.database_path,
            acompletion=self.acompletion,
        )

    def reopen(
        self,
        acompletion: Callable[..., Awaitable[object]] | None = None,
    ) -> AgentSDK:
        return AgentSDK.for_test(
            database_path=self.database_path,
            acompletion=acompletion or self.acompletion,
        )


@pytest.fixture
def v01_harness(tmp_path: Path) -> V01Harness:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def acompletion(**_: object) -> object:
        return _text_stream("baseline complete")

    return V01Harness(
        database_path=tmp_path / "agent-sdk.sqlite3",
        workspace=workspace,
        acompletion=acompletion,
    )
