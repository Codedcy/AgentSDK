from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_sdk.context.models import CompactionLevel, ContextRuntimeConfig
from agent_sdk.context.planner import ContextPlanner, PlannedContext
from agent_sdk.models.litellm_gateway import LiteLLMGateway
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.reconciliation import RunCheckpoint, RunCheckpointPhase
from agent_sdk.storage.base import CommitBatch, CommitResult
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.subagents.mailbox import MailboxService
from agent_sdk.subagents.models import MailboxCursorSnapshot


async def _unused_provider(**_: Any) -> object:
    raise AssertionError("deterministic context must not call the model")


async def _seed_mailbox(
    store: InMemoryStore,
) -> tuple[str, str, str, RunCheckpoint]:
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
    checkpoint = RunCheckpoint(
        run_id=child.run_id,
        session_id=session.session_id,
        checkpoint_version=1,
        turn=0,
        phase=RunCheckpointPhase.READY_FOR_MODEL,
        messages=({"role": "user", "content": "child"},),
    )
    return session.session_id, parent.run_id, child.run_id, checkpoint


def _planner(store: Any) -> ContextPlanner:
    return ContextPlanner(
        store,
        LiteLLMGateway._for_test(_unused_provider),
        model="test/model",
        model_window=16_000,
    )


async def _prepare(
    planner: ContextPlanner,
    *,
    session_id: str,
    run_id: str,
    checkpoint: RunCheckpoint,
    level: CompactionLevel = CompactionLevel.L0,
) -> PlannedContext:
    return await planner.prepare(
        session_id=session_id,
        run_id=run_id,
        checkpoint=checkpoint,
        config=ContextRuntimeConfig(force_level=level, recent_messages=2),
    )


