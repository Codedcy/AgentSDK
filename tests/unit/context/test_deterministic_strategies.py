from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from agent_sdk.context.models import CompactionLevel, SourceMessage
from agent_sdk.context.rendering import render_level
from agent_sdk.context.sources import checkpoint_ref, extract_sources
from agent_sdk.context.strategies import apply_l0, apply_l1, apply_l2
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.reconciliation import RunCheckpoint, RunCheckpointPhase
from agent_sdk.storage.base import StoredEvent


def _source(
    ref: str,
    role: str,
    content: str,
    *,
    protected: bool = False,
    current: bool = False,
    **message_fields: Any,
) -> SourceMessage:
    return SourceMessage(
        ref=ref,
        message={"role": role, "content": content, **message_fields},
        protected=protected,
        current=current,
    )


def _strategy_sources() -> tuple[SourceMessage, ...]:
    long_result = json.dumps(
        {"rows": ["数据🙂" * 100, {"b": 2, "a": 1}]},
        ensure_ascii=False,
    )
    return (
        _source("evt-user-old", "user", "older request"),
        _source("evt-assistant-old", "assistant", "older answer"),
        _source(
            "evt-tool",
            "tool",
            long_result,
            tool_call_id="call-old",
            name="lookup",
        ),
        _source(
            "evt-tool-2",
            "tool",
            json.dumps(
                {"rows": ["数据🙂" * 100, {"a": 1, "b": 2}]},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            tool_call_id="call-repeat",
            name="lookup",
        ),
        _source(
            "evt-constraint",
            "system",
            "Never publish secrets.",
            protected=True,
        ),
        _source(
            "checkpoint:run-current:7:0",
            "user",
            "current request",
            protected=True,
            current=True,
        ),
        _source(
            "evt-current-model",
            "assistant",
            "",
            protected=True,
            current=True,
            tool_calls=[
                {
                    "id": "call-current",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        ),
        _source(
            "evt-current-tool",
            "tool",
            '{"ok":true}',
            protected=True,
            current=True,
            tool_call_id="call-current",
            name="lookup",
        ),
        _source(
            "state:workflow:wfr-active",
            "system",
            '{"status":"running","workflow_run_id":"wfr-active"}',
            protected=True,
        ),
        _source(
            "state:child:run-child",
            "system",
            '{"run_id":"run-child","status":"running"}',
            protected=True,
        ),
    )


def _outcome(item: SourceMessage) -> dict[str, Any]:
    value = json.loads(item.message["content"])
    assert list(value) == ["kind", "role", "source_refs", "status", "summary"]
    return value


def test_l0_returns_all_messages_unchanged_ordered_and_detached() -> None:
    sources = _strategy_sources()
    before = copy.deepcopy([item.model_dump(mode="json") for item in sources])

    rendered = apply_l0(sources)

    assert rendered.items == sources
    assert rendered.source_refs == tuple(item.ref for item in sources)
    assert rendered.transformations == ()
    assert [item.model_dump(mode="json") for item in sources] == before
    with pytest.raises(TypeError):
        rendered.items[0].message["content"] = "mutated"  # type: ignore[index]


def test_l1_previews_tools_byte_safely_and_deduplicates_canonical_json() -> None:
    sources = _strategy_sources()
    before = copy.deepcopy([item.model_dump(mode="json") for item in sources])

    rendered = apply_l1(sources, tool_preview_bytes=256)

    tool = next(item for item in rendered.items if item.ref == "evt-tool")
    duplicate = next(item for item in rendered.items if item.ref == "evt-tool-2")
    assert len(tool.message["content"].encode("utf-8")) <= 256 + 96
    assert "[source:evt-tool]" in tool.message["content"]
    assert duplicate.message["content"] == "[duplicate:evt-tool]"
    assert rendered.source_refs == tuple(item.ref for item in sources)
    assert rendered.transformations == (
        "tool_preview:evt-tool",
        "dedupe:evt-tool-2",
    )
    assert [item.model_dump(mode="json") for item in sources] == before


def test_l1_treats_nonstandard_json_constants_as_plain_tool_text() -> None:
    sources = (
        _source("evt-nan", "tool", "NaN"),
        _source("evt-nan-repeat", "tool", "NaN"),
    )

    rendered = apply_l1(sources, tool_preview_bytes=16)

    assert rendered.items[0] == sources[0]
    assert rendered.items[1].message["content"] == "[duplicate:evt-nan]"


def test_l2_retains_protected_current_and_recent_and_structures_old_outcomes() -> None:
    sources = _strategy_sources()
    before = copy.deepcopy([item.model_dump(mode="json") for item in sources])

    rendered = apply_l2(
        sources,
        recent_messages=1,
        tool_preview_bytes=64,
    )

    by_ref = {item.ref: item for item in rendered.items}
    for source in sources[4:]:
        assert by_ref[source.ref].message == source.message
    for ref, expected_role, expected_kind in (
        ("evt-user-old", "user", "exchange"),
        ("evt-assistant-old", "assistant", "exchange"),
        ("evt-tool", "tool", "tool_result"),
        ("evt-tool-2", "tool", "tool_result"),
    ):
        outcome = _outcome(by_ref[ref])
        assert outcome["kind"] == expected_kind
        assert outcome["role"] == expected_role
        assert outcome["status"] == "completed"
        assert outcome["source_refs"] == [ref]
        assert outcome["summary"]
    assert rendered.source_refs == tuple(item.ref for item in sources)
    assert len(rendered.source_refs) == len(set(rendered.source_refs))
    assert [item.model_dump(mode="json") for item in sources] == before


def test_render_level_dispatches_l0_l2_and_rejects_model_levels() -> None:
    sources = _strategy_sources()
    assert render_level(
        CompactionLevel.L0,
        sources,
        recent_messages=2,
        tool_preview_bytes=32,
    ) == apply_l0(sources)
    assert render_level(
        CompactionLevel.L1,
        sources,
        recent_messages=2,
        tool_preview_bytes=32,
    ) == apply_l1(sources, tool_preview_bytes=32)
    assert render_level(
        CompactionLevel.L2,
        sources,
        recent_messages=2,
        tool_preview_bytes=32,
    ) == apply_l2(sources, recent_messages=2, tool_preview_bytes=32)
    with pytest.raises(
        ValueError,
        match="deterministic renderer supports L0-L2 only",
    ):
        render_level(
            CompactionLevel.L3,
            sources,
            recent_messages=2,
            tool_preview_bytes=32,
        )


def test_source_messages_validate_detached_json_and_unique_refs() -> None:
    message = {"role": "user", "content": ["nested", {"value": 1}]}
    source = SourceMessage(ref="evt-detached", message=message)
    message["content"][1]["value"] = 2
    assert source.message["content"][1]["value"] == 1

    with pytest.raises(ValidationError):
        SourceMessage(ref="evt-invalid", message={"role": "user", "bad": object()})
    with pytest.raises(ValueError, match="source message refs must be unique"):
        apply_l0((source, source))


def test_checkpoint_refs_are_stable() -> None:
    assert checkpoint_ref("run-current", 7, 3) == "checkpoint:run-current:7:3"


def _event(
    cursor: int,
    event_id: str,
    event_type: str,
    *,
    run_id: str | None,
    payload: dict[str, Any],
) -> StoredEvent:
    return StoredEvent(
        cursor,
        EventEnvelope(
            event_id=event_id,
            type=event_type,
            session_id="ses-current",
            run_id=run_id,
            sequence=cursor,
            payload=payload,
            occurred_at=datetime(2026, 7, 20, tzinfo=UTC),
        ),
    )


def test_extract_sources_correlates_checkpoint_and_protects_active_state() -> None:
    current_messages = [
        {"role": "user", "content": "current request"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-current",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-current",
            "name": "lookup",
            "content": '{"ok":true}',
        },
    ]
    checkpoint = RunCheckpoint(
        run_id="run-current",
        session_id="ses-current",
        checkpoint_version=7,
        turn=1,
        phase=RunCheckpointPhase.READY_FOR_MODEL,
        messages=tuple(current_messages),
    )
    events = (
        _event(
            1,
            "evt-old-user",
            "run.created",
            run_id="run-old",
            payload={"user_input": "older request"},
        ),
        _event(
            2,
            "evt-current-user",
            "run.created",
            run_id="run-current",
            payload={"user_input": "current request"},
        ),
        _event(
            3,
            "evt-current-model",
            "model.call.completed",
            run_id="run-current",
            payload={"finish_reason": "tool_calls"},
        ),
        _event(
            4,
            "evt-current-tool",
            "tool.call.completed",
            run_id="run-current",
            payload={
                "call_id": "call-current",
                "tool_name": "lookup",
                "status": "succeeded",
                "content": '{"ok":true}',
                "value": {"ok": True},
                "error": None,
            },
        ),
    )
    state = _source(
        "state:workflow:wfr-active",
        "system",
        '{"status":"running"}',
    )

    sources = extract_sources(
        events,
        checkpoint,
        protected_event_ids={"evt-old-user"},
        active_state_summaries=(state,),
    )

    assert tuple(source.ref for source in sources) == (
        "evt-old-user",
        "evt-current-user",
        "evt-current-model",
        "evt-current-tool",
        "state:workflow:wfr-active",
    )
    assert sources[0].protected
    assert all(source.current for source in sources[1:4])
    assert all(source.protected for source in sources)
    assert [
        source.model_dump(mode="json")["message"] for source in sources[1:4]
    ] == current_messages
    assert sources[-1].protected
    events[0].event.payload["user_input"] = "mutated"
    current_messages[0]["content"] = "mutated"
    assert sources[0].message["content"] == "older request"
    assert sources[1].message["content"] == "current request"
