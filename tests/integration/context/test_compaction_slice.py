from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_sdk.context import (
    CompactionLevel,
    CompactionPolicy,
    ContextBudget,
    ContextCapsule,
    ContextPlanner,
    ContextRetrieval,
)
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest, UsageReported
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


def _event(
    event_id: str,
    event_type: str,
    *,
    session_id: str = "ses_context",
    run_id: str | None,
    sequence: int,
    payload: dict[str, Any],
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        type=event_type,
        session_id=session_id,
        run_id=run_id,
        sequence=sequence,
        payload=payload,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_forced_compaction_preserves_ledger_and_sources() -> None:
    store = InMemoryStore()
    sources = (
        _event(
            "evt_user",
            "run.created",
            run_id="run_1",
            sequence=1,
            payload={"user_input": "ship the slice"},
        ),
        _event(
            "evt_assistant",
            "model.text.delta",
            run_id="run_1",
            sequence=2,
            payload={"text": "working"},
        ),
        _event(
            "evt_ignored",
            "run.started",
            run_id="run_1",
            sequence=3,
            payload={"user_input": "must not be projected"},
        ),
        _event(
            "evt_tool",
            "tool.call.completed",
            run_id="run_1",
            sequence=4,
            payload={"content": "tool evidence"},
        ),
        _event(
            "evt_latest_user",
            "context.message.appended",
            run_id=None,
            sequence=1,
            payload={"role": "user", "content": "keep this exact"},
        ),
        _event(
            "evt_derived",
            "context.view.created",
            run_id="view_old",
            sequence=1,
            payload={"view_id": "view_old"},
        ),
    )
    await store.commit(
        CommitBatch(
            events=sources,
            snapshots=(
                SnapshotWrite(
                    "session",
                    "ses_context",
                    "ses_context",
                    1,
                    {"session_id": "ses_context"},
                ),
            ),
        )
    )
    before = await store.read_events(after_cursor=0, session_id="ses_context")
    calls: list[dict[str, object]] = []

    async def acompletion(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "parsed": {
                            "objective": "ship the slice",
                            "constraints": ["keep originals"],
                            "decisions": [],
                            "facts": ["tool evidence exists"],
                            "next_actions": ["verify"],
                            "artifact_refs": [],
                            "source_event_ids": [
                                "evt_user",
                                "evt_assistant",
                                "evt_tool",
                                "evt_latest_user",
                            ],
                        }
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 6,
                "total_tokens": 18,
            },
        }

    token_counts = iter((40, 9))
    planner = ContextPlanner(
        store,
        LiteLLMGateway._for_test(acompletion),
        model="fake/compact",
        model_window=100,
        output_reserve=10,
        tool_schema_tokens=5,
        safety_reserve=5,
        _token_counter=lambda **_: next(token_counts),
    )
    view = await planner.build(
        "ses_context",
        force_level="L3",
        protected_event_ids={"evt_tool"},
    )

    assert view.capsule_id is not None
    assert view.message_refs == ("evt_tool", "evt_latest_user")
    retrieval = ContextRetrieval(store)
    capsule = await retrieval.get_capsule(
        view.capsule_id,
        session_id="ses_context",
    )
    assert isinstance(capsule, ContextCapsule)
    assert set(capsule.source_event_ids) <= {event.event_id for event in sources}
    assert {"evt_tool", "evt_latest_user"} <= set(capsule.source_event_ids)
    after = await store.read_events(
        after_cursor=0,
        session_id="ses_context",
    )
    assert after[: len(before)] == before
    assert [stored.event.type for stored in after[len(before) :]] == [
        "context.compaction.completed",
        "context.view.created",
    ]
    assert [stored.event.run_id for stored in after[len(before) :]] == [
        view.view_id,
        view.view_id,
    ]
    assert [stored.event.sequence for stored in after[len(before) :]] == [1, 2]
    assert view.budget.available_input_tokens == 80
    assert view.budget.watermark_ratio == 0.5
    assert view.estimated_tokens == 9
    assert view.recommended_level is CompactionLevel.L0
    assert view.applied_level is CompactionLevel.L3
    assert calls[0]["stream"] is False
    assert calls[0]["response_format"] is ContextCapsule
    assert "purpose" not in calls[0]
    raw_messages = calls[0]["messages"]
    assert isinstance(raw_messages, list)
    source_document = json.loads(raw_messages[-1]["content"])
    assert source_document["protected_event_ids"] == [
        "evt_tool",
        "evt_latest_user",
    ]
    assert [item["event_id"] for item in source_document["sources"]] == [
        "evt_user",
        "evt_assistant",
        "evt_tool",
        "evt_latest_user",
    ]


