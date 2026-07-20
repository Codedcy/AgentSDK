from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_sdk import AgentSDK, AgentSDKError, AgentSpec, ToolSpec
from agent_sdk.runtime.reconciliation import ModelCallOperation
from agent_sdk.storage.base import CommitBatch, CommitResult
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.tools.models import ToolContext


def _tool_stream() -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_lookup",
                                "function": {
                                    "name": "lookup",
                                    "arguments": json.dumps({"query": "context"}),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    return chunks()


def _text_stream(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {"content": text},
                    "finish_reason": "stop",
                }
            ]
        }

    return chunks()


class _DeleteViewAfterManifestStore:
    def __init__(self, delegate: InMemoryStore) -> None:
        self.delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        result = await self.delegate.commit(batch)
        manifest = next(
            (
                event
                for event in batch.events
                if event.type == "prompt.manifest.created"
            ),
            None,
        )
        if manifest is not None:
            view_id = manifest.payload["context_view_id"]
            assert isinstance(view_id, str)
            del self.delegate._snapshots[("context_view", view_id)]
        return result


@pytest.mark.asyncio
async def test_context_is_prepared_before_each_new_model_call() -> None:
    store = InMemoryStore()
    requests: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> object:
        requests.append(kwargs)
        return _tool_stream() if len(requests) == 1 else _text_stream("done")

    async def lookup(
        _context: ToolContext,
        *,
        query: str,
    ) -> dict[str, str]:
        return {"query": query, "answer": "use durable context"}

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=provider,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    sdk.tools.register(
        ToolSpec(
            name="lookup",
            description="Look up context",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        lookup,
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(
                name="context-agent",
                model="test/model",
                system_prompt="Application constraint.",
            ),
            "Use the lookup tool.",
        )

        assert (await handle.result()).output_text == "done"
        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        views = [
            stored
            for stored in events
            if stored.event.type == "context.view.created"
        ]
        starts = [
            stored
            for stored in events
            if stored.event.type == "model.call.started"
            and stored.event.run_id == handle.run_id
        ]
        assert len(views) == len(starts) == len(requests) == 2
        assert views[0].event.payload["view_id"] != views[1].event.payload["view_id"]
        for view, started in zip(views, starts, strict=True):
            assert view.cursor < started.cursor
            assert (
                started.event.payload["context_view_id"]
                == view.event.payload["view_id"]
            )
            assert started.event.payload["prompt_manifest_id"]

        assert [message["role"] for message in requests[0]["messages"][:2]] == [
            "system",
            "system",
        ]
        assert requests[0]["messages"][1]["content"] == "Application constraint."
        assert any(
            message.get("role") == "tool"
            and "durable context" in str(message.get("content"))
            for message in requests[1]["messages"]
        )
        tool_event = next(
            stored.event
            for stored in events
            if stored.event.type == "tool.call.completed"
        )
        assert tool_event.event_id in views[1].event.payload["source_refs"]

        checkpoint = await store.get_run_checkpoint(handle.run_id)
        assert checkpoint is not None
        checkpoint_messages = checkpoint.model_dump(mode="json")["messages"]
        assert all(message["role"] != "system" for message in checkpoint_messages)
        assert [message["role"] for message in checkpoint_messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]

        operations = await store.list_external_operations(handle.run_id)
        model_operations = tuple(
            operation
            for operation in operations
            if isinstance(operation, ModelCallOperation)
        )
        assert len(model_operations) == 2
        assert tuple(operation.context_view_id for operation in model_operations) == (
            views[0].event.payload["view_id"],
            views[1].event.payload["view_id"],
        )
        assert all(operation.prepared_request is not None for operation in model_operations)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_model_start_requires_prepared_snapshots_to_still_exist() -> None:
    durable = InMemoryStore()
    store = _DeleteViewAfterManifestStore(durable)
    provider_calls = 0

    async def provider(**_: Any) -> object:
        nonlocal provider_calls
        provider_calls += 1
        return _text_stream("must not run")

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=provider,
        enable_builtin_tools=False,
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="context-race", model="test/model"),
            "Require durable references.",
        )

        with pytest.raises(AgentSDKError):
            await handle.result()
        assert provider_calls == 0
        events = await durable.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        assert all(
            event.event.type != "model.call.started" for event in events
        )
        assert await durable.list_external_operations(handle.run_id) == ()
    finally:
        await sdk.close()
