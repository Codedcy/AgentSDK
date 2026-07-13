from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import litellm
import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKConfig,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    RunStatus,
    TokenUsage,
)
from agent_sdk.models.litellm_gateway import (
    LiteLLMGateway,
    ModelCompleted,
    ModelRequest,
    TextDelta,
    UsageReported,
)
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.storage.base import CommitBatch, CommitResult, StoredEvent
from agent_sdk.storage.memory import InMemoryStore


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


async def _scripted_success(**_: object) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": "hel"}}]}
        yield {
            "choices": [
                {
                    "delta": {"content": "lo"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        }

    return chunks()


def _run_events(events: list[StoredEvent], run_id: str) -> list[StoredEvent]:
    return [stored for stored in events if stored.event.run_id == run_id]


async def _event_of_type(handle: Any, event_type: str) -> StoredEvent:
    async for stored in handle.events():
        if stored.event.type == event_type:
            return stored
    raise AssertionError(f"run ended without {event_type}")


@pytest.mark.asyncio
async def test_agent_loop_persists_stream_usage_and_result(store: InMemoryStore) -> None:
    calls: list[dict[str, object]] = []

    async def fake_acompletion(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        calls.append(kwargs)
        return await _scripted_success()

    sdk = AgentSDK.for_test(store=store, acompletion=fake_acompletion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        spec = AgentSpec(
            name="test",
            model="fake/model",
            model_params={"temperature": 0.25},
        )
        handle = await sdk.runs.start(session.session_id, spec, "say hello")

        result = await handle.result()
        snapshot = await sdk.runs.get(handle.run_id)
        events = _run_events(await store.read_events(after_cursor=0), handle.run_id)

        usage = TokenUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3)
        assert result.run_id == handle.run_id
        assert result.output_text == "hello"
        assert result.usage == usage
        assert snapshot.status is RunStatus.COMPLETED
        assert snapshot.version == 3
        assert snapshot.output_text == "hello"
        assert snapshot.usage == usage
        assert [stored.event.type for stored in events] == [
            "run.created",
            "run.started",
            "step.started",
            "model.call.started",
            "model.text.delta",
            "model.usage.reported",
            "model.call.completed",
            "step.completed",
            "run.completed",
        ]
        assert [stored.event.sequence for stored in events] == list(range(1, 10))
        assert events[-1].event.payload["usage"] == usage.model_dump()
        assert calls == [
            {
                "model": "fake/model",
                "messages": [{"role": "user", "content": "say hello"}],
                "tools": [],
                "stream": True,
                "temperature": 0.25,
            }
        ]
    finally:
        await sdk.close()


def test_agent_spec_recursively_detaches_and_freezes_model_params() -> None:
    source = {"metadata": {"labels": ["original"]}}
    spec = AgentSpec(name="test", model="fake/model", model_params=source)
    same_default_revision = AgentSpec(name="other", model="fake/model")

    source["metadata"]["labels"].append("external mutation")

    assert spec.model_params["metadata"]["labels"] == ("original",)
    assert spec.revision == same_default_revision.revision
    with pytest.raises(TypeError):
        spec.model_params["new"] = "mutation"  # type: ignore[index]
    with pytest.raises(TypeError):
        spec.model_params["metadata"]["labels"][0] = "mutation"  # type: ignore[index]


@pytest.mark.asyncio
async def test_gateway_normalizes_real_litellm_chunks_to_sdk_events() -> None:
    raw_chunks = (
        litellm.ModelResponseStream(
            id="chatcmpl_test_1",
            created=1,
            model="fake/model",
            object="chat.completion.chunk",
            choices=[
                {
                    "index": 0,
                    "delta": {"content": "hel"},
                    "finish_reason": None,
                }
            ],
        ),
        litellm.ModelResponseStream(
            id="chatcmpl_test_2",
            created=2,
            model="fake/model",
            object="chat.completion.chunk",
            choices=[
                {
                    "index": 0,
                    "delta": {"content": "lo"},
                    "finish_reason": "stop",
                }
            ],
            usage={
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        ),
    )

    async def fake_acompletion(**_: object) -> AsyncIterator[object]:
        async def chunks() -> AsyncIterator[object]:
            for chunk in raw_chunks:
                yield chunk

        return chunks()

    gateway = LiteLLMGateway._for_test(fake_acompletion)
    events = [
        event
        async for event in gateway.stream(
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "hello"},),
            )
        )
    ]

    assert events == [
        TextDelta("hel"),
        TextDelta("lo"),
        UsageReported(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        ModelCompleted(finish_reason="stop"),
    ]
    assert sum(isinstance(event, ModelCompleted) for event in events) == 1
    assert not any(isinstance(event, litellm.ModelResponseStream) for event in events)


@pytest.mark.asyncio
async def test_gateway_defaults_to_litellm_acompletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_acompletion(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        calls.append(kwargs)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {}, "finish_reason": None}]}

        return chunks()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    gateway = LiteLLMGateway()
    events = [
        event
        async for event in gateway.stream(
            ModelRequest(model="fake/model", messages=({"role": "user", "content": "hi"},))
        )
    ]

    assert events == [ModelCompleted(finish_reason=None)]
    assert calls == [
        {
            "model": "fake/model",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [],
            "stream": True,
        }
    ]


@pytest.mark.asyncio
async def test_run_events_observe_timer_flushed_delta_before_result(
    store: InMemoryStore,
) -> None:
    fragment_accepted = asyncio.Event()
    release_provider = asyncio.Event()

    async def fake_acompletion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "waiting"}}]}
            fragment_accepted.set()
            await release_provider.wait()
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    sdk = AgentSDK.for_test(store=store, acompletion=fake_acompletion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "wait",
        )
        result_task = asyncio.create_task(handle.result())
        await asyncio.wait_for(fragment_accepted.wait(), timeout=1)

        delta = await asyncio.wait_for(
            _event_of_type(handle, "model.text.delta"),
            timeout=1,
        )

        assert delta.cursor > 0
        assert delta.event.payload == {"text": "waiting"}
        assert not result_task.done()
        running = await sdk.runs.get(handle.run_id)
        assert running.status is RunStatus.RUNNING
        assert running.version == 2

        release_provider.set()
        assert (await asyncio.wait_for(result_task, timeout=1)).output_text == "waiting"
    finally:
        release_provider.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_delta_flushes_at_four_kib_before_provider_continues(
    store: InMemoryStore,
) -> None:
    threshold_reached = asyncio.Event()
    release_provider = asyncio.Event()
    text = "a" * 2048 + "b" * 2048

    async def fake_acompletion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "a" * 2048}}]}
            yield {"choices": [{"delta": {"content": "b" * 2048}}]}
            threshold_reached.set()
            await release_provider.wait()
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    sdk = AgentSDK.for_test(store=store, acompletion=fake_acompletion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "large delta",
        )
        await asyncio.wait_for(threshold_reached.wait(), timeout=1)

        delta = next(
            stored
            for stored in _run_events(
                await store.read_events(after_cursor=0),
                handle.run_id,
            )
            if stored.event.type == "model.text.delta"
        )

        assert delta.event.payload == {"text": text}
        release_provider.set()
        assert (await asyncio.wait_for(handle.result(), timeout=1)).output_text == text
    finally:
        release_provider.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_provider_failure_is_durable_before_stable_error(store: InMemoryStore) -> None:
    async def fake_acompletion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "accepted"}}]}
            raise RuntimeError("provider-specific secret details")

        return chunks()

    sdk = AgentSDK.for_test(store=store, acompletion=fake_acompletion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "fail",
        )

        with pytest.raises(AgentSDKError) as raised:
            await handle.result()

        assert raised.value.code is ErrorCode.INTERNAL
        assert raised.value.message == "model call failed"
        assert raised.value.retryable is False
        assert isinstance(raised.value.__cause__, RuntimeError)
        snapshot = await sdk.runs.get(handle.run_id)
        events = _run_events(await store.read_events(after_cursor=0), handle.run_id)
        assert snapshot.status is RunStatus.FAILED
        assert snapshot.version == 3
        assert [stored.event.type for stored in events] == [
            "run.created",
            "run.started",
            "step.started",
            "model.call.started",
            "model.text.delta",
            "model.call.failed",
            "step.failed",
            "run.failed",
        ]
        assert [stored.event.sequence for stored in events] == list(range(1, 9))
        assert events[4].event.payload == {"text": "accepted"}
        assert events[-1].event.payload["error"] == raised.value.to_dict()
        assert "provider-specific" not in str(events[-1].event.payload)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_run_events_filter_by_run_preserve_global_cursor_and_resume(
    store: InMemoryStore,
) -> None:
    sdk = AgentSDK.for_test(store=store, acompletion=_scripted_success)
    try:
        session = await sdk.sessions.create(workspaces=[])
        first = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="first", model="fake/model"),
            "first",
        )
        await first.result()
        second = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="second", model="fake/model"),
            "second",
        )
        await second.result()

        first_events = await asyncio.wait_for(
            _collect_events(first.events()),
            timeout=1,
        )
        resume_cursor = first_events[3].cursor
        resumed_events = await asyncio.wait_for(
            _collect_events(first.events(cursor=resume_cursor)),
            timeout=1,
        )

        assert all(stored.event.run_id == first.run_id for stored in first_events)
        assert [stored.cursor for stored in first_events] == sorted(
            stored.cursor for stored in first_events
        )
        assert first_events[0].cursor > 1
        assert first_events[-1].event.type == "run.completed"
        assert resumed_events == [
            stored for stored in first_events if stored.cursor > resume_cursor
        ]
    finally:
        await sdk.close()


