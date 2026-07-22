# M02-T003 Phase A Review-Fix Brief

## Context

Independent review of `065d4ef` returned REQUEST CHANGES:

- Spec: C0 / I4 / M0
- Quality: C0 / I1 / M0

This repair stays inside Phase A. Phase B and M02-T004 remain blocked. Use
strict TDD for every item, preserve the already-green migration/storage/full
suite behavior, write a repair report, and obtain a fresh independent re-review
with Spec C0/I0 and Quality C0/I0 before proceeding.

## I1 — Loop-neutral, thread-safe per-database coordinator

### Reproduction

The global `WeakValueDictionary[str, asyncio.Lock]` shares an event-loop-bound
lock across two threads/loops. One loop releasing another loop's waiter raises
`RuntimeError` and can leave the waiter hung. The weak-map lookup/create is also
not thread-safe.

### Required design

- Replace `asyncio.Lock` with a loop-neutral coordinator backed by a
  thread-safe per-identity primitive and a thread-safe registry.
- Waiting for a coordinator must never block an event-loop thread. If a
  blocking acquire is delegated to a worker thread, cancellation must settle
  the acquire and release it if it completed; it must never leak ownership.
- Active holders and waiters must keep one coordinator alive. An idle identity
  must remain reclaimable without permitting active waiters to split across two
  coordinators.
- Same canonical database identity serializes `plan`, `applied`, `apply`, and
  `SQLiteStore.open` across threads and event loops. Distinct databases still
  progress independently. SQLite `BEGIN IMMEDIATE` remains the cross-process
  authority.

### Required RED/GREEN tests

- Two real threads, each with its own debug event loop, concurrently execute
  same-database `plan/apply/open`; both finish within a strict timeout with no
  cross-loop exception or live thread and schema 4 is applied once.
- Instrumented critical sections prove same-database operations do not overlap.
- Distinct-database operations overlap/progress independently.
- Cancellation while waiting does not leak the coordinator; a later operation
  acquires it.
- Idle identity collection and active-waiter non-splitting remain covered.

## I2 — One explicit read-only transaction snapshot per inspection

### Reproduction

`plan()`/`applied()` issue multiple schema queries without `BEGIN`. A
cross-process v3-to-v4 apply between the migration-column read and later schema
reads produces a mixed generation and `MigrationSchemaError`.

### Required design

- Wrap every `_inspect_applied` operation in one explicit read-only transaction.
- Establish the SQLite snapshot on the first read and keep it through all table,
  column, index, DDL, projection, recovery-row, and migration-history queries.
- Commit/rollback/close is cancellation safe. No read-only inspection creates or
  repairs the database or mutates DB/WAL/SHM bytes.
- Connection-local inspection used while a writer transaction is already held
  must not start a nested transaction; give the two modes explicit APIs.

### Required RED/GREEN tests

- In WAL mode, pause `plan()` after its first generation read, apply v4 from a
  separate process, then continue. The result is only old `(4,)` or new `()`;
  mixed state/error is forbidden.
- Repeat for `applied()` and cancellation at multiple read stages.
- Empty/v1/v2/v3/v4 database and sidecar bytes remain unchanged by plan/applied.

## I3 — SQL normalization must preserve quoted-token semantics

### Reproduction

`_normalized_sql()` currently case-folds and removes whitespace inside string
literals. Changing Artifact state `'ready'` to `'READY'` is accepted as exact
schema. The same helper weakens v1-v3 validation.

### Required design

- Implement a small deterministic SQLite SQL lexical normalizer (or equivalent
  token comparison) that ignores only semantically insignificant whitespace and
  case outside quoted tokens.
- Preserve the exact content and escaping of single-quoted strings, double-
  quoted identifiers, backtick identifiers, and bracket identifiers.
- Reject malformed/unclosed quoted input fail-closed. Do not introduce a general
  SQL parser dependency.
- Use the corrected comparison consistently for v1-v4 table and index shapes.

