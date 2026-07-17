from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_sdk.permissions import (
    PermissionRequest,
    PermissionRule,
    PolicyEngine,
)


def request(tool: str, arguments: dict[str, object]) -> PermissionRequest:
    return PermissionRequest(
        request_id="permission-1",
        run_id="run-1",
        session_id="session-1",
        tool_name=tool,
        arguments=arguments,
        effects=(f"builtin.{tool}",),
    )


def test_explicit_deny_beats_more_general_allow(tmp_path: Path) -> None:
    policy = PolicyEngine(
        default_outcome="ask",
        rules=(
            PermissionRule(outcome="allow", tool="read", path_prefix=tmp_path),
            PermissionRule(
                outcome="deny",
                tool="read",
                path_prefix=tmp_path / "secrets",
            ),
        ),
    )

    decision = policy.evaluate(
        request("read", {"path": str(tmp_path / "secrets" / "key.txt")})
    )

    assert decision.action == "deny"


def test_explicit_deny_beats_a_more_specific_allow(tmp_path: Path) -> None:
    policy = PolicyEngine(
        default_outcome="ask",
        rules=(
            PermissionRule(
                outcome="allow",
                tool="read",
                path_prefix=tmp_path / "workspace",
            ),
            PermissionRule(outcome="deny"),
        ),
    )

    decision = policy.evaluate(
        request("read", {"path": str(tmp_path / "workspace" / "notes.txt")})
    )

    assert decision.action == "deny"


def test_longest_matching_command_prefix_wins() -> None:
    policy = PolicyEngine(
        default_outcome="deny",
        rules=(
            PermissionRule(outcome="ask", tool="bash", command_prefix=("git",)),
            PermissionRule(
                outcome="allow",
                tool="bash",
                command_prefix=("git", "status"),
            ),
        ),
    )

    assert policy.evaluate(request("bash", {"argv": ["git", "status"]})).allowed


def test_no_matching_rule_uses_default() -> None:
    policy = PolicyEngine(
        default_outcome="ask",
        rules=(PermissionRule(outcome="allow", tool="write"),),
    )

    assert policy.evaluate(request("read", {"path": "notes.txt"})).action == "ask"


def test_execution_config_returns_canonical_immutable_rule_snapshot() -> None:
    policy = PolicyEngine(
        rules=(
            PermissionRule(
                outcome="allow",
                tool="bash",
                command_prefix=("git", "status"),
            ),
        ),
    )

    snapshot = policy.execution_config()
    rule = snapshot["permission_rules"][0]

    assert rule == {
        "outcome": "allow",
        "tool": "bash",
        "path_prefix": None,
        "command_prefix": ("git", "status"),
    }
    with pytest.raises(TypeError):
        rule["tool"] = "write"


def test_path_prefix_respects_component_boundary(tmp_path: Path) -> None:
    work = tmp_path / "work"
    workspace_file = tmp_path / "workspace" / "notes.txt"
    policy = PolicyEngine(
        default_outcome="deny",
        rules=(PermissionRule(outcome="allow", tool="read", path_prefix=work),),
    )

    assert policy.evaluate(request("read", {"path": str(work / "notes.txt")})).allowed
    assert (
        policy.evaluate(request("read", {"path": str(workspace_file)})).action
        == "deny"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison behavior")
def test_path_prefix_normalizes_case_on_windows(tmp_path: Path) -> None:
    root = tmp_path / "Work"
    target = root / "Notes.txt"
    policy = PolicyEngine(
        default_outcome="deny",
        rules=(
            PermissionRule(
                outcome="allow",
                tool="read",
                path_prefix=Path(str(root).swapcase()),
            ),
        ),
    )

    assert policy.evaluate(request("read", {"path": str(target)})).allowed
