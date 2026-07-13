# M02-T003 Artifact Lifecycle and Migrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store large payloads atomically, migrate databases safely, and clean Session-owned artifacts after deletion.

**Architecture:** FileArtifactStore writes content-addressed files beside SQLite; metadata remains transactional. MigrationRunner verifies checksums and supports plan/dry-run before applying ordered SQL. T003+ Store opens participate in a migration/open coordinator and every write transaction verifies the opened schema generation before mutation.

**Tech Stack:** pathlib, hashlib, aiosqlite, pytest tmp paths.

## Global Constraints

- Workspace files are never managed/deleted as Artifacts.
- Artifact write uses temp file plus atomic replace.
- Cleanup jobs contain anonymous ids after Session data removal.
- The checksum system bootstraps the existing `(version, applied_at)` rows for migrations 1-3 before requiring checksums for migration 4 and later.
- Migration 2's identity includes both `0002_idempotency.sql` and the stable `session-ownership-v1-to-v2` Python transform id; changing either after bootstrap is rejected.
- The migration/open coordinator serializes T003+ opens and migrations for one
  database. Every T003+ write transaction verifies that its opened schema
  generation/checksum set is still current, so a connection opened before a
  later migration fails closed instead of writing through an obsolete model.
  Pre-T003 binaries still require the documented quiescent-upgrade rule because
  they cannot retroactively honor this fence.

---

### Task 1: Add ArtifactStore and MigrationRunner

**Files:**
- Create: `src/agent_sdk/storage/artifacts.py`
- Create: `src/agent_sdk/storage/migrations.py`
- Create: `src/agent_sdk/storage/migrations/0004_migration_checksums_and_artifacts.sql`
- Modify: `src/agent_sdk/storage/sqlite.py`
- Create: `tests/integration/storage/test_artifacts.py`
- Create: `tests/integration/storage/test_migrations.py`

**Interfaces:**
- Produces: `ArtifactMetadata`, `ArtifactStore.put/read/delete_session`,
  `Migration`, async `MigrationRunner.open/plan/apply/applied`.
- Consumes: Session delete command and SQLite connection factory.

- [ ] **Step 1: Write atomic Artifact and checksum migration tests**

```python
@pytest.mark.asyncio
async def test_artifact_is_content_addressed_and_deleted(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    meta = await store.put("ses_1", b"large output", mime_type="text/plain")
    assert await store.read(meta.artifact_id) == b"large output"
    await store.delete_session("ses_1")
    assert not meta.path.exists()

@pytest.mark.asyncio
async def test_changed_applied_migration_checksum_is_rejected(sqlite_path: Path) -> None:
    runner = await migration_fixture(sqlite_path, applied_sql="CREATE TABLE x(a INT)")
    with pytest.raises(MigrationChecksumError):
        await runner.plan(replacement_sql="CREATE TABLE x(a TEXT)")

@pytest.mark.asyncio
async def test_bootstrap_records_trusted_checksums_for_versions_one_to_three(
    version_three_database: Path,
) -> None:
    runner = await MigrationRunner.open(version_three_database)
    await runner.apply()
    applied = await runner.applied()
    assert tuple(item.version for item in applied) == (1, 2, 3, 4)
    assert all(item.checksum for item in applied)

@pytest.mark.asyncio
async def test_stale_open_generation_cannot_write_after_later_migration(
    version_three_database: Path,
) -> None:
    stale = await open_store(version_three_database)
    runner = await MigrationRunner.open(version_three_database)
    await runner.apply()
    with pytest.raises(SchemaGenerationChangedError):
        await stale.commit(sample_batch())
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/storage/test_artifacts.py tests/integration/storage/test_migrations.py -v`

Expected: missing Artifact/Migration types.

- [ ] **Step 3: Implement atomic FileArtifactStore**

Hash bytes with SHA-256, write `<hash>.tmp-<id>`, flush/fsync, `os.replace` to `<hash>`, then persist metadata. Existing hash reuses content but records Session ownership separately.

```python
async def put(self, session_id: str, content: bytes, mime_type: str) -> ArtifactMetadata:
    digest = sha256(content).hexdigest()
    target = self._root / digest
    staged = await asyncio.to_thread(write_staged_bytes, target, content)
    try:
        async with self._metadata.immediate_transaction() as transaction:
            if not target.exists():
                await asyncio.to_thread(os.replace, staged, target)
            return await transaction.add_owner(
                session_id, digest, len(content), mime_type
            )
    finally:
        await asyncio.to_thread(unlink_if_exists, staged)
```

