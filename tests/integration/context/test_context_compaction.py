from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_sdk.context import (
    CompactionLevel,
    ContextPlanner,
    ContextRetrieval,
)
from agent_sdk.events.models import EventEnvelope
from agent_sdk.models.litellm_gateway import LiteLLMGateway
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore


def _event(
    event_id: str,
    *,
    sequence: int,
    role: str,
    content: str,
    session_id: str = "ses_task2",
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        type="context.message.appended",
        session_id=session_id,
        run_id=None,
        sequence=sequence,
        payload={"role": role, "content": content},
        occurred_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


async def _seed(
    store: InMemoryStore,
    *,
    session_id: str = "ses_task2",
) -> tuple[EventEnvelope, ...]:
    events = (
        _event("evt_old_user", sequence=1, role="user", content="old question"),
        _event(
            "evt_old_answer",
            sequence=2,
            role="assistant",
            content="old answer",
        ),
        _event(
            "evt_old_tool",
            sequence=3,
            role="tool",
            content="old tool detail " * 40,
        ),
        _event(
            "evt_recent_answer",
            sequence=4,
            role="assistant",
            content="recent answer",
        ),
        _event(
            "evt_latest_user",
            sequence=5,
            role="user",
            content="latest question",
        ),
    )
    await store.commit(
        CommitBatch(
            events=events,
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
    return events


def _response(*refs: str, objective: str = "ship") -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "parsed": {
                        "objective": objective,
                        "constraints": ["preserve evidence"],
                        "decisions": [],
                        "facts": [],
                        "next_actions": ["verify"],
                        "artifact_refs": [],
                        "source_event_ids": list(refs),
                    }
                }
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 5,
            "total_tokens": 17,
        },
    }


def _planner(
    store: InMemoryStore,
    acompletion: Any,
    *,
    token_count: int,
) -> ContextPlanner:
    return ContextPlanner(
        store,
        LiteLLMGateway._for_test(acompletion),
        model="fake/compact",
        model_window=100,
        recent_messages=2,
        tool_preview_bytes=256,
        _token_counter=lambda **_: token_count,
    )


def _planner_with_counts(
    store: InMemoryStore,
    acompletion: Any,
    *counts: int,
    recent_messages: int = 2,
) -> ContextPlanner:
    token_counts = iter(counts)
    return ContextPlanner(
        store,
        LiteLLMGateway._for_test(acompletion),
        model="fake/compact",
        model_window=100,
        recent_messages=recent_messages,
        tool_preview_bytes=256,
        _token_counter=lambda **_: next(token_counts),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token_count", "expected"),
    [
        (70, CompactionLevel.L1),
        (80, CompactionLevel.L2),
    ],
)
async def test_automatic_policy_applies_deterministic_levels(
    token_count: int,
    expected: CompactionLevel,
) -> None:
    store = InMemoryStore()
    await _seed(store)
    model_calls = 0

    async def acompletion(**_: object) -> object:
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("L1/L2 must not call the model")

    view = await _planner(
        store,
        acompletion,
        token_count=token_count,
    ).build("ses_task2")

    assert view.recommended_level is expected
    assert view.applied_level is expected
    assert view.fallback_from is None
    assert view.source_refs == (
        "evt_old_user",
        "evt_old_answer",
        "evt_old_tool",
        "evt_recent_answer",
        "evt_latest_user",
    )
    assert model_calls == 0


@pytest.mark.asyncio
async def test_allow_lossy_false_caps_automatic_l4_at_l2() -> None:
    store = InMemoryStore()
    await _seed(store)

    async def acompletion(**_: object) -> object:
        raise AssertionError("lossless cap must not call the model")

    view = await _planner(
        store,
        acompletion,
        token_count=96,
    ).build("ses_task2", allow_lossy=False)

    assert view.recommended_level is CompactionLevel.L4
    assert view.applied_level is CompactionLevel.L2
    assert view.fallback_from is None
    assert any(value.startswith("outcome:") for value in view.transformations)


@pytest.mark.asyncio
async def test_l3_retains_recent_and_protected_messages() -> None:
    store = InMemoryStore()
    await _seed(store)
    requests: list[dict[str, object]] = []

    async def acompletion(**kwargs: object) -> dict[str, object]:
        requests.append(kwargs)
        return _response("evt_old_user", "evt_old_answer", objective="summary")

    view = await _planner(store, acompletion, token_count=90).build(
        "ses_task2",
        protected_event_ids={"evt_old_tool"},
    )

    assert view.applied_level is CompactionLevel.L3
    assert view.message_refs == (
        "evt_old_tool",
        "evt_recent_answer",
        "evt_latest_user",
    )
    assert view.capsule_id is not None
    assert view.source_refs == (
        "evt_old_user",
        "evt_old_answer",
        "evt_old_tool",
        "evt_recent_answer",
        "evt_latest_user",
    )
    document = json.loads(requests[0]["messages"][-1]["content"])
    assert [item["event_id"] for item in document["sources"]] == [
        "evt_old_user",
        "evt_old_answer",
    ]


