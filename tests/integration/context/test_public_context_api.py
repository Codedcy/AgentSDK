from collections.abc import AsyncIterator
import json
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    CompactionLevel,
    ContextAPI,
    ErrorCode,
    ObservedEvent,
)
from agent_sdk.storage.memory import InMemoryStore


async def _provider(**params: Any) -> object:
    if params["stream"] is False:
        document = json.loads(params["messages"][1]["content"])
        source_ids = [item["event_id"] for item in document["sources"]]
        return {
            "choices": [
                {
                    "message": {
                        "parsed": {
                            "objective": "retain the completed run",
                            "constraints": [],
                            "decisions": [],
                            "facts": ["run completed"],
                            "next_actions": ["verify after reopen"],
                            "artifact_refs": [],
                            "source_event_ids": source_ids,
                        }
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "total_tokens": 6,
            },
        }

    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {"delta": {"content": "done"}, "finish_reason": "stop"}
            ]
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


@pytest.mark.asyncio
async def test_context_facade_builds_retrieves_and_deletes_session_capsule() -> None:
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=_provider)
    assert isinstance(sdk.context, ContextAPI)
    session = await sdk.sessions.create(workspaces=[])
    agent = sdk.agents.define(AgentSpec(name="main", model="fake/main"))
    run = await sdk.runs.start(session.session_id, agent, "retain this input")
    await run.result()

    view = await sdk.context.build(
        session.session_id,
        model="gpt-4o-mini",
        model_window=8_192,
        force_level=CompactionLevel.L3,
    )
    assert view.applied_level is CompactionLevel.L3
    assert view.capsule_id is not None
    capsule = await sdk.context.get_capsule(
        view.capsule_id,
        session_id=session.session_id,
    )
    sources = await sdk.context.read_sources(
        view.capsule_id,
        session_id=session.session_id,
    )
    assert capsule.source_event_ids == tuple(
        item.event.event_id for item in sources
    )
    assert all(isinstance(item, ObservedEvent) for item in sources)

    await sdk.sessions.close(session.session_id)
    await sdk.sessions.delete(session.session_id)
    with pytest.raises(AgentSDKError) as missing:
        await sdk.context.get_capsule(
            view.capsule_id,
            session_id=session.session_id,
        )
    assert missing.value.code is ErrorCode.NOT_FOUND
    await sdk.close()


@pytest.mark.asyncio
async def test_context_facade_rejects_calls_after_close_without_calling_provider() -> None:
    provider_calls = 0

    async def counting_provider(**_: Any) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("closed Context API must not call LiteLLM")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=counting_provider,
    )
    session = await sdk.sessions.create(workspaces=[])
    await sdk.close()

    with pytest.raises(AgentSDKError) as closed:
        await sdk.context.build(
            session.session_id,
            model="gpt-4o-mini",
            model_window=8_192,
            force_level=CompactionLevel.L3,
        )
    assert closed.value.code is ErrorCode.INVALID_STATE
    assert closed.value.message == "SDK is closing"
    assert provider_calls == 0
