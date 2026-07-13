from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import litellm
import pytest
from pydantic import ValidationError

from agent_sdk import (
    AgentSDK,
    AgentSDKConfig,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    RunHandle,
    RunResult,
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
from agent_sdk.storage.sqlite import SQLiteStore


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


def test_agent_spec_model_copy_update_detaches_nested_model_params() -> None:
    source = {"metadata": {"labels": ["copied"]}}
    original = AgentSpec(name="test", model="fake/model")

    copied = original.model_copy(update={"model_params": source})
    source["metadata"]["labels"].append("external mutation")

    assert copied.model_params["metadata"]["labels"] == ("copied",)


def test_agent_spec_model_copy_update_returns_frozen_nested_values() -> None:
    original = AgentSpec(name="test", model="fake/model")
    copied = original.model_copy(
        update={"model_params": {"metadata": {"labels": ["copied"]}}}
    )

    with pytest.raises(TypeError):
        copied.model_params["metadata"]["labels"][0] = "mutation"  # type: ignore[index]
    with pytest.raises(TypeError):
        copied.model_params["metadata"]["new"] = "mutation"  # type: ignore[index]


def test_agent_spec_model_copy_deep_succeeds_with_independent_equal_values() -> None:
    original = AgentSpec(
        name="test",
        model="fake/model",
        model_params={"metadata": {"labels": ["original"]}},
    )

    copied = original.model_copy(deep=True)

    assert copied == original
    assert copied is not original
    assert copied.model_params is not original.model_params
    assert copied.model_params["metadata"] is not original.model_params["metadata"]


def test_agent_spec_model_copy_revalidates_updates() -> None:
    original = AgentSpec(name="test", model="fake/model")

    with pytest.raises(ValidationError):
        original.model_copy(update={"unknown": "forbidden"})
    with pytest.raises(ValidationError):
        original.model_copy(update={"name": 42})


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


async def _created_run(store: InMemoryStore) -> tuple[str, int]:
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="1",
        user_input="startup",
    )
    cursor = (await store.read_events(after_cursor=0))[-1].cursor
    return run.run_id, cursor


