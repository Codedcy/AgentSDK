# Agent SDK v0.1 tracing and analysis

## Live and historical views

Trace collection is SDK-side; the application decides whether and how to display
it. Subscribe to bounded raw events while work is active:

```python
from agent_sdk import EventFilter

async for observed in sdk.trace.subscribe(
    filters=EventFilter(session_id=session_id), cursor=0
):
    render(observed)
```

After completion, `await sdk.trace.timeline(run_or_workflow_id)` projects a stable,
normalized stage tree from one durable high-water mark. Stages cover Runs, Context,
model calls, Tools, permissions, Workflows/nodes, Children/messages, evaluations,
and recovery. Payloads contain identifiers, hashes, bounded previews, usage/cost,
sanitized error facts, and evidence references—not credentials or unbounded raw
provider values.

## Evaluation and cross-Run metrics

`await sdk.evaluations.evaluate(run_id, evaluator)` stores an evidence-linked
decision. v0.1 exposes:

- `sdk.analytics.success_rate(evaluator_id=...)`: pass evaluations divided by
  known evaluations;
- `sdk.analytics.tool_failures(tool_name=...)`: count of non-succeeded known Tool
  outcomes;
- `sdk.analytics.tool_failure_rate(tool_name=...)`: non-succeeded outcomes divided
  by known outcomes.

Each result reports `sample_count`, `missing_count`, method, and evidence ids.
Missing or invalid facts are excluded from known samples rather than silently
counted as success or failure. Session deletion removes that Session's contribution.

## Attribution boundary

`await sdk.trace.attribution(run_id)` implements
`deterministic_event_evidence_v1`. It is deterministic correlation, not causality.
The projector joins durable evidence for Model, Tool, Context, Workflow, Child, and
evaluation records. It identifies consumed/unused/supporting/terminal contributors,
the first terminal failing stage when applicable, and a fixed set of evidence-linked
hints. It does not use an LLM, infer intent, estimate causal effect, or claim that a
correlated contributor caused the Run outcome.

Keep the method and evidence ids when exporting attribution. Recompute against the
same durable high-water when comparing results; do not compare a live partial view
with a completed view as though they were equivalent.

## Deferred beyond v0.1

The following are deliberately deferred: aggregate Tool usefulness ranking,
multidimensional failure analysis/taxonomy, statistical confidence, generated
insights, automatic optimization, causal analysis, and dashboards/exporters. Build
those only after defining product-specific semantics and validating their evidence;
the v0.1 per-Run attribution and basic rates do not imply them.
