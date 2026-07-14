# M02-T002 Phase 3A Implementation Report

## Status

DONE. Phase 3A adds one atomic, generation-fenced Run-progress transaction to
the Store boundary, with Memory/SQLite parity, exact ambiguous-commit replay,
and exact lazy SQLite forwarding. Existing `StateStore.commit` behavior and the
SQLite v3 schema are unchanged.

No RunEngine, RunAPI, Run status, RecoveryAPI, provider/Tool/MCP, Workflow,
roadmap, milestone, or task-index behavior was changed.

## Interface implemented

- `src/agent_sdk/storage/base.py`
  - `ExternalOperationWrite`
  - `RunCheckpointWrite`
  - `RunProgressBatch`
  - `StateStore.commit_run_progress`
- `InMemoryStore.commit_run_progress`
- `SQLiteStore.commit_run_progress`
- `_LazySQLiteStore.commit_run_progress`

The public batch fields and method signature exactly match the Phase 3A brief.

## Behavior implemented

- A nonempty batch may atomically append events, advance snapshots, create or
  transition one external operation, and create or advance one Run checkpoint.
- First application requires the exact active, non-released, unexpired Run lease
  owner/generation at the timezone-aware batch time.
- The authoritative durable Run snapshot owns the batch Run and Session. Run,
  Session, recovery-record, operation-kind, lease-generation, checkpoint-version,
  event-sequence, and snapshot-version mismatches fail closed.
- Operation transitions reuse the Phase 2 started-to-terminal immutable-field CAS
  contract. Checkpoints reuse its full-record adjacent-version CAS contract.
- In-flight checkpoints resolve their operation from the same batch target first,
  or from an already durable exact operation, and validate kind/Run/Session/
  generation.
- Memory validates against copied collections under one lock and publishes event,
  snapshot, operation, checkpoint, and cursor state only after all checks pass.
- SQLite uses one `BEGIN IMMEDIATE`, performs replay/scope/lease/precondition/
  sequence/version/CAS checks before writes, applies every component directly in
  that transaction, and commits once. It never calls a public method that would
  start a nested transaction.
- Exact all-target replay is detected before the active-lease requirement. It
  validates the invocation's create/update shape and internal target uniqueness,
  returns the current cursor with `applied=False`, and writes nothing after lease
  release or expiry.
- A differing target or nonempty strict subset of exact durable targets is a
  constant conflict; no remainder is completed.
- Non-JSON targets, empty batches, naive times, illegal replay shapes, duplicate
  ids/sequences, stale fences, and failed preconditions are constant sanitized
  `RecoveryStateConflictError` failures.
- The lazy SQLite facade forwards the identical batch object and sanitizes its own
  conflict traceback frame.
- Session deletion removes recovery state created by the composite batch.

No external I/O occurs in either Store transaction.

## Strict TDD RED/GREEN evidence

All valid commands used explicit uv with Python 3.13:

```text
C:\Users\10176\AppData\Local\Programs\Python\Python314\python.exe \
  -m uv run --python 3.13 ...
```

An initial bare `uv` invocation failed because `uv.exe` was not on PowerShell's
`PATH`; it was an environment error and is not counted as a behavior RED. The uv
module above was then used for every recorded test and gate. One early test fixture
also used an invalid RUNNING Run version; it was corrected before counting the
behavior GREEN.

1. Public batch types and Memory atomic model-start
   - RED: `1 failed`; `ExternalOperationWrite` was absent.
   - GREEN: `1 passed`.
   - The GREEN asserts one cursor/event plus exact started operation and in-flight
     checkpoint from the same call.
2. Memory update and snapshot application
   - RED: `3 failed, 1 passed`; operation/checkpoint update branches were create-only
     and snapshots were not published.
   - GREEN: `4 passed`.
   - Covered model outcome, event/snapshot-only fencing, and terminal Run + Session
     events/snapshots + terminal checkpoint.
3. Exact replay ordering and fencing
   - RED: `1 failed, 10 passed`; exact replay checked the released lease first.
   - GREEN: `11 passed`.
   - Exact replay returned `applied=False` with no duplicate event or changed
     snapshot/operation/checkpoint; partial and illegal-shape replays conflicted.
4. Preconditions and empty batch
   - RED: `3 failed, 11 deselected`; event/snapshot preconditions and empty targets
     were not checked.
   - GREEN: focused Memory batch reached `14 passed`.
5. Snapshot data identity
   - RED: `1 failed, 31 passed`; a Run snapshot wrapper could contain a foreign
     `data.run_id`.
   - GREEN: `32 passed` after strict target Run/Session model identity checks.
6. SQLite composite surface
   - RED: representative SQLite model-start `1 failed` with missing
     `commit_run_progress`.
   - GREEN: representative `1 passed`; the implementation used a single direct
     transaction with no nested public calls.
7. Shared Memory/SQLite contract
   - GREEN: `64 passed` after parameterizing outcome, terminal, fencing, replay,
     partial-target, CAS/preconditions, mismatch, cleanup, race, and traceback cases.
8. Lazy SQLite forwarding
   - RED: `1 failed`; `_LazySQLiteStore.commit_run_progress` was absent.
   - GREEN: `1 passed`, proving result identity and the exact same batch object.
9. Fault/cancellation and ambiguous commit
   - GREEN: `5 passed, 65 deselected` for Memory cancel-before-publish, SQLite
     pre-commit fault/cancellation rollback, cancellation racing a completed commit
     followed by exact replay, and lazy conflict sanitization.
