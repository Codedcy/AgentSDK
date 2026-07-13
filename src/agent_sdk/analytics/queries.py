from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import NoReturn

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.evaluation import EvaluationResult, EvaluationVerdict
from agent_sdk.storage.base import StateStore, StoredEvent
from agent_sdk.tools import ToolResult, ToolResultStatus

from .models import AnalyticsResult

_PAGE_SIZE = 100
_STABLE_SCAN_ATTEMPTS = 4


class _StoreFailure(Enum):
    FAILED = "failed"


@dataclass
class _Counts:
    known: int = 0
    positive: int = 0
    missing: int = 0
    evidence: list[str] = field(default_factory=list)


class AnalyticsQueries:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def success_rate(
        self,
        *,
        evaluator_id: str | None = None,
    ) -> AnalyticsResult:
        cursor, counts = await self._evaluation_counts(evaluator_id)
        return AnalyticsResult(
            metric="success_rate",
            value=(counts.positive / counts.known if counts.known else None),
            sample_count=counts.known,
            missing_count=counts.missing,
            method="explicit_evaluation_verdict",
            filters=(
                {} if evaluator_id is None else {"evaluator_id": evaluator_id}
            ),
            evidence_event_ids=tuple(counts.evidence),
            as_of_cursor=cursor,
        )

    async def tool_failures(
        self,
        *,
        tool_name: str | None = None,
    ) -> AnalyticsResult:
        cursor, counts = await self._tool_counts(tool_name)
        return self._tool_result(
            metric="tool_failures",
            value=float(counts.positive),
            cursor=cursor,
            counts=counts,
            tool_name=tool_name,
        )

    async def tool_failure_rate(
        self,
        *,
        tool_name: str | None = None,
    ) -> AnalyticsResult:
        cursor, counts = await self._tool_counts(tool_name)
        return self._tool_result(
            metric="tool_failure_rate",
            value=(counts.positive / counts.known if counts.known else None),
            cursor=cursor,
            counts=counts,
            tool_name=tool_name,
        )

    async def _evaluation_counts(
        self,
        evaluator_id: str | None,
    ) -> tuple[int, _Counts]:
        for _ in range(_STABLE_SCAN_ATTEMPTS):
            cursor = await self._cursor()
            first = _Counts()
            second = _Counts()
            await self._scan_counts(
                cursor,
                lambda stored: _count_evaluation(stored, first, evaluator_id),
            )
            await self._scan_counts(
                cursor,
                lambda stored: _count_evaluation(stored, second, evaluator_id),
            )
            if first == second:
                return cursor, second
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "analytics facts changed while being observed",
            retryable=True,
        )

    async def _tool_counts(self, tool_name: str | None) -> tuple[int, _Counts]:
        for _ in range(_STABLE_SCAN_ATTEMPTS):
            cursor = await self._cursor()
            first = _Counts()
            second = _Counts()
            await self._scan_counts(
                cursor,
                lambda stored: _count_tool(stored, first, tool_name),
            )
            await self._scan_counts(
                cursor,
                lambda stored: _count_tool(stored, second, tool_name),
            )
            if first == second:
                return cursor, second
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "analytics facts changed while being observed",
            retryable=True,
        )

    async def _scan_counts(
        self,
        cursor: int,
        visit: Callable[[StoredEvent], None],
    ) -> None:
        scanned = await _scan(self._store, cursor, visit)
        if scanned is _StoreFailure.FAILED:
            self._internal("failed to read analytics events")

    async def _cursor(self) -> int:
        cursor = await _latest_cursor(self._store)
        if isinstance(cursor, _StoreFailure):
            self._internal("failed to read analytics cursor")
        return cursor

    @staticmethod
    def _tool_result(
        *,
        metric: str,
        value: float | None,
        cursor: int,
        counts: _Counts,
        tool_name: str | None,
    ) -> AnalyticsResult:
        return AnalyticsResult(
            metric=metric,
            value=value,
            sample_count=counts.known,
            missing_count=counts.missing,
            method="terminal_tool_status",
            filters={} if tool_name is None else {"tool_name": tool_name},
            evidence_event_ids=tuple(counts.evidence),
            as_of_cursor=cursor,
        )

    @staticmethod
    def _internal(message: str) -> NoReturn:
        raise AgentSDKError(ErrorCode.INTERNAL, message, retryable=False)


async def _latest_cursor(store: StateStore) -> int | _StoreFailure:
    try:
        return await store.latest_cursor()
    except Exception:
        return _StoreFailure.FAILED


async def _scan(
    store: StateStore,
    up_to_cursor: int,
    visit: Callable[[StoredEvent], None],
) -> None | _StoreFailure:
    current = 0
    try:
        while current < up_to_cursor:
            page = await store.read_events(
                after_cursor=current,
                up_to_cursor=up_to_cursor,
                limit=_PAGE_SIZE,
            )
            if not page:
                break
            if (
                page[0].cursor <= current
                or page[-1].cursor > up_to_cursor
                or any(
                    left.cursor >= right.cursor
                    for left, right in zip(page, page[1:], strict=False)
                )
            ):
                return _StoreFailure.FAILED
            for stored in page:
                visit(stored)
            current = page[-1].cursor
            if len(page) < _PAGE_SIZE:
                break
        return None
    except Exception:
        return _StoreFailure.FAILED


def _evaluation(payload: dict[str, object]) -> EvaluationResult | _StoreFailure:
    try:
        return EvaluationResult.model_validate(payload)
    except Exception:
        return _StoreFailure.FAILED


def _tool_result(payload: dict[str, object]) -> ToolResult | _StoreFailure:
    try:
        return ToolResult.model_validate(payload)
    except Exception:
        return _StoreFailure.FAILED


def _count_evaluation(
    stored: StoredEvent,
    counts: _Counts,
    evaluator_id: str | None,
) -> None:
    event = stored.event
    if event.type != "evaluation.completed":
        return
    identity = event.payload.get("evaluator_id")
    if evaluator_id is not None and identity != evaluator_id:
        return
    counts.evidence.append(event.event_id)
    parsed = _evaluation(event.payload)
    if (
        parsed is _StoreFailure.FAILED
        or event.run_id != parsed.evaluation_id
        or event.session_id != parsed.session_id
    ):
        counts.missing += 1
        return
    if parsed.verdict is EvaluationVerdict.UNKNOWN:
        counts.missing += 1
        return
    counts.known += 1
    if parsed.verdict is EvaluationVerdict.PASS:
        counts.positive += 1


def _count_tool(
    stored: StoredEvent,
    counts: _Counts,
    tool_name: str | None,
) -> None:
    event = stored.event
    if event.type != "tool.call.completed":
        return
    identity = event.payload.get("tool_name")
    if tool_name is not None and identity != tool_name:
        return
    counts.evidence.append(event.event_id)
    parsed = _tool_result(event.payload)
    if parsed is _StoreFailure.FAILED:
        counts.missing += 1
        return
    counts.known += 1
    if parsed.status is not ToolResultStatus.SUCCEEDED:
        counts.positive += 1
