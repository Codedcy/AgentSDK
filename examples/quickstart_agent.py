from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
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
    if session_id is not None:
        return await sdk.sessions.get(session_id)
    return await sdk.sessions.create(workspaces=(workspace.resolve(),))


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
    resolution = asyncio.create_task(
        sdk.permissions.resolve(request.request_id, decision)
    )
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


async def execute_turn(
    sdk: AgentSDK,
    session_id: str,
    agent: AgentSpec,
    user_input: str,
    *,
    resolve_permission: PermissionResolver,
) -> RunResult:
    handle = await sdk.runs.start(session_id, agent, user_input)
    result_waiter = asyncio.create_task(handle.result())
    permission_waiter: asyncio.Task[PermissionRequest] | None = None
    pending_request: PermissionRequest | None = None
    try:
        while not result_waiter.done():
            permission_waiter = asyncio.create_task(
                sdk.permissions.next_request(handle.run_id)
            )
            done, _ = await asyncio.wait(
                {result_waiter, permission_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if result_waiter in done:
                pending_request = await _settle_permission_waiter(
                    permission_waiter
                )
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
    finally:
        try:
            if permission_waiter is not None:
                with suppress(BaseException):
                    recovered = await _settle_permission_waiter(permission_waiter)
                    if pending_request is None:
                        pending_request = recovered
            if pending_request is not None:
                request = pending_request
                pending_request = None
                await _resolve_permission_best_effort(
                    sdk,
                    request,
                    PermissionDecision.deny("quickstart stopped"),
                )
        finally:
            await _cancel_and_await_result_waiter(result_waiter)


async def summarize_run(
    sdk: AgentSDK,
    result: RunResult,
) -> RunSummary:
    timeline = await sdk.trace.timeline(result.run_id)
    traced_call_ids = {
        stage.entity_id
        for stage in timeline.stages
        if stage.kind is TraceStageKind.TOOL
    }
    tools = tuple(
        dict.fromkeys(
            item.tool_name
            for item in result.tool_results
            if item.call_id in traced_call_ids
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


async def prompt_for_permission(
    request: PermissionRequest,
) -> PermissionDecision:
    arguments = json.dumps(
        request.model_dump(mode="json")["arguments"],
        ensure_ascii=False,
    )
    answer = await _console_read(
        f"Allow {request.tool_name} once with {arguments}? [y/N] "
    )
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
        result = await execute_turn(
            sdk,
            session_id,
            agent,
            user_input,
            resolve_permission=resolve_permission,
        )
        summary = await summarize_run(sdk, result)
        write_line(f"Agent> {result.output_text}")
        tool_text = ", ".join(summary.tools) if summary.tools else "none"
        token_text = (
            str(summary.total_tokens)
            if summary.total_tokens is not None
            else "unknown"
        )
        write_line(
            f"Run {summary.run_id} | tokens={token_text} | tools={tool_text}"
        )


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
