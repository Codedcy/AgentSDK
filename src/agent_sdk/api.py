from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator, Literal, cast

from agent_sdk.analytics import AnalyticsQueries, AnalyticsResult
from agent_sdk.config import AgentSDKConfig
from agent_sdk.context import (
    CompactionLevel,
    CompactionPolicy,
    ContextCapsule,
    ContextPlanner,
    ContextRetrieval,
    ContextView,
)
from agent_sdk.evaluation import EvaluationEngine, EvaluationResult, Evaluator
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest
from agent_sdk.permissions.broker import InProcessPermissionBridge
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.observability import (
    EventFilter,
    EventQueryResult,
    ExecutionTree,
    ObservedEvent,
    ObservedRun,
    QueryService,
    RunTimeline,
    SubscriptionService,
)
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
    ToolCapabilityDescriptor,
)
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.handles import RunHandle
from agent_sdk.runtime.leases import Lease
from agent_sdk.runtime.models import (
    AgentSpec,
    RunResult,
    RunSnapshot,
    SessionSnapshot,
    mutable_model_params,
)
from agent_sdk.runtime.provider_recovery import (
    ProviderRecoveryAdapter,
    ProviderRecoveryRegistry,
)
from agent_sdk.runtime.recovery import (
    RecoveryScanner,
    RunRecoveryService,
)
from agent_sdk.runtime.reconciliation import (
    ExternalOperation,
    ReconciliationRequest,
    RunCheckpoint,
    _context_free_recovery_errors,
)
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    RunProgressBatch,
    StateStore,
    StoredEvent,
)
from agent_sdk.storage.idempotency import IdempotencyRecord
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools.registry import ToolRegistry
from agent_sdk.workflow import (
    WorkflowCompiler,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowHandle,
    WorkflowIR,
    WorkflowRunSnapshot,
)

_ACompletion = Callable[..., Awaitable[Any]]
_PermissionDefault = Literal["allow", "deny", "ask"]
_DEFAULT_PERMISSION_BRIDGE = object()


class _WorkflowCompileFailure(Enum):
    INVALID = "invalid"


