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
    event_type = {
        "system": "context.message.appended",
        "user": "run.created",
        "assistant": "model.call.completed",
        "tool": "tool.call.completed",
    }[role]
    return SourceMessage(
        ref=ref,
        role=role,
        message={"role": role, "content": content, **message_fields},
        event_type=event_type,
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


@pytest.mark.parametrize(
    ("first", "second", "duplicate"),
    [
        ("alpha", '"alpha"', False),
        ('{"a":1,"a":2}', '{"a":2}', False),
        ('{"b":2,"a":1}', '{"a":1,"b":2}', True),
        ("[1,2]", "[1,2]", True),
        ("[1,2]", "[2,1]", False),
        ("1", "1.0", False),
        ("true", "false", False),
        ("NaN", "NaN", True),
        ("Infinity", '"Infinity"', False),
    ],
)
def test_l1_uses_collision_safe_json_and_raw_hash_domains(
    first: str,
    second: str,
    duplicate: bool,
) -> None:
    sources = (
        _source("evt-first", "tool", first),
        _source("evt-second", "tool", second),
    )

    rendered = apply_l1(sources, tool_preview_bytes=64)

    if duplicate:
        assert rendered.items[1].message["content"] == "[duplicate:evt-first]"
        assert rendered.transformations == ("dedupe:evt-second",)
    else:
        assert rendered.items == sources
        assert rendered.transformations == ()


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


def test_l2_layers_l1_preview_over_a_recent_unprotected_tool() -> None:
    sources = (
        _source("evt-old-user", "user", "older request"),
        _source("evt-recent-tool", "tool", "数据🙂" * 300),
    )

    l1 = apply_l1(sources, tool_preview_bytes=64)
    l2 = apply_l2(sources, recent_messages=1, tool_preview_bytes=64)

    recent_l1 = l1.items[1].message["content"]
    recent_l2 = l2.items[1].message["content"]
    assert "[source:evt-recent-tool]" in recent_l2
    assert len(recent_l2.encode("utf-8")) <= 64 + 96
    assert len(recent_l2.encode("utf-8")) <= len(recent_l1.encode("utf-8"))
    assert l2.transformations == (
        "tool_preview:evt-recent-tool",
        "outcome:evt-old-user",
    )


@pytest.mark.parametrize(
    "ref",
    [
        "r" * 64,
        ("界" * 21) + "r",
    ],
)
def test_l1_and_l2_bound_complete_preview_for_maximum_byte_ref(ref: str) -> None:
    sources = (
        _source("evt-old-user", "user", "older request"),
        _source(ref, "tool", "数据🙂" * 300),
    )

    rendered = (
        apply_l1(sources, tool_preview_bytes=64),
        apply_l2(sources, recent_messages=1, tool_preview_bytes=64),
    )

    assert len(ref.encode("utf-8")) == 64
    for result in rendered:
        preview = result.items[1].message["content"]
        assert f"[source:{ref}]" in preview
        assert len(preview.encode("utf-8")) <= 64 + 96


@pytest.mark.parametrize(
    "ref",
    [
        "r" * 65,
        ("界" * 21) + "rr",
    ],
)
def test_source_message_rejects_ref_above_64_utf8_bytes(ref: str) -> None:
    assert len(ref.encode("utf-8")) == 65
    with pytest.raises(ValidationError, match="ref must not exceed 64 UTF-8 bytes"):
        _source(ref, "tool", "result")


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
    message = {
        "role": "user",
        "content": "nested",
        "metadata": [{"value": 1}],
    }
    source = SourceMessage(
        ref="evt-detached",
        role="user",
        message=message,
        event_type="run.created",
    )
    message["metadata"][0]["value"] = 2
    assert source.message["metadata"][0]["value"] == 1

    with pytest.raises(ValidationError):
        SourceMessage(
            ref="evt-invalid",
            role="user",
            message={"role": "user", "content": "valid", "bad": object()},
            event_type="run.created",
        )
    with pytest.raises(ValueError, match="source message refs must be unique"):
        apply_l0((source, source))


def _source_errors(**updates: Any) -> list[dict[str, Any]]:
    values: dict[str, Any] = {
        "ref": "evt-valid",
        "role": "user",
        "message": {"role": "user", "content": "valid"},
        "event_type": "run.created",
    }
    values.update(updates)
    if values["role"] is None:
        del values["role"]
    with pytest.raises(ValidationError) as raised:
        SourceMessage(**values)
    return raised.value.errors()


def test_source_message_exposes_strict_bounded_runtime_interface() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ],
    }
    source = SourceMessage(
        ref="evt-valid",
        role="assistant",
        message=message,
        event_type="model.call.completed",
        protected=False,
        current=True,
    )
    message["tool_calls"][0]["function"]["name"] = "mutated"
    assert source.role == "assistant"
    assert source.event_type == "model.call.completed"
    assert source.message["tool_calls"][0]["function"]["name"] == "lookup"
    assert source.model_dump(mode="json")["message"]["tool_calls"][0]["id"] == "call-1"


