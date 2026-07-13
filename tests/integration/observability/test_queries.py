from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_sdk import AgentSDKError, ErrorCode
from agent_sdk.observability import EventFilter, QueryService
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.models import RunSnapshot, RunStatus
from agent_sdk.storage.base import CommitBatch, SnapshotWrite, StateStore, StoredEvent
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


@pytest.fixture(params=("memory", "sqlite"))
async def store(request: pytest.FixtureRequest, tmp_path: Path):
    current: StateStore
    if request.param == "memory":
        current = InMemoryStore()
    else:
        current = await SQLiteStore.open(tmp_path / "observability.db")
    try:
        yield current
    finally:
        close = getattr(current, "close", None)
        if close is not None:
            await close()


@pytest.mark.asyncio
async def test_queries_are_cursor_qualified_and_high_water_survives_deletion(
    store: StateStore,
) -> None:
    commands = RuntimeCommands(store)
    first_session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        first_session.session_id,
        agent_revision="agent:1",
        user_input="observe me",
    )
    service = QueryService(store)

    observed = await service.get_run(run.run_id)
    queried = await service.query_events(
        EventFilter(session_id=first_session.session_id, run_id=run.run_id),
        after_cursor=0,
    )

    assert observed.snapshot == run
    assert observed.as_of_cursor >= 2
    assert tuple(item.event.type for item in queried.events) == ("run.created",)
    assert queried.next_cursor == queried.as_of_cursor

    high_water = await store.latest_cursor()
    await commands.delete_session(first_session.session_id)

    assert await store.latest_cursor() == high_water


@pytest.mark.asyncio
async def test_timeline_filters_exact_run_and_returns_immutable_detached_events(
    store: StateStore,
) -> None:
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    first = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="first",
    )
    await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="second",
    )
    service = QueryService(store)

    timeline = await service.timeline(first.run_id)
    filtered = await service.query_events(
        EventFilter(
            session_id=session.session_id,
            run_id=first.run_id,
            event_types=("run.created",),
        ),
        after_cursor=1,
    )

    assert [item.event.type for item in timeline.events] == ["run.created"]
    assert [item.cursor for item in timeline.events] == sorted(
        item.cursor for item in timeline.events
    )
    assert filtered.events == timeline.events
    assert filtered.next_cursor == filtered.as_of_cursor
    with pytest.raises(TypeError):
        timeline.events[0].event.payload["status"] = "tampered"
    persisted = await store.get_snapshot("run", first.run_id)
    assert persisted is not None
    assert persisted["status"] == RunStatus.CREATED.value


@pytest.mark.asyncio
async def test_timeline_rejects_same_run_event_from_another_session_without_leak() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="timeline",
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.progress",
                    session_id="ses_attacker",
                    run_id=run.run_id,
                    sequence=2,
                    payload={"secret": "must-not-leak-cross-session-timeline"},
                ),
            )
        )
    )

    with pytest.raises(AgentSDKError) as captured:
        await QueryService(store).timeline(run.run_id)

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak-cross-session-timeline" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )


class _OneEventQueryStore:
    def __init__(self, delegate: InMemoryStore) -> None:
        self.delegate = delegate

    async def commit(self, batch: CommitBatch):
        return await self.delegate.commit(batch)

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        return await self.delegate.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=1,
        )

    async def get_snapshot(self, kind: str, entity_id: str):
        return await self.delegate.get_snapshot(kind, entity_id)

    async def latest_cursor(self) -> int:
        return await self.delegate.latest_cursor()

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


@pytest.mark.asyncio
async def test_queries_continue_across_valid_short_store_pages() -> None:
    delegate = InMemoryStore()
    store = _OneEventQueryStore(delegate)
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="short pages",
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.progress",
                    session_id=session.session_id,
                    run_id=run.run_id,
                    sequence=2,
                    payload={"step": 1},
                ),
                EventEnvelope.new(
                    type="run.progress",
                    session_id=session.session_id,
                    run_id=run.run_id,
                    sequence=3,
                    payload={"step": 2},
                ),
            )
        )
    )
    service = QueryService(store)

    first = await service.query_events(after_cursor=0, limit=100)
    timeline = await service.timeline(run.run_id)

    assert first.next_cursor == first.events[-1].cursor == 1
    assert first.next_cursor < first.as_of_cursor
    assert [item.event.type for item in timeline.events] == [
        "run.created",
        "run.progress",
        "run.progress",
    ]