def _capsule_data(source_event_ids: list[str]) -> dict[str, object]:
    return {
        "objective": "objective",
        "constraints": ["constraint"],
        "decisions": ["decision"],
        "facts": ["fact"],
        "next_actions": ["next"],
        "artifact_refs": ["artifact_1"],
        "source_event_ids": source_event_ids,
    }


def test_policy_thresholds_are_exact_strict_and_recommend_at_boundaries() -> None:
    policy = CompactionPolicy()
    assert (
        policy.l1_reference,
        policy.l2_selective,
        policy.l3_summary,
        policy.l4_rebase,
        policy.recovery_target,
    ) == (0.70, 0.80, 0.90, 0.96, 0.75)
    assert [
        policy.recommend(ratio)
        for ratio in (0.69, 0.70, 0.80, 0.90, 0.96)
    ] == [
        CompactionLevel.L0,
        CompactionLevel.L1,
        CompactionLevel.L2,
        CompactionLevel.L3,
        CompactionLevel.L4,
    ]

    with pytest.raises(ValidationError):
        CompactionPolicy(l2_selective=0.70)
    with pytest.raises(ValidationError):
        CompactionPolicy(l1_reference=0.80, recovery_target=0.80)
    with pytest.raises(ValidationError):
        CompactionPolicy(l1_reference="0.70")  # type: ignore[arg-type]


def test_budget_reserve_arithmetic_is_exact_and_strict() -> None:
    budget = ContextBudget.calculate(
        model_window=100,
        output_reserve=20,
        tool_schema_tokens=7,
        safety_reserve=3,
        projected_source_tokens=35,
    )
    assert budget.available_input_tokens == 70
    assert budget.watermark_ratio == 0.5
    with pytest.raises(ValidationError):
        ContextBudget.calculate(
            model_window=100,
            output_reserve=-1,
            tool_schema_tokens=0,
            safety_reserve=0,
            projected_source_tokens=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["model", "mapping", "json"])
async def test_gateway_structured_completion_accepts_supported_shapes_and_usage(
    response_kind: str,
) -> None:
    calls: list[dict[str, object]] = []
    source = _capsule_data(["evt_1"])
    if response_kind == "model":
        parsed: object = ContextCapsule.model_validate(source)
    elif response_kind == "mapping":
        parsed = source
    else:
        parsed = None

    async def acompletion(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        message: dict[str, object] = (
            {"content": json.dumps(source)}
            if response_kind == "json"
            else {"parsed": parsed}
        )
        return {
            "choices": [{"message": message}],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 5,
                "total_tokens": 9,
            },
        }

    result = await LiteLLMGateway._for_test(acompletion).complete_structured(
        ModelRequest(
            model="fake/model",
            messages=({"role": "user", "content": "compact"},),
            params={"temperature": 0},
            purpose="compaction",
        ),
        ContextCapsule,
    )

    assert result.parsed == ContextCapsule.model_validate(source)
    assert result.usage == UsageReported(4, 5, 9)
    assert calls == [
        {
            "model": "fake/model",
            "messages": [{"role": "user", "content": "compact"}],
            "tools": [],
            "temperature": 0,
            "stream": False,
            "response_format": ContextCapsule,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {"content": "not json"}}]},
    ],
)
async def test_gateway_sanitizes_malformed_structured_responses(
    response: dict[str, object],
) -> None:
    async def acompletion(**_: object) -> dict[str, object]:
        return response

    with pytest.raises(AgentSDKError) as raised:
        await LiteLLMGateway._for_test(acompletion).complete_structured(
            ModelRequest(model="fake/model", messages=()),
            ContextCapsule,
        )
    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "structured model response invalid"
    assert "not json" not in str(raised.value)


