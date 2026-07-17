from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    PermissionDecision,
    RunStatus,
    ToolResultStatus,
)
from agent_sdk.tools.models import thaw_json

if TYPE_CHECKING:
    from tests.fixtures.v01_runtime import V01Harness


pytest_plugins = ("tests.fixtures.v01_runtime",)


@pytest.mark.asyncio
async def test_v01_release_baseline_reopens_and_deletes_history(
    v01_harness: V01Harness,
) -> None:
    workspace_file = v01_harness.workspace / "keep.txt"
    workspace_file.write_text("application-owned", encoding="utf-8")

    sdk: AgentSDK = v01_harness.open()
    session = await sdk.sessions.create(
        workspaces=(v01_harness.workspace,),
        idempotency_key="v01-session",
    )
    agent = sdk.agents.define(
        AgentSpec(
            name="release-agent",
            model="test/model",
        )
    )
    handle = await sdk.runs.start(
        session.session_id,
        agent,
        "baseline",
        idempotency_key="v01-run",
    )
    permission = await asyncio.wait_for(
        sdk.permissions.next_request(handle.run_id),
        timeout=2,
    )
    assert permission.tool_name == "read"
    assert permission.arguments["path"] == "keep.txt"
    await asyncio.wait_for(
        sdk.permissions.resolve(
            permission.request_id,
            PermissionDecision.allow_once(),
        ),
        timeout=2,
    )
    terminal = await asyncio.wait_for(handle.result(), timeout=5)
    assert terminal.output_text == "baseline complete"
    assert [result.tool_name for result in terminal.tool_results] == [
        "write",
        "read",
        "bash",
        "write",
    ]
    assert [result.status for result in terminal.tool_results] == [
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.DENIED,
    ]
    assert (v01_harness.workspace / "generated.txt").read_text(
        encoding="utf-8"
    ) == "created by builtin write"
    assert thaw_json(terminal.tool_results[1].value)["content"] == "application-owned"
    assert (
        "builtin bash complete"
        in thaw_json(terminal.tool_results[2].value)["stdout"]
    )
    assert v01_harness.outside_file.read_text(
        encoding="utf-8"
    ) == "outside fixture"
    timeline = await sdk.queries.timeline(handle.run_id)
    assert timeline.run_id == handle.run_id
    event_types = [item.event.type for item in timeline.events]
    assert event_types.count("permission.requested") == 1
    assert event_types.count("permission.resolved") == 1
    assert event_types.count("tool.call.completed") == 4
    assert event_types.count("tool.call.started") == 4
    await asyncio.wait_for(sdk.close(), timeout=5)

    provider_calls = 0

    async def must_not_call(**_: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("completed Run must not call LiteLLM after reopen")

    reopened = v01_harness.reopen(must_not_call)
    observed = await reopened.queries.get_run(handle.run_id)
    assert observed.snapshot.status is RunStatus.COMPLETED
    await reopened.sessions.close(session.session_id)
    await reopened.sessions.delete(session.session_id)
    with pytest.raises(AgentSDKError) as deleted:
        await reopened.sessions.get(session.session_id)
    assert deleted.value.code is ErrorCode.NOT_FOUND
    assert workspace_file.read_text(encoding="utf-8") == "application-owned"
    assert v01_harness.outside_file.read_text(
        encoding="utf-8"
    ) == "outside fixture"
    await asyncio.wait_for(reopened.close(), timeout=5)
    assert provider_calls == 0
