import pytest

from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore


@pytest.mark.asyncio
async def test_commit_assigns_cursor_and_snapshot_atomically() -> None:
    store = InMemoryStore()
    event = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={},
    )
    result = await store.commit(
        CommitBatch(
            events=(event,),
            snapshots=(
                SnapshotWrite("run", "run_1", "ses_1", 1, {"status": "created"}),
            ),
        )
    )
    assert result.last_cursor == 1
    snapshot = await store.get_snapshot("run", "run_1")
    assert snapshot is not None
    assert snapshot["status"] == "created"
    assert [item.cursor for item in await store.read_events(after_cursor=0)] == [1]


@pytest.mark.asyncio
async def test_delete_session_removes_events_and_snapshots() -> None:
    store = InMemoryStore()
    event = EventEnvelope.new(
        type="session.created",
        session_id="ses_1",
        run_id=None,
        sequence=1,
        payload={},
    )
    await store.commit(
        CommitBatch(
            events=(event,),
            snapshots=(
                SnapshotWrite(
                    "session",
                    "ses_1",
                    "ses_1",
                    1,
                    {"session_id": "ses_1"},
                ),
            ),
        )
    )
    await store.delete_session("ses_1")
    assert await store.read_events(after_cursor=0) == []
    assert await store.get_snapshot("session", "ses_1") is None


@pytest.mark.asyncio
async def test_invalid_sequence_rolls_back_events_and_snapshots() -> None:
    store = InMemoryStore()
    duplicate_sequence = (
        EventEnvelope.new(
            type="run.created",
            session_id="ses_1",
            run_id="run_1",
            sequence=1,
            payload={},
        ),
        EventEnvelope.new(
            type="run.completed",
            session_id="ses_1",
            run_id="run_1",
            sequence=1,
            payload={},
        ),
    )
    with pytest.raises(ValueError, match="sequence"):
        await store.commit(
            CommitBatch(
                events=duplicate_sequence,
                snapshots=(
                    SnapshotWrite(
                        "run",
                        "run_1",
                        "ses_1",
                        1,
                        {"status": "completed"},
                    ),
                ),
            )
        )
    assert await store.read_events(after_cursor=0) == []
    assert await store.get_snapshot("run", "run_1") is None


@pytest.mark.asyncio
async def test_duplicate_event_id_rolls_back_entire_batch() -> None:
    store = InMemoryStore()
    first = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={},
    )
    duplicate = first.model_copy(update={"sequence": 2, "type": "run.completed"})

    with pytest.raises(ValueError, match="event id"):
        await store.commit(
            CommitBatch(
                events=(first, duplicate),
                snapshots=(
                    SnapshotWrite(
                        "run",
                        "run_1",
                        "ses_1",
                        2,
                        {"status": "completed"},
                    ),
                ),
            )
        )

    assert await store.read_events(after_cursor=0) == []
    assert await store.get_snapshot("run", "run_1") is None

    result = await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="session.created",
                    session_id="ses_2",
                    run_id=None,
                    sequence=1,
                    payload={},
                ),
            )
        )
    )
    assert result.last_cursor == 1


@pytest.mark.asyncio
async def test_duplicate_event_id_rejects_replayed_commit() -> None:
    store = InMemoryStore()
    event = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={},
    )
    await store.commit(CommitBatch(events=(event,)))

    with pytest.raises(ValueError, match="event id"):
        await store.commit(
            CommitBatch(
                events=(event,),
                snapshots=(
                    SnapshotWrite(
                        "run",
                        "run_1",
                        "ses_1",
                        2,
                        {"status": "replayed"},
                    ),
                ),
            )
        )

    assert [item.cursor for item in await store.read_events(after_cursor=0)] == [1]
    assert await store.get_snapshot("run", "run_1") is None


@pytest.mark.asyncio
async def test_session_events_use_session_sequence_aggregate() -> None:
    store = InMemoryStore()
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="session.created",
                    session_id="ses_1",
                    run_id=None,
                    sequence=1,
                    payload={},
                ),
            )
        )
    )

    with pytest.raises(ValueError, match="sequence"):
        await store.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="session.updated",
                        session_id="ses_1",
                        run_id=None,
                        sequence=1,
                        payload={},
                    ),
                ),
                snapshots=(
                    SnapshotWrite(
                        "session",
                        "ses_1",
                        "ses_1",
                        2,
                        {"status": "updated"},
                    ),
                ),
            )
        )

    assert [item.cursor for item in await store.read_events(after_cursor=0)] == [1]
    assert await store.get_snapshot("session", "ses_1") is None


@pytest.mark.asyncio
async def test_delete_session_preserves_global_cursor_hole() -> None:
    store = InMemoryStore()
    first = EventEnvelope.new(
        type="session.created",
        session_id="ses_1",
        run_id=None,
        sequence=1,
        payload={},
    )
    await store.commit(CommitBatch(events=(first,)))
    await store.delete_session("ses_1")

    second = EventEnvelope.new(
        type="session.created",
        session_id="ses_2",
        run_id=None,
        sequence=1,
        payload={},
    )
    result = await store.commit(CommitBatch(events=(second,)))

    assert result.last_cursor == 2
    assert [item.cursor for item in await store.read_events(after_cursor=0)] == [2]


@pytest.mark.asyncio
async def test_delete_session_uses_snapshot_ownership_field() -> None:
    store = InMemoryStore()
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    "run_1",
                    "ses_1",
                    1,
                    {"status": "created"},
                ),
            ),
        )
    )

    await store.delete_session("ses_1")

    assert await store.get_snapshot("run", "run_1") is None


@pytest.mark.asyncio
async def test_event_schema_version_and_delivery_are_stable() -> None:
    store = InMemoryStore()
    event = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={"status": "created"},
    )
    await store.commit(CommitBatch(events=(event,)))

    first_delivery = await store.read_events(after_cursor=0, session_id="ses_1")
    second_delivery = await store.read_events(after_cursor=0, session_id="ses_1")

    assert event.schema_version == 1
    assert "schema_version" in event.model_dump()
    assert first_delivery == second_delivery
    assert first_delivery[0].event.event_id == event.event_id
    assert await store.read_events(after_cursor=0, session_id="ses_2") == []
