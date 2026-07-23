from __future__ import annotations

import ast
import asyncio
import json
import socket
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
    RunStatus,
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
    build_sdk_config,
    build_parser,
    define_agent,
    execute_turn,
    prompt_for_permission,
    run_chat,
    select_session,
    summarize_permission_request,
    summarize_run,
)


@pytest.fixture(autouse=True)
def _forbid_network_connections(
    event_loop: asyncio.AbstractEventLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del event_loop

    def forbid_network(*_: object, **__: object) -> object:
        raise AssertionError("quickstart tests must not open a network socket")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid_network)


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


def test_sdk_config_uses_exact_workspace_permission_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    database = tmp_path / "quickstart.db"
    args = build_parser().parse_args(
        [
            "--database",
            str(database),
            "--workspace",
            str(workspace),
        ]
    )

    config = build_sdk_config(args)

    assert config.database_path == database
    assert config.permission_default == "ask"
    assert [
        (rule.tool, rule.outcome, rule.path_prefix, rule.command_prefix)
        for rule in config.permission_rules
    ] == [
        ("read", "allow", workspace.resolve(), ()),
        ("write", "ask", workspace.resolve(), ()),
        ("bash", "ask", workspace.resolve(), ()),
    ]


def test_permission_summary_for_large_write_hides_content_and_counts_utf8_bytes() -> None:
    content = "private-write-marker-秘密" * 1000
    request = PermissionRequest(
        request_id="write-summary-request",
        run_id="write-summary-run",
        session_id="write-summary-session",
        tool_name="write",
        arguments={
            "path": "notes/output.txt",
            "content": content,
            "overwrite": True,
        },
    )

    summary = summarize_permission_request(request)

    assert "path=notes/output.txt" in summary
    assert f"content_bytes={len(content.encode('utf-8'))}" in summary
    assert "overwrite=true" in summary
    assert "private-write-marker" not in summary
    assert "秘密" not in summary
    assert len(summary) <= 512


