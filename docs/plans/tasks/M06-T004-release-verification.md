# M06-T004 Documentation and Release Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the public documentation and enforce a reproducible release gate against an installed distribution.

**Architecture:** Documentation is executable where possible and generated API references import the public surface. A release script builds artifacts, installs the wheel into a clean environment, runs representative recovery/security/analytics scenarios, and emits a machine-readable report.

**Tech Stack:** MkDocs Material, mkdocstrings, uv, pytest, PowerShell/Python release scripts.

## Global Constraints

- Documentation promises only behavior covered by tests.
- All examples use public APIs and default to recoverable SQLite storage.
- Release verification runs against the installed wheel, not the source tree.
- Version, changelog, schema/protocol compatibility, and artifact hashes must agree.

---

### Task 1: Write guides and generated API reference

**Files:**
- Create: `README.md`
- Create: `mkdocs.yml`
- Create: `docs/getting-started.md`
- Create: `docs/guides/tools-permissions.md`
- Create: `docs/guides/mcp-skills.md`
- Create: `docs/guides/context-compaction.md`
- Create: `docs/guides/workflows.md`
- Create: `docs/guides/subagents.md`
- Create: `docs/guides/observability-analytics.md`
- Create: `docs/guides/storage-recovery.md`
- Create: `docs/api.md`
- Create: `tests/docs/test_snippets.py`

- [ ] **Step 1: Write failing documentation snippet test**

```python
@pytest.mark.parametrize("snippet", discover_python_snippets([Path("README.md"), Path("docs")]))
def test_documented_python_snippet_compiles(snippet) -> None:
    compile(snippet.code, str(snippet.source), "exec")

def test_documented_public_names_exist() -> None:
    assert_public_names_match_docs(Path("docs/api.md"), module=agent_sdk)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/docs/test_snippets.py -v`

Expected: release documentation is incomplete.

- [ ] **Step 3: Write task-oriented documentation**

Getting started creates a SQLite-backed SDK, registers an application tool, handles permission prompts, runs an Agent, watches events, closes/reopens a Session, and deletes it. Guides cover every configurable policy, persistence lifecycle, workflow generation approval, child control, trace levels, evaluators, tool analytics, and insight limitations.

```python
sdk = AgentSDK(storage=SQLiteStore("agent-sdk.db"), permission_bridge=MyPermissionBridge())
sdk.tools.register(ToolSpec(name="weather", description="Read weather", input_schema={"type": "object"}), weather)
result = await sdk.run(AgentSpec(name="assistant"), "Check the weather")
```

- [ ] **Step 4: Build documentation and verify**

Run: `uv run pytest tests/docs/test_snippets.py -v; uv run mkdocs build --strict`

Expected: snippets compile/run in their declared modes, public names resolve, links are valid, and strict docs build passes.

```powershell
uv run pytest tests/docs/test_snippets.py -v
uv run mkdocs build --strict
```

- [ ] **Step 5: Commit**

```powershell
git add README.md mkdocs.yml docs/getting-started.md docs/guides docs/api.md tests/docs/test_snippets.py
git commit -m "docs: complete agent sdk user guides"
```

---

### Task 2: Implement installed-wheel release gate

**Files:**
- Create: `scripts/release_check.py`
- Create: `tests/release/test_installed_wheel.py`
- Create: `docs/release-checklist.md`
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: Write failing release report test**

```python
def test_release_report_requires_all_gates() -> None:
    report = ReleaseReport(gates=[GateResult(name="wheel", passed=True), GateResult(name="recovery", passed=False)])
    assert not report.passed
    assert report.failed_gates == ("recovery",)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/release/test_installed_wheel.py -v`

Expected: release gate/report are missing.

- [ ] **Step 3: Implement clean environment verification**

```python
REQUIRED_GATES = (
    "metadata", "wheel_contents", "public_imports", "sqlite_recovery", "session_deletion",
    "permission_denial", "mcp_stdio", "skill_activation", "compaction_provenance",
    "workflow_restart", "subagent_limits", "subscription_resume", "analytics_deletion",
)

def main() -> int:
    report = run_installed_wheel_gates(REQUIRED_GATES)
    Path(".artifacts/release-report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return 0 if report.passed else 1
```

Build sdist/wheel, hash artifacts, create a clean environment outside the repository, install the wheel plus test fixtures, prevent source-tree imports, and run every gate with deterministic fake services.

- [ ] **Step 4: Verify and commit**

Run: `uv build; $wheel = (Get-ChildItem dist\*.whl | Select-Object -First 1).FullName; uv run python scripts/release_check.py --wheel $wheel`

Expected: exit code 0 and `.artifacts/release-report.json` lists every required gate passed with artifact/version hashes.

```powershell
git add scripts/release_check.py tests/release/test_installed_wheel.py docs/release-checklist.md .github/workflows/release.yml
git commit -m "build: add installed wheel release gate"
```

---

### Task 3: Cut the release candidate

**Files:**
- Create: `CHANGELOG.md`
- Create: `docs/release-report.md`
- Modify: `pyproject.toml`

- [ ] **Step 1: Confirm repository state and full test suites**

Run: `git status --short; uv run ruff check .; uv run mypy src; uv run pytest -q`

Expected: clean worktree before release edits; lint, types, and all tests exit 0.

- [ ] **Step 2: Run compatibility, fault, performance, docs, and release gates**

Run: `uv run python scripts/run_fault_matrix.py --json .artifacts/faults.json --junit .artifacts/faults.xml; uv run pytest benchmarks --benchmark-json=.artifacts/benchmarks.json; uv run mkdocs build --strict; uv build; uv run python scripts/release_check.py`

Expected: every command exits 0; reports contain no failed case or exceeded budget.

- [ ] **Step 3: Record version and evidence**

Set the release-candidate version once, add changelog entries grouped by runtime, capabilities, durability, workflows, observability, analytics, security, and compatibility, and write report links/hashes without inventing a license or publishing destination.

```powershell
uv version --bump rc
uv lock
Get-FileHash -Algorithm SHA256 dist\* | Format-Table Path, Hash
```

- [ ] **Step 4: Rebuild after version edit and commit**

Run: `uv lock --check; uv build; uv run python scripts/release_check.py`

Expected: artifact metadata matches `pyproject.toml`, the lockfile is current, and all installed-wheel gates pass.

```powershell
git add pyproject.toml uv.lock CHANGELOG.md docs/release-report.md
git commit -m "chore: prepare agent sdk release candidate"
```
