# M03 Capabilities and Context Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete safe Tool execution, permission policies, Coding tools, MCP/Skills interoperability, Prompt Profiles, and verified multi-level context compaction.

**Architecture:** All capability sources normalize into ToolRegistry and pass PolicyEngine. Prompt/Context code consumes immutable snapshots and writes new ContextViews/Capsules without changing the Session Ledger.

**Tech Stack:** Pydantic, asyncio subprocess, pathlib, MCP Python SDK, PyYAML, LiteLLM, Hypothesis.

## Global Constraints

- Path checks use normalized real paths and include non-existing write targets.
- Permission precedence is deny, specific allow, specific ask, default.
- `allowed-tools` in Skills never grants permission.
- Context thresholds are 0.70/0.80/0.90/0.96 with target recovery below 0.75.

---

## Tasks

1. [`M03-T001-tool-executor.md`](../tasks/M03-T001-tool-executor.md)
2. [`M03-T002-policy-permissions.md`](../tasks/M03-T002-policy-permissions.md)
3. [`M03-T003-coding-tools.md`](../tasks/M03-T003-coding-tools.md)
4. [`M03-T004-mcp-skills.md`](../tasks/M03-T004-mcp-skills.md)
5. [`M03-T005-prompts-compaction.md`](../tasks/M03-T005-prompts-compaction.md)

## Milestone Verification

```powershell
uv run pytest tests/unit/permissions tests/property/test_path_policy.py tests/integration/tools -v
uv run pytest tests/integration/mcp tests/integration/skills tests/integration/context -v
```

Expected: no tested path/command/Skill/MCP route bypasses Policy; all compression strategies retain source references and protected items.
