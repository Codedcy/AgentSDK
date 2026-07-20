from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_serializer,
    field_validator,
)


class _PromptModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        arbitrary_types_allowed=True,
    )

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


class PromptLayer(_PromptModel):
    layer_id: StrictStr = Field(min_length=1)
    version: StrictStr = Field(min_length=1)
    text: StrictStr
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class PromptLayerManifest(_PromptModel):
    layer_id: StrictStr = Field(min_length=1)
    version: StrictStr = Field(min_length=1)
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class PromptManifest(_PromptModel):
    manifest_id: StrictStr = Field(min_length=1)
    layers: tuple[PromptLayerManifest, ...]
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    context_view_id: StrictStr = Field(min_length=1)
    model: StrictStr = Field(min_length=1)
    tools_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def layer_names(self) -> tuple[str, ...]:
        return tuple(layer.layer_id for layer in self.layers)


class BuiltPrompt(_PromptModel):
    messages: tuple[Mapping[str, str], ...]
    manifest: PromptManifest

    @field_validator("messages", mode="after")
    @classmethod
    def _freeze_messages(
        cls,
        messages: tuple[Mapping[str, str], ...],
    ) -> tuple[Mapping[str, str], ...]:
        frozen: list[Mapping[str, str]] = []
        for message in messages:
            if set(message) != {"role", "content"}:
                raise ValueError("prompt message fields are invalid")
            if message["role"] != "system":
                raise ValueError("prompt layers must be system messages")
            frozen.append(MappingProxyType(dict(message)))
        return tuple(frozen)

    @field_serializer("messages")
    def _serialize_messages(
        self,
        messages: tuple[Mapping[str, str], ...],
    ) -> list[dict[str, str]]:
        return [dict(message) for message in messages]

    @property
    def text(self) -> str:
        return "\n\n".join(message["content"] for message in self.messages)
