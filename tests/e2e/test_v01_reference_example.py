from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_v01_reference_smoke(tmp_path: Path) -> None:
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
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("\n") == 1
    result = json.loads(completed.stdout)
    assert result == {
        "run_status": "completed",
        "workflow_status": "completed",
        "child_status": "completed",
        "context_levels": ["L0", "L1", "L2", "L3", "L4"],
        "trace_stage_count": 1,
        "evaluation_verdict": "pass",
        "attribution_method": "deterministic_event_evidence_v1",
    }
