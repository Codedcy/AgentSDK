from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import AgentSDKError, ErrorCode
from agent_sdk.evaluation import (
    EvaluationDecision,
    EvaluationEngine,
    EvaluationSubject,
    EvaluationResult,
    EvaluationVerdict,
    ExactOutputEvaluator,
)
from agent_sdk.events.models import EventEnvelope
from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.models import RunSnapshot
from agent_sdk.storage.base import CommitBatch, SnapshotWrite, StateStore, StoredEvent
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


def _response(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": text}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    return chunks()


async def _terminal_run(
    store: StateStore,
    *,
    output: str = "ok",
) -> tuple[str, RunSnapshot]:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _response(output)

    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    created = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="evaluate",
    )
    await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
        created.run_id,
        ModelRequest(model="fake/model", messages=({"role": "user", "content": "go"},)),
    )
    terminal = RunSnapshot.model_validate(await store.get_snapshot("run", created.run_id))
    return session.session_id, terminal


async def _failed_run(store: StateStore) -> RunSnapshot:
    async def provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        raise RuntimeError("provider failed")

    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    created = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="fail",
    )
    with pytest.raises(AgentSDKError):
        await RunEngine(store, LiteLLMGateway._for_test(provider)).execute(
            created.run_id,
            ModelRequest(
                model="fake/model",
                messages=({"role": "user", "content": "fail"},),
            ),
        )
    return RunSnapshot.model_validate(await store.get_snapshot("run", created.run_id))


@pytest.mark.asyncio
async def test_exact_output_evaluation_is_evidence_backed_immutable_and_append_only() -> None:
    store = InMemoryStore()
    session_id, terminal = await _terminal_run(store)
    before = await store.get_snapshot("run", terminal.run_id)

    result = await EvaluationEngine(store).evaluate(
        terminal.run_id,
        ExactOutputEvaluator(expected="ok"),
    )

    assert result.session_id == session_id
    assert result.subject_run_id == terminal.run_id
    assert result.subject_type == "run"
    assert result.verdict is EvaluationVerdict.PASS
    assert result.metrics == {"exact_match": 1.0}
    assert result.evaluator_id == "exact_output"
    assert result.evaluator_version == "1"
    assert result.method == "deterministic_exact_match"
    assert result.confidence == 1.0
    assert result.schema_version == 1
    assert result.record_version == 1
    assert result.subject_cursor > 0
    assert result.created_at is not None
    assert len(result.evidence_event_ids) == 1
    with pytest.raises(TypeError):
        result.metrics["exact_match"] = 0.0

    persisted = await store.get_snapshot("evaluation", result.evaluation_id)
    assert persisted == result.model_dump(mode="json")
    assert await store.get_snapshot("run", terminal.run_id) == before
    events = await store.read_events(after_cursor=0)
    evaluation_events = [
        stored for stored in events if stored.event.type == "evaluation.completed"
    ]
    assert len(evaluation_events) == 1
    assert evaluation_events[0].event.run_id == result.evaluation_id
    assert evaluation_events[0].event.sequence == 1
    assert evaluation_events[0].event.payload == result.model_dump(mode="json")
    terminal_event_ids = {
        stored.event.event_id
        for stored in events
        if stored.event.run_id == terminal.run_id
        and stored.event.type in {"run.completed", "run.failed"}
    }
    assert set(result.evidence_event_ids) == terminal_event_ids


@pytest.mark.parametrize(
    ("expected", "verdict", "metric"),
    (("ok", EvaluationVerdict.PASS, 1.0), ("different", EvaluationVerdict.FAIL, 0.0)),
)
@pytest.mark.asyncio
async def test_exact_output_is_deterministic(
    expected: str,
    verdict: EvaluationVerdict,
    metric: float,
) -> None:
    store = InMemoryStore()
    _, terminal = await _terminal_run(store)

    result = await EvaluationEngine(store).evaluate(
        terminal.run_id,
        ExactOutputEvaluator(expected=expected),
    )

    assert result.verdict is verdict
    assert result.metrics["exact_match"] == metric


