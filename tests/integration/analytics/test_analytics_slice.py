from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_sdk import AgentSDKError, ErrorCode
from agent_sdk.analytics import AnalyticsQueries
from agent_sdk.evaluation import EvaluationResult, EvaluationVerdict
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.storage.base import CommitBatch, StateStore
from agent_sdk.storage.base import StoredEvent
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools import ToolResult, ToolResultStatus


@pytest.fixture(params=("memory", "sqlite"), ids=("memory", "sqlite"))
async def store(request: pytest.FixtureRequest, tmp_path: Path):
    current: StateStore
    if request.param == "memory":
        current = InMemoryStore()
    else:
        current = await SQLiteStore.open(tmp_path / "analytics.db")
    try:
        yield current
    finally:
        close = getattr(current, "close", None)
        if close is not None:
            await close()


def _evaluation(
    *,
    evaluation_id: str,
    session_id: str,
    evaluator_id: str,
    verdict: EvaluationVerdict,
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=evaluation_id,
        session_id=session_id,
        subject_run_id=f"run_{evaluation_id}",
        evaluator_id=evaluator_id,
        evaluator_version="1",
        method="deterministic_test",
        verdict=verdict,
        metrics={"score": 1.0},
        reason="fixture",
        confidence=1.0,
        evidence_event_ids=(f"evt_evidence_{evaluation_id}",),
        created_at=datetime.now(UTC),
        subject_cursor=1,
    )


def _evaluation_event(
    result: EvaluationResult,
    *,
    envelope_run_id: str | None = None,
    envelope_session_id: str | None = None,
) -> EventEnvelope:
    return EventEnvelope.new(
        type="evaluation.completed",
        session_id=envelope_session_id or result.session_id,
        run_id=envelope_run_id or result.evaluation_id,
        sequence=1,
        payload=result.model_dump(mode="json"),
    )


def _tool_event(
    *,
    session_id: str,
    call_id: str,
    tool_name: str,
    status: ToolResultStatus,
) -> EventEnvelope:
    result = (
        ToolResult.succeeded(call_id, tool_name, {"ok": True})
        if status is ToolResultStatus.SUCCEEDED
        else ToolResult.normalized_error(call_id, tool_name, status, "failed")
    )
    return EventEnvelope.new(
        type="tool.call.completed",
        session_id=session_id,
        run_id=f"run_{call_id}",
        sequence=1,
        payload=result.model_dump(mode="json"),
    )


@pytest.mark.asyncio
async def test_evaluation_sequence_and_schema_are_attributable_missing() -> None:
    store = InMemoryStore()
    session = await RuntimeCommands(store).create_session(workspaces=[])
    result = _evaluation(
        evaluation_id="evl_immutable",
        session_id=session.session_id,
        evaluator_id="eval-a",
        verdict=EvaluationVerdict.PASS,
    )
    invalid_schema = _evaluation(
        evaluation_id="evl_schema_2",
        session_id=session.session_id,
        evaluator_id="eval-a",
        verdict=EvaluationVerdict.PASS,
    )
    for event in (
        _evaluation_event(result),
        EventEnvelope.new(
            type="evaluation.completed",
            session_id=result.session_id,
            run_id=result.evaluation_id,
            sequence=2,
            payload=result.model_dump(mode="json"),
        ),
        EventEnvelope.new(
            schema_version=2,
            type="evaluation.completed",
            session_id=invalid_schema.session_id,
            run_id=invalid_schema.evaluation_id,
            sequence=1,
            payload=invalid_schema.model_dump(mode="json"),
        ),
    ):
        await store.commit(CommitBatch(events=(event,)))

    metric = await AnalyticsQueries(store).success_rate()

    assert metric.value == 1.0
    assert metric.sample_count == 1
    assert metric.missing_count == 2
    assert len(metric.evidence_event_ids) == 3


@pytest.mark.asyncio
async def test_unknown_tool_event_schema_is_attributable_missing() -> None:
    store = InMemoryStore()
    result = ToolResult.normalized_error(
        "call_future",
        "shell",
        ToolResultStatus.FAILED,
        "future schema",
    )
    event = EventEnvelope.new(
        schema_version=999,
        type="tool.call.completed",
        session_id="ses_future_tool",
        run_id="run_future_tool",
        sequence=7,
        payload=result.model_dump(mode="json"),
    )
    await store.commit(CommitBatch(events=(event,)))

    metric = await AnalyticsQueries(store).tool_failure_rate(tool_name="shell")

    assert metric.value is None
    assert metric.sample_count == 0
    assert metric.missing_count == 1
    assert metric.evidence_event_ids == (event.event_id,)


class _OneEventAnalyticsStore:
    def __init__(self, delegate: InMemoryStore) -> None:
        self.delegate = delegate

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

    async def latest_cursor(self) -> int:
        return await self.delegate.latest_cursor()


