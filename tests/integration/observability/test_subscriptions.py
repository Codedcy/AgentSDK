from __future__ import annotations

import asyncio

import pytest

from agent_sdk import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.observability import EventFilter, SubscriptionService
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.storage.base import CommitBatch, StoredEvent
from agent_sdk.storage.memory import InMemoryStore


@pytest.mark.asyncio
async def test_subscription_filters_advances_and_resumes_from_acknowledged_cursor() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="stream",
    )
    service = SubscriptionService(store, poll_interval=0.001)
    stream = service.subscribe(
        filters=EventFilter(run_id=run.run_id, event_types=("run.progress",)),
        cursor=0,
    )

    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="unrelated",
                    session_id=session.session_id,
                    run_id=run.run_id,
                    sequence=2,
                    payload={},
                ),
                EventEnvelope.new(
                    type="run.progress",
                    session_id=session.session_id,
                    run_id=run.run_id,
                    sequence=3,
                    payload={"step": 1},
                ),
            )
        )
    )
    first = await asyncio.wait_for(anext(stream), timeout=1)
    await stream.aclose()

    assert first.event.type == "run.progress"
    assert first.event.payload["step"] == 1

    resumed = service.subscribe(
        filters=EventFilter(run_id=run.run_id, event_types=("run.progress",)),
        cursor=first.cursor,
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="run.progress",
                    session_id=session.session_id,
                    run_id=run.run_id,
                    sequence=4,
                    payload={"step": 2},
                ),
            )
        )
    )
    second = await asyncio.wait_for(anext(resumed), timeout=1)
    await resumed.aclose()

    assert second.cursor > first.cursor
    assert second.event.payload["step"] == 2


@pytest.mark.asyncio
async def test_subscription_waits_for_later_commit_and_crosses_deleted_cursor_hole() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    deleted = await commands.create_session(workspaces=[])
    deleted_cursor = await store.latest_cursor()
    await commands.delete_session(deleted.session_id)
    service = SubscriptionService(store, poll_interval=0.001)
    stream = service.subscribe(cursor=deleted_cursor)
    waiting = asyncio.create_task(anext(stream))

    retained = await commands.create_session(workspaces=[])
    observed = await asyncio.wait_for(waiting, timeout=1)
    await stream.aclose()

    assert observed.cursor > deleted_cursor
    assert observed.event.session_id == retained.session_id


@pytest.mark.asyncio
async def test_subscription_cancellation_propagates_without_background_task() -> None:
    store = InMemoryStore()
    stream = SubscriptionService(store, poll_interval=10).subscribe()
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    waiting.cancel("subscription-cancelled")
    with pytest.raises(asyncio.CancelledError) as captured:
        await waiting
    await stream.aclose()

    assert captured.value.args == ("subscription-cancelled",)


class _CountingStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        self.reads += 1
        return await super().read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )


@pytest.mark.asyncio
async def test_subscription_rejects_negative_cursor_before_store_read() -> None:
    store = _CountingStore()
    stream = SubscriptionService(store).subscribe(cursor=-1)

    with pytest.raises(AgentSDKError) as captured:
        await anext(stream)

    assert captured.value.code is ErrorCode.INVALID_STATE
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert store.reads == 0


@pytest.mark.asyncio
async def test_subscription_rejects_cursor_ahead_of_high_water_before_polling() -> None:
    store = _CountingStore()
    stream = SubscriptionService(store).subscribe(cursor=1)

    with pytest.raises(AgentSDKError) as captured:
        await anext(stream)

    assert captured.value.code is ErrorCode.INVALID_STATE
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert store.reads == 0


@pytest.mark.asyncio
async def test_idle_subscription_stops_on_close_without_later_store_read() -> None:
    store = _CountingStore()
    closing = asyncio.Event()
    service = SubscriptionService(
        store,
        poll_interval=10,
        close_signal=closing,
    )
    stream = service.subscribe()
    waiting = asyncio.create_task(anext(stream))

    async def wait_until_read() -> None:
        while store.reads == 0:
            if waiting.done():
                await waiting
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_read(), timeout=1)
    reads_before_close = store.reads

    closing.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, timeout=1)
    assert store.reads == reads_before_close

    rejected = service.subscribe()
    with pytest.raises(AgentSDKError) as captured:
        await anext(rejected)
    assert captured.value.code is ErrorCode.INVALID_STATE
    assert store.reads == reads_before_close


class _CloseAfterFirstPageStore(InMemoryStore):
    def __init__(self, closing: asyncio.Event) -> None:
        super().__init__()
        self.closing = closing
        self.limits: list[int | None] = []

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        self.limits.append(limit)
        result = await super().read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )
        if len(self.limits) == 1:
            self.closing.set()
        return result