### Required RED/GREEN tests

- v4 reopen/plan rejects changed literal case and changed whitespace inside a
  literal without any repair/write.
- v3 bootstrap rejects equivalent tampering in a legacy table/index literal.
- Legitimate unquoted keyword case and external formatting differences accepted
  by SQLite remain compatible where they are semantically identical.

## I4 — One cancellation-safe transaction per pending migration

### Reproduction

`SQLiteStore._migrate()` applies versions 1-3 under one `BEGIN IMMEDIATE`; a
failure in migration 3 removes completed versions 1 and 2. Migration 4 begins
outside its settlement `try`, so cancellation after SQLite acquires the writer
transaction but before the await returns performs no explicit rollback.

### Required design

- Refactor legacy application so migration 1, migration 2 plus its Python
  transform, migration 3, and migration 4 each own one explicit transaction.
- At the start of every transaction, re-discover and validate the exact current
  state under `BEGIN IMMEDIATE`; cross-process competitors converge.
- Settlement covers cancellation/error during BEGIN acquisition, every
  statement/transform/version-row/final-validation checkpoint, and commit.
- Use a shield-and-settle pattern for BEGIN/commit/rollback. After any result,
  the connection has no transaction or writer lock.
- A failure/cancellation rolls back only the current migration. Previously
  committed migration generations remain exact and usable.
- Commit-race cancellation may expose either the complete prior generation or
  complete new generation, never partial state.

### Required RED/GREEN tests

- Empty database: failure/cancellation in v2 leaves exact v1; in v3 leaves exact
  v2. v1 leaves exact empty. v4 leaves exact v3.
- Existing v1/v2/v3 fixtures have the analogous current-migration rollback.
- Parameterize cancellation across every v4 fault checkpoint, not only one.
- Add a controlled BEGIN-completed/before-return cancellation test and assert an
  explicit rollback call plus no lock leak.
- Add a controlled commit-completed/before-return cancellation test and assert
  the database is exactly old/new, reopens, and accepts a new writer.
- Same-process and cross-process concurrent applies still converge.

## Q1 — Stable, sanitized public filesystem/SQLite error boundary

### Reproduction

When the database parent is a file, `MigrationRunner.apply()` and
`SQLiteStore.open()` leak raw `FileExistsError` text containing the absolute
path.

### Required design

- Add a stable dedicated migration I/O/open error type or an equally explicit
  existing migration error subtype.
- Map public path resolution/stat/mkdir/connect/configure/WAL/open failures from
  `MigrationRunner.open/plan/applied/apply` and `SQLiteStore.open` to stable safe
  messages. Preserve `CancelledError` and existing migration checksum/schema/
  resource errors unchanged.
- Do not include the supplied database path, OS error text, credentials, or
  environment-specific details in `str(error)`. Internal exception chaining may
  remain available only if it does not alter serialized/public text.
- Packaged-resource read failures map separately to a stable resource error.

### Required RED/GREEN tests

- Parent-is-file, denied parent/file, invalid/corrupt SQLite open, WAL/configure
  failure, stat failure, and packaged-resource read failure.
- Assert exact stable type/message and that the absolute path/OS message is not
  present.
- Cancellation passes through as `CancelledError`.

## Regression and completion gates

Before committing:

1. Focused migration/review-fix suite.
2. Complete `tests/integration/storage` suite.
3. Complete project suite on Python 3.13.
4. Ruff on `src tests examples`.
5. Strict mypy on all source files and focused migration/sqlite files.
6. `py_compile`, `git diff --check`, public import/package resource audit, and
   M02-T003 Phase A/Phase B/M02-T004 scope audit.
7. Write `.superpowers/sdd/M02-T003-phaseA-review-fix-report.md` with exact
   RED/GREEN evidence and commit the coherent repair.

Do not edit task checkboxes/index yet. Do not implement Artifact filesystem
publish/read/delete or cleanup behavior.
