from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Self

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from agent_sdk.tools.models import ToolResult


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    COMPLETED = "completed"
    FAILED = "failed"


def _freeze_model_param(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_model_param(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_model_param(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("model params must contain only JSON-like values")


def mutable_model_params(value: Mapping[str, Any]) -> dict[str, Any]:
    def thaw(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {key: thaw(nested) for key, nested in item.items()}
        if isinstance(item, tuple):
            return [thaw(nested) for nested in item]
        return item

    return {key: thaw(item) for key, item in value.items()}


class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    model: str
    model_params: Mapping[str, Any] = Field(default_factory=dict)
    revision: str = "1"

    @field_validator("model_params", mode="after")
    @classmethod
    def _freeze_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen = _freeze_model_param(value)
        assert isinstance(frozen, Mapping)
        return frozen

    @field_serializer("model_params")
    def _serialize_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return mutable_model_params(value)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        del deep
        data = self.model_dump(mode="json")
        if update is not None:
            data.update(update)
        return type(self).model_validate(data)


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    output_text: str
    usage: TokenUsage
    tool_results: tuple[ToolResult, ...] = ()


class SessionSnapshot(BaseModel):
    session_id: str
    status: Literal["active"] = "active"
    workspaces: tuple[str, ...]
    version: int = 1


class RunSnapshot(BaseModel):
    run_id: str
    session_id: str
    agent_revision: str
    status: RunStatus
    user_input: str
    version: int = 1
    output_text: str | None = None
    usage: TokenUsage | None = None
