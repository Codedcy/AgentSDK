from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal, cast

from agent_sdk.config import AgentSDKConfig
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest
from agent_sdk.permissions.broker import InProcessPermissionBridge
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.runtime.commands import RuntimeCommands
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

_ACompletion = Callable[..., Awaitable[Any]]
_PermissionDefault = Literal["allow", "deny", "ask"]
_DEFAULT_PERMISSION_BRIDGE = object()


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
    ) -> list[StoredEvent]:
        return await (await self._get()).read_events(
            after_cursor=after_cursor,
            session_id=session_id,
        )

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        return await (await self._get()).get_snapshot(kind, entity_id)

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
        active_tasks: set[asyncio.Task[RunResult]],
        owned_close: Callable[[], Awaitable[None]] | None,
    ) -> None:
        async with self._lock:
            self._closing = True
            if self._close_task is None:
                active = tuple(active_tasks)
                self._close_task = asyncio.create_task(
                    self._close_resources(active, owned_close)
                )
                self._close_task.add_done_callback(self._close_finished)
            close_task = self._close_task
        await asyncio.shield(close_task)

    @staticmethod
    def _close_finished(close_task: asyncio.Task[None]) -> None:
        if not close_task.cancelled():
            close_task.exception()

    @staticmethod
    async def _close_resources(
        active_tasks: tuple[asyncio.Task[RunResult], ...],
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
        self._active_tasks: set[asyncio.Task[RunResult]] = set()
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
        self.tools = tools
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

    def _track_task(self, task: asyncio.Task[RunResult]) -> None:
        self._active_tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[RunResult]) -> None:
        self._active_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        await self._lifecycle.close(self._active_tasks, self._owned_close)
