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
R1_CHECKPOINT = r"""$ .\.venv\Scripts\python.exe -m pytest tests/unit/permissions/test_policy_rules.py tests/unit/tools/test_workspace_paths.py tests/integration/tools/test_builtin_tools.py tests/integration/tools/test_permissioned_tool_slice.py tests/e2e/test_v01_release.py -q
..............s......................................................... [ 83%]
..............                                                           [100%]
85 passed, 1 skipped in 6.12s

$ .\.venv\Scripts\python.exe -m ruff check src/agent_sdk/config.py src/agent_sdk/permissions src/agent_sdk/tools tests/unit/permissions tests/unit/tools tests/integration/tools
All checks passed!

$ .\.venv\Scripts\python.exe -m mypy --strict src/agent_sdk/config.py src/agent_sdk/permissions src/agent_sdk/tools
Success: no issues found in 16 source files"""
R2_PLAN = "docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r2-workflow-control.md"
R2_RESUME_COMMAND = (
    r"Get-Content docs\superpowers\plans"
    r"\2026-07-17-agent-sdk-v0.1-r2-workflow-control.md"
)
R2_FIRST_TEST = "tests/unit/workflow/test_expressions.py"
R2_FIRST_RED = (
    rf".\.venv\Scripts\python.exe -m pytest {R2_FIRST_TEST} -q"
)


def _assert_r1_checkpoint_and_r2_resume(document: str) -> None:
    for commit in R1_COMMITS:
        assert commit in document
    normalized_document = "\n".join(
        line[2:] if line.startswith("  ") else line
        for line in document.splitlines()
    )
    assert R1_CHECKPOINT in normalized_document
    assert R2_PLAN in document
    assert R2_RESUME_COMMAND in document
    assert "R2 Task 1 Step 1" in document
    assert R2_FIRST_TEST in document
    assert R2_FIRST_RED in document
    assert "R2 remains pending" in document
    assert "has not started" in document


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
    for slice_id in ("R2", "R3", "R4", "R5"):
        assert f"| {slice_id} | pending |" in ledger
    assert "4 passed in 4.74s" in ledger
    assert "5.05s" not in ledger
    assert "74c1e3b" in ledger
    assert "R1 Tasks 1-3 are complete" in ledger
    assert "v0.1 R1 checkpoint: complete" in progress
    assert "v0.1 current implementation status: R0-R1 completed; R2 pending" in progress
    _assert_r1_checkpoint_and_r2_resume(ledger)
    _assert_r1_checkpoint_and_r2_resume(progress)


def test_active_roadmap_links_the_v01_plan_index() -> None:
    root = Path(__file__).parents[2]
    expected = "2026-07-17-agent-sdk-v0.1-implementation-index.md"
    assert expected in (root / "docs/plans/00-roadmap.md").read_text(encoding="utf-8")
    assert expected in (root / "docs/plans/tasks/index.md").read_text(encoding="utf-8")