class _LazySQLiteStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._open_task: asyncio.Task[SQLiteStore] | None = None
        self._closed = False

    async def commit(self, batch: CommitBatch) -> CommitResult:
        return await (await self._get()).commit(batch)

    @_context_free_recovery_errors
    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        return await (await self._get()).commit_run_progress(batch)

    async def read_events(
        self,
        *,
        after_cursor: int,
        session_id: str | None = None,
        up_to_cursor: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        return await (await self._get()).read_events(
            after_cursor=after_cursor,
            session_id=session_id,
            up_to_cursor=up_to_cursor,
            limit=limit,
        )

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        return await (await self._get()).get_snapshot(kind, entity_id)

    async def get_idempotency(self, scope: str, key: str) -> IdempotencyRecord | None:
        return await (await self._get()).get_idempotency(scope, key)

    async def latest_cursor(self) -> int:
        return await (await self._get()).latest_cursor()

    async def delete_session(self, session_id: str) -> None:
        await (await self._get()).delete_session(session_id)

    async def acquire_lease(
        self, *, run_id: str, owner: str, now: datetime, expires_at: datetime
    ) -> Lease:
        return await (await self._get()).acquire_lease(
            run_id=run_id,
            owner=owner,
            now=now,
            expires_at=expires_at,
        )

    async def renew_lease(
        self, lease: Lease, *, now: datetime, expires_at: datetime
    ) -> Lease:
        return await (await self._get()).renew_lease(
            lease,
            now=now,
            expires_at=expires_at,
        )

    async def release_lease(self, lease: Lease) -> None:
        await (await self._get()).release_lease(lease)

    async def assert_current_lease(self, lease: Lease, *, now: datetime) -> None:
        await (await self._get()).assert_current_lease(lease, now=now)

    @_context_free_recovery_errors
    async def get_run_lease(self, run_id: str) -> Lease | None:
        return await (await self._get()).get_run_lease(run_id)

    @_context_free_recovery_errors
    async def list_abandoned_run_ids(self, *, now: datetime) -> tuple[str, ...]:
        return await (await self._get()).list_abandoned_run_ids(now=now)

    @_context_free_recovery_errors
    async def latest_run_event_sequence(self, run_id: str) -> int | None:
        return await (await self._get()).latest_run_event_sequence(run_id)

    @_context_free_recovery_errors
    async def create_external_operation(
        self, operation: ExternalOperation, *, lease: Lease, now: datetime
    ) -> ExternalOperation:
        return await (await self._get()).create_external_operation(
            operation, lease=lease, now=now
        )

    async def get_external_operation(
        self, operation_id: str
    ) -> ExternalOperation | None:
        return await (await self._get()).get_external_operation(operation_id)

    async def list_unresolved_external_operations(
        self, run_id: str
    ) -> tuple[ExternalOperation, ...]:
        return await (await self._get()).list_unresolved_external_operations(run_id)

    @_context_free_recovery_errors
    async def list_external_operations(
        self, run_id: str
    ) -> tuple[ExternalOperation, ...]:
        return await (await self._get()).list_external_operations(run_id)

    @_context_free_recovery_errors
    async def transition_external_operation(
        self,
        *,
        expected: ExternalOperation,
        updated: ExternalOperation,
        lease: Lease,
        now: datetime,
    ) -> ExternalOperation:
        return await (await self._get()).transition_external_operation(
            expected=expected,
            updated=updated,
            lease=lease,
            now=now,
        )

    @_context_free_recovery_errors
    async def put_run_checkpoint(
        self,
        checkpoint: RunCheckpoint,
        *,
        expected: RunCheckpoint | None,
        lease: Lease,
        now: datetime,
    ) -> RunCheckpoint:
        return await (await self._get()).put_run_checkpoint(
            checkpoint,
            expected=expected,
            lease=lease,
            now=now,
        )

    async def get_run_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        return await (await self._get()).get_run_checkpoint(run_id)

    @_context_free_recovery_errors
    async def create_reconciliation_request(
        self, request: ReconciliationRequest
    ) -> ReconciliationRequest:
        return await (await self._get()).create_reconciliation_request(request)

    async def get_reconciliation_request(
        self, request_id: str
    ) -> ReconciliationRequest | None:
        return await (await self._get()).get_reconciliation_request(request_id)

    async def list_pending_reconciliation_requests(
        self, run_id: str
    ) -> tuple[ReconciliationRequest, ...]:
        return await (await self._get()).list_pending_reconciliation_requests(run_id)

    @_context_free_recovery_errors
    async def resolve_reconciliation_request(
        self,
        *,
        expected: ReconciliationRequest,
        resolved: ReconciliationRequest,
        event: EventEnvelope,
    ) -> ReconciliationRequest:
        return await (await self._get()).resolve_reconciliation_request(
            expected=expected,
            resolved=resolved,
            event=event,
        )

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            open_task = self._open_task
            self._open_task = None
            if open_task is None:
                return
            try:
                store = await open_task
            except asyncio.CancelledError:
                if open_task.cancelled():
                    return
                raise
            except Exception:
                return
            await store.close()

    async def _get(self) -> SQLiteStore:
        async with self._lock:
            if self._closed:
                raise RuntimeError("SQLiteStore is closed")
            if self._open_task is None:
                self._open_task = asyncio.create_task(SQLiteStore.open(self._path))
            return await self._open_task


class _SDKLifecycle:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._closing = False
        self._close_signal = asyncio.Event()
        self._close_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        async with self._lock:
            if self._closing:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "SDK is closing",
                    retryable=False,
                )
            yield

    async def close(
        self,
        active_tasks: set[asyncio.Task[Any]],
        owned_close: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self._closing = True
        self._close_signal.set()
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._coordinate_close(active_tasks, owned_close)
            )
            self._close_task.add_done_callback(self._close_finished)
        close_task = self._close_task
        await asyncio.shield(close_task)

    async def _coordinate_close(
        self,
        active_tasks: set[asyncio.Task[Any]],
        owned_close: Callable[[], Awaitable[None]] | None,
    ) -> None:
        async with self._lock:
            active = tuple(active_tasks)
        await self._close_resources(active, owned_close)

    @property
    def close_signal(self) -> asyncio.Event:
        return self._close_signal

    @staticmethod
    def _close_finished(close_task: asyncio.Task[None]) -> None:
        if not close_task.cancelled():
            close_task.exception()

    @staticmethod
    async def _close_resources(
        active_tasks: tuple[asyncio.Task[Any], ...],
        owned_close: Callable[[], Awaitable[None]] | None,
    ) -> None:
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        if owned_close is not None:
            await owned_close()


