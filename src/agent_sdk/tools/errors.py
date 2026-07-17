class ToolAccessDenied(Exception):
    """Private marker for a workspace or built-in tool access denial."""


class ToolExecutionTimedOut(Exception):
    """Private marker for a timeout enforced by a tool handler."""


__all__ = ["ToolAccessDenied", "ToolExecutionTimedOut"]
