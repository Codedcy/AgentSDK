from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.skills.loader import load_skill
from agent_sdk.skills.models import ActivatedSkill, SkillMetadata


class SkillRegistry:
    def __init__(self, roots: Iterable[str | Path]) -> None:
        self._roots = tuple(Path(root) for root in roots)
        self._catalog: dict[str, SkillMetadata] = {}

    def discover(self) -> tuple[SkillMetadata, ...]:
        discovered: dict[str, SkillMetadata] = {}
        for configured_root in self._roots:
            real_root = self._resolve_root(configured_root)
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
                discovered[metadata.name] = metadata
        self._catalog = discovered
        return tuple(discovered[name] for name in sorted(discovered))

    def activate(self, name: str) -> ActivatedSkill:
        try:
            metadata = self._catalog[name]
        except KeyError as error:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "skill not found",
                retryable=False,
            ) from error
        parsed = load_skill(
            metadata.location,
            expected_directory_name=metadata.name,
            expected_hash=metadata.content_hash,
        )
        return ActivatedSkill(
            metadata=metadata,
            instructions=parsed.instructions,
            root=parsed.root,
        )

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
