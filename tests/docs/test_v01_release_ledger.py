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
R2_TASK_COMMITS = (
    "e3494ae",
    "1fc9c72",
    "9b23e5a",
    "cfdf43a",
    "e4624f7",
    "36a7268",
    "04d8ee2",
)
R2_HISTORICAL_CHECKPOINT = r"""$ .\.venv\Scripts\python.exe -m pytest tests\unit\workflow tests\integration\workflow tests\e2e\test_v01_release.py -q
........................................................................ [ 18%]
........................................................................ [ 37%]
........................................................................ [ 56%]
........................................................................ [ 75%]
........................................................................ [ 94%]
....................                                                     [100%]
380 passed in 44.02s

$ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk\workflow src\agent_sdk\runtime\execution.py tests\unit\workflow tests\integration\workflow
All checks passed!

$ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk\workflow src\agent_sdk\runtime\execution.py
Success: no issues found in 10 source files"""
R2_FINAL_COMMITS = ("852692f", "309d63c")
R2_FINAL_CHECKPOINT = r"""$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\workflow tests\integration\workflow tests\e2e\test_v01_release.py -q
403 passed in 43.03s

$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\integration\workflow\test_control_child_parent.py tests\integration\workflow\test_control_recovery.py tests\integration\workflow\test_control_state.py tests\unit\workflow\test_control_compiler.py -q
47 passed in 7.31s

$ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk\workflow tests\unit\workflow tests\integration\workflow tests\e2e\test_v01_release.py
All checks passed!

$ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk\workflow src\agent_sdk\runtime\execution.py
Success: no issues found in 10 source files

$ git diff --check 56d60a8..309d63c
clean"""
R3_FIRST_TEST = "tests/unit/context/test_deterministic_strategies.py"
R3_TASK1_COMMITS = ("2bda910", "ba9d05d", "ead396b")
R3_TASK2_COMMITS = ("c3dc154", "3d8458e")
R3_TASK3_COMMITS = ("9fbcd16", "2bd48e3")
R3_TASK4_COMMITS = ("2ea0464", "3a4b65f", "b98e93f")
R4_TASK1_TEST = "tests/unit/runtime/test_capability_intersection.py"
R4_TASK2_MAILBOX_TEST = "tests/unit/subagents/test_mailbox.py"
R3_TASK5_FRESH_RESULT = "221 passed, 1 skipped in 25.32s"
R5_PLAN = "docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r5-trace-release.md"
R5_TASK1_TEST = "tests/unit/observability/test_stage_projection.py"
R5_FIRST_COMMAND = (
    "$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; "
    r".\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin "
    r"tests\unit\observability\test_stage_projection.py -q"
)


def _assert_release_checkpoint_and_resume(document: str) -> None:
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
    for commit in R2_TASK_COMMITS:
        assert commit in document
    assert "R2 implementation checkpoint: `56d60a8`" in document
    for commit in R2_FINAL_COMMITS:
        assert commit in document
    historical_r2_marker = "Historical R2 pre-final-hardening checkpoint evidence:"
    canonical_r2_marker = "Current canonical R2 final checkpoint evidence:"
    assert document.count(historical_r2_marker) == 1
    assert document.count(canonical_r2_marker) == 1
    historical_r2_index = normalized_document.index(historical_r2_marker)
    canonical_r2_index = normalized_document.index(canonical_r2_marker)
    assert historical_r2_index < canonical_r2_index
    assert R2_HISTORICAL_CHECKPOINT in normalized_document[
        historical_r2_index:canonical_r2_index
    ]
    assert "380 passed in 44.02s" not in normalized_document[canonical_r2_index:]
    assert R2_FINAL_CHECKPOINT in normalized_document
    assert "Critical 0 / Important 0 / Minor 0" in document
    assert "Spec compliance PASS" in document
    assert "Code quality PASS" in document
    assert "Ready to proceed to R2: Yes" in document
    assert "Ready to proceed to R3: Yes" in document
    assert "R2 Task 4" in document
    assert "final review Spec approved / Quality approved" in document
    for commit in R3_TASK1_COMMITS:
        assert commit in document
    assert R3_FIRST_TEST in document
    assert "R3 Task 1 deterministic L0-L2 is complete" in document
    assert "R3 Task 1 final review: Critical 0 / Important 0 / Minor 0" in document
    assert "Spec PASS; Quality PASS" in document
    assert "42 deterministic strategy tests" in document
    assert "48 context integration tests" in normalized_document
    for commit in R3_TASK2_COMMITS:
        assert commit in document
    assert "102 passed" in document
    assert "R3 Task 2 is complete" in document or "v0.1 R3 Task 2: complete" in document
    assert "Critical 0 / Important 0 / Minor 0" in document
    for commit in R3_TASK3_COMMITS:
        assert commit in document
    assert "R3 Task 3 is complete" in document or "v0.1 R3 Task 3: complete" in document
    assert "AgentSpec" in document
    assert "DurableAgentSpec" in document
    assert "SkillRegistry" in document
    assert "run.created" in document
    assert "schema v2" in document or "schema-v2" in document
    assert "schema-v1" in document
    assert "Critical 0 / Important 0 / Minor 0" in document
    assert "201 passed" in document
    assert "521 passed, 1 skipped" in document
    assert "25 passed" in document
    assert "92 source files" in document
    for commit in R3_TASK4_COMMITS:
        assert commit in document
    assert "R3 Task 4 is complete" in document or "v0.1 R3 Task 4: complete" in document
    assert "Task 4 final approval: Critical 0 / Important 0 / Minor 0" in document
    assert "Spec PASS; Quality PASS" in document
    assert R3_TASK5_FRESH_RESULT in document
    assert "13.65s" not in document
    assert R5_PLAN in document
    assert R5_TASK1_TEST in normalized_document.replace("\\", "/")
    assert R5_FIRST_COMMAND in normalized_document
    assert "first expected RED" in document
    assert "R3 Task 2 Step 1" not in document
    assert "tests/unit/context/test_compaction_levels.py" not in document
    assert "R3 Task 2 remains pending/unstarted" not in document
    assert "R3 remains pending" not in document
    assert "R3 is in progress" not in document
    assert "R3 implementation has not started" not in document
    assert "Tasks 4-5 have not started" not in document
    assert "R3 Task 4 Step 1" not in document
    assert "tests/integration/context/test_runtime_middleware.py" not in document


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
    assert (
        "| R2 | completed | condition and bounded loop | "
        "2026-07-20 final checkpoint: 403 passed in 43.03s; Ruff/mypy clean |"
    ) in ledger
    assert "| R4 | completed with known pre-R4 recovery debt |" in ledger
    assert "| R5 | completed |" in ledger
    assert "| R3 | completed | automatic L0-L4 | " in ledger
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
    assert "v0.1 R5 Task 5: complete" in progress
    assert "v0.1 R5 Task 6: complete" in progress
    assert "v0.1 final checkpoint: complete" in progress
    assert "1476554" in ledger
    assert "1476554" in progress
    assert "C0 / I0 / M1 APPROVE" in ledger
    assert "Minor resolved by Task 6 status cleanup" in ledger
    assert "2,953 passed, 6 skipped" in ledger
    assert "80 passed" in ledger
    assert "C0 / I0 / M0 APPROVE" in ledger
    assert "Final 0.1.0 wheel SHA256:" in ledger
    assert "2,953 passed, 6 skipped" in progress
    assert "Final 0.1.0 wheel SHA256:" in progress
    assert "R5 Task 5 pending" not in progress
    old_resume = r"tests\e2e\test_v01_release.py tests\e2e\test_v01_reference_example.py tests\docs -q"
    assert old_resume not in progress
    assert "Aggregate Trace analysis" in ledger
    assert "failure rate and useless-result identification" in ledger
    for retained_debt in ("TOCTOU", "handler cancellation", "Context scans"):
        assert retained_debt in ledger
    assert R5_TASK1_TEST in ledger.replace("\\", "/")
    assert R5_TASK1_TEST in progress.replace("\\", "/")
    _assert_release_checkpoint_and_resume(ledger)
    _assert_release_checkpoint_and_resume(progress)


