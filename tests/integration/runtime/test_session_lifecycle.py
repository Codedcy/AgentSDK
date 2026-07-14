from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    ErrorCode,
    SessionBusyError,
    SessionSnapshot,
    SessionStatus,
)
from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    SnapshotPreconditionError,
    SnapshotWrite,
)
from agent_sdk.storage.idempotency import IdempotencyReplay
from agent_sdk.storage.memory import InMemoryStore


async def _unused_acompletion(**_: object) -> AsyncIterator[dict[str, Any]]:
    raise AssertionError("session lifecycle must not call the model provider")


@pytest.fixture
async def sdk() -> AsyncIterator[AgentSDK]:
    instance = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unused_acompletion,
    )
    try:
        yield instance
    finally:
        await instance.close()


async def test_empty_session_closes_immediately_and_delete_removes_it(
    sdk: AgentSDK,
) -> None:
    session = await sdk.sessions.create(workspaces=[])

    closed = await sdk.sessions.close(session.session_id)

    assert closed.status is SessionStatus.CLOSED
    assert closed.version == session.version + 1
    await sdk.sessions.delete(session.session_id)
    with pytest.raises(AgentSDKError) as raised:
        await sdk.sessions.get(session.session_id)
    assert raised.value.code is ErrorCode.NOT_FOUND


async def test_close_is_same_state_idempotent(sdk: AgentSDK) -> None:
    session = await sdk.sessions.create(workspaces=[])

    first = await sdk.sessions.close(session.session_id)
    second = await sdk.sessions.close(session.session_id)

    assert second == first


async def test_active_session_cannot_be_deleted(sdk: AgentSDK) -> None:
    session = await sdk.sessions.create(workspaces=[])

    with pytest.raises(SessionBusyError) as raised:
        await sdk.sessions.delete(session.session_id)

    assert raised.value.code is ErrorCode.CONFLICT
    assert raised.value.retryable is False


async def _sdk_with_store(store: InMemoryStore) -> AgentSDK:
    return AgentSDK.for_test(store=store, acompletion=_unused_acompletion)


async def test_empty_close_persists_only_the_composite_closed_transition() -> None:
    store = InMemoryStore()
    instance = await _sdk_with_store(store)
    try:
        session = await instance.sessions.create(workspaces=[])

        closed = await instance.sessions.close(session.session_id)

        events = await store.read_events(after_cursor=0)
        assert [stored.event.type for stored in events] == [
            "session.created",
            "session.closed",
        ]
        assert closed.version == session.version + 1
        assert events[-1].event.payload["status"] == "closed"
    finally:
        await instance.close()


async def _store_session(store: InMemoryStore, snapshot: SessionSnapshot) -> None:
    data = snapshot.model_dump(mode="json")
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="session.created",
                    session_id=snapshot.session_id,
                    run_id=None,
                    sequence=snapshot.version,
                    payload=data,
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "session",
                    snapshot.session_id,
                    snapshot.session_id,
                    snapshot.version,
                    data,
                ),
            ),
        )
    )


async def test_session_with_active_work_closes_to_closing_and_remains_busy() -> None:
    store = InMemoryStore()
    await _store_session(
        store,
        SessionSnapshot(
            session_id="ses_busy",
            workspaces=(),
            active_run_ids=("run_1",),
        ),
    )
    instance = await _sdk_with_store(store)
    try:
        closing = await instance.sessions.close("ses_busy")

        assert closing.status is SessionStatus.CLOSING
        assert closing.active_run_ids == ("run_1",)
        with pytest.raises(SessionBusyError):
            await instance.sessions.delete("ses_busy")
    finally:
        await instance.close()


async def test_matching_concurrent_create_returns_one_durable_session() -> None:
    store = InMemoryStore()
    first_instance = await _sdk_with_store(store)
    second_instance = await _sdk_with_store(store)
    try:
        first, second = await asyncio.gather(
            first_instance.sessions.create(
                workspaces=[Path("one"), "two"], idempotency_key="key"
            ),
            second_instance.sessions.create(
                workspaces=[Path("one"), "two"], idempotency_key="key"
            ),
        )

        assert first == second
        assert first.workspaces == ("one", "two")
        events = await store.read_events(after_cursor=0)
        assert [stored.event.type for stored in events] == ["session.created"]
    finally:
        await first_instance.close()
        await second_instance.close()


