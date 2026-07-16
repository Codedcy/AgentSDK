from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import types as mcp_types

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ReconciliationAction,
    RunStatus,
    SessionBusyError,
    ToolContext,
    ToolRetryPolicy,
    ToolSpec,
)
from agent_sdk.mcp import MCPManager, MCPServerConfig, StdioMCPTransport
from agent_sdk.runtime.reconciliation import RunCheckpointPhase
from agent_sdk.storage.base import CommitResult, RunProgressBatch
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools.models import ToolResult
from agent_sdk.workflow import WorkflowNodeStatus, WorkflowRunStatus


_HARD_EXIT = 86
_CHILD_TIMEOUT_SECONDS = 15
_AGENT = AgentSpec(name="fault-agent", revision="1", model="fault/provider")
_WORKFLOW = {
    "api_version": "agent-sdk/v1",
    "kind": "Workflow",
    "name": "fault-recovery",
    "nodes": [
        {
            "id": "recover",
            "kind": "agent",
            "agent_revision": "fault-agent:1",
            "input": "recover after an external side effect",
        }
    ],
    "edges": [],
}


def _tool(*, source: str = "application") -> ToolSpec:
    return ToolSpec(
        name="external_lookup",
        description="Perform one externally visible lookup",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        version="1",
        source=source,
        effects=("external",),
        retry_policy=ToolRetryPolicy.NEVER,
    )


def _append_record(path: Path, **record: object) -> None:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _record_values(path: Path, key: str) -> list[object]:
    return [record[key] for record in _records(path) if key in record]


class _BoundaryObservingStore:
    def __init__(
        self,
        delegate: SQLiteStore,
        control_path: Path,
        effect_path: Path,
        *,
        exit_after_safe_tool: bool,
    ) -> None:
        self._delegate = delegate
        self._control_path = control_path
        self._effect_path = effect_path
        self._exit_after_safe_tool = exit_after_safe_tool

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        result = await self._delegate.commit_run_progress(batch)
        event_types = {event.type for event in batch.events}
        if "model.call.started" in event_types or "tool.call.started" in event_types:
            operation = None if batch.operation is None else batch.operation.updated
            _append_record(
                self._control_path,
                run_id=batch.lease.run_id,
                operation_id=(None if operation is None else operation.operation_id),
                operation_kind=(
                    None if operation is None else operation.operation_kind.value
                ),
            )
        if self._exit_after_safe_tool and "tool.call.completed" in event_types:
            _append_record(self._effect_path, effect="safe_tool_outcome_committed")
            os._exit(_HARD_EXIT)
        return result


class _PauseAfterSafeToolStore:
    def __init__(self) -> None:
        self._delegate = InMemoryStore()
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def commit_run_progress(self, batch: RunProgressBatch) -> CommitResult:
        result = await self._delegate.commit_run_progress(batch)
        if any(event.type == "tool.call.completed" for event in batch.events):
            self.reached.set()
            await self.release.wait()
        return result


class _FaultMCPSession:
    def __init__(self, effect_path: Path, label: str, *, hard_exit: bool) -> None:
        self._effect_path = effect_path
        self._label = label
        self._hard_exit = hard_exit
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> Any:
        return type("InitializeResult", (), {"protocolVersion": "2025-11-25"})()

    async def list_tools(
        self,
        cursor: str | None = None,
    ) -> mcp_types.ListToolsResult:
        assert cursor is None
        return mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(
                    name="external_lookup",
                    description="Perform one externally visible lookup",
                    inputSchema={
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                )
            ]
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        **_: Any,
    ) -> mcp_types.CallToolResult:
        self.calls.append((name, dict(arguments or {})))
        _append_record(self._effect_path, effect=self._label)
        if self._hard_exit:
            os._exit(_HARD_EXIT)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="remote-ok")]
        )

    def connector(self, _: MCPServerConfig) -> Any:
        session = self

        @asynccontextmanager
        async def connected() -> AsyncIterator[_FaultMCPSession]:
            yield session

        return connected()


def _mcp_config() -> MCPServerConfig:
    return MCPServerConfig(
        name="fault",
        transport=StdioMCPTransport(command="ignored"),
    )