async def _collect_events(events: AsyncIterator[StoredEvent]) -> list[StoredEvent]:
    return [stored async for stored in events]


class _BlockingTerminalStore:
    def __init__(self) -> None:
        self._store = InMemoryStore()
        self.terminal_commit_started = asyncio.Event()
        self.release_terminal_commit = asyncio.Event()

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(event.type == "run.completed" for event in batch.events):
            self.terminal_commit_started.set()
            await self.release_terminal_commit.wait()
        return await self._store.commit(batch)

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
    ) -> list[StoredEvent]:
        return await self._store.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
        )

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        return await self._store.get_snapshot(kind, entity_id)

    async def delete_session(self, session_id: str) -> None:
        await self._store.delete_session(session_id)


@pytest.mark.asyncio
async def test_result_waits_for_atomic_terminal_event_and_snapshot() -> None:
    store = _BlockingTerminalStore()
    sdk = AgentSDK.for_test(store=store, acompletion=_scripted_success)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "hello",
        )
        result_task = asyncio.create_task(handle.result())
        await asyncio.wait_for(store.terminal_commit_started.wait(), timeout=1)

        assert not result_task.done()
        assert (await sdk.runs.get(handle.run_id)).status is RunStatus.RUNNING

        store.release_terminal_commit.set()
        assert (await asyncio.wait_for(result_task, timeout=1)).output_text == "hello"
        assert (await sdk.runs.get(handle.run_id)).status is RunStatus.COMPLETED
    finally:
        store.release_terminal_commit.set()
        await sdk.close()