class _InvalidQueryStore:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        event = EventEnvelope.new(
            type="noise",
            session_id="ses_bad_query",
            run_id=None,
            sequence=1,
            payload={"secret": "must-not-leak-invalid-query-store"},
        )
        cursor: object = "1" if mode == "string-page-cursor" else -1
        stored_event: object = event
        if mode == "event-object":
            cursor = 1
            stored_event = object()
        self._page = [StoredEvent(cursor=cursor, event=stored_event)]

    async def latest_cursor(self):
        if self.mode == "negative-high-water":
            return -1
        if self.mode == "string-high-water":
            return "1"
        return 1

    async def read_events(self, **_: object):
        return self._page


@pytest.mark.parametrize(
    "mode",
    (
        "negative-high-water",
        "string-high-water",
        "negative-page-cursor",
        "string-page-cursor",
        "event-object",
    ),
)
@pytest.mark.asyncio
async def test_query_rejects_invalid_store_values_without_leak(mode: str) -> None:
    with pytest.raises(AgentSDKError) as captured:
        await QueryService(_InvalidQueryStore(mode)).query_events()

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak-invalid-query-store" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )


@pytest.mark.asyncio
async def test_execution_tree_is_transitive_and_creation_ordered(store: StateStore) -> None:
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        session.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    child = await commands.start_run(
        session.session_id,
        agent_revision="child:1",
        user_input="child",
        parent_run_id=root.run_id,
    )
    grandchild = await commands.start_run(
        session.session_id,
        agent_revision="child:1",
        user_input="grandchild",
        parent_run_id=child.run_id,
    )
    await commands.start_run(
        session.session_id,
        agent_revision="other:1",
        user_input="unrelated",
    )

    tree = await QueryService(store).execution_tree(root.run_id)

    assert [node.snapshot.run_id for node in tree.nodes] == [
        root.run_id,
        child.run_id,
        grandchild.run_id,
    ]
    assert [node.parent_run_id for node in tree.nodes] == [
        None,
        root.run_id,
        child.run_id,
    ]
    assert [node.created_cursor for node in tree.nodes] == sorted(
        node.created_cursor for node in tree.nodes
    )


@pytest.mark.asyncio
async def test_query_cursor_advances_over_unrelated_and_deleted_events(
    store: StateStore,
) -> None:
    commands = RuntimeCommands(store)
    removed = await commands.create_session(workspaces=[])
    hole_cursor = await store.latest_cursor()
    await commands.delete_session(removed.session_id)
    retained = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        retained.session_id,
        agent_revision="agent:1",
        user_input="retained",
    )
    service = QueryService(store)

    no_match = await service.query_events(
        EventFilter(run_id="run_missing"),
        after_cursor=hole_cursor,
    )
    match = await service.query_events(
        EventFilter(run_id=run.run_id),
        after_cursor=hole_cursor,
    )

    assert no_match.events == ()
    assert no_match.next_cursor == await store.latest_cursor()
    assert [event.event.type for event in match.events] == ["run.created"]


class _BusyUnrelatedCursorStore:
    def __init__(self, delegate: InMemoryStore, unrelated_session_id: str) -> None:
        self.delegate = delegate
        self.unrelated_session_id = unrelated_session_id
        self.sequence = 1

    async def commit(self, batch: CommitBatch):
        return await self.delegate.commit(batch)

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        return await self.delegate.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )

    async def get_snapshot(self, kind: str, entity_id: str):
        return await self.delegate.get_snapshot(kind, entity_id)

    async def latest_cursor(self) -> int:
        self.sequence += 1
        await self.delegate.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="unrelated.progress",
                        session_id=self.unrelated_session_id,
                        run_id=None,
                        sequence=self.sequence,
                        payload={},
                    ),
                )
            )
        )
        return await self.delegate.latest_cursor()

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


