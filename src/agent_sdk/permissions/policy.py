from __future__ import annotations

from types import MappingProxyType
from collections.abc import Mapping
from typing import Literal

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest

PermissionOutcome = Literal["allow", "deny", "ask"]


class PolicyEngine:
    def __init__(self, default_outcome: PermissionOutcome = "ask") -> None:
        if default_outcome not in {"allow", "deny", "ask"}:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "invalid permission default",
                retryable=False,
            )
        self._default_outcome = default_outcome

    def evaluate(self, request: PermissionRequest) -> PermissionDecision:
        del request
        if self._default_outcome == "allow":
            return PermissionDecision.allow_once()
        if self._default_outcome == "deny":
            return PermissionDecision.deny()
        return PermissionDecision.ask()

    def execution_config(self) -> Mapping[str, PermissionOutcome]:
        """Return a detached snapshot of every execution-affecting setting."""
        return MappingProxyType({"permission_default": self._default_outcome})
