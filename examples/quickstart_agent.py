from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from agent_sdk import (
    AgentSDK,
    AgentSDKConfig,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    PermissionDecision,
    PermissionRequest,
    RunHandle,
    RunResult,
    SessionSnapshot,
    TraceStageKind,
)
from agent_sdk.permissions import PermissionRule


PermissionResolver = Callable[
    [PermissionRequest],
    Awaitable[PermissionDecision],
]
LineReader = Callable[[str], Awaitable[str]]
LineWriter = Callable[[str], None]
_PERMISSION_SUMMARY_LIMIT = 512
_PERMISSION_FIELD_LIMIT = 192
_PERMISSION_ARG_LIMIT = 80
_PERMISSION_ARG_COUNT = 16
_UNKNOWN_ITEM_COUNT = 8
_UNKNOWN_DEPTH_LIMIT = 5
_REDACTED = "[REDACTED]"
_BASH_WARNING = (
    "WARNING: approved commands are not sandboxed; they can access paths "
    "outside the workspace and inherit the application environment."
)
_SENSITIVE_NAMES = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "key",
        "password",
        "passwd",
        "privatekey",
        "secret",
        "setcookie",
        "token",
    }
)
_SENSITIVE_BASH_FLAGS = frozenset(
    {
        "-p",
        "-u",
        "--auth",
        "--oauth2-bearer",
        "--passphrase",
        "--proxy-user",
        "--user",
    }
)
_SENSITIVE_HTTP_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
    }
)


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    total_tokens: int | None
    tools: tuple[str, ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a small general Agent with Agent SDK.",
    )
    parser.add_argument("--model", default="openai/gpt-4o-mini")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(".agent-sdk/quickstart.db"),
    )
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--session-id")
    return parser


def create_sdk(args: argparse.Namespace) -> AgentSDK:
    workspace = args.workspace.resolve()
    return AgentSDK(
        AgentSDKConfig(
            database_path=args.database,
            permission_default="ask",
            permission_rules=(
                PermissionRule(
                    outcome="allow",
                    tool="read",
                    path_prefix=workspace,
                ),
                PermissionRule(
                    outcome="ask",
                    tool="write",
                    path_prefix=workspace,
                ),
                PermissionRule(
                    outcome="ask",
                    tool="bash",
                    path_prefix=workspace,
                ),
            ),
        )
    )


async def select_session(
    sdk: AgentSDK,
    workspace: Path,
    session_id: str | None,
) -> SessionSnapshot:
    resolved_workspace = workspace.resolve()
    if session_id is not None:
        session = await sdk.sessions.get(session_id)
        if session.workspaces != (str(resolved_workspace),):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "session workspace does not match --workspace",
                retryable=False,
            )
        return session
    return await sdk.sessions.create(workspaces=(resolved_workspace,))


def define_agent(sdk: AgentSDK, model: str) -> AgentSpec:
    return sdk.agents.define(
        AgentSpec(
            name="quickstart",
            model=model,
            tool_allowlist=("read", "write", "bash"),
        )
    )


async def _settle_permission_waiter(
    waiter: asyncio.Task[PermissionRequest],
) -> PermissionRequest | None:
    if waiter.done():
        if waiter.cancelled():
            return None
        return waiter.result()
    waiter.cancel()
    with suppress(asyncio.CancelledError):
        await waiter
    return None


async def _resolve_permission_best_effort(
    sdk: AgentSDK,
    request: PermissionRequest,
    decision: PermissionDecision,
) -> None:
    resolution = asyncio.create_task(sdk.permissions.resolve(request.request_id, decision))
    cancellation: asyncio.CancelledError | None = None
    while not resolution.done():
        try:
            await asyncio.shield(resolution)
        except asyncio.CancelledError as error:
            if resolution.cancelled():
                return
            cancellation = error
        except BaseException:
            return
    with suppress(BaseException):
        resolution.result()
    if cancellation is not None:
        raise cancellation


async def _cancel_and_await_result_waiter(
    waiter: asyncio.Task[RunResult],
) -> None:
    if not waiter.done():
        waiter.cancel()
    with suppress(BaseException):
        await waiter