class _CloseTrackingStore:
    def __init__(self) -> None:
        self._store = InMemoryStore()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1

    async def commit(self, batch: CommitBatch) -> CommitResult:
        return await self._store.commit(batch)

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
    ) -> list[StoredEvent]:
        return await self._store.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
        )

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        return await self._store.get_snapshot(kind, entity_id)

    async def delete_session(self, session_id: str) -> None:
        await self._store.delete_session(session_id)


@pytest.mark.asyncio
async def test_close_waits_for_active_run_without_owning_injected_store() -> None:
    store = _CloseTrackingStore()
    provider_waiting = asyncio.Event()
    release_provider = asyncio.Event()

    async def fake_acompletion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "hello"}}]}
            provider_waiting.set()
            await release_provider.wait()
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        return chunks()

    sdk = AgentSDK.for_test(store=store, acompletion=fake_acompletion)
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(
        session.session_id,
        AgentSpec(name="test", model="fake/model"),
        "hello",
    )
    await asyncio.wait_for(provider_waiting.wait(), timeout=1)

    close_task = asyncio.create_task(sdk.close())
    await asyncio.sleep(0)
    assert not close_task.done()

    release_provider.set()
    await asyncio.wait_for(close_task, timeout=1)
    assert (await handle.result()).output_text == "hello"
    assert store.close_calls == 0


@pytest.mark.asyncio
async def test_engine_cancellation_settles_delta_timer(store: InMemoryStore) -> None:
    provider_waiting = asyncio.Event()

    async def fake_acompletion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "accepted"}}]}
            provider_waiting.set()
            await asyncio.Event().wait()

        return chunks()

    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="1",
        user_input="cancel",
    )
    engine = RunEngine(store, LiteLLMGateway._for_test(fake_acompletion))
    task = asyncio.create_task(
        engine.execute(
            run.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "cancel"},),
            ),
        )
    )
    await asyncio.wait_for(provider_waiting.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events_after_cancellation = await store.read_events(after_cursor=0)

    await asyncio.sleep(0.08)

    assert await store.read_events(after_cursor=0) == events_after_cancellation


@pytest.mark.asyncio
async def test_configured_sdk_owns_and_closes_sqlite_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acompletion(**_: object) -> AsyncIterator[dict[str, object]]:
        return await _scripted_success()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    database_path = tmp_path / "state.db"
    sdk = AgentSDK(AgentSDKConfig(database_path=database_path))
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(
        session.session_id,
        AgentSpec(name="test", model="fake/model"),
        "hello",
    )

    assert (await handle.result()).output_text == "hello"
    await sdk.close()

    database_path.unlink()
