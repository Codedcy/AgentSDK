from __future__ import annotations

import asyncio
import gc
import json
import sqlite3
import traceback
import weakref
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    SessionStatus,
)
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
)
from agent_sdk.runtime.session_lifecycle import (
    exact_session_precondition,
    session_write,
)
from agent_sdk.runtime.reconciliation import RecoveryStateConflictError
from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    RunProgressBatch,
)
from agent_sdk.tools.models import ToolContext, ToolSpec
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.idempotency import IdempotencyCorruptionError


AGENT = AgentSpec(name="test", model="fake/model", revision="1")


class BlockingCompletion:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self._release = asyncio.Event()
        self.call_count = 0

    async def __call__(self, **_: object) -> AsyncIterator[dict[str, object]]:
        self.call_count += 1
        self.started.set()
        await self._release.wait()

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

        return chunks()

    def finish(self) -> None:
        self._release.set()


class CountingCompletion:
    def __init__(self, text: str = "done") -> None:
        self.text = text
        self.call_count = 0

    async def __call__(self, **_: object) -> AsyncIterator[dict[str, object]]:
        self.call_count += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {"content": self.text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

        return chunks()


class FailingBlockingCompletion(BlockingCompletion):
    async def __call__(self, **_: object) -> AsyncIterator[dict[str, object]]:
        self.call_count += 1
        self.started.set()
        await self._release.wait()
        raise RuntimeError("must-not-leak-provider-failure")


class _ProviderSentinel:
    pass


class _SentinelProviderFailure(RuntimeError):
    def __init__(self, sentinel: _ProviderSentinel) -> None:
        super().__init__("must-not-retain-provider-sentinel")
        self.sentinel = sentinel


async def _must_not_call(**_: object) -> AsyncIterator[dict[str, object]]:
    raise AssertionError("durable replay must not call the provider")


def _assert_context_free_sanitizer(
    error: AgentSDKError,
    *,
    secret: str,
    original: BaseException | None = None,
) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    current = error.__traceback__
    while current is not None:
        local_values = current.tb_frame.f_locals
        filename = current.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in filename:
            if original is not None:
                assert all(value is not original for value in local_values.values())
            assert secret not in repr(local_values)
        current = current.tb_next


async def _yield_until_run_registry_empty(sdk: AgentSDK) -> None:
    for _ in range(20):
        if not sdk.runs._tasks:  # type: ignore[attr-defined]
            return
        await asyncio.sleep(0)


async def _consume_run_failure(handle: Any) -> ErrorCode:
    try:
        await handle.result()
    except AgentSDKError as error:
        assert error.__cause__ is None
        assert error.__context__ is None
        return error.code
    raise AssertionError("run did not fail")


@pytest.fixture
async def blocking_sdk() -> AsyncIterator[tuple[AgentSDK, BlockingCompletion]]:
    completion = BlockingCompletion()
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=completion)
    try:
        yield sdk, completion
    finally:
        completion.finish()
        await sdk.close()


async def test_close_rejects_new_run_and_last_run_closes_session(
    blocking_sdk: tuple[AgentSDK, BlockingCompletion],
) -> None:
    sdk, completion = blocking_sdk
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(session.session_id, AGENT, "first")
    await asyncio.wait_for(completion.started.wait(), timeout=1)

    closing = await sdk.sessions.close(session.session_id)

    assert closing.status is SessionStatus.CLOSING
    with pytest.raises(AgentSDKError) as raised:
        await sdk.runs.start(session.session_id, AGENT, "second")
    assert raised.value.code is ErrorCode.INVALID_STATE

    completion.finish()
    await asyncio.wait_for(handle.result(), timeout=1)
    assert (await sdk.sessions.get(session.session_id)).status is SessionStatus.CLOSED


async def test_concurrent_duplicate_run_start_executes_once(
    blocking_sdk: tuple[AgentSDK, BlockingCompletion],
) -> None:
    sdk, completion = blocking_sdk
    session = await sdk.sessions.create(workspaces=[])

    handles = await asyncio.gather(
        *(
            sdk.runs.start(
                session.session_id,
                AGENT,
                "same input",
                idempotency_key="run-request",
            )
            for _ in range(32)
        )
    )

    assert len({handle.run_id for handle in handles}) == 1
    assert all(handle.attached for handle in handles)
    assert (
        len(
            {id(handle._task) for handle in handles}  # type: ignore[attr-defined]
        )
        == 1
    )
    completion.finish()
    await asyncio.gather(*(handle.result() for handle in handles))
    assert completion.call_count == 1


