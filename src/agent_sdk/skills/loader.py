from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from yaml.nodes import MappingNode, Node, SequenceNode

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.skills.models import SkillMetadata

MAX_SKILL_FILE_BYTES = 1024 * 1024


class _Frontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    description: str = Field(min_length=1, max_length=1024)
    license: str | None = None
    compatibility: str | None = Field(default=None, min_length=1, max_length=500)
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: str | None = Field(default=None, alias="allowed-tools")


@dataclass(frozen=True)
class ParsedSkill:
    metadata: SkillMetadata
    instructions: str
    root: Path


def _invalid(message: str, error: BaseException | None = None) -> AgentSDKError:
    sdk_error = AgentSDKError(ErrorCode.INVALID_STATE, message, retryable=False)
    if error is not None:
        sdk_error.__cause__ = error
    return sdk_error


def _read_bytes(path: Path) -> bytes:
    try:
        resolved = path.resolve(strict=True)
        if resolved != path:
            raise _invalid("skill path changed after discovery")
        stat = resolved.stat()
        if not resolved.is_file():
            raise _invalid("skill file must be a regular file")
        if stat.st_size > MAX_SKILL_FILE_BYTES:
            raise _invalid("skill file is too large")
        return resolved.read_bytes()
    except AgentSDKError:
        raise
    except OSError as error:
        raise _invalid("failed to read skill file", error) from error


def _split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise _invalid("skill frontmatter is missing")
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    if closing is None:
        raise _invalid("skill frontmatter is not closed")
    return "".join(lines[1:closing]), "".join(lines[closing + 1 :])


def _reject_duplicate_keys(node: Node | None) -> None:
    if isinstance(node, MappingNode):
        seen: set[tuple[str, str]] = set()
        for key, value in node.value:
            marker = (key.tag, getattr(key, "value", repr(key)))
            if marker in seen:
                raise _invalid("skill frontmatter contains duplicate keys")
            seen.add(marker)
            _reject_duplicate_keys(value)
    elif isinstance(node, SequenceNode):
        for value in node.value:
            _reject_duplicate_keys(value)


def _validate_raw_types(data: Mapping[Any, Any]) -> None:
    string_fields = ("name", "description", "license", "compatibility", "allowed-tools")
    for field in string_fields:
        if field in data and type(data[field]) is not str:
            raise _invalid("skill frontmatter has invalid field types")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, Mapping) or any(
        type(key) is not str or type(value) is not str
        for key, value in metadata.items()
    ):
        raise _invalid("skill metadata must map strings to strings")


def _allowed_tools(value: str | None) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if any(character.isspace() and character != " " for character in value):
        raise _invalid("allowed-tools must be space-separated")
    return tuple(value.split())


def load_skill(
    path: Path,
    *,
    expected_directory_name: str,
    expected_hash: str | None = None,
) -> ParsedSkill:
    raw = _read_bytes(path)
    content_hash = hashlib.sha256(raw).hexdigest()
    if expected_hash is not None and content_hash != expected_hash:
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "skill changed after discovery",
            retryable=False,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _invalid("skill file must be UTF-8", error) from error
    frontmatter_text, instructions = _split_frontmatter(text)
    try:
        _reject_duplicate_keys(yaml.compose(frontmatter_text, Loader=yaml.SafeLoader))
        loaded = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as error:
        raise _invalid("skill frontmatter is invalid", error) from error
    if not isinstance(loaded, Mapping):
        raise _invalid("skill frontmatter must be a mapping")
    _validate_raw_types(loaded)
    try:
        parsed = _Frontmatter.model_validate(dict(loaded))
    except ValidationError as error:
        raise _invalid("skill frontmatter is invalid", error) from error
    if parsed.name != expected_directory_name:
        raise _invalid("skill name must match its directory")
    resolved = path.resolve(strict=True)
    metadata = SkillMetadata(
        name=parsed.name,
        description=parsed.description,
        location=resolved,
        content_hash=content_hash,
        license=parsed.license,
        compatibility=parsed.compatibility,
        metadata=parsed.metadata,
        allowed_tools=_allowed_tools(parsed.allowed_tools),
    )
    return ParsedSkill(metadata=metadata, instructions=instructions, root=resolved.parent)


__all__ = ["MAX_SKILL_FILE_BYTES", "ParsedSkill", "load_skill"]
