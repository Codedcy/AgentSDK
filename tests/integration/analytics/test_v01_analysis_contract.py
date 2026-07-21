from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    EvaluationDecision,
    EvaluationSubject,
    EvaluationVerdict,
    EventFilter,
    ExactOutputEvaluator,
    ToolContext,
    ToolResult,
    ToolSpec,
    TraceStageKind,
    TraceStageStatus,
)
from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import CommitBatch, StateStore
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


@pytest.fixture(params=("memory", "sqlite"), ids=("memory", "sqlite"))
async def store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[StateStore]:
    current: StateStore
    if request.param == "memory":
        current = InMemoryStore()
    else:
        current = await SQLiteStore.open(tmp_path / "v01-analysis.db")
    try:
        yield current
    finally:
        close = getattr(current, "close", None)
        if close is not None:
            await close()


class _AlwaysPassEvaluator:
    id = "contract_always_pass"
    version = "1"
    method = "deterministic_contract_pass"

    async def evaluate(self, subject: EvaluationSubject) -> EvaluationDecision:
        terminal = next(
            observed.event.event_id
            for observed in reversed(subject.timeline.events)
            if observed.event.type in {"run.completed", "run.failed"}
        )
        return EvaluationDecision(
            verdict=EvaluationVerdict.PASS,
            metrics={"contract_pass": 1.0},
            reason="contract fixture",
            confidence=1.0,
            evidence_event_ids=(terminal,),
        )


def _tool_call(
    *,
    call_id: str,
    tool_name: str,
    arguments: str,
    index: int = 0,
) -> dict[str, object]:
    return {
        "index": index,
        "id": call_id,
        "function": {"name": tool_name, "arguments": arguments},
    }


async def _scripted_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
    messages = params["messages"]
    assert isinstance(messages, (list, tuple))
    user_index = max(
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "user"
    )
    user_input = messages[user_index]["content"]
    assert isinstance(user_input, str)

    async def chunks() -> AsyncIterator[dict[str, object]]:
        if user_input == "contract failed stage":
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                _tool_call(
                                    call_id="call_failed_stage_one",
                                    tool_name="lookup",
                                    arguments='{"mode":"ok"}',
                                ),
                                _tool_call(
                                    call_id="call_failed_stage_two",
                                    tool_name="lookup",
                                    arguments='{"mode":"ok"}',
                                    index=1,
                                ),
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
            return

        has_tool_result = any(
            isinstance(message, dict) and message.get("role") == "tool"
            for message in messages[user_index + 1 :]
        )
        if has_tool_result:
            output = "wrong" if "output-wrong" in user_input else "pass"
            yield {
                "choices": [
                    {"delta": {"content": output}, "finish_reason": "stop"}
                ]
            }
            return

        tool_name = "always_fail" if "always-fail" in user_input else "lookup"
        mode = "fail" if "lookup-fail" in user_input else "ok"
        call_id = "call_" + user_input.replace(" ", "_").replace("-", "_")
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            _tool_call(
                                call_id=call_id,
                                tool_name=tool_name,
                                arguments=f'{{"mode":"{mode}"}}',
                            )
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    return chunks()


async def _lookup(_context: ToolContext, *, mode: str) -> object:
    if mode == "fail":
        raise RuntimeError("normalized lookup failure")
    return {"found": True}


async def _always_fail(_context: ToolContext, *, mode: str) -> object:
    del mode
    raise RuntimeError("normalized all-failed sample")


def _register_tools(sdk: AgentSDK) -> None:
    schema = {
        "type": "object",
        "properties": {"mode": {"type": "string"}},
        "required": ["mode"],
        "additionalProperties": False,
    }
    sdk.tools.register(
        ToolSpec(name="lookup", description="lookup", input_schema=schema),
        _lookup,
    )
    sdk.tools.register(
        ToolSpec(
            name="always_fail",
            description="always fails",
            input_schema=schema,
        ),
        _always_fail,
    )


async def _completed_run(sdk: AgentSDK, session_id: str, prompt: str) -> str:
    handle = await sdk.runs.start(
        session_id,
        AgentSpec(name="analysis-contract", model="fake/model"),
        prompt,
    )
    await handle.result()
    return handle.run_id


def _assert_metric_evidence_types(
    evidence_ids: tuple[str, ...],
    event_types: dict[str, str],
    expected_type: str,
) -> None:
    assert evidence_ids
    assert {event_types[event_id] for event_id in evidence_ids} == {expected_type}


