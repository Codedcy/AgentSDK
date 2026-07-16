# M02-T003 Phase A Brief — Checksummed Migrations and Schema Fence

## Scope

Implement only Phase A from `M02-T003-phase-plan.md`: generalized ordered
migrations through schema version 4, trusted checksum bootstrap for versions
1-3, per-database open/apply coordination, and fail-closed fencing of every
SQLite business write. Do not implement Artifact filesystem publish/read or
cleanup workers in this phase.

## Required TDD sequence

1. Add focused tests first and run them RED for the intended missing behavior.
2. Implement the smallest coherent migration/fence design.
3. Run focused tests, the complete existing SQLite migration/storage suites,
   Ruff, and strict mypy.
4. Record exact commands/counts and design decisions in
   `.superpowers/sdd/M02-T003-phaseA-report.md`.
5. Commit the coherent implementation. Do not update M02-T003 task checkboxes.

## Required contracts

### Migration identity

- `Migration` exposes at least ordered version, SQL bytes/text, checksum, and
  stable identity inputs. Applied rows expose version/checksum/applied_at.
- A release-owned manifest pins migrations 1-4. Version 2's checksum includes
  the exact packaged `0002_idempotency.sql` bytes plus the stable transform id
  `session-ownership-v1-to-v2`; changing either fails before migration begins.
- Version 1 and 3 identities cover their exact packaged SQL bytes. Version 4
  covers its exact packaged SQL bytes.
- Numbered resources must be contiguous, unique, and match the manifest. Future,
  missing, duplicate, malformed, changed, or untrusted resources fail closed.

### Planning and applying

- `MigrationRunner.open(path)` establishes canonical database identity and a
  configured connection/factory without changing schema.
- `plan()` performs no writes, returns pending ordered migrations with exact
  checksums, and rejects corrupt/incompatible/applied-checksum state.
- `applied()` returns validated ordered applied records without repair.
- `apply()` is the sole mutating entry. Each migration uses an explicit
  cancellation-safe transaction and complete SQL statement parsing without
  `executescript`.
- Empty databases reach schema 4. Exact v1/v2/v3 fixtures reach schema 4. Exact
  schema 4 opens without writes.

### Version-4 bootstrap

- Under one `BEGIN IMMEDIATE`, validate the exact version-3 schema plus v2
  transformed projections and all v3 lease/operation/checkpoint/reconciliation
  row invariants using the release-owned validators already implemented by
  `SQLiteStore` (refactor to shared private code only where necessary).
- Create the checksum-bearing migration table, copy versions 1-3 with trusted
  digests and original `applied_at`, install Artifact metadata tables/indexes,
  insert version 4, validate the complete result, then commit.
- Fault injection before/after every schema statement, each historical row
  copy, Artifact DDL, version-4 insert, and final validation must roll back to
  the byte-equivalent logical v3 schema: two-column migration rows and no
  Artifact tables/checksum residue.

### Coordinator and stale-writer fence

- Opens/applies in one process serialize by canonical database path; distinct
  databases do not block each other. SQLite's writer lock remains the
  cross-process authority, so discovery is repeated under the lock.
- A schema-4 `SQLiteStore` captures the exact ordered `(version, checksum)`
  generation at open.
- Every existing SQLite mutation begins through one shared helper that obtains
  `BEGIN IMMEDIATE`, reads the generation inside that transaction, compares it
  exactly, and raises `SchemaGenerationChangedError` before mutation on any
  difference. This includes normal commits, Run-progress/reconciliation/lease
  writes, and Session deletion.
- Audit all raw `BEGIN IMMEDIATE` sites and add a regression test or mechanical
  assertion proving none bypass the shared fence.
- A Store opened at v3 before a later v4 apply fails closed on its next write.
  A fresh Store opened after apply succeeds. Existing pre-T003 compatibility is
  documented as requiring quiescent upgrade; do not claim retroactive fencing.

## Required focused tests

- `plan()` is byte-for-byte/read-transaction non-mutating on empty, v1, v2, v3,
  and v4 databases.
- Trusted checksum bootstrap and exact `applied()` values.
- Changed SQL and changed transform identity rejection before any mutation.
- Malformed v3 table/index plus invalid v2 transformed snapshot/event data and
  invalid v3 lease/operation/checkpoint/reconciliation fixtures roll back.
- Failure/cancellation checkpoints across all version-4 boundaries roll back.
- Same-database concurrent `plan/apply/open` converges; different databases make
  independent progress.
- Stale writer fails for every mutation family before changing data; refreshed
  writer succeeds.
- Old v1/v2/v3 migration, open cancellation, busy retry, and corruption tests
  remain green.

## Quality constraints

- Keep migration-resource hashing, manifest verification, schema validation,
  coordination, and transaction settlement in focused private abstractions;
  do not further inflate unrelated runtime code.
- Errors have stable dedicated types/messages and do not expose filesystem or
  database secrets unnecessarily.
- No public API beyond the task's approved Migration contracts and errors.
- Python 3.12-compatible syntax and dependencies only.
- Preserve all M02-T001/M02-T002 behavior and schema-3 fixture compatibility.