async def test_same_run_key_with_different_input_conflicts_before_execution(
    blocking_sdk: tuple[AgentSDK, BlockingCompletion],
) -> None:
    sdk, completion = blocking_sdk
    session = await sdk.sessions.create(workspaces=[])
    first = await sdk.runs.start(
        session.session_id,
        AGENT,
        "first input",
        idempotency_key="run-request",
    )
    await asyncio.wait_for(completion.started.wait(), timeout=1)

    with pytest.raises(AgentSDKError) as raised:
        await sdk.runs.start(
            session.session_id,
            AGENT,
            "different input",
            idempotency_key="run-request",
        )

    assert raised.value.code is ErrorCode.CONFLICT
    assert completion.call_count == 1
    completion.finish()
    await asyncio.wait_for(first.result(), timeout=1)


async def test_no_key_creates_distinct_runs_and_session_owns_both(
    blocking_sdk: tuple[AgentSDK, BlockingCompletion],
) -> None:
    sdk, completion = blocking_sdk
    session = await sdk.sessions.create(workspaces=[])

    first = await sdk.runs.start(session.session_id, AGENT, "same input")
    second = await sdk.runs.start(session.session_id, AGENT, "same input")
    for _ in range(20):
        if completion.call_count == 2:
            break
        await asyncio.sleep(0)

    current = await sdk.sessions.get(session.session_id)
    assert first.run_id != second.run_id
    assert current.active_run_ids == tuple(sorted((first.run_id, second.run_id)))
    assert completion.call_count == 2

    closing = await sdk.sessions.close(session.session_id)
    assert closing.status is SessionStatus.CLOSING
    completion.finish()
    await asyncio.gather(first.result(), second.result())
    assert (await sdk.sessions.get(session.session_id)).status is SessionStatus.CLOSED


async def test_model_failure_detaches_run_and_closes_closing_session() -> None:
    completion = FailingBlockingCompletion()
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=completion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(session.session_id, AGENT, "fail")
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        assert (await sdk.sessions.close(session.session_id)).status is SessionStatus.CLOSING

        completion.finish()
        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(handle.result(), timeout=1)

        assert raised.value.code is ErrorCode.INTERNAL
        assert (await sdk.sessions.get(session.session_id)).status is SessionStatus.CLOSED
    finally:
        completion.finish()
        await sdk.close()


async def test_completed_replay_is_detached_and_uses_durable_result() -> None:
    completion = CountingCompletion("first result")
    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        first = await sdk.runs.start(
            session.session_id,
            AGENT,
            "same input",
            idempotency_key="run-request",
        )
        expected = await first.result()
        await _yield_until_run_registry_empty(sdk)

        replay = await sdk.runs.start(
            session.session_id,
            AGENT,
            "same input",
            idempotency_key="run-request",
        )

        assert replay.run_id == first.run_id
        assert replay.attached is False
        assert await replay.result() == expected
        assert completion.call_count == 1
        run_events = [
            stored
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == first.run_id
        ]
        assert sum(event.event.type == "run.created" for event in run_events) == 1
    finally:
        await sdk.close()


@pytest.mark.parametrize("idempotency_key", ["", "x" * 257])
async def test_invalid_run_key_precedes_missing_session_and_execution(
    idempotency_key: str,
) -> None:
    completion = CountingCompletion("unexpected")
    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    try:
        with pytest.raises(AgentSDKError) as invalid:
            await sdk.runs.start(
                "ses_missing",
                AGENT,
                "must not start",
                idempotency_key=idempotency_key,
            )
        assert invalid.value.code is ErrorCode.INVALID_STATE
        assert invalid.value.message == "idempotency key is invalid"
        assert invalid.value.retryable is False
        _assert_context_free_sanitizer(
            invalid.value,
            secret=idempotency_key or "idempotency text",
        )
        assert completion.call_count == 0
        assert await store.read_events(after_cursor=0) == []
    finally:
        await sdk.close()


