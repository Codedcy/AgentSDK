from typing import TYPE_CHECKING, Any

from agent_sdk.subagents.models import ChildResult, ChildUsage, TaskEnvelope

if TYPE_CHECKING:
    from agent_sdk.subagents.service import SubagentService


def __getattr__(name: str) -> Any:
    if name == "SubagentService":
        from agent_sdk.subagents.service import SubagentService

        return SubagentService
    raise AttributeError(name)

__all__ = ["ChildResult", "ChildUsage", "SubagentService", "TaskEnvelope"]
