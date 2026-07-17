from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from agent_sdk.models.litellm_gateway import ToolCallCompleted
from agent_sdk.permissions.broker import InProcessPermissionBridge
from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.permissions.rules import PermissionRule
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.tools import (
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
    register_builtin_tools,
)
from agent_sdk.tools.models import thaw_json
from agent_sdk.tools.builtins.files import _atomic_write


async def _noop_emit(_: str, __: dict[str, Any]) -> None:
    return None


async def _noop_transition(
    _: PermissionRequest,
    __: PermissionDecision | None,
) -> None:
    return None


async def _harness(
    workspace: Path | None,
    *,
    permission_default: str = "allow",
    permission_rules: Iterable[PermissionRule] = (),
    output_limit: int = 4096,
) -> tuple[
    ToolExecutor,
    ToolContext,
    InProcessPermissionBridge,
]:
    store = InMemoryStore()
    workspaces = () if workspace is None else (workspace,)
    session = await RuntimeCommands(store).create_session(workspaces=workspaces)
    registry = ToolRegistry()
    register_builtin_tools(
        registry=registry,
        store=store,
        output_limit=output_limit,
    )
    bridge = InProcessPermissionBridge()
    executor = ToolExecutor(
        registry,
        PolicyEngine(
            permission_default,  # type: ignore[arg-type]
            permission_rules,
        ),
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
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> ToolResult:
    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        if events is not None:
            events.append((event_type, payload))

    return await executor.execute(
        ToolCallCompleted(
            index=0,
            call_id=call_id,
            name=name,
            arguments_json=json.dumps(arguments),
        ),
        context,
        emit=emit if events is not None else _noop_emit,
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
    assert request.arguments["path"] == str((workspace / "hello.txt").resolve())
    assert result.status is ToolResultStatus.SUCCEEDED
    assert thaw_json(result.value)["content"] == "hello"


@pytest.mark.parametrize(
    ("name", "arguments"),
    (
        ("read", {"path": "secret.txt"}),
        (
            "write",
            {
                "path": "created.txt",
                "content": "must not be created",
            },
        ),
        (
            "bash",
            {
                "argv": [sys.executable, "-c", "print('must not run')"],
                "cwd": "child",
            },
        ),
        (
            "bash",
            {
                "argv": [sys.executable, "-c", "print('must not run')"],
            },
        ),
    ),
)
@pytest.mark.asyncio
async def test_canonical_workspace_rule_denies_relative_and_default_resources(
    tmp_path: Path,
    name: str,
    arguments: dict[str, object],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "child").mkdir()
    executor, context, _ = await _harness(
        workspace,
        permission_default="allow",
        permission_rules=(
            PermissionRule(
                outcome="deny",
                tool=name,
                path_prefix=workspace,
            ),
        ),
    )

    result = await _execute(
        executor,
        context,
        name,
        arguments,
        call_id=f"call-canonical-deny-{name}",
    )

    assert result.status is ToolResultStatus.DENIED
    assert not (workspace / "created.txt").exists()


@pytest.mark.asyncio
async def test_path_specific_ask_uses_canonical_workspace_resource(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "hello.txt"
    target.write_text("hello", encoding="utf-8")
    executor, context, bridge = await _harness(
        workspace,
        permission_default="deny",
        permission_rules=(
            PermissionRule(
                outcome="ask",
                tool="read",
                path_prefix=workspace,
            ),
        ),
    )

    execution = asyncio.create_task(
        _execute(
            executor,
            context,
            "read",
            {"path": "hello.txt"},
            call_id="call-path-ask",
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

    assert request.arguments["path"] == str(target.resolve())
    assert result.status is ToolResultStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_path_specific_allow_uses_canonical_workspace_resource(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor, context, _ = await _harness(
        workspace,
        permission_default="deny",
        permission_rules=(
            PermissionRule(
                outcome="allow",
                tool="write",
                path_prefix=workspace,
            ),
        ),
    )

    result = await _execute(
        executor,
        context,
        "write",
        {"path": "allowed.txt", "content": "allowed"},
        call_id="call-path-allow",
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert (workspace / "allowed.txt").read_text(encoding="utf-8") == "allowed"


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
    events: list[tuple[str, dict[str, Any]]] = []

    result = await _execute(
        executor,
        context,
        name,
        arguments,
        call_id=f"call-outside-{name}",
        events=events,
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.error == "tool access denied"
    assert str(tmp_path) not in result.content
    assert [event_type for event_type, _ in events] == ["tool.call.completed"]
    assert str(tmp_path) not in json.dumps(events)
    assert outside_file.read_text(encoding="utf-8") == "untouched"


@pytest.mark.asyncio
async def test_application_tool_without_permission_resolver_keeps_raw_arguments() -> None:
    observed: list[str] = []

    async def handler(_: ToolContext, path: str) -> str:
        observed.append(path)
        return path

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="application.inspect",
            description="Inspect one application path.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        handler,
    )
    bridge = InProcessPermissionBridge()
    executor = ToolExecutor(registry, PolicyEngine("ask"), bridge)
    context = ToolContext(run_id="run-application", session_id="session-application")
    execution = asyncio.create_task(
        _execute(
            executor,
            context,
            "application.inspect",
            {"path": "relative.txt"},
            call_id="call-application",
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

    assert request.arguments["path"] == "relative.txt"
    assert observed == ["relative.txt"]
    assert result.status is ToolResultStatus.SUCCEEDED


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


def test_write_no_clobber_is_atomic_against_concurrent_creator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    real_link = os.link

    def install_competitor(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        target.write_text("competitor", encoding="utf-8")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", install_competitor)

    with pytest.raises(FileExistsError):
        _atomic_write(target, b"sdk-content", overwrite=False)

    assert target.read_text(encoding="utf-8") == "competitor"
    assert list(workspace.iterdir()) == [target]


def test_write_failure_removes_only_its_owned_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    unrelated = workspace / ".agent-sdk-unrelated.tmp"
    unrelated.write_text("keep", encoding="utf-8")

    def fail_replace(_: Path, __: Path) -> None:
        raise OSError("injected install failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected install failure"):
        _atomic_write(target, b"sdk-content", overwrite=True)

    assert not target.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert list(workspace.iterdir()) == [unrelated]


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


@pytest.mark.parametrize(
    ("argv", "expected"),
    (
        ([], ToolResultStatus.INVALID_ARGUMENTS),
        (["bad\0argv"], ToolResultStatus.DENIED),
    ),
)
@pytest.mark.asyncio
async def test_bash_rejects_empty_or_nul_argv(
    tmp_path: Path,
    argv: list[str],
    expected: ToolResultStatus,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor, context, _ = await _harness(workspace)

    result = await _execute(
        executor,
        context,
        "bash",
        {"argv": argv},
        call_id="call-invalid-bash",
    )

    assert result.status is expected


@pytest.mark.asyncio
async def test_bash_empty_workspace_is_denied() -> None:
    executor, context, _ = await _harness(None)

    result = await _execute(
        executor,
        context,
        "bash",
        {"argv": [sys.executable, "-c", "print('must not run')"]},
        call_id="call-empty-workspace",
    )

    assert result.status is ToolResultStatus.DENIED
    assert result.error == "tool access denied"


@pytest.mark.asyncio
async def test_bash_cancellation_waits_for_child_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor, context, _ = await _harness(workspace)
    created = asyncio.Event()
    process: asyncio.subprocess.Process | None = None
    real_create = asyncio.create_subprocess_exec

    async def capture_process(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        nonlocal process
        process = await real_create(*args, **kwargs)
        created.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)
    execution = asyncio.create_task(
        _execute(
            executor,
            context,
            "bash",
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(10)",
                ]
            },
            call_id="call-cancel-bash",
        )
    )
    await asyncio.wait_for(created.wait(), timeout=1)

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert process is not None
    for _ in range(100):
        if process.returncode is not None:
            break
        await asyncio.sleep(0.01)

    assert process.returncode is not None


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
