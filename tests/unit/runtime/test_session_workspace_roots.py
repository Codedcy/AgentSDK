from __future__ import annotations

from pathlib import Path

import pytest

from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.models import SessionSnapshot
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools.builtins.files import workspace_roots
from agent_sdk.tools.errors import ToolAccessDenied


@pytest.mark.asyncio
async def test_session_creation_canonicalizes_roots_before_idempotency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "base"
    workspace = base / "workspace"
    base.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(base)
    commands = RuntimeCommands(InMemoryStore())

    created = await commands.create_session(
        workspaces=(Path("workspace"),),
        idempotency_key="canonical-root",
    )
    replayed = await commands.create_session(
        workspaces=(workspace.resolve(),),
        idempotency_key="canonical-root",
    )

    assert created == replayed
    assert created.workspaces == (str(workspace.resolve()),)


@pytest.mark.asyncio
async def test_sqlite_relative_session_root_survives_cwd_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.sqlite3"
    original = tmp_path / "original"
    other = tmp_path / "other"
    workspace = original / "workspace"
    original.mkdir()
    other.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(original)

    first = await SQLiteStore.open(database)
    session = await RuntimeCommands(first).create_session(
        workspaces=(Path("workspace"),),
    )
    await first.close()

    monkeypatch.chdir(other)
    reopened = await SQLiteStore.open(database)
    try:
        assert await workspace_roots(reopened, session.session_id) == (
            workspace.resolve(),
        )
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_legacy_relative_session_root_fails_closed_for_builtins() -> None:
    store = InMemoryStore()
    legacy = SessionSnapshot(
        session_id="ses_legacy_relative",
        workspaces=("relative-workspace",),
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "session",
                    legacy.session_id,
                    legacy.session_id,
                    legacy.version,
                    legacy.model_dump(mode="json"),
                ),
            )
        )
    )

    with pytest.raises(ToolAccessDenied, match="session workspace is unavailable"):
        await workspace_roots(store, legacy.session_id)