class SessionAPI:
    def __init__(self, commands: RuntimeCommands, lifecycle: _SDKLifecycle) -> None:
        self._commands = commands
        self._lifecycle = lifecycle

    async def create(
        self,
        *,
        workspaces: Iterable[str | Path],
        idempotency_key: str | None = None,
    ) -> SessionSnapshot:
        async with self._lifecycle.admit():
            return await self._commands.create_session(
                workspaces=workspaces,
                idempotency_key=idempotency_key,
            )

    async def get(self, session_id: str) -> SessionSnapshot:
        async with self._lifecycle.admit():
            return await self._commands.get_session(session_id)

    async def close(
        self,
        session_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> SessionSnapshot:
        async with self._lifecycle.admit():
            return await self._commands.close_session(
                session_id,
                idempotency_key=idempotency_key,
            )

    async def delete(self, session_id: str) -> None:
        async with self._lifecycle.admit():
            await self._commands.delete_session(session_id)


class AgentAPI:
    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    def define(self, spec: AgentSpec) -> AgentSpec:
        return self._registry.define(spec)


class WorkflowAPI:
    def __init__(
        self,
        executor: WorkflowExecutor,
        compiler: WorkflowCompiler,
        lifecycle: _SDKLifecycle,
    ) -> None:
        self._executor = executor
        self._compiler = compiler
        self._lifecycle = lifecycle

    async def start(
        self,
        session_id: str,
        definition: WorkflowIR | WorkflowDefinition | str,
        *,
        idempotency_key: str | None = None,
    ) -> WorkflowHandle:
        async with self._lifecycle.admit():
            workflow = self._compile(definition)
            try:
                return await self._executor.start(
                    session_id,
                    workflow,
                    idempotency_key=idempotency_key,
                )
            finally:
                del idempotency_key
                del workflow

    async def resume(
        self,
        workflow_run_id: str,
        *,
        expected_workflow: WorkflowIR | WorkflowDefinition | str | None = None,
    ) -> WorkflowHandle:
        async with self._lifecycle.admit():
            expected = None if expected_workflow is None else self._compile(expected_workflow)
            return await self._executor.resume(
                workflow_run_id,
                expected_workflow=expected,
            )

    async def get(self, workflow_run_id: str) -> WorkflowRunSnapshot:
        return await self._executor.get(workflow_run_id)

    def _compile(self, definition: WorkflowIR | WorkflowDefinition | str) -> WorkflowIR:
        result = _compile_workflow(self._compiler, definition)
        if result is _WorkflowCompileFailure.INVALID:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "workflow definition is invalid",
                retryable=False,
            )
        return result


def _compile_workflow(
    compiler: WorkflowCompiler,
    definition: WorkflowIR | WorkflowDefinition | str,
) -> WorkflowIR | _WorkflowCompileFailure:
    try:
        if isinstance(definition, WorkflowIR):
            return definition
        if isinstance(definition, WorkflowDefinition):
            return compiler.compile(definition)
        if isinstance(definition, str):
            return compiler.compile_yaml(definition)
        return _WorkflowCompileFailure.INVALID
    except Exception:
        return _WorkflowCompileFailure.INVALID