@pytest.mark.asyncio
async def test_nonterminal_run_is_not_eligible_and_creates_no_evaluation() -> None:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    created = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="not terminal",
    )

    with pytest.raises(AgentSDKError) as captured:
        await EvaluationEngine(store).evaluate(
            created.run_id,
            ExactOutputEvaluator(expected="ok"),
        )

    assert captured.value.code is ErrorCode.INVALID_STATE
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await store.read_events(after_cursor=0)
    )


@pytest.mark.asyncio
async def test_failed_run_is_an_eligible_evaluation_subject() -> None:
    store = InMemoryStore()
    failed = await _failed_run(store)

    result = await EvaluationEngine(store).evaluate(
        failed.run_id,
        ExactOutputEvaluator(expected=""),
    )

    assert result.verdict is EvaluationVerdict.PASS
    events = await store.read_events(after_cursor=0)
    evidence = next(
        item.event
        for item in events
        if item.event.event_id == result.evidence_event_ids[0]
    )
    assert evidence.type == "run.failed"


class _DecisionEvaluator:
    id = "custom"
    version = "7"
    method = "deterministic_test"

    def __init__(self, decision: object) -> None:
        self.decision = decision

    async def evaluate(self, subject: EvaluationSubject) -> object:
        del subject
        return self.decision


class _ModelConstructEvaluator:
    id = "model-construct"
    version = "1"
    method = "adversarial"

    def __init__(self, kind: str) -> None:
        self.kind = kind

    async def evaluate(self, subject: EvaluationSubject) -> EvaluationDecision:
        terminal = subject.timeline.events[-1].event.event_id
        values: dict[str, object] = {
            "verdict": EvaluationVerdict.PASS,
            "metrics": {"score": 1.0},
            "reason": "accepted",
            "confidence": 1.0,
            "evidence_event_ids": (terminal,),
        }
        if self.kind == "nan-metric":
            values["metrics"] = {"must-not-leak": float("nan")}
        else:
            values["reason"] = object()
        return EvaluationDecision.model_construct(**values)


class _RaisingEvaluator:
    id = "raising"
    version = "1"
    method = "application"

    async def evaluate(self, subject: EvaluationSubject) -> EvaluationDecision:
        del subject
        extension_secret = "must-not-leak"
        raise RuntimeError(extension_secret)


class _CancellingEvaluator:
    id = "cancel"
    version = "1"
    method = "application"

    def __init__(self, error: asyncio.CancelledError) -> None:
        self.error = error

    async def evaluate(self, subject: EvaluationSubject) -> EvaluationDecision:
        del subject
        raise self.error


class _GetterRaisingEvaluator:
    def __init__(self, failing: str) -> None:
        self.failing = failing

    def _value(self, name: str, value: str) -> str:
        if self.failing == name:
            extension_getter_secret = "getter-secret-must-not-leak"
            raise RuntimeError(extension_getter_secret)
        return value

    @property
    def id(self) -> str:
        return self._value("id", "getter")

    @property
    def version(self) -> str:
        return self._value("version", "1")

    @property
    def method(self) -> str:
        return self._value("method", "application")

    async def evaluate(self, subject: EvaluationSubject) -> EvaluationDecision:
        raise AssertionError(f"must not invoke evaluator for {subject.snapshot.run_id}")


def _assert_no_extension_traceback(error: AgentSDKError) -> None:
    frames = []
    traceback = error.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(frame.f_code.co_name != "_invoke_evaluator" for frame in frames)
    assert all(
        "must-not-leak" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )


@pytest.mark.parametrize("case", ("exception", "invalid-return", "foreign-evidence"))
@pytest.mark.asyncio
async def test_evaluator_failures_are_sanitized_with_zero_writes(case: str) -> None:
    store = InMemoryStore()
    _, terminal = await _terminal_run(store)
    if case == "exception":
        evaluator: object = _RaisingEvaluator()
    elif case == "invalid-return":
        evaluator = _DecisionEvaluator(object())
    else:
        evaluator = _DecisionEvaluator(
            EvaluationDecision(
                verdict=EvaluationVerdict.PASS,
                metrics={"claim": 1.0},
                reason="forged",
                confidence=1.0,
                evidence_event_ids=("evt_foreign",),
            )
        )

    with pytest.raises(AgentSDKError) as captured:
        await EvaluationEngine(store).evaluate(terminal.run_id, evaluator)  # type: ignore[arg-type]

    assert captured.value.code in {ErrorCode.INVALID_STATE, ErrorCode.INTERNAL}
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "must-not-leak" not in str(captured.value)
    _assert_no_extension_traceback(captured.value)
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await store.read_events(after_cursor=0)
    )


