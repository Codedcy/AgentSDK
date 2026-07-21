from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.models.litellm_gateway import ModelRequest
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.handles import RunHandle
from agent_sdk.runtime.models import (
    RunResult,
    RunSnapshot,
    RunStatus,
    run_created_event_matches,
)
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    StateStore,
    StoredEvent,
)
from agent_sdk.subagents.models import (
    ChildLimits,
    ChildProgress,
    ChildResult,
    ChildWaitResult,
    TaskEnvelope,
)
from agent_sdk.subagents.service import SubagentService
from agent_sdk.tools.models import ToolSpec
from agent_sdk.tools.registry import ToolRegistry
from agent_sdk.permissions.policy import PolicyEngine


@dataclass(frozen=True)
class _DurableRun:
    snapshot: RunSnapshot
    created: StoredEvent
    raw_data: dict[str, object]


class ChildCoordinator:
    def __init__(
        self,
        store: StateStore,
        commands: RuntimeCommands,
        engine: RunEngine,
        agents: AgentRegistry,
        *,
        tools: ToolRegistry | None = None,
        tool_schemas: Callable[[], tuple[dict[str, object], ...]] | None = None,
        tool_specs: Callable[[], tuple[ToolSpec, ...]] | None = None,
        policy: PolicyEngine | None = None,
        limits: ChildLimits | None = None,
        track_task: Callable[[asyncio.Task[RunResult]], None] | None = None,
    ) -> None:
        self._store = store
        self._engine = engine
        self._limits = limits or ChildLimits()
        self._spawn_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self._limits.max_concurrent_children)
        self._recover_run: Callable[[str], Awaitable[RunHandle]] | None = None
        self._recovery_waiters: dict[str, asyncio.Task[RunResult]] = {}
        self._track_task = track_task
        self._service = SubagentService(
            store,
            commands,
            engine,
            agents,
            tools=tools,
            tool_schemas=tool_schemas,
            tool_specs=tool_specs,
            policy=policy,
            track_task=track_task,
            execution_runner=self._run_when_scheduled,
        )

    async def spawn(
        self,
        parent_run_id: str,
        agent_revision: str,
        task: TaskEnvelope,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_node_id: str | None = None,
        workflow_node_execution: int | None = None,
    ) -> RunSnapshot:
        async with self._spawn_lock:
            parent = await self._load_durable_run(parent_run_id)
            selected_session_id = parent.snapshot.session_id
            if session_id is not None and session_id != selected_session_id:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "parent run not found",
                    retryable=False,
                )
            runs = await self._session_runs(selected_session_id)
            parent = next(
                (
                    durable
                    for durable in runs
                    if durable.snapshot.run_id == parent_run_id
                ),
                parent,
            )
            ancestor_chain = self._spawn_ancestor_chain(parent, runs)
            depth = len(ancestor_chain)
            self._enforce_limits(
                parent_run_id=parent_run_id,
                depth=depth,
                runs=runs,
            )
            return await self._service.spawn(
                session_id=selected_session_id,
                run_id=run_id,
                parent_run_id=parent_run_id,
                workflow_run_id=workflow_run_id,
                workflow_node_id=workflow_node_id,
                workflow_node_execution=workflow_node_execution,
                agent_revision=agent_revision,
                task=task,
                authenticated_ancestors=tuple(
                    ancestor.snapshot for ancestor in ancestor_chain
                ),
                ancestor_preconditions=tuple(
                    self._exact_run(ancestor) for ancestor in ancestor_chain
                ),
            )

    async def await_result(self, child_run_id: str) -> ChildResult:
        if self._service.task_for(child_run_id) is None and self._recover_run is not None:
            await (await self._recover_run(child_run_id)).result()
        return await self._service.await_result(child_run_id)

    def set_recover_run(
        self,
        recover_run: Callable[[str], Awaitable[RunHandle]],
    ) -> None:
        self._recover_run = recover_run

    async def wait(
        self,
        child_run_id: str,
        *,
        timeout_seconds: float | None = None,
        expected_parent_run_id: str | None = None,
    ) -> ChildWaitResult:
        timeout = self._limits.max_wait_seconds
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or timeout_seconds < 0
            ):
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "child wait timeout must be a finite non-negative number",
                    retryable=False,
                )
            timeout = min(float(timeout_seconds), timeout)
        durable = await self._load_durable_run(child_run_id)
        self._validate_child(durable.snapshot)
        ancestors = await self._ancestor_chain(durable)
        await self._assert_exact_runs(ancestors)
        if (
            expected_parent_run_id is not None
            and durable.snapshot.parent_run_id != expected_parent_run_id
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "child does not belong to the expected parent",
                retryable=False,
            )
        terminal = await self._terminal_wait_result(durable.snapshot)
        if terminal is not None:
            return terminal
        task = self._service.task_for(child_run_id)
        if task is None:
            task = self._recovery_waiter(child_run_id)
        if task is None:
            return ChildWaitResult(child_run_id=child_run_id, status="pending")
        done, _pending = await asyncio.wait((task,), timeout=timeout)
        if not done:
            snapshot = (await self._load_durable_run(child_run_id)).snapshot
            if snapshot.status is RunStatus.INTERRUPTED:
                return ChildWaitResult(
                    child_run_id=child_run_id,
                    status="interrupted",
                )
            return ChildWaitResult(child_run_id=child_run_id, status="pending")
        snapshot = (await self._load_durable_run(child_run_id)).snapshot
        result = await self._terminal_wait_result(snapshot)
        if result is not None:
            return result
        return ChildWaitResult(child_run_id=child_run_id, status="pending")

    def _recovery_waiter(
        self,
        child_run_id: str,
    ) -> asyncio.Task[RunResult] | None:
        existing = self._recovery_waiters.get(child_run_id)
        if existing is not None:
            return existing
        recover = self._recover_run
        if recover is None:
            return None
        waiter = asyncio.create_task(self._recover_and_wait(child_run_id, recover))
        self._recovery_waiters[child_run_id] = waiter
        waiter.add_done_callback(
            lambda settled: self._release_recovery_waiter(child_run_id, settled)
        )
        if self._track_task is not None:
            self._track_task(waiter)
        return waiter

    @staticmethod
    async def _recover_and_wait(
        child_run_id: str,
        recover: Callable[[str], Awaitable[RunHandle]],
    ) -> RunResult:
        handle = await recover(child_run_id)
        return await handle.result()

    def _release_recovery_waiter(
        self,
        child_run_id: str,
        task: asyncio.Task[RunResult],
    ) -> None:
        if self._recovery_waiters.get(child_run_id) is task:
            self._recovery_waiters.pop(child_run_id, None)
        if not task.cancelled():
            task.exception()

    async def _terminal_wait_result(
        self,
        snapshot: RunSnapshot,
    ) -> ChildWaitResult | None:
        if snapshot.status is RunStatus.COMPLETED:
            return ChildWaitResult(
                child_run_id=snapshot.run_id,
                status="completed",
                result=await self._service.await_result(snapshot.run_id),
            )
        if snapshot.status is RunStatus.FAILED:
            assert snapshot.error is not None
            return ChildWaitResult(
                child_run_id=snapshot.run_id,
                status="failed",
                error=snapshot.error,
            )
        if snapshot.status is RunStatus.INTERRUPTED:
            return ChildWaitResult(
                child_run_id=snapshot.run_id,
                status="interrupted",
            )
        return None

    @staticmethod
    def _validate_child(snapshot: RunSnapshot) -> None:
        if snapshot.parent_run_id is None or snapshot.task_envelope is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run is not a child",
                retryable=False,
            )

    async def _run_when_scheduled(
        self,
        child: RunSnapshot,
        request: ModelRequest,
    ) -> RunResult:
        async with self._semaphore:
            return await self._engine.execute(child.run_id, request)

    async def list(self, parent_run_id: str) -> tuple[ChildProgress, ...]:
        parent = await self._load_durable_run(parent_run_id)
        runs = await self._session_runs(parent.snapshot.session_id)
        depth = await self._child_depth(parent.snapshot, runs)
        events = await self._store.read_events(
            after_cursor=0,
            session_id=parent.snapshot.session_id,
        )
        children = tuple(
            run
            for run in runs
            if run.snapshot.parent_run_id == parent_run_id
        )
        progress: list[ChildProgress] = []
        for child in sorted(children, key=lambda item: item.created.cursor):
            envelope = child.snapshot.task_envelope
            if envelope is None:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "stored child relation is invalid",
                    retryable=False,
                )
            updated_at = child.created.event.occurred_at
            for stored in events:
                if stored.event.run_id == child.snapshot.run_id:
                    updated_at = max(updated_at, stored.event.occurred_at)
            progress.append(
                ChildProgress(
                    run_id=child.snapshot.run_id,
                    parent_run_id=parent_run_id,
                    status=_progress_status(child.snapshot.status),
                    objective=envelope.objective,
                    depth=depth,
                    created_at=child.created.event.occurred_at,
                    updated_at=updated_at,
                )
            )
        return tuple(progress)

    async def _load_durable_run(self, run_id: str) -> _DurableRun:
        data: dict[str, object] | None = None
        try:
            raw = await self._store.get_snapshot("run", run_id)
            if raw is None:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "run not found",
                    retryable=False,
                )
            data = raw
            snapshot = RunSnapshot.model_validate(data)
            if snapshot.run_id != run_id or data.get("run_id") != run_id:
                raise ValueError("run identity mismatch")
            events = await self._store.read_events(
                after_cursor=0,
                session_id=snapshot.session_id,
            )
            matches = tuple(
                stored
                for stored in events
                if stored.event.type == "run.created"
                and stored.event.run_id == run_id
                and stored.event.session_id == snapshot.session_id
                and run_created_event_matches(
                    snapshot,
                    stored.event.payload,
                    schema_version=stored.event.schema_version,
                )
            )
            if len(matches) != 1:
                raise ValueError("run creation identity is invalid")
            return _DurableRun(
                snapshot=snapshot,
                created=matches[0],
                raw_data=raw,
            )
        except AgentSDKError:
            raise
        except Exception:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "stored run is invalid",
                retryable=False,
            ) from None
        finally:
            data = None

    async def _session_runs(self, session_id: str) -> tuple[_DurableRun, ...]:
        try:
            events = await self._store.read_events(
                after_cursor=0,
                session_id=session_id,
            )
        except AgentSDKError:
            raise
        except Exception:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to load child relations",
                retryable=False,
            ) from None
        created = tuple(
            stored for stored in events if stored.event.type == "run.created"
        )
        runs: list[_DurableRun] = []
        seen: set[str] = set()
        for stored in created:
            run_id = stored.event.run_id
            if run_id is None or run_id in seen:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "stored child relations are invalid",
                    retryable=False,
                )
            seen.add(run_id)
            raw = await self._store.get_snapshot("run", run_id)
            try:
                if raw is None:
                    raise ValueError("run snapshot is missing")
                snapshot = RunSnapshot.model_validate(raw)
                if (
                    snapshot.run_id != run_id
                    or snapshot.session_id != session_id
                    or stored.event.session_id != session_id
                    or not run_created_event_matches(
                        snapshot,
                        stored.event.payload,
                        schema_version=stored.event.schema_version,
                    )
                ):
                    raise ValueError("run creation identity is invalid")
            except Exception:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "stored child relations are invalid",
                    retryable=False,
                ) from None
            runs.append(
                _DurableRun(
                    snapshot=snapshot,
                    created=stored,
                    raw_data=raw,
                )
            )
        return tuple(runs)

    async def _ancestor_chain(
        self,
        descendant: _DurableRun,
    ) -> tuple[_DurableRun, ...]:
        ancestors: list[_DurableRun] = []
        current = descendant
        visited = {current.snapshot.run_id}
        while current.snapshot.parent_run_id is not None:
            try:
                ancestor = await self._load_durable_run(
                    current.snapshot.parent_run_id
                )
            except AgentSDKError as error:
                if error.code is ErrorCode.NOT_FOUND:
                    raise AgentSDKError(
                        ErrorCode.INTERNAL,
                        "stored child relation is invalid",
                        retryable=False,
                    ) from None
                raise
            if (
                ancestor.snapshot.session_id != descendant.snapshot.session_id
                or ancestor.snapshot.run_id in visited
            ):
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "stored child relation is invalid",
                    retryable=False,
                )
            visited.add(ancestor.snapshot.run_id)
            ancestors.append(ancestor)
            current = ancestor
        return tuple(reversed(ancestors))

    async def _assert_exact_runs(self, runs: tuple[_DurableRun, ...]) -> None:
        try:
            await self._store.commit(
                CommitBatch(
                    events=(),
                    preconditions=tuple(self._exact_run(run) for run in runs),
                )
            )
        except SnapshotPreconditionError:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "stored child relation is invalid",
                retryable=False,
            ) from None

    @staticmethod
    def _exact_run(run: _DurableRun) -> SnapshotPrecondition:
        return SnapshotPrecondition(
            "run",
            run.snapshot.run_id,
            run.snapshot.version,
            run.snapshot.session_id,
            run.raw_data,
        )

    async def _child_depth(
        self,
        parent: RunSnapshot,
        runs: tuple[_DurableRun, ...],
    ) -> int:
        by_id = {run.snapshot.run_id: run.snapshot for run in runs}
        depth = 1
        current = parent
        visited = {current.run_id}
        while current.parent_run_id is not None:
            ancestor = by_id.get(current.parent_run_id)
            if ancestor is None:
                ancestor = (await self._load_durable_run(current.parent_run_id)).snapshot
            if (
                ancestor.session_id != parent.session_id
                or ancestor.run_id in visited
            ):
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "stored child relation is invalid",
                    retryable=False,
                )
            visited.add(ancestor.run_id)
            depth += 1
            current = ancestor
        return depth

    @staticmethod
    def _spawn_ancestor_chain(
        parent: _DurableRun,
        runs: tuple[_DurableRun, ...],
    ) -> tuple[_DurableRun, ...]:
        by_id = {run.snapshot.run_id: run for run in runs}
        chain = [parent]
        current = parent
        visited = {parent.snapshot.run_id}
        while current.snapshot.parent_run_id is not None:
            ancestor = by_id.get(current.snapshot.parent_run_id)
            if (
                ancestor is None
                or ancestor.snapshot.session_id != parent.snapshot.session_id
                or ancestor.snapshot.run_id in visited
            ):
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "stored child relation is invalid",
                    retryable=False,
                )
            visited.add(ancestor.snapshot.run_id)
            chain.append(ancestor)
            current = ancestor
        return tuple(reversed(chain))

    def _enforce_limits(
        self,
        *,
        parent_run_id: str,
        depth: int,
        runs: tuple[_DurableRun, ...],
    ) -> None:
        children = tuple(
            run.snapshot for run in runs if run.snapshot.parent_run_id is not None
        )
        if depth > self._limits.max_depth:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "child depth limit exceeded",
                retryable=False,
            )
        if sum(child.parent_run_id == parent_run_id for child in children) >= (
            self._limits.max_children_per_parent
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "children per parent limit exceeded",
                retryable=False,
            )
        if len(children) >= self._limits.max_children_per_session:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "children per session limit exceeded",
                retryable=False,
            )


_ProgressStatus = Literal[
    "queued",
    "running",
    "waiting",
    "interrupted",
    "completed",
    "failed",
]


def _progress_status(status: RunStatus) -> _ProgressStatus:
    if status is RunStatus.CREATED:
        return "queued"
    if status is RunStatus.RUNNING:
        return "running"
    if status in {RunStatus.WAITING_PERMISSION, RunStatus.WAITING_RECONCILIATION}:
        return "waiting"
    if status is RunStatus.INTERRUPTED:
        return "interrupted"
    if status is RunStatus.COMPLETED:
        return "completed"
    return "failed"
