from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from agent_sdk import (
    AgentSDK,
    AgentSpec,
    EventFilter,
    ObservedEvent,
    PermissionDecision,
    PermissionRequest,
    RunResult,
    ToolContext,
    ToolSpec,
    WorkflowCompiler,
    WorkflowIR,
    WorkflowResult,
)

PermissionResolver = Callable[[PermissionRequest], Awaitable[PermissionDecision]]
WorkflowApprover = Callable[[WorkflowIR], Awaitable[bool]]
EventSink = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class RunExecution:
    run_id: str
    result: RunResult
    events: tuple[ObservedEvent, ...]


async def _collect_run_events(
    sdk: AgentSDK,
    run_id: str,
    emit: EventSink,
) -> tuple[ObservedEvent, ...]:
    collected: list[ObservedEvent] = []
    async for item in sdk.events.subscribe(
        filters=EventFilter(run_id=run_id),
        cursor=0,
    ):
        collected.append(item)
        emit(
            {
                "cursor": item.cursor,
                "type": item.event.type,
                "run_id": item.event.run_id,
                "payload": item.event.model_dump(mode="json")["payload"],
            }
        )
        if item.event.type in {"run.completed", "run.failed"}:
            return tuple(collected)
    return tuple(collected)


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


async def execute_run(
    sdk: AgentSDK,
    session_id: str,
    agent: AgentSpec,
    user_input: str,
    *,
    resolve_permission: PermissionResolver,
    emit: EventSink,
) -> RunExecution:
    handle = await sdk.runs.start(session_id, agent, user_input)
    monitor = asyncio.create_task(_collect_run_events(sdk, handle.run_id, emit))
    result_waiter = asyncio.create_task(handle.result())
    pending_request: PermissionRequest | None = None
    permission_waiter: asyncio.Task[PermissionRequest] | None = None
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
                delivered = await _settle_permission_waiter(permission_waiter)
                permission_waiter = None
                if delivered is not None:
                    await sdk.permissions.resolve(
                        delivered.request_id,
                        PermissionDecision.deny("Run already terminated"),
                    )
                break
            pending_request = await permission_waiter
            permission_waiter = None
            decision = await resolve_permission(pending_request)
            await sdk.permissions.resolve(pending_request.request_id, decision)
            pending_request = None
        result = await result_waiter
        events = await monitor
        return RunExecution(run_id=handle.run_id, result=result, events=events)
    except BaseException:
        if permission_waiter is not None:
            recovered: PermissionRequest | None = None
            with suppress(BaseException):
                recovered = await _settle_permission_waiter(permission_waiter)
            if pending_request is None:
                pending_request = recovered
        if pending_request is not None:
            cleanup = asyncio.create_task(
                sdk.permissions.resolve(
                    pending_request.request_id,
                    PermissionDecision.deny("reference runner stopped"),
                )
            )
            with suppress(BaseException):
                await asyncio.shield(cleanup)
        if not result_waiter.done():
            result_waiter.cancel()
        with suppress(BaseException):
            await result_waiter
        if not monitor.done():
            monitor.cancel()
        with suppress(BaseException):
            await monitor
        raise


def register_workspace_write(sdk: AgentSDK, workspace: Path) -> None:
    root = workspace.resolve()
    target = (root / "result.txt").resolve()
    target.relative_to(root)

    async def write_note(_: ToolContext, content: str) -> dict[str, object]:
        target.write_text(content, encoding="utf-8")
        return {
            "path": str(target),
            "bytes": len(content.encode("utf-8")),
        }

    sdk.tools.register(
        ToolSpec(
            name="write_note",
            description="Write result.txt inside the configured workspace",
            input_schema={
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
                "additionalProperties": False,
            },
            effects=("filesystem.write",),
        ),
        write_note,
    )


async def run_workflow_if_approved(
    sdk: AgentSDK,
    session_id: str,
    document: str,
    *,
    approve: WorkflowApprover,
    emit: EventSink,
) -> WorkflowResult | None:
    try:
        workflow = WorkflowCompiler().compile_yaml(document)
    except (TypeError, ValueError):
        emit({"type": "workflow.candidate.invalid"})
        return None
    emit(
        {
            "type": "workflow.candidate.valid",
            "name": workflow.name,
            "definition_hash": workflow.definition_hash,
        }
    )
    if not await approve(workflow):
        emit({"type": "workflow.candidate.rejected"})
        return None
    handle = await sdk.workflows.start(session_id, workflow)
    async for stored in handle.events():
        emit(
            {
                "cursor": stored.cursor,
                "type": stored.event.type,
                "run_id": stored.event.run_id,
                "payload": stored.event.model_dump(mode="json")["payload"],
            }
        )
    return await handle.result()