async def _drain_run_after_error(
    sdk: AgentSDK,
    handle: RunHandle,
    result_waiter: asyncio.Task[RunResult],
    permission_waiter: asyncio.Task[PermissionRequest] | None,
    pending_request: PermissionRequest | None,
) -> None:
    if permission_waiter is not None:
        with suppress(BaseException):
            recovered = await _settle_permission_waiter(permission_waiter)
            if pending_request is None:
                pending_request = recovered
    await _cancel_and_await_result_waiter(result_waiter)

    terminal_waiter = asyncio.create_task(handle.result())
    permission_waiter = None
    try:
        if pending_request is not None:
            await _resolve_permission_best_effort(
                sdk,
                pending_request,
                PermissionDecision.deny("quickstart stopped"),
            )
            pending_request = None

        while not terminal_waiter.done():
            permission_waiter = asyncio.create_task(sdk.permissions.next_request(handle.run_id))
            done, _ = await asyncio.wait(
                {terminal_waiter, permission_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if terminal_waiter in done:
                recovered = await _settle_permission_waiter(permission_waiter)
                permission_waiter = None
                if recovered is not None:
                    await _resolve_permission_best_effort(
                        sdk,
                        recovered,
                        PermissionDecision.deny("quickstart stopped"),
                    )
                break
            request = await permission_waiter
            permission_waiter = None
            await _resolve_permission_best_effort(
                sdk,
                request,
                PermissionDecision.deny("quickstart stopped"),
            )
        with suppress(BaseException):
            await terminal_waiter
    finally:
        if permission_waiter is not None:
            with suppress(BaseException):
                recovered = await _settle_permission_waiter(permission_waiter)
                if recovered is not None:
                    await _resolve_permission_best_effort(
                        sdk,
                        recovered,
                        PermissionDecision.deny("quickstart stopped"),
                    )
        await _cancel_and_await_result_waiter(terminal_waiter)


async def _drain_started_run_after_error(
    sdk: AgentSDK,
    start_waiter: asyncio.Task[RunHandle],
) -> None:
    try:
        handle = await start_waiter
    except BaseException:
        return
    result_waiter = asyncio.create_task(handle.result())
    await _drain_run_after_error(
        sdk,
        handle,
        result_waiter,
        permission_waiter=None,
        pending_request=None,
    )


async def _finish_cleanup(
    cleanup: asyncio.Task[None],
) -> None:
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if not cleanup.cancelled():
        with suppress(BaseException):
            cleanup.result()


async def execute_turn(
    sdk: AgentSDK,
    session_id: str,
    agent: AgentSpec,
    user_input: str,
    *,
    resolve_permission: PermissionResolver,
) -> RunResult:
    start_waiter = asyncio.create_task(sdk.runs.start(session_id, agent, user_input))
    try:
        handle = await asyncio.shield(start_waiter)
    except BaseException:
        cleanup = asyncio.create_task(
            _drain_started_run_after_error(
                sdk,
                start_waiter,
            )
        )
        await _finish_cleanup(cleanup)
        raise

    result_waiter = asyncio.create_task(handle.result())
    permission_waiter: asyncio.Task[PermissionRequest] | None = None
    pending_request: PermissionRequest | None = None
    try:
        while not result_waiter.done():
            permission_waiter = asyncio.create_task(sdk.permissions.next_request(handle.run_id))
            done, _ = await asyncio.wait(
                {result_waiter, permission_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if result_waiter in done:
                pending_request = await _settle_permission_waiter(permission_waiter)
                permission_waiter = None
                if pending_request is not None:
                    request = pending_request
                    pending_request = None
                    await _resolve_permission_best_effort(
                        sdk,
                        request,
                        PermissionDecision.deny("Run already terminated"),
                    )
                break
            pending_request = await permission_waiter
            permission_waiter = None
            decision = await resolve_permission(pending_request)
            await sdk.permissions.resolve(
                pending_request.request_id,
                decision,
            )
            pending_request = None
        return await result_waiter
    except BaseException:
        cleanup = asyncio.create_task(
            _drain_run_after_error(
                sdk,
                handle,
                result_waiter,
                permission_waiter,
                pending_request,
            )
        )
        await _finish_cleanup(cleanup)
        raise


async def summarize_run(
    sdk: AgentSDK,
    result: RunResult,
) -> RunSummary:
    timeline = await sdk.trace.timeline(result.run_id)
    traced_call_ids = {
        stage.entity_id for stage in timeline.stages if stage.kind is TraceStageKind.TOOL
    }
    tools = tuple(
        dict.fromkeys(
            item.tool_name for item in result.tool_results if item.call_id in traced_call_ids
        )
    )
    return RunSummary(
        run_id=result.run_id,
        total_tokens=result.usage.total_tokens,
        tools=tools,
    )


async def _console_read(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


def _console_write(text: str) -> None:
    print(text, flush=True)


def _is_sensitive_name(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower().lstrip("-"))
    return normalized in _SENSITIVE_NAMES or normalized.endswith(
        (
            "apikey",
            "accesstoken",
            "refreshtoken",
            "password",
            "privatekey",
            "secret",
            "secretaccesskey",
            "token",
        )
    )


def _is_sensitive_bash_flag(value: str) -> bool:
    return value.lower() in _SENSITIVE_BASH_FLAGS or (
        value.startswith("-") and _is_sensitive_name(value)
    )


def _redact_http_header(value: str) -> str | None:
    header, colon, _ = value.partition(":")
    normalized_header = header.strip().lower()
    if colon and (
        normalized_header in _SENSITIVE_HTTP_HEADERS or _is_sensitive_name(normalized_header)
    ):
        return f"{header}: {_REDACTED}"
    return None


def _redact_bash_argument(value: str) -> str:
    if value.startswith("-H") and len(value) > 2:
        redacted_header = _redact_http_header(value[2:])
        if redacted_header is not None:
            return f"-H{redacted_header}"
    flag, flag_separator, flag_value = value.partition("=")
    if (
        flag_separator
        and flag.lower() in {"--header", "--proxy-header"}
        and (redacted_header := _redact_http_header(flag_value)) is not None
    ):
        return f"{flag}={redacted_header}"
    if len(value) > 2 and not value.startswith("--"):
        short_flag = value[:2].lower()
        if short_flag in {"-p", "-u"}:
            return f"{value[:2]}{_REDACTED}"
    name, separator, _ = value.partition("=")
    if separator and (_is_sensitive_name(name) or _is_sensitive_bash_flag(name)):
        return f"{name}={_REDACTED}"
    redacted_header = _redact_http_header(value)
    if redacted_header is not None:
        return redacted_header
    return value


def _redact_bash_argv(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    redacted: list[str] = []
    redact_next = False
    for item in value[:_PERMISSION_ARG_COUNT]:
        text = item if isinstance(item, str) else type(item).__name__
        if redact_next:
            redacted.append(_REDACTED)
            redact_next = False
            continue
        rendered = _redact_bash_argument(text)
        redacted.append(_bounded_display(rendered, limit=_PERMISSION_ARG_LIMIT))
        if (
            rendered == text
            and "=" not in text
            and (_is_sensitive_bash_flag(text) or _is_sensitive_name(text))
        ):
            redact_next = True
    if len(value) > _PERMISSION_ARG_COUNT:
        redacted.append(f"... {len(value) - _PERMISSION_ARG_COUNT} more arguments")
    return redacted


def _bounded_display(value: object, *, limit: int = _PERMISSION_FIELD_LIMIT) -> str:
    text = value if isinstance(value, str) else type(value).__name__
    if len(text) <= limit:
        return text
    tail_length = min(48, (limit - 3) // 2)
    return f"{text[: limit - tail_length - 3]}...{text[-tail_length:]}"


def _redact_unknown_value(
    value: object,
    *,
    field_name: str | None = None,
    depth: int = 0,
) -> object:
    if field_name is not None and _is_sensitive_name(field_name):
        return _REDACTED
    if depth >= _UNKNOWN_DEPTH_LIMIT:
        return "..."
    if isinstance(value, Mapping):
        items = list(value.items())
        redacted = {
            _bounded_display(key, limit=40): _redact_unknown_value(
                nested,
                field_name=key,
                depth=depth + 1,
            )
            for key, nested in items[:_UNKNOWN_ITEM_COUNT]
            if isinstance(key, str)
        }
        if len(items) > _UNKNOWN_ITEM_COUNT:
            redacted["..."] = f"{len(items) - _UNKNOWN_ITEM_COUNT} more fields"
        return redacted
    if isinstance(value, (list, tuple)):
        redacted_items = [
            _redact_unknown_value(item, depth=depth + 1) for item in value[:_UNKNOWN_ITEM_COUNT]
        ]
        if len(value) > _UNKNOWN_ITEM_COUNT:
            redacted_items.append(f"... {len(value) - _UNKNOWN_ITEM_COUNT} more items")
        return redacted_items
    if isinstance(value, str):
        return _bounded_display(
            _redact_bash_argument(value),
            limit=_PERMISSION_ARG_LIMIT,
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def summarize_permission_request(request: PermissionRequest) -> str:
    arguments = request.arguments
    if request.tool_name == "read":
        return f"path={_bounded_display(arguments.get('path'))}"
    if request.tool_name == "write":
        content = arguments.get("content")
        content_bytes = len(content.encode("utf-8")) if isinstance(content, str) else 0
        overwrite = "true" if arguments.get("overwrite") is True else "false"
        return (
            f"path={_bounded_display(arguments.get('path'))} "
            f"content_bytes={content_bytes} overwrite={overwrite}"
        )
    if request.tool_name == "bash":
        argv = json.dumps(
            _redact_bash_argv(arguments.get("argv")),
            ensure_ascii=False,
        )
        body = f"cwd={_bounded_display(arguments.get('cwd'))} argv={argv}"
        body_limit = _PERMISSION_SUMMARY_LIMIT - len(_BASH_WARNING) - 1
        return f"{_bounded_display(body, limit=body_limit)}\n{_BASH_WARNING}"
    rendered = json.dumps(
        _redact_unknown_value(arguments),
        ensure_ascii=False,
        sort_keys=True,
    )
    return _bounded_display(
        f"arguments={rendered}",
        limit=_PERMISSION_SUMMARY_LIMIT,
    )


async def prompt_for_permission(
    request: PermissionRequest,
) -> PermissionDecision:
    summary = summarize_permission_request(request)
    answer = await _console_read(f"Allow {request.tool_name} once with {summary}? [y/N] ")
    if answer.strip().lower() == "y":
        return PermissionDecision.allow_once()
    return PermissionDecision.deny("user denied")


async def run_chat(
    sdk: AgentSDK,
    session_id: str,
    agent: AgentSpec,
    *,
    read_line: LineReader = _console_read,
    write_line: LineWriter = _console_write,
    resolve_permission: PermissionResolver = prompt_for_permission,
) -> None:
    while True:
        try:
            user_input = (await read_line("You> ")).strip()
        except EOFError:
            return
        if user_input.lower() in {"exit", "quit"}:
            return
        if not user_input:
            continue
        try:
            result = await execute_turn(
                sdk,
                session_id,
                agent,
                user_input,
                resolve_permission=resolve_permission,
            )
        except EOFError:
            return
        summary = await summarize_run(sdk, result)
        write_line(f"Agent> {result.output_text}")
        tool_text = ", ".join(summary.tools) if summary.tools else "none"
        token_text = str(summary.total_tokens) if summary.total_tokens is not None else "unknown"
        write_line(f"Run {summary.run_id} | tokens={token_text} | tools={tool_text}")


async def async_main(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "workspace must be an existing directory",
            retryable=False,
        )
    sdk = create_sdk(args)
    try:
        session = await select_session(sdk, workspace, args.session_id)
        agent = define_agent(sdk, args.model)
        _console_write(f"Session: {session.session_id}")
        _console_write("Type exit to stop. The Session will be kept.")
        await run_chat(sdk, session.session_id, agent)
        return 0
    finally:
        await sdk.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        _console_write("\nStopped. The Session was kept.")
        return 130
    except AgentSDKError as error:
        _console_write(f"Agent SDK error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
