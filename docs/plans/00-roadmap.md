# Agent SDK Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a recoverable, observable Agent SDK by building one thin vertical slice across every approved capability, then hardening each subsystem without losing end-to-end operability.

**Architecture:** A single `agent_sdk` Python package owns stable contracts for events, storage, runtime, tools, policy, context, workflows, children, evaluation, and analytics. The first milestone implements the smallest usable path through all contracts; later milestones strengthen behavior behind those boundaries.

**Tech Stack:** Python 3.12–3.13, asyncio, uv, Pydantic v2, LiteLLM, aiosqlite, MCP Python SDK, PyYAML, pytest, pytest-asyncio, Hypothesis, Ruff, mypy, optional Typer/FastAPI/OpenTelemetry.

## Global Constraints

- Supported Python versions are 3.12 and 3.13; CI covers both. Python 3.14 remains a future compatibility gate until LiteLLM officially supports it.
- The import package is `agent_sdk`; source uses the `src/` layout.
- LiteLLM is the only model integration and is wrapped only by the internal `LiteLLMGateway` seam.
- Runtime code is asyncio-first; sync APIs are wrappers and never duplicate the engine.
- SQLite uses WAL and explicit migrations; external I/O never holds a database transaction.
- All public mutation commands accept idempotency keys where repetition could duplicate state.
- Every side effect passes Tool Schema and Policy checks.
- Events are immutable while a Session is retained; Session deletion removes that Session's events and derived data.
- Tests are written before implementation; every task commits independently.

---

## Delivery Waves

### M01 — Thin Vertical Slice

Plan: [`milestones/M01-vertical-slice.md`](milestones/M01-vertical-slice.md)

Creates an installable package and one integrated scenario containing SQLite Session persistence, LiteLLM streaming, a permissioned tool, one MCP server, one Skill, forced context compaction, a generated workflow, one Child Run, live events, an Evaluator, basic analytics, restart recovery, and Session deletion.

Release gate: `uv run pytest tests/e2e/test_vertical_slice.py -v` passes and the example CLI completes the scenario without private imports.

### M02 — Runtime and Storage Hardening

Plan: [`milestones/M02-runtime-storage.md`](milestones/M02-runtime-storage.md)

Adds complete Session lifecycle, leases, idempotency, migrations, Artifact lifecycle, cancellation, pause/resume, reconciliation, sync façade, and crash-boundary tests.

Release gate: all runtime/store contract and recovery tests pass on Python 3.12 and 3.13.

### M03 — Capabilities and Context Hardening

Plan: [`milestones/M03-capabilities-context.md`](milestones/M03-capabilities-context.md)

Completes Tool metadata/execution, path and command policy, four permission scopes, Coding tools, MCP health/reconnect, strict Skills, Prompt Profiles, multi-level compaction, and retrieval.

Release gate: security/property tests prove workspace, Skill, MCP, and Child paths cannot bypass Policy.

### M04 — Workflow and Subagent Hardening

Plan: [`milestones/M04-workflow-subagents.md`](milestones/M04-workflow-subagents.md)

Completes DSL/Builder parity, typed IR validation, durable node scheduling, parallel/foreach/waits/evaluation, dynamic workflow approvals, Child budgets, messaging, progress, cancellation, and deadlock checks.

Release gate: restart at every node/child boundary produces no duplicate completed work.

### M05 — Observability and Analytics Hardening

Plan: [`milestones/M05-observability-analytics.md`](milestones/M05-observability-analytics.md)

Completes event compatibility, projections, cursor subscriptions, redaction, exporters, failure taxonomy, Tool Effectiveness, cross-Run queries, attribution, insights, and deletion-aware aggregates.

Release gate: every metric and insight can be traced to events/evaluations, with missing data and confidence explicit.

### M06 — Conformance, References, and Release

Plan: [`milestones/M06-release.md`](milestones/M06-release.md)

Completes public contract suites, fault/property tests, reference CLI and monitor server, packaging, documentation, compatibility matrices, performance budgets, and the first releasable build.

Release gate: clean install, full CI, package build, examples, contract reports, and release checklist pass.

## Dependency Order

```text
M01 vertical slice
  -> M02 runtime/storage
  -> M03 capabilities/context
  -> M04 workflow/subagents
  -> M05 observability/analytics
  -> M06 conformance/release
```

Later milestones may start read-only design/test preparation early, but production code merges in this order so each main-branch commit preserves an executable integrated path.

## Progress Rules

- The task index is the authoritative work tracker; chat messages are not.
- A task moves to `done` only after its verification commands and commit are recorded.
- Any contract name change updates the file map, affected task documents, and design references in the same commit.
- A failed task remains isolated on its implementation branch/worktree until its test cycle passes.
- Milestone completion requires its release gate, not merely completion of individual task checkboxes.

## Spec Coverage

| Design | Primary milestone |
|---|---|
| Runtime/Session | M01, M02 |
| LiteLLM/Prompts | M01, M03 |
| Tools/Permissions | M01, M03 |
| MCP/Skills | M01, M03 |
| Context/Compaction | M01, M03 |
| Workflow | M01, M04 |
| Subagents | M01, M04 |
| Observability | M01, M05 |
| Evaluation/Analytics | M01, M05 |
| Resilience/Errors | M02, M06 |
| Testing/Conformance | all, finalized in M06 |