def test_source_message_rejects_missing_unsupported_or_mismatched_roles() -> None:
    missing = _source_errors(role=None)
    unsupported = _source_errors(
        role="invalid",
        message={"role": "invalid", "content": "bad"},
    )
    mismatch = _source_errors(
        role="user",
        message={"role": "assistant", "content": "bad"},
    )

    assert any(error["loc"] == ("role",) for error in missing)
    assert any(error["type"] == "literal_error" for error in unsupported)
    assert "message role must match source role" in mismatch[0]["msg"]


def test_source_message_rejects_invalid_provider_content_and_coerced_flags() -> None:
    numeric_tool = _source_errors(
        role="tool",
        message={"role": "tool", "content": 7},
        event_type="tool.call.completed",
    )
    coerced_protected = _source_errors(protected=1)
    coerced_current = _source_errors(current=0)

    assert "tool content must be a string" in numeric_tool[0]["msg"]
    assert any(
        error["loc"] == ("protected",) and error["type"] == "bool_type"
        for error in coerced_protected
    )
    assert any(
        error["loc"] == ("current",) and error["type"] == "bool_type"
        for error in coerced_current
    )


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "not-a-call",
        {},
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup"},
        },
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}", "extra": True},
        },
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
            "extra": True,
        },
        {
            "id": "call-1",
            "type": "other",
            "function": {"name": "lookup", "arguments": "{}"},
        },
        {
            "id": "",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        },
        {
            "id": 1,
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        },
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "", "arguments": "{}"},
        },
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": 1, "arguments": "{}"},
        },
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": {}},
        },
    ],
)
def test_source_message_rejects_invalid_tool_call_protocol_entries(
    entry: Any,
) -> None:
    with pytest.raises(ValidationError, match="tool_calls"):
        SourceMessage(
            ref="evt-invalid-call",
            role="assistant",
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [entry],
            },
            event_type="model.call.completed",
        )


def test_source_message_validates_tool_calls_even_with_text_content() -> None:
    with pytest.raises(ValidationError, match="tool_calls"):
        SourceMessage(
            ref="evt-empty-calls",
            role="assistant",
            message={
                "role": "assistant",
                "content": "text",
                "tool_calls": [],
            },
            event_type="model.call.completed",
        )


def test_source_message_rejects_identity_and_json_resource_overflows() -> None:
    long_ref = _source_errors(ref="r" * 513)
    long_event_type = _source_errors(event_type="e" * 129)
    oversized = _source_errors(
        message={"role": "user", "content": "x" * (256 * 1024)}
    )
    too_many = _source_errors(
        message={
            "role": "assistant",
            "content": "bounded",
            "data": {str(index): 0 for index in range(20_001)},
        },
        role="assistant",
    )

    assert any(error["loc"] == ("ref",) for error in long_ref)
    assert any(error["loc"] == ("event_type",) for error in long_event_type)
    assert "serialized message exceeds 262144 bytes" in oversized[0]["msg"]
    assert "message exceeds 20000 container entries" in too_many[0]["msg"]


def test_source_message_normalizes_deep_cyclic_and_non_json_validation() -> None:
    deep: list[Any] = []
    cursor = deep
    for _ in range(33):
        child: list[Any] = []
        cursor.append(child)
        cursor = child
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic

    deep_errors = _source_errors(
        role="assistant",
        message={"role": "assistant", "content": "bounded", "data": deep},
    )
    cyclic_errors = _source_errors(
        role="assistant",
        message={"role": "assistant", "content": "bounded", "data": cyclic},
    )
    key_errors = _source_errors(
        role="assistant",
        message={"role": "assistant", "content": "bounded", 1: "bad"},
    )
    number_errors = _source_errors(
        role="assistant",
        message={"role": "assistant", "content": "bounded", "score": float("inf")},
    )

    assert "message nesting exceeds 32" in deep_errors[0]["msg"]
    assert "message contains a cycle" in cyclic_errors[0]["msg"]
    assert "JSON object keys must be strings" in key_errors[0]["msg"]
    assert "JSON numbers must be finite" in number_errors[0]["msg"]


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