- [ ] **Step 4: Implement ordered checksum migrations**

Migration 4 rebuilds the migration table in the same transaction that installs
Artifact tables: create
`schema_migrations_next(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL,
applied_at TEXT NOT NULL)`, copy versions 1-3 with release-owned trusted digests,
drop the old two-column table, and rename the new table. This is necessary
because databases created before M02-T003 did not record checksums and
historical authenticity cannot be inferred from the database alone. Version 2's
digest hashes its SQL bytes plus the stable Python transform id; version 3
hashes its lease/reconciliation SQL. Reject any packaged resource/transform
whose digest differs from the trusted manifest before beginning migration 4.

Before trusting or writing checksums for versions 1-3, validate the claimed v3
database with the exact release-owned validators for every prior version:
schema-migration rows, tables, columns, indexes and SQL shapes; the v2
idempotency/Session ownership/legacy descriptor transform invariants across all
snapshots/events; and the v3 lease, external-operation, checkpoint, and
reconciliation schema/data invariants. Run this validation inside migration 4's
`BEGIN IMMEDIATE` before creating the replacement table. A database that merely
claims `(1, 2, 3)` but fails any validator is rejected with no checksum rows or
Artifact DDL. Add malformed v3 table/index, bad v2 transform data, and invalid
v3 lease/checkpoint fixtures.

After bootstrap, require every applied row to have a checksum. Read numbered SQL
package resources, compare each digest to `schema_migrations(version, checksum)`,
and apply pending files in one explicit transaction each without
`executescript`. The open coordinator serializes discovery/apply; each Store
captures the validated version/checksum generation at open and revalidates it
inside every `BEGIN IMMEDIATE` write transaction. `await plan()` returns
versions/checksums without writes; `await apply()` is the only mutating runner
entry point.

```python
async def apply(self) -> None:
    for migration in await self.plan():
        async with self._store.immediate_transaction() as transaction:
            for statement in complete_sql_statements(migration.sql):
                await transaction.execute(statement)
            await transaction.record_migration(migration.version, migration.sha256)
```

Fault-inject before/after the schema alteration, each bootstrap row update,
Artifact DDL, and version-4 insert. Any failure leaves the exact version-3
database usable with its original two-column migration table; no partially
bootstrapped checksums or Artifact tables remain.

- [ ] **Step 5: Wire Session deletion**

Delete Artifact ownership/contributions and create anonymous cleanup jobs in the
same SQLite transaction. A cleanup job contains only its stable job id, content
digest/path, and state—not the deleted Session id. After commit, a retryable
worker removes files. Do not emit Session-linked durable events after deletion.

```python
async def delete_session_artifacts(self, session_id: str) -> None:
    async with self._metadata.immediate_transaction() as transaction:
        orphan_hashes = await transaction.remove_owner_and_list_orphans(session_id)
        for digest in orphan_hashes:
            await transaction.enqueue_cleanup_once(
                f"artifact:{digest}", self._root / digest
            )
    await self._cleanup.run_pending()
```

The cleanup worker claims one job, opens `BEGIN IMMEDIATE`, rechecks that the
digest still has no owner, removes the file idempotently while the metadata
writer lock prevents a concurrent `put` finalization, marks the job complete,
and commits. A missing file counts as success. `put` stages/fsyncs its temp file
before its metadata transaction, then rechecks/replaces the target and adds the
owner while holding the same writer serialization. Fault-inject after ownership
removal, job insertion, transaction commit, file removal, and job completion;
every crash leaves either a live owner+file or a durable retryable cleanup job,
never an untracked orphan caused by Session deletion.

Because SQLite and the filesystem cannot share one atomic commit, add an
idempotent startup/maintenance sweep under the same metadata writer
serialization. It removes stale temp files and enqueues anonymous cleanup jobs
for content-hash files that have neither an Artifact owner/metadata row nor an
existing cleanup job. This recovers a crash after `os.replace` but before the
metadata transaction commits. Fault-inject every `put` boundary (temp fsync,
transaction begin, replace, metadata insert, commit) and prove reopen+sweep
converges without deleting a newly committed owner.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/storage/test_artifacts.py tests/integration/storage/test_migrations.py -v`

Expected: atomic read/delete and checksum safeguards pass.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/storage tests/integration/storage
git commit -m "feat: add artifact lifecycle and migrations"
```
