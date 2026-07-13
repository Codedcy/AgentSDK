from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


@pytest.fixture(params=("memory", "sqlite"), ids=("memory", "sqlite"))
async def store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[StateStore]:
    if request.param == "memory":
        yield InMemoryStore()
        return

    sqlite_store = await SQLiteStore.open(tmp_path / "state.db")
    try:
        yield sqlite_store
    finally:
        await sqlite_store.close()


@pytest.mark.asyncio
async def test_commit_assigns_cursor_and_snapshot_atomically(store: StateStore) -> None:
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


@pytest.mark.parametrize(
    "precondition",
    [
        SnapshotPrecondition("session", "ses_missing"),
        SnapshotPrecondition("session", "ses_1", version=2),
    ],
    ids=("missing", "wrong-version"),
)
@pytest.mark.asyncio
async def test_snapshot_precondition_failure_rolls_back_entire_batch(
    store: StateStore,
    precondition: SnapshotPrecondition,
) -> None:
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "session",
                    "ses_1",
                    "ses_1",
                    1,
                    {"session_id": "ses_1", "version": 1},
                ),
            ),
        )
    )
    rejected = EventEnvelope.new(
        type="context.view.created",
        session_id="ses_1",
        run_id="view_rejected",
        sequence=1,
        payload={},
    )

    with pytest.raises(
        SnapshotPreconditionError,
        match="snapshot precondition failed",
    ):
        await store.commit(
            CommitBatch(
                events=(rejected,),
                snapshots=(
                    SnapshotWrite(
                        "context_view",
                        "view_rejected",
                        "ses_1",
                        1,
                        {"view_id": "view_rejected"},
                    ),
                ),
                preconditions=(precondition,),
            )
        )

    assert await store.read_events(after_cursor=0) == []
    assert await store.get_snapshot("context_view", "view_rejected") is None
    assert await store.get_snapshot("session", "ses_1") == {
        "session_id": "ses_1",
        "version": 1,
    }


@pytest.mark.asyncio
async def test_snapshot_precondition_accepts_existence_and_exact_version(
    store: StateStore,
) -> None:
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "session",
                    "ses_1",
                    "ses_1",
                    3,
                    {"session_id": "ses_1", "version": 3},
                ),
            ),
        )
    )
    event = EventEnvelope.new(
        type="context.view.created",
        session_id="ses_1",
        run_id="view_committed",
        sequence=1,
        payload={},
    )

    result = await store.commit(
        CommitBatch(
            events=(event,),
            preconditions=(
                SnapshotPrecondition("session", "ses_1"),
                SnapshotPrecondition("session", "ses_1", version=3),
            ),
        )
    )

    assert result.last_cursor == 1
    assert [item.event.event_id for item in await store.read_events(after_cursor=0)] == [
        event.event_id
    ]


@pytest.mark.asyncio
async def test_delete_session_removes_events_and_snapshots(store: StateStore) -> None:
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
async def test_invalid_sequence_rolls_back_events_and_snapshots(store: StateStore) -> None:
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
async def test_duplicate_event_id_rolls_back_entire_batch(store: StateStore) -> None:
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
async def test_duplicate_event_id_rejects_replayed_commit(store: StateStore) -> None:
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
async def test_session_events_use_session_sequence_aggregate(store: StateStore) -> None:
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
async def test_delete_session_preserves_global_cursor_hole(store: StateStore) -> None:
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
async def test_delete_session_uses_snapshot_ownership_field(store: StateStore) -> None:
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
async def test_event_schema_version_and_delivery_are_stable(store: StateStore) -> None:
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


@pytest.mark.asyncio
async def test_commit_deeply_isolates_event_and_snapshot_inputs(store: StateStore) -> None:
    event = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={"nested": {"status": "committed"}},
    )
    snapshot = SnapshotWrite(
        "run",
        "run_1",
        "ses_1",
        1,
        {"nested": {"status": "committed"}},
    )
    await store.commit(CommitBatch(events=(event,), snapshots=(snapshot,)))

    event.payload["nested"]["status"] = "mutated through input"
    snapshot.data["nested"]["status"] = "mutated through input"

    stored_event = (await store.read_events(after_cursor=0))[0]
    stored_snapshot = await store.get_snapshot("run", "run_1")
    assert stored_snapshot is not None
    assert stored_event.event.payload["nested"]["status"] == "committed"
    assert stored_snapshot["nested"]["status"] == "committed"


@pytest.mark.asyncio
async def test_reads_return_deeply_isolated_event_and_snapshot_data(store: StateStore) -> None:
    event = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={"nested": {"status": "committed"}},
    )
    snapshot = SnapshotWrite(
        "run",
        "run_1",
        "ses_1",
        1,
        {"nested": {"status": "committed"}},
    )
    await store.commit(CommitBatch(events=(event,), snapshots=(snapshot,)))

    first_event = (await store.read_events(after_cursor=0))[0]
    first_snapshot = await store.get_snapshot("run", "run_1")
    assert first_snapshot is not None
    first_event.event.payload["nested"]["status"] = "mutated through read"
    first_snapshot["nested"]["status"] = "mutated through read"

    stored_event = (await store.read_events(after_cursor=0))[0]
    stored_snapshot = await store.get_snapshot("run", "run_1")
    assert stored_snapshot is not None
    assert stored_event.event.payload["nested"]["status"] == "committed"
    assert stored_snapshot["nested"]["status"] == "committed"


@pytest.mark.parametrize("version", [1, 0])
@pytest.mark.asyncio
async def test_snapshot_version_must_increase_from_existing(
    version: int,
    store: StateStore,
) -> None:
    initial_event = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={},
    )
    await store.commit(
        CommitBatch(
            events=(initial_event,),
            snapshots=(
                SnapshotWrite("run", "run_1", "ses_1", 1, {"status": "created"}),
            ),
        )
    )
    rejected_event = EventEnvelope.new(
        type="run.completed",
        session_id="ses_1",
        run_id="run_1",
        sequence=2,
        payload={},
    )

    with pytest.raises(ValueError, match="snapshot version"):
        await store.commit(
            CommitBatch(
                events=(rejected_event,),
                snapshots=(
                    SnapshotWrite(
                        "run",
                        "run_1",
                        "ses_1",
                        version,
                        {"status": "rejected"},
                    ),
                ),
            )
        )

    assert [item.cursor for item in await store.read_events(after_cursor=0)] == [1]
    snapshot = await store.get_snapshot("run", "run_1")
    assert snapshot is not None
    assert snapshot["status"] == "created"
    result = await store.commit(CommitBatch(events=(rejected_event,)))
    assert result.last_cursor == 2


@pytest.mark.parametrize("second_version", [2, 1])
@pytest.mark.asyncio
async def test_snapshot_version_must_increase_within_batch(
    second_version: int,
    store: StateStore,
) -> None:
    event = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={},
    )

    with pytest.raises(ValueError, match="snapshot version"):
        await store.commit(
            CommitBatch(
                events=(event,),
                snapshots=(
                    SnapshotWrite("run", "run_1", "ses_1", 2, {"status": "created"}),
                    SnapshotWrite(
                        "run",
                        "run_1",
                        "ses_1",
                        second_version,
                        {"status": "rejected"},
                    ),
                ),
            )
        )

    assert await store.read_events(after_cursor=0) == []
    assert await store.get_snapshot("run", "run_1") is None
    result = await store.commit(CommitBatch(events=(event,)))
    assert result.last_cursor == 1
