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
- Produces: `ArtifactMetadata`, `ArtifactStore.put/read/delete_session`, `Migration`, `MigrationRunner.plan/apply`.
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
    await MigrationRunner.open(version_three_database).apply()
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
    if not target.exists():
        await asyncio.to_thread(write_atomic_bytes, target, content)
    return await self._metadata.add_owner(session_id, digest, len(content), mime_type)
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

After bootstrap, require every applied row to have a checksum. Read numbered SQL
package resources, compare each digest to `schema_migrations(version, checksum)`,
and apply pending files in one explicit transaction each without
`executescript`. The open coordinator serializes discovery/apply; each Store
captures the validated version/checksum generation at open and revalidates it
inside every `BEGIN IMMEDIATE` write transaction. `plan()` returns
versions/checksums without writes.

```python
async def migrate(self) -> None:
    for migration in self.plan():
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

Delete Artifact ownership/contributions transactionally, then run retryable file cleanup by cleanup job id. Do not emit Session-linked durable events after deletion.

```python
async def delete_session_artifacts(self, session_id: str) -> None:
    orphan_hashes = await self._metadata.remove_owner_and_list_orphans(session_id)
    for digest in orphan_hashes:
        await self._cleanup.enqueue_once(f"artifact:{digest}", self._root / digest)
```

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/storage/test_artifacts.py tests/integration/storage/test_migrations.py -v`

Expected: atomic read/delete and checksum safeguards pass.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/storage tests/integration/storage
git commit -m "feat: add artifact lifecycle and migrations"
```