@pytest.mark.parametrize("kind", ("nan-metric", "invalid-reason"))
@pytest.mark.asyncio
async def test_model_construct_decision_is_revalidated_without_write(kind: str) -> None:
    store = InMemoryStore()
    _, terminal = await _terminal_run(store)

    with pytest.raises(AgentSDKError) as captured:
        await EvaluationEngine(store).evaluate(
            terminal.run_id,
            _ModelConstructEvaluator(kind),
        )

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    _assert_no_extension_traceback(captured.value)
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await store.read_events(after_cursor=0)
    )


@pytest.mark.parametrize("metadata", ("id", "version", "method"))
@pytest.mark.asyncio
async def test_evaluator_metadata_getter_failures_are_context_free_and_zero_write(
    metadata: str,
) -> None:
    store = InMemoryStore()
    _, terminal = await _terminal_run(store)

    with pytest.raises(AgentSDKError) as captured:
        await EvaluationEngine(store).evaluate(
            terminal.run_id,
            _GetterRaisingEvaluator(metadata),
        )

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "getter-secret-must-not-leak" not in str(captured.value)
    _assert_no_extension_traceback(captured.value)
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await store.read_events(after_cursor=0)
    )


@pytest.mark.asyncio
async def test_evaluator_cancelled_error_propagates_same_instance_without_write() -> None:
    store = InMemoryStore()
    _, terminal = await _terminal_run(store)
    cancellation = asyncio.CancelledError("evaluation-cancelled")

    with pytest.raises(asyncio.CancelledError) as captured:
        await EvaluationEngine(store).evaluate(
            terminal.run_id,
            _CancellingEvaluator(cancellation),
        )

    assert captured.value is cancellation
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await store.read_events(after_cursor=0)
    )


class _BlockingEvaluator:
    id = "blocking"
    version = "1"
    method = "deterministic_test"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def evaluate(self, subject: EvaluationSubject) -> EvaluationDecision:
        self.started.set()
        await self.release.wait()
        terminal_event = subject.timeline.events[-1].event.event_id
        return EvaluationDecision(
            verdict=EvaluationVerdict.PASS,
            metrics={"accepted": 1.0},
            reason="accepted",
            confidence=1.0,
            evidence_event_ids=(terminal_event,),
        )


@pytest.mark.asyncio
async def test_delete_recreate_same_ids_and_versions_cannot_satisfy_evaluation_commit() -> None:
    store = InMemoryStore()
    session_id, terminal = await _terminal_run(store)
    evaluator = _BlockingEvaluator()
    task = asyncio.create_task(EvaluationEngine(store).evaluate(terminal.run_id, evaluator))
    await asyncio.wait_for(evaluator.started.wait(), timeout=1)

    await store.delete_session(session_id)
    replacement_session = {
        "session_id": session_id,
        "status": "active",
        "workspaces": ["replacement"],
        "version": 1,
    }
    replacement_run = terminal.model_copy(update={"user_input": "replacement"})
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite("session", session_id, session_id, 1, replacement_session),
                SnapshotWrite(
                    "run",
                    terminal.run_id,
                    session_id,
                    terminal.version,
                    replacement_run.model_dump(mode="json"),
                ),
            ),
        )
    )
    evaluator.release.set()

    with pytest.raises(AgentSDKError) as captured:
        await task

    assert captured.value.code is ErrorCode.NOT_FOUND
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await store.read_events(after_cursor=0)
    )