def _assistant_call(call_id: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ],
    }


def _tool_message(call_id: str, content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "lookup",
        "content": content,
    }


def _tool_event(cursor: int, event_id: str, call_id: str, content: str) -> StoredEvent:
    return _event(
        cursor,
        event_id,
        "tool.call.completed",
        run_id="run-current",
        payload={
            "call_id": call_id,
            "tool_name": "lookup",
            "status": "succeeded",
            "content": content,
            "value": {"content": content},
            "error": None,
        },
    )


def test_extract_sources_consumes_repeated_tool_call_ids_in_event_order() -> None:
    messages = (
        {"role": "user", "content": "current request"},
        _assistant_call("call-reused"),
        _tool_message("call-reused", "first"),
        _assistant_call("call-reused"),
        _tool_message("call-reused", "second"),
    )
    checkpoint = RunCheckpoint(
        run_id="run-current",
        session_id="ses-current",
        checkpoint_version=5,
        turn=2,
        phase=RunCheckpointPhase.READY_FOR_MODEL,
        messages=messages,
    )
    events = (
        _event(
            1,
            "evt-user",
            "run.created",
            run_id="run-current",
            payload={"user_input": "current request"},
        ),
        _event(
            2,
            "evt-model-1",
            "model.call.completed",
            run_id="run-current",
            payload={"finish_reason": "tool_calls"},
        ),
        _tool_event(3, "evt-tool-1", "call-reused", "first"),
        _event(
            4,
            "evt-model-2",
            "model.call.completed",
            run_id="run-current",
            payload={"finish_reason": "tool_calls"},
        ),
        _tool_event(5, "evt-tool-2", "call-reused", "second"),
    )

    sources = extract_sources(events, checkpoint)

    assert tuple(source.ref for source in sources) == (
        "evt-user",
        "evt-model-1",
        "evt-tool-1",
        "evt-model-2",
        "evt-tool-2",
    )
    assert tuple(source.event_type for source in sources) == (
        "run.created",
        "model.call.completed",
        "tool.call.completed",
        "model.call.completed",
        "tool.call.completed",
    )


def test_extract_sources_handles_interleaved_and_unmatched_tool_call_ids() -> None:
    messages = (
        {"role": "user", "content": "current request"},
        _assistant_call("call-a"),
        _tool_message("call-a", "a-first"),
        _assistant_call("call-b"),
        _tool_message("call-b", "b"),
        _assistant_call("call-a"),
        _tool_message("call-a", "a-second"),
        _assistant_call("call-missing"),
        _tool_message("call-missing", "synthetic"),
    )
    checkpoint = RunCheckpoint(
        run_id="run-current",
        session_id="ses-current",
        checkpoint_version=9,
        turn=4,
        phase=RunCheckpointPhase.READY_FOR_MODEL,
        messages=messages,
    )
    events = (
        _event(
            1,
            "evt-user",
            "run.created",
            run_id="run-current",
            payload={"user_input": "current request"},
        ),
        _event(
            2,
            "evt-model-1",
            "model.call.completed",
            run_id="run-current",
            payload={"finish_reason": "tool_calls"},
        ),
        _tool_event(3, "evt-tool-a1", "call-a", "a-first"),
        _event(
            4,
            "evt-model-2",
            "model.call.completed",
            run_id="run-current",
            payload={"finish_reason": "tool_calls"},
        ),
        _tool_event(5, "evt-tool-b", "call-b", "b"),
        _event(
            6,
            "evt-model-3",
            "model.call.completed",
            run_id="run-current",
            payload={"finish_reason": "tool_calls"},
        ),
        _tool_event(7, "evt-tool-a2", "call-a", "a-second"),
        _event(
            8,
            "evt-model-4",
            "model.call.completed",
            run_id="run-current",
            payload={"finish_reason": "tool_calls"},
        ),
    )

    sources = extract_sources(events, checkpoint)

    assert tuple(source.ref for source in sources) == (
        "evt-user",
        "evt-model-1",
        "evt-tool-a1",
        "evt-model-2",
        "evt-tool-b",
        "evt-model-3",
        "evt-tool-a2",
        "evt-model-4",
        "checkpoint:run-current:9:8",
    )
    assert len({source.ref for source in sources}) == len(sources)
    assert sources[-1].event_type == "checkpoint.message"
