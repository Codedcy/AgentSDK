from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from typing import Any, Literal, cast

from agent_sdk.context.models import SourceMessage
from agent_sdk.runtime.reconciliation import RunCheckpoint
from agent_sdk.storage.base import StoredEvent

type _Role = Literal["system", "user", "assistant", "tool"]


def checkpoint_ref(run_id: str, checkpoint_version: int, index: int) -> str:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a nonempty string")
    if (
        isinstance(checkpoint_version, bool)
        or not isinstance(checkpoint_version, int)
        or checkpoint_version < 1
    ):
        raise ValueError("checkpoint_version must be a positive integer")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("checkpoint message index must be a non-negative integer")
    return f"checkpoint:{run_id}:{checkpoint_version}:{index}"


def extract_sources(
    events: Iterable[StoredEvent],
    checkpoint: RunCheckpoint,
    *,
    protected_event_ids: Iterable[str] = (),
    unresolved_event_ids: Iterable[str] = (),
    active_state_summaries: Iterable[SourceMessage] = (),
) -> tuple[SourceMessage, ...]:
    ordered = tuple(sorted(events, key=lambda item: item.cursor))
    protected_refs = set(protected_event_ids) | set(unresolved_event_ids)
    historical: list[SourceMessage] = []
    current_events: list[StoredEvent] = []
    for stored in ordered:
        if stored.event.run_id == checkpoint.run_id:
            current_events.append(stored)
            continue
        message = _historical_message(stored)
        if message is None:
            continue
        historical.append(
            SourceMessage(
                ref=stored.event.event_id,
                role=cast(_Role, message.get("role")),
                message=message,
                event_type=stored.event.type,
                protected=stored.event.event_id in protected_refs,
            )
        )

    dumped_messages = checkpoint.model_dump(mode="json")["messages"]
    checkpoint_messages = cast(tuple[dict[str, Any], ...], tuple(dumped_messages))
    correlated = _correlated_checkpoint_refs(
        tuple(current_events),
        checkpoint_messages,
    )
    latest_user = max(
        (
            index
            for index, message in enumerate(checkpoint_messages)
            if message.get("role") == "user"
        ),
        default=-1,
    )
    current: list[SourceMessage] = []
    for index, message in enumerate(checkpoint_messages):
        ref, event_type = correlated.get(
            index,
            (
                checkpoint_ref(
                    checkpoint.run_id,
                    checkpoint.checkpoint_version,
                    index,
                ),
                "checkpoint.message",
            ),
        )
        role = message.get("role")
        protocol_message = role == "tool" or (
            role == "assistant" and "tool_calls" in message
        )
        current.append(
            SourceMessage(
                ref=ref,
                role=cast(_Role, role),
                message=message,
                event_type=event_type,
                protected=(
                    index == latest_user
                    or protocol_message
                    or ref in protected_refs
                ),
                current=True,
            )
        )

    states = tuple(
        state.model_copy(update={"protected": True})
        for state in active_state_summaries
    )
    result = (*historical, *current, *states)
    refs = tuple(item.ref for item in result)
    if len(refs) != len(set(refs)):
        raise ValueError("source message refs must be unique")
    if protected_refs - set(refs):
        raise ValueError("protected context source not found")
    return result


def _historical_message(stored: StoredEvent) -> Mapping[str, Any] | None:
    event = stored.event
    payload = event.payload
    if event.type == "run.created":
        content = payload.get("user_input")
        return (
            {"role": "user", "content": content}
            if isinstance(content, str)
            else None
        )
    if event.type == "model.text.delta":
        content = payload.get("text")
        return (
            {"role": "assistant", "content": content}
            if isinstance(content, str)
            else None
        )
    if event.type == "tool.call.completed":
        content = payload.get("content")
        call_id = payload.get("call_id")
        name = payload.get("tool_name")
        if (
            isinstance(content, str)
            and isinstance(call_id, str)
            and isinstance(name, str)
        ):
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": content,
            }
        return None
    if event.type == "context.message.appended":
        role = payload.get("role")
        if isinstance(role, str) and "content" in payload:
            return payload
    return None


def _correlated_checkpoint_refs(
    current_events: tuple[StoredEvent, ...],
    checkpoint_messages: tuple[dict[str, Any], ...],
) -> dict[int, tuple[str, str]]:
    run_created = next(
        (
            stored.event
            for stored in current_events
            if stored.event.type == "run.created"
        ),
        None,
    )
    model_completed = iter(
        (stored.event.event_id, stored.event.type)
        for stored in current_events
        if stored.event.type == "model.call.completed"
    )
    tool_completed: dict[str, deque[tuple[str, str]]] = defaultdict(deque)
    for stored in current_events:
        call_id = stored.event.payload.get("call_id")
        if stored.event.type == "tool.call.completed" and isinstance(call_id, str):
            tool_completed[call_id].append(
                (stored.event.event_id, stored.event.type)
            )
    refs: dict[int, tuple[str, str]] = {}
    user_correlated = False
    for index, message in enumerate(checkpoint_messages):
        role = message.get("role")
        if (
            role == "user"
            and not user_correlated
            and run_created is not None
            and message.get("content") == run_created.payload.get("user_input")
        ):
            refs[index] = (run_created.event_id, run_created.type)
            user_correlated = True
        elif role == "assistant":
            correlated = next(model_completed, None)
            if correlated is not None:
                refs[index] = correlated
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and tool_completed[call_id]:
                refs[index] = tool_completed[call_id].popleft()
    return refs