@pytest.mark.asyncio
async def test_execution_tree_ignores_continuous_unrelated_global_events() -> None:
    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    root_session = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        root_session.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    unrelated = await commands.create_session(workspaces=[])
    busy = _BusyUnrelatedCursorStore(delegate, unrelated.session_id)

    tree = await QueryService(busy).execution_tree(root.run_id)

    assert [node.snapshot.run_id for node in tree.nodes] == [root.run_id]


class _InjectTailCreatedStore:
    def __init__(self, delegate: InMemoryStore, event: EventEnvelope) -> None:
        self.delegate = delegate
        self.event = event
        self.latest_calls = 0

    async def commit(self, batch: CommitBatch):
        return await self.delegate.commit(batch)

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        return await self.delegate.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )

    async def get_snapshot(self, kind: str, entity_id: str):
        return await self.delegate.get_snapshot(kind, entity_id)

    async def latest_cursor(self) -> int:
        self.latest_calls += 1
        if self.latest_calls == 2:
            await self.delegate.commit(CommitBatch(events=(self.event,)))
        return await self.delegate.latest_cursor()

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


@pytest.mark.parametrize("session", ("same", "cross"))
@pytest.mark.asyncio
async def test_execution_tree_rejects_selected_run_duplicate_in_tail_window(
    session: str,
) -> None:
    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    owner = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        owner.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    duplicate = EventEnvelope.new(
        type="run.created",
        session_id=(owner.session_id if session == "same" else "ses_foreign_tail"),
        run_id=root.run_id,
        sequence=2,
        payload=root.model_dump(mode="json"),
    )

    with pytest.raises(AgentSDKError) as captured:
        await QueryService(_InjectTailCreatedStore(delegate, duplicate)).execution_tree(
            root.run_id
        )

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_execution_tree_ignores_unrelated_invalid_parent_in_tail_window() -> None:
    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    owner = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        owner.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    unrelated = EventEnvelope.new(
        schema_version=2,
        type="run.created",
        session_id="ses_foreign_tail",
        run_id="run_unrelated_tail",
        sequence=1,
        payload={"parent_run_id": []},
    )

    tree = await QueryService(
        _InjectTailCreatedStore(delegate, unrelated)
    ).execution_tree(root.run_id)

    assert [node.snapshot.run_id for node in tree.nodes] == [root.run_id]


@pytest.mark.parametrize("window", ("initial", "tail"))
@pytest.mark.asyncio
async def test_execution_tree_rejects_relevant_unknown_schema_created(
    window: str,
) -> None:
    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    owner = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        owner.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    child = RunSnapshot(
        run_id=f"run_schema_2_{window}",
        session_id=owner.session_id,
        agent_revision="child:1",
        status=RunStatus.CREATED,
        user_input="child",
        parent_run_id=root.run_id,
    )
    created = EventEnvelope.new(
        schema_version=2,
        type="run.created",
        session_id=owner.session_id,
        run_id=child.run_id,
        sequence=1,
        payload=child.model_dump(mode="json"),
    )
    if window == "initial":
        await delegate.commit(
            CommitBatch(
                events=(created,),
                snapshots=(
                    SnapshotWrite(
                        "run",
                        child.run_id,
                        owner.session_id,
                        1,
                        child.model_dump(mode="json"),
                    ),
                ),
            )
        )
        store: object = delegate
    else:
        store = _InjectTailCreatedStore(delegate, created)

    with pytest.raises(AgentSDKError) as captured:
        await QueryService(store).execution_tree(root.run_id)  # type: ignore[arg-type]

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


class _InjectSecretTailStore(_InjectTailCreatedStore):
    def __init__(self, delegate: InMemoryStore, root: RunSnapshot) -> None:
        super().__init__(
            delegate,
            EventEnvelope.new(
                schema_version=2,
                type="run.created",
                session_id=root.session_id,
                run_id="run_secret_tail",
                sequence=1,
                payload={
                    "parent_run_id": root.run_id,
                    "secret": "must-not-leak-tree-integrity",
                },
            ),
        )