class _InvalidRunKeyAccessTrap(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.access_count = 0

    def _trap(self) -> None:
        self.access_count += 1
        raise AssertionError("invalid Run key reached Store")

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        del kind, entity_id
        self._trap()

    async def get_idempotency(self, scope: str, key: str):
        del scope, key
        self._trap()

    async def commit(self, batch: CommitBatch) -> CommitResult:
        del batch
        self._trap()


@pytest.mark.parametrize("idempotency_key", ["", "x" * 257])
async def test_invalid_run_key_is_rejected_before_any_store_access(
    idempotency_key: str,
) -> None:
    store = _InvalidRunKeyAccessTrap()
    sdk = AgentSDK.for_test(store=store, acompletion=CountingCompletion("unexpected"))
    try:
        with pytest.raises(AgentSDKError) as invalid:
            await sdk.runs.start(
                "ses_missing",
                AGENT,
                "must not start",
                idempotency_key=idempotency_key,
            )
        assert invalid.value.code is ErrorCode.INVALID_STATE
        assert invalid.value.message == "idempotency key is invalid"
        assert store.access_count == 0
    finally:
        await sdk.close()


async def test_completed_short_runs_do_not_accumulate_in_run_registry() -> None:
    completion = CountingCompletion()
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=completion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handles = []
        for index in range(8):
            handle = await sdk.runs.start(
                session.session_id,
                AGENT,
                f"input {index}",
            )
            handles.append(handle)
            await handle.result()

        await _yield_until_run_registry_empty(sdk)

        assert sdk.runs._tasks == {}  # type: ignore[attr-defined]
        assert completion.call_count == 8
    finally:
        await sdk.close()


async def test_failed_run_registry_does_not_retain_provider_failure() -> None:
    sentinel_refs: list[weakref.ReferenceType[_ProviderSentinel]] = []

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        sentinel = _ProviderSentinel()
        sentinel_refs.append(weakref.ref(sentinel))
        raise _SentinelProviderFailure(sentinel)

    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(session.session_id, AGENT, "fail")
        run_id = handle.run_id
        task_ref = weakref.ref(handle._task)  # type: ignore[attr-defined]
        assert await _consume_run_failure(handle) is ErrorCode.INTERNAL

        await _yield_until_run_registry_empty(sdk)
        for _ in range(2):
            await asyncio.sleep(0)
        del handle
        gc.collect()

        assert sentinel_refs[0]() is None
        assert task_ref() is None
        assert run_id not in sdk.runs._tasks  # type: ignore[attr-defined]
    finally:
        await sdk.close()


async def test_completed_replay_after_sqlite_reopen_is_detached_and_durable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completed-replay.db"
    completion = CountingCompletion("durable result")
    first_sdk = AgentSDK.for_test(database_path=database, acompletion=completion)
    try:
        session = await first_sdk.sessions.create(workspaces=[])
        first = await first_sdk.runs.start(
            session.session_id,
            AGENT,
            "same input",
            idempotency_key="run-request",
        )
        expected = await first.result()
    finally:
        await first_sdk.close()

    reopened = AgentSDK.for_test(database_path=database, acompletion=_must_not_call)
    try:
        replay = await reopened.runs.start(
            session.session_id,
            AGENT,
            "same input",
            idempotency_key="run-request",
        )

        assert replay.run_id == first.run_id
        assert replay.attached is False
        assert await asyncio.wait_for(replay.result(), timeout=1) == expected
        assert completion.call_count == 1
    finally:
        await reopened.close()


@pytest.mark.parametrize(
    "changed_agent",
    [
        AgentSpec(name="test", model="other/model", revision="1"),
        AgentSpec(
            name="test",
            model="fake/model",
            model_params={"temperature": 0.5},
            revision="1",
        ),
    ],
    ids=["model", "model-params"],
)
async def test_agent_content_change_conflicts_on_same_run_key(
    changed_agent: AgentSpec,
    blocking_sdk: tuple[AgentSDK, BlockingCompletion],
) -> None:
    sdk, completion = blocking_sdk
    session = await sdk.sessions.create(workspaces=[])
    first = await sdk.runs.start(
        session.session_id,
        AGENT,
        "same input",
        idempotency_key="run-request",
    )
    await asyncio.wait_for(completion.started.wait(), timeout=1)

    with pytest.raises(AgentSDKError) as raised:
        await sdk.runs.start(
            session.session_id,
            changed_agent,
            "same input",
            idempotency_key="run-request",
        )

    assert raised.value.code is ErrorCode.CONFLICT
    assert completion.call_count == 1
    completion.finish()
    await first.result()


async def _unused_tool_handler(_: ToolContext, **__: object) -> str:
    return "unused"


@pytest.mark.parametrize(
    "tool_update",
    [
        {"version": "2"},
        {"source": "mcp:test"},
        {"effects": ("network",)},
        {"timeout_seconds": 2.0},
        {
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
            }
        },
    ],
    ids=["version", "source", "effects", "timeout", "schema"],
)
async def test_tool_capability_change_conflicts_on_same_run_key(
    tool_update: dict[str, Any],
) -> None:
    store = InMemoryStore()
    completion = BlockingCompletion()
    first_sdk = AgentSDK.for_test(store=store, acompletion=completion)
    second_sdk = AgentSDK.for_test(store=store, acompletion=completion)
    base = ToolSpec(
        name="lookup",
        description="lookup value",
        input_schema={"type": "object", "properties": {}},
    )
    first_sdk.tools.register(base, _unused_tool_handler)
    second_sdk.tools.register(base.model_copy(update=tool_update), _unused_tool_handler)
    try:
        session = await first_sdk.sessions.create(workspaces=[])
        first = await first_sdk.runs.start(
            session.session_id,
            AGENT,
            "same input",
            idempotency_key="run-request",
        )
        await asyncio.wait_for(completion.started.wait(), timeout=1)

        with pytest.raises(AgentSDKError) as raised:
            await second_sdk.runs.start(
                session.session_id,
                AGENT,
                "same input",
                idempotency_key="run-request",
            )

        assert raised.value.code is ErrorCode.CONFLICT
        assert completion.call_count == 1
        completion.finish()
        await first.result()
    finally:
        completion.finish()
        await first_sdk.close()
        await second_sdk.close()