@pytest.mark.asyncio
async def test_analytics_reads_all_valid_short_store_pages() -> None:
    delegate = InMemoryStore()
    session = await RuntimeCommands(delegate).create_session(workspaces=[])
    for evaluation_id, verdict in (
        ("evl_short_pass", EvaluationVerdict.PASS),
        ("evl_short_fail", EvaluationVerdict.FAIL),
    ):
        await delegate.commit(
            CommitBatch(
                events=(
                    _evaluation_event(
                        _evaluation(
                            evaluation_id=evaluation_id,
                            session_id=session.session_id,
                            evaluator_id="eval-a",
                            verdict=verdict,
                        )
                    ),
                )
            )
        )

    metric = await AnalyticsQueries(_OneEventAnalyticsStore(delegate)).success_rate()

    assert metric.value == 0.5
    assert metric.sample_count == 2
    assert metric.missing_count == 0


class _InvalidAnalyticsStore:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        event = EventEnvelope.new(
            type="noise",
            session_id="ses_bad_analytics",
            run_id=None,
            sequence=1,
            payload={"secret": "must-not-leak-invalid-analytics-store"},
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
async def test_analytics_rejects_invalid_store_values_without_leak(mode: str) -> None:
    with pytest.raises(AgentSDKError) as captured:
        await AnalyticsQueries(_InvalidAnalyticsStore(mode)).success_rate()

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak-invalid-analytics-store" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )


@pytest.mark.asyncio
async def test_success_and_tool_metrics_use_explicit_known_denominators(
    store: StateStore,
) -> None:
    session = await RuntimeCommands(store).create_session(workspaces=[])
    evaluations = [
        _evaluation(
            evaluation_id="evl_pass_a",
            session_id=session.session_id,
            evaluator_id="eval-a",
            verdict=EvaluationVerdict.PASS,
        ),
        _evaluation(
            evaluation_id="evl_fail_a",
            session_id=session.session_id,
            evaluator_id="eval-a",
            verdict=EvaluationVerdict.FAIL,
        ),
        _evaluation(
            evaluation_id="evl_unknown_a",
            session_id=session.session_id,
            evaluator_id="eval-a",
            verdict=EvaluationVerdict.UNKNOWN,
        ),
        _evaluation(
            evaluation_id="evl_pass_b",
            session_id=session.session_id,
            evaluator_id="eval-b",
            verdict=EvaluationVerdict.PASS,
        ),
    ]
    events = [_evaluation_event(result) for result in evaluations]
    events.extend(
        (
            EventEnvelope.new(
                type="evaluation.completed",
                session_id=session.session_id,
                run_id="evl_bad_verdict",
                sequence=1,
                payload={
                    "evaluation_id": "evl_bad_verdict",
                    "session_id": session.session_id,
                    "evaluator_id": "eval-a",
                    "verdict": "bogus",
                },
            ),
            EventEnvelope.new(
                type="evaluation.completed",
                session_id=session.session_id,
                run_id="evl_missing_identity",
                sequence=1,
                payload={"verdict": "pass"},
            ),
            _evaluation_event(
                _evaluation(
                    evaluation_id="evl_identity_mismatch",
                    session_id=session.session_id,
                    evaluator_id="eval-a",
                    verdict=EvaluationVerdict.PASS,
                ),
                envelope_run_id="evl_wrong_envelope",
            ),
            EventEnvelope.new(
                type="evaluation.completed",
                session_id=session.session_id,
                run_id="evl_blank_method",
                sequence=1,
                payload={
                    **_evaluation(
                        evaluation_id="evl_blank_method",
                        session_id=session.session_id,
                        evaluator_id="eval-a",
                        verdict=EvaluationVerdict.PASS,
                    ).model_dump(mode="json"),
                    "method": "",
                },
            ),
        )
    )
    tool_statuses = (
        ToolResultStatus.SUCCEEDED,
        ToolResultStatus.FAILED,
        ToolResultStatus.TIMED_OUT,
        ToolResultStatus.DENIED,
    )
    events.extend(
        _tool_event(
            session_id=session.session_id,
            call_id=f"call_{status.value}",
            tool_name="shell",
            status=status,
        )
        for status in tool_statuses
    )
    events.extend(
        (
            EventEnvelope.new(
                type="tool.call.completed",
                session_id=session.session_id,
                run_id="run_unknown_tool_status",
                sequence=1,
                payload={
                    "call_id": "call_unknown",
                    "tool_name": "shell",
                    "status": "unknown",
                },
            ),
            EventEnvelope.new(
                type="tool.call.completed",
                session_id=session.session_id,
                run_id="run_missing_tool",
                sequence=1,
                payload={"status": "failed"},
            ),
            _tool_event(
                session_id=session.session_id,
                call_id="call_other",
                tool_name="read",
                status=ToolResultStatus.SUCCEEDED,
            ),
        )
    )
    for event in events:
        await store.commit(CommitBatch(events=(event,)))

    analytics = AnalyticsQueries(store)
    success = await analytics.success_rate()
    filtered_success = await analytics.success_rate(evaluator_id="eval-a")
    failures = await analytics.tool_failures(tool_name="shell")
    failure_rate = await analytics.tool_failure_rate(tool_name="shell")
    absent_count = await analytics.tool_failures(tool_name="missing")
    absent_rate = await analytics.tool_failure_rate(tool_name="missing")

    assert success.metric == "success_rate"
    assert success.value == pytest.approx(2 / 3)
    assert success.sample_count == 3
    assert success.missing_count == 5
    assert success.method == "explicit_evaluation_verdict"
    assert success.filters == {}
    assert len(success.evidence_event_ids) == 8
    assert success.as_of_cursor == await store.latest_cursor()

    assert filtered_success.value == 0.5
    assert filtered_success.sample_count == 2
    assert filtered_success.missing_count == 4
    assert filtered_success.filters == {"evaluator_id": "eval-a"}
    assert len(filtered_success.evidence_event_ids) == 6

    assert failures.metric == "tool_failures"
    assert failures.value == 3.0
    assert failures.sample_count == 4
    assert failures.missing_count == 1
    assert failures.method == "terminal_tool_status"
    assert failures.filters == {"tool_name": "shell"}
    assert len(failures.evidence_event_ids) == 5
    assert failure_rate.value == 0.75
    assert failure_rate.sample_count == 4
    assert failure_rate.missing_count == 1
    assert absent_count.value == 0.0
    assert absent_count.sample_count == 0
    assert absent_rate.value is None
    assert absent_rate.sample_count == 0


@pytest.mark.asyncio
async def test_restart_and_session_delete_recompute_without_removed_contributions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "delete-aware.db"
    store = await SQLiteStore.open(database)
    commands = RuntimeCommands(store)
    removed = await commands.create_session(workspaces=[])
    retained = await commands.create_session(workspaces=[])
    removed_pass = _evaluation(
        evaluation_id="evl_removed",
        session_id=removed.session_id,
        evaluator_id="eval-a",
        verdict=EvaluationVerdict.PASS,
    )
    retained_fail = _evaluation(
        evaluation_id="evl_retained",
        session_id=retained.session_id,
        evaluator_id="eval-a",
        verdict=EvaluationVerdict.FAIL,
    )
    borrowed = _evaluation(
        evaluation_id="evl_borrowed",
        session_id=removed.session_id,
        evaluator_id="eval-a",
        verdict=EvaluationVerdict.PASS,
    )
    for event in (
        _evaluation_event(removed_pass),
        _evaluation_event(retained_fail),
        _evaluation_event(borrowed, envelope_session_id=retained.session_id),
        _tool_event(
            session_id=removed.session_id,
            call_id="call_removed",
            tool_name="shell",
            status=ToolResultStatus.FAILED,
        ),
        _tool_event(
            session_id=retained.session_id,
            call_id="call_retained",
            tool_name="shell",
            status=ToolResultStatus.SUCCEEDED,
        ),
    ):
        await store.commit(CommitBatch(events=(event,)))
    before = await AnalyticsQueries(store).success_rate(evaluator_id="eval-a")
    await store.close()

    reopened = await SQLiteStore.open(database)
    try:
        restarted = await AnalyticsQueries(reopened).success_rate(evaluator_id="eval-a")
        assert restarted == before
        assert restarted.value == 0.5
        assert restarted.sample_count == 2
        assert restarted.missing_count == 1

        high_water = await reopened.latest_cursor()
        await reopened.delete_session(removed.session_id)
        success = await AnalyticsQueries(reopened).success_rate(evaluator_id="eval-a")
        failures = await AnalyticsQueries(reopened).tool_failure_rate(tool_name="shell")

        assert await reopened.latest_cursor() == high_water
        assert success.value == 0.0
        assert success.sample_count == 1
        assert success.missing_count == 1
        assert failures.value == 0.0
        assert failures.sample_count == 1
        assert failures.missing_count == 0
    finally:
        await reopened.close()


class _TrackingStore:
    def __init__(self, delegate: InMemoryStore) -> None:
        self.delegate = delegate
        self.reads: list[tuple[int, int | None, int | None]] = []

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
        self.reads.append((after_cursor, up_to_cursor, limit))
        return await self.delegate.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )

    async def get_snapshot(self, kind: str, entity_id: str):
        return await self.delegate.get_snapshot(kind, entity_id)

    async def commit(self, batch: CommitBatch):
        return await self.delegate.commit(batch)

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


