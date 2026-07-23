from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSpec,
    AgentSDKError,
    ErrorCode,
    PermissionDecision,
    PermissionRequest,
    RunResult,
    TokenUsage,
    ToolResult,
    TraceStage,
    TraceStageKind,
    TraceStageStatus,
    TraceTimeline,
)
from agent_sdk.permissions import PermissionRule
from agent_sdk.storage.memory import InMemoryStore
from examples.quickstart_agent import (
    build_parser,
    define_agent,
    execute_turn,
    run_chat,
    select_session,
    summarize_run,
)


async def _unexpected_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
    raise AssertionError("provider must not be called")


def test_quickstart_uses_only_public_agent_sdk_imports() -> None:
    path = Path(__file__).parents[3] / "examples" / "quickstart_agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("agent_sdk")
    }
    assert modules <= {"agent_sdk", "agent_sdk.permissions"}


def test_parser_supplies_documented_defaults() -> None:
    args = build_parser().parse_args([])

    assert args.model == "openai/gpt-4o-mini"
    assert args.database == Path(".agent-sdk/quickstart.db")
    assert args.workspace == Path(".")
    assert args.session_id is None


@pytest.mark.asyncio
async def test_select_session_creates_then_reopens_same_session(
    tmp_path: Path,
) -> None:
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unexpected_provider,
    )
    try:
        created = await select_session(sdk, tmp_path, None)
        reopened = await select_session(sdk, tmp_path, created.session_id)

        assert reopened.session_id == created.session_id
        assert reopened.workspaces == (str(tmp_path.resolve()),)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_general_agent_exposes_only_workspace_tools(tmp_path: Path) -> None:
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unexpected_provider,
    )
    try:
        agent = define_agent(sdk, "fake/general")

        assert agent.name == "quickstart"
        assert agent.model == "fake/general"
        assert agent.system_prompt is None
        assert agent.tool_allowlist == ("read", "write", "bash")
    finally:
        await sdk.close()


def _text_stream(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [{"delta": {"content": text}, "finish_reason": "stop"}]
        }
        yield {
            "choices": [],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        }

    return chunks()


def _tool_stream(
    name: str,
    arguments: dict[str, object],
) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "quickstart-call",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    return chunks()


class WriteThenAnswerProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_: Any) -> object:
        self.calls += 1
        if self.calls == 1:
            return _tool_stream(
                "write",
                {"path": "note.txt", "content": "hello"},
            )
        return _text_stream("finished")


class ConversationProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[tuple[dict[str, Any], ...]] = []

    async def __call__(self, **params: Any) -> object:
        self.calls += 1
        self.requests.append(tuple(dict(item) for item in params["messages"]))
        return _text_stream(f"answer-{self.calls}")