@pytest.mark.asyncio
async def test_gateway_sanitizes_provider_failure_and_propagates_cancellation() -> None:
    async def failed(**_: object) -> object:
        raise RuntimeError("provider secret")

    with pytest.raises(AgentSDKError) as raised:
        await LiteLLMGateway._for_test(failed).complete_structured(
            ModelRequest(model="fake/model", messages=()),
            ContextCapsule,
        )
    assert raised.value.message == "structured model call failed"
    assert "provider secret" not in str(raised.value)

    async def cancelled(**_: object) -> object:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await LiteLLMGateway._for_test(cancelled).complete_structured(
            ModelRequest(model="fake/model", messages=()),
            ContextCapsule,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["getter", "usage_overflow"])
async def test_gateway_sanitizes_nonstandard_response_parsing_failures(
    failure_kind: str,
) -> None:
    class BrokenResponse:
        @property
        def choices(self) -> object:
            raise RuntimeError("raw response secret")

    response: object
    if failure_kind == "getter":
        response = BrokenResponse()
    else:
        response = {
            "choices": [{"message": {"parsed": _capsule_data(["evt_1"])}}],
            "usage": {
                "prompt_tokens": float("inf"),
                "completion_tokens": 1,
                "total_tokens": 1,
            },
        }

    async def acompletion(**_: object) -> object:
        return response

    with pytest.raises(AgentSDKError) as raised:
        await LiteLLMGateway._for_test(acompletion).complete_structured(
            ModelRequest(model="fake/model", messages=()),
            ContextCapsule,
        )
    assert raised.value.message == "structured model response invalid"
    assert "raw response secret" not in str(raised.value)


class _RecordingStore:
    def __init__(self, delegate: StateStore) -> None:
        self.delegate = delegate
        self.batches: list[CommitBatch] = []

    async def commit(self, batch: CommitBatch) -> CommitResult:
        result = await self.delegate.commit(batch)
        self.batches.append(batch)
        return result

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
    ) -> list[Any]:
        return await self.delegate.read_events(
            after_cursor=after_cursor,
            session_id=session_id,
        )

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        return await self.delegate.get_snapshot(kind, entity_id)

    async def delete_session(self, session_id: str) -> None:
        await self.delegate.delete_session(session_id)


async def _seed_projection(store: StateStore, *, session_id: str = "ses_projection") -> None:
    await store.commit(
        CommitBatch(
            events=(
                _event(
                    "evt_projection_user",
                    "run.created",
                    session_id=session_id,
                    run_id="run_projection",
                    sequence=1,
                    payload={"user_input": "first user"},
                ),
                _event(
                    "evt_projection_assistant",
                    "model.text.delta",
                    session_id=session_id,
                    run_id="run_projection",
                    sequence=2,
                    payload={"text": "assistant"},
                ),
                _event(
                    "evt_projection_tool",
                    "tool.call.completed",
                    session_id=session_id,
                    run_id="run_projection",
                    sequence=3,
                    payload={"content": "tool"},
                ),
                _event(
                    "evt_projection_latest",
                    "context.message.appended",
                    session_id=session_id,
                    run_id=None,
                    sequence=1,
                    payload={"role": "user", "content": "latest user"},
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "session",
                    session_id,
                    session_id,
                    1,
                    {"session_id": session_id},
                ),
            ),
        )
    )


def _planner(
    store: StateStore,
    acompletion: Callable[..., Awaitable[Any]],
    *,
    token_count: int = 50,
    model_window: int = 100,
    output_reserve: int = 0,
    tool_schema_tokens: int = 0,
    safety_reserve: int = 0,
) -> ContextPlanner:
    return ContextPlanner(
        store,
        LiteLLMGateway._for_test(acompletion),
        model="fake/compact",
        model_window=model_window,
        output_reserve=output_reserve,
        tool_schema_tokens=tool_schema_tokens,
        safety_reserve=safety_reserve,
        _token_counter=lambda **_: token_count,
    )