@pytest.mark.asyncio
async def test_bash_permission_prompt_redacts_secrets_and_warns_before_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = PermissionRequest(
        request_id="bash-summary-request",
        run_id="bash-summary-run",
        session_id="bash-summary-session",
        tool_name="bash",
        arguments={
            "cwd": "C:/workspace",
            "argv": [
                "env",
                "OPENAI_API_KEY=sk-assignment-secret",
                "deploy",
                "--token",
                "separate-token-secret",
                "--password=inline-password-secret",
                "AWS_SESSION_TOKEN=session-token-secret",
                "-u",
                "curl-user:curl-password-secret",
                "--header",
                "Authorization: Bearer header-secret",
            ],
        },
    )
    prompts: list[str] = []

    async def capture_prompt(prompt: str) -> str:
        prompts.append(prompt)
        return "n"

    monkeypatch.setattr(
        "examples.quickstart_agent._console_read",
        capture_prompt,
    )

    decision = await prompt_for_permission(request)
    summary = summarize_permission_request(request)

    assert decision == PermissionDecision.deny("user denied")
    assert prompts and summary in prompts[0]
    for secret in (
        "sk-assignment-secret",
        "separate-token-secret",
        "inline-password-secret",
        "session-token-secret",
        "curl-password-secret",
        "header-secret",
    ):
        assert secret not in summary
        assert secret not in prompts[0]
    assert summary.count("[REDACTED]") >= 6
    assert "cwd=C:/workspace" in summary
    assert "argv=" in summary
    assert "not sandboxed" in summary
    assert "outside the workspace" in summary
    assert "inherit the application environment" in summary
    assert "stdout and stderr" in summary
    assert "sent to the model" in summary
    assert "stored in Session history" in summary
    assert len(summary) <= 512


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_newlines"),
    (
        (
            "read",
            {"path": "notes/\n\t\r\x1b\x07\x85\x7f\u202e\u200d.txt"},
            0,
        ),
        (
            "write",
            {
                "path": "notes/\n\t\r\x1b\x07\x85\x7f\u202e\u200d.txt",
                "content": "content is never displayed",
            },
            0,
        ),
        (
            "bash",
            {
                "cwd": "workspace/\n\t\r\x1b\x07\x85\x7f\u202e\u200d",
                "argv": ["printf", "value\n\t\r\x1b\x07\x85\x7f\u202e\u200d"],
            },
            1,
        ),
    ),
)
async def test_permission_prompt_escapes_terminal_controls_before_bounding(
    tool_name: str,
    arguments: dict[str, object],
    expected_newlines: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = PermissionRequest(
        request_id=f"{tool_name}-controls-request",
        run_id=f"{tool_name}-controls-run",
        session_id=f"{tool_name}-controls-session",
        tool_name=tool_name,
        arguments=arguments,
    )
    prompts: list[str] = []

    async def capture_prompt(prompt: str) -> str:
        prompts.append(prompt)
        return "n"

    monkeypatch.setattr(
        "examples.quickstart_agent._console_read",
        capture_prompt,
    )

    await prompt_for_permission(request)
    summary = summarize_permission_request(request)

    assert prompts == [f"Allow {tool_name} once with {summary}? [y/N] "]
    assert summary.count("\n") == expected_newlines
    for raw_control in ("\t", "\r", "\x1b", "\x07", "\x85", "\x7f", "\u202e", "\u200d"):
        assert raw_control not in summary
        assert raw_control not in prompts[0]
    for visible_escape in (
        r"\n",
        r"\t",
        r"\r",
        r"\x1b",
        r"\x07",
        r"\x85",
        r"\x7f",
        r"\u202e",
        r"\u200d",
    ):
        assert visible_escape in summary
        assert visible_escape in prompts[0]
    assert len(summary) <= 512
    assert len(prompts[0]) <= 600


def test_bash_permission_summary_redacts_bare_names_and_attached_short_options() -> None:
    request = PermissionRequest(
        request_id="bash-common-secret-forms-request",
        run_id="bash-common-secret-forms-run",
        session_id="bash-common-secret-forms-session",
        tool_name="bash",
        arguments={
            "cwd": "C:/workspace",
            "argv": [
                "env",
                "API_TOKEN",
                "bare-env-token-secret",
                "aws",
                "configure",
                "set",
                "aws_secret_access_key",
                "bare-aws-secret",
                "mysql",
                "-pattached-password-secret",
                "curl",
                "-uattached-user:attached-password-secret",
            ],
        },
    )

    summary = summarize_permission_request(request)

    for secret in (
        "bare-env-token-secret",
        "bare-aws-secret",
        "attached-password-secret",
        "attached-user",
    ):
        assert secret not in summary
    assert summary.count("[REDACTED]") >= 4
    assert "API_TOKEN" in summary
    assert "aws_secret_access_key" in summary
    assert len(summary) <= 512


def test_bash_permission_summary_redacts_attached_authorization_headers() -> None:
    request = PermissionRequest(
        request_id="bash-attached-header-request",
        run_id="bash-attached-header-run",
        session_id="bash-attached-header-session",
        tool_name="bash",
        arguments={
            "cwd": "C:/workspace",
            "argv": [
                "curl",
                "--header=Authorization: Bearer long-header-secret",
                "-HAuthorization: Basic short-header-secret",
                "--proxy-header=Proxy-Authorization: Basic proxy-header-secret",
            ],
        },
    )

    summary = summarize_permission_request(request)

    for secret in (
        "long-header-secret",
        "short-header-secret",
        "proxy-header-secret",
    ):
        assert secret not in summary
    assert summary.count("[REDACTED]") == 3
    assert "--header=Authorization: [REDACTED]" in summary
    assert "-HAuthorization: [REDACTED]" in summary
    assert "--proxy-header=Proxy-Authorization: [REDACTED]" in summary
    assert len(summary) <= 512


def test_read_permission_summary_bounds_long_path() -> None:
    path = "C:/workspace/" + ("nested/" * 200) + "target.txt"
    request = PermissionRequest(
        request_id="read-summary-request",
        run_id="read-summary-run",
        session_id="read-summary-session",
        tool_name="read",
        arguments={"path": path},
    )

    summary = summarize_permission_request(request)

    assert summary.startswith("path=C:/workspace/")
    assert summary.endswith("target.txt")
    assert "..." in summary
    assert path not in summary
    assert len(summary) <= 512


def test_bash_permission_summary_caps_complete_display_and_keeps_warning() -> None:
    huge_argument = "benign-segment-" * 200
    request = PermissionRequest(
        request_id="bash-truncation-request",
        run_id="bash-truncation-run",
        session_id="bash-truncation-session",
        tool_name="bash",
        arguments={
            "cwd": "C:/workspace/" + ("nested/" * 100),
            "argv": ["python", huge_argument, *(f"argument-{index}" for index in range(50))],
        },
    )

    summary = summarize_permission_request(request)

    assert len(summary) <= 512
    assert huge_argument not in summary
    assert "..." in summary
    assert "not sandboxed" in summary
    assert "outside the workspace" in summary
    assert "inherit the application environment" in summary


def test_unknown_permission_summary_omits_arguments() -> None:
    request = PermissionRequest(
        request_id="unknown-summary-request",
        run_id="unknown-summary-run",
        session_id="unknown-summary-session",
        tool_name="custom_tool",
        arguments={
            "action": "safe-action",
            "api_key": "unknown-api-secret",
            "nested": {
                "password": "unknown-password-secret",
                "items": [
                    {"token": "unknown-token-secret"},
                    {"note": "public-prefix-" + ("x" * 2000)},
                ],
            },
        },
    )

    summary = summarize_permission_request(request)

    assert summary == "arguments omitted"
    for secret in (
        "unknown-api-secret",
        "unknown-password-secret",
        "unknown-token-secret",
    ):
        assert secret not in summary


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
async def test_select_session_rejects_mismatched_workspace_before_run(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    requested = tmp_path / "requested"
    original.mkdir()
    requested.mkdir()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unexpected_provider,
    )
    try:
        created = await select_session(sdk, original, None)

        with pytest.raises(
            AgentSDKError,
            match="session workspace does not match --workspace",
        ) as captured:
            await select_session(sdk, requested, created.session_id)

        assert captured.value.code is ErrorCode.INVALID_STATE
        persisted = await sdk.sessions.get(created.session_id)
        assert persisted.active_run_ids == ()
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
        yield {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
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
    *,
    call_id: str = "quickstart-call",
) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
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


class FixedAnswerProvider:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.requests: list[tuple[dict[str, Any], ...]] = []

    async def __call__(self, **params: Any) -> object:
        self.requests.append(tuple(dict(item) for item in params["messages"]))
        return _text_stream(self.answer)


class DeferredTwoWritesProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.first_call_started = asyncio.Event()
        self.release_first_call = asyncio.Event()

    async def __call__(self, **_: Any) -> object:
        self.calls += 1
        if self.calls == 1:
            self.first_call_started.set()
            await self.release_first_call.wait()
            return _tool_stream(
                "write",
                {"path": "first.txt", "content": "first"},
                call_id="deferred-write-1",
            )
        if self.calls == 2:
            return _tool_stream(
                "write",
                {"path": "second.txt", "content": "second"},
                call_id="deferred-write-2",
            )
        return _text_stream("cleanup finished")


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
async def test_sqlite_restart_preserves_workspace_and_conversation_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "quickstart.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_provider = FixedAnswerProvider("first answer")
    first_sdk = AgentSDK.for_test(
        database_path=database,
        acompletion=first_provider,
        permission_default="ask",
    )
    session_id: str
    try:
        session = await select_session(first_sdk, workspace, None)
        session_id = session.session_id
        first_result = await execute_turn(
            first_sdk,
            session_id,
            define_agent(first_sdk, "fake/general"),
            "first question",
            resolve_permission=lambda _: asyncio.sleep(0),  # type: ignore[arg-type]
        )

        assert first_result.output_text == "first answer"
    finally:
        await asyncio.wait_for(first_sdk.close(), timeout=2)

    second_provider = FixedAnswerProvider("second answer")
    second_sdk = AgentSDK.for_test(
        database_path=database,
        acompletion=second_provider,
        permission_default="ask",
    )
    try:
        resumed = await select_session(second_sdk, workspace, session_id)
        second_result = await execute_turn(
            second_sdk,
            resumed.session_id,
            define_agent(second_sdk, "fake/general"),
            "second question",
            resolve_permission=lambda _: asyncio.sleep(0),  # type: ignore[arg-type]
        )

        assert resumed.workspaces == (str(workspace.resolve()),)
        assert second_result.output_text == "second answer"
        assert len(second_provider.requests) == 1
        second_request = second_provider.requests[0]
        assert any(
            message.get("role") == "user" and message.get("content") == "first question"
            for message in second_request
        )
        assert any(
            message.get("role") == "assistant" and message.get("content") == "first answer"
            for message in second_request
        )
        assert any(
            message.get("role") == "user" and message.get("content") == "second question"
            for message in second_request
        )
    finally:
        await asyncio.wait_for(second_sdk.close(), timeout=2)


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


@pytest.mark.asyncio
async def test_run_chat_treats_permission_prompt_eof_as_normal_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = WriteThenAnswerProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
    )
    requests: list[PermissionRequest] = []
    output: list[str] = []

    async def read_user_input(_: str) -> str:
        return "write a note"

    async def permission_eof(_: str) -> str:
        raise EOFError

    async def prompt(request: PermissionRequest) -> PermissionDecision:
        requests.append(request)
        return await prompt_for_permission(request)

    monkeypatch.setattr(
        "examples.quickstart_agent._console_read",
        permission_eof,
    )
    closed = False
    try:
        session = await sdk.sessions.create(workspaces=(tmp_path,))
        agent = define_agent(sdk, "fake/general")

        await run_chat(
            sdk,
            session.session_id,
            agent,
            read_line=read_user_input,
            write_line=output.append,
            resolve_permission=prompt,
        )

        assert len(requests) == 1
        snapshot = await sdk.runs.get(requests[0].run_id)
        assert snapshot.status is RunStatus.COMPLETED
        assert [item.status.value for item in snapshot.tool_results] == ["denied"]
        assert not (tmp_path / "note.txt").exists()
        assert output == []
        await asyncio.wait_for(sdk.close(), timeout=1)
        closed = True
    finally:
        if not closed:
            await asyncio.wait_for(sdk.close(), timeout=2)