@pytest.mark.asyncio
async def test_v01_cross_run_analysis_and_deletion_contract(store: StateStore) -> None:
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_scripted_provider,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    _register_tools(sdk)
    try:
        removed = await sdk.sessions.create(workspaces=[])
        retained = await sdk.sessions.create(workspaces=[])

        removed_lookup = await _completed_run(
            sdk,
            removed.session_id,
            "removed lookup-ok output-pass",
        )
        removed_all_fail = await _completed_run(
            sdk,
            removed.session_id,
            "removed always-fail output-pass",
        )
        retained_lookup = await _completed_run(
            sdk,
            retained.session_id,
            "retained lookup-fail output-wrong",
        )
        retained_all_fail = await _completed_run(
            sdk,
            retained.session_id,
            "retained always-fail output-pass",
        )

        removed_lookup_evaluation = await sdk.evaluations.evaluate(
            removed_lookup,
            ExactOutputEvaluator(expected="pass"),
        )
        removed_alternate_evaluation = await sdk.evaluations.evaluate(
            removed_all_fail,
            _AlwaysPassEvaluator(),
        )
        retained_fail_evaluation = await sdk.evaluations.evaluate(
            retained_lookup,
            ExactOutputEvaluator(expected="pass"),
        )
        retained_pass_evaluation = await sdk.evaluations.evaluate(
            retained_all_fail,
            ExactOutputEvaluator(expected="pass"),
        )

        retained_timeline = await sdk.queries.timeline(retained_all_fail)
        unused_result = ToolResult.succeeded(
            "call_unused_contract",
            "unused_probe",
            {"unused": True},
        )
        unused_event = EventEnvelope.new(
            type="tool.call.completed",
            session_id=retained.session_id,
            run_id=retained_all_fail,
            sequence=max(item.event.sequence for item in retained_timeline.events) + 1,
            payload=unused_result.model_dump(mode="json"),
        )
        invalid_evaluation = EventEnvelope.new(
            type="evaluation.completed",
            session_id=retained.session_id,
            run_id="evl_contract_missing",
            sequence=1,
            payload={
                "evaluation_id": "evl_contract_missing",
                "evaluator_id": "exact_output",
                "verdict": "invalid",
            },
        )
        invalid_tool = EventEnvelope.new(
            type="tool.call.completed",
            session_id=retained.session_id,
            run_id="run_contract_missing_tool",
            sequence=1,
            payload={
                "call_id": "call_contract_missing",
                "tool_name": "lookup",
                "status": "invalid",
            },
        )
        await store.commit(
            CommitBatch(events=(unused_event, invalid_evaluation, invalid_tool))
        )

        failed_handle = await sdk.runs.start(
            retained.session_id,
            AgentSpec(name="analysis-contract", model="fake/model"),
            "contract failed stage",
        )
        with pytest.raises(AgentSDKError) as failed:
            await failed_handle.result()
        assert failed.value.code is ErrorCode.INVALID_STATE

        overall = await sdk.analytics.success_rate()
        exact = await sdk.analytics.success_rate(evaluator_id="exact_output")
        lookup_failures = await sdk.analytics.tool_failures(tool_name="lookup")
        lookup_rate = await sdk.analytics.tool_failure_rate(tool_name="lookup")
        all_failed_count = await sdk.analytics.tool_failures(tool_name="always_fail")
        all_failed_rate = await sdk.analytics.tool_failure_rate(
            tool_name="always_fail"
        )

        assert overall.value == 0.75
        assert overall.sample_count == 4
        assert overall.missing_count == 1
        assert overall.method == "explicit_evaluation_verdict"
        assert exact.value == pytest.approx(2 / 3)
        assert exact.sample_count == 3
        assert exact.missing_count == 1
        assert exact.filters == {"evaluator_id": "exact_output"}

        assert lookup_failures.value == 1.0
        assert lookup_failures.sample_count == 2
        assert lookup_failures.missing_count == 1
        assert lookup_failures.method == "terminal_tool_status"
        assert lookup_rate.value == 0.5
        assert lookup_rate.sample_count == 2
        assert lookup_rate.missing_count == 1
        assert all_failed_count.value == 2.0
        assert all_failed_count.sample_count == 2
        assert all_failed_count.missing_count == 0
        assert all_failed_rate.value == 1.0
        assert all_failed_rate.sample_count == 2

        observed = await sdk.queries.query_events(limit=100)
        event_types = {
            item.event.event_id: item.event.type for item in observed.events
        }
        _assert_metric_evidence_types(
            overall.evidence_event_ids,
            event_types,
            "evaluation.completed",
        )
        _assert_metric_evidence_types(
            exact.evidence_event_ids,
            event_types,
            "evaluation.completed",
        )
        _assert_metric_evidence_types(
            lookup_rate.evidence_event_ids,
            event_types,
            "tool.call.completed",
        )
        assert {
            removed_lookup_evaluation.evaluation_id,
            removed_alternate_evaluation.evaluation_id,
            retained_fail_evaluation.evaluation_id,
            retained_pass_evaluation.evaluation_id,
        } <= {
            item.event.payload.get("evaluation_id")
            for item in observed.events
            if item.event.event_id in overall.evidence_event_ids
        }

        attribution = await sdk.trace.attribution(retained_all_fail)
        unused = next(
            item
            for item in attribution.contributors
            if item.kind == "tool" and item.entity_id == "call_unused_contract"
        )
        assert unused.disposition == "unused"
        assert unused_event.event_id in unused.evidence_ids

        failed_attribution = await sdk.trace.attribution(failed_handle.run_id)
        failed_timeline = await sdk.trace.timeline(failed_handle.run_id)
        assert failed_attribution.failure is not None
        assert failed_attribution.failure.stage_kind is TraceStageKind.STEP
        assert any(
            stage.stage_id == failed_attribution.failure.stage_id
            and stage.status is TraceStageStatus.FAILED
            for stage in failed_timeline.stages
        )
        failed_run_timeline = await sdk.queries.timeline(failed_handle.run_id)
        failed_events_by_id = {
            item.event.event_id: item for item in failed_run_timeline.events
        }
        assert set(failed_attribution.failure.evidence_ids) <= set(
            failed_events_by_id
        )
        failure_evidence = [
            failed_events_by_id[event_id]
            for event_id in failed_attribution.failure.evidence_ids
        ]
        assert {item.event.type for item in failure_evidence} == {
            "step.started",
            "step.failed",
        }
        assert {item.event.run_id for item in failure_evidence} == {
            failed_handle.run_id
        }

        await sdk.sessions.close(removed.session_id)
        await sdk.sessions.delete(removed.session_id)

        deleted_events = await sdk.queries.query_events(
            EventFilter(session_id=removed.session_id),
            limit=100,
        )
        assert deleted_events.events == ()
        for load in (
            sdk.sessions.get(removed.session_id),
            sdk.runs.get(removed_lookup),
            sdk.runs.get(removed_all_fail),
        ):
            with pytest.raises(AgentSDKError) as missing:
                await load
            assert missing.value.code is ErrorCode.NOT_FOUND

        after_delete = await sdk.analytics.success_rate()
        exact_after_delete = await sdk.analytics.success_rate(
            evaluator_id="exact_output"
        )
        lookup_after_delete = await sdk.analytics.tool_failure_rate(
            tool_name="lookup"
        )
        all_failed_after_delete = await sdk.analytics.tool_failure_rate(
            tool_name="always_fail"
        )

        assert after_delete.value == 0.5
        assert after_delete.sample_count == 2
        assert after_delete.missing_count == 1
        assert exact_after_delete.value == 0.5
        assert exact_after_delete.sample_count == 2
        assert exact_after_delete.missing_count == 1
        assert lookup_after_delete.value == 1.0
        assert lookup_after_delete.sample_count == 1
        assert lookup_after_delete.missing_count == 1
        assert all_failed_after_delete.value == 1.0
        assert all_failed_after_delete.sample_count == 1
        assert all_failed_after_delete.missing_count == 0
        removed_evaluation_ids = {
            removed_lookup_evaluation.evaluation_id,
            removed_alternate_evaluation.evaluation_id,
        }
        remaining_evidence = {
            item.event.payload.get("evaluation_id")
            for item in (
                await sdk.queries.query_events(
                    EventFilter(event_types=("evaluation.completed",)),
                    limit=100,
                )
            ).events
            if item.event.event_id in after_delete.evidence_event_ids
        }
        assert removed_evaluation_ids.isdisjoint(remaining_evidence)
    finally:
        await sdk.close()
