# M02-T002 Phase 3 Release Report

## Outcome

RELEASE CANDIDATE VERIFIED; PENDING WHOLE-PHASE INDEPENDENT REVIEW. Phase 3 now
provides durable Run progress transactions, live execution persistence,
conservative recovery planning, fenced coordination, application-certified
Provider recovery, and application-certified Tool retry. Every Phase 3 slice
has its own independent C0/I0 approval, and the final tree passed the dual-
Python release gates below.

The release candidate is `9cd44b902d8360288ed6e6c6f4dff20d932da962`,
reviewed against Phase 3 base `2309dfb`. Workflow recovery and reconciliation
resolution remain Phase 4/later work.

## Delivered phases

- Phase 3A: atomic Run progress transactions, event sequencing, checkpoint and
  durable-operation invariants, Memory/SQLite parity, and fenced CAS behavior.
- Phase 3B: live Run execution writes Model/Tool progress, operations,
  checkpoints, results, usage, and bounded events atomically.
- Phase 3C1: conservative scanner/admission reconstructs exact Run ownership,
  descriptor, checkpoint, operation, and event evidence.
- Phase 3C2: public recovery coordination, leases, heartbeat, cancellation,
  Session lifecycle, concurrent callers, and bounded reconciliation.
- Phase 3D1: application-certified Provider status/query and same-operation-id
  recovery adapters without adding a second normal model gateway; ordinary
  model calls remain LiteLLM-only.
- Phase 3D2: strict `ToolRetryPolicy`, default-hash-compatible Tool contracts,
  certified same-operation Tool retry, permission re-evaluation, exact ordered
  lifecycle replay, authoritative Tool-result reconstruction, registry race
  fences, bounded observability, and fail-closed reconciliation.

## Final behavior and trust boundaries

- Default, legacy, unstamped, changed, missing, malformed, or inconsistent
  evidence performs no certified Provider/Tool external work and creates one
  bounded durable reconciliation request.
- Provider and Tool external work is admitted twice: during planning and again
  under the coordinator lease immediately before execution.
- Full Run event envelopes and the closed ordered lifecycle grammar authenticate
  creation, model, tool, permission, interrupt, recovery, and terminal history.
  Every certified event is consumed in one reachable state and crossed against
  descriptor, operation, checkpoint, message, result, and event evidence.
- Historical permissions are evaluated through the production `PolicyEngine`
  using the recorded execution descriptor; recovery never calls the application
  permission bridge while authenticating history.
- Tool retry uses the same durable operation id, re-evaluates current permission,
  revalidates the exact registered Tool after the last lease await, and preserves
  normal ToolExecutor result semantics.
- Application certification remains a trust boundary: the SDK authenticates the
  recorded certification and evidence but cannot prove a business side effect
  is truly idempotent or safe.
- Durable Model outcomes and delta events authenticate exact joined output. The
  original provider stream chunk partition is not persisted and is not invented
  during recovery.

## Fresh Phase 3 focused gates — Python 3.13

All commands used the explicit workspace `uv` executable and disabled pytest's
cache provider.

- Public contracts and policy: 32 passed in 5.04s.
- Provider, Tool, RecoveryAPI fault/E2E/recovery: 314 passed in 80.90s.
- Scanner and reconciliation: 115 passed in 6.64s.
- Live progress: 40 passed in 3.54s.
- Store progress and recovery records: 206 passed in 10.08s.
- Compatibility files changed by Phase 3: 149 passed in 8.53s.
- All 17 test files changed by `2309dfb..HEAD`: 856 passed, zero failed,
  zero skipped.
- Existing `tests/e2e`: 3 passed in 4.60s. Fault cases are embedded in the
  recovery, live, and Store suites; the repository has no `tests/faults` folder.

## Dual-Python full gates

- Python 3.12.13, isolated and frozen: 1537 passed in 124.49s, zero failed,
  zero skipped.
- Python 3.13.14, isolated and frozen: 1537 passed in 118.36s, zero failed,
  zero skipped.

## Static, build, import, scope, and schema gates

- Ruff passed across `src` and `tests`.
- Mypy passed across 75 source files.
- A source distribution and wheel were built in a temporary directory outside
  the worktree. Isolated wheel installs and import smoke passed on Python 3.12
  and Python 3.13.
- The installed distribution was `agent-sdk==0.1.0.dev0`; root Phase 3 imports
  passed 9/9, root `__all__` contained 99 unique available names, and Provider
  recovery contracts imported successfully.
- `ToolRetryPolicy` values were exactly `never`, `idempotent`, and `safe_retry`.
  Default Tool canonical JSON omitted `retry_policy`, and its established hash
  remained `2a6f67bbdf395f62fe0d6ecd1770dc6a3f3fe79e16efc8cfc61783578d78fb14`.
- `git diff --check 2309dfb..HEAD` passed.
- Workflow production, roadmap/milestone/task-index, storage migration, and
  SQLite DDL/schema-version diffs were empty. SQLite schema remains version 3.
- Temporary build artifacts were removed; final staged, unstaged, and untracked
  status was clean at the verified HEAD.

One non-code command attempt used an unsupported `uv build --frozen` option and
stopped during CLI parsing before any build or filesystem change. The supported
external-output build command then completed successfully. A redundant
`--no-project` warning in the Python 3.12 wheel smoke had no functional effect.

## Release decision

This report records verified release evidence and does not self-approve Phase 3.
A fresh independent whole-Phase-3 Spec and Quality review over
`2309dfb..HEAD` must return C0/I0 before Phase 3 is marked complete and Phase 4
Workflow recovery begins.