@pytest.mark.asyncio
async def test_execute_turn_cancellation_drains_real_run_permissions(
    tmp_path: Path,
) -> None:
    provider = DeferredTwoWritesProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
    )
    session = await sdk.sessions.create(workspaces=(tmp_path,))
    agent = define_agent(sdk, "fake/general")
    resolver_calls: list[PermissionRequest] = []

    async def unexpected_resolver(
        request: PermissionRequest,
    ) -> PermissionDecision:
        resolver_calls.append(request)
        return PermissionDecision.allow_once()

    turn = asyncio.create_task(
        execute_turn(
            sdk,
            session.session_id,
            agent,
            "write two files",
            resolve_permission=unexpected_resolver,
        )
    )
    rescue: asyncio.Task[None] | None = None
    closed = False

    async def rescue_stalled_run(run_id: str) -> None:
        for _ in range(2):
            request = await sdk.permissions.next_request(run_id)
            await sdk.permissions.resolve(
                request.request_id,
                PermissionDecision.deny("test rescue"),
            )

    try:
        await asyncio.wait_for(provider.first_call_started.wait(), timeout=1)
        active = await sdk.sessions.get(session.session_id)
        assert len(active.active_run_ids) == 1
        run_id = active.active_run_ids[0]

        turn.cancel()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(turn), timeout=0.05)

        provider.release_first_call.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, timeout=2)

        snapshot = await sdk.runs.get(run_id)
        assert snapshot.status is RunStatus.COMPLETED
        assert [item.status.value for item in snapshot.tool_results] == [
            "denied",
            "denied",
        ]
        assert resolver_calls == []
        assert provider.calls == 3
        await asyncio.wait_for(sdk.close(), timeout=1)
        closed = True
    finally:
        provider.release_first_call.set()
        if not closed:
            if turn.done():
                active = await sdk.sessions.get(session.session_id)
                if active.active_run_ids:
                    rescue = asyncio.create_task(rescue_stalled_run(active.active_run_ids[0]))
            else:
                turn.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(turn, timeout=2)
            if rescue is not None:
                await asyncio.wait_for(rescue, timeout=2)
            await asyncio.wait_for(sdk.close(), timeout=2)