async def test_policy_change_conflicts_on_same_run_key() -> None:
    store = InMemoryStore()
    completion = BlockingCompletion()
    allow_sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    deny_sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="deny",
    )
    try:
        session = await allow_sdk.sessions.create(workspaces=[])
        first = await allow_sdk.runs.start(
            session.session_id,
            AGENT,
            "same input",
            idempotency_key="run-request",
        )
        await asyncio.wait_for(completion.started.wait(), timeout=1)

        with pytest.raises(AgentSDKError) as raised:
            await deny_sdk.runs.start(
                session.session_id,
                AGENT,
                "same input",
                idempotency_key="run-request",
            )

        assert raised.value.code is ErrorCode.CONFLICT
        assert completion.call_count == 1
        completion.finish()
        await first.result()
    finally:
        completion.finish()
        await allow_sdk.close()
        await deny_sdk.close()


class _RetainDeletingStore(InMemoryStore):
    async def delete_session(self, session_id: str) -> None:
        del session_id
        raise RuntimeError("must-not-leak-delete-failure")


async def test_deleting_session_rejects_old_matching_run_key() -> None:
    store = _RetainDeletingStore()
    completion = CountingCompletion()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        first = await sdk.runs.start(
            session.session_id,
            AGENT,
            "same input",
            idempotency_key="run-request",
        )
        await first.result()
        await sdk.sessions.close(session.session_id)
        with pytest.raises(AgentSDKError):
            await sdk.sessions.delete(session.session_id)

        with pytest.raises(AgentSDKError) as raised:
            await sdk.runs.start(
                session.session_id,
                AGENT,
                "same input",
                idempotency_key="run-request",
            )

        assert raised.value.code is ErrorCode.INVALID_STATE
        assert completion.call_count == 1
    finally:
        await sdk.close()


async def test_detached_nonterminal_replay_requires_recovery_without_execution(
    tmp_path: Path,
) -> None:
    database = tmp_path / "detached-created.db"
    from agent_sdk.storage.sqlite import SQLiteStore

    store = await SQLiteStore.open(database)
    try:
        commands = RuntimeCommands(store)
        session = await commands.create_session(workspaces=[])
        descriptor = ExecutionDescriptor.create(
            agent=AGENT,
            messages=({"role": "user", "content": "same input"},),
            tools=(),
            workspace_scopes=(),
            policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
        )
        outcome = await commands.start_run(
            session.session_id,
            agent_revision="test:1",
            user_input="same input",
            execution_descriptor=descriptor,
            idempotency_key="run-request",
        )
    finally:
        await store.close()

    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("detached replay must not call provider")

    reopened = AgentSDK.for_test(
        database_path=database,
        acompletion=provider,
        enable_builtin_tools=False,
    )
    try:
        replay = await reopened.runs.start(
            session.session_id,
            AGENT,
            "same input",
            idempotency_key="run-request",
        )

        assert replay.run_id == outcome.value.run_id
        assert replay.attached is False
        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(replay.result(), timeout=1)
        assert raised.value.code is ErrorCode.CONFLICT
        assert raised.value.message == "recovery required"
        assert raised.value.retryable is True
        assert provider_calls == 0
        events = [stored async for stored in replay.events()]
        assert [stored.event.type for stored in events] == ["run.created"]
    finally:
        await reopened.close()


class _CloseStartRaceStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.run_ready = asyncio.Event()
        self.close_ready = asyncio.Event()
        self.release_run = asyncio.Event()
        self.release_close = asyncio.Event()
        self._blocked_run = False
        self._blocked_close = False

    async def commit(self, batch: CommitBatch) -> CommitResult:
        event_types = {event.type for event in batch.events}
        if "session.run.attached" in event_types and not self._blocked_run:
            self._blocked_run = True
            self.run_ready.set()
            await asyncio.wait_for(self.release_run.wait(), timeout=2)
        if (
            event_types.intersection({"session.closed", "session.closing"})
            and not self._blocked_close
        ):
            self._blocked_close = True
            self.close_ready.set()
            await asyncio.wait_for(self.release_close.wait(), timeout=2)
        return await super().commit(batch)


