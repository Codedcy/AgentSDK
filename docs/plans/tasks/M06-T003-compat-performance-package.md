# M06-T003 Compatibility, Performance, and Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate the supported compatibility matrix, protect performance budgets, and produce complete installable distributions.

**Architecture:** CI separates fast unit/contract checks, OS/provider protocol integration, migrations, and benchmark gates. Packaging declares narrow core dependencies and optional extras while including prompt/workflow assets and typing metadata.

**Tech Stack:** uv, hatchling, GitHub Actions, pytest-benchmark, SQLite, official MCP SDK, LiteLLM.

## Global Constraints

- Supported Python versions are 3.12, 3.13, and 3.14.
- SQLite is the default and memory storage remains explicitly selectable.
- Compatibility fixtures pin recorded schema/protocol generations; latest dependency CI is non-blocking until qualified.
- Performance gates use stable synthetic workloads and report environment metadata.

---

### Task 1: Add compatibility and migration matrices

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/compatibility.yml`
- Create: `tests/compat/test_python_os_matrix.py`
- Create: `tests/compat/test_litellm_shapes.py`
- Create: `tests/compat/test_mcp_protocol.py`
- Create: `tests/fixtures/databases/`
- Create: `tests/integration/storage/test_migration_fixtures.py`

- [ ] **Step 1: Write failing migration fixture test**

```python
@pytest.mark.parametrize("fixture", discover_database_fixtures())
def test_database_fixture_migrates_and_rebuilds(fixture, tmp_path) -> None:
    database = copy_fixture(fixture, tmp_path)
    sdk = AgentSDK(storage=SQLiteStore(database))
    sdk.migrate_sync()
    assert sdk.storage.schema_version_sync() == CURRENT_SCHEMA_VERSION
    assert sdk.observability.rebuild_and_compare_sync().matches
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/compat tests/integration/storage/test_migration_fixtures.py -v`

Expected: compatibility fixtures/matrix are missing.

- [ ] **Step 3: Implement CI matrix**

```yaml
strategy:
  fail-fast: false
  matrix:
    os: [ubuntu-latest, windows-latest, macos-latest]
    python: ["3.12", "3.13", "3.14"]
steps:
  - uses: actions/checkout@v4
  - uses: astral-sh/setup-uv@v6
    with: {python-version: "${{ matrix.python }}"}
  - run: uv sync --all-extras --dev
  - run: uv run pytest tests/unit tests/contract tests/integration tests/compat -q
```

Test LiteLLM non-stream/stream/tool/error/usage normalized shapes, MCP 2025-11-25 stdio/Streamable HTTP fixtures, Windows path/process cases, WAL/foreign keys, and every historical schema fixture.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/compat tests/integration/storage/test_migration_fixtures.py -v`

Expected: local platform suite passes; CI configuration expands to nine OS/Python jobs plus protocol jobs.

```powershell
git add .github/workflows tests/compat tests/fixtures/databases tests/integration/storage/test_migration_fixtures.py
git commit -m "ci: add compatibility and migration matrix"
```

---

### Task 2: Establish performance and backpressure budgets

**Files:**
- Create: `benchmarks/test_event_append.py`
- Create: `benchmarks/test_projection_lag.py`
- Create: `benchmarks/test_context_planning.py`
- Create: `benchmarks/test_scheduler.py`
- Create: `benchmarks/budgets.json`
- Create: `scripts/check_benchmarks.py`
- Create: `scripts/tests/test_check_benchmarks.py`

- [ ] **Step 1: Write failing budget checker test**

```python
def test_budget_checker_fails_regression(tmp_path) -> None:
    result = check_benchmarks(measured={"event_append_p95_ms": 50}, budgets={"event_append_p95_ms": 10})
    assert not result.passed
    assert result.regressions[0].metric == "event_append_p95_ms"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest benchmarks scripts/tests/test_check_benchmarks.py -v`

Expected: benchmark suite/checker are missing.

- [ ] **Step 3: Implement benchmark workloads and thresholds**

```json
{
  "event_append_p95_ms": 10,
  "snapshot_query_p95_ms": 50,
  "context_plan_1000_items_p95_ms": 100,
  "projection_lag_p95_ms": 1000,
  "scheduler_overhead_per_node_ms": 5
}
```

Run warmups and multiple samples, record OS/Python/SQLite/CPU metadata, and check throughput plus bounded memory/backlog for event append, projection, subscription, exporter, context selection, workflow, and child queues.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest benchmarks --benchmark-json=.artifacts/benchmarks.json; uv run python scripts/check_benchmarks.py .artifacts/benchmarks.json benchmarks/budgets.json`

Expected: benchmark command and budget checker exit 0 on the reference development environment.

```powershell
git add benchmarks scripts/check_benchmarks.py scripts/tests/test_check_benchmarks.py
git commit -m "perf: establish sdk performance budgets"
```

---

### Task 3: Finalize package metadata and clean-install tests

**Files:**
- Modify: `pyproject.toml`
- Create: `src/agent_sdk/py.typed`
- Create: `tests/package/test_wheel_contents.py`
- Create: `scripts/smoke_install.ps1`

- [ ] **Step 1: Write failing wheel content test**

```python
def test_wheel_contains_profiles_templates_and_typing(built_wheel) -> None:
    names = built_wheel.names()
    assert "agent_sdk/py.typed" in names
    assert "agent_sdk/prompts/profiles/general/system.md" in names
    assert "agent_sdk/workflow/templates/coding.yaml" in names
```

- [ ] **Step 2: Verify failure**

Run: `uv build; uv run pytest tests/package/test_wheel_contents.py -v`

Expected: final metadata/package assets are incomplete.

- [ ] **Step 3: Declare dependencies, extras, entry points, and package data**

```toml
[project]
requires-python = ">=3.12"
dependencies = [
  "aiosqlite>=0.20,<1", "jinja2>=3.1,<4", "litellm>=1,<2",
  "mcp>=1,<2", "pydantic>=2,<3", "PyYAML>=6,<7",
]

[project.optional-dependencies]
cli = ["rich", "typer"]
monitor = ["fastapi", "uvicorn"]
otel = ["opentelemetry-api", "opentelemetry-sdk"]
testing = ["hypothesis", "pytest", "pytest-asyncio"]
```

Keep compatible upper bounds/constraints in the lockfile policy, include Markdown/YAML assets and `py.typed`, validate metadata, and smoke-test base plus each extra in clean virtual environments.

- [ ] **Step 4: Verify and commit**

Run: `uv build; powershell -ExecutionPolicy Bypass -File scripts/smoke_install.ps1`

Expected: sdist/wheel build, `twine check`, clean import, SQLite run, memory run, CLI, monitor, OTEL, and testing imports all pass.

```powershell
git add pyproject.toml src/agent_sdk/py.typed tests/package/test_wheel_contents.py scripts/smoke_install.ps1
git commit -m "build: finalize agent sdk packaging"
```
