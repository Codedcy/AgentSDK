from pathlib import Path


R1_COMMITS = (
    "8fc36ad",
    "8c2982b",
    "e6d9f3b",
    "2b145a7",
    "e8ce3db",
    "8fb3836",
    "cd82a6f",
)
R1_INITIAL_CHECKPOINT = r"""$ .\.venv\Scripts\python.exe -m pytest tests/unit/permissions/test_policy_rules.py tests/unit/tools/test_workspace_paths.py tests/integration/tools/test_builtin_tools.py tests/integration/tools/test_permissioned_tool_slice.py tests/e2e/test_v01_release.py -q
..............s......................................................... [ 83%]
..............                                                           [100%]
85 passed, 1 skipped in 6.12s

$ .\.venv\Scripts\python.exe -m ruff check src/agent_sdk/config.py src/agent_sdk/permissions src/agent_sdk/tools tests/unit/permissions tests/unit/tools tests/integration/tools
All checks passed!

$ .\.venv\Scripts\python.exe -m mypy --strict src/agent_sdk/config.py src/agent_sdk/permissions src/agent_sdk/tools
Success: no issues found in 16 source files"""
R1_FINAL_CHECKPOINT = r"""$ .\.venv\Scripts\python.exe -m pytest tests\unit\permissions\test_policy_rules.py tests\unit\tools\test_workspace_paths.py tests\unit\runtime\test_session_workspace_roots.py tests\integration\tools\test_builtin_tools.py tests\integration\tools\test_permissioned_tool_slice.py tests\integration\runtime\test_builtin_tool_recovery.py tests\e2e\test_v01_release.py -q
..............s............ss........................................... [ 72%]
............................                                             [100%]
97 passed, 3 skipped in 7.94s

$ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk tests\unit\permissions tests\unit\tools tests\unit\runtime\test_session_workspace_roots.py tests\integration\tools tests\integration\runtime\test_builtin_tool_recovery.py tests\e2e\test_v01_release.py
All checks passed!

$ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk
Success: no issues found in 84 source files"""
R1_FINAL_COMMITS = ("d4cd336", "2f0e922")
R2_TASK_1_COMMITS = ("e3494ae", "1fc9c72")
R2_PLAN = "docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r2-workflow-control.md"
R2_RESUME_COMMAND = (
    r"Get-Content docs\superpowers\plans"
    r"\2026-07-17-agent-sdk-v0.1-r2-workflow-control.md"
)
R2_FIRST_TEST = "tests/unit/workflow/test_control_compiler.py"
R2_FIRST_RED = (
    rf".\.venv\Scripts\python.exe -m pytest {R2_FIRST_TEST} "
    "tests/unit/workflow/test_workflow_compiler.py -q"
)


def _assert_r1_checkpoint_and_r2_resume(document: str) -> None:
    for commit in R1_COMMITS:
        assert commit in document
    normalized_document = "\n".join(
        line[2:] if line.startswith("  ") else line
        for line in document.splitlines()
    )
    assert R1_INITIAL_CHECKPOINT in normalized_document
    assert R1_FINAL_CHECKPOINT in normalized_document
    for commit in R1_FINAL_COMMITS:
        assert commit in document
    for commit in R2_TASK_1_COMMITS:
        assert commit in document
    assert "Critical 0 / Important 0 / Minor 0" in document
    assert "Ready to proceed to R2: Yes" in document
    assert R2_PLAN in document
    assert R2_RESUME_COMMAND in document
    assert "R2 Task 2 Step 1" in document
    assert R2_FIRST_TEST in document
    assert R2_FIRST_RED in document
    assert "R2 remains in progress" in document
    assert "Tasks 2-5 have not started" in document


def test_v01_release_ledger_names_every_required_slice() -> None:
    root = Path(__file__).parents[2]
    ledger = (root / "docs/plans/releases/v0.1.md").read_text(encoding="utf-8")
    progress = (root / ".superpowers/sdd/progress.md").read_text(encoding="utf-8")
    for slice_id in ("R0", "R1", "R2", "R3", "R4", "R5"):
        assert f"| {slice_id} |" in ledger
    assert "0.1.0" in ledger
    assert "post-v0.1" in ledger
    assert "| R0 | completed |" in ledger
    assert "| R1 | completed |" in ledger
    assert (
        "| R1 | completed | built-in Tool authorization | "
        "2026-07-17 final checkpoint: 97 passed, 3 skipped in 7.94s; "
        "Ruff/mypy clean |"
    ) in ledger
    assert "R1 is complete through final hardening commit `2f0e922`" in ledger
    assert "final review approved" in ledger
    assert "| R2 | in_progress |" in ledger
    for slice_id in ("R3", "R4", "R5"):
        assert f"| {slice_id} | pending |" in ledger
    assert "4 passed in 4.74s" in ledger
    assert "5.05s" not in ledger
    assert "74c1e3b" in ledger
    assert "R1 Tasks 1-3 are complete" in ledger
    historical_marker = "Historical initial checkpoint evidence:"
    canonical_marker = "Current canonical checkpoint evidence:"
    assert ledger.count(historical_marker) == 1
    assert ledger.count(canonical_marker) == 1
    historical_index = ledger.index(historical_marker)
    canonical_index = ledger.index(canonical_marker)
    assert historical_index < canonical_index
    assert "85 passed, 1 skipped in 6.12s" in ledger[
        historical_index:canonical_index
    ]
    assert "85 passed, 1 skipped in 6.12s" not in ledger[canonical_index:]
    assert "97 passed, 3 skipped in 7.94s" in ledger[canonical_index:]
    assert "v0.1 R1 checkpoint: complete" in progress
    assert "v0.1 R1 initial checkpoint historical evidence:" in progress
    assert "v0.1 R1 final checkpoint exact fresh evidence:" in progress
    assert (
        "v0.1 current implementation status: R0-R1 completed; "
        "R2 in progress; restricted expressions complete"
    ) in progress
    _assert_r1_checkpoint_and_r2_resume(ledger)
    _assert_r1_checkpoint_and_r2_resume(progress)


def test_active_roadmap_links_the_v01_plan_index() -> None:
    root = Path(__file__).parents[2]
    expected = "2026-07-17-agent-sdk-v0.1-implementation-index.md"
    assert expected in (root / "docs/plans/00-roadmap.md").read_text(encoding="utf-8")
    assert expected in (root / "docs/plans/tasks/index.md").read_text(encoding="utf-8")
