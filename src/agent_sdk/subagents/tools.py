from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.subagents.coordinator import ChildCoordinator
from agent_sdk.subagents.mailbox import MailboxService
from agent_sdk.subagents.models import TaskEnvelope
from agent_sdk.tools.models import ToolContext, ToolSpec
from agent_sdk.tools.registry import ToolRegistry

_CONTROL_TOOL_NAMES = (
    "spawn_agent",
    "send_message",
    "wait_child",
    "list_children",
)


async def _spawn_agent(
    context: ToolContext,
    agent_revision: str,
    task: Mapping[str, Any],
    *,
    coordinator: ChildCoordinator,
) -> dict[str, object]:
    created = await coordinator.spawn(
        parent_run_id=context.run_id,
        agent_revision=agent_revision,
        task=TaskEnvelope.model_validate(task),
    )
    return {"child_run_id": created.run_id, "status": "queued"}


async def _send_message(
    context: ToolContext,
    target_run_id: str,
    content: str,
    *,
    mailbox: MailboxService,
) -> dict[str, object]:
    message = await mailbox.send(context.run_id, target_run_id, content)
    return message.model_dump(mode="json")


async def _wait_child(
    context: ToolContext,
    child_run_id: str,
    timeout_seconds: float | None = None,
    *,
    coordinator: ChildCoordinator,
) -> dict[str, object]:
    result = await coordinator.wait(
        child_run_id,
        timeout_seconds=timeout_seconds,
        expected_parent_run_id=context.run_id,
    )
    return result.model_dump(mode="json")


async def _list_children(
    context: ToolContext,
    *,
    coordinator: ChildCoordinator,
) -> list[dict[str, object]]:
    progress = await coordinator.list(context.run_id)
    return [item.model_dump(mode="json") for item in progress]


def register_child_control_tools(
    *,
    registry: ToolRegistry,
    coordinator: ChildCoordinator,
    mailbox: MailboxService,
) -> None:
    registered_names = {spec.name for spec in registry.list()}
    for name in _CONTROL_TOOL_NAMES:
        if name in registered_names:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                f"child control tool name already registered: {name}",
                retryable=False,
            )

    registry.register(
        ToolSpec(
            name="spawn_agent",
            description="Spawn a direct child Agent Run from a closed task envelope.",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_revision": {"type": "string", "minLength": 1},
                    "task": TaskEnvelope.model_json_schema(),
                },
                "required": ["agent_revision", "task"],
                "additionalProperties": False,
            },
            source="builtin",
            effects=("agent.spawn",),
        ),
        partial(_spawn_agent, coordinator=coordinator),
    )
    registry.register(
        ToolSpec(
            name="send_message",
            description="Send a message to the caller's direct parent or child.",
            input_schema={
                "type": "object",
                "properties": {
                    "target_run_id": {"type": "string", "minLength": 1},
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 32_768,
                    },
                },
                "required": ["target_run_id", "content"],
                "additionalProperties": False,
            },
            source="builtin",
            effects=("agent.message",),
        ),
        partial(_send_message, mailbox=mailbox),
    )
    registry.register(
        ToolSpec(
            name="wait_child",
            description="Wait a bounded time for one direct child Run.",
            input_schema={
                "type": "object",
                "properties": {
                    "child_run_id": {"type": "string", "minLength": 1},
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 0,
                    },
                },
                "required": ["child_run_id"],
                "additionalProperties": False,
            },
            source="builtin",
            effects=("agent.inspect",),
        ),
        partial(_wait_child, coordinator=coordinator),
    )
    registry.register(
        ToolSpec(
            name="list_children",
            description="List progress for the caller's direct child Runs.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            source="builtin",
            effects=("agent.inspect",),
        ),
        partial(_list_children, coordinator=coordinator),
    )


__all__ = ["register_child_control_tools"]
