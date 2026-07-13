# M05 Observability and Analytics Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make runtime state, traces, usage, failures, evaluations, Tool Effectiveness, attribution, and improvement insights queryable and evidence-backed.

**Architecture:** Versioned events feed synchronous core projections and cursor-based asynchronous consumers. Evaluations and per-Session analytics contributions remain immutable and deletion-aware; insights reference underlying evidence queries.

**Tech Stack:** Pydantic event schemas, SQLite projections, async iterators, optional OpenTelemetry, LiteLLM Judge/Analyzer.

## Global Constraints

- Exporter failure never changes Run outcome.
- Redaction runs before persistence/export; sensitive redaction failures fail closed.
- Every aggregate returns sample count, missing rate, filters, method, and cursor.
- Judge inference is labeled and never represented as deterministic causality.

---

## Tasks

1. [`M05-T001-projections-subscriptions.md`](../tasks/M05-T001-projections-subscriptions.md)
2. [`M05-T002-redaction-exporters.md`](../tasks/M05-T002-redaction-exporters.md)
3. [`M05-T003-evaluation-failures.md`](../tasks/M05-T003-evaluation-failures.md)
4. [`M05-T004-analytics-insights.md`](../tasks/M05-T004-analytics-insights.md)

## Milestone Verification

```powershell
uv run pytest tests/contract/test_exporter_contract.py tests/integration/observability -v
uv run pytest tests/integration/evaluation tests/integration/analytics tests/e2e/test_deletion_analytics.py -v
```

Expected: cursor recovery, redaction, failure classification, Tool Effectiveness, attribution, insights, and deletion-aware aggregates pass.