@pytest.mark.asyncio
async def test_events_terminates_and_normalizes_failed_task_without_terminal(
    store: InMemoryStore,
) -> None:
    run_id, cursor = await _created_run(store)

    async def fail_startup() -> RunResult:
        raise RuntimeError("raw provider startup failure")

    task = asyncio.create_task(fail_startup())
    await asyncio.sleep(0)
    handle = RunHandle(run_id, store, task)

    with pytest.raises(AgentSDKError) as raised:
        await asyncio.wait_for(
            _collect_events(handle.events(cursor=cursor)),
            timeout=0.1,
        )

    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "run execution failed"
    assert isinstance(raised.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_result_normalizes_raw_startup_failure(store: InMemoryStore) -> None:
    run_id, _ = await _created_run(store)

    async def fail_startup() -> RunResult:
        raise RuntimeError("raw provider startup failure")

    handle = RunHandle(run_id, store, asyncio.create_task(fail_startup()))

    with pytest.raises(AgentSDKError) as raised:
        await handle.result()

    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "run execution failed"
    assert isinstance(raised.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_events_preserves_agent_sdk_error_without_terminal(
    store: InMemoryStore,
) -> None:
    run_id, cursor = await _created_run(store)
    expected = AgentSDKError(ErrorCode.INVALID_STATE, "startup rejected", retryable=False)

    async def fail_startup() -> RunResult:
        raise expected

    task = asyncio.create_task(fail_startup())
    await asyncio.sleep(0)
    handle = RunHandle(run_id, store, task)

    with pytest.raises(AgentSDKError) as raised:
        await asyncio.wait_for(
            _collect_events(handle.events(cursor=cursor)),
            timeout=0.1,
        )

    assert raised.value is expected


@pytest.mark.asyncio
async def test_events_normalizes_cancelled_task_without_terminal(
    store: InMemoryStore,
) -> None:
    run_id, cursor = await _created_run(store)

    async def wait_forever() -> RunResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(wait_forever())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    handle = RunHandle(run_id, store, task)

    with pytest.raises(AgentSDKError) as raised:
        await asyncio.wait_for(
            _collect_events(handle.events(cursor=cursor)),
            timeout=0.1,
        )

    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "run execution failed"


@pytest.mark.asyncio
async def test_events_normalizes_successful_task_with_missing_snapshot(
    store: InMemoryStore,
) -> None:
    run_id = "run_missing"

    async def finish_without_terminal() -> RunResult:
        return RunResult(run_id=run_id, output_text="bad", usage=TokenUsage())

    task = asyncio.create_task(finish_without_terminal())
    await asyncio.sleep(0)
    handle = RunHandle(run_id, store, task)

    with pytest.raises(AgentSDKError) as raised:
        await asyncio.wait_for(_collect_events(handle.events()), timeout=0.1)

    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "run execution failed"


class _FailingSnapshotStore(InMemoryStore):
    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        del kind, entity_id
        raise RuntimeError("raw store failure")


@pytest.mark.asyncio
async def test_run_get_normalizes_store_failure() -> None:
    sdk = AgentSDK.for_test(store=_FailingSnapshotStore(), acompletion=_scripted_success)
    try:
        with pytest.raises(AgentSDKError) as raised:
            await sdk.runs.get("run_store_failure")

        assert raised.value.code is ErrorCode.INTERNAL
        assert raised.value.message == "failed to load run"
        assert raised.value.retryable is False
        assert "raw store failure" not in str(raised.value)
        assert isinstance(raised.value.__cause__, RuntimeError)
    finally:
        await sdk.close()


class _InvalidRunSnapshotStore(InMemoryStore):
    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        del kind
        return {"run_id": entity_id, "status": "corrupt"}


@pytest.mark.asyncio
async def test_run_get_normalizes_invalid_snapshot() -> None:
    sdk = AgentSDK.for_test(
        store=_InvalidRunSnapshotStore(),
        acompletion=_scripted_success,
    )
    try:
        with pytest.raises(AgentSDKError) as raised:
            await sdk.runs.get("run_invalid")

        assert raised.value.code is ErrorCode.INTERNAL
        assert raised.value.message == "failed to load run"
        assert raised.value.retryable is False
        assert "corrupt" not in str(raised.value)
        assert isinstance(raised.value.__cause__, ValidationError)
    finally:
        await sdk.close()


class _AgentSDKErrorSnapshotStore(InMemoryStore):
    def __init__(self, error: AgentSDKError) -> None:
        super().__init__()
        self._error = error

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        del kind, entity_id
        raise self._error


@pytest.mark.asyncio
async def test_run_get_preserves_agent_sdk_error() -> None:
    expected = AgentSDKError(ErrorCode.INVALID_STATE, "store unavailable", retryable=True)
    sdk = AgentSDK.for_test(
        store=_AgentSDKErrorSnapshotStore(expected),
        acompletion=_scripted_success,
    )
    try:
        with pytest.raises(AgentSDKError) as raised:
            await sdk.runs.get("run_sdk_error")

        assert raised.value is expected
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_run_get_missing_snapshot_remains_not_found(store: InMemoryStore) -> None:
    sdk = AgentSDK.for_test(store=store, acompletion=_scripted_success)
    try:
        with pytest.raises(AgentSDKError) as raised:
            await sdk.runs.get("run_missing")

        assert raised.value.code is ErrorCode.NOT_FOUND
        assert raised.value.message == "run not found"
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_events_normalizes_store_failure_after_task_done() -> None:
    store = _FailingSnapshotStore()
    run_id = "run_store_failure"

    async def finish_without_terminal() -> RunResult:
        return RunResult(run_id=run_id, output_text="bad", usage=TokenUsage())

    task = asyncio.create_task(finish_without_terminal())
    await asyncio.sleep(0)
    handle = RunHandle(run_id, store, task)

    with pytest.raises(AgentSDKError) as raised:
        await _collect_events(handle.events())

    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "run execution failed"
    assert isinstance(raised.value.__cause__, RuntimeError)


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
async def test_session_create_is_rejected_once_close_begins(store: InMemoryStore) -> None:
    sdk = AgentSDK.for_test(store=store, acompletion=_scripted_success)
    await sdk.close()

    with pytest.raises(AgentSDKError) as raised:
        await sdk.sessions.create(workspaces=[])

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "SDK is closing"


@pytest.mark.asyncio
async def test_run_start_is_rejected_once_close_begins(store: InMemoryStore) -> None:
    sdk = AgentSDK.for_test(store=store, acompletion=_scripted_success)
    session = await sdk.sessions.create(workspaces=[])
    await sdk.close()

    with pytest.raises(AgentSDKError) as raised:
        await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "late run",
        )

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "SDK is closing"


class _BlockingRunCreatedStore:
    def __init__(self) -> None:
        self._store = InMemoryStore()
        self.run_created_commit_started = asyncio.Event()
        self.release_run_created_commit = asyncio.Event()

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(event.type == "run.created" for event in batch.events):
            self.run_created_commit_started.set()
            await self.release_run_created_commit.wait()
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
async def test_concurrent_close_calls_wait_for_start_admission_and_active_run() -> None:
    store = _BlockingRunCreatedStore()
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
    start_task = asyncio.create_task(
        sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "race close",
        )
    )
    close_tasks: tuple[asyncio.Task[None], ...] = ()
    handle = None
    try:
        await asyncio.wait_for(store.run_created_commit_started.wait(), timeout=1)
        close_tasks = (asyncio.create_task(sdk.close()), asyncio.create_task(sdk.close()))
        completed_close_tasks, _ = await asyncio.wait(close_tasks, timeout=0.05)

        assert completed_close_tasks == set()

        store.release_run_created_commit.set()
        handle = await asyncio.wait_for(start_task, timeout=1)
        await asyncio.wait_for(provider_waiting.wait(), timeout=1)
        assert not any(task.done() for task in close_tasks)

        release_provider.set()
        await asyncio.wait_for(asyncio.gather(*close_tasks), timeout=1)
        assert (await handle.result()).output_text == "hello"
    finally:
        store.release_run_created_commit.set()
        release_provider.set()
        if handle is None:
            handle = await start_task
        await handle.result()
        if close_tasks:
            await asyncio.gather(*close_tasks)
        await sdk.close()


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


