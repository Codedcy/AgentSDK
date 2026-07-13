from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from yaml.nodes import MappingNode, Node, SequenceNode

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.skills.models import (
    PathIdentity,
    SkillMetadata,
    _path_identity,
    _stat_identity,
)

MAX_SKILL_FILE_BYTES = 1024 * 1024
_MAX_YAML_DEPTH = 64
_MAX_YAML_NODES = 4096


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
    file_identity: PathIdentity


@dataclass(frozen=True)
class _StableRead:
    content: bytes
    path: Path
    identity: PathIdentity


def _invalid(message: str, error: BaseException | None = None) -> AgentSDKError:
    sdk_error = AgentSDKError(ErrorCode.INVALID_STATE, message, retryable=False)
    if error is not None:
        sdk_error.__cause__ = error
    return sdk_error


def _file_snapshot(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_bytes(path: Path) -> _StableRead:
    try:
        resolved = path.resolve(strict=True)
        if resolved != path:
            raise _invalid("skill path changed after discovery")
        with resolved.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise _invalid("skill file must be a regular file")
            if before.st_size > MAX_SKILL_FILE_BYTES:
                raise _invalid("skill file is too large")
            current = resolved.resolve(strict=True)
            if (
                current != resolved
                or _path_identity(current) != _stat_identity(before)
            ):
                raise _invalid("skill file changed while opening")
            content = handle.read(MAX_SKILL_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
            if _file_snapshot(after) != _file_snapshot(before):
                raise _invalid("skill file changed while reading")
            if len(content) > MAX_SKILL_FILE_BYTES:
                raise _invalid("skill file is too large")
        return _StableRead(
            content=content,
            path=resolved,
            identity=_stat_identity(before),
        )
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


def _reject_complex_or_duplicate_yaml(node: Node | None) -> None:
    visited: set[int] = set()
    active: set[int] = set()
    node_count = 0

    def walk(current: Node, depth: int) -> None:
        nonlocal node_count
        if depth > _MAX_YAML_DEPTH:
            raise _invalid("skill frontmatter exceeds complexity limits")
        identity = id(current)
        if identity in active:
            raise _invalid("skill frontmatter exceeds complexity limits")
        if identity in visited:
            return
        visited.add(identity)
        active.add(identity)
        node_count += 1
        if node_count > _MAX_YAML_NODES:
            raise _invalid("skill frontmatter exceeds complexity limits")
        try:
            if isinstance(current, MappingNode):
                keys: set[tuple[str, str]] = set()
                for key, value in current.value:
                    marker = (key.tag, getattr(key, "value", repr(key)))
                    if marker in keys:
                        raise _invalid("skill frontmatter contains duplicate keys")
                    keys.add(marker)
                    walk(key, depth + 1)
                    walk(value, depth + 1)
            elif isinstance(current, SequenceNode):
                for value in current.value:
                    walk(value, depth + 1)
        finally:
            active.remove(identity)

    if node is not None:
        walk(node, 0)


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
    expected_path: Path | None = None,
    expected_identity: PathIdentity | None = None,
) -> ParsedSkill:
    stable = _read_bytes(path)
    if expected_path is not None and stable.path != expected_path:
        raise _invalid("skill path changed after discovery")
    if expected_identity is not None and stable.identity != expected_identity:
        raise _invalid("skill file identity changed after discovery")
    raw = stable.content
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
        _reject_complex_or_duplicate_yaml(
            yaml.compose(frontmatter_text, Loader=yaml.SafeLoader)
        )
        loaded = yaml.safe_load(frontmatter_text)
    except RecursionError as error:
        raise _invalid("skill frontmatter exceeds complexity limits", error) from error
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
    metadata = SkillMetadata(
        name=parsed.name,
        description=parsed.description,
        location=stable.path,
        content_hash=content_hash,
        license=parsed.license,
        compatibility=parsed.compatibility,
        metadata=parsed.metadata,
        allowed_tools=_allowed_tools(parsed.allowed_tools),
    )
    return ParsedSkill(
        metadata=metadata,
        instructions=instructions,
        root=stable.path.parent,
        file_identity=stable.identity,
    )


__all__ = ["MAX_SKILL_FILE_BYTES", "ParsedSkill", "load_skill"]