class RunAPI:
    def __init__(
        self,
        store: StateStore,
        commands: RuntimeCommands,
        engine: RunEngine,
        track_task: Callable[[asyncio.Task[RunResult]], None],
        lifecycle: _SDKLifecycle,
        tools: ToolRegistry,
        policy: PolicyEngine,
    ) -> None:
        self._store = store
        self._commands = commands
        self._engine = engine
        self._track_task = track_task
        self._lifecycle = lifecycle
        self._tools = tools
        self._policy = policy
        self._start_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[RunResult]] = {}

    async def start(
        self,
        session_id: str,
        agent: AgentSpec,
        user_input: str,
        *,
        idempotency_key: str | None = None,
    ) -> RunHandle:
        try:
            async with self._lifecycle.admit():
                messages = ({"role": "user", "content": user_input},)
                config = self._policy.execution_config()
                descriptor = ExecutionDescriptor.create(
                    agent=agent,
                    messages=messages,
                    tools=tuple(
                        ToolCapabilityDescriptor.from_spec(spec)
                        for spec in self._tools.list()
                    ),
                    policy=ExecutionPolicyDescriptor.create(
                        permission_default=config["permission_default"]
                    ),
                )
                request = ModelRequest(
                    model=agent.model,
                    messages=messages,
                    tools=self._tools.schemas(),
                    params=mutable_model_params(agent.model_params),
                )
                coordinator = asyncio.create_task(
                    self._coordinate_start(
                        session_id=session_id,
                        agent_revision=f"{agent.name}:{agent.revision}",
                        user_input=user_input,
                        execution_descriptor=descriptor,
                        request=request,
                        idempotency_key=idempotency_key,
                    )
                )
                return await self._await_start_coordinator(coordinator)
        finally:
            del idempotency_key

    async def _coordinate_start(
        self,
        *,
        session_id: str,
        agent_revision: str,
        user_input: str,
        execution_descriptor: ExecutionDescriptor,
        request: ModelRequest,
        idempotency_key: str | None,
    ) -> RunHandle:
        try:
            async with self._start_lock:
                outcome = await self._commands.start_run(
                    session_id,
                    agent_revision=agent_revision,
                    user_input=user_input,
                    execution_descriptor=execution_descriptor,
                    idempotency_key=idempotency_key,
                )
                snapshot = outcome.value
                task = self._tasks.get(snapshot.run_id)
                if not outcome.replayed:
                    task = asyncio.create_task(
                        self._engine.execute(snapshot.run_id, request)
                    )
                    self._tasks[snapshot.run_id] = task
                    task.add_done_callback(
                        partial(self._release_task, snapshot.run_id)
                    )
                    self._track_task(task)
                return RunHandle(snapshot.run_id, self._store, task)
        finally:
            del idempotency_key

    def _release_task(
        self,
        run_id: str,
        task: asyncio.Task[RunResult],
    ) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id)

    @staticmethod
    async def _await_start_coordinator(
        coordinator: asyncio.Task[RunHandle],
    ) -> RunHandle:
        cancellation: asyncio.CancelledError | None = None
        try:
            return await asyncio.shield(coordinator)
        except asyncio.CancelledError as error:
            cancellation = error

        while not coordinator.done():
            try:
                await asyncio.shield(coordinator)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if coordinator.done() and not coordinator.cancelled():
            coordinator.exception()
        assert cancellation is not None
        raise cancellation from None

    async def get(self, run_id: str) -> RunSnapshot:
        data: dict[str, Any] | None = None
        store_failed = False
        try:
            data = await self._store.get_snapshot("run", run_id)
        except AgentSDKError:
            raise
        except Exception:
            store_failed = True
        if store_failed:
            data = None
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to load run",
                retryable=False,
            ) from None
        if data is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "run not found",
                retryable=False,
            )
        snapshot: RunSnapshot | None = None
        validation_failed = False
        try:
            snapshot = RunSnapshot.model_validate(data)
        except Exception:
            validation_failed = True
        data = None
        if validation_failed:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to load run",
                retryable=False,
            ) from None
        assert snapshot is not None
        return snapshot


