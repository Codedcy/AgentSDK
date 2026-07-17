from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
from agent_sdk.permissions.rules import (
    PermissionOutcome,
    PermissionRule,
    match_rule,
)
from agent_sdk.tools.models import freeze_json


def _canonicalize_rule(rule: PermissionRule) -> PermissionRule:
    if rule.path_prefix is None:
        return rule
    return PermissionRule.model_validate(
        {
            **rule.model_dump(mode="python"),
            "path_prefix": rule.path_prefix.resolve(strict=False),
        }
    )


class PolicyEngine:
    def __init__(
        self,
        default_outcome: PermissionOutcome = "ask",
        rules: Iterable[PermissionRule] = (),
    ) -> None:
        if default_outcome not in {"allow", "deny", "ask"}:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "invalid permission default",
                retryable=False,
            )
        self._default_outcome = default_outcome
        self._rules = tuple(_canonicalize_rule(rule) for rule in rules)

    def evaluate(self, request: PermissionRequest) -> PermissionDecision:
        matches = tuple(
            match
            for rule in self._rules
            if (match := match_rule(rule, request)) is not None
        )
        denials = tuple(match for match in matches if match.rule.outcome == "deny")
        selected = max(
            denials or matches,
            key=lambda item: item.specificity,
            default=None,
        )
        outcome = selected.rule.outcome if selected else self._default_outcome
        if outcome == "allow":
            return PermissionDecision.allow_once()
        if outcome == "deny":
            return PermissionDecision.deny()
        return PermissionDecision.ask()

    def execution_config(self) -> Mapping[str, Any]:
        """Return a detached snapshot of every execution-affecting setting."""
        rules = tuple(
            freeze_json(rule.model_dump(mode="json")) for rule in self._rules
        )
        return MappingProxyType(
            {
                "permission_default": self._default_outcome,
                "permission_rules": rules,
            }
        )
