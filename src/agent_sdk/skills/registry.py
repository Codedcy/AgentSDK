from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.skills.loader import load_skill
from agent_sdk.skills.models import (
    ActivatedSkill,
    PathIdentity,
    SkillMetadata,
    _path_identity,
)


@dataclass(frozen=True)
class _CatalogEntry:
    metadata: SkillMetadata
    configured_root: Path
    configured_root_identity: PathIdentity
    skill_root: Path
    skill_root_identity: PathIdentity
    file_identity: PathIdentity


class SkillRegistry:
    def __init__(self, roots: Iterable[str | Path]) -> None:
        self._roots = tuple(Path(root) for root in roots)
        self._catalog: dict[str, _CatalogEntry] = {}

    def discover(self) -> tuple[SkillMetadata, ...]:
        discovered: dict[str, _CatalogEntry] = {}
        for configured_root in self._roots:
            real_root = self._resolve_root(configured_root)
            configured_root_identity = _path_identity(real_root)
            try:
                children = sorted(real_root.iterdir(), key=lambda path: path.name)
            except OSError as error:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "failed to enumerate skill root",
                    retryable=False,
                ) from error
            for child in children:
                if not child.is_dir():
                    continue
                real_skill_root = self._contained(child, real_root)
                skill_root_identity = _path_identity(real_skill_root)
                skill_file = child / "SKILL.md"
                if not skill_file.exists():
                    continue
                real_skill_file = self._contained(skill_file, real_skill_root)
                parsed = load_skill(
                    real_skill_file,
                    expected_directory_name=child.name,
                )
                metadata = parsed.metadata
                if metadata.name in discovered:
                    raise AgentSDKError(
                        ErrorCode.CONFLICT,
                        "duplicate skill name",
                        retryable=False,
                    )
                self._verify_directory(real_root, configured_root_identity)
                self._verify_directory(real_skill_root, skill_root_identity)
                if parsed.root != real_skill_root:
                    raise AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "skill path changed during discovery",
                        retryable=False,
                    )
                discovered[metadata.name] = _CatalogEntry(
                    metadata=metadata,
                    configured_root=real_root,
                    configured_root_identity=configured_root_identity,
                    skill_root=real_skill_root,
                    skill_root_identity=skill_root_identity,
                    file_identity=parsed.file_identity,
                )
        self._catalog = discovered
        return tuple(discovered[name].metadata for name in sorted(discovered))

    def activate(self, name: str) -> ActivatedSkill:
        try:
            entry = self._catalog[name]
        except KeyError as error:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "skill not found",
                retryable=False,
            ) from error
        metadata = entry.metadata
        self._verify_entry(entry)
        parsed = load_skill(
            metadata.location,
            expected_directory_name=metadata.name,
            expected_hash=metadata.content_hash,
            expected_path=metadata.location,
            expected_identity=entry.file_identity,
        )
        self._verify_entry(entry)
        if (
            parsed.root != entry.skill_root
            or parsed.metadata.location != metadata.location
            or parsed.file_identity != entry.file_identity
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill identity changed during activation",
                retryable=False,
            )
        return ActivatedSkill._from_pinned(
            metadata=metadata,
            instructions=parsed.instructions,
            root=entry.skill_root,
            root_identity=entry.skill_root_identity,
        )

    @classmethod
    def _verify_entry(cls, entry: _CatalogEntry) -> None:
        cls._verify_directory(
            entry.configured_root,
            entry.configured_root_identity,
        )
        cls._verify_directory(entry.skill_root, entry.skill_root_identity)
        try:
            entry.skill_root.relative_to(entry.configured_root)
            entry.metadata.location.relative_to(entry.skill_root)
        except ValueError as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill path escapes configured root",
                retryable=False,
            ) from error

    @staticmethod
    def _verify_directory(path: Path, expected: PathIdentity) -> None:
        try:
            resolved = path.resolve(strict=True)
            if (
                resolved != path
                or not resolved.is_dir()
                or _path_identity(resolved) != expected
            ):
                raise ValueError("directory identity changed")
        except (OSError, ValueError) as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill directory identity changed",
                retryable=False,
            ) from error

    @staticmethod
    def _resolve_root(root: Path) -> Path:
        try:
            resolved = root.resolve(strict=True)
        except OSError as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill root does not exist",
                retryable=False,
            ) from error
        if not resolved.is_dir():
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill root must be a directory",
                retryable=False,
            )
        return resolved

    @staticmethod
    def _contained(path: Path, root: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "skill path escapes configured root",
                retryable=False,
            ) from error
        return resolved


__all__ = ["SkillRegistry"]