@pytest.mark.asyncio
async def test_configured_sdk_does_not_lazy_reopen_after_close(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    sdk = AgentSDK(AgentSDKConfig(database_path=database_path))
    try:
        await sdk.close()

        with pytest.raises(AgentSDKError) as raised:
            await sdk.sessions.create(workspaces=[])

        assert raised.value.code is ErrorCode.INVALID_STATE
        assert not database_path.exists()
        await sdk.close()
    finally:
        await sdk._owned_close()  # type: ignore[misc]


def _install_failing_sqlite_close(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, asyncio.Event, asyncio.Event]:
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_failed = asyncio.Event()
    original_close = SQLiteStore.close

    async def failing_close(store: SQLiteStore) -> None:
        close_started.set()
        await release_close.wait()
        await original_close(store)
        close_failed.set()
        raise RuntimeError("owned close failure")

    monkeypatch.setattr(SQLiteStore, "close", failing_close)
    return close_started, release_close, close_failed


@pytest.mark.asyncio
async def test_cancelled_only_close_waiter_leaves_no_unretrieved_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_started, release_close, close_failed = _install_failing_sqlite_close(
        monkeypatch
    )
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    diagnostics: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: diagnostics.append(context))
    sdk = AgentSDK(AgentSDKConfig(database_path=tmp_path / "state.db"))
    close_waiter: asyncio.Task[None] | None = None
    try:
        await sdk.sessions.create(workspaces=[])
        close_waiter = asyncio.create_task(sdk.close())
        await asyncio.wait_for(close_started.wait(), timeout=1)

        close_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_waiter
        release_close.set()
        await asyncio.wait_for(close_failed.wait(), timeout=1)
        await asyncio.sleep(0)

        del close_waiter
        close_waiter = None
        del sdk
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0)

        assert not any(
            diagnostic.get("message") == "Task exception was never retrieved"
            for diagnostic in diagnostics
        )
    finally:
        release_close.set()
        if close_waiter is not None and not close_waiter.done():
            close_waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_waiter
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_second_close_replays_failure_after_only_waiter_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_started, release_close, close_failed = _install_failing_sqlite_close(
        monkeypatch
    )
    sdk = AgentSDK(AgentSDKConfig(database_path=tmp_path / "state.db"))
    await sdk.sessions.create(workspaces=[])
    close_waiter = asyncio.create_task(sdk.close())
    await asyncio.wait_for(close_started.wait(), timeout=1)

    close_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_waiter
    release_close.set()
    await asyncio.wait_for(close_failed.wait(), timeout=1)

    with pytest.raises(RuntimeError, match="owned close failure"):
        await sdk.close()


@pytest.mark.asyncio
async def test_close_failure_reaches_normal_waiter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_started, release_close, _ = _install_failing_sqlite_close(monkeypatch)
    sdk = AgentSDK(AgentSDKConfig(database_path=tmp_path / "state.db"))
    await sdk.sessions.create(workspaces=[])
    release_close.set()

    with pytest.raises(RuntimeError, match="owned close failure"):
        await sdk.close()

    assert close_started.is_set()