@pytest.mark.parametrize("winner", ["close", "start"])
async def test_close_start_race_linearizes_without_orphaned_run(winner: str) -> None:
    store = _CloseStartRaceStore()
    completion = BlockingCompletion()
    starting_sdk = AgentSDK.for_test(store=store, acompletion=completion)
    closing_sdk = AgentSDK.for_test(store=store, acompletion=completion)
    start_task: asyncio.Task[Any] | None = None
    close_task: asyncio.Task[Any] | None = None
    handle = None
    try:
        session = await starting_sdk.sessions.create(workspaces=[])
        start_task = asyncio.create_task(starting_sdk.runs.start(session.session_id, AGENT, "race"))
        await asyncio.wait_for(store.run_ready.wait(), timeout=1)
        close_task = asyncio.create_task(closing_sdk.sessions.close(session.session_id))
        await asyncio.wait_for(store.close_ready.wait(), timeout=1)

        if winner == "close":
            store.release_close.set()
            closed = await asyncio.wait_for(close_task, timeout=1)
            assert closed.status is SessionStatus.CLOSED
            store.release_run.set()
            with pytest.raises(AgentSDKError) as raised:
                await asyncio.wait_for(start_task, timeout=1)
            assert raised.value.code is ErrorCode.INVALID_STATE
            assert completion.call_count == 0
        else:
            store.release_run.set()
            handle = await asyncio.wait_for(start_task, timeout=1)
            await asyncio.wait_for(completion.started.wait(), timeout=1)
            store.release_close.set()
            closing = await asyncio.wait_for(close_task, timeout=1)
            assert closing.status is SessionStatus.CLOSING
            completion.finish()
            await asyncio.wait_for(handle.result(), timeout=1)
            assert (
                await starting_sdk.sessions.get(session.session_id)
            ).status is SessionStatus.CLOSED
    finally:
        store.release_run.set()
        store.release_close.set()
        completion.finish()
        for task in (start_task, close_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (start_task, close_task) if task is not None),
            return_exceptions=True,
        )
        await starting_sdk.close()
        await closing_sdk.close()


class _TerminalRaceStore(InMemoryStore):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.terminal_attempts = 0
        self._rejected_batches: dict[int, RunProgressBatch] = {}

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        if any(event.type in {"run.completed", "run.failed"} for event in batch.events):
            batch_id = id(batch)
            if batch_id not in self._rejected_batches:
                self.terminal_attempts += 1
                if self.failures:
                    self.failures -= 1
                    self._rejected_batches[batch_id] = batch
            if batch_id in self._rejected_batches:
                raise RecoveryStateConflictError
        return await super().commit_run_progress(batch)


async def test_terminal_session_race_retries_once_and_writes_one_terminal_event() -> None:
    store = _TerminalRaceStore(failures=1)
    completion = BlockingCompletion()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(session.session_id, AGENT, "retry terminal")
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        assert (await sdk.sessions.close(session.session_id)).status is SessionStatus.CLOSING
        completion.finish()

        await asyncio.wait_for(handle.result(), timeout=1)

        assert store.terminal_attempts == 2
        assert (await sdk.sessions.get(session.session_id)).status is SessionStatus.CLOSED
        run_events = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == handle.run_id
        ]
        assert run_events.count("run.completed") == 1
    finally:
        completion.finish()
        await sdk.close()


async def test_terminal_session_race_exhaustion_is_retryable_and_nonterminal() -> None:
    store = _TerminalRaceStore(failures=8)
    completion = BlockingCompletion()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(session.session_id, AGENT, "exhaust terminal")
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        await sdk.sessions.close(session.session_id)
        completion.finish()

        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(handle.result(), timeout=1)

        assert raised.value.code is ErrorCode.CONFLICT
        assert raised.value.message == "session state changed concurrently"
        assert raised.value.retryable is True
        assert store.terminal_attempts == 8
        current = await sdk.sessions.get(session.session_id)
        assert current.status is SessionStatus.CLOSING
        assert current.active_run_ids == (handle.run_id,)
        assert all(
            stored.event.type != "run.completed"
            for stored in await store.read_events(after_cursor=0)
        )
    finally:
        completion.finish()
        await sdk.close()


class _FailingTerminalStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.error = RuntimeError("must-not-leak-terminal-store-secret")

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        if any(event.type in {"run.completed", "run.failed"} for event in batch.events):
            raise self.error
        return await super().commit_run_progress(batch)


