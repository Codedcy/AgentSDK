from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from agent_sdk.errors import AgentSDKError, ErrorCode


class SkillMetadata(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        validate_default=True,
    )

    name: str
    description: str
    location: Path
    content_hash: str
    license: str | None = None
    compatibility: str | None = None
    metadata: Mapping[str, str] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    instructions: None = None

    @field_validator("metadata", mode="before")
    @classmethod
    def _detach_metadata(cls, value: Any) -> Mapping[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("metadata must be a string mapping")
        detached: dict[str, str] = {}
        for key, item in value.items():
            if type(key) is not str or type(item) is not str:
                raise ValueError("metadata must be a string mapping")
            detached[key] = item
        return MappingProxyType(detached)

    @field_serializer("metadata")
    def _serialize_metadata(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

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


class ActivatedSkill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: SkillMetadata
    instructions: str
    root: Path

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

    def resolve_member(self, member: str | Path) -> Path:
        requested = Path(member)
        if requested.is_absolute() or not requested.parts or ".." in requested.parts:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill member path is invalid",
                retryable=False,
            )
        try:
            real_root = self.root.resolve(strict=True)
            if real_root != self.root:
                raise ValueError("activated skill root was rebound")
            resolved = (real_root / requested).resolve(strict=True)
            resolved.relative_to(real_root)
        except FileNotFoundError as error:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "skill member not found",
                retryable=False,
            ) from error
        except (OSError, ValueError) as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill member path escapes skill root",
                retryable=False,
            ) from error
        return resolved

    def read_text(self, member: str | Path) -> str:
        resolved = self.resolve_member(member)
        if not resolved.is_file():
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill member must be a regular file",
                retryable=False,
            )
        try:
            return resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill member must be UTF-8",
                retryable=False,
            ) from error


__all__ = ["ActivatedSkill", "SkillMetadata"]
