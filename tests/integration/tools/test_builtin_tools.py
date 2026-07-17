from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_sdk.models.litellm_gateway import ToolCallCompleted
from agent_sdk.permissions.broker import InProcessPermissionBridge
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.tools import (
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
    register_builtin_tools,
)
from agent_sdk.tools.models import thaw_json


async def _noop_emit(_: str, __: dict[str, Any]) -> None:
    return None


async def _noop_transition(
    _: PermissionRequest,
    __: PermissionDecision | None,
) -> None:
    return None


async def _harness(
    workspace: Path,
    *,
    permission_default: str = "allow",
    output_limit: int = 4096,
) -> tuple[
    ToolExecutor,
    ToolContext,
    InProcessPermissionBridge,
]:
    store = InMemoryStore()
    session = await RuntimeCommands(store).create_session(workspaces=(workspace,))
    registry = ToolRegistry()
    register_builtin_tools(
        registry=registry,
        store=store,
        output_limit=output_limit,
    )
    bridge = InProcessPermissionBridge()
    executor = ToolExecutor(
        registry,
        PolicyEngine(permission_default),  # type: ignore[arg-type]
        bridge,
    )
    return (
        executor,
        ToolContext(run_id="run-builtins", session_id=session.session_id),
        bridge,
    )


async def _execute(
    executor: ToolExecutor,
    context: ToolContext,
    name: str,
    arguments: dict[str, object],
    *,
    call_id: str,
) -> ToolResult:
    return await executor.execute(
        ToolCallCompleted(
            index=0,
            call_id=call_id,
            name=name,
            arguments_json=json.dumps(arguments),
        ),
        context,
        emit=_noop_emit,
        on_permission_requested=_noop_transition,
        on_permission_resolved=_noop_transition,
    )


