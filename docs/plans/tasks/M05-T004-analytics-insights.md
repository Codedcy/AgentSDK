# M05-T004 Cross-run Analytics and Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Analyze success rates, failure causes/stages, result attribution, tool reliability/usefulness, and evidence-backed improvement directions across Runs.

**Architecture:** Analytics queries operate on version-fingerprinted projections and explicitly state sample size, missingness, method, and high-water cursor. Deterministic statistics and provenance rules produce facts; optional LiteLLM analysis produces labeled hypotheses and recommendations.

**Tech Stack:** SQLite SQL, Python statistics, LiteLLM structured outputs, Pydantic v2, pytest.

## Global Constraints

- Comparisons never silently mix incompatible agent/prompt/model/tool/workflow/evaluator versions.
- Every metric reports numerator, denominator, missing count, filters, time window, and data cursor.
- Correlation and attribution are named separately; observational data does not become causal proof.
- Insights never mutate prompts, tools, workflows, or policy automatically.

---

### Task 1: Implement tool effectiveness projections and queries

**Files:**
- Create: `src/agent_sdk/analytics/models.py`
- Create: `src/agent_sdk/analytics/tool_effectiveness.py`
- Modify: `src/agent_sdk/observability/projections.py`
- Create: `tests/integration/analytics/test_tool_metrics.py`

- [ ] **Step 1: Write failing tool metric tests**

```python
@pytest.mark.asyncio
async def test_tool_failure_rate_reports_denominator(analytics_fixture) -> None:
    report = await analytics_fixture.tool_report(name="search")
    assert report.failure_rate.numerator == 2
    assert report.failure_rate.denominator == 10
    assert report.sample_size == 10

@pytest.mark.asyncio
async def test_unused_result_is_not_the_same_as_failed_tool(analytics_fixture) -> None:
    report = await analytics_fixture.tool_report(name="read")
    assert report.reliability.failure_count == 0
    assert report.usefulness.unused_count == 3
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/analytics/test_tool_metrics.py -v`

Expected: reliability/efficiency/usefulness metrics are missing.

- [ ] **Step 3: Implement explicit metric models**

```python
class RateMetric(BaseModel, frozen=True):
    numerator: int
    denominator: int
    missing: int = 0
    value: float | None

class ToolEffectiveness(BaseModel, frozen=True):
    tool_name: str
    tool_version: str
    reliability: ReliabilityMetrics
    efficiency: EfficiencyMetrics
    usefulness: UsefulnessMetrics
    sample_size: int
    high_water_cursor: int
```

Reliability includes failure/timeout/denial/unknown-outcome/retry rates. Efficiency includes latency/tokens/output bytes/repeated calls. Usefulness uses downstream citations, context retention, follow-up dependence, evaluator deltas, and explicit labels—each method reported separately.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/integration/analytics/test_tool_metrics.py -v`

Expected: version splits, missing evidence, retries, unused results, always-failing tools, percentiles, and deletion recomputation pass.

```powershell
git add src/agent_sdk/analytics src/agent_sdk/observability/projections.py tests/integration/analytics/test_tool_metrics.py
git commit -m "feat: analyze tool effectiveness"
```

---

### Task 2: Implement cross-run success, failure, and version comparisons

**Files:**
- Create: `src/agent_sdk/analytics/queries.py`
- Create: `src/agent_sdk/analytics/comparison.py`
- Create: `src/agent_sdk/analytics/attribution.py`
- Create: `tests/integration/analytics/test_cross_run.py`

- [ ] **Step 1: Write failing cohort and deletion tests**

```python
@pytest.mark.asyncio
async def test_default_cohort_does_not_mix_prompt_versions(analytics_fixture) -> None:
    report = await analytics_fixture.success_report()
    assert len(report.cohorts) == 2
    assert {cohort.fingerprint.prompt_sha256 for cohort in report.cohorts} == {"p1", "p2"}

@pytest.mark.asyncio
async def test_session_deletion_removes_analytics_contribution(analytics_fixture) -> None:
    before = await analytics_fixture.success_report()
    await analytics_fixture.delete_session("failed-session")
    after = await analytics_fixture.success_report()
    assert after.total_runs == before.total_runs - 1
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/analytics/test_cross_run.py -v`

Expected: versioned cohorts and deletion-safe aggregation are missing.

- [ ] **Step 3: Implement query/fingerprint/report contracts**

```python
class VersionFingerprint(BaseModel, frozen=True):
    agent_version: str
    prompt_sha256: str
    model: str
    tool_registry_sha256: str
    workflow_sha256: str | None
    evaluator_set_sha256: str

