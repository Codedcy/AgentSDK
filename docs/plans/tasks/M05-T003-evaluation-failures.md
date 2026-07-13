# M05-T003 Evaluation and Failure Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evaluate Runs, workflow nodes, and tool results with reproducible evidence, then distinguish root failures from cascading symptoms.

**Architecture:** Pluggable evaluators return versioned `EvaluationResult` values. Synchronous results can gate workflow progress; asynchronous results enrich analytics. A deterministic classifier creates a failure chain and permits Judge assistance only as separately labeled evidence.

**Tech Stack:** Pydantic v2, LiteLLM structured outputs, pytest-asyncio.

## Global Constraints

- Evaluations include scope, evaluator/version, configuration hash, evidence references, timing, and status.
- Built-in deterministic evaluators run without a model; Judge results are labeled probabilistic.
- Root cause, contributing cause, and symptom are distinct relations.
- Unknown is a valid classification and is preferable to unsupported certainty.

---

### Task 1: Implement evaluator protocol and built-ins

**Files:**
- Modify: `src/agent_sdk/evaluation/models.py`
- Modify: `src/agent_sdk/evaluation/registry.py`
- Create: `src/agent_sdk/evaluation/builtins.py`
- Modify: `src/agent_sdk/evaluation/engine.py`
- Create: `tests/unit/evaluation/test_builtins.py`
- Create: `tests/integration/evaluation/test_quality_gates.py`

- [ ] **Step 1: Write failing evaluator tests**

```python
def test_assertion_evaluator_records_evidence() -> None:
    result = AssertionEvaluator("output.answer == 42").evaluate(evaluation_case(answer=41))
    assert result.status == "failed"
    assert result.evidence_refs
    assert result.evaluator_version

@pytest.mark.asyncio
async def test_failed_sync_evaluation_blocks_workflow(workflow_runner) -> None:
    result = await workflow_runner.run(workflow_with_gate("command", command=[sys.executable, "-c", "raise SystemExit(1)"]))
    assert result.status == "failed"
    assert result.failure.stage == "evaluation"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/evaluation/test_builtins.py tests/integration/evaluation/test_quality_gates.py -v`

Expected: evaluator contracts and gates are incomplete.

- [ ] **Step 3: Implement protocol, registry, and result provenance**

```python
class Evaluator(Protocol):
    name: str
    version: str
    async def evaluate(self, case: EvaluationCase) -> EvaluationResult: ...

class EvaluationResult(BaseModel, frozen=True):
    status: Literal["passed", "failed", "inconclusive", "error"]
    score: float | None = None
    reason: str
    evaluator_name: str
    evaluator_version: str
    config_sha256: str
    evidence_refs: tuple[str, ...]
    metrics: dict[str, float] = Field(default_factory=dict)
```

- [ ] **Step 4: Add deterministic and Judge evaluators**

Implement schema/assertion, process exit, test command, file/content, explicit user outcome, repetition/stall, and application callback evaluators. LiteLLM Judge uses structured output, stores prompt/model hashes, and never overwrites deterministic results.

```python
class JudgeEvaluator:
    name = "litellm_judge"
    version = "1"

    async def evaluate(self, case: EvaluationCase) -> EvaluationResult:
        judged = await self._models.complete_structured(self._messages(case), schema=JudgeVerdict)
        return EvaluationResult(
            status=judged.status,
            score=judged.score,
            reason=judged.reason,
            evaluator_name=self.name,
            evaluator_version=self.version,
            config_sha256=self._config_sha256,
            evidence_refs=case.evidence_refs,
        )
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/unit/evaluation/test_builtins.py tests/integration/evaluation/test_quality_gates.py -v`

Expected: pass/fail/inconclusive/error, sync/async modes, gates, versioning, and Judge provenance pass.

```powershell
git add src/agent_sdk/evaluation tests/unit/evaluation tests/integration/evaluation/test_quality_gates.py
git commit -m "feat: add evidence-backed evaluators"
```

---

### Task 2: Implement failure taxonomy and causal chains

**Files:**
- Create: `src/agent_sdk/evaluation/failures.py`
- Create: `src/agent_sdk/evaluation/classifier.py`
- Modify: `src/agent_sdk/evaluation/engine.py`
- Create: `tests/unit/evaluation/test_failure_classifier.py`

- [ ] **Step 1: Write failing root/cascade tests**

```python
def test_tool_timeout_is_root_and_downstream_gate_is_symptom(classifier) -> None:
    chain = classifier.classify(events_for_tool_timeout_then_gate_failure())
    assert chain.root.stage == "tool_execution"
    assert chain.root.category == "timeout"
    assert chain.symptoms[0].stage == "evaluation"

def test_unsupported_case_remains_unknown(classifier) -> None:
    assert classifier.classify(ambiguous_events()).root.category == "unknown"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/evaluation/test_failure_classifier.py -v`

Expected: taxonomy and causal relations are missing.

- [ ] **Step 3: Implement deterministic classification rules**

```python
class FailureStage(StrEnum):
    MODEL = "model"
    TOOL_SELECTION = "tool_selection"
    PERMISSION = "permission"
    TOOL_EXECUTION = "tool_execution"
    MCP = "mcp"
    SKILL = "skill"
    COMPACTION = "compaction"
    WORKFLOW = "workflow"
    SUBAGENT = "subagent"
    EVALUATION = "evaluation"
    STORAGE = "storage"
    UNKNOWN = "unknown"

class FailureRecord(BaseModel, frozen=True):
    stage: FailureStage
    category: str
    relation: Literal["root", "contributing", "symptom"]
    confidence: float
    evidence_refs: tuple[str, ...]
```

Use temporal/causation links, terminal error codes, retry exhaustion, permission decisions, and evaluator evidence. Add optional Judge suggestions as a separate candidate with its own confidence and provenance.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/evaluation/test_failure_classifier.py -v`

Expected: representative model/tool/MCP/workflow/subagent/storage chains and unknown cases pass.

```powershell
git add src/agent_sdk/evaluation/failures.py src/agent_sdk/evaluation/classifier.py src/agent_sdk/evaluation/engine.py tests/unit/evaluation/test_failure_classifier.py
git commit -m "feat: classify failure stages and causes"
```
