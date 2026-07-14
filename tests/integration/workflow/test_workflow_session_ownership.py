from __future__ import annotations

import asyncio
import json
import sqlite3
import traceback
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    ToolContext,
    ToolSpec,
    WorkflowDefinition,
)
from agent_sdk.runtime.models import SessionSnapshot, SessionStatus
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.session_lifecycle import exact_session_precondition, session_write
from agent_sdk.storage.base import (
    CommitBatch,
    CommitResult,
    SnapshotPreconditionError,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.idempotency import (
    IdempotencyCorruptionError,
    IdempotencyReplay,
)
from agent_sdk.workflow.handles import WorkflowHandle
from agent_sdk.workflow.compiler import WorkflowCompiler
from agent_sdk.workflow.state import WorkflowState


WORKFLOW = WorkflowDefinition.model_validate(
    {
        "api_version": "agent-sdk/v1",
        "kind": "Workflow",
        "name": "owned-workflow",
        "nodes": [
            {
                "id": "main",
                "kind": "agent",
                "agent_revision": "worker:1",
                "input": "finish the workflow",
            }
        ],
        "edges": [],
    }
)


def _chunks(text: str) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": text}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    return generate()


def _sdk(provider: Any) -> AgentSDK:
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    return sdk


def _assert_context_free_sanitizer(
    error: AgentSDKError,
    *,
    secret: str,
    original: BaseException | None = None,
) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    current = error.__traceback__
    while current is not None:
        local_values = current.tb_frame.f_locals
        filename = current.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in filename:
            if original is not None:
                assert all(value is not original for value in local_values.values())
            assert secret not in repr(local_values)
        current = current.tb_next


@pytest.mark.asyncio
async def test_workflow_start_attaches_to_session_and_close_enters_closing() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        started.set()
        await release.wait()
        return _chunks("done")

    sdk = _sdk(provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.workflows.start(session.session_id, WORKFLOW)
        await asyncio.wait_for(started.wait(), timeout=1)

        owned = await sdk.sessions.get(session.session_id)
        assert owned.active_workflow_run_ids == (handle.workflow_run_id,)
        assert (await sdk.sessions.close(session.session_id)).status is SessionStatus.CLOSING
    finally:
        release.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_closed_session_rejects_new_workflow_start() -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("unexpected")

    sdk = _sdk(provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        assert (await sdk.sessions.close(session.session_id)).status is SessionStatus.CLOSED

        with pytest.raises(AgentSDKError) as rejected:
            await sdk.workflows.start(session.session_id, WORKFLOW)
        assert rejected.value.code is ErrorCode.INVALID_STATE
        assert calls == 0
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_last_workflow_completion_detaches_and_closes_closing_session() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        started.set()
        await release.wait()
        return _chunks("done")

    sdk = _sdk(provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.workflows.start(session.session_id, WORKFLOW)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert (await sdk.sessions.get(session.session_id)).active_workflow_run_ids == (
            handle.workflow_run_id,
        )
        assert (await sdk.sessions.close(session.session_id)).status is SessionStatus.CLOSING

        release.set()
        await asyncio.wait_for(handle.result(), timeout=1)

        closed = await sdk.sessions.get(session.session_id)
        assert closed.status is SessionStatus.CLOSED
        assert closed.active_workflow_run_ids == ()
    finally:
        release.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_workflow_failure_detaches_from_active_session() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        started.set()
        await release.wait()
        raise RuntimeError("private provider failure")

    sdk = _sdk(provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.workflows.start(session.session_id, WORKFLOW)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert (await sdk.sessions.get(session.session_id)).active_workflow_run_ids == (
            handle.workflow_run_id,
        )

        release.set()
        with pytest.raises(AgentSDKError):
            await asyncio.wait_for(handle.result(), timeout=1)

        active = await sdk.sessions.get(session.session_id)
        assert active.status is SessionStatus.ACTIVE
        assert active.active_workflow_run_ids == ()
    finally:
        release.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_completed_workflow_start_replays_same_key_without_reexecution() -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("done")

    sdk = _sdk(provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        first = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="same-workflow",
        )
        first_result = await asyncio.wait_for(first.result(), timeout=1)

        replay = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="same-workflow",
        )
        replay_result = await asyncio.wait_for(replay.result(), timeout=1)

        assert replay.workflow_run_id == first.workflow_run_id
        assert replay_result == first_result
        assert calls == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_same_workflow_key_with_different_ir_conflicts_without_execution() -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("done")

    changed = type(WORKFLOW).model_validate(
        {
            **WORKFLOW.model_dump(mode="json"),
            "name": "changed-workflow",
        }
    )
    sdk = _sdk(provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        first = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="workflow-key",
        )
        await asyncio.wait_for(first.result(), timeout=1)

        with pytest.raises(AgentSDKError) as conflict:
            await sdk.workflows.start(
                session.session_id,
                changed,
                idempotency_key="workflow-key",
            )
        assert conflict.value.code is ErrorCode.CONFLICT
        assert calls == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_workflow_persists_current_complete_execution_descriptor() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        started.set()
        await release.wait()
        return _chunks("done")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="deny",
    )
    sdk.agents.define(
        AgentSpec(
            name="worker",
            revision="1",
            model="fake/worker-v2",
            model_params={"temperature": 0.25},
        )
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.workflows.start(session.session_id, WORKFLOW)
        await asyncio.wait_for(started.wait(), timeout=1)
        snapshot = await sdk.workflows.get(handle.workflow_run_id)

        assert snapshot.execution_compatibility == "current"
        descriptor = snapshot.execution_descriptor
        assert descriptor is not None
        assert descriptor.workflow_definition_hash == snapshot.workflow.definition_hash
        assert tuple(agent.revision for agent in descriptor.agents) == ("worker:1",)
        assert descriptor.agents[0].execution.agent.model == "fake/worker-v2"
        assert descriptor.agents[0].execution.agent.model_params == {"temperature": 0.25}
        assert descriptor.tools == ()
        assert descriptor.policy.permission_default == "deny"
    finally:
        release.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_24_concurrent_duplicate_workflow_starts_execute_once() -> None:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        started.set()
        await release.wait()
        return _chunks("done")

    sdk = _sdk(provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handles = await asyncio.gather(
            *(
                sdk.workflows.start(
                    session.session_id,
                    WORKFLOW,
                    idempotency_key="concurrent-workflow",
                )
                for _ in range(24)
            )
        )
        assert len({handle.workflow_run_id for handle in handles}) == 1
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        await asyncio.gather(*(handle.result() for handle in handles))
        assert calls == 1
    finally:
        release.set()
        await sdk.close()


class _RetainDeletingStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.retain_once = True

    async def delete_session(self, session_id: str) -> None:
        if self.retain_once:
            self.retain_once = False
            raise RuntimeError("private delete failure")
        await super().delete_session(session_id)


@pytest.mark.asyncio
async def test_deleting_session_rejects_matching_workflow_replay() -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("done")

    store = _RetainDeletingStore()
    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        first = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="workflow-replay",
        )
        await first.result()
        await sdk.sessions.close(session.session_id)
        with pytest.raises(AgentSDKError):
            await sdk.sessions.delete(session.session_id)

        with pytest.raises(AgentSDKError) as rejected:
            await sdk.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="workflow-replay",
            )
        assert rejected.value.code is ErrorCode.INVALID_STATE
        assert rejected.value.message == "session is deleting"
        assert calls == 1
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_same_key_same_revision_changed_agent_conflicts_without_execution() -> None:
    first_calls = 0
    second_calls = 0

    async def first_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal first_calls
        del params
        first_calls += 1
        return _chunks("done")

    async def second_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal second_calls
        del params
        second_calls += 1
        return _chunks("unexpected")

    store = InMemoryStore()
    first = AgentSDK.for_test(store=store, acompletion=first_provider)
    second = AgentSDK.for_test(store=store, acompletion=second_provider)
    first.agents.define(
        AgentSpec(name="worker", revision="1", model="fake/first", model_params={"seed": 1})
    )
    second.agents.define(
        AgentSpec(name="worker", revision="1", model="fake/second", model_params={"seed": 2})
    )
    try:
        session = await first.sessions.create(workspaces=[])
        original = await first.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="descriptor-key",
        )
        await original.result()

        with pytest.raises(AgentSDKError) as conflict:
            await second.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="descriptor-key",
            )
        assert conflict.value.code is ErrorCode.CONFLICT
        assert first_calls == 1
        assert second_calls == 0
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_capability",
    [
        {"version": "2"},
        {"source": "mcp:test"},
        {"effects": ("filesystem.write",)},
        {"timeout_seconds": 2.0},
    ],
)
async def test_same_key_changed_tool_capability_conflicts_without_execution(
    changed_capability: dict[str, object],
) -> None:
    second_calls = 0

    async def first_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        return _chunks("done")

    async def second_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal second_calls
        del params
        second_calls += 1
        return _chunks("unexpected")

    async def handler(_: ToolContext, **values: object) -> object:
        return values

    store = InMemoryStore()
    first = AgentSDK.for_test(store=store, acompletion=first_provider)
    second = AgentSDK.for_test(store=store, acompletion=second_provider)
    for sdk in (first, second):
        sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    base = {
        "name": "inspect",
        "description": "Inspect input",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
    first.tools.register(ToolSpec(**base), handler)
    second.tools.register(ToolSpec(**base, **changed_capability), handler)
    try:
        session = await first.sessions.create(workspaces=[])
        original = await first.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="tool-key",
        )
        await original.result()

        with pytest.raises(AgentSDKError) as conflict:
            await second.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="tool-key",
            )
        assert conflict.value.code is ErrorCode.CONFLICT
        assert second_calls == 0
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_same_key_changed_effective_policy_conflicts_without_execution() -> None:
    second_calls = 0

    async def first_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        return _chunks("done")

    async def second_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal second_calls
        del params
        second_calls += 1
        return _chunks("unexpected")

    store = InMemoryStore()
    first = AgentSDK.for_test(
        store=store,
        acompletion=first_provider,
        permission_default="allow",
    )
    second = AgentSDK.for_test(
        store=store,
        acompletion=second_provider,
        permission_default="deny",
    )
    for sdk in (first, second):
        sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await first.sessions.create(workspaces=[])
        original = await first.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="policy-key",
        )
        await original.result()

        with pytest.raises(AgentSDKError) as conflict:
            await second.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="policy-key",
            )
        assert conflict.value.code is ErrorCode.CONFLICT
        assert second_calls == 0
    finally:
        await first.close()
        await second.close()


