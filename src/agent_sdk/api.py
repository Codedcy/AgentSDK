from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Literal, cast

from agent_sdk.analytics import AnalyticsQueries, AnalyticsResult
from agent_sdk.config import AgentSDKConfig
from agent_sdk.evaluation import EvaluationEngine, EvaluationResult, Evaluator
from agent_sdk.errors import AgentSDKError, ErrorCode
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
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.handles import RunHandle
from agent_sdk.runtime.models import (
    AgentSpec,
    RunResult,
    RunSnapshot,
    SessionSnapshot,
    mutable_model_params,
)
from agent_sdk.storage.base import CommitBatch, CommitResult, StateStore, StoredEvent
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

    async def latest_cursor(self) -> int:
        return await (await self._get()).latest_cursor()

    async def delete_session(self, session_id: str) -> None:
        await (await self._get()).delete_session(session_id)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._open_task is None:
                return
            store = await self._open_task
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

    async def create(self, *, workspaces: Iterable[str | Path]) -> SessionSnapshot:
        async with self._lifecycle.admit():
            return await self._commands.create_session(workspaces=workspaces)

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
    ) -> WorkflowHandle:
        async with self._lifecycle.admit():
            workflow = self._compile(definition)
            return await self._executor.start(session_id, workflow)

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
    ) -> None:
        self._store = store
        self._commands = commands
        self._engine = engine
        self._track_task = track_task
        self._lifecycle = lifecycle
        self._tools = tools

    async def start(
        self,
        session_id: str,
        agent: AgentSpec,
        user_input: str,
    ) -> RunHandle:
        async with self._lifecycle.admit():
            if await self._store.get_snapshot("session", session_id) is None:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "session not found",
                    retryable=False,
                )
            created = await self._commands.start_run(
                session_id,
                agent_revision=agent.revision,
                user_input=user_input,
            )
            request = ModelRequest(
                model=agent.model,
                messages=({"role": "user", "content": user_input},),
                tools=self._tools.schemas(),
                params=mutable_model_params(agent.model_params),
            )
            task = asyncio.create_task(self._engine.execute(created.run_id, request))
            self._track_task(task)
            return RunHandle(created.run_id, self._store, task)

    async def get(self, run_id: str) -> RunSnapshot:
        try:
            data = await self._store.get_snapshot("run", run_id)
            if data is None:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "run not found",
                    retryable=False,
                )
            return RunSnapshot.model_validate(data)
        except AgentSDKError:
            raise
        except Exception as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to load run",
                retryable=False,
            ) from error


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
        )

    @classmethod
    def for_test(
        cls,
        *,
        store: StateStore,
        acompletion: _ACompletion,
        permission_default: _PermissionDefault = "ask",
        permission_bridge: InProcessPermissionBridge | None | object = (
            _DEFAULT_PERMISSION_BRIDGE
        ),
    ) -> AgentSDK:
        sdk = cls.__new__(cls)
        bridge = (
            InProcessPermissionBridge()
            if permission_bridge is _DEFAULT_PERMISSION_BRIDGE
            else cast(InProcessPermissionBridge | None, permission_bridge)
        )
        sdk._initialize(
            store,
            LiteLLMGateway._for_test(acompletion),
            permission_default=permission_default,
            permission_bridge=bridge,
            owned_close=None,
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
    ) -> None:
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._owned_close = owned_close
        self._lifecycle = _SDKLifecycle()
        commands = RuntimeCommands(store)
        tools = ToolRegistry()
        engine = RunEngine(
            store,
            models,
            tools,
            PolicyEngine(permission_default),
            permission_bridge,
        )
        agents = AgentRegistry()
        workflows = WorkflowExecutor(
            store,
            commands,
            engine,
            agents,
            tool_schemas=tools.schemas,
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
        )
        self.workflows = WorkflowAPI(workflows, WorkflowCompiler(), self._lifecycle)
        self.queries = QueryAPI(QueryService(store), self._lifecycle)
        self.events = EventAPI(
            SubscriptionService(store, close_signal=self._lifecycle.close_signal)
        )
        self.evaluations = EvaluationAPI(EvaluationEngine(store), self._lifecycle)
        self.analytics = AnalyticsAPI(AnalyticsQueries(store), self._lifecycle)

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        self._active_tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[Any]) -> None:
        self._active_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        await self._lifecycle.close(self._active_tasks, self._owned_close)
