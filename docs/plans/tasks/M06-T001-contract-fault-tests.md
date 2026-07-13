# M06-T001 Contract, Property, and Fault Testing Kit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship reusable tests and deterministic fixtures so SDK extensions can prove protocol compatibility and recovery invariants.

**Architecture:** `agent_sdk.testing` supplies fake model/MCP/permission/time services, scenario builders, contract suites, and fault points. The repository runs the same contracts against built-ins and reference third-party implementations.

**Tech Stack:** pytest, pytest-asyncio, Hypothesis, LiteLLM mock responses, JUnit XML/JSON reports.

## Global Constraints

- Tests use public extension protocols and deterministic clocks/ids/randomness.
- Fault injection covers boundaries before/after side effects and before/after durable commits.
- Contract suites report actionable capability names and protocol versions.
- Test helpers are included in the `testing` package extra without production-only dependencies.

---

### Task 1: Build deterministic scenario and fake services

**Files:**
- Create: `src/agent_sdk/testing/__init__.py`
- Create: `src/agent_sdk/testing/scenario.py`
- Create: `src/agent_sdk/testing/fakes.py`
- Create: `src/agent_sdk/testing/faults.py`
- Create: `tests/unit/testing/test_scenario_runner.py`

- [ ] **Step 1: Write failing deterministic replay test**

```python
@pytest.mark.asyncio
async def test_same_scenario_produces_same_semantic_events() -> None:
    scenario = Scenario().user("hello").model_tool_call("echo", {"text": "hi"}).tool_result("hi").model_text("done")
    first = await ScenarioRunner(seed=7).run(scenario)
    second = await ScenarioRunner(seed=7).run(scenario)
    assert semantic_events(first.events) == semantic_events(second.events)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/testing/test_scenario_runner.py -v`

Expected: testing package and deterministic runner are missing.

- [ ] **Step 3: Implement fluent scenario and fakes**

```python
class Scenario:
    def __init__(self) -> None:
        self.steps: list[ScenarioStep] = []

    def model_text(self, text: str, *, usage: Usage | None = None) -> "Scenario":
        self.steps.append(ModelTextStep(text=text, usage=usage or Usage.zero()))
        return self

class ScenarioRunner:
    async def run(self, scenario: Scenario) -> ScenarioResult:
        services = FakeServices(clock=FakeClock(), ids=SequentialIds(), model=ScriptedLiteLLM(scenario.steps))
        return await run_scenario_with_public_sdk(services, scenario)
```

Provide fake MCP stdio/HTTP servers, permission bridge, exporter, evaluator, clock, id generator, artifact store, and fault controller.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/testing/test_scenario_runner.py -v`

Expected: deterministic success/failure/tool/approval/child/workflow scenarios and mismatch diagnostics pass.

```powershell
git add src/agent_sdk/testing tests/unit/testing/test_scenario_runner.py
git commit -m "test: add deterministic sdk scenario kit"
```

---

### Task 2: Publish extension contract suites

**Files:**
- Create: `src/agent_sdk/testing/contracts.py`
- Create: `tests/contract/test_tool_contract.py`
- Create: `tests/contract/test_store_contract.py`
- Create: `tests/contract/test_exporter_contract.py`
- Create: `tests/contract/test_evaluator_contract.py`
- Create: `tests/fixtures/extensions/`

- [ ] **Step 1: Write failing contract self-tests**

```python
@pytest.mark.asyncio
async def test_bad_store_is_rejected_by_contract_suite() -> None:
    report = await StoreContractSuite(BrokenNonAtomicStore()).run()
    assert report.failed_capabilities == {"atomic_event_and_projection_commit"}

@pytest.mark.asyncio
async def test_builtin_extensions_satisfy_contracts(extension_fixtures) -> None:
    assert (await ToolContractSuite(extension_fixtures.tool).run()).passed
    assert (await ExporterContractSuite(extension_fixtures.exporter).run()).passed
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/contract -v`

Expected: public contract suites are missing.

- [ ] **Step 3: Implement executable suites**

```python
class ContractReport(BaseModel, frozen=True):
    protocol: str
    protocol_version: str
    passed_capabilities: frozenset[str]
    failed_capabilities: frozenset[str]
    diagnostics: tuple[ContractDiagnostic, ...]

class StoreContractSuite:
    async def run(self) -> ContractReport:
        checks = [self._atomic_append, self._optimistic_sequence, self._delete_session, self._migration_lock, self._cursor_monotonicity]
        return await execute_contract_checks("Store", STORE_PROTOCOL_VERSION, checks)
```

Tool contracts cover schema/timeout/cancel/output; Store covers atomicity/idempotency/deletion/cursors; Exporter covers retry/duplicates/redaction; Evaluator covers provenance/status/error containment.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/contract -v`

Expected: good reference implementations pass and intentionally broken fixtures fail only their expected capability.

```powershell
git add src/agent_sdk/testing/contracts.py tests/contract tests/fixtures/extensions
git commit -m "test: publish extension contract suites"
```

---

### Task 3: Add property, concurrency, and crash matrices

**Files:**
- Create: `tests/property/test_runtime_invariants.py`
- Create: `tests/faults/test_commit_boundaries.py`
- Create: `tests/faults/test_transport_failures.py`
- Create: `tests/faults/test_storage_failures.py`
- Create: `scripts/run_fault_matrix.py`
- Create: `tests/unit/testing/test_fault_report.py`

- [ ] **Step 1: Write failing report test**

```python
def test_fault_matrix_report_lists_boundary_and_invariant(tmp_path) -> None:
    report = run_fault_matrix(output=tmp_path / "faults.json", cases=["after_tool_effect_before_commit"])
    assert report.cases[0].fault_point == "after_tool_effect_before_commit"
    assert "no_silent_duplicate_effect" in report.cases[0].checked_invariants
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/property/test_runtime_invariants.py tests/faults tests/unit/testing/test_fault_report.py -v`

Expected: matrix runner and full invariants are missing.

- [ ] **Step 3: Implement named fault boundaries and machine-readable results**

```python
FAULT_POINTS = (
    "before_effect", "after_effect_before_commit", "after_commit_before_signal",
    "during_model_stream", "during_mcp_call", "during_projection_batch", "during_export",
)

class FaultCaseResult(BaseModel):
    fault_point: str
    checked_invariants: tuple[str, ...]
    status: Literal["passed", "failed"]
    diagnostics: tuple[str, ...] = ()
```

Generate races for event sequences, lease ownership, cancel/complete ordering, permission precedence, graph scheduling, and compaction protected items. Write JSON plus JUnit XML for CI.

- [ ] **Step 4: Verify and commit**

Run: `uv run python scripts/run_fault_matrix.py --json .artifacts/faults.json --junit .artifacts/faults.xml`

Expected: exit code 0 and every named boundary reports all required invariants passed.

```powershell
git add tests/property tests/faults scripts/run_fault_matrix.py tests/unit/testing/test_fault_report.py
git commit -m "test: add property and fault matrices"
```
