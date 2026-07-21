from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_sdk.runtime.models import RunSnapshot, SessionSnapshot
from agent_sdk.storage.base import StateStore
from agent_sdk.tools.builtins.workspace import resolve_workspace_path
from agent_sdk.tools.errors import ToolAccessDenied
from agent_sdk.tools.models import ToolContext

# ToolResult's durable JSON envelope is 16 KiB; 2 KiB remains safe even when
# every captured byte requires JSON escaping.
_DURABLE_PREVIEW_BYTES = 2048


async def workspace_roots(
    store: StateStore,
    session_id: str,
    *,
    run_id: str | None = None,
) -> tuple[Path, ...]:
    try:
        data = await store.get_snapshot("session", session_id)
        if data is None:
            raise ValueError
        session = SessionSnapshot.model_validate(data)
    except Exception as error:
        raise ToolAccessDenied("session workspace is unavailable") from error
    roots = tuple(Path(root) for root in session.workspaces)
    if run_id is not None:
        try:
            run_data = await store.get_snapshot("run", run_id)
            if run_data is None:
                raise ValueError
            run = RunSnapshot.model_validate(run_data)
            if run.session_id != session_id:
                raise ValueError
            descriptor = run.execution_descriptor
            if descriptor is not None and descriptor.workspace_scopes is not None:
                roots = tuple(Path(root) for root in descriptor.workspace_scopes)
        except Exception as error:
            raise ToolAccessDenied("run workspace is unavailable") from error
    if any(not root.is_absolute() for root in roots):
        raise ToolAccessDenied("session workspace is unavailable")
    return roots


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
    roots = await workspace_roots(store, context.session_id, run_id=context.run_id)
    target = resolve_workspace_path(roots, path, for_write=False)
    limit = min(
        max_bytes if max_bytes is not None else output_limit,
        output_limit,
        _DURABLE_PREVIEW_BYTES,
    )
    preview, truncated = await asyncio.to_thread(_read_prefix, target, limit)
    return {
        "path": relative_display_path(target, roots),
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
    roots = await workspace_roots(store, context.session_id, run_id=context.run_id)
    requested = arguments.get("path")
    if not isinstance(requested, str):
        raise ToolAccessDenied("invalid workspace path")
    target = resolve_workspace_path(
        roots,
        requested,
        for_write=for_write,
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
    roots = await workspace_roots(store, context.session_id, run_id=context.run_id)
    target = resolve_workspace_path(roots, path, for_write=True)
    encoded = content.encode("utf-8")
    await asyncio.to_thread(_atomic_write, target, encoded, overwrite)
    return {
        "path": relative_display_path(target, roots),
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
    "workspace_roots",
    "write_file",
]
