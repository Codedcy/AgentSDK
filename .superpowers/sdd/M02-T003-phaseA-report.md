# M02-T003 Phase A Implementation Report

## Scope

Implemented only Phase A from `M02-T003-phase-plan.md`:

- trusted migration identities and release manifest for schema versions 1-4;
- checksum-bearing schema version 4 and Artifact metadata DDL;
- read-only migration planning/applied inspection and atomic apply;
- canonical per-database coordination plus SQLite cross-process writer-lock
  convergence;
- default `SQLiteStore.open()` upgrade/recovery to schema 4;
- exact opened-schema generation fencing for every SQLite business mutation.

No Artifact file publish/read, cleanup worker, Session-owned Artifact deletion,
or M02-T004 behavior was implemented.

## TDD evidence

The implementation was developed through explicit RED/GREEN slices.

1. Importing the focused migration tests failed because `MigrationRunner` and
   its contracts did not exist.
2. Empty apply and changed transform-identity tests failed because apply was
   absent and migration 2 did not yet include the Python transform identity.
3. Exact-schema tests failed when changed migration-table SQL and an extra
   index were accepted.
4. The stale-writer matrix failed in all 15 initial cases because the default
   Store still opened schema 3, commit/Session deletion bypassed the shared
   begin helper, and a true schema-3 connection could not be captured.
5. The malformed-resource manifest case failed because a non-numbered `.sql`
   resource was ignored.
6. The coordinator lifecycle test failed because the original strong global
   registry retained an idle database identity indefinitely.

Each RED was followed by the smallest corresponding implementation and a
focused GREEN run. The complete schema-4 fault matrix covers 32 checkpoints;
every injected failure leaves the exact logical schema-3 state.

## Design decisions

### Migration package and identity

The existing SQL resource directory is now a Python package at
`agent_sdk.storage.migrations`; this avoids a module/directory name collision
while keeping packaged SQL at its established location.

The release-owned manifest pins filenames and SHA-256 identities for versions
1-4. Versions 1, 3, and 4 hash exact SQL bytes. Version 2 hashes exact SQL bytes
plus a NUL-delimited `session-ownership-v1-to-v2` identity input. Resource
enumeration rejects missing, future, duplicate, malformed, changed, or otherwise
untrusted `.sql` files before database mutation.

### Planning, bootstrap, and validation

`MigrationRunner.open()` canonicalizes the database path without touching the
database. `plan()` and `applied()` use read-only connections and do not create or
repair files. `apply()` is the only mutating runner entry.

The established release-owned schema/projection/recovery validators are reused
for v1-v3. Migration 4 obtains `BEGIN IMMEDIATE`, rediscovers and validates the
exact state under that lock, creates the checksum table and Artifact metadata,
copies original `applied_at` values with trusted checksums, inserts version 4,
validates the complete schema and rows, and commits with cancellation-safe
settlement. SQL is parsed into complete statements; `executescript` is not used.

The Artifact generation DDL represents all required lifecycle states:
`publishing`, `ready`, `delete_pending`, and `deleting`, with claim-shape checks.
Cleanup jobs are anonymous and contain no Session id.

### Coordination and open behavior

Canonical database identities map to `asyncio.Lock` instances held in a
`WeakValueDictionary`. Active holders and waiters retain one shared lock, while
an idle identity is reclaimable, preventing a path-cardinality memory leak.
Distinct database identities progress independently.

The in-process coordinator serializes plan/apply/open discovery. SQLite's
`BEGIN IMMEDIATE` remains the cross-process authority. Migration 4 rediscovers
after acquiring that lock; a competitor that then observes valid schema 4
validates and converges rather than replaying bootstrap.

`SQLiteStore.open()` retains the configured connection used by migration,
preserving busy-retry behavior and avoiding a close/reopen race. Exact schema-4
reopen validates without replaying legacy migration. Exact v1/v2/v3 fixtures
still upgrade automatically.

### Generation fence

Every one of the 12 SQLite business mutation families reaches the shared
`_begin_immediate()` helper. The helper acquires the writer transaction, reads
the exact ordered `(version, checksum)` generation inside it, and compares it
with the generation captured when the Store opened. Any mismatch raises
`SchemaGenerationChangedError` before mutation. The caller's existing rollback
path settles the transaction; tests prove no transaction/write lock remains and
a fresh schema-4 Store can write while the fenced stale Store is still open.

The stale test opens a real validated schema-3 connection through production
code before a separate runner applies version 4; it does not tamper with private
generation fields.

## Verification

Baseline before Phase A changes:

```text
uv run pytest tests/integration/storage -q
449 passed in 21.46s
```

Final verification from the coherent working tree:

```text
uv run python -m py_compile \
  src/agent_sdk/storage/sqlite.py \
  src/agent_sdk/storage/migrations/__init__.py \
  tests/integration/storage/test_migrations.py \
  tests/integration/storage/test_sqlite_spine.py \
  tests/integration/storage/test_sqlite_v3_migration.py
exit 0

uv run pytest tests/integration/storage/test_migrations.py -q
80 passed in 17.78s

uv run pytest tests/integration/storage -q
529 passed in 48.12s

uv run ruff check src tests examples
All checks passed!

uv run mypy src
Success: no issues found in 76 source files
```

An additional strict focused type run also passed:

```text
uv run mypy --strict src/agent_sdk/storage/migrations src/agent_sdk/storage/sqlite.py
Success: no issues found in 2 source files
```

## Review status

Implementation and local verification are complete for the Phase A slice.
Independent Phase A Spec and Quality review remains the next gate. Phase B must
not begin until that review reaches C0/I0.
