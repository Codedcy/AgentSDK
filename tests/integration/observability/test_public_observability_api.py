from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKConfig,
    AgentSDKError,
    AgentSpec,
    AnalyticsResult,
    ErrorCode,
    EvaluationDecision,
    EvaluationResult,
    EvaluationSubject,
    EvaluationVerdict,
    EventFilter,
    EventQueryResult,
    ExactOutputEvaluator,
    ObservedRun,
    RunTimeline,
)
from agent_sdk.storage.memory import InMemoryStore


def _response(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": text}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    return chunks()


async def _provider(**_: Any) -> AsyncIterator[dict[str, object]]:
    return _response("ok")


@pytest.mark.asyncio
async def test_public_sdk_queries_subscribes_evaluates_and_aggregates() -> None:
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=_provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="agent", model="fake/model"),
            "work",
        )
        await handle.result()

        observed = await sdk.queries.get_run(handle.run_id)
        timeline = await sdk.queries.timeline(handle.run_id)
        page = await sdk.queries.query_events(
            EventFilter(run_id=handle.run_id),
            after_cursor=0,
            limit=3,
        )
        tree = await sdk.queries.execution_tree(handle.run_id)
        stream = sdk.events.subscribe(
            filters=EventFilter(
                run_id=handle.run_id,
                event_types=("run.completed",),
            ),
            cursor=0,
        )
        terminal = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()
        serialized_terminal = terminal.model_dump(mode="json")
        assert json.loads(json.dumps(serialized_terminal))["event"]["type"] == (
            "run.completed"
        )
        evaluation = await sdk.evaluations.evaluate(
            handle.run_id,
            ExactOutputEvaluator(expected="ok"),
        )
        success = await sdk.analytics.success_rate(evaluator_id="exact_output")

        assert isinstance(observed, ObservedRun)
        assert isinstance(timeline, RunTimeline)
        assert isinstance(page, EventQueryResult)
        assert tree.nodes[0].snapshot.run_id == handle.run_id
        assert terminal.event.type == "run.completed"
        assert isinstance(evaluation, EvaluationResult)
        assert evaluation.verdict is EvaluationVerdict.PASS
        assert isinstance(success, AnalyticsResult)
        assert success.value == 1.0
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_default_sqlite_sdk_exposes_observability_without_eager_open(
    tmp_path: Path,
) -> None:
    database = tmp_path / "lazy.db"
    sdk = AgentSDK(AgentSDKConfig(database_path=database))

    assert sdk.queries is not None
    assert sdk.events is not None
    assert sdk.evaluations is not None
    assert sdk.analytics is not None
    assert not database.exists()
    await sdk.close()


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
        return EvaluationDecision(
            verdict=EvaluationVerdict.PASS,
            metrics={"accepted": 1.0},
            reason="accepted",
            confidence=1.0,
            evidence_event_ids=(subject.timeline.events[-1].event.event_id,),
        )


@pytest.mark.asyncio
async def test_close_waits_for_admitted_evaluation_and_stops_subscription() -> None:
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=_provider)
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(
        session.session_id,
        AgentSpec(name="agent", model="fake/model"),
        "work",
    )
    await handle.result()
    idle = sdk.events.subscribe(cursor=(await sdk.queries.timeline(handle.run_id)).as_of_cursor)
    waiting = asyncio.create_task(anext(idle))
    evaluator = _BlockingEvaluator()
    evaluating = asyncio.create_task(
        sdk.evaluations.evaluate(handle.run_id, evaluator)
    )
    await asyncio.wait_for(evaluator.started.wait(), timeout=1)

    closing = asyncio.create_task(sdk.close())
    await asyncio.sleep(0)
    assert not closing.done()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiting, timeout=1)

    rejected_evaluation = asyncio.create_task(
        sdk.evaluations.evaluate(handle.run_id, ExactOutputEvaluator(expected="ok"))
    )
    evaluator.release.set()
    completed = await evaluating
    await asyncio.wait_for(closing, timeout=1)
    assert completed.verdict is EvaluationVerdict.PASS
    with pytest.raises(AgentSDKError) as captured:
        await rejected_evaluation
    assert captured.value.code is ErrorCode.INVALID_STATE

    for operation in (
        sdk.queries.get_run(handle.run_id),
        sdk.analytics.success_rate(),
    ):
        with pytest.raises(AgentSDKError) as after_close:
            await operation
        assert after_close.value.code is ErrorCode.INVALID_STATE
    rejected_stream = sdk.events.subscribe(cursor=0)
    with pytest.raises(AgentSDKError) as after_close:
        await anext(rejected_stream)
    assert after_close.value.code is ErrorCode.INVALID_STATE


class _CloseProbeStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0
        self.closed = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


@pytest.mark.asyncio
async def test_cancelled_close_waiter_does_not_cancel_coordinator_waiting_on_admission() -> None:
    store = _CloseProbeStore()
    sdk = AgentSDK.for_test(store=store, acompletion=_provider)
    sdk._owned_close = store.close  # type: ignore[attr-defined]
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(
        session.session_id,
        AgentSpec(name="agent", model="fake/model"),
        "work",
    )
    await handle.result()
    evaluator = _BlockingEvaluator()
    evaluating = asyncio.create_task(
        sdk.evaluations.evaluate(handle.run_id, evaluator)
    )
    await asyncio.wait_for(evaluator.started.wait(), timeout=1)
    close_waiter = asyncio.create_task(sdk.close())
    await asyncio.sleep(0)
    close_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_waiter
    assert store.close_calls == 0

    evaluator.release.set()
    await evaluating
    await asyncio.wait_for(store.closed.wait(), timeout=1)
    assert store.close_calls == 1
    await asyncio.wait_for(sdk.close(), timeout=1)
    assert store.close_calls == 1
