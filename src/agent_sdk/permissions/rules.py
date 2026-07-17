from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from agent_sdk.permissions.models import PermissionRequest

PermissionOutcome = Literal["allow", "deny", "ask"]


class PermissionRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: PermissionOutcome
    tool: str = "*"
    path_prefix: Path | None = None
    command_prefix: tuple[str, ...] = ()

    @field_validator("tool")
    @classmethod
    def validate_tool(cls, value: str) -> str:
        if not value:
            raise ValueError("tool cannot be empty")
        return value


@dataclass(frozen=True)
class RuleMatch:
    rule: PermissionRule
    specificity: tuple[int, int, int]


def _match_path(prefix: Path | None, requested: object) -> int | None:
    if prefix is None:
        return 0
    if not isinstance(requested, str) or not requested:
        return None
    canonical_requested = Path(requested).resolve(strict=False)
    if not canonical_requested.is_relative_to(prefix):
        return None
    return len(prefix.parts)


def match_rule(
    rule: PermissionRule,
    request: PermissionRequest,
) -> RuleMatch | None:
    if rule.tool not in {"*", request.tool_name}:
        return None
    path_score = _match_path(rule.path_prefix, request.arguments.get("path"))
    if path_score is None:
        path_score = _match_path(rule.path_prefix, request.arguments.get("cwd"))
    if rule.path_prefix is not None and path_score is None:
        return None
    argv = request.arguments.get("argv")
    if rule.command_prefix and (
        not isinstance(argv, (tuple, list))
        or tuple(argv[: len(rule.command_prefix)]) != rule.command_prefix
    ):
        return None
    return RuleMatch(
        rule=rule,
        specificity=(
            int(rule.tool != "*"),
            path_score or 0,
            len(rule.command_prefix),
        ),
    )
