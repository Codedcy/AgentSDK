from __future__ import annotations

from enum import Enum
from typing import Any, NoReturn

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.runtime.models import RunSnapshot
from agent_sdk.storage.base import StateStore, StoredEvent
from agent_sdk.storage.validation import validate_event_page, validate_latest_cursor

from .models import (
    EventFilter,
    EventQueryResult,
    ExecutionTree,
    ExecutionTreeNode,
    ObservedEvent,
    ObservedRun,
    RunTimeline,
)

_STABLE_READ_ATTEMPTS = 4
_PAGE_SIZE = 100
_RUN_SNAPSHOT_TRANSITIONS = frozenset(
    {
        "run.started",
        "run.completed",
        "run.failed",
        "permission.requested",
        "permission.resolved",
    }
)


class _InvalidParent(Enum):
    INVALID = "invalid"


class _ReadFailure(Enum):
    FAILED = "failed"


class _TreeTailStatus(Enum):
    STABLE = "stable"
    CHANGED = "changed"
    INVALID = "invalid"


class _TreeAssemblyFailure(Enum):
    INVALID = "invalid"
    MISSING_SELECTED = "missing_selected"


class QueryService:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def get_run(self, run_id: str) -> ObservedRun:
        saw_transition = False
        for _ in range(_STABLE_READ_ATTEMPTS):
            before = await self._load_run(run_id)
            cursor = await self._latest_cursor()
            after = await self._load_run(run_id)
            if before == after:
                return ObservedRun(snapshot=after, as_of_cursor=cursor)
            saw_transition = True
        if saw_transition:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "run changed while it was being observed",
                retryable=True,
            )
        raise AssertionError("unreachable")

    async def timeline(self, run_id: str) -> RunTimeline:
        for _ in range(_STABLE_READ_ATTEMPTS):
            before = await self._load_run(run_id)
            cursor = await self._latest_cursor()
            events = _timeline_events(
                await self._read_through(up_to_cursor=cursor),
                run=before,
                up_to_cursor=cursor,
            )
            if isinstance(events, _ReadFailure):
                self._internal("failed to load run timeline")
            after = await self._load_run(run_id)
            if before == after:
                return RunTimeline(
                    run_id=run_id,
                    events=events,
                    as_of_cursor=cursor,
                )
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "run changed while its timeline was being observed",
            retryable=True,
        )

    async def query_events(
        self,
        filters: EventFilter | None = None,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> EventQueryResult:
        if after_cursor < 0:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "event cursor must not be negative",
                retryable=False,
            )
        if not 1 <= limit <= 1000:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "event query limit must be between 1 and 1000",
                retryable=False,
            )
        selected = filters or EventFilter()
        cursor = await self._latest_cursor()
        if after_cursor > cursor:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "event cursor is ahead of durable high-water",
                retryable=False,
            )
        stored_events = await self._read_events(
            after_cursor=after_cursor,
            up_to_cursor=cursor,
            limit=limit,
        )
        events = tuple(
            self._observed(stored)
            for stored in stored_events
            if stored.cursor <= cursor and _matches(stored, selected)
        )
        return EventQueryResult(
            events=events,
            next_cursor=(stored_events[-1].cursor if stored_events else cursor),
            as_of_cursor=cursor,
        )

    async def execution_tree(self, root_run_id: str) -> ExecutionTree:
        for _ in range(_STABLE_READ_ATTEMPTS):
            root = await self._load_run(root_run_id)
            cursor = await self._latest_cursor()
            nodes = await self._assemble_tree(
                root,
                await self._read_through(up_to_cursor=cursor),
                cursor,
            )
            if nodes is _TreeAssemblyFailure.MISSING_SELECTED:
                current_root = await _stored_run(self._store, root_run_id)
                if isinstance(current_root, _ReadFailure):
                    self._internal("failed to load execution tree")
                if current_root is None:
                    raise AgentSDKError(
                        ErrorCode.NOT_FOUND,
                        "run not found",
                        retryable=False,
                    )
                self._internal("failed to load execution tree")
            if nodes is _TreeAssemblyFailure.INVALID:
                self._internal("failed to load execution tree")
            after = await self._load_run(root_run_id)
            if root == after and await self._tree_is_stable(root, nodes, cursor):
                return ExecutionTree(
                    root_run_id=root_run_id,
                    nodes=nodes,
                    as_of_cursor=cursor,
                )
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "execution tree changed while it was being observed",
            retryable=True,
        )

    async def _assemble_tree(
        self,
        root: RunSnapshot,
        stored_events: list[StoredEvent],
        cursor: int,
    ) -> tuple[ExecutionTreeNode, ...] | _TreeAssemblyFailure:
        try:
            return await self._assemble_tree_unchecked(root, stored_events, cursor)
        except AgentSDKError as error:
            if error.code is ErrorCode.NOT_FOUND:
                return _TreeAssemblyFailure.MISSING_SELECTED
            return _TreeAssemblyFailure.INVALID
        except Exception:
            return _TreeAssemblyFailure.INVALID

    async def _assemble_tree_unchecked(
        self,
        root: RunSnapshot,
        stored_events: list[StoredEvent],
        cursor: int,
    ) -> tuple[ExecutionTreeNode, ...]:
        created = [
            stored
            for stored in stored_events
            if stored.cursor <= cursor
            and stored.event.type == "run.created"
            and stored.event.session_id == root.session_id
        ]
        descendants = {root.run_id}
        selected_ids: set[str] = set()
        selected: list[tuple[StoredEvent, RunSnapshot]] = []
        pending = created
        while pending:
            progressed = False
            remaining: list[StoredEvent] = []
            for stored in pending:
                initial = _run_snapshot(stored.event.payload)
                if isinstance(initial, _ReadFailure):
                    self._internal("failed to load execution tree")
                if initial.run_id in descendants:
                    if stored.event.schema_version != 1:
                        self._internal("failed to load execution tree")
                    if initial.run_id in selected_ids:
                        self._internal("failed to load execution tree")
                    selected_ids.add(initial.run_id)
                    selected.append((stored, initial))
                    progressed = True
                elif initial.parent_run_id in descendants:
                    if stored.event.schema_version != 1:
                        self._internal("failed to load execution tree")
                    if initial.session_id != root.session_id:
                        self._internal("failed to load execution tree")
                    descendants.add(initial.run_id)
                    selected_ids.add(initial.run_id)
                    selected.append((stored, initial))
                    progressed = True
                else:
                    remaining.append(stored)
            if not progressed:
                break
            pending = remaining
        if any(
            stored.cursor <= cursor
            and stored.event.run_id in descendants
            and stored.event.session_id != root.session_id
            for stored in stored_events
        ):
            self._internal("failed to load execution tree")
        by_id: dict[str, ExecutionTreeNode] = {}
        for stored, initial in sorted(selected, key=lambda item: item[0].cursor):
            current = await self._load_run(initial.run_id)
            if (
                current.session_id != root.session_id
                or current.parent_run_id != initial.parent_run_id
                or stored.event.session_id != current.session_id
                or stored.event.run_id != current.run_id
                or not _same_creation_identity(initial, current)
            ):
                self._internal("failed to load execution tree")
            by_id[current.run_id] = ExecutionTreeNode(
                snapshot=current,
                parent_run_id=current.parent_run_id,
                created_cursor=stored.cursor,
            )
        if root.run_id not in by_id:
            self._internal("failed to load execution tree")
        for stored in stored_events:
            parent_claim = _parent_claim(stored.event.payload)
            if (
                stored.cursor <= cursor
                and stored.event.type == "run.created"
                and stored.event.session_id != root.session_id
            ):
                if stored.event.run_id in descendants:
                    self._internal("failed to load execution tree")
                if parent_claim is _InvalidParent.INVALID:
                    continue
                if parent_claim in descendants:
                    self._internal("failed to load execution tree")
        return tuple(by_id.values())

    async def _tree_is_stable(
        self,
        root: RunSnapshot,
        nodes: tuple[ExecutionTreeNode, ...],
        cursor: int,
    ) -> bool:
        descendants = {node.snapshot.run_id for node in nodes}
        for node in nodes:
            current = await _stored_run(self._store, node.snapshot.run_id)
            if isinstance(current, _ReadFailure):
                self._internal("failed to load execution tree")
            if current is None:
                return False
            if current != node.snapshot:
                return False
        tail_cursor = await self._latest_cursor()
        tail_status = _tree_tail_status(
            await self._read_through(
                after_cursor=cursor,
                up_to_cursor=tail_cursor,
            ),
            descendants=descendants,
            session_id=root.session_id,
        )
        if tail_status is _TreeTailStatus.INVALID:
            self._internal("failed to load execution tree")
        if tail_status is _TreeTailStatus.CHANGED:
            return False
        for node in nodes:
            current = await _stored_run(self._store, node.snapshot.run_id)
            if isinstance(current, _ReadFailure):
                self._internal("failed to load execution tree")
            if current is None:
                return False
            if (
                current != node.snapshot
                or current.session_id != root.session_id
                or current.run_id != node.snapshot.run_id
            ):
                return False
        return True

    async def _load_run(self, run_id: str) -> RunSnapshot:
        run = await _stored_run(self._store, run_id)
        if isinstance(run, _ReadFailure):
            self._internal("failed to load run")
        if run is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "run not found",
                retryable=False,
            )
        return run

    async def _latest_cursor(self) -> int:
        cursor = await _cursor(self._store)
        if isinstance(cursor, _ReadFailure):
            self._internal("failed to read event cursor")
        return cursor

    async def _read_events(
        self,
        *,
        after_cursor: int,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        events = await _events(
            self._store,
            after_cursor=after_cursor,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )
        if isinstance(events, _ReadFailure):
            self._internal("failed to read events")
        return events

    async def _read_through(
        self,
        *,
        up_to_cursor: int,
        after_cursor: int = 0,
    ) -> list[StoredEvent]:
        events: list[StoredEvent] = []
        current = after_cursor
        while current < up_to_cursor:
            page = await self._read_events(
                after_cursor=current,
                up_to_cursor=up_to_cursor,
                limit=_PAGE_SIZE,
            )
            if not page:
                break
            if (
                page[0].cursor <= current
                or any(
                    left.cursor >= right.cursor
                    for left, right in zip(page, page[1:], strict=False)
                )
                or page[-1].cursor > up_to_cursor
            ):
                self._internal("event page did not advance")
            events.extend(page)
            current = page[-1].cursor
        return events

    @staticmethod
    def _observed(stored: StoredEvent) -> ObservedEvent:
        observed = _observed_event(stored)
        if isinstance(observed, _ReadFailure):
            QueryService._internal("failed to load event")
        return observed

    @staticmethod
    def _internal(message: str) -> NoReturn:
        raise AgentSDKError(ErrorCode.INTERNAL, message, retryable=False) from None


def _matches(stored: StoredEvent, filters: EventFilter) -> bool:
    event = stored.event
    return (
        (filters.session_id is None or event.session_id == filters.session_id)
        and (filters.run_id is None or event.run_id == filters.run_id)
        and (not filters.event_types or event.type in filters.event_types)
    )


def _parent_claim(payload: dict[str, Any]) -> str | None | _InvalidParent:
    value = payload.get("parent_run_id")
    if value is None or isinstance(value, str):
        return value
    return _InvalidParent.INVALID


def _same_creation_identity(created: RunSnapshot, current: RunSnapshot) -> bool:
    return (
        created.run_id == current.run_id
        and created.session_id == current.session_id
        and created.agent_revision == current.agent_revision
        and created.user_input == current.user_input
        and created.parent_run_id == current.parent_run_id
        and created.workflow_run_id == current.workflow_run_id
        and created.workflow_node_id == current.workflow_node_id
        and created.workflow_node_execution == current.workflow_node_execution
        and created.task_envelope == current.task_envelope
    )


async def _stored_run(
    store: StateStore,
    run_id: str,
) -> RunSnapshot | None | _ReadFailure:
    try:
        data = await store.get_snapshot("run", run_id)
        if data is None:
            return None
        return RunSnapshot.model_validate(data)
    except Exception:
        return _ReadFailure.FAILED


async def _cursor(store: StateStore) -> int | _ReadFailure:
    try:
        return validate_latest_cursor(await store.latest_cursor())
    except Exception:
        return _ReadFailure.FAILED


async def _events(
    store: StateStore,
    *,
    after_cursor: int,
    up_to_cursor: int | None,
    limit: int | None,
) -> list[StoredEvent] | _ReadFailure:
    try:
        return validate_event_page(
            await store.read_events(
                after_cursor=after_cursor,
                up_to_cursor=up_to_cursor,
                limit=limit,
            ),
            after_cursor=after_cursor,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )
    except Exception:
        return _ReadFailure.FAILED


def _run_snapshot(data: dict[str, Any]) -> RunSnapshot | _ReadFailure:
    try:
        return RunSnapshot.model_validate(data)
    except Exception:
        return _ReadFailure.FAILED


def _observed_event(stored: StoredEvent) -> ObservedEvent | _ReadFailure:
    try:
        return ObservedEvent(cursor=stored.cursor, event=stored.event)
    except Exception:
        return _ReadFailure.FAILED


def _timeline_events(
    stored_events: list[StoredEvent],
    *,
    run: RunSnapshot,
    up_to_cursor: int,
) -> tuple[ObservedEvent, ...] | _ReadFailure:
    try:
        selected: list[ObservedEvent] = []
        for stored in stored_events:
            if stored.cursor > up_to_cursor or stored.event.run_id != run.run_id:
                continue
            if stored.event.session_id != run.session_id:
                return _ReadFailure.FAILED
            observed = _observed_event(stored)
            if isinstance(observed, _ReadFailure):
                return _ReadFailure.FAILED
            selected.append(observed)
        return tuple(selected)
    except Exception:
        return _ReadFailure.FAILED


def _tree_tail_status(
    stored_events: list[StoredEvent],
    *,
    descendants: set[str],
    session_id: str,
) -> _TreeTailStatus:
    try:
        for stored in stored_events:
            event = stored.event
            if event.run_id in descendants and event.session_id != session_id:
                return _TreeTailStatus.INVALID
            if event.type == "run.created":
                if event.run_id in descendants:
                    return _TreeTailStatus.INVALID
                parent_run_id = _parent_claim(event.payload)
                if parent_run_id is _InvalidParent.INVALID:
                    continue
                if parent_run_id not in descendants:
                    continue
                if event.schema_version != 1 or event.session_id != session_id:
                    return _TreeTailStatus.INVALID
                return _TreeTailStatus.CHANGED
            if (
                event.run_id in descendants
                and event.type in _RUN_SNAPSHOT_TRANSITIONS
            ):
                if event.schema_version != 1 or event.session_id != session_id:
                    return _TreeTailStatus.INVALID
                return _TreeTailStatus.CHANGED
        return _TreeTailStatus.STABLE
    except Exception:
        return _TreeTailStatus.INVALID