@pytest.mark.asyncio
async def test_automatic_recommendation_does_not_claim_l1_or_l2_is_applied() -> None:
    store = _RecordingStore(InMemoryStore())
    await _seed_projection(store)
    store.batches.clear()
    model_calls = 0

    async def acompletion(**_: object) -> object:
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("automatic recommendation must not compact in M01")

    view = await _planner(store, acompletion, token_count=80).build("ses_projection")

    assert view.recommended_level is CompactionLevel.L2
    assert view.applied_level is CompactionLevel.L0
    assert view.capsule_id is None
    assert view.message_refs == (
        "evt_projection_user",
        "evt_projection_assistant",
        "evt_projection_tool",
        "evt_projection_latest",
    )
    assert model_calls == 0
    assert len(store.batches) == 1
    assert [event.type for event in store.batches[0].events] == [
        "context.view.created"
    ]


@pytest.mark.asyncio
async def test_non_positive_capacity_and_unknown_protected_id_fail_before_model_call() -> None:
    store = _RecordingStore(InMemoryStore())
    await _seed_projection(store)
    store.batches.clear()
    model_calls = 0

    async def acompletion(**_: object) -> object:
        nonlocal model_calls
        model_calls += 1
        return {}

    with pytest.raises(AgentSDKError) as capacity:
        await _planner(
            store,
            acompletion,
            model_window=10,
            output_reserve=5,
            tool_schema_tokens=3,
            safety_reserve=2,
        ).build("ses_projection", force_level="L3")
    assert capacity.value.code is ErrorCode.INVALID_STATE
    assert capacity.value.message == "context budget has no input capacity"

    with pytest.raises(AgentSDKError) as protected:
        await _planner(store, acompletion).build(
            "ses_projection",
            force_level="L3",
            protected_event_ids={"evt_not_projected"},
        )
    assert protected.value.code is ErrorCode.INVALID_STATE
    assert protected.value.message == "protected context source not found"
    assert model_calls == 0
    assert store.batches == []


@pytest.mark.asyncio
async def test_missing_session_or_malformed_allowlisted_event_fails_closed() -> None:
    no_session = InMemoryStore()
    await no_session.commit(
        CommitBatch(
            events=(
                _event(
                    "evt_orphan",
                    "run.created",
                    session_id="ses_orphan",
                    run_id="run_orphan",
                    sequence=1,
                    payload={"user_input": "orphan"},
                ),
            )
        )
    )

    async def acompletion(**_: object) -> object:
        raise AssertionError("invalid session data must fail before a model call")

    with pytest.raises(AgentSDKError) as missing_session:
        await _planner(no_session, acompletion).build("ses_orphan")
    assert missing_session.value.code is ErrorCode.NOT_FOUND
    assert missing_session.value.message == "session not found"

    malformed = InMemoryStore()
    await malformed.commit(
        CommitBatch(
            events=(
                _event(
                    "evt_malformed",
                    "context.message.appended",
                    session_id="ses_malformed",
                    run_id=None,
                    sequence=1,
                    payload={
                        "role": "user",
                        "content": "content",
                        "extra": True,
                    },
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "session",
                    "ses_malformed",
                    "ses_malformed",
                    1,
                    {"session_id": "ses_malformed"},
                ),
            ),
        )
    )
    with pytest.raises(AgentSDKError) as malformed_event:
        await _planner(malformed, acompletion).build("ses_malformed")
    assert malformed_event.value.code is ErrorCode.INVALID_STATE
    assert malformed_event.value.message == "context source event is invalid"


