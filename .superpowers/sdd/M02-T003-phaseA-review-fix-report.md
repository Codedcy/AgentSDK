# M02-T003 Phase A Review Fix Report

Date: 2026-07-17
Branch: `feature/agent-sdk-implementation`
Baseline: `ac7f0ca` (`feat(storage): add checksummed schema migration fence`)
Implementation: `db7cd77` (`fix(storage): harden phase A migration boundaries`)

## Scope

This repair resolves the Phase A review findings I1-I4 and Quality I1. It does
not implement Artifact filesystem publish/read/delete, cleanup behavior, Phase
B, or M02-T004. Task checkboxes and indexes were not changed.

## Implemented repairs

### I1 — loop-neutral migration coordination

- Replaced the per-database `asyncio.Lock` registry with a weak, thread-safe
  registry of coordinators backed by `threading.Lock`.
- Blocking acquisition runs outside the event-loop thread.
- Cancellation settles an in-flight acquire and releases ownership if the
  acquire won the race. Idle identities remain collectable and active
  holders/waiters cannot split across coordinators.
- Added real-thread, independent-debug-loop coverage for same-database
  serialization, distinct-database overlap, cancellation, collection, and
  waiter non-splitting.

### I2 — one explicit read snapshot per inspection

- Split managed inspection from transaction-local inspection into explicit
  APIs.
- `plan()` and `applied()` now hold one read transaction from the first schema
  read through all DDL, projection, recovery-row, and history validation.
- BEGIN/commit/rollback/close settlement preserves cancellation and leaves no
  transaction behind.
- Added WAL old-or-new snapshot tests for both public inspection APIs and
  cancellation tests at multiple read stages.

### I3 — lexical SQL normalization

- Replaced whitespace deletion with a deterministic token lexer.
- Unquoted words are case-folded while token boundaries, operators, numbers,
  quoted identifiers, quoted literals, and doubled quote escapes are
  preserved.
- Unterminated quoting fails closed. All schema-shape comparison sites use the
  guarded equality helper.
- Added v3/v4 semantic-tamper tests and positive formatting compatibility
  coverage.

### I4 — one cancellation-safe transaction per migration

- Migration 1, migration 2 plus its Python projection transform, migration 3,
  and migration 4 now commit in separate `BEGIN IMMEDIATE` transactions.
- Every transaction re-discovers and validates the exact current generation
  while holding the writer lock, so same-process and cross-process competitors
  converge.
- BEGIN, statements, transform, version insertion, final validation, commit,
  and rollback use settlement that covers cancellation races.
- Preserved legacy checkpoints and added before/after checkpoints. A failed or
  cancelled migration preserves the exact previously committed generation.
- Added every-v4-checkpoint cancellation coverage and controlled
  BEGIN-completed/commit-completed-before-cancellation race tests with reopen
  and writer-lock verification.

### Quality I1 — sanitized public I/O boundary

- Added public `MigrationIOError` with exact message
  `migration database I/O failed` for path, stat, mkdir, connect, configure,
  and WAL/open failures.
- Kept corrupt/incompatible database contents under the existing exact
  `MigrationSchemaError("incompatible database schema")` contract.
- Packaged migration read failures now use
  `MigrationResourceError("packaged migration resource is unavailable")`.
- Public error text excludes the supplied path, OS message, and credentials;
  internal exception chaining remains available. `CancelledError` passes
  through unchanged.

## TDD evidence

### RED

- I1: the former `asyncio.Lock` left the second real thread/event loop alive
  past the test deadline for one database identity.
- I2: with a real non-empty WAL, pausing between schema reads and applying v4
  produced mixed-generation `MigrationSchemaError`; cancellation recorded zero
  explicit rollbacks.
- I3: four semantic cases were accepted by the old normalizer (v4 literal
  case, v4 literal whitespace, v3 literal case, and direct token-boundary
  equality); only the positive formatting case passed.
- I4: `test_empty_bootstrap_fault_rolls_back_only_the_current_migration` was
  `3 failed in 3.28s`: v1 had no statement checkpoint and failures in v2/v3
  rolled the database back to empty instead of exact v1/v2.
- Quality I1: the focused public-boundary run was
  `9 failed, 1 passed in 3.35s`; raw path/filesystem/configuration/resource
  exceptions escaped while cancellation already passed through.

### GREEN

- I1 focused coordinator coverage: `5 passed in 5.62s`.
- I2 snapshot/cancellation coverage: `8 passed in 9.56s`.
- I3 lexical/schema-shape coverage: `9 passed in 3.85s`.
- I4 core selection (legacy generation boundaries, all v4 cancellation
  checkpoints, BEGIN and commit races): `43 passed`.
- Quality I1 public-boundary coverage: `10 passed, 54 deselected in 2.83s`.
- Combined migration plus review-fix suite:
  `144 passed in 33.03s`.

## Completion gates

All commands ran from `D:\code\AgentSDK\.worktrees\agent-sdk-implementation`
with `C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe`.

1. `uv run pytest tests/integration/storage/test_migrations.py tests/integration/storage/test_migration_review_fixes.py -q`
   - `144 passed in 33.03s`
2. `uv run pytest tests/integration/storage -q`
   - `593 passed in 73.13s`
3. `uv run python --version; uv run pytest -q`
   - `Python 3.13.14`
   - `2303 passed in 264.97s`
4. `uv run ruff check src tests examples`
   - `All checks passed!`
5. `uv run mypy src`
   - `Success: no issues found in 76 source files`
6. `uv run mypy --strict src/agent_sdk/storage/migrations/__init__.py src/agent_sdk/storage/sqlite.py`
   - `Success: no issues found in 2 source files`
7. `uv run python -m py_compile <all Python files under src tests examples>`
   - exit 0
8. `uv build`
   - source distribution and wheel built successfully
9. Public import and packaged-resource audit
   - `MigrationIOError` and `MigrationRunner` import successfully
   - trusted versions are exactly `(1, 2, 3, 4)`
   - wheel contains all four numbered SQL migrations
10. `git diff --check`
    - exit 0
11. Scope audit
    - changed production code is limited to migration coordination,
      inspection, SQL validation, transaction settlement, and public error
      boundaries
    - no Artifact filesystem behavior, Phase B, or M02-T004 implementation
      was added

Two earlier long-running test invocations were terminated only by undersized
outer command timeouts. Both were rerun with sufficient time; the authoritative
results are the successful storage and full-project runs above.
