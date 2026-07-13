from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def _string_mapping(value: Mapping[str, str] | None, *, field: str) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a string mapping")
    detached: dict[str, str] = {}
    for key, item in value.items():
        if type(key) is not str or type(item) is not str:
            raise ValueError(f"{field} must be a string mapping")
        detached[key] = item
    return MappingProxyType(detached)


class StdioMCPTransport(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        validate_default=True,
    )

    type: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = Field(default_factory=dict)
    cwd: Path | None = None

    @field_validator("command")
    @classmethod
    def _command_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be blank")
        return value

    @field_validator("args", mode="before")
    @classmethod
    def _detach_args(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError("args must be a sequence of strings")
        if any(type(item) is not str for item in value):
            raise ValueError("args must be a sequence of strings")
        return tuple(value)

    @field_validator("env", mode="before")
    @classmethod
    def _detach_env(cls, value: Any) -> Mapping[str, str]:
        return _string_mapping(value, field="env")

    @field_serializer("env")
    def _serialize_env(self, value: Mapping[str, str]) -> dict[str, str]:
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


class StreamableHTTPMCPTransport(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        validate_default=True,
    )

    type: Literal["streamable_http"] = "streamable_http"
    url: str = Field(min_length=1)
    headers: Mapping[str, str] = Field(default_factory=dict)
    terminate_on_close: bool = True

    @field_validator("url")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must use HTTP or HTTPS")
        return value

    @field_validator("headers", mode="before")
    @classmethod
    def _detach_headers(cls, value: Any) -> Mapping[str, str]:
        return _string_mapping(value, field="headers")

    @field_serializer("headers")
    def _serialize_headers(self, value: Mapping[str, str]) -> dict[str, str]:
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


MCPTransport = Annotated[
    StdioMCPTransport | StreamableHTTPMCPTransport,
    Field(discriminator="type"),
]


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    transport: MCPTransport
    startup_timeout: float = Field(default=30.0, gt=0)
    request_timeout: float = Field(default=30.0, gt=0)

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


__all__ = [
    "MCPServerConfig",
    "MCPTransport",
    "StdioMCPTransport",
    "StreamableHTTPMCPTransport",
]
