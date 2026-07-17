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
    from agent_sdk.tools.builtins.registration import register_builtin_tools


def __getattr__(name: str) -> Any:
    if name == "ToolExecutor":
        from agent_sdk.tools.executor import ToolExecutor

        return ToolExecutor
    if name == "register_builtin_tools":
        from agent_sdk.tools.builtins.registration import register_builtin_tools

        return register_builtin_tools
    raise AttributeError(name)

__all__ = [
    "ToolContext",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "ToolResultStatus",
    "ToolRetryPolicy",
    "ToolSpec",
    "register_builtin_tools",
]