@pytest.mark.asyncio
async def test_allowed_read_returns_bounded_workspace_relative_preview(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_bytes(b"hello!")
    executor, context, _ = await _harness(workspace)

    result = await _execute(
        executor,
        context,
        "read",
        {"path": "hello.txt", "max_bytes": 5},
        call_id="call-read",
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert thaw_json(result.value) == {
        "path": "hello.txt",
        "content": "hello",
        "truncated": True,
        "bytes_read": 5,
    }


@pytest.mark.asyncio
async def test_ask_read_waits_for_existing_permission_resolution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello", encoding="utf-8")
    executor, context, bridge = await _harness(
        workspace,
        permission_default="ask",
    )

    execution = asyncio.create_task(
        _execute(
            executor,
            context,
            "read",
            {"path": "hello.txt"},
            call_id="call-ask-read",
        )
    )
    request = await asyncio.wait_for(
        bridge.next_request(context.run_id),
        timeout=1,
    )
    resolution = asyncio.create_task(
        bridge.resolve(request.request_id, PermissionDecision.allow_once())
    )
    result = await asyncio.wait_for(execution, timeout=1)
    await asyncio.wait_for(resolution, timeout=1)

    assert request.tool_name == "read"
    assert request.arguments["path"] == "hello.txt"
    assert result.status is ToolResultStatus.SUCCEEDED
    assert thaw_json(result.value)["content"] == "hello"


@pytest.mark.asyncio
async def test_denied_write_leaves_target_untouched(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("original", encoding="utf-8")
    executor, context, _ = await _harness(
        workspace,
        permission_default="deny",
    )

    result = await _execute(
        executor,
        context,
        "write",
        {"path": "note.txt", "content": "changed", "overwrite": True},
        call_id="call-denied-write",
    )

    assert result.status is ToolResultStatus.DENIED
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(
    ("name", "arguments"),
    (
        ("read", {"path": "outside.txt"}),
        ("write", {"path": "outside.txt", "content": "changed"}),
        ("bash", {"argv": [sys.executable, "-c", "print('no')"], "cwd": "outside"}),
    ),
)
@pytest.mark.asyncio
async def test_global_allow_cannot_escape_workspace(
    tmp_path: Path,
    name: str,
    arguments: dict[str, object],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("untouched", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    arguments = dict(arguments)
    if name in {"read", "write"}:
        arguments["path"] = str(outside_file)
    else:
        arguments["cwd"] = str(outside_dir)
    executor, context, _ = await _harness(workspace)

    result = await _execute(
        executor,
        context,
        name,
        arguments,
        call_id=f"call-outside-{name}",
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.error == "tool access denied"
    assert str(tmp_path) not in result.content
    assert outside_file.read_text(encoding="utf-8") == "untouched"


@pytest.mark.asyncio
async def test_write_replaces_atomically_only_when_overwrite_is_enabled(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("original", encoding="utf-8")
    executor, context, _ = await _harness(workspace)

    refused = await _execute(
        executor,
        context,
        "write",
        {"path": "note.txt", "content": "refused"},
        call_id="call-write-refused",
    )
    written = await _execute(
        executor,
        context,
        "write",
        {"path": "note.txt", "content": "replacement", "overwrite": True},
        call_id="call-write-replace",
    )

    assert refused.status is ToolResultStatus.FAILED
    assert written.status is ToolResultStatus.SUCCEEDED
    assert thaw_json(written.value) == {
        "path": "note.txt",
        "bytes_written": len(b"replacement"),
    }
    assert target.read_text(encoding="utf-8") == "replacement"
    assert list(workspace.iterdir()) == [target]


@pytest.mark.asyncio
async def test_bash_runs_argv_in_contained_cwd_and_reports_both_streams(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = workspace / "child"
    child.mkdir()
    executor, context, _ = await _harness(workspace)

    result = await _execute(
        executor,
        context,
        "bash",
        {
            "argv": [
                sys.executable,
                "-c",
                (
                    "import pathlib,sys;"
                    "print(pathlib.Path.cwd().name);"
                    "print('problem', file=sys.stderr)"
                ),
            ],
            "cwd": "child",
        },
        call_id="call-bash",
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    value = thaw_json(result.value)
    assert value["exit_code"] == 0
    assert value["stdout"].splitlines() == ["child"]
    assert value["stderr"].splitlines() == ["problem"]
    assert value["truncated"] is False


@pytest.mark.asyncio
async def test_bash_timeout_is_normalized(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor, context, _ = await _harness(workspace)

    result = await asyncio.wait_for(
        _execute(
            executor,
            context,
            "bash",
            {
                "argv": [sys.executable, "-c", "import time; time.sleep(10)"],
                "timeout_seconds": 0.05,
            },
            call_id="call-bash-timeout",
        ),
        timeout=2,
    )

    assert result.status is ToolResultStatus.TIMED_OUT
    assert result.error == "tool execution timed out"


@pytest.mark.asyncio
async def test_bash_combined_output_is_bounded_and_marked_truncated(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor, context, _ = await _harness(workspace, output_limit=1024)

    result = await _execute(
        executor,
        context,
        "bash",
        {
            "argv": [
                sys.executable,
                "-c",
                "import sys; print('o' * 4000); print('e' * 4000, file=sys.stderr)",
            ]
        },
        call_id="call-bash-bounded",
    )
    value = thaw_json(result.value)

    assert result.status is ToolResultStatus.SUCCEEDED
    assert value["truncated"] is True
    assert len(value["stdout"].encode()) + len(value["stderr"].encode()) <= 1024


@pytest.mark.asyncio
async def test_default_builtin_limit_still_fits_the_durable_tool_result(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor, context, _ = await _harness(
        workspace,
        output_limit=64 * 1024,
    )

    result = await _execute(
        executor,
        context,
        "bash",
        {
            "argv": [
                sys.executable,
                "-c",
                "print('x' * 20000)",
            ]
        },
        call_id="call-bash-default-bounded",
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert thaw_json(result.value)["truncated"] is True
