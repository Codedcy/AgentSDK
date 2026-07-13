from __future__ import annotations

import json

from agent_sdk.events.models import EventEnvelope

from .base import StoredEvent


def validate_latest_cursor(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid durable event cursor")
    return value


def validate_event_page(
    value: object,
    *,
    after_cursor: int,
    up_to_cursor: int | None = None,
    limit: int | None = None,
) -> list[StoredEvent]:
    if not isinstance(value, list):
        raise ValueError("invalid event page")
    if limit is not None and len(value) > limit:
        raise ValueError("event page exceeds requested limit")

    previous_cursor = after_cursor
    validated: list[StoredEvent] = []
    for stored in value:
        if not isinstance(stored, StoredEvent):
            raise ValueError("invalid stored event")
        cursor = validate_latest_cursor(stored.cursor)
        if cursor <= previous_cursor:
            raise ValueError("event page did not advance")
        if up_to_cursor is not None and cursor > up_to_cursor:
            raise ValueError("event page exceeded high-water")
        if not isinstance(stored.event, EventEnvelope):
            raise ValueError("invalid event envelope")
        event_data = stored.event.model_dump(mode="python", warnings="error")
        event = EventEnvelope.model_validate(event_data, strict=True)
        json.dumps(
            event.payload,
            ensure_ascii=False,
            allow_nan=False,
        )
        json.dumps(
            event.model_dump(mode="json", warnings="error"),
            ensure_ascii=False,
            allow_nan=False,
        )
        validated.append(StoredEvent(cursor=cursor, event=event))
        previous_cursor = cursor
    return validated
