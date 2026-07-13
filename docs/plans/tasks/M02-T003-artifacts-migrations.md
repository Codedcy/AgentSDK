# M02-T003 Artifact Lifecycle and Migrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store large payloads atomically, migrate databases safely, and clean Session-owned artifacts after deletion.

**Architecture:** FileArtifactStore writes content-addressed files beside SQLite; metadata remains transactional. MigrationRunner verifies checksums and supports plan/dry-run before applying ordered SQL.

**Tech Stack:** pathlib, hashlib, aiosqlite, pytest tmp paths.

## Global Constraints

- Workspace files are never managed/deleted as Artifacts.
- Artifact write uses temp file plus atomic replace.
- Cleanup jobs contain anonymous ids after Session data removal.

---

### Task 1: Add ArtifactStore and MigrationRunner

**Files:**
- Create: `src/agent_sdk/storage/artifacts.py`
- Create: `src/agent_sdk/storage/migrations.py`
- Create: `src/agent_sdk/storage/migrations/0004_artifacts.sql`
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

Read numbered SQL package resources, SHA-256 each file, compare to `schema_migrations(version, checksum)`, and apply pending files in one transaction each. `plan()` returns versions/checksums without writes.

```python
async def migrate(self) -> None:
    for migration in self.plan():
        async with self._store.immediate_transaction() as transaction:
            await transaction.execute_script(migration.sql)
            await transaction.record_migration(migration.version, migration.sha256)
```

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