async def test_terminal_commit_failure_rolls_back_both_aggregates_and_is_sanitized() -> None:
    store = _FailingTerminalStore()
    completion = BlockingCompletion()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(session.session_id, AGENT, "rollback terminal")
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        await sdk.sessions.close(session.session_id)
        completion.finish()

        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(handle.result(), timeout=1)

        assert raised.value.code is ErrorCode.INTERNAL
        _assert_context_free_sanitizer(
            raised.value,
            secret="must-not-leak-terminal-store-secret",
            original=store.error,
        )
        current = await sdk.sessions.get(session.session_id)
        run = await sdk.runs.get(handle.run_id)
        assert current.status is SessionStatus.CLOSING
        assert current.active_run_ids == (handle.run_id,)
        assert run.status.value == "running"
        assert all(
            stored.event.type != "run.completed"
            for stored in await store.read_events(after_cursor=0)
        )
    finally:
        completion.finish()
        await sdk.close()


async def test_terminal_commit_rejects_session_that_no_longer_owns_run() -> None:
    store = InMemoryStore()
    completion = BlockingCompletion()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(session.session_id, AGENT, "corrupt owner")
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        current = await sdk.sessions.get(session.session_id)
        corrupted = current.model_copy(
            update={"active_run_ids": (), "version": current.version + 1}
        )
        await store.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="session.test.owner-removed",
                        session_id=session.session_id,
                        run_id=None,
                        sequence=corrupted.version,
                        payload={},
                    ),
                ),
                snapshots=(session_write(corrupted),),
                preconditions=(exact_session_precondition(current),),
            )
        )
        completion.finish()

        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(handle.result(), timeout=1)

        assert raised.value.code is ErrorCode.CONFLICT
        assert raised.value.message == "run is not owned by session"
        assert all(
            stored.event.type != "run.completed"
            for stored in await store.read_events(after_cursor=0)
        )
    finally:
        completion.finish()
        await sdk.close()


class _StartBoundaryStore(InMemoryStore):
    def __init__(self, phase: str) -> None:
        super().__init__()
        self.phase = phase
        self.enabled = False
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self._blocked = False

    async def _maybe_block(self, phase: str) -> None:
        if self.enabled and self.phase == phase and not self._blocked:
            self._blocked = True
            self.reached.set()
            await asyncio.wait_for(self.release.wait(), timeout=2)

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        if kind == "session":
            await self._maybe_block("session-load")
        return await super().get_snapshot(kind, entity_id)

    async def get_idempotency(self, scope: str, key: str):
        await self._maybe_block("idempotency-hint")
        return await super().get_idempotency(scope, key)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(event.type == "session.run.attached" for event in batch.events):
            await self._maybe_block("commit")
        return await super().commit(batch)


@pytest.mark.parametrize(
    "phase",
    ["session-load", "idempotency-hint", "commit", "post-command"],
)
async def test_cancelled_start_finishes_registration_before_reraising(
    phase: str,
) -> None:
    store = _StartBoundaryStore(phase)
    completion = BlockingCompletion()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    starter: asyncio.Task[Any] | None = None
    original_start = sdk.runs._commands.start_run  # type: ignore[attr-defined]
    try:
        session = await sdk.sessions.create(workspaces=[])
        if phase == "post-command":

            async def blocked_start(*args: Any, **kwargs: Any):
                outcome = await original_start(*args, **kwargs)
                store.reached.set()
                await asyncio.wait_for(store.release.wait(), timeout=2)
                return outcome

            sdk.runs._commands.start_run = blocked_start  # type: ignore[attr-defined,method-assign]
        store.enabled = True
        starter = asyncio.create_task(
            sdk.runs.start(
                session.session_id,
                AGENT,
                "cancel handoff",
                idempotency_key="run-request",
            )
        )
        await asyncio.wait_for(store.reached.wait(), timeout=1)
        starter.cancel("first caller cancellation")
        store.release.set()

        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(starter, timeout=1)
        assert raised.value.args == ("first caller cancellation",)

        replay = await sdk.runs.start(
            session.session_id,
            AGENT,
            "cancel handoff",
            idempotency_key="run-request",
        )
        assert replay.attached is True
        await asyncio.wait_for(completion.started.wait(), timeout=1)
        assert completion.call_count == 1
        completion.finish()
        assert (await asyncio.wait_for(replay.result(), timeout=1)).output_text == "done"
    finally:
        store.release.set()
        completion.finish()
        sdk.runs._commands.start_run = original_start  # type: ignore[attr-defined,method-assign]
        if starter is not None and not starter.done():
            starter.cancel()
        if starter is not None:
            await asyncio.gather(starter, return_exceptions=True)
        await sdk.close()