class RecoveryAPI:
    def __init__(
        self,
        store: StateStore,
        scanner: RecoveryScanner,
        engine: RunEngine,
        agents: AgentRegistry,
        tools: ToolRegistry,
        policy: PolicyEngine,
        track_task: Callable[[asyncio.Task[RunResult]], None],
        lifecycle: _SDKLifecycle,
        ensure_startup_scan: Callable[
            [], Awaitable[tuple[asyncio.Task[None], bool]]
        ],
        provider_recovery: ProviderRecoveryRegistry,
        provider_recovery_timeout_seconds: float,
    ) -> None:
        self._store = store
        self._scanner = scanner
        self._engine = engine
        self._agents = agents
        self._tools = tools
        self._policy = policy
        self._track_task = track_task
        self._lifecycle = lifecycle
        self._ensure_startup_scan = ensure_startup_scan
        self._provider_recovery = provider_recovery
        self._start_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[RunResult]] = {}
        self._service = RunRecoveryService(
            store,
            engine,
            agents,
            tools,
            policy,
            provider_recovery,
            _stopping=lifecycle.close_signal.is_set,
            _wait_stopping=lifecycle.close_signal.wait,
            _adapter_timeout=provider_recovery_timeout_seconds,
        )

    def register_adapter(
        self,
        adapter: ProviderRecoveryAdapter,
    ) -> ProviderRecoveryAdapter:
        return self._provider_recovery.register(adapter)

    def unregister_adapter(
        self,
        provider_identity: str,
        *,
        expected: ProviderRecoveryAdapter | None = None,
    ) -> bool:
        return self._provider_recovery.unregister(
            provider_identity,
            expected=expected,
        )

    def get_adapter(self, provider_identity: str) -> ProviderRecoveryAdapter:
        return self._provider_recovery.get(provider_identity)

    def list_adapters(self) -> tuple[ProviderRecoveryAdapter, ...]:
        return self._provider_recovery.list()

    async def scan(self) -> None:
        async with self._lifecycle.admit():
            startup, created = await self._ensure_startup_scan()
            settled_at_entry = startup.done()
            await asyncio.shield(startup)
            if not created and settled_at_entry:
                await self._scanner.scan()

    async def recover_run(self, run_id: str) -> RunHandle:
        async with self._lifecycle.admit():
            startup, _created = await self._ensure_startup_scan()
            await asyncio.shield(startup)
            async with self._start_lock:
                existing = self._tasks.get(run_id)
                if existing is not None:
                    return RunHandle(run_id, self._store, existing)
                plan = await self._service.plan(run_id)
                if plan.kind == "detached":
                    return RunHandle(run_id, self._store, None)
                task = asyncio.create_task(self._service.execute(plan))
                self._tasks[run_id] = task
                task.add_done_callback(partial(self._release_task, run_id))
                self._track_task(task)
                return RunHandle(run_id, self._store, task)

    async def pending_requests(
        self,
        run_id: str,
    ) -> tuple[ReconciliationRequest, ...]:
        async with self._lifecycle.admit():
            startup, _created = await self._ensure_startup_scan()
            await asyncio.shield(startup)
            return await self._service.pending_requests(run_id)

    def _release_task(
        self,
        run_id: str,
        task: asyncio.Task[RunResult],
    ) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id)

class ContextAPI:
    def __init__(
        self,
        store: StateStore,
        models: LiteLLMGateway,
        lifecycle: _SDKLifecycle,
    ) -> None:
        self._store = store
        self._models = models
        self._lifecycle = lifecycle
        self._retrieval = ContextRetrieval(store)

    async def build(
        self,
        session_id: str,
        *,
        model: str,
        model_window: int,
        output_reserve: int = 0,
        tool_schema_tokens: int = 0,
        safety_reserve: int = 0,
        policy: CompactionPolicy | None = None,
        force_level: CompactionLevel | str | None = None,
        protected_event_ids: Iterable[str] = (),
    ) -> ContextView:
        async with self._lifecycle.admit():
            planner = ContextPlanner(
                self._store,
                self._models,
                model=model,
                model_window=model_window,
                output_reserve=output_reserve,
                tool_schema_tokens=tool_schema_tokens,
                safety_reserve=safety_reserve,
                policy=policy,
            )
            return await planner.build(
                session_id,
                force_level=force_level,
                protected_event_ids=protected_event_ids,
            )

    async def get_capsule(
        self,
        capsule_id: str,
        *,
        session_id: str,
    ) -> ContextCapsule:
        async with self._lifecycle.admit():
            return await self._retrieval.get_capsule(
                capsule_id,
                session_id=session_id,
            )

    async def read_sources(
        self,
        capsule_id: str,
        *,
        session_id: str,
    ) -> tuple[ObservedEvent, ...]:
        async with self._lifecycle.admit():
            stored = await self._retrieval.read_sources(
                capsule_id,
                session_id=session_id,
            )
            return tuple(
                ObservedEvent(cursor=item.cursor, event=item.event)
                for item in stored
            )


