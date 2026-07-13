# Task Index

Status values: `pending`, `in_progress`, `blocked`, `done`.

| ID | Task | Milestone | Depends on | Status |
|---|---|---|---|---|
| [M01-T001](M01-T001-project-scaffold.md) | Project scaffold and core configuration | M01 | — | done |
| [M01-T002](M01-T002-event-storage-contracts.md) | Event and storage contracts | M01 | T001 | done |
| [M01-T003](M01-T003-sqlite-runtime-spine.md) | SQLite Session and Run spine | M01 | T002 | done |
| [M01-T004](M01-T004-litellm-agent-loop.md) | LiteLLM Agent Loop slice | M01 | T003 | done |
| [M01-T005](M01-T005-tool-permission-slice.md) | Tool and permission slice | M01 | T004 | done |
| [M01-T006](M01-T006-mcp-skill-slice.md) | MCP and Skill slice | M01 | T005 | done |
| [M01-T007](M01-T007-context-compaction-slice.md) | Context and compaction slice | M01 | T006 | done |
| [M01-T008](M01-T008-workflow-child-slice.md) | Workflow and Child slice | M01 | T007 | done |
| [M01-T009](M01-T009-observability-evaluation-slice.md) | Observability, evaluation, analytics slice | M01 | T008 | done |
| [M01-T010](M01-T010-integrated-e2e.md) | Integrated vertical-slice CLI and E2E | M01 | T009 | done |
| [M02-T001](M02-T001-session-idempotency.md) | Complete Session lifecycle and idempotency | M02 | M01 | in_progress |
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
- M01-T002: implementation `955163b`, fixes `475d221`; Python 3.13 `pytest` 16 passed, Ruff passed, mypy passed; task review approved after re-review. TDD evidence is recorded in `.superpowers/sdd/M01-T002-report.md`.
- M01-T003: implementation `fce50b5`, fixes `363989a` and `5ee5c97`; Python 3.13 `pytest` 47 passed, Ruff passed, mypy passed; task review approved after cancellation-safety re-review. TDD and packaging evidence is recorded in `.superpowers/sdd/M01-T003-report.md`.
- M01-T004: implementation `9680fad`, fixes `8058c60` and `6eae6a1`; Python 3.13 `pytest` 80 passed, Ruff passed, mypy passed; task review approved after lifecycle, termination, immutability, and public-error re-reviews. TDD evidence is recorded in `.superpowers/sdd/M01-T004-report.md`.
- M01-T005: implementation `ae1d523`, fixes `66256e1` and `933bc71`; Python 3.13 `pytest` 110 passed, Ruff passed, mypy passed; task review approved after permission-failure, cancellation, boundedness, and concurrent-resolution re-reviews. TDD evidence is recorded in `.superpowers/sdd/M01-T005-report.md`.
- M01-T006: implementation `eb2d59b`, fixes `1f71d2d` and `2259b85`; Python 3.13 `pytest` 186 passed, Ruff passed, mypy passed; task review approved after MCP task-affine lifecycle, cancellation/readiness settlement, YAML complexity, and Skill path-identity re-reviews. TDD evidence is recorded in `.superpowers/sdd/M01-T006-report.md`.
- M01-T007: implementation `b0b40e5`, fixes `c6d1340` and `29c38a6`; Python 3.13 `pytest` 246 passed, Ruff passed, mypy passed, wheel/sdist resources verified; task review approved after Session-delete atomicity, structured-usage validation, ContextView invariants, and provider traceback-local isolation re-reviews. TDD and packaging evidence is recorded in `.superpowers/sdd/M01-T007-report.md`.
- M01-T008: implementation `24c19c7`, fixes `a5b5c3d` and `7fdd03d`; Python 3.13 `pytest` 306 passed, Ruff passed, mypy passed; task review approved after Workflow IR validation, recovery ownership, event final-drain boundedness, and child failure-isolation re-reviews. TDD evidence is recorded in `.superpowers/sdd/M01-T008-report.md`.
- M01-T009: implementation `209cc92`, fixes `5237f72`, `9513226`, `0891658`, and `0d1ca6c`; Python 3.13 `pytest` 442 passed, Ruff passed, mypy passed; task review approved after bounded cursor/page validation, evidence-event lifetime preconditions, extension traceback isolation, execution-tree temporal/ownership integrity, and analytics schema/deletion re-reviews. TDD evidence is recorded in `.superpowers/sdd/M01-T009-report.md`.
- M01-T010: design/plan `77878e2` and `e5fcfcb`; implementation `f47f13f`, `5d84725`, `8188546`, `025eaf2`, fix `97bc98a`, and E2E `346693c`; Python 3.13 `pytest` 458 passed, independent focused gate 46 passed, Ruff passed, mypy passed, wheel/sdist build and CLI help passed; task review approved with Critical, Important, and Minor all 0. TDD evidence is recorded in `.superpowers/sdd/M01-T010-report.md`.