async def _tool_call_completion(
    gate: asyncio.Event,
    *,
    tool_name: str = "external_lookup",
) -> AsyncIterator[dict[str, object]]:
    await gate.wait()

    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_fault_1",
                                "function": {
                                    "name": tool_name,
                                    "arguments": '{"value":7}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    return chunks()


async def _child_main(
    scenario: str,
    database_path: Path,
    effect_path: Path,
    control_path: Path,
) -> None:
    gate = asyncio.Event()
    delegate = await SQLiteStore.open(database_path)
    store: Any = _BoundaryObservingStore(
        delegate,
        control_path,
        effect_path,
        exit_after_safe_tool=scenario in {"safe_tool", "safe_tool_workflow"},
    )

    if scenario == "provider_unknown":

        async def completion(**_: object) -> Any:
            await gate.wait()
            _append_record(effect_path, effect="provider_accepted")
            os._exit(_HARD_EXIT)

    else:

        async def completion(**_: object) -> Any:
            return await _tool_call_completion(
                gate,
                tool_name=(
                    "mcp.fault.external_lookup"
                    if scenario == "mcp_unknown"
                    else "external_lookup"
                ),
            )

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=completion,
        permission_default="allow",
    )
    sdk.agents.define(_AGENT)

    async def handler(_: ToolContext, value: int) -> object:
        assert value == 7
        if scenario == "tool_workflow_unknown":
            _append_record(effect_path, effect="application:side_effect")
            os._exit(_HARD_EXIT)
        return {"value": value + 1}

    if scenario == "mcp_unknown":
        mcp_session = _FaultMCPSession(
            effect_path,
            "mcp_session:side_effect",
            hard_exit=True,
        )
        mcp_manager = MCPManager._for_test(sdk.tools, mcp_session.connector)
        await mcp_manager.connect(_mcp_config())
    else:
        sdk.tools.register(_tool(), handler)
    session = await sdk.sessions.create(workspaces=[])
    if scenario in {"tool_workflow_unknown", "safe_tool_workflow"}:
        handle = await sdk.workflows.start(
            session.session_id,
            yaml.safe_dump(_WORKFLOW, sort_keys=False),
        )
        _append_record(
            control_path,
            session_id=session.session_id,
            workflow_run_id=handle.workflow_run_id,
        )
    else:
        handle = await sdk.runs.start(session.session_id, _AGENT, "fault recovery")
        _append_record(
            control_path,
            session_id=session.session_id,
            run_id=handle.run_id,
        )
    gate.set()
    await handle.result()
    raise AssertionError("fault child reached a graceful terminal state")


def _launch_child(
    tmp_path: Path,
    scenario: str,
) -> tuple[Path, Path, dict[str, object], subprocess.CompletedProcess[str]]:
    database_path = tmp_path / f"{scenario}.sqlite3"
    effect_path = tmp_path / f"{scenario}.effects.jsonl"
    control_path = tmp_path / f"{scenario}.control.jsonl"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            scenario,
            str(database_path),
            str(effect_path),
            str(control_path),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == _HARD_EXIT, (
        f"child exited {completed.returncode}\nstdout:\n{completed.stdout}"
        f"\nstderr:\n{completed.stderr}"
    )
    merged: dict[str, object] = {}
    for record in _records(control_path):
        merged.update(record)
    assert "run_id" in merged
    return database_path, effect_path, merged, completed


def _advance_scanner(sdk: AgentSDK) -> None:
    sdk._recovery_scanner._clock = (  # type: ignore[attr-defined]
        lambda: datetime.now(UTC) + timedelta(hours=1)
    )