@pytest.mark.asyncio
async def test_budget_and_token_counter_failures_are_stable_and_leave_no_view() -> None:
    store = _RecordingStore(InMemoryStore())
    await _seed_projection(store)
    store.batches.clear()

    async def acompletion(**_: object) -> object:
        raise AssertionError("budget failures must happen before a model call")

    invalid = ContextPlanner(
        store,
        LiteLLMGateway._for_test(acompletion),
        model="fake/model",
        model_window="100",  # type: ignore[arg-type]
        _token_counter=lambda **_: 1,
    )
    with pytest.raises(AgentSDKError) as invalid_config:
        await invalid.build("ses_projection")
    assert invalid_config.value.message == "context budget configuration invalid"

    def failed_counter(**_: object) -> int:
        raise RuntimeError("counter secret")

    with pytest.raises(AgentSDKError) as failed_estimate:
        await ContextPlanner(
            store,
            LiteLLMGateway._for_test(acompletion),
            model="fake/model",
            model_window=100,
            _token_counter=failed_counter,
        ).build("ses_projection")
    assert failed_estimate.value.message == "context token estimation failed"
    assert "counter secret" not in str(failed_estimate.value)

    with pytest.raises(AgentSDKError) as invalid_count:
        await ContextPlanner(
            store,
            LiteLLMGateway._for_test(acompletion),
            model="fake/model",
            model_window=100,
            _token_counter=lambda **_: True,  # type: ignore[arg-type,return-value]
        ).build("ses_projection")
    assert invalid_count.value.message == "context token estimation failed"
    assert store.batches == []


@pytest.mark.asyncio
async def test_compacted_view_reestimation_failure_is_stable_and_not_persisted() -> None:
    store = _RecordingStore(InMemoryStore())
    await _seed_projection(store)
    store.batches.clear()
    counter_calls = 0

    def counter(**_: object) -> int:
        nonlocal counter_calls
        counter_calls += 1
        if counter_calls == 1:
            return 50
        raise RuntimeError("second counter secret")

    async def acompletion(**_: object) -> dict[str, object]:
        return _structured_response(
            ["evt_projection_tool", "evt_projection_latest"]
        )

    with pytest.raises(AgentSDKError) as raised:
        await ContextPlanner(
            store,
            LiteLLMGateway._for_test(acompletion),
            model="fake/model",
            model_window=100,
            _token_counter=counter,
        ).build(
            "ses_projection",
            force_level="L3",
            protected_event_ids={"evt_projection_tool"},
        )
    assert raised.value.message == "context token estimation failed"
    assert "second counter secret" not in str(raised.value)
    assert store.batches == []


def _structured_response(source_event_ids: list[str]) -> dict[str, object]:
    return {
        "choices": [{"message": {"parsed": _capsule_data(source_event_ids)}}],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_factory",
    [
        lambda: _structured_response(["evt_unknown", "evt_projection_latest"]),
        lambda: _structured_response(["evt_projection_user"]),
        lambda: _structured_response(
            ["evt_projection_latest", "evt_projection_latest"]
        ),
        lambda: {"choices": []},
    ],
)
async def test_invalid_capsule_or_malformed_model_response_falls_back_safely(
    response_factory: Callable[[], dict[str, object]],
) -> None:
    store = _RecordingStore(InMemoryStore())
    await _seed_projection(store)
    store.batches.clear()

    async def acompletion(**_: object) -> dict[str, object]:
        return response_factory()

    view = await _planner(store, acompletion).build(
        "ses_projection",
        force_level="L4",
        protected_event_ids={"evt_projection_tool"},
    )

    assert view.applied_level is CompactionLevel.L0
    assert view.capsule_id is None
    assert view.message_refs == (
        "evt_projection_user",
        "evt_projection_assistant",
        "evt_projection_tool",
        "evt_projection_latest",
    )
    assert len(store.batches) == 1
    batch = store.batches[0]
    assert [event.type for event in batch.events] == [
        "context.compaction.failed",
        "context.view.created",
    ]
    assert [snapshot.kind for snapshot in batch.snapshots] == ["context_view"]
    assert batch.events[0].payload["code"] == "context_compaction_failed"
    serialized = json.dumps(
        [event.payload for event in batch.events],
        sort_keys=True,
    )
    assert "evt_unknown" not in serialized
    assert "choices" not in serialized


@pytest.mark.asyncio
async def test_model_failure_fallback_is_sanitized_and_has_no_capsule_snapshot() -> None:
    store = _RecordingStore(InMemoryStore())
    await _seed_projection(store)
    store.batches.clear()

    async def acompletion(**_: object) -> object:
        raise RuntimeError("raw provider secret")

    view = await _planner(store, acompletion).build(
        "ses_projection",
        force_level="L3",
    )
    assert view.capsule_id is None
    assert [snapshot.kind for snapshot in store.batches[0].snapshots] == [
        "context_view"
    ]
    payload_text = json.dumps(store.batches[0].events[0].payload)
    assert "raw provider secret" not in payload_text


