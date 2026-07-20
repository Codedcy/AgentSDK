from agent_sdk.context.models import CompactionLevel, SourceMessage
from agent_sdk.context.strategies import (
    StrategyResult,
    apply_l0,
    apply_l1,
    apply_l2,
)


def render_level(
    level: CompactionLevel,
    sources: tuple[SourceMessage, ...],
    *,
    recent_messages: int,
    tool_preview_bytes: int,
) -> StrategyResult:
    if level is CompactionLevel.L0:
        return apply_l0(sources)
    if level is CompactionLevel.L1:
        return apply_l1(sources, tool_preview_bytes=tool_preview_bytes)
    if level is CompactionLevel.L2:
        return apply_l2(
            sources,
            recent_messages=recent_messages,
            tool_preview_bytes=tool_preview_bytes,
        )
    raise ValueError("deterministic renderer supports L0-L2 only")
