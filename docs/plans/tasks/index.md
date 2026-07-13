# Task Index

Status values: `pending`, `in_progress`, `blocked`, `done`.

| ID | Task | Milestone | Depends on | Status |
|---|---|---|---|---|
| [M01-T001](M01-T001-project-scaffold.md) | Project scaffold and core configuration | M01 | — | done |
| [M01-T002](M01-T002-event-storage-contracts.md) | Event and storage contracts | M01 | T001 | in_progress |
| [M01-T003](M01-T003-sqlite-runtime-spine.md) | SQLite Session and Run spine | M01 | T002 | pending |
| [M01-T004](M01-T004-litellm-agent-loop.md) | LiteLLM Agent Loop slice | M01 | T003 | pending |
| [M01-T005](M01-T005-tool-permission-slice.md) | Tool and permission slice | M01 | T004 | pending |
| [M01-T006](M01-T006-mcp-skill-slice.md) | MCP and Skill slice | M01 | T005 | pending |
| [M01-T007](M01-T007-context-compaction-slice.md) | Context and compaction slice | M01 | T006 | pending |
| [M01-T008](M01-T008-workflow-child-slice.md) | Workflow and Child slice | M01 | T007 | pending |
| [M01-T009](M01-T009-observability-evaluation-slice.md) | Observability, evaluation, analytics slice | M01 | T008 | pending |
| [M01-T010](M01-T010-integrated-e2e.md) | Integrated vertical-slice CLI and E2E | M01 | T009 | pending |
| [M02-T001](M02-T001-session-idempotency.md) | Complete Session lifecycle and idempotency | M02 | M01 | pending |
| [M02-T002](M02-T002-leases-reconciliation.md) | Leases, interruption, and reconciliation | M02 | T001 | pending |
| [M02-T003](M02-T003-artifacts-migrations.md) | Artifact lifecycle and migrations | M02 | T001 | pending |
| [M02-T004](M02-T004-control-sync-api.md) | Cancellation, pause/resume, sync facade | M02 | T002 | pending |
| [M03-T001](M03-T001-tool-executor.md) | Tool registry and executor hardening | M03 | M02 | pending |
| [M03-T002](M03-T002-policy-permissions.md) | Path/command policy and permission scopes | M03 | T001 | pending |
| [M03-T003](M03-T003-coding-tools.md) | Coding tools and security boundaries | M03 | T002 | pending |
| [M03-T004](M03-T004-mcp-skills.md) | MCP and Skills hardening | M03 | T001 | pending |
| [M03-T005](M03-T005-prompts-compaction.md) | Prompt Profiles and multi-level compaction | M03 | T004 | pending |
| [M04-T001](M04-T001-workflow-compiler.md) | Workflow DSL, Builder, and compiler | M04 | M03 | pending |
| [M04-T002](M04-T002-workflow-executor.md) | Durable Workflow executor and control nodes | M04 | T001 | pending |
| [M04-T003](M04-T003-subagent-scheduler.md) | Child scheduler, budgets, and permissions | M04 | T002 | pending |
| [M04-T004](M04-T004-dynamic-workflow.md) | Dynamic Workflow approval and Coding template | M04 | T003 | pending |
| [M05-T001](M05-T001-projections-subscriptions.md) | Event projections, queries, and subscriptions | M05 | M04 | pending |
| [M05-T002](M05-T002-redaction-exporters.md) | Redaction and exporters | M05 | T001 | pending |
| [M05-T003](M05-T003-evaluation-failures.md) | Evaluation and failure taxonomy | M05 | T001 | pending |
| [M05-T004](M05-T004-analytics-insights.md) | Tool Effectiveness, analytics, and insights | M05 | T003 | pending |
| [M06-T001](M06-T001-contract-fault-tests.md) | Contract kits and fault/property tests | M06 | M05 | pending |
| [M06-T002](M06-T002-reference-apps.md) | Reference CLI and monitor server | M06 | T001 | pending |
| [M06-T003](M06-T003-compat-performance-package.md) | Compatibility, performance, and packaging | M06 | T001 | pending |
| [M06-T004](M06-T004-release-verification.md) | Release verification and documentation | M06 | T002,T003 | pending |

When a task completes, add its commit id and verification result below the table in the same commit that marks it `done`.

- M01-T001: implementation `065aaa1`; Python 3.13 `pytest` 1 passed, Ruff passed, mypy passed; task review approved. TDD evidence is recorded in `.superpowers/sdd/M01-T001-report.md`.
