from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.execution import ExecutionDescriptor, ExecutionPolicyDescriptor
from agent_sdk.runtime.models import AgentSpec, RunSnapshot, RunStatus, SessionSnapshot
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools.builtins.files import workspace_roots
from agent_sdk.tools.builtins.workspace import resolve_workspace_path
from agent_sdk.tools.errors import ToolAccessDenied


def _current_run(
    *,
    run_id: str,
    session_id: str,
    workspace_scopes: tuple[str, ...] | None,
) -> RunSnapshot:
    descriptor = ExecutionDescriptor.create(
        agent=AgentSpec(name="workspace", model="test/model"),
        messages=({"role": "user", "content": "go"},),
        tools=(),
        workspace_scopes=workspace_scopes,
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    )
    return RunSnapshot(
        run_id=run_id,
        session_id=session_id,
        agent_revision="workspace:1",
        status=RunStatus.CREATED,
        user_input="go",
        execution_compatibility="current",
        execution_descriptor=descriptor,
    )


def _pre_r4_run_data(run: RunSnapshot) -> dict[str, object]:
    data = run.model_dump(mode="json")
    descriptor = data["execution_descriptor"]
    assert isinstance(descriptor, dict)
    agent = descriptor["agent"]
    assert isinstance(agent, dict)
    agent.pop("tool_allowlist")
    agent.pop("workspace_allowlist")
    descriptor.pop("workspace_scopes")
    descriptor["agent_hash"] = _canonical_hash(agent)
    descriptor["descriptor_hash"] = _canonical_hash(
        {key: value for key, value in descriptor.items() if key != "descriptor_hash"}
    )
    return data


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


@pytest.mark.asyncio
async def test_run_scoped_workspace_lookup_fails_closed_without_a_matching_run(
    tmp_path: Path,
) -> None:
    store = InMemoryStore()
    root = tmp_path / "workspace"
    root.mkdir()
    session = await RuntimeCommands(store).create_session(workspaces=(root,))

    with pytest.raises(ToolAccessDenied, match="run workspace is unavailable"):
        await workspace_roots(store, session.session_id, run_id="run_missing")

    foreign = _current_run(
        run_id="run_foreign",
        session_id="ses_foreign",
        workspace_scopes=(str(root.resolve()),),
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    foreign.run_id,
                    foreign.session_id,
                    foreign.version,
                    foreign.model_dump(mode="json"),
                ),
            ),
        )
    )
    with pytest.raises(ToolAccessDenied, match="run workspace is unavailable"):
        await workspace_roots(store, session.session_id, run_id=foreign.run_id)


@pytest.mark.asyncio
async def test_legacy_run_workspace_scope_falls_back_to_session_roots(
    tmp_path: Path,
) -> None:
    store = InMemoryStore()
    root = tmp_path / "workspace"
    root.mkdir()
    session = await RuntimeCommands(store).create_session(workspaces=(root,))
    run = _current_run(
        run_id="run_legacy_scope",
        session_id=session.session_id,
        workspace_scopes=None,
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    run.run_id,
                    run.session_id,
                    run.version,
                    _pre_r4_run_data(run),
                ),
            ),
        )
    )

    assert await workspace_roots(store, session.session_id, run_id=run.run_id) == (
        root.resolve(),
    )


@pytest.mark.asyncio
async def test_explicit_empty_run_workspace_scope_does_not_inherit_session_roots(
    tmp_path: Path,
) -> None:
    store = InMemoryStore()
    root = tmp_path / "workspace"
    root.mkdir()
    session = await RuntimeCommands(store).create_session(workspaces=(root,))
    run = _current_run(
        run_id="run_empty_scope",
        session_id=session.session_id,
        workspace_scopes=(),
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    run.run_id,
                    run.session_id,
                    run.version,
                    run.model_dump(mode="json"),
                ),
            ),
        )
    )

    roots = await workspace_roots(store, session.session_id, run_id=run.run_id)

    assert roots == ()
    with pytest.raises(ToolAccessDenied):
        resolve_workspace_path(roots, "denied.txt", for_write=False)


