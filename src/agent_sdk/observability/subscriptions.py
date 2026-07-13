from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from enum import Enum

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.storage.base import StateStore, StoredEvent

from .models import EventFilter, ObservedEvent

_PAGE_SIZE = 100


class _StoreFailure(Enum):
    FAILED = "failed"


class SubscriptionService:
    def __init__(
        self,
        store: StateStore,
        *,
        poll_interval: float = 0.05,
        close_signal: asyncio.Event | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")
        self._store = store
        self._poll_interval = poll_interval
        self._close_signal = close_signal

    async def subscribe(
        self,
        *,
        filters: EventFilter | None = None,
        cursor: int = 0,
    ) -> AsyncIterator[ObservedEvent]:
        if cursor < 0:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "event cursor must not be negative",
                retryable=False,
            )
        if self._is_closing():
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "SDK is closing",
                retryable=False,
            )
        high_water = await _latest_cursor(self._store)
        if high_water is _StoreFailure.FAILED:
            if self._is_closing():
                return
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to read subscription cursor",
                retryable=False,
            )
        if cursor > high_water:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "event cursor is ahead of durable high-water",
                retryable=False,
            )

        selected = filters or EventFilter()
        current = cursor
        while True:
            if self._is_closing():
                return
            page = await _read_page(self._store, current)
            if page is _StoreFailure.FAILED:
                if self._is_closing():
                    return
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "failed to read subscribed events",
                    retryable=False,
                )
            if self._is_closing():
                return
            for stored in page:
                current = stored.cursor
                if self._is_closing():
                    return
                event = stored.event
                if _matches(event.session_id, event.run_id, event.type, selected):
                    observed = _observed(stored)
                    if observed is _StoreFailure.FAILED:
                        raise AgentSDKError(
                            ErrorCode.INTERNAL,
                            "failed to load subscribed event",
                            retryable=False,
                        )
                    yield observed
            if len(page) == _PAGE_SIZE:
                continue
            if await self._wait_for_close():
                return

    def _is_closing(self) -> bool:
        return self._close_signal is not None and self._close_signal.is_set()

    async def _wait_for_close(self) -> bool:
        if self._close_signal is None:
            await asyncio.sleep(self._poll_interval)
            return False
        try:
            async with asyncio.timeout(self._poll_interval):
                await self._close_signal.wait()
        except TimeoutError:
            return False
        return True


async def _latest_cursor(store: StateStore) -> int | _StoreFailure:
    try:
        return await store.latest_cursor()
    except Exception:
        return _StoreFailure.FAILED


async def _read_page(
    store: StateStore,
    after_cursor: int,
) -> list[StoredEvent] | _StoreFailure:
    try:
        return await store.read_events(
            after_cursor=after_cursor,
            limit=_PAGE_SIZE,
        )
    except Exception:
        return _StoreFailure.FAILED


def _observed(stored: StoredEvent) -> ObservedEvent | _StoreFailure:
    try:
        return ObservedEvent(cursor=stored.cursor, event=stored.event)
    except Exception:
        return _StoreFailure.FAILED


def _matches(
    session_id: str,
    run_id: str | None,
    event_type: str,
    filters: EventFilter,
) -> bool:
    return (
        (filters.session_id is None or session_id == filters.session_id)
        and (filters.run_id is None or run_id == filters.run_id)
        and (not filters.event_types or event_type in filters.event_types)
    )
