from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NoReturn

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.runtime.models import RunSnapshot, RunStatus, run_created_event_matches
from agent_sdk.storage.base import StateStore, StoredEvent
from agent_sdk.storage.validation import validate_event_page, validate_latest_cursor
from agent_sdk.workflow.models import WorkflowRunSnapshot

from .attribution import project_attribution
from .models import AttributionSummary, ObservedEvent, TraceTimeline
from .stages import project_stages

_STABLE_READ_ATTEMPTS = 4
_PAGE_SIZE = 100
_RUN_SNAPSHOT_TRANSITIONS = frozenset(
    {
        "run.started",
        "run.completed",
        "run.failed",
        "run.interrupted",
        "run.recovery.started",
        "permission.requested",
        "permission.resolved",
    }
)
_WORKFLOW_SNAPSHOT_TRANSITIONS = frozenset(
    {
        "workflow.started",
        "workflow.node.started",
        "workflow.node.completed",
        "workflow.node.failed",
        "workflow.completed",
        "workflow.failed",
    }
)


class _LoadFailure(Enum):
    FAILED = "failed"


@dataclass(frozen=True)
class _LoadedTrace:
    root_run: RunSnapshot | None
    timeline: TraceTimeline
    events: tuple[ObservedEvent, ...]


class TraceService:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def timeline(self, root_id: str) -> TraceTimeline:
        return (await self._load(root_id)).timeline

    async def attribution(self, run_id: str) -> AttributionSummary:
        loaded = await self._load(run_id)
        root = loaded.root_run
        if root is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "attribution root is not a run",
                retryable=False,
            )
        if root.status not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
        }:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not terminal",
                retryable=False,
            )
        return project_attribution(
            root_run_id=root.run_id,
            terminal_status=root.status,
            timeline=loaded.timeline,
            events=loaded.events,
        )

    async def _load(self, root_id: str) -> _LoadedTrace:
        for _ in range(_STABLE_READ_ATTEMPTS):
            run = await self._run(root_id)
            workflow: WorkflowRunSnapshot | None = None
            if run is None:
                workflow = await self._workflow(root_id)
                if workflow is None:
                    raise AgentSDKError(
                        ErrorCode.NOT_FOUND,
                        "trace root not found",
                        retryable=False,
                    )
            cursor = await self._cursor()
            stored = await self._read_through(cursor)
            try:
                run_snapshots, selected = await self._select(
                    stored,
                    root_run=run,
                    root_workflow=workflow,
                    cursor=cursor,
                )
            except Exception:
                self._internal()
            if not await self._stable(
                run,
                workflow,
                run_snapshots,
                cursor=cursor,
            ):
                continue
            events = tuple(selected)
            return _LoadedTrace(
                root_run=run,
                timeline=TraceTimeline(
                    root_id=root_id,
                    stages=project_stages(events),
                    as_of_cursor=cursor,
                ),
                events=events,
            )
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "trace changed while it was being observed",
            retryable=True,
        )

    async def _select(
        self,
        stored: list[StoredEvent],
        *,
        root_run: RunSnapshot | None,
        root_workflow: WorkflowRunSnapshot | None,
        cursor: int,
    ) -> tuple[dict[str, RunSnapshot], list[ObservedEvent]]:
        session_id = (
            root_run.session_id if root_run is not None else root_workflow.session_id  # type: ignore[union-attr]
        )
        run_ids = {root_run.run_id} if root_run is not None else set()
        workflow_id = root_workflow.workflow_run_id if root_workflow is not None else None
        changed = True
        while changed:
            changed = False
            for item in stored:
                event = item.event
                if item.cursor > cursor or event.type != "run.created":
                    continue
                run_id = event.run_id
                parent_id = event.payload.get("parent_run_id")
                bound_workflow = event.payload.get("workflow_run_id")
                if not isinstance(run_id, str):
                    raise ValueError
                if (
                    (isinstance(parent_id, str) and parent_id in run_ids)
                    or (workflow_id is not None and bound_workflow == workflow_id)
                ) and run_id not in run_ids:
                    run_ids.add(run_id)
                    changed = True
            if workflow_id is not None:
                for item in stored:
                    if item.cursor > cursor or not item.event.type.startswith("workflow.node."):
                        continue
                    if item.event.run_id != workflow_id:
                        continue
                    node_run = item.event.payload.get("run_id")
                    if isinstance(node_run, str) and node_run not in run_ids:
                        run_ids.add(node_run)
                        changed = True

        snapshots: dict[str, RunSnapshot] = {}
        for run_id in run_ids:
            snapshot = await self._run(run_id)
            if snapshot is None or snapshot.session_id != session_id:
                raise ValueError
            if root_run is not None and run_id != root_run.run_id:
                ancestor = snapshot.parent_run_id
                if ancestor not in run_ids:
                    raise ValueError
            creation = [
                item
                for item in stored
                if item.cursor <= cursor
                and item.event.type == "run.created"
                and item.event.run_id == run_id
                and item.event.session_id == session_id
            ]
            if len(creation) != 1 or not run_created_event_matches(
                snapshot,
                dict(creation[0].event.payload),
                schema_version=creation[0].event.schema_version,
            ):
                raise ValueError
            snapshots[run_id] = snapshot

        context_view_ids = {
            context_view_id
            for item in stored
            if item.cursor <= cursor and item.event.run_id in run_ids
            for context_view_id in (item.event.payload.get("context_view_id"),)
            if isinstance(context_view_id, str)
        }
        prompt_manifest_ids = {
            prompt_manifest_id
            for item in stored
            if item.cursor <= cursor and item.event.run_id in run_ids
            for prompt_manifest_id in (item.event.payload.get("prompt_manifest_id"),)
            if isinstance(prompt_manifest_id, str)
        }

        selected: list[ObservedEvent] = []
        for item in stored:
            if item.cursor > cursor or item.event.session_id != session_id:
                continue
            payload = item.event.payload
            related = item.event.run_id in run_ids
            if workflow_id is not None and item.event.run_id == workflow_id:
                related = True
            if payload.get("subject_run_id") in run_ids:
                related = True
            if payload.get("sender_run_id") in run_ids or payload.get("recipient_run_id") in run_ids:
                related = True
            if (
                item.event.type.startswith("workflow.node.")
                and payload.get("run_id") in run_ids
            ):
                related = True
            if (
                item.event.type == "context.view.created"
                and item.event.run_id in context_view_ids
            ):
                related = True
            if (
                item.event.type == "prompt.manifest.created"
                and item.event.run_id in prompt_manifest_ids
            ):
                related = True
            if related:
                selected.append(ObservedEvent(cursor=item.cursor, event=item.event))
        return snapshots, selected

    async def _stable(
        self,
        run: RunSnapshot | None,
        workflow: WorkflowRunSnapshot | None,
        snapshots: dict[str, RunSnapshot],
        *,
        cursor: int,
    ) -> bool:
        if not await self._snapshots_match(run, workflow, snapshots):
            return False
        tail_cursor = await self._cursor()
        tail = await self._read_through(tail_cursor, after_cursor=cursor)
        run_ids = set(snapshots)
        workflow_id = None if workflow is None else workflow.workflow_run_id
        if run is not None:
            session_id = run.session_id
        else:
            assert workflow is not None
            session_id = workflow.session_id
        for item in tail:
            event = item.event
            if event.type == "run.created":
                if event.run_id in run_ids:
                    self._internal()
                parent_id = event.payload.get("parent_run_id")
                bound_workflow = event.payload.get("workflow_run_id")
                relevant = (
                    isinstance(parent_id, str) and parent_id in run_ids
                ) or (workflow_id is not None and bound_workflow == workflow_id)
                if not relevant:
                    continue
                if (
                    event.schema_version not in {1, 2, 3}
                    or event.session_id != session_id
                    or not isinstance(event.run_id, str)
                ):
                    self._internal()
                return False
            event_run_id = event.run_id
            if (
                isinstance(event_run_id, str)
                and event_run_id in run_ids
                and event.type in _RUN_SNAPSHOT_TRANSITIONS
            ):
                allowed_versions = (
                    {1, 2}
                    if event.type in {"permission.requested", "permission.resolved"}
                    else {1}
                )
                if (
                    event.schema_version not in allowed_versions
                    or event.session_id != snapshots[event_run_id].session_id
                ):
                    self._internal()
                return False
            if (
                workflow_id is not None
                and event.run_id == workflow_id
                and event.type in _WORKFLOW_SNAPSHOT_TRANSITIONS
            ):
                if event.schema_version != 1 or event.session_id != session_id:
                    self._internal()
                return False
        return await self._snapshots_match(run, workflow, snapshots)

    async def _snapshots_match(
        self,
        run: RunSnapshot | None,
        workflow: WorkflowRunSnapshot | None,
        snapshots: dict[str, RunSnapshot],
    ) -> bool:
        if run is not None and await self._run(run.run_id) != run:
            return False
        if workflow is not None and await self._workflow(workflow.workflow_run_id) != workflow:
            return False
        for run_id, snapshot in snapshots.items():
            if await self._run(run_id) != snapshot:
                return False
        return True

    async def _run(self, run_id: str) -> RunSnapshot | None:
        try:
            data = await self._store.get_snapshot("run", run_id)
            return None if data is None else RunSnapshot.model_validate(data)
        except Exception:
            self._internal()

    async def _workflow(self, workflow_id: str) -> WorkflowRunSnapshot | None:
        try:
            data = await self._store.get_snapshot("workflow", workflow_id)
            return None if data is None else WorkflowRunSnapshot.model_validate(data)
        except Exception:
            self._internal()

    async def _cursor(self) -> int:
        try:
            return validate_latest_cursor(await self._store.latest_cursor())
        except Exception:
            self._internal()

    async def _read_through(
        self,
        up_to_cursor: int,
        *,
        after_cursor: int = 0,
    ) -> list[StoredEvent]:
        events: list[StoredEvent] = []
        current = after_cursor
        while current < up_to_cursor:
            try:
                page = validate_event_page(
                    await self._store.read_events(
                        after_cursor=current,
                        up_to_cursor=up_to_cursor,
                        limit=_PAGE_SIZE,
                    ),
                    after_cursor=current,
                    up_to_cursor=up_to_cursor,
                    limit=_PAGE_SIZE,
                )
            except Exception:
                self._internal()
            if not page:
                break
            events.extend(page)
            current = page[-1].cursor
        return events

    @staticmethod
    def _internal() -> NoReturn:
        raise AgentSDKError(
            ErrorCode.INTERNAL,
            "failed to load trace timeline",
            retryable=False,
        ) from None