@pytest.mark.asyncio
async def test_identical_snapshot_recreation_cannot_outlive_evidence() -> None:
    store = InMemoryStore()
    session_id, terminal = await _terminal_run(store)
    session_data = await store.get_snapshot("session", session_id)
    run_data = await store.get_snapshot("run", terminal.run_id)
    assert session_data is not None
    assert run_data is not None
    evaluator = _BlockingEvaluator()
    task = asyncio.create_task(EvaluationEngine(store).evaluate(terminal.run_id, evaluator))
    await asyncio.wait_for(evaluator.started.wait(), timeout=1)

    await store.delete_session(session_id)
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite("session", session_id, session_id, 1, session_data),
                SnapshotWrite(
                    "run",
                    terminal.run_id,
                    session_id,
                    terminal.version,
                    run_data,
                ),
            ),
        )
    )
    evaluator.release.set()

    with pytest.raises(AgentSDKError) as captured:
        await task

    assert captured.value.code is ErrorCode.NOT_FOUND
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await store.read_events(after_cursor=0)
    )


class _OneEventPageStore:
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
async def test_evaluation_reads_complete_subject_from_short_store_pages() -> None:
    delegate = InMemoryStore()
    store = _OneEventPageStore(delegate)
    _, terminal = await _terminal_run(store)

    result = await EvaluationEngine(store).evaluate(
        terminal.run_id,
        ExactOutputEvaluator(expected="ok"),
    )

    assert result.verdict is EvaluationVerdict.PASS
    assert len(result.evidence_event_ids) == 1


class _InvalidEvaluationStore(_OneEventPageStore):
    def __init__(self, delegate: InMemoryStore, mode: str) -> None:
        super().__init__(delegate)
        self.mode = mode
        event = EventEnvelope.new(
            type="noise",
            session_id="ses_bad_evaluation",
            run_id=None,
            sequence=1,
            payload={"secret": "must-not-leak-invalid-evaluation-store"},
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
        return await self.delegate.latest_cursor()

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
async def test_evaluation_rejects_invalid_store_values_without_leak(mode: str) -> None:
    delegate = InMemoryStore()
    _, terminal = await _terminal_run(delegate)

    with pytest.raises(AgentSDKError) as captured:
        await EvaluationEngine(_InvalidEvaluationStore(delegate, mode)).evaluate(
            terminal.run_id,
            ExactOutputEvaluator(expected="ok"),
        )

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak-invalid-evaluation-store" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await delegate.read_events(after_cursor=0)
    )


class _SchemaTwoTerminalStore(_OneEventPageStore):
    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        page = await self.delegate.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )
        return [
            StoredEvent(
                cursor=stored.cursor,
                event=(
                    EventEnvelope.model_validate(
                        {
                            **stored.event.model_dump(mode="python"),
                            "schema_version": 2,
                        }
                    )
                    if stored.event.type in {"run.completed", "run.failed"}
                    else stored.event
                ),
            )
            for stored in page
        ]


@pytest.mark.asyncio
async def test_evaluation_rejects_unknown_schema_as_sole_terminal_event() -> None:
    delegate = InMemoryStore()
    _, terminal = await _terminal_run(delegate)

    with pytest.raises(AgentSDKError) as captured:
        await EvaluationEngine(_SchemaTwoTerminalStore(delegate)).evaluate(
            terminal.run_id,
            ExactOutputEvaluator(expected="ok"),
        )

    assert captured.value.code is ErrorCode.INTERNAL
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await delegate.read_events(after_cursor=0)
    )


async def _corrupt_evaluation_snapshot(
    store: InMemoryStore,
    *,
    session_id: str,
    terminal: RunSnapshot,
    kind: str,
) -> None:
    entity_id = session_id if kind == "session" else terminal.run_id
    data = await store.get_snapshot(kind, entity_id)
    assert data is not None
    version = int(data["version"]) + 1
    data = {
        **data,
        "version": version,
        "secret": "must-not-leak-corrupt-evaluation-snapshot",
    }
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    kind,
                    entity_id,
                    session_id,
                    version,
                    data,
                ),
            ),
        )
    )


@pytest.mark.parametrize("kind", ("run", "session"))
@pytest.mark.asyncio
async def test_evaluation_corrupt_snapshot_does_not_leak_raw_data(kind: str) -> None:
    store = InMemoryStore()
    session_id, terminal = await _terminal_run(store)
    await _corrupt_evaluation_snapshot(
        store,
        session_id=session_id,
        terminal=terminal,
        kind=kind,
    )

    with pytest.raises(AgentSDKError) as captured:
        await EvaluationEngine(store).evaluate(
            terminal.run_id,
            ExactOutputEvaluator(expected="ok"),
        )

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak-corrupt-evaluation-snapshot" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await store.read_events(after_cursor=0)
    )