async def _commit_secret_initial_created(
    store: InMemoryStore,
    root: RunSnapshot,
) -> None:
    child = RunSnapshot(
        run_id="run_secret_initial",
        session_id=root.session_id,
        agent_revision="child:1",
        status=RunStatus.CREATED,
        user_input="must-not-leak-tree-integrity",
        parent_run_id=root.run_id,
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    schema_version=2,
                    type="run.created",
                    session_id=root.session_id,
                    run_id=child.run_id,
                    sequence=1,
                    payload=child.model_dump(mode="json"),
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "run",
                    child.run_id,
                    root.session_id,
                    1,
                    child.model_dump(mode="json"),
                ),
            ),
        )
    )


@pytest.mark.parametrize("boundary", ("assemble", "tail"))
@pytest.mark.asyncio
async def test_execution_tree_integrity_error_does_not_leak_event_payload(
    boundary: str,
) -> None:
    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    owner = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        owner.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    if boundary == "assemble":
        await _commit_secret_initial_created(delegate, root)
        store: object = delegate
    else:
        store = _InjectSecretTailStore(delegate, root)

    with pytest.raises(AgentSDKError) as captured:
        await QueryService(store).execution_tree(root.run_id)  # type: ignore[arg-type]

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak-tree-integrity" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )


async def _commit_corrupt_run_snapshot(store: InMemoryStore) -> None:
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    "run_corrupt_snapshot",
                    "ses_corrupt_snapshot",
                    1,
                    {"secret": "must-not-leak-corrupt-run-snapshot"},
                ),
            ),
        )
    )


@pytest.mark.asyncio
async def test_query_corrupt_run_snapshot_is_context_free_without_local_leak() -> None:
    store = InMemoryStore()
    await _commit_corrupt_run_snapshot(store)

    with pytest.raises(AgentSDKError) as captured:
        await QueryService(store).get_run("run_corrupt_snapshot")

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak-corrupt-run-snapshot" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )


@pytest.mark.asyncio
async def test_execution_tree_missing_related_child_snapshot_is_internal() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    owner = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        owner.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    child = RunSnapshot(
        run_id="run_missing_child_snapshot",
        session_id=owner.session_id,
        agent_revision="child:1",
        status=RunStatus.CREATED,
        user_input="child",
        parent_run_id=root.run_id,
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.created",
                    session_id=owner.session_id,
                    run_id=child.run_id,
                    sequence=1,
                    payload=child.model_dump(mode="json"),
                ),
            )
        )
    )

    with pytest.raises(AgentSDKError) as captured:
        await QueryService(store).execution_tree(root.run_id)

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


class _TransitionChildAfterCapturedHStore(_OneEventQueryStore):
    def __init__(self, delegate: InMemoryStore, child: RunSnapshot) -> None:
        super().__init__(delegate)
        self.child = child
        self.latest_calls = 0
        self.transition_cursor = 0

    async def latest_cursor(self) -> int:
        self.latest_calls += 1
        if self.latest_calls == 1:
            captured = await self.delegate.latest_cursor()
            running = RunSnapshot.model_validate(
                {
                    **self.child.model_dump(mode="json"),
                    "status": "running",
                    "version": 2,
                }
            )
            committed = await self.delegate.commit(
                CommitBatch(
                    events=(
                        EventEnvelope.new(
                            type="run.started",
                            session_id=self.child.session_id,
                            run_id=self.child.run_id,
                            sequence=2,
                            payload={"status": "running"},
                        ),
                    ),
                    snapshots=(
                        SnapshotWrite(
                            "run",
                            self.child.run_id,
                            self.child.session_id,
                            2,
                            running.model_dump(mode="json"),
                        ),
                    ),
                )
            )
            self.transition_cursor = committed.last_cursor
            return captured
        return await self.delegate.latest_cursor()


@pytest.mark.asyncio
async def test_execution_tree_retries_snapshot_transition_after_captured_h() -> None:
    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    owner = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        owner.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    child = await commands.start_run(
        owner.session_id,
        agent_revision="child:1",
        user_input="child",
        parent_run_id=root.run_id,
    )
    store = _TransitionChildAfterCapturedHStore(delegate, child)

    tree = await QueryService(store).execution_tree(root.run_id)

    child_node = next(
        node for node in tree.nodes if node.snapshot.run_id == child.run_id
    )
    assert child_node.snapshot.status is RunStatus.RUNNING
    assert tree.as_of_cursor >= store.transition_cursor
    assert store.latest_calls >= 3


