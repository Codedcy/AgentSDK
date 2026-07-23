from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from agent_sdk import (
    AgentSDK,
    AgentSDKConfig,
    AgentSpec,
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
