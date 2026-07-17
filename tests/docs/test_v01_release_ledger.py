from pathlib import Path


def test_v01_release_ledger_names_every_required_slice() -> None:
    root = Path(__file__).parents[2]
    ledger = (root / "docs/plans/releases/v0.1.md").read_text(encoding="utf-8")
    for slice_id in ("R0", "R1", "R2", "R3", "R4", "R5"):
        assert f"| {slice_id} |" in ledger
    assert "0.1.0" in ledger
    assert "post-v0.1" in ledger
    assert "| R0 | completed |" in ledger
    for slice_id in ("R1", "R2", "R3", "R4", "R5"):
        assert f"| {slice_id} | pending |" in ledger
    assert "4 passed in 4.74s" in ledger
    assert "5.05s" not in ledger
    assert "74c1e3b" in ledger
    assert (
        r"Get-Content docs\superpowers\plans"
        r"\2026-07-17-agent-sdk-v0.1-r1-tools-policy.md"
    ) in ledger
    assert (
        r".\.venv\Scripts\python.exe -m pytest "
        "tests/unit/permissions/test_policy_rules.py "
        "tests/unit/test_core_config.py -q"
    ) in ledger
    assert "R1 has not started." in ledger


def test_active_roadmap_links_the_v01_plan_index() -> None:
    root = Path(__file__).parents[2]
    expected = "2026-07-17-agent-sdk-v0.1-implementation-index.md"
    assert expected in (root / "docs/plans/00-roadmap.md").read_text(encoding="utf-8")
    assert expected in (root / "docs/plans/tasks/index.md").read_text(encoding="utf-8")
