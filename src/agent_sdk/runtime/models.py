import hashlib
import json
from enum import StrEnum
from pathlib import Path
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

from agent_sdk.context_runtime import ContextRuntimeConfig
from agent_sdk.tools.models import ToolResult
from agent_sdk.tools.builtins.workspace import canonical_workspace_scope
from agent_sdk.subagents.models import TaskEnvelope
from agent_sdk.runtime.execution import ExecutionDescriptor
from agent_sdk.runtime.failures import RunFailure as RunFailure
from agent_sdk.runtime.model_params import (
    freeze_model_params,
    validate_model_params_for_durability,
)


def intersect_names(
    available: tuple[str, ...],
    *allowlists: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Return the canonical capability names allowed by every explicit scope."""
    selected = set(available)
    for allowlist in allowlists:
        if allowlist is not None:
            selected.intersection_update(allowlist)
    return tuple(name for name in sorted(set(available)) if name in selected)


def intersect_workspaces(
    available: tuple[Path, ...],
    *allowlists: tuple[str, ...] | None,
) -> tuple[Path, ...]:
    """Narrow workspace roots without allowing a later scope to expand them."""
    selected = tuple(sorted({_canonical_workspace(root) for root in available}, key=str))
    for allowlist in allowlists:
        if allowlist is None:
            continue
        narrowed: set[Path] = set()
        for raw_scope in allowlist:
            candidate = _canonical_workspace(raw_scope)
            for root in selected:
                if _is_within(candidate, root):
                    narrowed.add(candidate)
                elif _is_within(root, candidate):
                    narrowed.add(root)
        selected = tuple(sorted(narrowed, key=str))
    return selected


def _canonical_workspace(value: str | Path) -> Path:
    return canonical_workspace_scope(value)


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    INTERRUPTED = "interrupted"
    WAITING_RECONCILIATION = "waiting_reconciliation"
    COMPLETED = "completed"
    FAILED = "failed"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    DELETING = "deleting"


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
    prompt_profile: Literal["general", "coding"] = "general"
    system_prompt: str | None = None
    skills: tuple[str, ...] = ()
    context: ContextRuntimeConfig = Field(default_factory=ContextRuntimeConfig)
    tool_allowlist: tuple[str, ...] | None = None
    workspace_allowlist: tuple[str, ...] | None = None

    @field_validator("model_params", mode="before")
    @classmethod
    def _reject_credentials(cls, value: Any) -> Any:
        validate_model_params_for_durability(value)
        return value

    @field_validator("model_params", mode="after")
    @classmethod
    def _freeze_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return freeze_model_params(value)

    @field_serializer("model_params")
    def _serialize_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return mutable_model_params(value)

    @field_validator("skills")
    @classmethod
    def _validate_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not name.strip() for name in value):
            raise ValueError("skills must contain nonempty names")
        if len(set(value)) != len(value):
            raise ValueError("skills must be unique")
        return value

    @field_validator("tool_allowlist", "workspace_allowlist")
    @classmethod
    def _validate_capability_allowlist(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if value is not None and any(not item.strip() for item in value):
            raise ValueError("capability allowlists must contain nonempty values")
        return value

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
    cost_usd: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        exclude_if=lambda value: value is None,
    )


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    output_text: str
    usage: TokenUsage
    tool_results: tuple[ToolResult, ...] = ()


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
    workflow_node_execution: int | None = Field(
        default=None,
        ge=1,
        strict=True,
        exclude_if=lambda value: value is None,
    )
    task_envelope: TaskEnvelope | None = None
    error: RunFailure | None = None
    execution_compatibility: Literal["legacy_unknown", "current"] = "legacy_unknown"
    execution_descriptor: ExecutionDescriptor | None = None
    tool_results: tuple[ToolResult, ...] = ()

    @model_validator(mode="after")
    def _validate_status_fields(self) -> Self:
        if self.workflow_node_execution is not None and (
            self.workflow_run_id is None or self.workflow_node_id is None
        ):
            raise ValueError("workflow node execution has no workflow binding")
        if (self.execution_compatibility == "current") != (
            self.execution_descriptor is not None
        ):
            raise ValueError("run execution compatibility is invalid")
        if self.execution_descriptor is not None:
            descriptor = self.execution_descriptor
            expected_revision = f"{descriptor.agent.name}:{descriptor.agent.revision}"
            if self.agent_revision != expected_revision:
                raise ValueError("run agent does not match execution descriptor")
            expected_messages = ({"role": "user", "content": self.user_input},)
            actual_messages = tuple(dict(message) for message in descriptor.messages)
            if actual_messages != expected_messages:
                raise ValueError("run input messages do not match execution descriptor")
        if self.status is RunStatus.CREATED:
            if self.version != 1 or any(
                value is not None for value in (self.output_text, self.usage, self.error)
            ):
                raise ValueError("created run contains execution state")
        elif self.status in {
            RunStatus.RUNNING,
            RunStatus.WAITING_PERMISSION,
            RunStatus.INTERRUPTED,
            RunStatus.WAITING_RECONCILIATION,
        }:
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
            RunStatus.INTERRUPTED,
            RunStatus.WAITING_RECONCILIATION,
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


class RunCreatedEventPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    session_id: str
    agent_revision: str
    status: Literal["created"] = "created"
    version: Literal[1] = 1
    parent_run_id: str | None = None
    workflow_run_id: str | None = None
    workflow_node_id: str | None = None
    workflow_node_execution: int | None = None
    execution_compatibility: Literal["legacy_unknown", "current"]
    user_input: str
    user_input_sha256: str
    task_envelope_sha256: str | None = None
    execution_descriptor_hash: str | None = None
    agent_hash: str | None = None
    tool_capability_hashes: tuple[str, ...] = ()

    @classmethod
    def from_snapshot(cls, snapshot: RunSnapshot) -> Self:
        descriptor = snapshot.execution_descriptor
        return cls(
            run_id=snapshot.run_id,
            session_id=snapshot.session_id,
            agent_revision=snapshot.agent_revision,
            parent_run_id=snapshot.parent_run_id,
            workflow_run_id=snapshot.workflow_run_id,
            workflow_node_id=snapshot.workflow_node_id,
            workflow_node_execution=snapshot.workflow_node_execution,
            execution_compatibility=snapshot.execution_compatibility,
            user_input=snapshot.user_input,
            user_input_sha256=_canonical_sha256(snapshot.user_input),
            task_envelope_sha256=(
                None
                if snapshot.task_envelope is None
                else _canonical_sha256(
                    snapshot.task_envelope.model_dump(mode="json")
                )
            ),
            execution_descriptor_hash=(
                None if descriptor is None else descriptor.descriptor_hash
            ),
            agent_hash=None if descriptor is None else descriptor.agent_hash,
            tool_capability_hashes=(
                ()
                if descriptor is None
                else tuple(tool.capability_hash for tool in descriptor.tools)
            ),
        )


def run_created_event_payload(snapshot: RunSnapshot) -> dict[str, Any]:
    return RunCreatedEventPayload.from_snapshot(snapshot).model_dump(mode="json")


def run_created_event_matches(
    snapshot: RunSnapshot,
    payload: Mapping[str, Any],
    *,
    schema_version: int,
) -> bool:
    try:
        if schema_version == 1:
            created = RunSnapshot(
                run_id=snapshot.run_id,
                session_id=snapshot.session_id,
                agent_revision=snapshot.agent_revision,
                status=RunStatus.CREATED,
                user_input=snapshot.user_input,
                parent_run_id=snapshot.parent_run_id,
                workflow_run_id=snapshot.workflow_run_id,
                workflow_node_id=snapshot.workflow_node_id,
                workflow_node_execution=snapshot.workflow_node_execution,
                task_envelope=snapshot.task_envelope,
                execution_compatibility=snapshot.execution_compatibility,
                execution_descriptor=snapshot.execution_descriptor,
            )
            historical = RunSnapshot.model_validate(dict(payload))
            return historical == created
        if schema_version in {2, 3}:
            historical_event = RunCreatedEventPayload.model_validate(dict(payload))
            current = RunCreatedEventPayload.from_snapshot(snapshot)
            if historical_event == current:
                return True
            if schema_version == 3:
                return False
            legacy = _pre_r4_run_created_event_payload(snapshot)
            return legacy is not None and historical_event == legacy
    except Exception:
        return False
    return False


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pre_r4_run_created_event_payload(
    snapshot: RunSnapshot,
) -> RunCreatedEventPayload | None:
    """Project an upgraded R3 descriptor into its immutable schema-v2 shape."""
    descriptor = snapshot.execution_descriptor
    if (
        descriptor is None
        or descriptor.agent.tool_allowlist is not None
        or descriptor.agent.workspace_allowlist is not None
        or descriptor.workspace_scopes is not None
    ):
        return None
    raw_descriptor = descriptor.model_dump(mode="json")
    raw_agent = dict(raw_descriptor["agent"])
    raw_agent.pop("tool_allowlist")
    raw_agent.pop("workspace_allowlist")
    raw_descriptor["agent"] = raw_agent
    raw_descriptor["agent_hash"] = _descriptor_sha256(raw_agent)
    raw_descriptor.pop("workspace_scopes")
    raw_descriptor.pop("descriptor_hash")
    return RunCreatedEventPayload.from_snapshot(snapshot).model_copy(
        update={
            "agent_hash": raw_descriptor["agent_hash"],
            "execution_descriptor_hash": _descriptor_sha256(raw_descriptor),
        }
    )


def _descriptor_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
