from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from agent_sdk.tools.errors import ToolAccessDenied


def resolve_workspace_path(
    roots: Sequence[Path],
    requested: str | Path,
    *,
    for_write: bool,
    containment_roots: Sequence[Path] | None = None,
) -> Path:
    """Resolve a path while keeping it inside one configured workspace root."""
    if not roots:
        raise ToolAccessDenied("session has no workspace")

    candidate = _validated_path(requested)
    try:
        canonical_containment = (
            ()
            if containment_roots is None
            else tuple(Path(root).resolve(strict=True) for root in containment_roots)
        )
    except (OSError, RuntimeError) as error:
        raise ToolAccessDenied("path is outside configured workspace") from error
    for root in roots:
        try:
            canonical_root = Path(root).resolve(strict=True)
            if canonical_containment and not any(
                canonical_root.is_relative_to(ancestor)
                for ancestor in canonical_containment
            ):
                continue
            raw = candidate if candidate.is_absolute() else canonical_root / candidate
            canonical = (
                _resolve_with_existing_parent(raw)
                if for_write
                else raw.resolve(strict=True)
            )
        except (OSError, RuntimeError):
            continue
        if canonical.is_relative_to(canonical_root) and (
            not canonical_containment
            or any(canonical.is_relative_to(ancestor) for ancestor in canonical_containment)
        ):
            return canonical

    if not for_write and not candidate.is_absolute():
        raise ToolAccessDenied("path is unavailable")
    raise ToolAccessDenied("path is outside configured workspace")


def canonical_workspace_scope(value: str | Path) -> Path:
    """Normalize a durable workspace scope with the same path rules as tools."""
    return _resolve_with_existing_parent(_validated_path(value))


def _validated_path(requested: str | Path) -> Path:
    try:
        raw = os.fspath(requested)
    except TypeError as error:
        raise ToolAccessDenied("invalid workspace path") from error
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise ToolAccessDenied("invalid workspace path")

    candidate = Path(raw)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    filesystem_root = bool(candidate.anchor) and candidate == Path(candidate.anchor)
    if (not parts and not filesystem_root) or any(
        part in {"", "."} for part in parts
    ):
        raise ToolAccessDenied("invalid workspace path")
    if any(part == ".." for part in parts):
        raise ToolAccessDenied("path is outside configured workspace")
    if any(":" in part for part in parts):
        raise ToolAccessDenied("invalid workspace path")
    if os.name == "nt" and any(part.endswith((".", " ")) for part in parts):
        raise ToolAccessDenied("invalid workspace path")

    alternate_parts = raw.replace("\\", "/").split("/")
    if any(part == ".." for part in alternate_parts):
        raise ToolAccessDenied("path is outside configured workspace")
    if any(part == "." for part in alternate_parts):
        raise ToolAccessDenied("invalid workspace path")
    if os.name == "nt" and any(
        part.endswith((".", " ")) for part in alternate_parts if part
    ):
        raise ToolAccessDenied("invalid workspace path")
    return candidate


def _resolve_with_existing_parent(path: Path) -> Path:
    current = path
    missing: list[str] = []
    while not _exists_or_link(current):
        name = current.name
        if not name or name in {".", ".."} or ":" in name:
            raise ToolAccessDenied("invalid workspace path")
        missing.append(name)
        parent = current.parent
        if parent == current:
            raise ToolAccessDenied("path is unavailable")
        current = parent

    canonical = current.resolve(strict=True)
    for name in reversed(missing):
        canonical /= name
    return canonical


def _exists_or_link(path: Path) -> bool:
    return path.exists() or path.is_symlink() or path.is_junction()


__all__ = ["canonical_workspace_scope", "resolve_workspace_path"]
