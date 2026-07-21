from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from examples.v01_reference import run_reference


def test_v01_reference_smoke(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            "examples/v01_reference.py",
            "--smoke",
            "--database",
            str(tmp_path / "sdk.sqlite3"),
            "--workspace",
            str(tmp_path / "workspace"),
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("\n") == 1
    result = json.loads(completed.stdout)
    assert result["run_status"] == "completed"
    assert result["workflow_status"] == "completed"
    assert result["child_status"] == "completed"
    assert result["context_levels"] == ["L0", "L1", "L2", "L3", "L4"]
    assert result["trace_stage_count"] >= 1
    assert result["evaluation_verdict"] == "pass"
    assert result["attribution_method"] == "deterministic_event_evidence_v1"
    assert result["condition_selection"] == "then"
    assert result["loop_iterations"] == 2
    assert result["message_count"] == 2
    assert result["child_result_consumed"] is True
    assert result["live_subscription_observed"] is True
    assert result["safe_reopen_no_replay"] is True
    assert result["session_deleted"] is True
    assert result["workspace_preserved"] is True


def test_v01_reference_uses_automatic_context_compaction() -> None:
    source = Path("examples/v01_reference.py").read_text(encoding="utf-8")

    assert "force_level" not in source


def test_v01_reference_smoke_never_opens_a_network_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.new_event_loop()

    def forbid_network(*_: object, **__: object) -> object:
        raise AssertionError("smoke mode must not open a network socket")

    try:
        monkeypatch.setattr(socket.socket, "connect", forbid_network)
        monkeypatch.setattr(socket.socket, "connect_ex", forbid_network)
        result = loop.run_until_complete(
            run_reference(
                argparse.Namespace(
                    database=tmp_path / "sdk.sqlite3",
                    workspace=tmp_path / "workspace",
                    model="test/reference",
                    smoke=True,
                )
            )
        )
    finally:
        loop.close()

    assert result["run_status"] == "completed"
