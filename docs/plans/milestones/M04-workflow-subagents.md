# M04 Workflow and Subagents Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement durable typed Workflows and fully controlled Child Runs with dynamic approval and Coding templates.

**Architecture:** YAML/JSON DSL and Python Builder compile to the same immutable WorkflowIR. Node and Child scheduling use persisted events/projections, bounded concurrency, explicit waits, and narrowed permission/context scopes.

**Tech Stack:** Pydantic discriminated unions, PyYAML, asyncio scheduler primitives, SQLite projections, pytest/Hypothesis.

## Global Constraints

- Dynamic Workflow data cannot execute arbitrary Python or eval expressions.
- Loops and foreach are bounded by explicit item/iteration limits.
- Child permissions are the intersection of parent, TaskEnvelope, and Child AgentSpec.
- Completed nodes and children are never repeated during restart recovery.

---

## Tasks

1. [`M04-T001-workflow-compiler.md`](../tasks/M04-T001-workflow-compiler.md)
2. [`M04-T002-workflow-executor.md`](../tasks/M04-T002-workflow-executor.md)
3. [`M04-T003-subagent-scheduler.md`](../tasks/M04-T003-subagent-scheduler.md)
4. [`M04-T004-dynamic-workflow.md`](../tasks/M04-T004-dynamic-workflow.md)

## Milestone Verification

```powershell
uv run pytest tests/property/test_workflow_graphs.py tests/integration/workflow -v
uv run pytest tests/integration/subagents tests/e2e/test_workflow_restart_matrix.py -v
```

Expected: DSL/Builder hashes match; dynamic approval, waits, parallel joins, budgets, cancellation, and recovery pass.