@pytest.mark.asyncio
async def test_cancellation_propagates_without_persistence_or_orphan_tasks() -> None:
    store = _RecordingStore(InMemoryStore())
    await _seed_projection(store)
    store.batches.clear()
    before_tasks = set(asyncio.all_tasks())

    async def acompletion(**_: object) -> object:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _planner(store, acompletion).build(
            "ses_projection",
            force_level="L3",
        )
    await asyncio.sleep(0)
    assert store.batches == []
    assert set(asyncio.all_tasks()) <= before_tasks


@pytest.mark.asyncio
async def test_success_is_one_atomic_commit_with_capsule_view_and_events() -> None:
    store = _RecordingStore(InMemoryStore())
    await _seed_projection(store)
    store.batches.clear()

    async def acompletion(**_: object) -> dict[str, object]:
        return _structured_response(
            ["evt_projection_tool", "evt_projection_latest"]
        )

    view = await _planner(store, acompletion).build(
        "ses_projection",
        force_level="L3",
        protected_event_ids={"evt_projection_tool"},
    )

    assert len(store.batches) == 1
    batch = store.batches[0]
    assert [snapshot.kind for snapshot in batch.snapshots] == [
        "context_capsule",
        "context_view",
    ]
    assert [event.type for event in batch.events] == [
        "context.compaction.completed",
        "context.view.created",
    ]
    assert all(event.run_id == view.view_id for event in batch.events)


class _RejectingCommitStore(_RecordingStore):
    async def commit(self, batch: CommitBatch) -> CommitResult:
        self.batches.append(batch)
        raise RuntimeError("commit rejected")


@pytest.mark.asyncio
async def test_commit_failure_does_not_claim_fallback_or_leave_partial_state() -> None:
    durable = InMemoryStore()
    await _seed_projection(durable)
    before = await durable.read_events(after_cursor=0)
    store = _RejectingCommitStore(durable)

    async def acompletion(**_: object) -> dict[str, object]:
        return _structured_response(
            ["evt_projection_tool", "evt_projection_latest"]
        )

    with pytest.raises(AgentSDKError) as raised:
        await _planner(store, acompletion).build(
            "ses_projection",
            force_level="L3",
            protected_event_ids={"evt_projection_tool"},
        )
    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "context persistence failed"
    assert "commit rejected" not in str(raised.value)
    assert len(store.batches) == 1
    assert await durable.read_events(after_cursor=0) == before
    attempted_capsule_id = store.batches[0].snapshots[0].entity_id
    attempted_view_id = store.batches[0].snapshots[-1].entity_id
    assert await durable.get_snapshot("context_capsule", attempted_capsule_id) is None
    assert await durable.get_snapshot("context_view", attempted_view_id) is None


