# Agent SDK Implementation Plan Index

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved Agent SDK through a thin end-to-end vertical slice followed by subsystem hardening and release validation.

**Architecture:** The implementation is a modular Python package backed by an immutable event log and state projections. SQLite is the default durable store, LiteLLM is the only model layer, and every capability routes through shared runtime, policy, context, and observability contracts.

**Tech Stack:** Python 3.12–3.14, asyncio, uv, Pydantic v2, LiteLLM, aiosqlite, official MCP Python SDK, PyYAML, pytest, pytest-asyncio, Hypothesis, Ruff, mypy, optional FastAPI/OpenTelemetry.

## Global Constraints

- Python minimum version is 3.12; CI covers 3.12, 3.13, and 3.14.
- Model calls use LiteLLM only; no public provider abstraction is permitted.
- SQLite is the default store; InMemoryStore is opt-in and custom stores must pass the contract suite.
- Session close retains data; Session delete removes all SDK-managed session data and analytics contributions.
- Tool side effects always pass schema validation and the permission engine.
- Context compression never mutates the original Session Ledger.
- Generated workflows require validation and approval unless an explicit low-risk auto-approval policy matches.
- Child permissions can only narrow parent permissions.
- Events and public errors are versioned; unknown non-idempotent outcomes are never silently retried.
- Every task follows TDD, ends with exact verification, and commits independently.

---

The canonical roadmap is [`docs/plans/00-roadmap.md`](../../plans/00-roadmap.md). File ownership is fixed in [`docs/plans/01-file-map.md`](../../plans/01-file-map.md). Milestone and task status is tracked in [`docs/plans/tasks/index.md`](../../plans/tasks/index.md).
