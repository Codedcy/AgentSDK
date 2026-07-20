from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ProviderRecoveryAdapter,
    ProviderRecoveryDisposition,
    ProviderRecoveryRequest,
    ProviderRecoveryResult,
    TokenUsage,
)
from agent_sdk.runtime.engine import _model_request_fingerprint
from agent_sdk.runtime import reconciliation
from agent_sdk.runtime.reconciliation import ModelCallOperation
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.tools.models import thaw_json


@pytest.mark.asyncio
async def test_in_flight_model_operation_stores_exact_prepared_request() -> None:
    store = InMemoryStore()
    accepted = asyncio.Event()
    release = asyncio.Event()
    observed: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> AsyncIterator[dict[str, object]]:
        observed.append(kwargs)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            accepted.set()
            await release.wait()
            yield {
                "choices": [
                    {"delta": {"content": "done"}, "finish_reason": "stop"}
                ]
            }

        return chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=provider,
        enable_builtin_tools=False,
    )
    handle = None
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="recoverable", model="test/model"),
            "Persist this exact request.",
        )
        await asyncio.wait_for(accepted.wait(), timeout=2)

        operations = await store.list_unresolved_external_operations(handle.run_id)
        assert len(operations) == 1
        operation = operations[0]
        assert isinstance(operation, ModelCallOperation)
        assert operation.context_view_id is not None
        assert operation.prompt_manifest_id is not None
        assert operation.prepared_request is not None
        prepared = thaw_json(operation.prepared_request)
        assert isinstance(prepared, dict)
        request = reconciliation.deserialize_model_request(prepared)
        assert request.messages == tuple(observed[0]["messages"])
        assert request.tools == tuple(observed[0]["tools"])
        assert _model_request_fingerprint(request) == operation.request_fingerprint

        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        started = next(
            item.event
            for item in events
            if item.event.type == "model.call.started"
        )
        public_payload = started.model_dump_json()
        assert started.payload == {
            "model": "test/model",
            "context_view_id": operation.context_view_id,
            "prompt_manifest_id": operation.prompt_manifest_id,
            "request_fingerprint": operation.request_fingerprint,
        }
        assert "Persist this exact request." not in public_payload
        assert "You are" not in public_payload
    finally:
        release.set()
        if handle is not None:
            await handle.result()
        await sdk.close()


@pytest.mark.asyncio
async def test_reopen_reuses_in_flight_prepared_request_without_new_context() -> None:
    store = InMemoryStore()
    accepted = asyncio.Event()

    async def hanging_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            accepted.set()
            await asyncio.Event().wait()
            yield {"choices": []}

        return chunks()

    spec = AgentSpec(name="recoverable", model="test/model")
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=hanging_provider,
        enable_builtin_tools=False,
    )
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(
        session.session_id,
        spec,
        "Crash after model acceptance.",
    )
    await asyncio.wait_for(accepted.wait(), timeout=2)
    original = (await store.list_unresolved_external_operations(handle.run_id))[0]
    assert isinstance(original, ModelCallOperation)
    events_before = await store.read_events(
        after_cursor=0,
        session_id=session.session_id,
    )
    counts_before = {
        event_type: sum(
            item.event.type == event_type for item in events_before
        )
        for event_type in (
            "context.view.created",
            "context.compaction.completed",
            "prompt.manifest.created",
            "model.call.started",
        )
    }
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle._task
    await sdk.close()

    provider_calls = 0

    async def must_not_call(**_: Any) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("unknown model outcome must not be replayed")

    reopened = AgentSDK.for_test(
        store=store,
        acompletion=must_not_call,
        enable_builtin_tools=False,
    )
    reopened.agents.define(spec)
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await reopened.recovery.recover_run(handle.run_id)).result()

        pending = await reopened.recovery.pending_requests(handle.run_id)
        assert len(pending) == 1
        assert pending[0].reason == "model_call_unknown_outcome"
        recovered = await store.get_external_operation(original.operation_id)
        assert recovered == original
        assert isinstance(recovered, ModelCallOperation)
        assert recovered.context_view_id == original.context_view_id
        assert recovered.prompt_manifest_id == original.prompt_manifest_id
        assert recovered.prepared_request == original.prepared_request
        assert recovered.request_fingerprint == original.request_fingerprint

        events_after = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        counts_after = {
            event_type: sum(
                item.event.type == event_type for item in events_after
            )
            for event_type in counts_before
        }
        assert counts_after == counts_before
        assert provider_calls == 0
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_authoritative_recovery_receives_exact_stored_prepared_request() -> None:
    store = InMemoryStore()
    accepted = asyncio.Event()
    observed: list[ProviderRecoveryRequest] = []

    async def hanging_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            accepted.set()
            await asyncio.Event().wait()
            yield {"choices": []}

        return chunks()

    async def query(
        request: ProviderRecoveryRequest,
    ) -> ProviderRecoveryResult:
        observed.append(request)
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="recovered",
            usage=TokenUsage(
                prompt_tokens=3,
                completion_tokens=1,
                total_tokens=4,
            ),
        )

    adapter = ProviderRecoveryAdapter(
        provider_identity="test/model",
        adapter_id="test.authoritative",
        version="1",
        authoritative_status=True,
        same_operation_id_resend=False,
        query_status=query,
    )
    spec = AgentSpec(name="recoverable", model="test/model")
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=hanging_provider,
        enable_builtin_tools=False,
    )
    sdk.recovery.register_adapter(adapter)
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(
        session.session_id,
        spec,
        "Recover the exact prepared request.",
    )
    await asyncio.wait_for(accepted.wait(), timeout=2)
    original = (await store.list_unresolved_external_operations(handle.run_id))[0]
    assert isinstance(original, ModelCallOperation)
    assert original.prepared_request is not None
    exact_request = reconciliation.deserialize_model_request(
        original.prepared_request
    )
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle._task
    await sdk.close()

    async def must_not_call(**_: Any) -> object:
        raise AssertionError("certified recovery must not call LiteLLM")

    reopened = AgentSDK.for_test(
        store=store,
        acompletion=must_not_call,
        enable_builtin_tools=False,
    )
    reopened.agents.define(spec)
    reopened.recovery.register_adapter(adapter)
    try:
        await reopened.recovery.scan()
        result = await (
            await reopened.recovery.recover_run(handle.run_id)
        ).result()

        assert result.output_text == "recovered"
        assert len(observed) == 1
        assert observed[0].model_request == exact_request
        assert observed[0].request_fingerprint == original.request_fingerprint
        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        assert sum(
            item.event.type == "context.view.created" for item in events
        ) == 1
        assert sum(
            item.event.type == "prompt.manifest.created" for item in events
        ) == 1
        assert sum(
            item.event.type == "model.call.started" for item in events
        ) == 1
    finally:
        await reopened.close()
