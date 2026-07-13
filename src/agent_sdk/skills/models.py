from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_serializer,
    field_validator,
)

from agent_sdk.errors import AgentSDKError, ErrorCode

PathIdentity = tuple[int, int]


def _stat_identity(value: os.stat_result) -> PathIdentity:
    return (value.st_dev, value.st_ino)


def _path_identity(path: Path) -> PathIdentity:
    return _stat_identity(path.stat())


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
    _root_identity: PathIdentity | None = PrivateAttr(default=None)

    def model_post_init(self, __context: Any) -> None:
        del __context
        try:
            resolved = self.root.resolve(strict=True)
            if resolved == self.root and resolved.is_dir():
                self._root_identity = _path_identity(resolved)
        except OSError:
            self._root_identity = None

    @classmethod
    def _from_pinned(
        cls,
        *,
        metadata: SkillMetadata,
        instructions: str,
        root: Path,
        root_identity: PathIdentity,
    ) -> ActivatedSkill:
        activated = cls(metadata=metadata, instructions=instructions, root=root)
        activated._root_identity = root_identity
        return activated

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
        copied = type(self).model_validate(data)
        if copied.root == self.root:
            copied._root_identity = self._root_identity
        return copied

    def _verified_root(self) -> Path:
        try:
            resolved = self.root.resolve(strict=True)
            if (
                resolved != self.root
                or not resolved.is_dir()
                or self._root_identity is None
                or _path_identity(resolved) != self._root_identity
            ):
                raise ValueError("activated skill root identity changed")
            return resolved
        except (OSError, ValueError) as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "activated skill root changed",
                retryable=False,
            ) from error

    def resolve_member(self, member: str | Path) -> Path:
        requested = Path(member)
        if requested.is_absolute() or not requested.parts or ".." in requested.parts:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill member path is invalid",
                retryable=False,
            )
        try:
            real_root = self._verified_root()
            resolved = (real_root / requested).resolve(strict=True)
            resolved.relative_to(real_root)
            self._verified_root()
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
        try:
            resolved = self.resolve_member(member)
            with resolved.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "skill member must be a regular file",
                        retryable=False,
                    )
                real_root = self._verified_root()
                current = resolved.resolve(strict=True)
                current.relative_to(real_root)
                if current != resolved or _path_identity(current) != _stat_identity(opened):
                    raise AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "skill member changed while opening",
                        retryable=False,
                    )
                raw = handle.read()
                self._verified_root()
        except FileNotFoundError as error:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "skill member not found",
                retryable=False,
            ) from error
        except AgentSDKError:
            raise
        except (OSError, ValueError) as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "failed to read skill member",
                retryable=False,
            ) from error
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill member must be UTF-8",
                retryable=False,
            ) from error


__all__ = ["ActivatedSkill", "SkillMetadata"]