class AnalyticsQuery(BaseModel, frozen=True):
    session_ids: tuple[str, ...] | None = None
    time_window: TimeWindow | None = None
    group_by: tuple[str, ...] = ("version_fingerprint",)
    minimum_sample_size: int = 1
```

Return success rate, outcome distribution, failure stage/category/root chain, cost/tokens/latency, workflow node dropout, child contribution, and result attribution with confidence/method labels.

- [ ] **Step 4: Implement comparisons without causal overclaiming**

Provide absolute/relative deltas and Wilson intervals for binary outcomes when samples allow. Mark comparisons `insufficient_data`, `observational_association`, or `controlled_comparison` based on query metadata; never label observational deltas as causal.

```python
def compare_rates(left: RateMetric, right: RateMetric, design: StudyDesign) -> RateComparison:
    if min(left.denominator, right.denominator) < design.minimum_sample_size:
        return RateComparison.insufficient_data(left, right)
    label = "controlled_comparison" if design.controlled else "observational_association"
    return RateComparison(label=label, absolute_delta=right.value - left.value, relative_delta=safe_relative_delta(left.value, right.value), intervals=(wilson(left), wilson(right)))
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/integration/analytics/test_cross_run.py -v`

Expected: cohort splitting, missing data, windows, sample thresholds, statistics, attribution labels, and Session deletion pass.

```powershell
git add src/agent_sdk/analytics tests/integration/analytics/test_cross_run.py
git commit -m "feat: add versioned cross-run analytics"
```

---

### Task 3: Generate evidence-backed improvement insights

**Files:**
- Create: `src/agent_sdk/analytics/insights.py`
- Create: `src/agent_sdk/analytics/rules.py`
- Create: `tests/unit/analytics/test_insights.py`
- Create: `tests/integration/analytics/test_judge_insights.py`

- [ ] **Step 1: Write failing evidence tests**

```python
def test_rule_insight_cites_metric_and_runs(insight_engine) -> None:
    insight = insight_engine.analyze(report_with_high_search_timeout_rate())[0]
    assert insight.category == "tool_reliability"
    assert insight.evidence.metric_ids
    assert insight.evidence.run_ids
    assert insight.recommended_action

def test_low_sample_warning_suppresses_strong_recommendation(insight_engine) -> None:
    insights = insight_engine.analyze(report_with_one_failure())
    assert all(item.confidence <= 0.3 for item in insights)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/analytics/test_insights.py tests/integration/analytics/test_judge_insights.py -v`

Expected: rules and model-assisted insights are missing.

- [ ] **Step 3: Implement deterministic rule insights**

```python
class ImprovementInsight(BaseModel, frozen=True):
    category: str
    finding: str
    hypothesis: str | None
    recommended_action: str
    confidence: float
    method: Literal["rule", "judge"]
    evidence: InsightEvidence
    limitations: tuple[str, ...]
```

Rules cover repeated failure stage, always-failing/slow/unused tools, redundant calls, permission bottlenecks, compaction loss, workflow dropout, child-agent cost without outcome improvement, and evaluator disagreement. Each rule has minimum sample/effect thresholds.

- [ ] **Step 4: Add optional LiteLLM hypothesis synthesis**

Pass only redacted aggregate facts and selected evidence excerpts to structured output. Validate every returned evidence id against the input set, preserve model/prompt hashes, label output `judge`, and reject unsupported claims.

```python
async def synthesize(self, report: AnalyticsReport) -> tuple[ImprovementInsight, ...]:
    evidence = self._redactor.apply(report.judge_evidence())
    proposed = await self._models.complete_structured(self._messages(evidence), schema=JudgeInsights)
    allowed = set(report.evidence_ids())
    if any(not set(item.evidence.ids) <= allowed for item in proposed.items):
        raise UnsupportedInsightEvidence()
    return tuple(item.as_insight(method="judge", provenance=self._provenance) for item in proposed.items)
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/unit/analytics/test_insights.py tests/integration/analytics/test_judge_insights.py -v`

Expected: deterministic rules, low-sample cautions, Judge validation, redaction, provenance, and no-auto-mutation pass.

```powershell
git add src/agent_sdk/analytics tests/unit/analytics tests/integration/analytics/test_judge_insights.py
git commit -m "feat: generate evidence-backed insights"
```