@pytest.mark.asyncio
async def test_sqlite_reopen_retrieval_order_and_session_deletion(tmp_path: Path) -> None:
    database = tmp_path / "context.db"
    store = await SQLiteStore.open(database)
    await _seed_projection(store)

    async def acompletion(**_: object) -> dict[str, object]:
        return _structured_response(
            [
                "evt_projection_latest",
                "evt_projection_user",
                "evt_projection_tool",
            ]
        )

    view = await _planner(store, acompletion).build(
        "ses_projection",
        force_level="L4",
        protected_event_ids={"evt_projection_tool"},
    )
    assert view.capsule_id is not None
    await store.close()

    reopened = await SQLiteStore.open(database)
    try:
        retrieval = ContextRetrieval(reopened)
        capsule = await retrieval.get_capsule(
            view.capsule_id,
            session_id="ses_projection",
        )
        assert capsule.source_event_ids == (
            "evt_projection_latest",
            "evt_projection_user",
            "evt_projection_tool",
        )
        sources = await retrieval.read_sources(
            view.capsule_id,
            session_id="ses_projection",
        )
        assert tuple(item.event.event_id for item in sources) == capsule.source_event_ids
        with pytest.raises(AgentSDKError):
            await retrieval.read_sources(
                view.capsule_id,
                session_id="ses_other",
            )

        await reopened.delete_session("ses_projection")
        assert await reopened.get_snapshot("context_view", view.view_id) is None
        assert await reopened.get_snapshot(
            "context_capsule",
            view.capsule_id,
        ) is None
        assert await reopened.read_events(
            after_cursor=0,
            session_id="ses_projection",
        ) == []
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_retrieval_fails_closed_for_missing_or_corrupt_records() -> None:
    store = InMemoryStore()
    await store.commit(
        CommitBatch(
            events=(
                _event(
                    "evt_present",
                    "run.created",
                    session_id="ses_retrieval",
                    run_id="run_retrieval",
                    sequence=1,
                    payload={"user_input": "present"},
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "context_capsule",
                    "cap_missing_source",
                    "ses_retrieval",
                    1,
                    {
                        "session_id": "ses_retrieval",
                        "capsule": _capsule_data(
                            ["evt_present", "evt_missing"]
                        ),
                    },
                ),
                SnapshotWrite(
                    "context_capsule",
                    "cap_corrupt",
                    "ses_retrieval",
                    1,
                    {"session_id": "ses_retrieval", "capsule": {"bad": True}},
                ),
            ),
        )
    )
    retrieval = ContextRetrieval(store)
    with pytest.raises(AgentSDKError) as missing:
        await retrieval.read_sources(
            "cap_missing_source",
            session_id="ses_retrieval",
        )
    assert missing.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(AgentSDKError) as corrupt:
        await retrieval.get_capsule(
            "cap_corrupt",
            session_id="ses_retrieval",
        )
    assert corrupt.value.code is ErrorCode.INTERNAL


class _RetrievalFaultStore(_RecordingStore):
    def __init__(
        self,
        delegate: StateStore,
        *,
        fail_snapshot: bool = False,
        cancel_snapshot: bool = False,
        fail_events: bool = False,
    ) -> None:
        super().__init__(delegate)
        self.fail_snapshot = fail_snapshot
        self.cancel_snapshot = cancel_snapshot
        self.fail_events = fail_events

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        if self.cancel_snapshot:
            raise asyncio.CancelledError
        if self.fail_snapshot:
            raise RuntimeError("snapshot store secret")
        return await super().get_snapshot(kind, entity_id)

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
    ) -> list[Any]:
        if self.fail_events:
            raise RuntimeError("event store secret")
        return await super().read_events(
            after_cursor=after_cursor,
            session_id=session_id,
        )


@pytest.mark.asyncio
async def test_retrieval_sanitizes_store_failures_and_propagates_cancellation() -> None:
    capsule_id = "cap_retrieval_fault"
    durable = InMemoryStore()
    await durable.commit(
        CommitBatch(
            snapshots=(
                SnapshotWrite(
                    "context_capsule",
                    capsule_id,
                    "ses_retrieval_fault",
                    1,
                    {
                        "session_id": "ses_retrieval_fault",
                        "capsule": _capsule_data(["evt_missing"]),
                    },
                ),
            ),
            events=(),
        )
    )

    with pytest.raises(AgentSDKError) as snapshot_failure:
        await ContextRetrieval(
            _RetrievalFaultStore(durable, fail_snapshot=True)
        ).get_capsule(capsule_id, session_id="ses_retrieval_fault")
    assert snapshot_failure.value.message == "context retrieval failed"
    assert "snapshot store secret" not in str(snapshot_failure.value)

    with pytest.raises(AgentSDKError) as event_failure:
        await ContextRetrieval(
            _RetrievalFaultStore(durable, fail_events=True)
        ).read_sources(capsule_id, session_id="ses_retrieval_fault")
    assert event_failure.value.message == "context retrieval failed"
    assert "event store secret" not in str(event_failure.value)

    with pytest.raises(asyncio.CancelledError):
        await ContextRetrieval(
            _RetrievalFaultStore(durable, cancel_snapshot=True)
        ).get_capsule(capsule_id, session_id="ses_retrieval_fault")