async def test_same_create_key_with_different_workspace_order_conflicts() -> None:
    store = InMemoryStore()
    first_instance = await _sdk_with_store(store)
    second_instance = await _sdk_with_store(store)
    try:
        outcomes = await asyncio.gather(
            first_instance.sessions.create(
                workspaces=["one", "two"], idempotency_key="same"
            ),
            second_instance.sessions.create(
                workspaces=["two", "one"], idempotency_key="same"
            ),
            return_exceptions=True,
        )

        snapshots = [value for value in outcomes if isinstance(value, SessionSnapshot)]
        errors = [value for value in outcomes if isinstance(value, AgentSDKError)]
        assert len(snapshots) == 1
        assert len(errors) == 1
        assert errors[0].code is ErrorCode.CONFLICT
        assert errors[0].message == "idempotency key conflicts with another request"
        assert errors[0].retryable is False
        assert len(await store.read_events(after_cursor=0)) == 1
    finally:
        await first_instance.close()
        await second_instance.close()


@pytest.mark.parametrize("key", ["", "x" * 257])
async def test_create_rejects_invalid_idempotency_key(key: str) -> None:
    store = InMemoryStore()
    instance = await _sdk_with_store(store)
    try:
        with pytest.raises(AgentSDKError) as raised:
            await instance.sessions.create(workspaces=[], idempotency_key=key)

        assert raised.value.code is ErrorCode.INVALID_STATE
        assert raised.value.message == "idempotency key is invalid"
        assert raised.value.retryable is False
        assert await store.read_events(after_cursor=0) == []
    finally:
        await instance.close()