class _WorkflowStartBoundaryStore(InMemoryStore):
    def __init__(self, phase: str) -> None:
        super().__init__()
        self.phase = phase
        self.enabled = False
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self._blocked = False

    async def _maybe_block(self, phase: str) -> None:
        if self.enabled and self.phase == phase and not self._blocked:
            self._blocked = True
            self.reached.set()
            await asyncio.wait_for(self.release.wait(), timeout=2)

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        if kind == "session":
            await self._maybe_block("session-load")
        return await super().get_snapshot(kind, entity_id)

    async def get_idempotency(self, scope: str, key: str):
        await self._maybe_block("idempotency-hint")
        return await super().get_idempotency(scope, key)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(event.type == "session.workflow.attached" for event in batch.events):
            await self._maybe_block("commit")
        return await super().commit(batch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    ["session-load", "idempotency-hint", "commit", "post-command"],
)
async def test_cancelled_workflow_start_registers_task_before_reraising(
    phase: str,
) -> None:
    store = _WorkflowStartBoundaryStore(phase)
    provider_started = asyncio.Event()
    provider_release = asyncio.Event()
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        provider_started.set()
        await provider_release.wait()
        return _chunks("done")

    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    executor = sdk.workflows._executor  # type: ignore[attr-defined]
    original_create = executor._state.create
    starter: asyncio.Task[Any] | None = None
    try:
        session = await sdk.sessions.create(workspaces=[])
        if phase == "post-command":

            async def blocked_create(*args: Any, **kwargs: Any):
                outcome = await original_create(*args, **kwargs)
                store.reached.set()
                await asyncio.wait_for(store.release.wait(), timeout=2)
                return outcome

            executor._state.create = blocked_create
        store.enabled = True
        starter = asyncio.create_task(
            sdk.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="cancelled-workflow",
            )
        )
        await asyncio.wait_for(store.reached.wait(), timeout=1)
        starter.cancel("original workflow cancellation")
        store.release.set()

        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(starter, timeout=1)
        assert cancelled.value.args == ("original workflow cancellation",)

        replay = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="cancelled-workflow",
        )
        await asyncio.wait_for(provider_started.wait(), timeout=1)
        assert calls == 1
        assert replay.workflow_run_id in executor._active
        provider_release.set()
        assert (await asyncio.wait_for(replay.result(), timeout=1)).output_text == "done"
    finally:
        store.release.set()
        provider_release.set()
        executor._state.create = original_create
        if starter is not None and not starter.done():
            starter.cancel()
        if starter is not None:
            await asyncio.gather(starter, return_exceptions=True)
        await sdk.close()