class QueryAPI:
    def __init__(self, queries: QueryService, lifecycle: _SDKLifecycle) -> None:
        self._queries = queries
        self._lifecycle = lifecycle

    async def get_run(self, run_id: str) -> ObservedRun:
        async with self._lifecycle.admit():
            return await self._queries.get_run(run_id)

    async def timeline(self, run_id: str) -> RunTimeline:
        async with self._lifecycle.admit():
            return await self._queries.timeline(run_id)

    async def execution_tree(self, root_run_id: str) -> ExecutionTree:
        async with self._lifecycle.admit():
            return await self._queries.execution_tree(root_run_id)

    async def query_events(
        self,
        filters: EventFilter | None = None,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> EventQueryResult:
        async with self._lifecycle.admit():
            return await self._queries.query_events(
                filters,
                after_cursor=after_cursor,
                limit=limit,
            )


class EventAPI:
    def __init__(self, subscriptions: SubscriptionService) -> None:
        self._subscriptions = subscriptions

    def subscribe(
        self,
        *,
        filters: EventFilter | None = None,
        cursor: int = 0,
    ) -> AsyncIterator[ObservedEvent]:
        return self._subscriptions.subscribe(filters=filters, cursor=cursor)


class EvaluationAPI:
    def __init__(
        self,
        evaluations: EvaluationEngine,
        lifecycle: _SDKLifecycle,
    ) -> None:
        self._evaluations = evaluations
        self._lifecycle = lifecycle

    async def evaluate(
        self,
        run_id: str,
        evaluator: Evaluator,
    ) -> EvaluationResult:
        async with self._lifecycle.admit():
            return await self._evaluations.evaluate(run_id, evaluator)


class AnalyticsAPI:
    def __init__(
        self,
        analytics: AnalyticsQueries,
        lifecycle: _SDKLifecycle,
    ) -> None:
        self._analytics = analytics
        self._lifecycle = lifecycle

    async def success_rate(
        self,
        *,
        evaluator_id: str | None = None,
    ) -> AnalyticsResult:
        async with self._lifecycle.admit():
            return await self._analytics.success_rate(evaluator_id=evaluator_id)

    async def tool_failures(
        self,
        *,
        tool_name: str | None = None,
    ) -> AnalyticsResult:
        async with self._lifecycle.admit():
            return await self._analytics.tool_failures(tool_name=tool_name)

    async def tool_failure_rate(
        self,
        *,
        tool_name: str | None = None,
    ) -> AnalyticsResult:
        async with self._lifecycle.admit():
            return await self._analytics.tool_failure_rate(tool_name=tool_name)


class PermissionAPI:
    def __init__(self, bridge: InProcessPermissionBridge | None) -> None:
        self._bridge = bridge

    async def next_request(self, run_id: str) -> PermissionRequest:
        if self._bridge is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "permission bridge unavailable",
                retryable=False,
            )
        return await self._bridge.next_request(run_id)

    async def resolve(
        self,
        request_id: str,
        decision: PermissionDecision,
    ) -> None:
        if self._bridge is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "permission bridge unavailable",
                retryable=False,
            )
        await self._bridge.resolve(request_id, decision)


