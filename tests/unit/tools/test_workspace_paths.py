from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_sdk.models.litellm_gateway import ToolCallCompleted
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.tools.builtins.bash import _resolve_bash_cwd
from agent_sdk.tools.builtins.workspace import resolve_workspace_path
from agent_sdk.tools.errors import ToolAccessDenied, ToolExecutionTimedOut
from agent_sdk.tools.executor import ToolExecutor
from agent_sdk.tools.models import ToolContext, ToolResult, ToolResultStatus, ToolSpec
from agent_sdk.tools.registry import ToolRegistry


def test_resolve_workspace_path_accepts_relative_child(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    resolved = resolve_workspace_path((root,), "notes/a.txt", for_write=True)

    assert resolved == root.resolve() / "notes" / "a.txt"


def test_resolve_workspace_path_accepts_absolute_child(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    child = root / "notes.txt"
    root.mkdir()
    child.write_text("hello", encoding="utf-8")

    resolved = resolve_workspace_path((root,), child, for_write=False)

    assert resolved == child.resolve()


def test_resolve_workspace_path_uses_first_containing_root(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    child = second / "notes.txt"
    child.write_text("hello", encoding="utf-8")

    resolved = resolve_workspace_path((first, second), child, for_write=False)

    assert resolved == child.resolve()


@pytest.mark.parametrize("requested", ("../outside.txt", "..\\outside.txt"))
def test_resolve_workspace_path_rejects_parent_escape(
    tmp_path: Path,
    requested: str,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(ToolAccessDenied, match="outside configured workspace"):
        resolve_workspace_path((root,), requested, for_write=True)


def test_resolve_workspace_path_rejects_sibling_prefix(tmp_path: Path) -> None:
    root = tmp_path / "work"
    sibling = tmp_path / "workspace"
    root.mkdir()
    sibling.mkdir()
    target = sibling / "notes.txt"
    target.write_text("outside", encoding="utf-8")

    with pytest.raises(ToolAccessDenied, match="outside configured workspace"):
        resolve_workspace_path((root,), target, for_write=False)


def test_resolve_workspace_path_rejects_existing_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"platform refused symlink creation: {error}")

    with pytest.raises(ToolAccessDenied, match="outside configured workspace"):
        resolve_workspace_path((root,), "link/secret.txt", for_write=True)


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows path primitive")
def test_resolve_workspace_path_rejects_existing_junction_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    junction = root / "junction"
    try:
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        pytest.skip(f"platform refused junction creation: {error}")
    if created.returncode != 0:
        pytest.skip(f"platform refused junction creation: {created.stderr}")

    with pytest.raises(ToolAccessDenied, match="outside configured workspace"):
        resolve_workspace_path((root,), "junction/secret.txt", for_write=True)


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows path primitive")
def test_resolve_workspace_path_rejects_dangling_junction_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    junction = root / "junction"
    try:
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        pytest.skip(f"platform refused junction creation: {error}")
    if created.returncode != 0:
        pytest.skip(f"platform refused junction creation: {created.stderr}")
    outside.rmdir()

    with pytest.raises(ToolAccessDenied, match="outside configured workspace"):
        resolve_workspace_path((root,), "junction/secret.txt", for_write=True)


def test_resolve_workspace_path_resolves_existing_parent_for_write(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    existing = root / "existing"
    existing.mkdir(parents=True)

    resolved = resolve_workspace_path(
        (root,),
        "existing/new/deep/file.txt",
        for_write=True,
    )

    assert resolved == existing.resolve() / "new" / "deep" / "file.txt"


def test_resolve_workspace_path_rejects_missing_read_target(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(ToolAccessDenied, match="path is unavailable"):
        resolve_workspace_path((root,), "missing.txt", for_write=False)


def test_resolve_workspace_path_rejects_empty_workspace() -> None:
    with pytest.raises(ToolAccessDenied, match="session has no workspace"):
        resolve_workspace_path((), "notes.txt", for_write=True)


@pytest.mark.parametrize(
    ("requested", "expected_message"),
    (
        ("", "invalid workspace path"),
        (".", "invalid workspace path"),
        ("notes/../secret.txt", "outside configured workspace"),
        ("notes.txt:secret", "invalid workspace path"),
        ("folder/name:stream", "invalid workspace path"),
        ("bad\0name", "invalid workspace path"),
    ),
)
def test_resolve_workspace_path_rejects_ambiguous_or_unsafe_input(
    tmp_path: Path,
    requested: str,
    expected_message: str,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(ToolAccessDenied, match=expected_message):
        resolve_workspace_path((root,), requested, for_write=True)


def test_configured_filesystem_root_is_a_valid_default_bash_cwd(
    tmp_path: Path,
) -> None:
    filesystem_root = Path(tmp_path.anchor)

    assert _resolve_bash_cwd((filesystem_root,), None) == filesystem_root.resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX filename semantics only")
@pytest.mark.parametrize("name", ("trailing.", "trailing "))
def test_posix_trailing_dot_and_space_names_remain_valid(
    tmp_path: Path,
    name: str,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    resolved = resolve_workspace_path((root,), name, for_write=True)

    assert resolved == root.resolve() / name


async def _noop_emit(_: str, __: dict[str, Any]) -> None:
    return None


async def _noop_permission(
    _: object,
    __: object,
) -> None:
    return None


async def _execute_marker(
    marker: type[ToolAccessDenied] | type[ToolExecutionTimedOut],
    message: str,
) -> tuple[ToolResult, list[tuple[str, dict[str, Any]]]]:
    async def handler(_: ToolContext) -> None:
        raise marker(message)

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="marker",
            description="raise an execution marker",
            input_schema={"type": "object", "additionalProperties": False},
        ),
        handler,
    )
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        events.append((event_type, payload))

    executor = ToolExecutor(registry, PolicyEngine(default_outcome="allow"), None)
    result = await executor.execute(
        ToolCallCompleted(0, "call-1", "marker", json.dumps({})),
        ToolContext(run_id="run-1", session_id="session-1"),
        emit=emit,
        on_permission_requested=_noop_permission,
        on_permission_resolved=_noop_permission,
    )
    return result, events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marker", "expected_status", "expected_error"),
    (
        (ToolAccessDenied, ToolResultStatus.DENIED, "tool access denied"),
        (
            ToolExecutionTimedOut,
            ToolResultStatus.TIMED_OUT,
            "tool execution timed out",
        ),
    ),
)
async def test_executor_maps_private_execution_markers_to_sanitized_results(
    marker: type[ToolAccessDenied] | type[ToolExecutionTimedOut],
    expected_status: ToolResultStatus,
    expected_error: str,
    tmp_path: Path,
) -> None:
    outside_secret = str(tmp_path.parent / "outside" / "secret.txt")

    result, events = await _execute_marker(marker, outside_secret)

    assert result.status is expected_status
    assert result.error == expected_error
    assert outside_secret not in result.content
    assert outside_secret not in str(events)
    completed = [payload for event, payload in events if event == "tool.call.completed"]
    assert completed == [result.model_dump(mode="json")]