@pytest.mark.asyncio
async def test_running_replay_reuses_task_and_terminal_cleanup_releases_registry() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        started.set()
        await release.wait()
        return _chunks("done")

    sdk = _sdk(provider)
    executor = sdk.workflows._executor  # type: ignore[attr-defined]
    try:
        session = await sdk.sessions.create(workspaces=[])
        first = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="live-replay",
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        replay = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="live-replay",
        )

        assert first._task is replay._task  # type: ignore[attr-defined]
        assert calls == 1
        release.set()
        await asyncio.gather(first.result(), replay.result())
        await asyncio.sleep(0)
        assert executor._active == {}
    finally:
        release.set()
        await sdk.close()


@pytest.mark.asyncio
async def test_completed_workflow_replay_is_detached_and_uses_durable_result() -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("done")

    sdk = _sdk(provider)
    executor = sdk.workflows._executor  # type: ignore[attr-defined]
    try:
        session = await sdk.sessions.create(workspaces=[])
        first = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="completed-replay",
        )
        first_result = await first.result()
        await asyncio.sleep(0)
        assert executor._active == {}

        replay = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="completed-replay",
        )
        assert replay.attached is False
        assert await asyncio.wait_for(replay.result(), timeout=1) == first_result
        assert calls == 1
        assert executor._active == {}
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_sqlite_reopen_completed_workflow_replay_is_durable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workflow-terminal.db"
    first_calls = 0
    reopened_calls = 0

    async def first_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal first_calls
        del params
        first_calls += 1
        return _chunks("durable")

    async def reopened_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal reopened_calls
        del params
        reopened_calls += 1
        raise AssertionError("reopened provider must not run")

    first = AgentSDK.for_test(database_path=database, acompletion=first_provider)
    first.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await first.sessions.create(workspaces=[])
        handle = await first.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="sqlite-terminal",
        )
        expected = await handle.result()
        workflow_run_id = handle.workflow_run_id
    finally:
        await first.close()

    reopened = AgentSDK.for_test(database_path=database, acompletion=reopened_provider)
    reopened.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        replay = await reopened.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="sqlite-terminal",
        )
        assert replay.workflow_run_id == workflow_run_id
        assert replay.attached is False
        assert await asyncio.wait_for(replay.result(), timeout=1) == expected
        assert first_calls == 1
        assert reopened_calls == 0
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_reopened_nonterminal_workflow_refuses_result_resume_and_side_effects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workflow-abandoned.db"
    started = asyncio.Event()
    release = asyncio.Event()

    async def first_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        started.set()
        await release.wait()
        return _chunks("never committed")

    first = AgentSDK.for_test(database_path=database, acompletion=first_provider)
    first.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await first.sessions.create(workspaces=[])
        handle = await first.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="abandoned-workflow",
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        handle._task.cancel()  # type: ignore[union-attr]
        await asyncio.gather(handle._task, return_exceptions=True)  # type: ignore[arg-type]
        workflow_run_id = handle.workflow_run_id
    finally:
        release.set()
        await first.close()

    calls = [0, 0]

    def reopened(index: int) -> AgentSDK:
        async def must_not_call(**params: Any) -> AsyncIterator[dict[str, object]]:
            del params
            calls[index] += 1
            raise AssertionError("recovery path must not execute provider")

        sdk = AgentSDK.for_test(database_path=database, acompletion=must_not_call)
        sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
        return sdk

    reopened_sdks = (reopened(0), reopened(1))
    try:
        for sdk in reopened_sdks:
            replay = await sdk.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="abandoned-workflow",
            )
            assert replay.workflow_run_id == workflow_run_id
            assert replay.attached is False
            with pytest.raises(AgentSDKError) as detached:
                await asyncio.wait_for(replay.result(), timeout=1)
            assert detached.value.code is ErrorCode.CONFLICT
            assert detached.value.message == "recovery required"
            assert detached.value.retryable is True

            with pytest.raises(AgentSDKError) as resume:
                await asyncio.wait_for(
                    sdk.workflows.resume(workflow_run_id),
                    timeout=1,
                )
            assert resume.value.code is ErrorCode.CONFLICT
            assert resume.value.message == "recovery required"
            assert resume.value.retryable is True
        assert calls == [0, 0]
    finally:
        await asyncio.gather(*(sdk.close() for sdk in reopened_sdks))