@pytest.mark.asyncio
async def test_analytics_scans_more_than_two_pages_with_fixed_captured_bound() -> None:
    delegate = InMemoryStore()
    for sequence in range(1, 251):
        await delegate.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="noise",
                        session_id="ses_noise",
                        run_id=None,
                        sequence=sequence,
                        payload={},
                    ),
                )
            )
        )
    result = _evaluation(
        evaluation_id="evl_last",
        session_id="ses_noise",
        evaluator_id="eval-a",
        verdict=EvaluationVerdict.PASS,
    )
    await delegate.commit(CommitBatch(events=(_evaluation_event(result),)))
    high_water = await delegate.latest_cursor()
    store = _TrackingStore(delegate)

    metric = await AnalyticsQueries(store).success_rate(evaluator_id="eval-a")

    assert metric.value == 1.0
    assert metric.sample_count == 1
    assert [read[2] for read in store.reads] == [100] * 6
    assert all(read[1] == high_water for read in store.reads)
    assert [read[0] for read in store.reads] == [0, 100, 200] * 2


class _FaultStore(InMemoryStore):
    def __init__(
        self,
        *,
        stage: str,
        error: BaseException,
    ) -> None:
        super().__init__()
        self.stage = stage
        self.error = error

    async def latest_cursor(self) -> int:
        if self.stage == "latest":
            store_secret = "store-secret-must-not-leak"
            if store_secret:
                raise self.error
        return await super().latest_cursor()

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        if self.stage == "read":
            store_secret = "store-secret-must-not-leak"
            if store_secret:
                raise self.error
        return await super().read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )


@pytest.mark.parametrize("stage", ("latest", "read"))
@pytest.mark.asyncio
async def test_analytics_store_errors_are_context_free(stage: str) -> None:
    store = _FaultStore(stage=stage, error=RuntimeError("private-store-error"))
    if stage == "read":
        await store.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="noise",
                        session_id="ses_1",
                        run_id=None,
                        sequence=1,
                        payload={},
                    ),
                )
            )
        )

    with pytest.raises(AgentSDKError) as captured:
        await AnalyticsQueries(store).success_rate()

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


@pytest.mark.parametrize("stage", ("latest", "read"))
@pytest.mark.asyncio
async def test_analytics_store_cancellation_propagates_same_instance(stage: str) -> None:
    cancellation = asyncio.CancelledError(f"cancel-{stage}")
    store = _FaultStore(stage=stage, error=cancellation)
    if stage == "read":
        await store.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="noise",
                        session_id="ses_1",
                        run_id=None,
                        sequence=1,
                        payload={},
                    ),
                )
            )
        )

    with pytest.raises(asyncio.CancelledError) as captured:
        await AnalyticsQueries(store).success_rate()

    assert captured.value is cancellation


class _DeleteBetweenAnalyticsPagesStore(_TrackingStore):
    def __init__(self, delegate: InMemoryStore, deleted_session_id: str) -> None:
        super().__init__(delegate)
        self.deleted_session_id = deleted_session_id
        self.deleted = False

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        result = await super().read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )
        if not self.deleted:
            self.deleted = True
            await self.delegate.delete_session(self.deleted_session_id)
        return result


@pytest.mark.asyncio
async def test_analytics_retries_when_session_delete_occurs_between_pages() -> None:
    delegate = InMemoryStore()
    commands = RuntimeCommands(delegate)
    removed = await commands.create_session(workspaces=[])
    retained = await commands.create_session(workspaces=[])
    early = _evaluation(
        evaluation_id="evl_early_removed",
        session_id=removed.session_id,
        evaluator_id="eval-delete",
        verdict=EvaluationVerdict.PASS,
    )
    await delegate.commit(CommitBatch(events=(_evaluation_event(early),)))
    for sequence in range(2, 151):
        await delegate.commit(
            CommitBatch(
                events=(
                    EventEnvelope.new(
                        type="noise",
                        session_id=retained.session_id,
                        run_id=None,
                        sequence=sequence,
                        payload={},
                    ),
                )
            )
        )
    late = _evaluation(
        evaluation_id="evl_late_removed",
        session_id=removed.session_id,
        evaluator_id="eval-delete",
        verdict=EvaluationVerdict.PASS,
    )
    await delegate.commit(CommitBatch(events=(_evaluation_event(late),)))
    store = _DeleteBetweenAnalyticsPagesStore(delegate, removed.session_id)

    result = await AnalyticsQueries(store).success_rate(evaluator_id="eval-delete")

    assert result.value is None
    assert result.sample_count == 0
    assert result.missing_count == 0
    assert result.evidence_event_ids == ()