@pytest.mark.asyncio
async def test_l3_over_budget_output_falls_back_to_l2_with_usage() -> None:
    store = InMemoryStore()
    await _seed(store)

    async def acompletion(**_: object) -> dict[str, object]:
        return _response(
            "evt_old_user",
            "evt_old_answer",
            "evt_old_tool",
            objective="oversized summary",
        )

    view = await _planner_with_counts(
        store,
        acompletion,
        90,
        101,
        60,
    ).build("ses_task2", force_level="L3")

    assert view.applied_level is CompactionLevel.L2
    assert view.fallback_from is CompactionLevel.L3
    assert view.capsule_id is None
    assert view.estimated_tokens == 60
    events = await store.read_events(after_cursor=0, session_id="ses_task2")
    created = [item.event for item in events if item.event.type == "context.view.created"]
    failed = [
        item.event for item in events if item.event.type == "context.compaction.failed"
    ]
    completed = [
        item.event for item in events if item.event.type == "context.compaction.completed"
    ]
    assert completed == []
    assert created[-1].payload["compaction_usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
    }
    assert failed[-1].payload["usage"] == created[-1].payload["compaction_usage"]


@pytest.mark.asyncio
async def test_forced_l3_with_empty_closed_slice_skips_model_and_falls_back() -> None:
    store = InMemoryStore()
    await _seed(store)
    model_calls = 0

    async def acompletion(**_: object) -> object:
        nonlocal model_calls
        model_calls += 1
        return _response("evt_latest_user")

    view = await _planner_with_counts(
        store,
        acompletion,
        90,
        60,
        recent_messages=5,
    ).build("ses_task2", force_level="L3")

    assert model_calls == 0
    assert view.applied_level is CompactionLevel.L2
    assert view.fallback_from is CompactionLevel.L3
    assert view.capsule_id is None


@pytest.mark.asyncio
async def test_l4_rebases_prior_capsule_evidence() -> None:
    store = InMemoryStore()
    await _seed(store)
    call_count = 0

    async def acompletion(**kwargs: object) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _response(
                "evt_old_user",
                "evt_old_answer",
                "evt_old_tool",
                objective="summary",
            )
        document = json.loads(kwargs["messages"][-1]["content"])
        return _response(
            document["capsule_ids"][0],
            "evt_recent_answer",
            "evt_latest_user",
            objective="rebased",
        )

    first = await _planner(store, acompletion, token_count=90).build(
        "ses_task2",
        force_level="L3",
    )
    assert first.capsule_id is not None
    second = await _planner(store, acompletion, token_count=96).build(
        "ses_task2",
    )

    assert second.applied_level is CompactionLevel.L4
    assert second.capsule_id is not None
    capsule = await ContextRetrieval(store).get_capsule(
        second.capsule_id,
        session_id="ses_task2",
    )
    assert first.capsule_id in capsule.source_event_ids
    recovered = await ContextRetrieval(store).read_sources(
        second.capsule_id,
        session_id="ses_task2",
    )
    assert {"evt_old_user", "evt_old_answer"} <= {
        item.event.event_id for item in recovered
    }


@pytest.mark.asyncio
async def test_l4_over_budget_output_falls_back_to_l2_with_usage() -> None:
    store = InMemoryStore()
    await _seed(store)
    call_count = 0

    async def acompletion(**kwargs: object) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _response(
                "evt_old_user",
                "evt_old_answer",
                "evt_old_tool",
                objective="summary",
            )
        document = json.loads(kwargs["messages"][-1]["content"])
        return _response(
            document["capsule_ids"][0],
            "evt_recent_answer",
            "evt_latest_user",
            objective="oversized rebase",
        )

    first = await _planner(store, acompletion, token_count=90).build(
        "ses_task2",
        force_level="L3",
    )
    assert first.capsule_id is not None
    view = await _planner_with_counts(
        store,
        acompletion,
        96,
        101,
        60,
    ).build("ses_task2", force_level="L4")

    assert view.applied_level is CompactionLevel.L2
    assert view.fallback_from is CompactionLevel.L4
    assert view.capsule_id is None
    assert view.estimated_tokens == 60
    events = await store.read_events(after_cursor=0, session_id="ses_task2")
    completed = [
        item.event for item in events if item.event.type == "context.compaction.completed"
    ]
    failed = [
        item.event for item in events if item.event.type == "context.compaction.failed"
    ]
    assert len(completed) == 1
    assert failed[-1].payload["requested_level"] == "L4"
    assert failed[-1].payload["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
    }


@pytest.mark.asyncio
async def test_invalid_l4_persists_same_l2_fallback_and_original_events() -> None:
    store = InMemoryStore()
    originals = await _seed(store)

    async def invalid(**_: object) -> dict[str, object]:
        return _response("evt_unknown", objective="invalid")

    planner = _planner(store, invalid, token_count=96)
    fallback = await planner.build("ses_task2")

    async def forbidden(**_: object) -> object:
        raise AssertionError("forced L2 must not call the model")

    expected = await _planner(store, forbidden, token_count=96).build(
        "ses_task2",
        force_level="L2",
    )

    assert fallback.applied_level is CompactionLevel.L2
    assert fallback.fallback_from is CompactionLevel.L4
    assert fallback.capsule_id is None
    assert fallback.message_refs == expected.message_refs
    assert fallback.source_refs == expected.source_refs
    assert fallback.transformations == expected.transformations
    events = await store.read_events(after_cursor=0, session_id="ses_task2")
    assert tuple(item.event for item in events[: len(originals)]) == originals
    created = [item.event for item in events if item.event.type == "context.view.created"]
    failed = [
        item.event for item in events if item.event.type == "context.compaction.failed"
    ]
    assert created[-2].payload["fallback_from"] == "L4"
    assert failed[-1].payload["requested_level"] == "L4"
