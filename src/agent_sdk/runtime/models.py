from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Self

from collections.abc import Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from agent_sdk.tools.models import ToolResult
from agent_sdk.subagents.models import TaskEnvelope
from agent_sdk.runtime.execution import ExecutionDescriptor


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    COMPLETED = "completed"
    FAILED = "failed"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    DELETING = "deleting"


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


class RunFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    retryable: bool


class SessionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    status: SessionStatus = SessionStatus.ACTIVE
    workspaces: tuple[str, ...]
    version: int = Field(default=1, gt=0)
    active_run_ids: tuple[str, ...] = ()
    active_workflow_run_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_active_work(self) -> Self:
        for values in (self.active_run_ids, self.active_workflow_run_ids):
            if any(not value for value in values):
                raise ValueError("active execution ids must be nonempty")
            if tuple(sorted(values)) != values or len(set(values)) != len(values):
                raise ValueError("active execution ids must be sorted and unique")
        if self.status in {SessionStatus.CLOSED, SessionStatus.DELETING} and (
            self.active_run_ids or self.active_workflow_run_ids
        ):
            raise ValueError("closed or deleting session cannot own active work")
        return self

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


class RunSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    session_id: str
    agent_revision: str
    status: RunStatus
    user_input: str
    version: int = Field(default=1, gt=0)
    output_text: str | None = None
    usage: TokenUsage | None = None
    parent_run_id: str | None = None
    workflow_run_id: str | None = None
    workflow_node_id: str | None = None
    task_envelope: TaskEnvelope | None = None
    error: RunFailure | None = None
    execution_compatibility: Literal["legacy_unknown", "current"] = "legacy_unknown"
    execution_descriptor: ExecutionDescriptor | None = None
    tool_results: tuple[ToolResult, ...] = ()

    @model_validator(mode="after")
    def _validate_status_fields(self) -> Self:
        if (self.execution_compatibility == "current") != (
            self.execution_descriptor is not None
        ):
            raise ValueError("run execution compatibility is invalid")
        if self.status is RunStatus.CREATED:
            if self.version != 1 or any(
                value is not None for value in (self.output_text, self.usage, self.error)
            ):
                raise ValueError("created run contains execution state")
        elif self.status in {RunStatus.RUNNING, RunStatus.WAITING_PERMISSION}:
            minimum_version = 2 if self.status is RunStatus.RUNNING else 3
            if self.version < minimum_version:
                raise ValueError("nonterminal run version is invalid")
            if any(
                value is not None for value in (self.output_text, self.usage, self.error)
            ):
                raise ValueError("nonterminal run contains terminal state")
        elif self.status is RunStatus.COMPLETED:
            if (
                self.version < 3
                or self.output_text is None
                or self.usage is None
                or self.error is not None
            ):
                raise ValueError("completed run state is invalid")
        elif (
            self.version < 3
            or self.output_text is None
            or self.usage is None
            or self.error is None
        ):
            raise ValueError("failed run state is invalid")
        if self.status in {
            RunStatus.CREATED,
            RunStatus.RUNNING,
            RunStatus.WAITING_PERMISSION,
        } and self.tool_results:
            raise ValueError("nonterminal run contains durable tool results")
        return self

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
