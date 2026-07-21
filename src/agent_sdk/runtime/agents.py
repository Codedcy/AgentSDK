from __future__ import annotations

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.runtime.model_params import validate_model_params_for_durability
from agent_sdk.runtime.models import AgentSpec


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentSpec] = {}

    def define(self, spec: AgentSpec) -> AgentSpec:
        validate_model_params_for_durability(spec.model_params)
        key = self.key(spec)
        if key in self._agents:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "agent revision already defined",
                retryable=False,
            )
        detached = AgentSpec.model_validate(spec.model_dump(mode="json"))
        self._agents[key] = detached
        return detached

    def resolve(self, revision: str) -> AgentSpec:
        try:
            return self._agents[revision]
        except KeyError:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "agent revision not found",
                retryable=False,
            ) from None

    @staticmethod
    def key(spec: AgentSpec) -> str:
        return f"{spec.name}:{spec.revision}"