@pytest.mark.asyncio
async def test_second_sdk_cannot_take_over_live_workflow_side_effects() -> None:
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    first_calls = 0
    second_calls = 0

    async def first_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal first_calls
        del params
        first_calls += 1
        first_started.set()
        await first_release.wait()
        return _chunks("done")

    async def second_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal second_calls
        del params
        second_calls += 1
        raise AssertionError("second SDK must not execute")

    store = InMemoryStore()
    first = AgentSDK.for_test(store=store, acompletion=first_provider)
    second = AgentSDK.for_test(store=store, acompletion=second_provider)
    for sdk in (first, second):
        sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await first.sessions.create(workspaces=[])
        original = await first.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="live-owner",
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)

        replay = await second.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="live-owner",
        )
        assert replay.attached is False
        with pytest.raises(AgentSDKError) as detached:
            await asyncio.wait_for(replay.result(), timeout=1)
        assert detached.value.code is ErrorCode.CONFLICT
        assert detached.value.message == "recovery required"
        with pytest.raises(AgentSDKError) as resume:
            await asyncio.wait_for(second.workflows.resume(original.workflow_run_id), timeout=1)
        assert resume.value.code is ErrorCode.CONFLICT
        assert resume.value.message == "recovery required"
        assert first_calls == 1
        assert second_calls == 0

        first_release.set()
        assert (await original.result()).output_text == "done"
    finally:
        first_release.set()
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_close_between_nodes_fails_workflow_detaches_and_closes_session() -> None:
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        if calls == 1:
            first_started.set()
            await first_release.wait()
        return _chunks("done")

    two_nodes = WorkflowDefinition.model_validate(
        {
            "api_version": "agent-sdk/v1",
            "kind": "Workflow",
            "name": "close-between-nodes",
            "nodes": [
                {
                    "id": "first",
                    "kind": "agent",
                    "agent_revision": "worker:1",
                    "input": "first",
                },
                {
                    "id": "second",
                    "kind": "agent",
                    "agent_revision": "worker:1",
                    "input": "second",
                },
            ],
            "edges": [{"source": "first", "target": "second"}],
        }
    )
    sdk = _sdk(provider)
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.workflows.start(session.session_id, two_nodes)
        await asyncio.wait_for(first_started.wait(), timeout=1)
        assert (await sdk.sessions.close(session.session_id)).status is SessionStatus.CLOSING
        first_release.set()

        with pytest.raises(AgentSDKError) as failed:
            await asyncio.wait_for(handle.result(), timeout=1)
        assert failed.value.code is ErrorCode.INVALID_STATE
        snapshot = await sdk.workflows.get(handle.workflow_run_id)
        assert snapshot.status.value == "failed"
        closed = await sdk.sessions.get(session.session_id)
        assert closed.status is SessionStatus.CLOSED
        assert closed.active_workflow_run_ids == ()
        assert calls == 1
    finally:
        first_release.set()
        await sdk.close()


class _WorkflowAttachBarrierStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        self._blocked = False

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if (
            not self._blocked
            and any(event.type == "session.workflow.attached" for event in batch.events)
        ):
            self._blocked = True
            self.ready.set()
            await asyncio.wait_for(self.release.wait(), timeout=2)
        return await super().commit(batch)


@pytest.mark.asyncio
async def test_close_winning_before_workflow_attach_rejects_start_without_writes() -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("unexpected")

    store = _WorkflowAttachBarrierStore()
    starting = AgentSDK.for_test(store=store, acompletion=provider)
    closing = AgentSDK.for_test(store=store, acompletion=provider)
    starting.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    starter: asyncio.Task[Any] | None = None
    try:
        session = await starting.sessions.create(workspaces=[])
        starter = asyncio.create_task(
            starting.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="close-wins",
            )
        )
        await asyncio.wait_for(store.ready.wait(), timeout=1)
        assert (await closing.sessions.close(session.session_id)).status is SessionStatus.CLOSED
        store.release.set()

        with pytest.raises(AgentSDKError) as rejected:
            await asyncio.wait_for(starter, timeout=1)
        assert rejected.value.code is ErrorCode.INVALID_STATE
        assert calls == 0
        events = await store.read_events(after_cursor=0)
        assert not any(event.event.type == "workflow.started" for event in events)
        assert await store.get_idempotency(
            f"session/{session.session_id}/workflow.start",
            "close-wins",
        ) is None
    finally:
        store.release.set()
        if starter is not None and not starter.done():
            starter.cancel()
        if starter is not None:
            await asyncio.gather(starter, return_exceptions=True)
        await starting.close()
        await closing.close()


class _FailWorkflowAttachStore(InMemoryStore):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure
        self.attempts = 0

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(event.type == "session.workflow.attached" for event in batch.events):
            self.attempts += 1
            if self.failure == "precondition":
                raise SnapshotPreconditionError("injected workflow attach race")
            raise RuntimeError("private workflow attach store failure")
        return await super().commit(batch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_attempts", "retryable"),
    [
        ("precondition", ErrorCode.CONFLICT, 8, True),
        ("store", ErrorCode.INTERNAL, 1, False),
    ],
)
async def test_workflow_attach_failure_has_bounded_retry_and_no_partial_state(
    failure: str,
    expected_code: ErrorCode,
    expected_attempts: int,
    retryable: bool,
) -> None:
    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        return _chunks("unexpected")

    store = _FailWorkflowAttachStore(failure)
    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        with pytest.raises(AgentSDKError) as failed:
            await sdk.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="failed-attach",
            )
        assert failed.value.code is expected_code
        assert failed.value.retryable is retryable
        if failure == "store":
            _assert_context_free_sanitizer(
                failed.value,
                secret="private workflow attach store failure",
            )
        assert store.attempts == expected_attempts
        unchanged = await sdk.sessions.get(session.session_id)
        assert unchanged.version == session.version
        assert unchanged.active_workflow_run_ids == ()
        assert await store.get_idempotency(
            f"session/{session.session_id}/workflow.start",
            "failed-attach",
        ) is None
        assert not any(
            event.event.type.startswith("workflow.")
            for event in await store.read_events(after_cursor=0)
        )
    finally:
        await sdk.close()


