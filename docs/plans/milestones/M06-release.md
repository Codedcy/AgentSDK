# M06 Conformance and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the hardened SDK into a verified distributable with public contract suites, reference integrations, performance budgets, and release evidence.

**Architecture:** Public testing helpers validate extension contracts without private imports. Examples exercise the same installed wheel users receive; CI spans supported Python/OS/SQLite combinations and migration fixtures.

**Tech Stack:** pytest plugins/helpers, Hypothesis, FastAPI/SSE, Typer, build/uv, GitHub Actions or equivalent CI.

## Global Constraints

- Examples use only public imports.
- CI tests the built wheel in a clean environment.
- Contract failures produce human-readable and JSON reports.
- Release completion requires verification evidence, not only task status.

---

## Tasks

1. [`M06-T001-contract-fault-tests.md`](../tasks/M06-T001-contract-fault-tests.md)
2. [`M06-T002-reference-apps.md`](../tasks/M06-T002-reference-apps.md)
3. [`M06-T003-compat-performance-package.md`](../tasks/M06-T003-compat-performance-package.md)
4. [`M06-T004-release-verification.md`](../tasks/M06-T004-release-verification.md)

## Milestone Verification

```powershell
uv run ruff check .
uv run mypy src
uv run pytest -q
uv build
uv run python -m agent_sdk.testing.release_check dist/*.whl
```

Expected: all commands exit 0; release report contains compatibility, migration, performance, conformance, example, and package-install evidence.
