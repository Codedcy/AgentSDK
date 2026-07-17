from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_sdk import AgentSDK, AgentSDKError, AgentSpec, ErrorCode, RunStatus

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
    terminal = await handle.result()
    assert terminal.output_text == "baseline complete"
    assert (await sdk.queries.timeline(handle.run_id)).run_id == handle.run_id
    await sdk.close()

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
    await reopened.close()
    assert provider_calls == 0