class _TerminalSessionRaceStore(InMemoryStore):
    def __init__(self, injected_races: int) -> None:
        super().__init__()
        self.injected_races = injected_races
        self.attempts = 0

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if any(
            event.type in {"workflow.completed", "workflow.failed"}
            for event in batch.events
        ):
            self.attempts += 1
            if self.attempts <= self.injected_races:
                session_id = batch.events[0].session_id
                data = await super().get_snapshot("session", session_id)
                assert data is not None
                session = SessionSnapshot.model_validate(data)
                bumped = session.model_copy(update={"version": session.version + 1})
                await super().commit(
                    CommitBatch(
                        events=(),
                        snapshots=(session_write(bumped),),
                        preconditions=(exact_session_precondition(session),),
                    )
                )
        return await super().commit(batch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("injected_races", "succeeds", "expected_attempts"),
    [(1, True, 2), (8, False, 8)],
)
async def test_workflow_terminal_retries_only_session_races_with_bound(
    injected_races: int,
    succeeds: bool,
    expected_attempts: int,
) -> None:
    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        return _chunks("done")

    store = _TerminalSessionRaceStore(injected_races)
    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.workflows.start(session.session_id, WORKFLOW)
        if succeeds:
            assert (await handle.result()).output_text == "done"
            assert (await sdk.sessions.get(session.session_id)).active_workflow_run_ids == ()
        else:
            with pytest.raises(AgentSDKError) as conflict:
                await handle.result()
            assert conflict.value.code is ErrorCode.CONFLICT
            assert conflict.value.retryable is True
            retained = await sdk.sessions.get(session.session_id)
            assert retained.active_workflow_run_ids == (handle.workflow_run_id,)
        assert store.attempts == expected_attempts
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_workflow_terminal_rejects_lost_session_ownership_without_partial_commit() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        started.set()
        await release.wait()
        return _chunks("done")

    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.workflows.start(session.session_id, WORKFLOW)
        await asyncio.wait_for(started.wait(), timeout=1)
        owned = await sdk.sessions.get(session.session_id)
        corrupted = owned.model_copy(
            update={
                "active_workflow_run_ids": (),
                "version": owned.version + 1,
            }
        )
        await store.commit(
            CommitBatch(
                events=(),
                snapshots=(session_write(corrupted),),
                preconditions=(exact_session_precondition(owned),),
            )
        )
        release.set()

        with pytest.raises(AgentSDKError) as conflict:
            await asyncio.wait_for(handle.result(), timeout=1)
        assert conflict.value.code is ErrorCode.CONFLICT
        assert conflict.value.message == "workflow is not owned by session"
        snapshot = await sdk.workflows.get(handle.workflow_run_id)
        assert snapshot.status.value == "running"
        assert snapshot.nodes[0].status.value == "completed"
        assert not any(
            event.event.type == "workflow.completed"
            for event in await store.read_events(after_cursor=0)
        )
    finally:
        release.set()
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("idempotency_key", ["", "x" * 257])
async def test_invalid_workflow_key_is_public_invalid_state_without_writes(
    idempotency_key: str,
) -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("unexpected")

    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        with pytest.raises(AgentSDKError) as invalid:
            await sdk.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key=idempotency_key,
            )
        assert invalid.value.code is ErrorCode.INVALID_STATE
        assert invalid.value.message == "idempotency key is invalid"
        _assert_context_free_sanitizer(
            invalid.value,
            secret=idempotency_key or "idempotency text",
        )
        assert calls == 0
        assert (await sdk.sessions.get(session.session_id)).active_workflow_run_ids == ()
        assert not any(
            event.event.type.startswith("workflow.")
            for event in await store.read_events(after_cursor=0)
        )
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("idempotency_key", ["", "x" * 257])
async def test_invalid_workflow_key_precedes_missing_session(
    idempotency_key: str,
) -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("unexpected")

    sdk = _sdk(provider)
    try:
        with pytest.raises(AgentSDKError) as invalid:
            await sdk.workflows.start(
                "ses_missing",
                WORKFLOW,
                idempotency_key=idempotency_key,
            )
        assert invalid.value.code is ErrorCode.INVALID_STATE
        assert invalid.value.message == "idempotency key is invalid"
        _assert_context_free_sanitizer(
            invalid.value,
            secret=idempotency_key or "idempotency text",
        )
        assert calls == 0
    finally:
        await sdk.close()


class _InvalidWorkflowKeyAccessTrap(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.access_count = 0

    def _record(self) -> None:
        if self.armed:
            self.access_count += 1
            raise AssertionError("invalid workflow key reached Store")

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        self._record()
        return await super().get_snapshot(kind, entity_id)

    async def get_idempotency(self, scope: str, key: str):
        self._record()
        return await super().get_idempotency(scope, key)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        self._record()
        return await super().commit(batch)


@pytest.mark.asyncio
@pytest.mark.parametrize("idempotency_key", ["", "x" * 257])
async def test_invalid_workflow_key_is_rejected_before_any_store_access(
    idempotency_key: str,
) -> None:
    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        return _chunks("unexpected")

    store = _InvalidWorkflowKeyAccessTrap()
    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        store.armed = True
        with pytest.raises(AgentSDKError) as invalid:
            await sdk.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key=idempotency_key,
            )
        assert invalid.value.code is ErrorCode.INVALID_STATE
        assert invalid.value.message == "idempotency key is invalid"
        _assert_context_free_sanitizer(
            invalid.value,
            secret=idempotency_key or "idempotency text",
        )
        assert store.access_count == 0
    finally:
        store.armed = False
        await sdk.close()


