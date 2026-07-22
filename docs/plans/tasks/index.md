# Task Index

> **Active v0.1 track:** Execution follows the [v0.1 implementation plan index](../../superpowers/plans/2026-07-17-agent-sdk-v0.1-implementation-index.md). Existing milestones remain hardening backlog unless referenced by a release slice.

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
| [M02-T001](M02-T001-session-idempotency.md) | Complete Session lifecycle and idempotency | M02 | M01 | done |
| [M02-T002](M02-T002-leases-reconciliation.md) | Leases, interruption, and reconciliation | M02 | T001 | done |
| [M02-T003](M02-T003-artifacts-migrations.md) | Artifact lifecycle and migrations | M02 | T001 | in_progress |
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

- M01-T001: implementation `d8c1463`; Python 3.13 `pytest` 1 passed, Ruff passed, mypy passed; task review approved. TDD evidence is recorded in `.superpowers/sdd/M01-T001-report.md`.
- M01-T002: implementation `d0aa2c8`, fixes `483a533`; Python 3.13 `pytest` 16 passed, Ruff passed, mypy passed; task review approved after re-review. TDD evidence is recorded in `.superpowers/sdd/M01-T002-report.md`.
- M01-T003: implementation `f715d3f`, fixes `369847d` and `7bd5839`; Python 3.13 `pytest` 47 passed, Ruff passed, mypy passed; task review approved after cancellation-safety re-review. TDD and packaging evidence is recorded in `.superpowers/sdd/M01-T003-report.md`.
- M01-T004: implementation `d26a593`, fixes `93f2c99` and `cf0d337`; Python 3.13 `pytest` 80 passed, Ruff passed, mypy passed; task review approved after lifecycle, termination, immutability, and public-error re-reviews. TDD evidence is recorded in `.superpowers/sdd/M01-T004-report.md`.
- M01-T005: implementation `48c8be5`, fixes `ca988a8` and `05c3701`; Python 3.13 `pytest` 110 passed, Ruff passed, mypy passed; task review approved after permission-failure, cancellation, boundedness, and concurrent-resolution re-reviews. TDD evidence is recorded in `.superpowers/sdd/M01-T005-report.md`.
- M01-T006: implementation `4c12c09`, fixes `19d6552` and `c905c2f`; Python 3.13 `pytest` 186 passed, Ruff passed, mypy passed; task review approved after MCP task-affine lifecycle, cancellation/readiness settlement, YAML complexity, and Skill path-identity re-reviews. TDD evidence is recorded in `.superpowers/sdd/M01-T006-report.md`.
- M01-T007: implementation `d81320a`, fixes `2e3559a` and `b0fa718`; Python 3.13 `pytest` 246 passed, Ruff passed, mypy passed, wheel/sdist resources verified; task review approved after Session-delete atomicity, structured-usage validation, ContextView invariants, and provider traceback-local isolation re-reviews. TDD and packaging evidence is recorded in `.superpowers/sdd/M01-T007-report.md`.
- M01-T008: implementation `25fb210`, fixes `90ca640` and `c8256f8`; Python 3.13 `pytest` 306 passed, Ruff passed, mypy passed; task review approved after Workflow IR validation, recovery ownership, event final-drain boundedness, and child failure-isolation re-reviews. TDD evidence is recorded in `.superpowers/sdd/M01-T008-report.md`.
- M01-T009: implementation `7aabbcd`, fixes `67ba421`, `8c81b87`, `afdae44`, and `ace12f7`; Python 3.13 `pytest` 442 passed, Ruff passed, mypy passed; task review approved after bounded cursor/page validation, evidence-event lifetime preconditions, extension traceback isolation, execution-tree temporal/ownership integrity, and analytics schema/deletion re-reviews. TDD evidence is recorded in `.superpowers/sdd/M01-T009-report.md`.
- M01-T010: design/plan `7c12c7d` and `75e2e3e`; implementation `cd28918`, `0ba22be`, `6755260`, `b62ef0c`, fix `62e5d29`, and E2E `74b7c5f`; Python 3.13 `pytest` 458 passed, independent focused gate 46 passed, Ruff passed, mypy passed, wheel/sdist build and CLI help passed; task review approved with Critical, Important, and Minor all 0. TDD evidence is recorded in `.superpowers/sdd/M01-T010-report.md`.
- M02-T001: design/plan `52cca6c`, `8f8d337`, `eb1697e`, `fac67d7`, `afbe0f9`, `8ef9d2c`, and `f89fcb8`; implementation/fixes `ea447be`, `4148b77`, `72123a1`, `32f4acc`, `a4b88a3`, `7048ee9`, `adde148`, `c2b2365`, `03ef62d`, `adbc0c2`, `dc76ea8`, and `a7459ea`; Python 3.13.14 and 3.12.13 focused gates each passed 152 tests and full suites each passed 705 tests, Ruff passed, mypy passed across 70 source files, wheel/sdist build and CLI help passed, and diff-check passed; Tasks 1-5 and final Task 5 re-review approved with Critical, Important, and Minor all 0. TDD evidence is recorded in `.superpowers/sdd/M02-T001-report.md` and the Task 1-5 reports.
