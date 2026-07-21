from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_sdk import AgentSDKError, ErrorCode
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.models import RunSnapshot, RunStatus, TokenUsage
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    SnapshotPrecondition,
    SnapshotWrite,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.subagents import AgentMessage, MailboxService, MailboxSnapshot


async def _related_runs(
    store: InMemoryStore,
) -> tuple[str, str, str]:
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    parent = await commands.start_run(
        session.session_id,
        agent_revision="parent:1",
        user_input="parent",
    )
    child = await commands.start_run(
        session.session_id,
        agent_revision="child:1",
        user_input="child",
        parent_run_id=parent.run_id,
    )
    return session.session_id, parent.run_id, child.run_id


def test_mailbox_snapshot_version_is_not_derived_from_message_count() -> None:
    message = AgentMessage(
        message_id="msg_1",
        session_id="ses_1",
        sender_run_id="run_parent",
        recipient_run_id="run_child",
        sequence=1,
        content="one",
        created_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    snapshot = MailboxSnapshot(
        recipient_run_id="run_child",
        session_id="ses_1",
        messages=(message,),
    )

    assert snapshot.version == 1


@pytest.mark.asyncio
async def test_send_assigns_monotonic_recipient_sequence() -> None:
    store = InMemoryStore()
    _, parent_id, child_id = await _related_runs(store)
    mailbox = MailboxService(store)

    first = await mailbox.send(parent_id, child_id, "one")
    second = await mailbox.send(parent_id, child_id, "two")

    assert (first.sequence, second.sequence) == (1, 2)
    assert await mailbox.unread(child_id) == (first, second)


@pytest.mark.asyncio
async def test_unrelated_runs_cannot_message() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    parent = await commands.start_run(
        session.session_id,
        agent_revision="parent:1",
        user_input="parent",
    )
    first = await commands.start_run(
        session.session_id,
        agent_revision="child:1",
        user_input="first",
        parent_run_id=parent.run_id,
    )
    second = await commands.start_run(
        session.session_id,
        agent_revision="child:1",
        user_input="second",
        parent_run_id=parent.run_id,
    )

    with pytest.raises(AgentSDKError, match="direct parent or child"):
        await MailboxService(store).send(first.run_id, second.run_id, "not allowed")


@pytest.mark.asyncio
async def test_cross_session_parent_reference_cannot_message() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    first_session = await commands.create_session(workspaces=[])
    second_session = await commands.create_session(workspaces=[])
    parent = await commands.start_run(
        first_session.session_id,
        agent_revision="parent:1",
        user_input="parent",
    )
    foreign_child = await commands.start_run(
        second_session.session_id,
        agent_revision="child:1",
        user_input="child",
        parent_run_id=parent.run_id,
    )

    with pytest.raises(AgentSDKError, match="direct parent or child") as raised:
        await MailboxService(store).send(parent.run_id, foreign_child.run_id, "no")
    assert raised.value.code is ErrorCode.INVALID_STATE


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["sender", "recipient"])
async def test_missing_run_fails_without_disclosing_which_relation_exists(
    missing: str,
) -> None:
    store = InMemoryStore()
    _, parent_id, child_id = await _related_runs(store)
    sender_id = "run_missing" if missing == "sender" else parent_id
    recipient_id = "run_missing" if missing == "recipient" else child_id

    with pytest.raises(AgentSDKError) as raised:
        await MailboxService(store).send(sender_id, recipient_id, "hello")
    assert raised.value.code is ErrorCode.NOT_FOUND
    assert raised.value.message == "run not found"
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["sender", "recipient"])
async def test_terminal_run_cannot_send_or_receive(terminal: str) -> None:
    store = InMemoryStore()
    _, parent_id, child_id = await _related_runs(store)
    run_id = parent_id if terminal == "sender" else child_id
    current = RunSnapshot.model_validate(await store.get_snapshot("run", run_id))
    completed = current.model_copy(
        update={
            "status": RunStatus.COMPLETED,
            "version": 3,
            "output_text": "done",
            "usage": TokenUsage(),
        }
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    completed.run_id,
                    completed.session_id,
                    completed.version,
                    completed.model_dump(mode="json"),
                ),
            ),
            preconditions=(
                SnapshotPrecondition(
                    "run",
                    current.run_id,
                    current.version,
                    current.session_id,
                    current.model_dump(mode="json"),
                ),
            ),
        )
    )

    with pytest.raises(AgentSDKError) as raised:
        await MailboxService(store).send(parent_id, child_id, "too late")
    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "terminal run cannot send or receive messages"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [None, "", "x" * 32_769],
    ids=["none", "empty", "oversized"],
)
async def test_invalid_content_is_rejected_safely(content: str | None) -> None:
    store = InMemoryStore()
    _, parent_id, child_id = await _related_runs(store)

    with pytest.raises(AgentSDKError) as raised:
        await MailboxService(store).send(parent_id, child_id, content)  # type: ignore[arg-type]
    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "message content must contain 1..32768 characters"
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_idempotency_key_replays_the_original_message() -> None:
    store = InMemoryStore()
    _, parent_id, child_id = await _related_runs(store)
    mailbox = MailboxService(store)

    first = await mailbox.send(
        parent_id,
        child_id,
        "one",
        idempotency_key="send-1",
    )
    replay = await mailbox.send(
        parent_id,
        child_id,
        "one",
        idempotency_key="send-1",
    )

    assert replay == first
    assert await mailbox.unread(child_id) == (first,)
    events = await store.read_events(after_cursor=0)
    assert sum(item.event.type == "agent.message.sent" for item in events) == 1