@pytest.mark.asyncio
async def test_run_workspace_scope_requires_same_storage_owner_and_session_containment(
    tmp_path: Path,
) -> None:
    store = InMemoryStore()
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    child = root / "child"
    root.mkdir()
    outside.mkdir()
    child.mkdir()
    session = await RuntimeCommands(store).create_session(workspaces=(root,))

    wrong_owner = _current_run(
        run_id="run_wrong_owner",
        session_id=session.session_id,
        workspace_scopes=(str(child),),
    )
    outside_scope = _current_run(
        run_id="run_outside_scope",
        session_id=session.session_id,
        workspace_scopes=(str(outside),),
    )
    valid_scope = _current_run(
        run_id="run_child_scope",
        session_id=session.session_id,
        workspace_scopes=(str(child),),
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    wrong_owner.run_id,
                    "ses_wrong_owner",
                    wrong_owner.version,
                    wrong_owner.model_dump(mode="json"),
                ),
                SnapshotWrite(
                    "run",
                    outside_scope.run_id,
                    outside_scope.session_id,
                    outside_scope.version,
                    outside_scope.model_dump(mode="json"),
                ),
                SnapshotWrite(
                    "run",
                    valid_scope.run_id,
                    valid_scope.session_id,
                    valid_scope.version,
                    valid_scope.model_dump(mode="json"),
                ),
            ),
        )
    )

    with pytest.raises(ToolAccessDenied, match="run workspace is unavailable"):
        await workspace_roots(store, session.session_id, run_id=wrong_owner.run_id)
    with pytest.raises(ToolAccessDenied, match="run workspace is unavailable"):
        await workspace_roots(store, session.session_id, run_id=outside_scope.run_id)
    assert await workspace_roots(store, session.session_id, run_id=valid_scope.run_id) == (
        child.resolve(),
    )


@pytest.mark.asyncio
async def test_sqlite_run_workspace_scope_rejects_a_wrong_storage_owner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    root = tmp_path / "workspace"
    root.mkdir()
    store = await SQLiteStore.open(database)
    try:
        session = await RuntimeCommands(store).create_session(workspaces=(root,))
        run = _current_run(
            run_id="run_sqlite_wrong_owner",
            session_id=session.session_id,
            workspace_scopes=(str(root),),
        )
        await store.commit(
            CommitBatch(
                events=(),
                snapshots=(
                    SnapshotWrite(
                        "run",
                        run.run_id,
                        "ses_wrong_owner",
                        run.version,
                        run.model_dump(mode="json"),
                    ),
                ),
            )
        )

        with pytest.raises(ToolAccessDenied, match="run workspace is unavailable"):
            await workspace_roots(store, session.session_id, run_id=run.run_id)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_run_workspace_scope_rejects_a_symlink_redirected_outside(
    tmp_path: Path,
) -> None:
    store = InMemoryStore()
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    child = root / "child"
    child.mkdir()
    session = await RuntimeCommands(store).create_session(workspaces=(root,))
    run = _current_run(
        run_id="run_redirected_scope",
        session_id=session.session_id,
        workspace_scopes=(str(child),),
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    run.run_id,
                    run.session_id,
                    run.version,
                    run.model_dump(mode="json"),
                ),
            ),
        )
    )
    child.rmdir()
    try:
        child.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(ToolAccessDenied, match="run workspace is unavailable"):
        await workspace_roots(store, session.session_id, run_id=run.run_id)


@pytest.mark.asyncio
async def test_run_workspace_scope_rejects_relative_descriptor_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.chdir(root)
    session = await RuntimeCommands(store).create_session(workspaces=(root,))
    run = _current_run(
        run_id="run_relative_scope",
        session_id=session.session_id,
        workspace_scopes=("child",),
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    run.run_id,
                    run.session_id,
                    run.version,
                    run.model_dump(mode="json"),
                ),
            ),
        )
    )

    with pytest.raises(ToolAccessDenied, match="run workspace is unavailable"):
        await workspace_roots(store, session.session_id, run_id=run.run_id)


@pytest.mark.asyncio
async def test_final_workspace_resolution_rechecks_scope_after_redirect(
    tmp_path: Path,
) -> None:
    store = InMemoryStore()
    root = tmp_path / "workspace"
    child = root / "child"
    outside = tmp_path / "outside"
    root.mkdir()
    child.mkdir()
    outside.mkdir()
    session = await RuntimeCommands(store).create_session(workspaces=(root,))
    run = _current_run(
        run_id="run_redirect_between_checks",
        session_id=session.session_id,
        workspace_scopes=(str(child),),
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(
                SnapshotWrite(
                    "run",
                    run.run_id,
                    run.session_id,
                    run.version,
                    run.model_dump(mode="json"),
                ),
            ),
        )
    )
    scopes = await workspace_roots(store, session.session_id, run_id=run.run_id)
    child.rmdir()
    try:
        child.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(ToolAccessDenied, match="outside configured workspace"):
        resolve_workspace_path(
            scopes,
            "secret.txt",
            for_write=True,
            containment_roots=(root,),
        )
