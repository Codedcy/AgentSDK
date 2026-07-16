# M02-T003 Operational Phase Plan

## Objective

Complete `docs/plans/tasks/M02-T003-artifacts-migrations.md` without entering
M02-T004 control/cancellation scope. Every phase uses strict RED/GREEN tests,
an implementation report, and an independent Spec/Quality review with zero
Critical or Important findings before the next phase begins.

## Non-negotiable boundaries

- SQLite is the authoritative Artifact metadata store. Files are content
  addressed but every publish generation has a unique immutable physical path.
- No filesystem scan, fsync, replace, or unlink occurs while a SQLite
  transaction is open.
- Workspace files are never adopted as managed Artifact files and are never
  deleted by Artifact cleanup.
- Migration 4 bootstraps release-owned checksums for versions 1-3 only after
  exact v1/v2/v3 schema and data validation under `BEGIN IMMEDIATE`.
- Migration 2 identity covers the packaged SQL bytes and the stable
  `session-ownership-v1-to-v2` transform id.
- `plan()` is read-only. Only `apply()` mutates. Migration/open discovery is
  serialized per canonical database identity.
- Every T003+ SQLite write checks the exact schema version/checksum generation
  captured at open inside its writer transaction and fails closed when stale.
- Session deletion removes Artifact ownership and enqueues anonymous cleanup
  jobs in the same transaction as all other Session-owned state.
- Cleanup jobs and durable diagnostics contain no deleted Session id.
- Cancellation and failure settle open transactions before the connection is
  closed; a migration failure leaves the exact usable pre-migration database.

## Phase A — Migration identities, bootstrap, coordinator, and write fence

Deliver:

- `Migration`, applied migration records, stable checksum manifest, and public
  migration errors.
- Migration 4 SQL for checksum-bearing migration rows and Artifact metadata
  tables required by later phases.
- `MigrationRunner.open/plan/applied/apply` with read-only planning, exact
  packaged-resource verification, v3 validator reuse, atomic bootstrap, and
  same-database open/apply coordination.
- `SQLiteStore.open` integration at schema 4 and an opened-generation token.
- A shared write-begin path that validates version/checksum rows inside every
  existing `BEGIN IMMEDIATE` business mutation before any mutation occurs.
- Fault, cancellation, malformed-v3, changed-resource, concurrent-open, and
  stale-connection tests.

Exit gate:

- Migration/fence focused suite is green on Memory-independent SQLite tests.
- Existing v1/v2/v3 migration fixtures still upgrade correctly.
- Every SQLite mutation path is proven fenced or mechanically audited.
- Independent review: Spec C0/I0 and Quality C0/I0.

## Phase B — Durable Artifact publish/read state machine

Deliver:

- `ArtifactMetadata`, `ArtifactStore` protocol, SQLite metadata repository, and
  `FileArtifactStore.put/read`.
- Durable `publishing -> ready` CAS with unique generation paths, claim tokens,
  expiry/reclaim, pending-owner joins, hash/size verification, bounded waits,
  and filesystem work outside transactions.
- Atomic staging write with file fsync, replace, and containing-directory fsync
  where supported; staged-file cleanup is cancellation safe.
- Same-content concurrency, stale-publisher, replace/finalize crash, corrupt or
  missing file, reopen/help, path confinement, and cancellation tests.

Exit gate:

- Artifact publish/read matrix passes with real SQLite and real files.
- No transaction spans filesystem I/O; no physical generation path is reused.
- Independent review: Spec C0/I0 and Quality C0/I0.

## Phase C — Session-owned deletion, cleanup, and recovery sweep

Deliver:

- Session deletion atomically removes Artifact owners/contributions and creates
  idempotent anonymous `delete_pending` jobs before deleting the Session.
- Two-phase cleanup CAS (`delete_pending -> deleting -> complete`) with exact
  generation/path/claim fencing and unlink outside transactions.
- Put/delete race handling, expired-claim recovery, orphan temp/content scan,
  and startup/maintenance sweep with recheck-before-enqueue behavior.
- Exact stale-cleaner race proof: cleaner A expires, cleaner B finishes, a new
  generation becomes ready, then A resumes and can affect only its old path.
- Fault injection at every publish/delete boundary and reopen-to-convergence
  tests, including shared-digest multi-owner Sessions and workspace safety.

Exit gate:

- Session deletion remains atomic for all pre-existing Session-owned tables and
  Artifact ownership/jobs.
- Repeated cleanup/sweep converges without ready-owner loss or unrecoverable
  files and never persists deleted Session identity.
- Independent review: Spec C0/I0 and Quality C0/I0.

## Phase D — Whole-task compatibility and release gate

Deliver:

- Root exports and package resources required by the approved public contract.
- Fresh-install and old-schema upgrade E2E, multi-process migration/open race,
  stale-writer proof, hard-exit Artifact boundary proof, and reference CLI
  no-open/no-model smoke.
- Full supported-Python suites, Ruff, strict mypy, import/export/signature
  audit, sdist/wheel clean-install tests, migration-resource/hash audit, scope
  audit, and final independent whole-task review.
- Update task checkboxes, task index, and progress ledger only after approval.

Exit gate:

- M02-T003 is approved with Spec C0/I0 and Quality C0/I0.
- M02-T003 is marked `done`; M02-T004 may then become `in_progress`.

## Execution order

1. Phase A
2. Independent Phase A review and repairs until approved
3. Phase B
4. Independent Phase B review and repairs until approved
5. Phase C
6. Independent Phase C review and repairs until approved
7. Phase D and independent whole-task review

No later phase may weaken an earlier phase's validators, fencing, cancellation
settlement, or failure atomicity to make its own tests pass.