@pytest.mark.asyncio
async def test_busy_nonmatching_backlog_is_bounded_and_checks_close_between_pages() -> None:
    closing = asyncio.Event()
    store = _CloseAfterFirstPageStore(closing)
    for sequence in range(1, 251):
        await store.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="skip",
                        session_id="ses_backlog",
                        run_id=None,
                        sequence=sequence,
                        payload={},
                    ),
                )
            )
        )
    stream = SubscriptionService(
        store,
        poll_interval=10,
        close_signal=closing,
    ).subscribe(filters=EventFilter(event_types=("match",)))

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=1)

    assert store.limits == [100]


class _MalformedPageStore(InMemoryStore):
    def __init__(self, cursors: list[int]) -> None:
        super().__init__()
        self.reads = 0
        self._page = [
            StoredEvent(
                cursor=cursor,
                event=EventEnvelope.new(
                    type="skip",
                    session_id="ses_malformed",
                    run_id=None,
                    sequence=index,
                    payload={"secret": "must-not-leak-invalid-store-page"},
                ),
            )
            for index, cursor in enumerate(cursors, start=1)
        ]

    async def latest_cursor(self) -> int:
        return 10_000

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        self.reads += 1
        await asyncio.sleep(0)
        return self._page


@pytest.mark.parametrize(
    "cursors",
    (
        list(range(1, 102)),
        [0, 1],
        [1] * 100,
        [2, 1],
    ),
    ids=(
        "oversized",
        "first-cursor-not-after-current",
        "duplicate-full-page",
        "descending",
    ),
)
@pytest.mark.asyncio
async def test_subscription_rejects_malformed_store_page_without_spinning(
    cursors: list[int],
) -> None:
    store = _MalformedPageStore(cursors)
    stream = SubscriptionService(store, poll_interval=0.001).subscribe(
        filters=EventFilter(event_types=("match",)),
    )

    with pytest.raises(AgentSDKError) as captured:
        await asyncio.wait_for(anext(stream), timeout=1)

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert store.reads == 1
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak-invalid-store-page" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )


class _InvalidEventPageStore(InMemoryStore):
    def __init__(self, kind: str) -> None:
        super().__init__()
        valid = EventEnvelope.new(
            type="skip",
            session_id="ses_invalid_event",
            run_id=None,
            sequence=1,
            payload={"secret": "must-not-leak-invalid-event-page"},
        )
        event: object
        if kind == "object":
            event = object()
        elif kind == "model-construct":
            event = EventEnvelope.model_construct(
                **valid.model_dump(exclude={"type"})
            )
        else:
            values = valid.model_dump()
            values["payload"] = (
                {
                    "secret": "must-not-leak-invalid-event-page",
                    "bad": object(),
                }
                if kind == "object-payload"
                else {
                    "secret": "must-not-leak-invalid-event-page",
                    "bad": float("nan"),
                }
            )
            event = EventEnvelope.model_construct(**values)
        self._page = [StoredEvent(cursor=1, event=event)]
        self.reads = 0

    async def latest_cursor(self) -> int:
        return 1

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        self.reads += 1
        return self._page


@pytest.mark.parametrize(
    "kind",
    ("object", "model-construct", "object-payload", "nan-payload"),
)
@pytest.mark.asyncio
async def test_subscription_rejects_invalid_event_without_leak_or_reread(
    kind: str,
) -> None:
    store = _InvalidEventPageStore(kind)
    stream = SubscriptionService(store).subscribe(
        filters=EventFilter(event_types=("match",)),
    )

    with pytest.raises(AgentSDKError) as captured:
        await asyncio.wait_for(anext(stream), timeout=1)

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert store.reads == 1
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak-invalid-event-page" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )


class _OneEventSubscriptionStore(InMemoryStore):
    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        return await super().read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=1,
        )


@pytest.mark.asyncio
async def test_subscription_immediately_continues_across_short_nonempty_page() -> None:
    store = _OneEventSubscriptionStore()
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="skip",
                    session_id="ses_short_subscription",
                    run_id=None,
                    sequence=1,
                    payload={},
                ),
                EventEnvelope.new(
                    type="match",
                    session_id="ses_short_subscription",
                    run_id=None,
                    sequence=2,
                    payload={},
                ),
            )
        )
    )
    stream = SubscriptionService(store, poll_interval=10).subscribe(
        filters=EventFilter(event_types=("match",)),
    )

    observed = await asyncio.wait_for(anext(stream), timeout=1)
    await stream.aclose()

    assert observed.event.type == "match"