@pytest.mark.asyncio
async def test_idempotency_key_conflict_is_not_retryable() -> None:
    store = InMemoryStore()
    _, parent_id, child_id = await _related_runs(store)
    mailbox = MailboxService(store)
    await mailbox.send(parent_id, child_id, "one", idempotency_key="send-1")

    with pytest.raises(AgentSDKError) as raised:
        await mailbox.send(parent_id, child_id, "two", idempotency_key="send-1")
    assert raised.value.code is ErrorCode.CONFLICT
    assert raised.value.retryable is False


class _ConcurrentSendStore:
    def __init__(self) -> None:
        self.delegate = InMemoryStore()
        self._waiting = 0
        self._release = asyncio.Event()

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(
            event.type == "agent.message.sent" and event.sequence == 2
            for event in batch.events
        ):
            self._waiting += 1
            if self._waiting == 2:
                self._release.set()
            await self._release.wait()
        return await self.delegate.commit(batch)

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, object] | None:
        return await self.delegate.get_snapshot(kind, entity_id)


@pytest.mark.asyncio
async def test_concurrent_sends_retry_precondition_conflicts_in_recipient_order() -> None:
    store = _ConcurrentSendStore()
    _, parent_id, child_id = await _related_runs(store.delegate)
    mailbox = MailboxService(store)  # type: ignore[arg-type]
    first = await mailbox.send(parent_id, child_id, "seed")

    sent = await asyncio.gather(
        mailbox.send(parent_id, child_id, "left"),
        mailbox.send(parent_id, child_id, "right"),
    )

    unread = await mailbox.unread(child_id)
    assert unread[0] == first
    assert {item.content for item in unread[1:]} == {"left", "right"}
    assert tuple(item.sequence for item in unread) == (1, 2, 3)
    assert {item.message_id for item in sent} == {
        unread[1].message_id,
        unread[2].message_id,
    }


class _FirstMailboxBootstrapRaceStore:
    def __init__(self, delegate: InMemoryStore) -> None:
        self.delegate = delegate
        self.bootstrap_calls = 0
        self.both_bootstraps = asyncio.Event()
        self.first_message_committed = asyncio.Event()

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        is_bootstrap = any(
            item.kind == "mailbox" and item.version == 1
            for item in batch.snapshots
        )
        if is_bootstrap:
            self.bootstrap_calls += 1
            call = self.bootstrap_calls
            if call == 2:
                self.both_bootstraps.set()
            await self.both_bootstraps.wait()
            if call == 2:
                await self.first_message_committed.wait()
            return await self.delegate.commit(batch)
        result = await self.delegate.commit(batch)
        if any(event.type == "agent.message.sent" for event in batch.events):
            self.first_message_committed.set()
        return result