async def test_create_replay_survives_sqlite_reopen(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    first_sdk = AgentSDK.for_test(
        database_path=database,
        acompletion=_unused_acompletion,
    )
    first = await first_sdk.sessions.create(
        workspaces=["workspace"],
        idempotency_key="reopen",
    )
    await first_sdk.close()

    second_sdk = AgentSDK.for_test(
        database_path=database,
        acompletion=_unused_acompletion,
    )
    try:
        replayed = await second_sdk.sessions.create(
            workspaces=["workspace"],
            idempotency_key="reopen",
        )

        assert replayed == first
    finally:
        await second_sdk.close()


async def test_malformed_stored_create_result_is_sanitized(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    first_sdk = AgentSDK.for_test(
        database_path=database,
        acompletion=_unused_acompletion,
    )
    await first_sdk.sessions.create(workspaces=[], idempotency_key="corrupt")
    await first_sdk.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE idempotency_records SET result_json = ? WHERE scope = ? AND key = ?",
            ('{"secret":"must-not-leak"}', "session.create", "corrupt"),
        )
        connection.commit()

    second_sdk = AgentSDK.for_test(
        database_path=database,
        acompletion=_unused_acompletion,
    )
    try:
        with pytest.raises(AgentSDKError) as raised:
            await second_sdk.sessions.create(workspaces=[], idempotency_key="corrupt")

        assert raised.value.code is ErrorCode.INTERNAL
        assert raised.value.retryable is False
        assert "must-not-leak" not in str(raised.value)
        assert "must-not-leak" not in repr(raised.value)
    finally:
        await second_sdk.close()


class _RetainDeletingStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.retain_once = True

    async def delete_session(self, session_id: str) -> None:
        if self.retain_once:
            self.retain_once = False
            raise RuntimeError("must-not-leak-delete-failure")
        await super().delete_session(session_id)


async def test_delete_retries_retained_deleting_and_deleting_has_precedence() -> None:
    store = _RetainDeletingStore()
    instance = await _sdk_with_store(store)
    try:
        session = await instance.sessions.create(workspaces=[])
        await instance.sessions.close(session.session_id, idempotency_key="close-key")

        with pytest.raises(AgentSDKError) as failed_delete:
            await instance.sessions.delete(session.session_id)
        assert failed_delete.value.code is ErrorCode.INTERNAL
        assert "must-not-leak-delete-failure" not in str(failed_delete.value)
        retained = await store.get_snapshot("session", session.session_id)
        assert retained is not None
        assert retained["status"] == "deleting"

        for operation in (
            instance.sessions.get(session.session_id),
            instance.sessions.close(session.session_id, idempotency_key="close-key"),
        ):
            with pytest.raises(AgentSDKError) as rejected:
                await operation
            assert rejected.value.code is ErrorCode.INVALID_STATE
            assert rejected.value.message == "session is deleting"

        await instance.sessions.delete(session.session_id)
        assert await store.get_snapshot("session", session.session_id) is None
    finally:
        await instance.close()


class _ReplayDeleteRaceStore(_RetainDeletingStore):
    def __init__(self) -> None:
        super().__init__()
        self.block_close_replay = False
        self.close_replay_ready = asyncio.Event()
        self.release_close_replay = asyncio.Event()

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if (
            self.block_close_replay
            and isinstance(batch.idempotency, IdempotencyReplay)
            and batch.idempotency.scope.endswith("/close")
        ):
            self.close_replay_ready.set()
            await self.release_close_replay.wait()
        return await super().commit(batch)


async def test_close_replay_loses_to_deleting_under_exact_precondition() -> None:
    store = _ReplayDeleteRaceStore()
    instance = await _sdk_with_store(store)
    deleting_instance = await _sdk_with_store(store)
    replay: asyncio.Task[SessionSnapshot] | None = None
    try:
        session = await instance.sessions.create(workspaces=[])
        await instance.sessions.close(session.session_id, idempotency_key="close-key")
        store.block_close_replay = True
        replay = asyncio.create_task(
            instance.sessions.close(session.session_id, idempotency_key="close-key")
        )
        await asyncio.wait_for(store.close_replay_ready.wait(), timeout=1)

        with pytest.raises(AgentSDKError):
            await deleting_instance.sessions.delete(session.session_id)
        store.release_close_replay.set()

        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(replay, timeout=1)
        assert raised.value.code is ErrorCode.INVALID_STATE
        assert raised.value.message == "session is deleting"
    finally:
        store.release_close_replay.set()
        if replay is not None and not replay.done():
            replay.cancel()
        if replay is not None:
            await asyncio.gather(replay, return_exceptions=True)
        await instance.close()
        await deleting_instance.close()


class _AlwaysRacingStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.transition_attempts = 0

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(event.type in {"session.closed", "session.closing"} for event in batch.events):
            self.transition_attempts += 1
            raise SnapshotPreconditionError("synthetic race")
        return await super().commit(batch)


async def test_close_bounds_exact_precondition_retries() -> None:
    store = _AlwaysRacingStore()
    instance = await _sdk_with_store(store)
    try:
        session = await instance.sessions.create(workspaces=[])

        with pytest.raises(AgentSDKError) as raised:
            await instance.sessions.close(session.session_id)

        assert raised.value.code is ErrorCode.CONFLICT
        assert raised.value.retryable is True
        assert store.transition_attempts == 8
    finally:
        await instance.close()


class _FailingCommandStore(InMemoryStore):
    def __init__(self, operation: str, error: BaseException) -> None:
        super().__init__()
        self.operation = operation
        self.error = error
        self.enabled = False

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if self.enabled and self.operation == "commit":
            raise self.error
        return await super().commit(batch)

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        if self.enabled and self.operation == "get_snapshot":
            raise self.error
        return await super().get_snapshot(kind, entity_id)

    async def delete_session(self, session_id: str) -> None:
        if self.enabled and self.operation == "delete_session":
            raise self.error
        await super().delete_session(session_id)


@pytest.mark.parametrize(
    "operation",
    ["create", "get", "close", "delete"],
)
async def test_custom_store_failures_are_sanitized(operation: str) -> None:
    store_operation = "commit" if operation in {"create", "close"} else (
        "get_snapshot" if operation == "get" else "delete_session"
    )
    store = _FailingCommandStore(
        store_operation,
        RuntimeError("must-not-leak-custom-store-failure"),
    )
    instance = await _sdk_with_store(store)
    try:
        session = None
        if operation != "create":
            session = await instance.sessions.create(workspaces=[])
            if operation == "delete":
                await instance.sessions.close(session.session_id)
        store.enabled = True

        with pytest.raises(AgentSDKError) as raised:
            if operation == "create":
                await instance.sessions.create(workspaces=[])
            elif operation == "get":
                assert session is not None
                await instance.sessions.get(session.session_id)
            elif operation == "close":
                assert session is not None
                await instance.sessions.close(session.session_id)
            else:
                assert session is not None
                await instance.sessions.delete(session.session_id)

        assert raised.value.code is ErrorCode.INTERNAL
        assert raised.value.retryable is False
        assert "must-not-leak-custom-store-failure" not in str(raised.value)
        assert "must-not-leak-custom-store-failure" not in repr(raised.value)
    finally:
        await instance.close()


async def test_store_cancelled_error_propagates_unchanged() -> None:
    cancellation = asyncio.CancelledError()
    store = _FailingCommandStore("commit", cancellation)
    store.enabled = True
    instance = await _sdk_with_store(store)
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            await instance.sessions.create(workspaces=[])

        assert raised.value is cancellation
    finally:
        await instance.close()