def test_active_roadmap_links_the_v01_plan_index() -> None:
    root = Path(__file__).parents[2]
    expected = "2026-07-17-agent-sdk-v0.1-implementation-index.md"
    assert expected in (root / "docs/plans/00-roadmap.md").read_text(encoding="utf-8")
    assert expected in (root / "docs/plans/tasks/index.md").read_text(encoding="utf-8")


def test_r3_plan_hands_r4_to_capability_intersection_before_mailbox() -> None:
    root = Path(__file__).parents[2]
    plan = (
        root
        / "docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md"
    ).read_text(encoding="utf-8")

    assert R4_TASK1_TEST in plan
    assert "first expected RED" in plan
    assert "R4 Task 1" in plan
    assert "R4 Task 2" in plan
    assert R4_TASK2_MAILBOX_TEST in plan
    assert plan.index(R4_TASK1_TEST) < plan.index(R4_TASK2_MAILBOX_TEST)
    assert "uv run pytest tests/unit/subagents/test_mailbox.py -q" not in plan


def test_v01_user_guides_state_public_flow_and_safety_boundaries() -> None:
    root = Path(__file__).parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    quickstart = (root / "docs/guides/v01-quickstart.md").read_text(
        encoding="utf-8"
    )
    recovery = (root / "docs/guides/v01-recovery.md").read_text(encoding="utf-8")
    analysis = (root / "docs/guides/v01-tracing-and-analysis.md").read_text(
        encoding="utf-8"
    )

    for document in (readme, quickstart):
        assert "AgentSDKConfig" in document
        assert "system_prompt" in document
        assert "sdk.tools.register" in document
        assert "PermissionDecision.allow_once" in document
        assert "PermissionDecision.deny" in document
        assert "MCPServerConfig" in document
        assert "Skill" in document
        assert "sdk.workflows.compile" in document
        assert "sdk.workflows.start" in document
        assert "spawn_agent" in document
        assert "sdk.trace.subscribe" in document
        assert "sdk.trace.attribution" in document
        assert "sdk.recovery.resolve" in document

    assert "0.1.0" in changelog
    assert "no exactly-once guarantee for external effects" in recovery
    assert "one SDK instance in one process" in recovery
    for document in (readme, quickstart, recovery):
        assert "ReconciliationAction.TERMINATE" in document
        assert 'evidence={"reason":' in document
        assert "application_resolution_aborted" in document
        assert "reserved but not supported" not in document
        assert "reserved but not implemented" not in document
    assert "retry or abort" in changelog
    assert "deterministic correlation" in analysis
    assert "not causality" in analysis
    assert "aggregate Tool usefulness" in analysis
    assert "multidimensional failure analysis" in analysis
