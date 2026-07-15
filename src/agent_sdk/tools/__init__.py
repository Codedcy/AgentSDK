from typing import TYPE_CHECKING, Any

from agent_sdk.tools.models import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    ToolRetryPolicy,
    ToolSpec,
)
from agent_sdk.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agent_sdk.tools.executor import ToolExecutor


def __getattr__(name: str) -> Any:
    if name == "ToolExecutor":
        from agent_sdk.tools.executor import ToolExecutor

        return ToolExecutor
    raise AttributeError(name)

__all__ = [
    "ToolContext",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "ToolResultStatus",
    "ToolRetryPolicy",
    "ToolSpec",
]