class AgentSDK:
    def __init__(self, config: AgentSDKConfig) -> None:
        store = _LazySQLiteStore(config.database_path)
        self._initialize(
            store,
            LiteLLMGateway(),
            permission_default=config.permission_default,
            permission_bridge=InProcessPermissionBridge(),
            owned_close=store.close,
            provider_recovery_timeout_seconds=30.0,
        )

    @classmethod
    def for_test(
        cls,
        *,
        acompletion: _ACompletion,
        store: StateStore | None = None,
        database_path: str | Path | None = None,
        permission_default: _PermissionDefault = "ask",
        permission_bridge: InProcessPermissionBridge | None | object = (
            _DEFAULT_PERMISSION_BRIDGE
        ),
        provider_recovery_timeout_seconds: float = 30.0,
    ) -> AgentSDK:
        if (store is None) == (database_path is None):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "exactly one test Store or database path is required",
                retryable=False,
            )
        selected_store: StateStore
        owned_close: Callable[[], Awaitable[None]] | None
        if database_path is not None:
            lazy_store = _LazySQLiteStore(Path(database_path))
            selected_store = lazy_store
            owned_close = lazy_store.close
        else:
            assert store is not None
            selected_store = store
            owned_close = None
        sdk = cls.__new__(cls)
        bridge = (
            InProcessPermissionBridge()
            if permission_bridge is _DEFAULT_PERMISSION_BRIDGE
            else cast(InProcessPermissionBridge | None, permission_bridge)
        )
        sdk._initialize(
            selected_store,
            LiteLLMGateway._for_test(acompletion),
            permission_default=permission_default,
            permission_bridge=bridge,
            owned_close=owned_close,
            provider_recovery_timeout_seconds=provider_recovery_timeout_seconds,
        )
        return sdk

    def _initialize(
        self,
        store: StateStore,
        models: LiteLLMGateway,
        *,
        permission_default: _PermissionDefault,
        permission_bridge: InProcessPermissionBridge | None,
        owned_close: Callable[[], Awaitable[None]] | None,
        provider_recovery_timeout_seconds: float,
    ) -> None:
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._owned_close = owned_close
        self._lifecycle = _SDKLifecycle()
        self._startup_scan_lock = asyncio.Lock()
        self._startup_scan_task: asyncio.Task[None] | None = None
        commands = RuntimeCommands(store)
        tools = ToolRegistry()
        provider_recovery = ProviderRecoveryRegistry()
        policy = PolicyEngine(permission_default)
        engine = RunEngine(
            store,
            models,
            tools,
            policy,
            permission_bridge,
            provider_recovery=provider_recovery,
        )
        agents = AgentRegistry()
        recovery_scanner = RecoveryScanner(store)
        workflows = WorkflowExecutor(
            store,
            commands,
            engine,
            agents,
            tool_schemas=tools.schemas,
            tool_specs=tools.list,
            policy=policy,
            track_run_task=self._track_task,
            track_workflow_task=self._track_task,
        )
        self.tools = tools
        self.agents = AgentAPI(agents)
        self.permissions = PermissionAPI(permission_bridge)
        self.sessions = SessionAPI(commands, self._lifecycle)
        self.runs = RunAPI(
            store,
            commands,
            engine,
            self._track_task,
            self._lifecycle,
            tools,
            policy,
        )
        self.context = ContextAPI(store, models, self._lifecycle)
        self.workflows = WorkflowAPI(workflows, WorkflowCompiler(), self._lifecycle)
        self.queries = QueryAPI(QueryService(store), self._lifecycle)
        self.events = EventAPI(
            SubscriptionService(store, close_signal=self._lifecycle.close_signal)
        )
        self.evaluations = EvaluationAPI(EvaluationEngine(store), self._lifecycle)
        self.analytics = AnalyticsAPI(AnalyticsQueries(store), self._lifecycle)
        self._recovery_scanner = recovery_scanner
        self.recovery = RecoveryAPI(
            store,
            recovery_scanner,
            engine,
            agents,
            tools,
            policy,
            self._track_task,
            self._lifecycle,
            self._ensure_startup_scan,
            provider_recovery,
            provider_recovery_timeout_seconds,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            self._startup_scan_task = loop.create_task(recovery_scanner.scan())
            self._track_task(self._startup_scan_task)

    async def _ensure_startup_scan(
        self,
    ) -> tuple[asyncio.Task[None], bool]:
        async with self._startup_scan_lock:
            task = self._startup_scan_task
            if task is not None:
                return task, False
            task = asyncio.create_task(self._recovery_scanner.scan())
            self._startup_scan_task = task
            self._track_task(task)
            return task, True

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        self._active_tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[Any]) -> None:
        self._active_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        await self._lifecycle.close(self._active_tasks, self._owned_close)
