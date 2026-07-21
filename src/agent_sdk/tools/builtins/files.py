from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_sdk.runtime.models import RunSnapshot, SessionSnapshot
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    StateStore,
)
from agent_sdk.tools.builtins.workspace import (
    canonical_workspace_scope,
    resolve_workspace_path,
)
from agent_sdk.tools.errors import ToolAccessDenied
from agent_sdk.tools.models import ToolContext

# ToolResult's durable JSON envelope is 16 KiB; 2 KiB remains safe even when
# every captured byte requires JSON escaping.
_DURABLE_PREVIEW_BYTES = 2048


@dataclass(frozen=True)
class WorkspaceBoundaries:
    capability_roots: tuple[Path, ...]
    session_roots: tuple[Path, ...]


async def workspace_boundaries(
    store: StateStore,
    session_id: str,
    *,
    run_id: str | None = None,
) -> WorkspaceBoundaries:
    try:
        session_data = await store.get_snapshot("session", session_id)
        if session_data is None:
            raise ValueError
        session = SessionSnapshot.model_validate(session_data)
        if any(not Path(root).is_absolute() for root in session.workspaces):
            raise ValueError
        session_roots = tuple(
            canonical_workspace_scope(root) for root in session.workspaces
        )
    except Exception as error:
        raise ToolAccessDenied("session workspace is unavailable") from error
    if run_id is not None:
        try:
            run_data = await store.get_snapshot("run", run_id)
            if run_data is None:
                raise ValueError
            run = RunSnapshot.model_validate(run_data)
            if run.session_id != session_id:
                raise ValueError
            await store.commit(
                CommitBatch(
                    events=(),
                    preconditions=(
                        SnapshotPrecondition(
                            "session",
                            session_id,
                            session_id=session_id,
                            data=session_data,
                        ),
                        SnapshotPrecondition(
                            "run",
                            run_id,
                            session_id=session_id,
                            data=run_data,
                        ),
                    ),
                )
            )
            descriptor = run.execution_descriptor
            if descriptor is not None and descriptor.workspace_scopes is not None:
                if any(
                    not Path(scope).is_absolute()
                    for scope in descriptor.workspace_scopes
                ):
                    raise ValueError
                scopes = tuple(
                    canonical_workspace_scope(scope)
                    for scope in descriptor.workspace_scopes
                )
                if any(
                    not any(_is_within(scope, root) for root in session_roots)
                    for scope in scopes
                ):
                    raise ValueError
                return WorkspaceBoundaries(scopes, session_roots)
        except SnapshotPreconditionError as error:
            raise ToolAccessDenied("run workspace is unavailable") from error
        except Exception as error:
            raise ToolAccessDenied("run workspace is unavailable") from error
    return WorkspaceBoundaries(session_roots, session_roots)


async def workspace_roots(
    store: StateStore,
    session_id: str,
    *,
    run_id: str | None = None,
) -> tuple[Path, ...]:
    boundaries = await workspace_boundaries(store, session_id, run_id=run_id)
    return boundaries.capability_roots


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def relative_display_path(target: Path, roots: tuple[Path, ...]) -> str:
    for root in roots:
        try:
            relative = target.relative_to(root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            continue
        return relative.as_posix()
    raise ToolAccessDenied("path is outside configured workspace")


async def read_file(
    context: ToolContext,
    path: str,
    max_bytes: int | None = None,
    *,
    store: StateStore,
    output_limit: int,
) -> dict[str, object]:
    boundaries = await workspace_boundaries(
        store,
        context.session_id,
        run_id=context.run_id,
    )
    target = resolve_workspace_path(
        boundaries.capability_roots,
        path,
        for_write=False,
        containment_roots=boundaries.session_roots,
    )
    limit = min(
        max_bytes if max_bytes is not None else output_limit,
        output_limit,
        _DURABLE_PREVIEW_BYTES,
    )
    preview, truncated = await asyncio.to_thread(_read_prefix, target, limit)
    return {
        "path": relative_display_path(target, boundaries.capability_roots),
        "content": preview.decode("utf-8", errors="replace"),
        "truncated": truncated,
        "bytes_read": len(preview),
    }


async def file_permission_arguments(
    context: ToolContext,
    arguments: Mapping[str, Any],
    *,
    store: StateStore,
    for_write: bool,
) -> Mapping[str, Any]:
    boundaries = await workspace_boundaries(
        store,
        context.session_id,
        run_id=context.run_id,
    )
    requested = arguments.get("path")
    if not isinstance(requested, str):
        raise ToolAccessDenied("invalid workspace path")
    target = resolve_workspace_path(
        boundaries.capability_roots,
        requested,
        for_write=for_write,
        containment_roots=boundaries.session_roots,
    )
    return {**arguments, "path": str(target)}


async def write_file(
    context: ToolContext,
    path: str,
    content: str,
    overwrite: bool = False,
    *,
    store: StateStore,
    output_limit: int,
) -> dict[str, object]:
    del output_limit
    boundaries = await workspace_boundaries(
        store,
        context.session_id,
        run_id=context.run_id,
    )
    target = resolve_workspace_path(
        boundaries.capability_roots,
        path,
        for_write=True,
        containment_roots=boundaries.session_roots,
    )
    encoded = content.encode("utf-8")
    await asyncio.to_thread(_atomic_write, target, encoded, overwrite)
    return {
        "path": relative_display_path(target, boundaries.capability_roots),
        "bytes_written": len(encoded),
    }


def _read_prefix(target: Path, limit: int) -> tuple[bytes, bool]:
    with target.open("rb") as source:
        captured = source.read(limit + 1)
    return captured[:limit], len(captured) > limit


def _atomic_write(
    target: Path,
    content: bytes,
    overwrite: bool,
) -> None:
    descriptor = -1
    owned_temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".agent-sdk-",
            suffix=".tmp",
            dir=target.parent,
        )
        owned_temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        if overwrite:
            os.replace(owned_temporary, target)
        else:
            os.link(owned_temporary, target)
            owned_temporary.unlink()
        owned_temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if owned_temporary is not None:
            try:
                owned_temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = [
    "read_file",
    "file_permission_arguments",
    "relative_display_path",
    "WorkspaceBoundaries",
    "workspace_boundaries",
    "workspace_roots",
    "write_file",
]