@pytest.mark.asyncio
async def test_execute_turn_cancellation_during_start_retains_real_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = DeferredTwoWritesProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
    )
    session = await sdk.sessions.create(workspaces=(tmp_path,))
    agent = define_agent(sdk, "fake/general")
    start_committed = asyncio.Event()
    release_start = asyncio.Event()
    committed_run_ids: list[str] = []
    original_start_run = sdk.runs._commands.start_run  # type: ignore[attr-defined]

    async def delay_committed_start(*args: Any, **kwargs: Any) -> Any:
        outcome = await original_start_run(*args, **kwargs)
        committed_run_ids.append(outcome.value.run_id)
        start_committed.set()
        await release_start.wait()
        return outcome

    monkeypatch.setattr(
        sdk.runs._commands,  # type: ignore[attr-defined]
        "start_run",
        delay_committed_start,
    )

    async def unexpected_resolver(_: PermissionRequest) -> PermissionDecision:
        raise AssertionError("cancelled turn must deny without prompting")

    turn = asyncio.create_task(
        execute_turn(
            sdk,
            session.session_id,
            agent,
            "write two files",
            resolve_permission=unexpected_resolver,
        )
    )
    rescue: asyncio.Task[None] | None = None
    closed = False

    async def rescue_stalled_run(run_id: str) -> None:
        for _ in range(2):
            request = await sdk.permissions.next_request(run_id)
            await sdk.permissions.resolve(
                request.request_id,
                PermissionDecision.deny("test rescue"),
            )

    try:
        await asyncio.wait_for(start_committed.wait(), timeout=1)
        assert len(committed_run_ids) == 1
        run_id = committed_run_ids[0]

        turn.cancel()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(turn), timeout=0.05)

        release_start.set()
        await asyncio.wait_for(provider.first_call_started.wait(), timeout=1)
        assert not turn.done(), "execute_turn returned before draining the started Run"

        provider.release_first_call.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, timeout=2)

        snapshot = await sdk.runs.get(run_id)
        assert snapshot.status is RunStatus.COMPLETED
        assert [item.status.value for item in snapshot.tool_results] == [
            "denied",
            "denied",
        ]
        assert provider.calls == 3
        await asyncio.wait_for(sdk.close(), timeout=1)
        closed = True
    finally:
        release_start.set()
        provider.release_first_call.set()
        if not closed:
            if turn.done() and committed_run_ids:
                rescue = asyncio.create_task(rescue_stalled_run(committed_run_ids[0]))
            else:
                turn.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(turn, timeout=2)
            if rescue is not None:
                await asyncio.wait_for(rescue, timeout=2)
            await asyncio.wait_for(sdk.close(), timeout=2)


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
async def test_execute_turn_cancellation_while_resolver_waits_denies_request_and_cancels_run_waiter() -> (
    None
):
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