class _WorkflowHintFailureStore(InMemoryStore):
    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self.failure = failure

    async def get_idempotency(self, scope: str, key: str):
        del scope, key
        raise self.failure


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [False, True])
async def test_workflow_hint_failure_is_sanitized_and_never_executes(typed: bool) -> None:
    secret = "private workflow hint failure"
    original: BaseException
    if typed:
        original = IdempotencyCorruptionError(secret)
    else:
        original = RuntimeError(secret)
    store = _WorkflowHintFailureStore(original)
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("unexpected")

    sdk = AgentSDK.for_test(store=store, acompletion=provider)
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await sdk.sessions.create(workspaces=[])
        with pytest.raises(AgentSDKError) as failed:
            await sdk.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="hint-failure",
            )
        assert failed.value.code is ErrorCode.INTERNAL
        assert failed.value.retryable is False
        _assert_context_free_sanitizer(
            failed.value,
            secret=secret,
            original=original,
        )
        assert calls == 0
        assert (await sdk.sessions.get(session.session_id)).active_workflow_run_ids == ()
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_corrupt_sqlite_workflow_replay_result_is_sanitized_without_execution(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corrupt-workflow-result.db"

    async def first_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        del params
        return _chunks("done")

    first = AgentSDK.for_test(database_path=database, acompletion=first_provider)
    first.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        session = await first.sessions.create(workspaces=[])
        handle = await first.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="corrupt-result",
        )
        await handle.result()
    finally:
        await first.close()

    secret = "private-corrupt-workflow-result"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT result_json FROM idempotency_records WHERE key = ?",
            ("corrupt-result",),
        ).fetchone()
        assert row is not None
        result = json.loads(row[0])
        result["execution_descriptor"]["descriptor_hash"] = secret
        connection.execute(
            "UPDATE idempotency_records SET result_json = ? WHERE key = ?",
            (json.dumps(result), "corrupt-result"),
        )
        connection.commit()

    calls = 0

    async def reopened_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("unexpected")

    reopened = AgentSDK.for_test(database_path=database, acompletion=reopened_provider)
    reopened.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    try:
        with pytest.raises(AgentSDKError) as corrupt:
            await reopened.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="corrupt-result",
            )
        assert corrupt.value.code is ErrorCode.INTERNAL
        assert corrupt.value.retryable is False
        _assert_context_free_sanitizer(corrupt.value, secret=secret)
        assert calls == 0
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_valid_foreign_workflow_replay_result_is_rejected_and_sanitized(
    tmp_path: Path,
) -> None:
    database = tmp_path / "substituted-workflow-result.db"
    secret = "private-substituted-workflow-b-secret"
    tool_calls = 0

    async def tool_handler(_: ToolContext, **values: object) -> object:
        nonlocal tool_calls
        tool_calls += 1
        return values

    tool_spec = ToolSpec(
        name="unused",
        description="must remain unused",
        input_schema={"type": "object"},
    )
    workflow_b = WorkflowDefinition.model_validate(
        {
            "api_version": "agent-sdk/v1",
            "kind": "Workflow",
            "name": "foreign-workflow-b",
            "nodes": [
                {
                    "id": "foreign",
                    "kind": "agent",
                    "agent_revision": "foreign:1",
                    "input": secret,
                }
            ],
            "edges": [],
        }
    )

    async def first_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        return _chunks(f"done:{params['model']}")

    first = AgentSDK.for_test(database_path=database, acompletion=first_provider)
    first.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    first.agents.define(
        AgentSpec(
            name="foreign",
            revision="1",
            model="fake/foreign",
            model_params={"private": secret},
        )
    )
    first.tools.register(tool_spec, tool_handler)
    try:
        session = await first.sessions.create(workspaces=[])
        workflow_a_handle = await first.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="workflow-a-key",
        )
        await workflow_a_handle.result()
        workflow_b_handle = await first.workflows.start(
            session.session_id,
            workflow_b,
            idempotency_key="workflow-b-key",
        )
        await workflow_b_handle.result()
        workflow_b_snapshot = await first.workflows.get(
            workflow_b_handle.workflow_run_id
        )
    finally:
        await first.close()

    with sqlite3.connect(database) as connection:
        before_events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        before_runs = connection.execute(
            "SELECT COUNT(*) FROM snapshots WHERE kind = 'run'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE idempotency_records SET result_json = ? WHERE key = ?",
            (
                json.dumps(workflow_b_snapshot.model_dump(mode="json")),
                "workflow-a-key",
            ),
        )
        connection.commit()

    provider_calls = 0
    async def reopened_provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        del params
        provider_calls += 1
        return _chunks("unexpected")

    reopened = AgentSDK.for_test(database_path=database, acompletion=reopened_provider)
    reopened.agents.define(
        AgentSpec(name="worker", revision="1", model="fake/worker")
    )
    reopened.agents.define(
        AgentSpec(
            name="foreign",
            revision="1",
            model="fake/foreign",
            model_params={"private": secret},
        )
    )
    reopened.tools.register(tool_spec, tool_handler)
    try:
        with pytest.raises(AgentSDKError) as substituted:
            await reopened.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="workflow-a-key",
            )
        assert substituted.value.code is ErrorCode.INTERNAL
        assert substituted.value.retryable is False
        _assert_context_free_sanitizer(substituted.value, secret=secret)
        assert provider_calls == 0
        assert tool_calls == 0
    finally:
        await reopened.close()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events
        assert connection.execute(
            "SELECT COUNT(*) FROM snapshots WHERE kind = 'run'"
        ).fetchone()[0] == before_runs