class _SecretTerminalIntegrityStore(_OneEventPageStore):
    def __init__(self, delegate: InMemoryStore, mode: str) -> None:
        super().__init__(delegate)
        self.mode = mode

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ):
        page = await self.delegate.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )
        result = []
        for stored in page:
            event = stored.event
            if event.type in {"run.completed", "run.failed"}:
                data = event.model_dump(mode="python")
                data["payload"] = {
                    **event.payload,
                    "secret": "must-not-leak-evaluation-timeline",
                }
                if self.mode == "cross-session":
                    data["session_id"] = "ses_foreign_terminal"
                else:
                    data["schema_version"] = 2
                event = EventEnvelope.model_validate(data)
            result.append(StoredEvent(cursor=stored.cursor, event=event))
        return result


@pytest.mark.parametrize("mode", ("cross-session", "terminal-schema"))
@pytest.mark.asyncio
async def test_evaluation_timeline_integrity_error_does_not_leak_payload(
    mode: str,
) -> None:
    delegate = InMemoryStore()
    _, terminal = await _terminal_run(delegate)
    store = _SecretTerminalIntegrityStore(delegate, mode)

    with pytest.raises(AgentSDKError) as captured:
        await EvaluationEngine(store).evaluate(
            terminal.run_id,
            ExactOutputEvaluator(expected="ok"),
        )

    assert captured.value.code is ErrorCode.INTERNAL
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    frames = []
    traceback = captured.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert all(
        "must-not-leak-evaluation-timeline" not in repr(value)
        for frame in frames
        for value in frame.f_locals.values()
    )
    assert not any(
        item.event.type == "evaluation.completed"
        for item in await delegate.read_events(after_cursor=0)
    )


@pytest.mark.asyncio
async def test_evaluation_id_collision_is_atomic_retryable_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    _, terminal = await _terminal_run(store)
    monkeypatch.setattr("agent_sdk.evaluation.engine.new_id", lambda _: "evl_collision")
    engine = EvaluationEngine(store)
    first = await engine.evaluate(terminal.run_id, ExactOutputEvaluator(expected="ok"))

    with pytest.raises(AgentSDKError) as captured:
        await engine.evaluate(terminal.run_id, ExactOutputEvaluator(expected="ok"))

    assert captured.value.code is ErrorCode.CONFLICT
    assert captured.value.retryable is True
    assert await store.get_snapshot("evaluation", first.evaluation_id) == first.model_dump(
        mode="json"
    )
    assert sum(
        item.event.type == "evaluation.completed"
        for item in await store.read_events(after_cursor=0)
    ) == 1


@pytest.mark.asyncio
async def test_evaluation_reopens_from_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "evaluation.db"
    store = await SQLiteStore.open(database)
    _, terminal = await _terminal_run(store)
    result = await EvaluationEngine(store).evaluate(
        terminal.run_id,
        ExactOutputEvaluator(expected="ok"),
    )
    await store.close()

    reopened = await SQLiteStore.open(database)
    try:
        assert await reopened.get_snapshot(
            "evaluation", result.evaluation_id
        ) == result.model_dump(mode="json")
    finally:
        await reopened.close()


@pytest.mark.parametrize("field", ("evaluator_id", "evaluator_version", "method"))
def test_evaluation_result_rejects_invalid_persisted_metadata(field: str) -> None:
    data = {
        "evaluation_id": "evl_1",
        "session_id": "ses_1",
        "subject_run_id": "run_1",
        "evaluator_id": "evaluator",
        "evaluator_version": "1",
        "method": "deterministic",
        "verdict": "pass",
        "metrics": {"score": 1.0},
        "reason": "accepted",
        "confidence": 1.0,
        "evidence_event_ids": ["evt_1"],
        "created_at": "2026-07-14T00:00:00Z",
        "subject_cursor": 1,
    }
    data[field] = " "

    with pytest.raises(Exception):
        EvaluationResult.model_validate(data)