@pytest.mark.asyncio
async def test_run_chat_keeps_multiple_turns_in_one_session(
    tmp_path: Path,
) -> None:
    provider = ConversationProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
    )
    inputs = iter(("first question", "second question", "exit"))
    output: list[str] = []

    async def read_line(_: str) -> str:
        return next(inputs)

    async def deny(_: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.deny("not needed")

    try:
        session = await sdk.sessions.create(workspaces=(tmp_path,))
        agent = define_agent(sdk, "fake/general")
        await run_chat(
            sdk,
            session.session_id,
            agent,
            read_line=read_line,
            write_line=output.append,
            resolve_permission=deny,
        )

        assert provider.calls == 2
        assert any("answer-1" in str(message) for message in provider.requests[1])
        assert any("answer-1" in line for line in output)
        assert any("answer-2" in line for line in output)
        assert sum(line.startswith("Run ") for line in output) == 2
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_execute_turn_resolves_asked_write_and_summarizes_trace(
    tmp_path: Path,
) -> None:
    provider = WriteThenAnswerProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
        permission_rules=(
            PermissionRule(
                outcome="ask",
                tool="write",
                path_prefix=tmp_path.resolve(),
            ),
        ),
    )
    requests: list[PermissionRequest] = []

    async def allow(request: PermissionRequest) -> PermissionDecision:
        requests.append(request)
        return PermissionDecision.allow_once()

    try:
        session = await sdk.sessions.create(workspaces=(tmp_path,))
        agent = sdk.agents.define(
            AgentSpec(
                name="quickstart",
                model="fake/general",
                tool_allowlist=("read", "write", "bash"),
            )
        )

        result = await execute_turn(
            sdk,
            session.session_id,
            agent,
            "write a note",
            resolve_permission=allow,
        )
        summary = await summarize_run(sdk, result)

        assert result.output_text == "finished"
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello"
        assert [request.tool_name for request in requests] == ["write"]
        assert summary.run_id == result.run_id
        assert summary.total_tokens == 3
        assert summary.tools == ("write",)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_execute_turn_can_deny_write(tmp_path: Path) -> None:
    provider = WriteThenAnswerProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
    )

    async def deny(_: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.deny("not this time")

    try:
        session = await sdk.sessions.create(workspaces=(tmp_path,))
        agent = define_agent(sdk, "fake/general")
        result = await execute_turn(
            sdk,
            session.session_id,
            agent,
            "write a note",
            resolve_permission=deny,
        )

        assert result.output_text == "finished"
        assert not (tmp_path / "note.txt").exists()
        assert result.tool_results[0].status.value == "denied"
    finally:
        await sdk.close()


def _run_result() -> RunResult:
    return RunResult(
        run_id="quickstart-run",
        output_text="finished",
        usage=TokenUsage(total_tokens=3),
    )


class _ControlledRunHandle:
    def __init__(self, result: RunResult | None = None) -> None:
        loop = asyncio.get_running_loop()
        self.run_id = "quickstart-run"
        self._completion: asyncio.Future[RunResult] = loop.create_future()
        if result is not None:
            self._completion.set_result(result)
        self.result_started = asyncio.Event()
        self.result_cancelled = asyncio.Event()
        self.result_finished = asyncio.Event()

    def finish(self, result: RunResult) -> None:
        if not self._completion.done():
            self._completion.set_result(result)

    async def result(self) -> RunResult:
        self.result_started.set()
        try:
            return await self._completion
        except asyncio.CancelledError:
            self.result_cancelled.set()
            raise
        finally:
            self.result_finished.set()


class _ControlledRuns:
    def __init__(self, handle: _ControlledRunHandle) -> None:
        self._handle = handle

    async def start(self, *_: object) -> _ControlledRunHandle:
        return self._handle


class _ControlledPermissions:
    def __init__(
        self,
        request: PermissionRequest | None = None,
        *,
        resolve_error: Exception | None = None,
    ) -> None:
        self._request = request
        self._resolve_error = resolve_error
        self.next_started = asyncio.Event()
        self.resolved = asyncio.Event()
        self.resolutions: list[tuple[str, PermissionDecision]] = []
        self._delivery = asyncio.Event()

    async def next_request(self, _: str) -> PermissionRequest:
        self.next_started.set()
        await self._delivery.wait()
        assert self._request is not None
        return self._request

    async def resolve(
        self,
        request_id: str,
        decision: PermissionDecision,
    ) -> None:
        self.resolutions.append((request_id, decision))
        self.resolved.set()
        if self._resolve_error is not None:
            raise self._resolve_error

    def deliver(self) -> None:
        self._delivery.set()


class _ControlledSDK:
    def __init__(
        self,
        handle: _ControlledRunHandle,
        permissions: _ControlledPermissions,
    ) -> None:
        self.runs = _ControlledRuns(handle)
        self.permissions = permissions


def _permission_request() -> PermissionRequest:
    return PermissionRequest(
        request_id="quickstart-request",
        run_id="quickstart-run",
        session_id="quickstart-session",
        tool_name="write",
        arguments={},
    )


@pytest.mark.asyncio
async def test_execute_turn_cancellation_before_permission_delivery_cancels_run_waiter() -> None:
    handle = _ControlledRunHandle()
    permissions = _ControlledPermissions()
    sdk = _ControlledSDK(handle, permissions)
    agent = AgentSpec(name="quickstart", model="fake/general")
    turn = asyncio.create_task(
        execute_turn(
            sdk,  # type: ignore[arg-type]
            "quickstart-session",
            agent,
            "write a note",
            resolve_permission=lambda _: asyncio.sleep(0),  # type: ignore[arg-type]
        )
    )

    try:
        await asyncio.wait_for(permissions.next_started.wait(), timeout=1)
        turn.cancel()

        with pytest.raises(asyncio.CancelledError):
            await turn

        await asyncio.wait_for(handle.result_cancelled.wait(), timeout=1)
    finally:
        handle.finish(_run_result())
        await asyncio.wait_for(handle.result_finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_execute_turn_cancellation_while_resolver_waits_denies_request_and_cancels_run_waiter() -> None:
    handle = _ControlledRunHandle()
    request = _permission_request()
    permissions = _ControlledPermissions(request)
    permissions.deliver()
    sdk = _ControlledSDK(handle, permissions)
    agent = AgentSpec(name="quickstart", model="fake/general")
    resolver_started = asyncio.Event()
    resolver_release = asyncio.Event()

    async def wait_for_decision(_: PermissionRequest) -> PermissionDecision:
        resolver_started.set()
        await resolver_release.wait()
        return PermissionDecision.allow_once()

    turn = asyncio.create_task(
        execute_turn(
            sdk,  # type: ignore[arg-type]
            "quickstart-session",
            agent,
            "write a note",
            resolve_permission=wait_for_decision,
        )
    )

    try:
        await asyncio.wait_for(resolver_started.wait(), timeout=1)
        turn.cancel()

        with pytest.raises(asyncio.CancelledError):
            await turn

        await asyncio.wait_for(permissions.resolved.wait(), timeout=1)
        assert permissions.resolutions == [
            (request.request_id, PermissionDecision.deny("quickstart stopped"))
        ]
        await asyncio.wait_for(handle.result_cancelled.wait(), timeout=1)
    finally:
        resolver_release.set()
        handle.finish(_run_result())
        await asyncio.wait_for(handle.result_finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_execute_turn_returns_result_when_recovered_request_is_already_resolved() -> None:
    expected = _run_result()
    handle = _ControlledRunHandle(expected)
    permissions = _ControlledPermissions(
        _permission_request(),
        resolve_error=AgentSDKError(
            ErrorCode.NOT_FOUND,
            "permission request not found",
            retryable=False,
        ),
    )
    permissions.deliver()
    sdk = _ControlledSDK(handle, permissions)
    agent = AgentSpec(name="quickstart", model="fake/general")

    result = await execute_turn(
        sdk,  # type: ignore[arg-type]
        "quickstart-session",
        agent,
        "write a note",
        resolve_permission=lambda _: asyncio.sleep(0),  # type: ignore[arg-type]
    )

    assert result == expected
    assert permissions.resolutions == [
        ("quickstart-request", PermissionDecision.deny("Run already terminated"))
    ]


class _TraceSDK:
    def __init__(self, timeline: TraceTimeline) -> None:
        self.trace = self
        self._timeline = timeline

    async def timeline(self, _: str) -> TraceTimeline:
        return self._timeline


@pytest.mark.asyncio
async def test_summarize_run_includes_only_tool_results_in_trace_timeline() -> None:
    result = RunResult(
        run_id="quickstart-run",
        output_text="finished",
        usage=TokenUsage(total_tokens=3),
        tool_results=(
            ToolResult.succeeded("included-call", "write", {"ok": True}),
            ToolResult.succeeded("unrelated-call", "bash", {"ok": True}),
        ),
    )
    timeline = TraceTimeline(
        root_id=result.run_id,
        root_kind="run",
        stages=(
            TraceStage(
                stage_id="tool-stage",
                kind=TraceStageKind.TOOL,
                status=TraceStageStatus.COMPLETED,
                entity_id="included-call",
                run_id=result.run_id,
                session_id="quickstart-session",
                first_cursor=1,
                last_cursor=1,
            ),
        ),
        as_of_cursor=1,
    )

    summary = await summarize_run(_TraceSDK(timeline), result)  # type: ignore[arg-type]

    assert summary.tools == ("write",)
