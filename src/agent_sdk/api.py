from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from agent_sdk.config import AgentSDKConfig
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest
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

_ACompletion = Callable[..., Awaitable[Any]]


class _LazySQLiteStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._open_task: asyncio.Task[SQLiteStore] | None = None

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
        if self._open_task is None:
            return
        store = await self._open_task
        await store.close()

    async def _get(self) -> SQLiteStore:
        if self._open_task is None:
            self._open_task = asyncio.create_task(SQLiteStore.open(self._path))
        return await self._open_task


class SessionAPI:
    def __init__(self, commands: RuntimeCommands) -> None:
        self._commands = commands

    async def create(self, *, workspaces: Iterable[str | Path]) -> SessionSnapshot:
        return await self._commands.create_session(workspaces=workspaces)


class RunAPI:
    def __init__(
        self,
        store: StateStore,
        commands: RuntimeCommands,
        engine: RunEngine,
        track_task: Callable[[asyncio.Task[RunResult]], None],
    ) -> None:
        self._store = store
        self._commands = commands
        self._engine = engine
        self._track_task = track_task

    async def start(
        self,
        session_id: str,
        agent: AgentSpec,
        user_input: str,
    ) -> RunHandle:
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
            params=mutable_model_params(agent.model_params),
        )
        task = asyncio.create_task(self._engine.execute(created.run_id, request))
        self._track_task(task)
        return RunHandle(created.run_id, self._store, task)

    async def get(self, run_id: str) -> RunSnapshot:
        data = await self._store.get_snapshot("run", run_id)
        if data is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "run not found",
                retryable=False,
            )
        return RunSnapshot.model_validate(data)


class AgentSDK:
    def __init__(self, config: AgentSDKConfig) -> None:
        store = _LazySQLiteStore(config.database_path)
        self._initialize(store, LiteLLMGateway(), owned_close=store.close)

    @classmethod
    def for_test(
        cls,
        *,
        store: StateStore,
        acompletion: _ACompletion,
    ) -> AgentSDK:
        sdk = cls.__new__(cls)
        sdk._initialize(store, LiteLLMGateway._for_test(acompletion), owned_close=None)
        return sdk

    def _initialize(
        self,
        store: StateStore,
        models: LiteLLMGateway,
        *,
        owned_close: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self._active_tasks: set[asyncio.Task[RunResult]] = set()
        self._owned_close = owned_close
        self._close_task: asyncio.Task[None] | None = None
        commands = RuntimeCommands(store)
        engine = RunEngine(store, models)
        self.sessions = SessionAPI(commands)
        self.runs = RunAPI(store, commands, engine, self._track_task)

    def _track_task(self, task: asyncio.Task[RunResult]) -> None:
        self._active_tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[RunResult]) -> None:
        self._active_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self._close_task)

    async def _close_resources(self) -> None:
        active = tuple(self._active_tasks)
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if self._owned_close is not None:
            await self._owned_close()
