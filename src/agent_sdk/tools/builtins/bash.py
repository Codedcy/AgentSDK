from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_sdk.storage.base import StateStore
from agent_sdk.tools.builtins.files import workspace_roots
from agent_sdk.tools.builtins.workspace import resolve_workspace_path
from agent_sdk.tools.errors import ToolAccessDenied, ToolExecutionTimedOut
from agent_sdk.tools.models import ToolContext, bounded_text

_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAXIMUM_TIMEOUT_SECONDS = 300.0
_READ_SIZE = 8192
# ToolResult's durable JSON envelope is 16 KiB; 2 KiB remains safe even when
# every captured byte requires JSON escaping.
_DURABLE_PREVIEW_BYTES = 2048


@dataclass
class _OutputBudget:
    remaining: int
    truncated: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def capture(self, chunk: bytes) -> bytes:
        async with self.lock:
            retained = chunk[: self.remaining]
            self.remaining -= len(retained)
            if len(retained) != len(chunk):
                self.truncated = True
            return retained


async def run_bash(
    context: ToolContext,
    argv: list[str],
    cwd: str | None = None,
    timeout_seconds: float | None = None,
    *,
    store: StateStore,
    output_limit: int,
) -> dict[str, object]:
    if not argv or any(not isinstance(item, str) or "\0" in item for item in argv):
        raise ToolAccessDenied("invalid process arguments")

    roots = await workspace_roots(store, context.session_id, run_id=context.run_id)
    canonical_cwd = _resolve_bash_cwd(roots, cwd)

    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(canonical_cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    budget = _OutputBudget(min(output_limit, _DURABLE_PREVIEW_BYTES))
    stdout_task = asyncio.create_task(_drain_bounded(process.stdout, budget))
    stderr_task = asyncio.create_task(_drain_bounded(process.stderr, budget))
    timeout = min(
        timeout_seconds
        if timeout_seconds is not None
        else _DEFAULT_TIMEOUT_SECONDS,
        _MAXIMUM_TIMEOUT_SECONDS,
    )
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            raise ToolExecutionTimedOut("bash command timed out") from None
    except asyncio.CancelledError:
        process.kill()
        await asyncio.shield(process.wait())
        await asyncio.shield(asyncio.gather(stdout_task, stderr_task))
        raise

    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    return {
        "exit_code": process.returncode,
        "stdout": _decode_bounded(stdout),
        "stderr": _decode_bounded(stderr),
        "truncated": budget.truncated,
    }


async def bash_permission_arguments(
    context: ToolContext,
    arguments: Mapping[str, Any],
    *,
    store: StateStore,
) -> Mapping[str, Any]:
    argv = arguments.get("argv")
    if (
        not isinstance(argv, (list, tuple))
        or not argv
        or any(not isinstance(item, str) or "\0" in item for item in argv)
    ):
        raise ToolAccessDenied("invalid process arguments")
    cwd = arguments.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ToolAccessDenied("invalid process cwd")
    roots = await workspace_roots(store, context.session_id, run_id=context.run_id)
    canonical_cwd = _resolve_bash_cwd(roots, cwd)
    return {**arguments, "cwd": str(canonical_cwd)}


def _resolve_bash_cwd(
    roots: tuple[Path, ...],
    cwd: str | None,
) -> Path:
    requested: str | Path
    if cwd is not None:
        requested = cwd
    elif roots:
        requested = roots[0]
    else:
        requested = Path(".")
    canonical = resolve_workspace_path(
        roots,
        requested,
        for_write=False,
    )
    if not canonical.is_dir():
        raise ToolAccessDenied("process cwd is unavailable")
    return canonical


async def _drain_bounded(
    stream: asyncio.StreamReader,
    budget: _OutputBudget,
) -> bytes:
    captured = bytearray()
    while True:
        chunk = await stream.read(_READ_SIZE)
        if not chunk:
            return bytes(captured)
        captured.extend(await budget.capture(chunk))


def _decode_bounded(value: bytes) -> str:
    return bounded_text(
        value.decode("utf-8", errors="replace"),
        max_bytes=len(value),
    )


__all__ = ["bash_permission_arguments", "run_bash"]
