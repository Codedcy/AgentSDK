# M02 Runtime and Storage Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Session, Run, persistence, recovery, cancellation, and synchronization semantics production-reliable.

**Architecture:** M02 strengthens the M01 contracts without changing their public shapes unnecessarily. Short SQLite transactions append events and update projections; leases and reconciliation handle failures around external I/O.

**Tech Stack:** asyncio, aiosqlite WAL, Pydantic, pytest-asyncio, subprocess fault fixtures.

## Global Constraints

- External I/O never occurs inside a SQLite transaction.
- Unknown non-idempotent outcomes always require reconciliation.
- Session deletion removes events, projections, evaluations, analytics contributions, and artifacts.
- Sync APIs wrap the async engine and reject calls from an active same-thread event loop.

---

## Tasks

1. [`M02-T001-session-idempotency.md`](../tasks/M02-T001-session-idempotency.md)
2. [`M02-T002-leases-reconciliation.md`](../tasks/M02-T002-leases-reconciliation.md)
3. [`M02-T003-artifacts-migrations.md`](../tasks/M02-T003-artifacts-migrations.md)
4. [`M02-T004-control-sync-api.md`](../tasks/M02-T004-control-sync-api.md)

## Milestone Verification

```powershell
uv run pytest tests/contract/test_store_contract.py tests/integration/storage tests/integration/runtime -v
uv run pytest tests/e2e/test_crash_matrix.py tests/e2e/test_session_delete.py -v
```

Expected: all tests pass across transaction boundaries; repeated commands do not duplicate state or side effects.