@pytest.mark.parametrize("corruption", ["descriptor", "terminal-result"])
async def test_corrupted_sqlite_run_replay_is_context_free_and_does_not_execute(
    tmp_path: Path,
    corruption: str,
) -> None:
    database = tmp_path / f"corrupt-{corruption}.db"
    completion = CountingCompletion()
    first_sdk = AgentSDK.for_test(database_path=database, acompletion=completion)
    try:
        session = await first_sdk.sessions.create(workspaces=[])
        first = await first_sdk.runs.start(
            session.session_id,
            AGENT,
            "same input",
            idempotency_key="run-request",
        )
        await first.result()
    finally:
        await first_sdk.close()

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT result_json FROM idempotency_records WHERE key = ?",
            ("run-request",),
        ).fetchone()
        assert row is not None
        result = json.loads(row[0])
        if corruption == "descriptor":
            result["execution_descriptor"]["private_secret"] = "must-not-leak-corrupt-replay"
        else:
            result["output_text"] = None
            result["private_secret"] = "must-not-leak-corrupt-replay"
        connection.execute(
            "UPDATE idempotency_records SET result_json = ? WHERE key = ?",
            (json.dumps(result), "run-request"),
        )
        connection.commit()

    provider_calls = 0

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("corrupt replay must not execute")

    reopened = AgentSDK.for_test(database_path=database, acompletion=provider)
    try:
        with pytest.raises(AgentSDKError) as raised:
            await reopened.runs.start(
                session.session_id,
                AGENT,
                "same input",
                idempotency_key="run-request",
            )

        assert raised.value.code is ErrorCode.INTERNAL
        _assert_context_free_sanitizer(
            raised.value,
            secret="must-not-leak-corrupt-replay",
        )
        assert provider_calls == 0
    finally:
        await reopened.close()


async def test_run_start_rejects_valid_foreign_current_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "valid-foreign-run-result.db"
    completion = CountingCompletion()
    first_sdk = AgentSDK.for_test(database_path=database, acompletion=completion)
    try:
        session = await first_sdk.sessions.create(workspaces=[])
        expected = await first_sdk.runs.start(
            session.session_id,
            AGENT,
            "expected input",
            idempotency_key="expected-run",
        )
        await expected.result()
        foreign = await first_sdk.runs.start(
            session.session_id,
            AgentSpec(
                name="test",
                model="fake/model",
                revision="1",
                model_params={"private": "must-not-leak-foreign-run"},
            ),
            "foreign input",
            idempotency_key="foreign-run",
        )
        await foreign.result()
    finally:
        await first_sdk.close()

    with sqlite3.connect(database) as connection:
        before_events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        connection.execute(
            "UPDATE idempotency_records "
            "SET result_json = ("
            "SELECT result_json FROM idempotency_records WHERE key = 'foreign-run'"
            ") WHERE key = 'expected-run'"
        )
        connection.commit()

    replay_completion = CountingCompletion("unexpected")
    reopened = AgentSDK.for_test(
        database_path=database,
        acompletion=replay_completion,
    )
    try:
        with pytest.raises(AgentSDKError) as substituted:
            await reopened.runs.start(
                session.session_id,
                AGENT,
                "expected input",
                idempotency_key="expected-run",
            )
        assert substituted.value.code is ErrorCode.INTERNAL
        assert substituted.value.retryable is False
        _assert_context_free_sanitizer(
            substituted.value,
            secret="must-not-leak-foreign-run",
        )
        assert replay_completion.call_count == 0
    finally:
        await reopened.close()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == (before_events)


class _DetachedFailingStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.error = RuntimeError("must-not-leak-detached-store-secret")

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        del kind, entity_id
        raise self.error


async def test_detached_result_store_failure_is_context_free() -> None:
    from agent_sdk.runtime.handles import RunHandle

    store = _DetachedFailingStore()
    handle = RunHandle("run_detached", store, None)

    with pytest.raises(AgentSDKError) as raised:
        await handle.result()

    assert raised.value.code is ErrorCode.INTERNAL
    _assert_context_free_sanitizer(
        raised.value,
        secret="must-not-leak-detached-store-secret",
        original=store.error,
    )


class _CorruptHintStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.enabled = False
        self.error = IdempotencyCorruptionError("must-not-leak-corrupt-idempotency-hint")

    async def get_idempotency(self, scope: str, key: str):
        if self.enabled:
            raise self.error
        return await super().get_idempotency(scope, key)


