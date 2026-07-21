from typing import TYPE_CHECKING, Any

from agent_sdk.subagents.models import (
    AgentMessage,
    ChildResult,
    ChildUsage,
    MailboxCursorSnapshot,
    MailboxSnapshot,
    TaskEnvelope,
)

if TYPE_CHECKING:
    from agent_sdk.subagents.mailbox import MailboxService
    from agent_sdk.subagents.service import SubagentService


def __getattr__(name: str) -> Any:
    if name == "MailboxService":
        from agent_sdk.subagents.mailbox import MailboxService

        return MailboxService
    if name == "SubagentService":
        from agent_sdk.subagents.service import SubagentService

        return SubagentService
    raise AttributeError(name)

__all__ = [
    "AgentMessage",
    "ChildResult",
    "ChildUsage",
    "MailboxCursorSnapshot",
    "MailboxService",
    "MailboxSnapshot",
    "SubagentService",
    "TaskEnvelope",
]
