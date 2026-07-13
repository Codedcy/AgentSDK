from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from agent_sdk.tools.models import bounded_text, freeze_json, thaw_json


class PermissionEffect(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str
    resource: str


class PermissionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    request_id: str
    run_id: str
    session_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    effects: tuple[str, ...] = ()

    @field_validator("arguments", mode="after")
    @classmethod
    def _freeze_arguments(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen = freeze_json(value)
        assert isinstance(frozen, Mapping)
        return frozen

    @field_serializer("arguments")
    def _serialize_arguments(self, value: Mapping[str, Any]) -> dict[str, Any]:
        thawed = thaw_json(value)
        assert isinstance(thawed, dict)
        return thawed

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


class PermissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["allow", "deny", "ask"]
    scope: Literal["once", "run", "session", "persistent"] | None = None
    reason: str | None = None

    @field_validator("reason", mode="after")
    @classmethod
    def _bound_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return bounded_text(value, max_bytes=512)

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @classmethod
    def allow_once(cls) -> PermissionDecision:
        return cls(action="allow", scope="once")

    @classmethod
    def deny(cls, reason: str = "permission denied") -> PermissionDecision:
        return cls(action="deny", scope="once", reason=reason)

    @classmethod
    def ask(cls) -> PermissionDecision:
        return cls(action="ask")
