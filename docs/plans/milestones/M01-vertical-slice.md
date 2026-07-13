# M01 Thin Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce the smallest installable, restartable Agent SDK path that exercises every approved subsystem once.

**Architecture:** M01 creates stable contracts and intentionally narrow implementations behind them. The integrated scenario uses SQLite, LiteLLM, one permissioned Tool, one MCP Server, one Skill, one compaction event, one Workflow/Child, one Evaluator, and one analytics query.

**Tech Stack:** Python 3.12+, uv, Pydantic v2, LiteLLM, aiosqlite, MCP Python SDK, PyYAML, pytest/pytest-asyncio.

## Global Constraints

- Do not add hardening behavior not needed by the vertical-slice acceptance test.
- Public names must match `docs/plans/01-file-map.md`.
- Every state change is represented by an EventEnvelope and persisted before it is exposed.
- Focused LiteLLM/MCP tests may use injected fake seams. The integrated E2E uses a real
  local stdio MCP server, a scripted LiteLLM seam, and no network credentials.
- M01 recovery means public read recovery after all active work and external
  connections are quiescent; in-flight permission recovery starts in M02.
- Generated Workflow YAML is application-confirmed before the existing Workflow API is
  called. Durable dynamic proposals remain M04 scope.
- The M01 write-like Tool is application-registered; built-in coding Tools and hardened
  path/command policies remain M03 scope.
- M01 ships a minimal public-API CLI; release-grade reference applications and the
  monitor server remain M06 scope.
- Every task must leave `uv run pytest -q` green.

---

## Tasks

1. [`M01-T001-project-scaffold.md`](../tasks/M01-T001-project-scaffold.md) — package, quality tools, configuration, ids, errors.
2. [`M01-T002-event-storage-contracts.md`](../tasks/M01-T002-event-storage-contracts.md) — EventEnvelope, Store Protocols, InMemoryStore.
3. [`M01-T003-sqlite-runtime-spine.md`](../tasks/M01-T003-sqlite-runtime-spine.md) — migration, SQLite Store, Session/Run snapshots.
4. [`M01-T004-litellm-agent-loop.md`](../tasks/M01-T004-litellm-agent-loop.md) — stream normalization and minimal Agent Loop.
5. [`M01-T005-tool-permission-slice.md`](../tasks/M01-T005-tool-permission-slice.md) — one registered Tool, persisted ask/resolve permission, and Tool Outcome.
6. [`M01-T006-mcp-skill-slice.md`](../tasks/M01-T006-mcp-skill-slice.md) — one MCP tool and one progressively loaded Skill.
7. [`M01-T007-context-compaction-slice.md`](../tasks/M01-T007-context-compaction-slice.md) — Prompt Manifest, Artifact reference, forced Capsule, and source retrieval.
8. [`M01-T008-workflow-child-slice.md`](../tasks/M01-T008-workflow-child-slice.md) — sequential Workflow IR and one isolated Child Run.
9. [`M01-T009-observability-evaluation-slice.md`](../tasks/M01-T009-observability-evaluation-slice.md) — live query/subscription, one Evaluator, and simple success/tool aggregates.
10. [`M01-T010-integrated-e2e.md`](../tasks/M01-T010-integrated-e2e.md) — reference CLI and complete recovery/deletion scenario.

## Milestone Verification

Run:

```powershell
uv sync --all-extras
uv run ruff check .
uv run mypy src
uv run pytest tests/unit tests/integration tests/e2e/test_vertical_slice.py -v
```

Expected: all commands exit 0; the E2E test proves restart recovery, live event queries, one Child result, one compaction record, one Evaluation, one analytics result, and no Session data after deletion.