async def _final_completion(effect_path: Path, label: str) -> Any:
    _append_record(effect_path, effect=label)

    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [{"delta": {"content": "recovered"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    return chunks()


@pytest.mark.asyncio
async def test_interrupted_after_safe_tool_batch_resumes_without_step_marker() -> None:
    store = _PauseAfterSafeToolStore()
    calls = 0

    async def initial_completion(**_: object) -> Any:
        gate = asyncio.Event()
        gate.set()
        return await _tool_call_completion(gate)

    async def handler(_: ToolContext, value: int) -> object:
        nonlocal calls
        calls += 1
        return value + 1

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=initial_completion,
        permission_default="allow",
    )
    sdk.tools.register(_tool(), handler)
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(session.session_id, _AGENT, "safe batch")
    await asyncio.wait_for(store.reached.wait(), timeout=1)
    handle._task.cancel()  # type: ignore[attr-defined]
    store.release.set()
    with pytest.raises(AgentSDKError):
        await handle.result()
    await sdk.close()

    async def final_completion(**_: object) -> Any:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {"delta": {"content": "recovered"}, "finish_reason": "stop"}
                ]
            }

        return chunks()

    reopened = AgentSDK.for_test(
        store=store,
        acompletion=final_completion,
        permission_default="allow",
    )
    reopened.agents.define(_AGENT)
    reopened.tools.register(_tool(), handler)
    try:
        await reopened.recovery.scan()
        result = await (await reopened.recovery.recover_run(handle.run_id)).result()
        assert result.output_text == "recovered"
        assert calls == 1
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_provider_accept_hard_exit_requires_explicit_decision_without_replay(
    tmp_path: Path,
) -> None:
    database_path, effects, control, _child = _launch_child(
        tmp_path,
        "provider_unknown",
    )
    run_id = str(control["run_id"])
    session_id = str(control["session_id"])
    sdk = AgentSDK.for_test(
        database_path=database_path,
        acompletion=lambda **_: _final_completion(effects, "provider_after_decision"),
        permission_default="allow",
    )
    sdk.agents.define(_AGENT)

    async def unused_tool(_: ToolContext, value: int) -> object:
        raise AssertionError(f"Provider-only recovery invoked Tool with {value}")

    sdk.tools.register(_tool(), unused_tool)
    _advance_scanner(sdk)
    try:
        await sdk.recovery.scan()
        assert (await sdk.runs.get(run_id)).status is RunStatus.INTERRUPTED

        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        requests = await sdk.recovery.pending_requests(run_id)
        assert len(requests) == 1
        assert _record_values(effects, "effect") == ["provider_accepted"]

        assert (await sdk.sessions.close(session_id)).status.value == "closing"
        with pytest.raises(SessionBusyError):
            await sdk.sessions.delete(session_id)

        await sdk.recovery.resolve(
            requests[0].request_id,
            ReconciliationAction.CONFIRM_NOT_EXECUTED,
            actor={"type": "operator", "id": "fault-test"},
            evidence={"disposition": "not_executed"},
        )
        assert _record_values(effects, "effect") == ["provider_accepted"]

        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert result.output_text == "recovered"
        assert _record_values(effects, "effect") == [
            "provider_accepted",
            "provider_after_decision",
        ]
        assert (await sdk.sessions.get(session_id)).active_run_ids == ()
        await sdk.sessions.delete(session_id)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_tool_side_effect_hard_exit_projects_workflow_without_replay(
    tmp_path: Path,
) -> None:
    database_path, effects, control, _child = _launch_child(
        tmp_path,
        "tool_workflow_unknown",
    )
    run_id = str(control["run_id"])
    workflow_run_id = str(control["workflow_run_id"])

    async def duplicate_tool(_: ToolContext, value: int) -> object:
        _append_record(effects, effect="application:duplicate_tool")
        return value + 1

    sdk = AgentSDK.for_test(
        database_path=database_path,
        acompletion=lambda **_: _final_completion(effects, "workflow_final_model"),
        permission_default="allow",
    )
    sdk.agents.define(_AGENT)
    sdk.tools.register(_tool(), duplicate_tool)
    _advance_scanner(sdk)
    try:
        await sdk.recovery.scan()
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]
        assert _record_values(effects, "effect") == ["application:side_effect"]

        before = await sdk.workflows.get(workflow_run_id)
        assert before.status is WorkflowRunStatus.RUNNING
        assert before.nodes[0].status is WorkflowNodeStatus.RUNNING
        tool_result = ToolResult.succeeded(
            "call_fault_1",
            "external_lookup",
            {"confirmed": True},
        )
        await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.CONFIRM_COMPLETED,
            actor={"type": "operator", "id": "fault-test"},
            evidence={"tool_result": tool_result.model_dump(mode="json")},
        )
        assert await sdk.workflows.get(workflow_run_id) == before
        assert _record_values(effects, "effect") == ["application:side_effect"]

        recovered = await (
            await sdk.recovery.recover_workflow(workflow_run_id)
        ).result()
        assert recovered.status is WorkflowRunStatus.COMPLETED
        assert recovered.nodes[0].status is WorkflowNodeStatus.COMPLETED
        assert _record_values(effects, "effect") == [
            "application:side_effect",
            "workflow_final_model",
        ]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_mcp_side_effect_hard_exit_waits_for_explicit_retry(
    tmp_path: Path,
) -> None:
    database_path, effects, control, _child = _launch_child(
        tmp_path,
        "mcp_unknown",
    )
    run_id = str(control["run_id"])

    sdk = AgentSDK.for_test(
        database_path=database_path,
        acompletion=lambda **_: _final_completion(effects, "mcp_final_model"),
        permission_default="allow",
    )
    sdk.agents.define(_AGENT)
    _advance_scanner(sdk)
    mcp_session = _FaultMCPSession(
        effects,
        "mcp_session:explicit_retry",
        hard_exit=False,
    )
    mcp_manager = MCPManager._for_test(sdk.tools, mcp_session.connector)
    await mcp_manager.connect(_mcp_config())
    try:
        await sdk.recovery.scan()
        waiting = await sdk.recovery.recover_run(run_id)
        with pytest.raises(AgentSDKError, match="recovery required"):
            await waiting.result()
        request = (await sdk.recovery.pending_requests(run_id))[0]
        assert _record_values(effects, "effect") == ["mcp_session:side_effect"]
        assert mcp_session.calls == []

        await sdk.recovery.resolve(
            request.request_id,
            ReconciliationAction.RETRY,
            actor={"type": "operator", "id": "fault-test"},
            evidence={"acknowledge_duplicate_side_effect_risk": True},
        )
        assert _record_values(effects, "effect") == ["mcp_session:side_effect"]
        assert mcp_session.calls == []

        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert result.output_text == "recovered"
        assert _record_values(effects, "effect") == [
            "mcp_session:side_effect",
            "mcp_session:explicit_retry",
            "mcp_final_model",
        ]
        assert mcp_session.calls == [("external_lookup", {"value": 7})]
    finally:
        await sdk.close()
        await mcp_manager.close()