10. Exact replay cross-Run ownership
    - RED: `1 failed, 1 passed, 70 deselected`; Memory accepted an exact operation
      belonging to another Run while SQLite rejected it.
    - GREEN: `2 passed, 70 deselected` after moving Memory target ownership,
      generation, and checkpoint-operation checks before replay classification.
11. Non-JSON target parity
    - RED: `2 failed, 2 passed, 72 deselected`; Memory accepted non-finite event and
      snapshot targets that SQLite rejected.
    - Intermediate: `1 failed, 3 passed`; raw event payload still needed validation.
    - GREEN: `4 passed, 72 deselected` with canonical target validation.
12. Illegal duplicate exact-replay invocation
    - RED: `2 failed, 82 deselected`; both Stores treated a duplicated exact event
      target as a valid replay.
    - GREEN: `2 passed, 82 deselected` after pre-replay internal id/sequence/version
      validation.

## Review remediation

The first independent review returned `C0/I2/M0`, not approved. Both Important
findings were repaired under strict TDD without changing the schema or expanding
Phase 3A scope.

1. Multiple snapshot targets with one identity
   - Finding: a batch could contain the same `(kind, entity_id)` twice when the
     versions increased. First application published only the last snapshot, so
     the original multi-target invocation could never be exactly replayed.
   - RED: `2 failed, 4 passed, 84 deselected`; active Memory and SQLite batches
     accepted Run snapshot versions 3 then 4 for the same identity.
   - GREEN: `6 passed, 84 deselected`.
   - Fix: both pre-replay internal validators now treat the second occurrence of
     any snapshot identity as an illegal invocation, before replay, lease checks,
     or mutation. Shared tests cover active commit, released invocation, durable
     state, replay rejection, and zero mutation.
2. Unified signed-64-bit batch integers
   - Finding: oversized integers were accepted by Memory, leaked SQLite
     `OverflowError` at binding, or bypassed validation on exact replay.
   - RED: `23 failed, 4 passed, 90 deselected`. The failures included Memory
     publication, raw SQLite/lazy overflow, and accepted oversized replay lease or
     precondition values.
   - GREEN: `27 passed, 90 deselected`.
   - Fix: one pure storage-base validator enforces exact Python `int` values in
     the inclusive signed-64-bit range for lease generation, event schema version
     and sequence, snapshot version, snapshot-precondition version,
     event-precondition cursor and sequence, operation expected/updated turn and
     lease generation, and checkpoint expected/updated version and turn. Memory
     and SQLite call it before replay, lease validation, and SQLite transaction
     creation; lazy SQLite inherits the same decision and sanitizes its own frame.
   - Every parameter asserts constant `RecoveryStateConflictError`, empty cause
     and context, nonempty SDK traceback frames without the secret, and exact zero
     mutation of cursor, events, snapshots, operations, and checkpoints.

Fresh final Phase 3A focused result: `117 passed in 6.58s`.

## Required gates on final code

- Phase 3A focused:
  `117 passed in 6.58s`.
- Phase 2 focused (`test_reconciliation_models.py`, `test_recovery_records.py`,
  `test_sqlite_recovery_validation.py`):
  `136 passed in 8.55s`.
- Phase 1 + M02-T001 regression (`test_leases.py`,
  `test_sqlite_v3_migration.py`, `test_idempotency_store_contract.py`,
  `test_execution_descriptors.py`, `test_sqlite_spine.py`):
  `188 passed in 14.15s`.
- Final full Python 3.13 pytest:
  `1027 passed in 34.69s`.
- Ruff (`ruff check src tests`):
  `All checks passed!`.
- mypy (`mypy src/agent_sdk`):
  `Success: no issues found in 72 source files`.
- `git diff --check`: exit 0; only Windows LF-to-CRLF informational warnings.

The first correct mypy source invocation found 33 strict local-narrowing errors
from reusing branch-local names such as `target` and `current`. A name-only
refactor produced the final clean mypy result; focused and full tests were rerun
afterward. A no-argument mypy attempt treated the local package as an installed
untyped distribution and was discarded as an invalid gate command.

## Scope and schema checks

- Production changes are limited to `storage/base.py`, `storage/memory.py`,
  `storage/sqlite.py`, and the lazy SQLite facade in `api.py`.
- Tests are isolated in `tests/integration/storage/test_run_progress.py`.
- This report is the only documentation change.
- No migration or schema definition changed; schema version remains 3.
- `StateStore.commit` and its existing callers were not changed.
- Forbidden RunEngine, RunAPI/status, RecoveryAPI, Workflow, roadmap, milestone,
  and task-index paths have an empty diff.

## Self-review

- Replay validation now precedes lease validation but never mutation validation:
  exact all-target replay is read-only, while every first application still
  requires the current active lease.
- All Memory containers are copied before publication. SQLite performs every
  check in the one writer transaction and uses the existing cancellation-safe
  commit/rollback helpers.
- Every public conflict path reconstructs the constant error after discarding the
  original batch arguments/traceback; Memory, SQLite, and lazy tests require
  nonempty SDK traceback frame sets with no retained secret.
- No schema expansion was needed. No external work was moved into storage.
- Backend-specific query/application code remains separate because Memory and
  SQLite have different atomic publication mechanisms; shared tests enforce
  behavioral parity.
- No known Critical, Important, or in-scope correctness concern remains.