async def test_corrupted_run_idempotency_hint_is_context_free_and_does_not_execute() -> None:
    store = _CorruptHintStore()
    completion = CountingCompletion()
    sdk = AgentSDK.for_test(store=store, acompletion=completion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        store.enabled = True

        with pytest.raises(AgentSDKError) as raised:
            await sdk.runs.start(
                session.session_id,
                AGENT,
                "same input",
                idempotency_key="run-request",
            )

        assert raised.value.code is ErrorCode.INTERNAL
        _assert_context_free_sanitizer(
            raised.value,
            secret="must-not-leak-corrupt-idempotency-hint",
            original=store.error,
        )
        assert completion.call_count == 0
    finally:
        await sdk.close()


class _LegacyIdempotencyGuardStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.access_count = 0

    def _record_access(self) -> None:
        if self.armed:
            self.access_count += 1

    async def commit(self, batch: CommitBatch) -> CommitResult:
        self._record_access()
        return await super().commit(batch)

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        self._record_access()
        return await super().get_snapshot(kind, entity_id)

    async def get_idempotency(self, scope: str, key: str):
        self._record_access()
        return await super().get_idempotency(scope, key)


async def test_legacy_run_start_rejects_idempotency_before_store_access() -> None:
    store = _LegacyIdempotencyGuardStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    cursor_before = await store.latest_cursor()
    store.armed = True

    for revision in ("legacy:1", "legacy:2"):
        with pytest.raises(AgentSDKError) as raised:
            await commands.start_run(
                session.session_id,
                agent_revision=revision,
                user_input="same input",
                execution_descriptor=None,
                idempotency_key="legacy-run-request",
            )

        assert raised.value.code is ErrorCode.INVALID_STATE
        assert raised.value.retryable is False
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

    assert store.access_count == 0
    store.armed = False
    assert await store.latest_cursor() == cursor_before
    assert (
        await store.get_idempotency(
            f"session/{session.session_id}/run.start",
            "legacy-run-request",
        )
        is None
    )


@pytest.mark.asyncio
async def test_explicit_run_id_idempotency_replays_only_the_exact_selected_id() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    descriptor = ExecutionDescriptor.create(
        agent=AGENT,
        messages=({"role": "user", "content": "workflow input"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )

    first = await commands.start_run(
        session.session_id,
        run_id="run_workflow_selected",
        agent_revision="test:1",
        user_input="workflow input",
        execution_descriptor=descriptor,
        idempotency_key="workflow-node",
    )
    replay = await commands.start_run(
        session.session_id,
        run_id="run_workflow_selected",
        agent_revision="test:1",
        user_input="workflow input",
        execution_descriptor=descriptor,
        idempotency_key="workflow-node",
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.run_id == "run_workflow_selected"

    with pytest.raises(AgentSDKError) as conflict:
        await commands.start_run(
            session.session_id,
            run_id="run_substituted",
            agent_revision="test:1",
            user_input="workflow input",
            execution_descriptor=descriptor,
            idempotency_key="workflow-node",
        )

    assert conflict.value.code is ErrorCode.CONFLICT
    assert conflict.value.retryable is False
    assert await store.get_snapshot("run", "run_substituted") is None


@pytest.mark.asyncio
async def test_omitted_run_id_idempotency_replay_keeps_generated_id_compatibility() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    descriptor = ExecutionDescriptor.create(
        agent=AGENT,
        messages=({"role": "user", "content": "generated input"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )

    first = await commands.start_run(
        session.session_id,
        agent_revision="test:1",
        user_input="generated input",
        execution_descriptor=descriptor,
        idempotency_key="generated-run",
    )
    replay = await commands.start_run(
        session.session_id,
        agent_revision="test:1",
        user_input="generated input",
        execution_descriptor=descriptor,
        idempotency_key="generated-run",
    )

    assert replay.replayed is True
    assert replay.run_id == first.run_id


@pytest.mark.asyncio
async def test_sqlite_explicit_run_id_idempotency_rejects_substitution(
    tmp_path: Path,
) -> None:
    from agent_sdk.storage.sqlite import SQLiteStore

    store = await SQLiteStore.open(tmp_path / "explicit-run-id.db")
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    descriptor = ExecutionDescriptor.create(
        agent=AGENT,
        messages=({"role": "user", "content": "workflow input"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
    )
    try:
        first = await commands.start_run(
            session.session_id,
            run_id="run_sqlite_selected",
            agent_revision="test:1",
            user_input="workflow input",
            execution_descriptor=descriptor,
            idempotency_key="workflow-node",
        )
        replay = await commands.start_run(
            session.session_id,
            run_id="run_sqlite_selected",
            agent_revision="test:1",
            user_input="workflow input",
            execution_descriptor=descriptor,
            idempotency_key="workflow-node",
        )

        assert replay.replayed is True
        assert replay.run_id == first.run_id
        with pytest.raises(AgentSDKError) as conflict:
            await commands.start_run(
                session.session_id,
                run_id="run_sqlite_substituted",
                agent_revision="test:1",
                user_input="workflow input",
                execution_descriptor=descriptor,
                idempotency_key="workflow-node",
            )
        assert conflict.value.code is ErrorCode.CONFLICT
        assert await store.get_snapshot("run", "run_sqlite_substituted") is None
    finally:
        await store.close()