def _capsule_response(*refs: str) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "parsed": {
                        "objective": "preserve mailbox evidence",
                        "constraints": [],
                        "decisions": [],
                        "facts": [],
                        "next_actions": [],
                        "artifact_refs": [],
                        "source_event_ids": list(refs),
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


async def _successful_compactor(**kwargs: Any) -> dict[str, object]:
    document = json.loads(kwargs["messages"][-1]["content"])
    refs = [item["event_id"] for item in document.get("sources", [])]
    refs.extend(document.get("capsule_ids", []))
    return _capsule_response(*refs)


class _RecordingStore:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.batches: list[CommitBatch] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        result = await self.delegate.commit(batch)
        self.batches.append(batch)
        return result


def _assert_atomic_consumption(
    planned: PlannedContext,
    *,
    message_id: str,
    message_sequence: int,
    batches: list[CommitBatch],
) -> None:
    assert planned.view.consumed_message_ids == (message_id,)
    batch = next(
        item
        for item in batches
        if any(
            event.type == "context.view.created"
            and event.payload["view_id"] == planned.view.view_id
            for event in item.events
        )
    )
    snapshots = {item.kind: item for item in batch.snapshots}
    assert {"context_view", "mailbox_cursor"} <= snapshots.keys()
    assert snapshots["mailbox_cursor"].data["last_consumed_sequence"] == (
        message_sequence
    )


@pytest.mark.asyncio
async def test_context_view_atomically_consumes_protected_mailbox_messages() -> None:
    store = InMemoryStore()
    session_id, parent_id, child_id, checkpoint = await _seed_mailbox(store)
    message = await MailboxService(store).send(
        parent_id,
        child_id,
        "Use source evt-2",
    )

    first = await _prepare(
        _planner(store),
        session_id=session_id,
        run_id=child_id,
        checkpoint=checkpoint,
    )

    assert first.view.consumed_message_ids == (message.message_id,)
    assert message.message_id in first.view.message_refs
    assert any(
        item.get("role") == "user"
        and item.get("content")
        == f"Agent message from {parent_id}:\n{message.content}"
        for item in first.messages
    )
    stored_view = await store.get_snapshot("context_view", first.view.view_id)
    assert stored_view == first.view.model_dump(mode="json")
    cursor = MailboxCursorSnapshot.model_validate(
        await store.get_snapshot("mailbox_cursor", child_id)
    )
    assert cursor.last_consumed_sequence == message.sequence

    second = await _prepare(
        _planner(store),
        session_id=session_id,
        run_id=child_id,
        checkpoint=checkpoint,
    )
    assert second.view.consumed_message_ids == ()
    assert message.message_id not in second.view.message_refs
    assert not any(message.content in str(item.get("content")) for item in second.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "level",
    [CompactionLevel.L0, CompactionLevel.L1, CompactionLevel.L2],
)
async def test_deterministic_levels_consume_mailbox_in_view_batch(
    level: CompactionLevel,
) -> None:
    durable = InMemoryStore()
    session_id, parent_id, child_id, checkpoint = await _seed_mailbox(durable)
    message = await MailboxService(durable).send(parent_id, child_id, level.value)
    store = _RecordingStore(durable)

    planned = await _prepare(
        ContextPlanner(
            store,
            LiteLLMGateway._for_test(_unused_provider),
            model="test/model",
            model_window=16_000,
        ),
        session_id=session_id,
        run_id=child_id,
        checkpoint=checkpoint,
        level=level,
    )

    assert planned.view.applied_level is level
    _assert_atomic_consumption(
        planned,
        message_id=message.message_id,
        message_sequence=message.sequence,
        batches=store.batches,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_l3_success_and_fallback_consume_mailbox_in_view_batch(
    fallback: bool,
) -> None:
    durable = InMemoryStore()
    session_id, parent_id, child_id, checkpoint = await _seed_mailbox(durable)
    message = await MailboxService(durable).send(parent_id, child_id, "L3")
    store = _RecordingStore(durable)

    async def provider(**kwargs: Any) -> dict[str, object]:
        if fallback:
            return {"choices": []}
        return await _successful_compactor(**kwargs)

    planned = await _prepare(
        ContextPlanner(
            store,
            LiteLLMGateway._for_test(provider),
            model="test/model",
            model_window=16_000,
        ),
        session_id=session_id,
        run_id=child_id,
        checkpoint=checkpoint,
        level=CompactionLevel.L3,
    )

    assert planned.view.applied_level is (
        CompactionLevel.L2 if fallback else CompactionLevel.L3
    )
    assert planned.view.fallback_from is (
        CompactionLevel.L3 if fallback else None
    )
    _assert_atomic_consumption(
        planned,
        message_id=message.message_id,
        message_sequence=message.sequence,
        batches=store.batches,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_l4_success_and_fallback_consume_mailbox_in_view_batch(
    fallback: bool,
) -> None:
    durable = InMemoryStore()
    session_id, parent_id, child_id, checkpoint = await _seed_mailbox(durable)
    if not fallback:
        await RuntimeCommands(durable).start_run(
            session_id,
            agent_revision="historical:1",
            user_input="older context eligible for compaction",
        )
        prior = await _prepare(
            ContextPlanner(
                durable,
                LiteLLMGateway._for_test(_successful_compactor),
                model="test/model",
                model_window=16_000,
            ),
            session_id=session_id,
            run_id=child_id,
            checkpoint=checkpoint,
            level=CompactionLevel.L3,
        )
        assert prior.view.capsule_id is not None
    message = await MailboxService(durable).send(parent_id, child_id, "L4")
    store = _RecordingStore(durable)

    planned = await _prepare(
        ContextPlanner(
            store,
            LiteLLMGateway._for_test(_successful_compactor),
            model="test/model",
            model_window=16_000,
        ),
        session_id=session_id,
        run_id=child_id,
        checkpoint=checkpoint,
        level=CompactionLevel.L4,
    )

    assert planned.view.applied_level is (
        CompactionLevel.L2 if fallback else CompactionLevel.L4
    )
    assert planned.view.fallback_from is (
        CompactionLevel.L4 if fallback else None
    )
    _assert_atomic_consumption(
        planned,
        message_id=message.message_id,
        message_sequence=message.sequence,
        batches=store.batches,
    )


class _BarrierViewStore:
    def __init__(self, delegate: InMemoryStore) -> None:
        self.delegate = delegate
        self.waiting = 0
        self.release = asyncio.Event()
        self.view_batches: list[CommitBatch] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(event.type == "context.view.created" for event in batch.events):
            self.view_batches.append(batch)
            if any(item.kind == "mailbox_cursor" for item in batch.snapshots):
                self.waiting += 1
                if self.waiting == 2:
                    self.release.set()
                await self.release.wait()
        return await self.delegate.commit(batch)


@pytest.mark.asyncio
async def test_cursor_conflict_reloads_and_rebuilds_context() -> None:
    durable = InMemoryStore()
    session_id, parent_id, child_id, checkpoint = await _seed_mailbox(durable)
    message = await MailboxService(durable).send(parent_id, child_id, "race")
    store = _BarrierViewStore(durable)

    views = await asyncio.gather(
        _prepare(
            _planner(store),
            session_id=session_id,
            run_id=child_id,
            checkpoint=checkpoint,
        ),
        _prepare(
            _planner(store),
            session_id=session_id,
            run_id=child_id,
            checkpoint=checkpoint,
        ),
    )

    assert sorted(len(item.view.consumed_message_ids) for item in views) == [0, 1]
    consumed = next(item for item in views if item.view.consumed_message_ids)
    assert consumed.view.consumed_message_ids == (message.message_id,)
    cursor = MailboxCursorSnapshot.model_validate(
        await durable.get_snapshot("mailbox_cursor", child_id)
    )
    assert cursor.last_consumed_sequence == message.sequence
    atomic = [
        batch
        for batch in store.view_batches
        if {item.kind for item in batch.snapshots}
        >= {"context_view", "mailbox_cursor"}
    ]
    assert len(atomic) == 2


class _FirstCursorBootstrapRaceStore:
    def __init__(self, delegate: InMemoryStore) -> None:
        self.delegate = delegate
        self.bootstrap_calls = 0
        self.both_bootstraps = asyncio.Event()
        self.first_view_committed = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        is_bootstrap = any(
            item.kind == "mailbox_cursor" and item.version == 1
            for item in batch.snapshots
        )
        if is_bootstrap:
            self.bootstrap_calls += 1
            call = self.bootstrap_calls
            if call == 2:
                self.both_bootstraps.set()
            await self.both_bootstraps.wait()
            if call == 2:
                await self.first_view_committed.wait()
            return await self.delegate.commit(batch)
        result = await self.delegate.commit(batch)
        if any(event.type == "context.view.created" for event in batch.events):
            self.first_view_committed.set()
        return result


@pytest.mark.asyncio
async def test_concurrent_first_prepare_reloads_after_cursor_bootstrap_race() -> None:
    durable = InMemoryStore()
    session_id, parent_id, child_id, checkpoint = await _seed_mailbox(durable)
    message = await MailboxService(durable).send(parent_id, child_id, "bootstrap race")
    store = _FirstCursorBootstrapRaceStore(durable)

    views = await asyncio.gather(
        _prepare(
            _planner(store),
            session_id=session_id,
            run_id=child_id,
            checkpoint=checkpoint,
        ),
        _prepare(
            _planner(store),
            session_id=session_id,
            run_id=child_id,
            checkpoint=checkpoint,
        ),
    )

    assert store.bootstrap_calls == 2
    assert sorted(len(item.view.consumed_message_ids) for item in views) == [0, 1]
    assert next(
        item.view.consumed_message_ids
        for item in views
        if item.view.consumed_message_ids
    ) == (message.message_id,)


@pytest.mark.asyncio
async def test_sqlite_reopen_preserves_advanced_consumption_cursor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mailbox-context.db"
    store = await SQLiteStore.open(database)
    try:
        session_id, parent_id, child_id, checkpoint = await _seed_mailbox(store)  # type: ignore[arg-type]
        message = await MailboxService(store).send(parent_id, child_id, "consume")
        planned = await _prepare(
            _planner(store),
            session_id=session_id,
            run_id=child_id,
            checkpoint=checkpoint,
        )
        assert planned.view.consumed_message_ids == (message.message_id,)
    finally:
        await store.close()

    reopened = await SQLiteStore.open(database)
    try:
        cursor = MailboxCursorSnapshot.model_validate(
            await reopened.get_snapshot("mailbox_cursor", child_id)
        )
        assert cursor.last_consumed_sequence == message.sequence
        assert await MailboxService(reopened).unread(child_id) == ()
    finally:
        await reopened.close()