@pytest.mark.asyncio
async def test_safe_tool_commit_hard_exit_resumes_without_repeating_tool(
    tmp_path: Path,
) -> None:
    database_path, effects, control, _child = _launch_child(tmp_path, "safe_tool")
    run_id = str(control["run_id"])
    tool_calls = 0
    inspection = await SQLiteStore.open(database_path)
    try:
        checkpoint = await inspection.get_run_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.phase is RunCheckpointPhase.READY_FOR_MODEL
    finally:
        await inspection.close()

    async def duplicate_tool(_: ToolContext, value: int) -> object:
        nonlocal tool_calls
        tool_calls += 1
        return value + 1

    sdk = AgentSDK.for_test(
        database_path=database_path,
        acompletion=lambda **_: _final_completion(effects, "safe_resume_model"),
        permission_default="allow",
    )
    sdk.agents.define(_AGENT)
    sdk.tools.register(_tool(), duplicate_tool)
    _advance_scanner(sdk)
    try:
        await sdk.recovery.scan()
        assert (await sdk.runs.get(run_id)).status is RunStatus.INTERRUPTED

        result = await (await sdk.recovery.recover_run(run_id)).result()
        assert result.output_text == "recovered"
        assert tool_calls == 0
        assert _record_values(effects, "effect") == [
            "safe_tool_outcome_committed",
            "safe_resume_model",
        ]
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_safe_tool_commit_hard_exit_completes_workflow_projection(
    tmp_path: Path,
) -> None:
    database_path, effects, control, _child = _launch_child(
        tmp_path,
        "safe_tool_workflow",
    )
    workflow_run_id = str(control["workflow_run_id"])
    tool_calls = 0

    async def duplicate_tool(_: ToolContext, value: int) -> object:
        nonlocal tool_calls
        tool_calls += 1
        return value + 1

    sdk = AgentSDK.for_test(
        database_path=database_path,
        acompletion=lambda **_: _final_completion(effects, "safe_workflow_model"),
        permission_default="allow",
    )
    sdk.agents.define(_AGENT)
    sdk.tools.register(_tool(), duplicate_tool)
    _advance_scanner(sdk)
    try:
        await sdk.recovery.scan()
        result = await (
            await sdk.recovery.recover_workflow(workflow_run_id)
        ).result()
        assert result.status is WorkflowRunStatus.COMPLETED
        assert result.nodes[0].status is WorkflowNodeStatus.COMPLETED
        assert result.output_text == "recovered"
        assert tool_calls == 0
        assert _record_values(effects, "effect") == [
            "safe_tool_outcome_committed",
            "safe_workflow_model",
        ]
    finally:
        await sdk.close()


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "--child":
        raise SystemExit("invalid fault-child invocation")
    asyncio.run(
        _child_main(
            sys.argv[2],
            Path(sys.argv[3]),
            Path(sys.argv[4]),
            Path(sys.argv[5]),
        )
    )