@pytest.mark.asyncio
async def test_execution_tree_ignores_malformed_unrelated_session_run_created() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    root_session = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        root_session.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    unrelated = await commands.create_session(workspaces=[])
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.created",
                    session_id=unrelated.session_id,
                    run_id="run_malformed",
                    sequence=1,
                    payload={"malformed": True},
                ),
            )
        )
    )

    tree = await QueryService(store).execution_tree(root.run_id)

    assert [node.snapshot.run_id for node in tree.nodes] == [root.run_id]


@pytest.mark.asyncio
async def test_event_query_rejects_cursor_ahead_of_durable_high_water() -> None:
    store = InMemoryStore()
    service = QueryService(store)

    with pytest.raises(Exception) as captured:
        await service.query_events(after_cursor=1)

    error = captured.value
    assert getattr(error, "code", None).value == "invalid_state"
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.asyncio
async def test_event_query_pages_raw_records_and_advances_empty_filtered_page() -> None:
    store = InMemoryStore()
    for sequence in range(1, 6):
        await store.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="match" if sequence == 5 else "skip",
                        session_id="ses_page",
                        run_id=None,
                        sequence=sequence,
                        payload={},
                    ),
                )
            )
        )
    service = QueryService(store)
    filters = EventFilter(event_types=("match",))

    first = await service.query_events(filters, after_cursor=0, limit=2)
    second = await service.query_events(
        filters,
        after_cursor=first.next_cursor,
        limit=2,
    )
    third = await service.query_events(
        filters,
        after_cursor=second.next_cursor,
        limit=2,
    )

    assert first.events == ()
    assert first.next_cursor == 2
    assert second.events == ()
    assert second.next_cursor == 4
    assert [item.event.type for item in third.events] == ["match"]
    assert third.next_cursor == third.as_of_cursor == 5


@pytest.mark.parametrize("limit", (0, -1, 1001))
@pytest.mark.asyncio
async def test_event_query_rejects_invalid_public_limit(limit: int) -> None:
    with pytest.raises(Exception) as captured:
        await QueryService(InMemoryStore()).query_events(limit=limit)

    assert getattr(captured.value, "code", None).value == "invalid_state"


class _DeleteAfterTreeTailStore:
    def __init__(self, delegate: InMemoryStore, session_id: str) -> None:
        self.delegate = delegate
        self.session_id = session_id
        self.deleted = False
        self.latest_calls = 0

    async def commit(self, batch: CommitBatch):
        return await self.delegate.commit(batch)

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        result = await self.delegate.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )
        if after_cursor > 0 and not self.deleted:
            self.deleted = True
            await self.delegate.delete_session(self.session_id)
        return result

    async def get_snapshot(self, kind: str, entity_id: str):
        return await self.delegate.get_snapshot(kind, entity_id)

    async def latest_cursor(self) -> int:
        self.latest_calls += 1
        if self.latest_calls == 2:
            await self.delegate.commit(
                CommitBatch(
                    events=(
                        EventEnvelope.new(
                            type="unrelated",
                            session_id="ses_unrelated_tail",
                            run_id=None,
                            sequence=1,
                            payload={},
                        ),
                    )
                )
            )
        return await self.delegate.latest_cursor()

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


@pytest.mark.asyncio
async def test_execution_tree_final_confirmation_detects_eventless_session_delete() -> None:
    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    session = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        session.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    store = _DeleteAfterTreeTailStore(delegate, session.session_id)

    with pytest.raises(Exception) as captured:
        await QueryService(store).execution_tree(root.run_id)

    assert getattr(captured.value, "code", None).value == "not_found"


@pytest.mark.asyncio
async def test_execution_tree_ignores_unhashable_unrelated_cross_session_parent() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        session.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.created",
                    session_id="ses_foreign",
                    run_id="run_foreign",
                    sequence=1,
                    payload={"parent_run_id": []},
                ),
            )
        )
    )

    tree = await QueryService(store).execution_tree(root.run_id)

    assert [node.snapshot.run_id for node in tree.nodes] == [root.run_id]