@pytest.mark.asyncio
async def test_concurrent_first_sends_reload_after_bootstrap_replay_race() -> None:
    durable = InMemoryStore()
    _, parent_id, child_id = await _related_runs(durable)
    store = _FirstMailboxBootstrapRaceStore(durable)
    mailbox = MailboxService(store)  # type: ignore[arg-type]

    sent = await asyncio.gather(
        mailbox.send(parent_id, child_id, "left"),
        mailbox.send(parent_id, child_id, "right"),
    )

    assert store.bootstrap_calls == 2
    assert {item.content for item in sent} == {"left", "right"}
    assert tuple(item.sequence for item in await mailbox.unread(child_id)) == (1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "mailbox_recipient",
        "cursor_recipient",
        "message_recipient",
        "message_session",
        "message_sequence",
        "duplicate_message_id",
    ],
)
async def test_corrupt_memory_mailbox_fails_closed(case: str) -> None:
    store = InMemoryStore()
    session_id, parent_id, child_id = await _related_runs(store)
    mailbox = MailboxService(store)
    await mailbox.send(parent_id, child_id, "one")
    await mailbox.send(parent_id, child_id, "two")
    await mailbox.unread(child_id)

    target_kind = "mailbox_cursor" if case == "cursor_recipient" else "mailbox"
    target = store._snapshots[(target_kind, child_id)]
    data = target.data.copy()
    if case == "mailbox_recipient":
        data["recipient_run_id"] = parent_id
    elif case == "cursor_recipient":
        data["recipient_run_id"] = parent_id
    else:
        messages = [dict(item) for item in data["messages"]]
        if case == "message_recipient":
            messages[0]["recipient_run_id"] = parent_id
        elif case == "message_session":
            messages[0]["session_id"] = f"{session_id}_other"
        elif case == "message_sequence":
            messages[0]["sequence"] = 2
        else:
            messages[1]["message_id"] = messages[0]["message_id"]
        data["messages"] = messages
    store._snapshots[(target_kind, child_id)] = SnapshotWrite(
        target.kind,
        target.entity_id,
        target.session_id,
        target.version,
        data,
    )

    with pytest.raises(AgentSDKError) as raised:
        await mailbox.unread(child_id)
    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "stored mailbox is invalid"
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_sqlite_reopen_preserves_mailbox_and_idempotency(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mailbox.db"
    store = await SQLiteStore.open(database)
    try:
        commands = RuntimeCommands(store)
        session = await commands.create_session(workspaces=[])
        parent = await commands.start_run(
            session.session_id,
            agent_revision="parent:1",
            user_input="parent",
        )
        child = await commands.start_run(
            session.session_id,
            agent_revision="child:1",
            user_input="child",
            parent_run_id=parent.run_id,
        )
        original = await MailboxService(store).send(
            parent.run_id,
            child.run_id,
            "durable",
            idempotency_key="durable-send",
        )
    finally:
        await store.close()

    reopened = await SQLiteStore.open(database)
    try:
        mailbox = MailboxService(reopened)
        assert await mailbox.unread(child.run_id) == (original,)
        assert (
            await mailbox.send(
                parent.run_id,
                child.run_id,
                "durable",
                idempotency_key="durable-send",
            )
            == original
        )
        assert await mailbox.unread(child.run_id) == (original,)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_sqlite_reopen_rejects_corrupt_mailbox_message_owner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corrupt-mailbox.db"
    store = await SQLiteStore.open(database)
    try:
        commands = RuntimeCommands(store)
        session = await commands.create_session(workspaces=[])
        parent = await commands.start_run(
            session.session_id,
            agent_revision="parent:1",
            user_input="parent",
        )
        child = await commands.start_run(
            session.session_id,
            agent_revision="child:1",
            user_input="child",
            parent_run_id=parent.run_id,
        )
        await MailboxService(store).send(parent.run_id, child.run_id, "durable")
    finally:
        await store.close()

    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT data_json FROM snapshots WHERE kind = 'mailbox' AND entity_id = ?",
            (child.run_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["messages"][0]["recipient_run_id"] = parent.run_id
        connection.execute(
            "UPDATE snapshots SET data_json = ? WHERE kind = 'mailbox' AND entity_id = ?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                child.run_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    reopened: SQLiteStore | None = None
    try:
        with pytest.raises(ValueError, match="incompatible current projections"):
            reopened = await SQLiteStore.open(database)
    finally:
        if reopened is not None:
            await reopened.close()
