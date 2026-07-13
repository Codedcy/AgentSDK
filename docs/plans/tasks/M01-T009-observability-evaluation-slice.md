# M01-T009 Observability, Evaluation, and Analytics Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Query current execution state, subscribe by cursor, evaluate one Run, and aggregate success/tool failure counts.

**Architecture:** QueryService reads snapshots/events; SubscriptionService polls durable cursors for the slice. EvaluationEngine appends immutable results, and AnalyticsQueries aggregate facts directly from SQLite/InMemory records.

**Tech Stack:** Pydantic, async iterators, StateStore, SQLite JSON queries.

## Global Constraints

- Query results expose `as_of_cursor`.
- Evaluations include method, evidence refs, and version.
- Analytics never infer causality in the slice.

---

### Task 1: Add query/subscription/evaluation/aggregate contracts

**Files:**
- Create: `src/agent_sdk/observability/queries.py`
- Create: `src/agent_sdk/observability/subscriptions.py`
- Create: `src/agent_sdk/evaluation/models.py`
- Create: `src/agent_sdk/evaluation/engine.py`
- Create: `src/agent_sdk/analytics/models.py`
- Create: `src/agent_sdk/analytics/queries.py`
- Create: `tests/integration/observability/test_observability_slice.py`

**Interfaces:**
- Produces: `QueryService.get_run/timeline/execution_tree`, `SubscriptionService.subscribe`, `Evaluator`, `EvaluationResult`, `EvaluationEngine.evaluate`, `AnalyticsQueries.success_rate/tool_failures`.
- Consumes: `StateStore`, Run/Workflow/Tool events and snapshots.

- [ ] **Step 1: Write a cursor/evaluation/aggregate test**

```python
@pytest.mark.asyncio
async def test_run_is_queryable_evaluated_and_aggregated(sdk: AgentSDK) -> None:
    run = await sdk.fixtures.completed_run(output="ok", one_failed_tool=True)
    snapshot = await sdk.queries.get_run(run.run_id)
    assert snapshot.status == "completed" and snapshot.as_of_cursor > 0
    evaluation = await sdk.evaluations.evaluate(run.run_id, ExactOutputEvaluator("ok"))
    assert evaluation.verdict == "pass" and evaluation.evidence_event_ids
    metrics = await sdk.analytics.success_rate()
    assert metrics.sample_count == 1 and metrics.value == 1.0
    assert (await sdk.analytics.tool_failures()).value == 1
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/observability/test_observability_slice.py -v`

Expected: missing observability/evaluation/analytics modules.

- [ ] **Step 3: Implement query and cursor subscription**

`get_run` returns Run snapshot plus store's latest cursor. `timeline` filters Run events. `execution_tree` joins parent/Workflow/Child ids from payloads. Subscription repeatedly reads after cursor, yields StoredEvents, and stops on cancellation.

```python
async def subscribe(self, after_cursor: int = 0) -> AsyncIterator[StoredEvent]:
    cursor = after_cursor
    while True:
        events = await self._store.read_events(after_cursor=cursor)
        if not events:
            await self._notifier.wait_after(cursor)
            continue
        for event in events:
            yield event
            cursor = event.cursor
```

- [ ] **Step 4: Implement Evaluator result persistence**

```python
class EvaluationResult(BaseModel):
    evaluation_id: str; subject_id: str; evaluator_id: str; evaluator_version: str
    verdict: Literal["pass", "fail", "unknown"]; metrics: dict[str, float]
    method: str; confidence: float | None; evidence_event_ids: tuple[str, ...]
```

Persist `evaluation.completed` and an Evaluation snapshot; do not change Run status.

- [ ] **Step 5: Implement simple aggregate results**

```python
class AnalyticsResult(BaseModel):
    metric: str; value: float; sample_count: int; missing_count: int
    method: str; as_of_cursor: int
```

Success rate uses explicit Run Evaluation verdicts; tool failure count uses terminal ToolCall events.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/observability/test_observability_slice.py -v`

Expected: snapshot/cursor, Evaluation evidence, and aggregates match the seeded Run.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/observability src/agent_sdk/evaluation src/agent_sdk/analytics tests/integration/observability
git commit -m "feat: add observability and evaluation slice"
```