@pytest.mark.parametrize(
    "case",
    ("immutable-mismatch", "duplicate-created", "cross-session-duplicate"),
)
@pytest.mark.asyncio
async def test_execution_tree_rejects_inconsistent_run_creation(case: str) -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    root = await commands.start_run(
        session.session_id,
        agent_revision="root:1",
        user_input="root",
    )
    if case == "immutable-mismatch":
        await store.commit(
            CommitBatch(
                events=(),
                snapshots=(
                    SnapshotWrite(
                        "run",
                        root.run_id,
                        session.session_id,
                        2,
                        RunSnapshot.model_validate(
                            {
                                **root.model_dump(mode="json"),
                                "agent_revision": "tampered:2",
                                "status": "running",
                                "version": 2,
                            }
                        ).model_dump(mode="json"),
                    ),
                ),
            )
        )
    else:
        await store.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="run.created",
                        session_id=(
                            "ses_foreign_duplicate"
                            if case == "cross-session-duplicate"
                            else session.session_id
                        ),
                        run_id=root.run_id,
                        sequence=2,
                        payload=root.model_dump(mode="json"),
                    ),
                )
            )
        )

    with pytest.raises(AgentSDKError) as captured:
        await QueryService(store).execution_tree(root.run_id)

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


class _NonProgressingPageStore:
    def __init__(self, delegate: InMemoryStore) -> None:
        self.delegate = delegate

    async def get_snapshot(self, kind: str, entity_id: str):
        return await self.delegate.get_snapshot(kind, entity_id)

    async def latest_cursor(self) -> int:
        return await self.delegate.latest_cursor()

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        del after_cursor, session_id, up_to_cursor, limit
        event = (await self.delegate.read_events(after_cursor=0))[0]
        return [event] * 100

    async def commit(self, batch: CommitBatch):
        return await self.delegate.commit(batch)

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


@pytest.mark.asyncio
async def test_paginated_query_fails_closed_when_custom_store_cursor_does_not_advance() -> None:
    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="run",
    )

    with pytest.raises(Exception) as captured:
        await asyncio.wait_for(
            QueryService(_NonProgressingPageStore(delegate)).timeline(run.run_id),
            timeout=1,
        )

    assert getattr(captured.value, "code", None).value == "internal"


class _FaultQueryStore:
    def __init__(
        self,
        delegate: InMemoryStore,
        *,
        stage: str,
        error: BaseException,
    ) -> None:
        self.delegate = delegate
        self.stage = stage
        self.error = error

    async def get_snapshot(self, kind: str, entity_id: str):
        if self.stage == "snapshot":
            store_secret = "query-store-secret-must-not-leak"
            if store_secret:
                raise self.error
        return await self.delegate.get_snapshot(kind, entity_id)

    async def latest_cursor(self) -> int:
        if self.stage == "latest":
            store_secret = "query-store-secret-must-not-leak"
            if store_secret:
                raise self.error
        return await self.delegate.latest_cursor()

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        if self.stage == "read":
            store_secret = "query-store-secret-must-not-leak"
            if store_secret:
                raise self.error
        return await self.delegate.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )

    async def commit(self, batch: CommitBatch):
        return await self.delegate.commit(batch)

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


async def _faulting_query(stage: str, error: BaseException) -> None:
    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="run",
    )
    service = QueryService(_FaultQueryStore(delegate, stage=stage, error=error))
    if stage == "snapshot":
        await service.get_run(run.run_id)
    elif stage == "latest":
        await service.query_events()
    else:
        await service.timeline(run.run_id)


@pytest.mark.parametrize("stage", ("snapshot", "latest", "read"))
@pytest.mark.asyncio
async def test_query_store_errors_are_context_free(stage: str) -> None:
    with pytest.raises(AgentSDKError) as captured:
        await _faulting_query(stage, RuntimeError("private-query-store-error"))

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )


@pytest.mark.parametrize("stage", ("snapshot", "latest", "read"))
@pytest.mark.asyncio
async def test_query_store_cancellation_propagates_same_instance(stage: str) -> None:
    cancellation = asyncio.CancelledError(f"cancel-{stage}")

    with pytest.raises(asyncio.CancelledError) as captured:
        await _faulting_query(stage, cancellation)

    assert captured.value is cancellation