@pytest.mark.asyncio
async def test_failed_workflow_replay_is_detached_durable_and_context_free() -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        raise RuntimeError("private durable workflow provider failure")

    sdk = _sdk(provider)
    executor = sdk.workflows._executor  # type: ignore[attr-defined]
    try:
        session = await sdk.sessions.create(workspaces=[])
        first = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="failed-durable",
        )
        with pytest.raises(AgentSDKError) as original:
            await first.result()
        _assert_context_free_sanitizer(
            original.value,
            secret="private durable workflow provider failure",
        )
        await asyncio.sleep(0)
        assert executor._active == {}

        replay = await sdk.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="failed-durable",
        )
        assert replay.attached is False
        with pytest.raises(AgentSDKError) as durable:
            await asyncio.wait_for(replay.result(), timeout=1)
        assert durable.value.code == original.value.code
        assert durable.value.message == original.value.message
        _assert_context_free_sanitizer(
            durable.value,
            secret="private durable workflow provider failure",
        )
        assert calls == 1
        assert executor._active == {}
    finally:
        await sdk.close()


class _LegacyWorkflowStoreGuard(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.accesses = 0

    def _record(self) -> None:
        if self.armed:
            self.accesses += 1

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        self._record()
        return await super().get_snapshot(kind, entity_id)

    async def get_idempotency(self, scope: str, key: str):
        self._record()
        return await super().get_idempotency(scope, key)

    async def commit(self, batch: CommitBatch) -> CommitResult:
        self._record()
        return await super().commit(batch)


@pytest.mark.asyncio
async def test_legacy_workflow_key_is_rejected_before_store_access() -> None:
    store = _LegacyWorkflowStoreGuard()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    workflow = WorkflowCompiler().compile(WORKFLOW)
    store.armed = True

    with pytest.raises(AgentSDKError) as rejected:
        await WorkflowState(store).create(
            session.session_id,
            workflow,
            idempotency_key="legacy-key",
        )
    assert rejected.value.code is ErrorCode.INVALID_STATE
    assert rejected.value.message == "legacy workflow cannot use idempotency"
    assert store.accesses == 0


class _WorkflowReplayDeleteRaceStore(_RetainDeletingStore):
    def __init__(self) -> None:
        super().__init__()
        self.block_replay = False
        self.replay_ready = asyncio.Event()
        self.release_replay = asyncio.Event()

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if (
            self.block_replay
            and isinstance(batch.idempotency, IdempotencyReplay)
            and batch.idempotency.scope.endswith("/workflow.start")
        ):
            self.replay_ready.set()
            await asyncio.wait_for(self.release_replay.wait(), timeout=2)
        return await super().commit(batch)


@pytest.mark.asyncio
async def test_workflow_replay_losing_to_delete_reloads_deleting_before_hint() -> None:
    calls = 0

    async def provider(**params: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal calls
        del params
        calls += 1
        return _chunks("done")

    store = _WorkflowReplayDeleteRaceStore()
    replaying = AgentSDK.for_test(store=store, acompletion=provider)
    deleting = AgentSDK.for_test(store=store, acompletion=provider)
    replaying.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    replay: asyncio.Task[Any] | None = None
    try:
        session = await replaying.sessions.create(workspaces=[])
        first = await replaying.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="delete-race",
        )
        await first.result()
        await replaying.sessions.close(session.session_id)

        store.block_replay = True
        replay = asyncio.create_task(
            replaying.workflows.start(
                session.session_id,
                WORKFLOW,
                idempotency_key="delete-race",
            )
        )
        await asyncio.wait_for(store.replay_ready.wait(), timeout=1)
        with pytest.raises(AgentSDKError):
            await deleting.sessions.delete(session.session_id)
        store.release_replay.set()

        with pytest.raises(AgentSDKError) as rejected:
            await asyncio.wait_for(replay, timeout=1)
        assert rejected.value.code is ErrorCode.INVALID_STATE
        assert rejected.value.message == "session is deleting"
        assert calls == 1
    finally:
        store.release_replay.set()
        if replay is not None and not replay.done():
            replay.cancel()
        if replay is not None:
            await asyncio.gather(replay, return_exceptions=True)
        await replaying.close()
        await deleting.close()


class _DetachedWorkflowLoadFailureStore(InMemoryStore):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.error = RuntimeError("private detached workflow Store failure")

    async def get_snapshot(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        del kind, entity_id
        if self.mode == "store":
            raise self.error
        return {"workflow_run_id": "private detached workflow validation failure"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["store", "validation"])
async def test_detached_workflow_load_failure_is_context_free(mode: str) -> None:
    store = _DetachedWorkflowLoadFailureStore(mode)
    handle = WorkflowHandle("wfr_detached", store, None)
    secret = (
        "private detached workflow Store failure"
        if mode == "store"
        else "private detached workflow validation failure"
    )

    with pytest.raises(AgentSDKError) as failed:
        await handle.result()
    assert failed.value.code is ErrorCode.INTERNAL
    _assert_context_free_sanitizer(
        failed.value,
        secret=secret,
        original=store.error if mode == "store" else None,
    )
