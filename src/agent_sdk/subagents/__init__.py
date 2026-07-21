from typing import TYPE_CHECKING, Any

from agent_sdk.subagents.models import (
    AgentMessage,
    ChildLimits,
    ChildProgress,
    ChildResult,
    ChildUsage,
    ChildWaitResult,
    MailboxCursorSnapshot,
    MailboxSnapshot,
    TaskEnvelope,
)

if TYPE_CHECKING:
    from agent_sdk.subagents.coordinator import ChildCoordinator
    from agent_sdk.subagents.mailbox import MailboxService
    from agent_sdk.subagents.service import SubagentService
    from agent_sdk.subagents.tools import register_child_control_tools


def __getattr__(name: str) -> Any:
    if name == "ChildCoordinator":
        from agent_sdk.subagents.coordinator import ChildCoordinator

        return ChildCoordinator
    if name == "MailboxService":
        from agent_sdk.subagents.mailbox import MailboxService

        return MailboxService
    if name == "SubagentService":
        from agent_sdk.subagents.service import SubagentService

        return SubagentService
    if name == "register_child_control_tools":
        from agent_sdk.subagents.tools import register_child_control_tools

        return register_child_control_tools
    raise AttributeError(name)

__all__ = [
    "AgentMessage",
    "ChildLimits",
    "ChildCoordinator",
    "ChildProgress",
    "ChildResult",
    "ChildUsage",
    "ChildWaitResult",
    "MailboxCursorSnapshot",
    "MailboxService",
    "MailboxSnapshot",
    "SubagentService",
    "TaskEnvelope",
    "register_child_control_tools",
]
