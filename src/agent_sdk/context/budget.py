from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any, TypeAlias, cast

import litellm

from agent_sdk.context.models import ContextBudget

TokenCounter: TypeAlias = Callable[..., int]


def default_token_counter(
    *,
    model: str,
    messages: Sequence[dict[str, Any]],
) -> int:
    count = litellm.token_counter(
        model=model,
        messages=deepcopy(list(messages)),
    )
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("LiteLLM token counter returned an invalid count")
    return cast(int, count)


def calculate_budget(
    *,
    model: str,
    messages: Sequence[dict[str, Any]],
    model_window: int,
    output_reserve: int,
    tool_schema_tokens: int,
    safety_reserve: int,
    token_counter: TokenCounter = default_token_counter,
) -> ContextBudget:
    baseline = ContextBudget.calculate(
        model_window=model_window,
        output_reserve=output_reserve,
        tool_schema_tokens=tool_schema_tokens,
        safety_reserve=safety_reserve,
        projected_source_tokens=0,
    )
    if baseline.available_input_tokens <= 0:
        return baseline
    projected = token_counter(model=model, messages=deepcopy(list(messages)))
    if isinstance(projected, bool) or not isinstance(projected, int) or projected < 0:
        raise ValueError("token counter returned an invalid count")
    return ContextBudget.calculate(
        model_window=model_window,
        output_reserve=output_reserve,
        tool_schema_tokens=tool_schema_tokens,
        safety_reserve=safety_reserve,
        projected_source_tokens=projected,
    )
