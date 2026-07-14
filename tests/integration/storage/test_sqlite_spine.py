import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import agent_sdk.storage.sqlite as sqlite_storage
import pytest
from aiosqlite.context import Result

from agent_sdk.api import AgentSDK
from agent_sdk.context.models import (
    CompactionLevel,
    ContextBudget,
    ContextCapsule,
    ContextView,
)
from agent_sdk.evaluation.models import EvaluationResult, EvaluationVerdict
from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot, TokenUsage
from agent_sdk.workflow.models import (
    AgentNode,
    WorkflowIR,
    WorkflowNodeSnapshot,
    WorkflowNodeStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)


def _create_v1_database(path: Path, *, corrupt_run_status: str | None = None) -> None:
    migration = (
        Path(__file__).parents[3]
        / "src"
        / "agent_sdk"
        / "storage"
        / "migrations"
        / "0001_initial.sql"
    ).read_text(encoding="utf-8")
    session = SessionSnapshot(session_id="ses_v1", workspaces=("workspace",)).model_dump(
        mode="json"
    )
    session.pop("active_run_ids")
    session.pop("active_workflow_run_ids")
    created_run = RunSnapshot(
        run_id="run_active",
        session_id="ses_v1",
        agent_revision="agent:1",
        status=RunStatus.CREATED,
        user_input="active",
    ).model_dump(mode="json")
    completed_run = RunSnapshot(
        run_id="run_done",
        session_id="ses_v1",
        agent_revision="agent:1",
        status=RunStatus.COMPLETED,
        user_input="done",
        version=3,
        output_text="done",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    ).model_dump(mode="json")
    completed_run_created = RunSnapshot(
        run_id="run_done",
        session_id="ses_v1",
        agent_revision="agent:1",
        status=RunStatus.CREATED,
        user_input="done",
    ).model_dump(mode="json")
    for run in (created_run, completed_run, completed_run_created):
        run.pop("execution_compatibility")
        run.pop("execution_descriptor")
        run.pop("tool_results")
    if corrupt_run_status is not None:
        created_run["status"] = corrupt_run_status

    active_ir = WorkflowIR.create(
        name="active",
        nodes=(AgentNode(id="one", agent_revision="agent:1", input="work"),),
        edges=(),
    )
    active_node = WorkflowNodeSnapshot(
        entity_id="wf_active:one",
        workflow_run_id="wf_active",
        session_id="ses_v1",
        node_id="one",
        status=WorkflowNodeStatus.PENDING,
    )
    active_workflow = WorkflowRunSnapshot(
        workflow_run_id="wf_active",
        session_id="ses_v1",
        status=WorkflowRunStatus.RUNNING,
        workflow=active_ir,
        nodes=(active_node,),
    ).model_dump(mode="json")
    active_workflow.pop("execution_compatibility")
    active_workflow.pop("execution_descriptor")

    done_ir = WorkflowIR.create(
        name="done",
        nodes=(AgentNode(id="one", agent_revision="agent:1", input="work"),),
        edges=(),
    )
    usage = TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    done_node = WorkflowNodeSnapshot(
        entity_id="wf_done:one",
        workflow_run_id="wf_done",
        session_id="ses_v1",
        node_id="one",
        status=WorkflowNodeStatus.COMPLETED,
        version=3,
        run_id="run_done",
        output_text="done",
        usage=usage,
    )
    done_workflow = WorkflowRunSnapshot(
        workflow_run_id="wf_done",
        session_id="ses_v1",
        status=WorkflowRunStatus.COMPLETED,
        workflow=done_ir,
        nodes=(done_node,),
        version=4,
        output_text="done",
        usage=usage,
    ).model_dump(mode="json")
    done_workflow.pop("execution_compatibility")
    done_workflow.pop("execution_descriptor")

    capsule = ContextCapsule(
        objective="continue",
        constraints=(),
        decisions=(),
        facts=("fact",),
        next_actions=("next",),
        artifact_refs=(),
        source_event_ids=("evt_run_active",),
    )
    context_budget = ContextBudget.calculate(
        model_window=100,
        output_reserve=10,
        tool_schema_tokens=5,
        safety_reserve=5,
        projected_source_tokens=80,
    )
    view = ContextView(
        view_id="ctx_v1",
        session_id="ses_v1",
        message_refs=("evt_run_active",),
        capsule_id="cap_v1",
        estimated_tokens=1,
        recommended_level=CompactionLevel.L3,
        applied_level=CompactionLevel.L3,
        budget=context_budget,
    )
    evaluation = EvaluationResult(
        evaluation_id="eval_v1",
        session_id="ses_v1",
        subject_run_id="run_active",
        evaluator_id="exact",
        evaluator_version="1",
        method="test",
        verdict=EvaluationVerdict.PASS,
        metrics={"score": 1.0},
        reason="ok",
        confidence=1.0,
        evidence_event_ids=("evt_run_active",),
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        subject_cursor=2,
    )

    snapshots = (
        ("session", "ses_v1", "ses_v1", 1, session),
        ("run", "run_active", "ses_v1", 1, created_run),
        ("run", "run_done", "ses_v1", 3, completed_run),
        ("workflow", "wf_active", "ses_v1", 1, active_workflow),
        ("workflow_node", "wf_active:one", "ses_v1", 1, active_node.model_dump(mode="json")),
        ("workflow", "wf_done", "ses_v1", 4, done_workflow),
        ("workflow_node", "wf_done:one", "ses_v1", 3, done_node.model_dump(mode="json")),
        (
            "context_capsule",
            "cap_v1",
            "ses_v1",
            1,
            {"session_id": "ses_v1", "capsule": capsule.model_dump(mode="json")},
        ),
        ("context_view", "ctx_v1", "ses_v1", 1, view.model_dump(mode="json")),
        ("evaluation", "eval_v1", "ses_v1", 1, evaluation.model_dump(mode="json")),
    )
    events = (
        ("evt_session", "ses_v1", None, 1, "session.created", session),
        ("evt_run_active", "ses_v1", "run_active", 1, "run.created", created_run),
        ("evt_run_done", "ses_v1", "run_done", 1, "run.created", completed_run_created),
        ("evt_run_done_started", "ses_v1", "run_done", 2, "run.started", {}),
        ("evt_run_done_completed", "ses_v1", "run_done", 3, "run.completed", {}),
        (
            "evt_wf_active",
            "ses_v1",
            "wf_active",
            1,
            "workflow.started",
            {"definition_hash": active_ir.definition_hash, "name": active_ir.name},
        ),
        (
            "evt_wf_done",
            "ses_v1",
            "wf_done",
            1,
            "workflow.started",
            {"definition_hash": done_ir.definition_hash, "name": done_ir.name},
        ),
        (
            "evt_wf_done_node_started",
            "ses_v1",
            "wf_done",
            2,
            "workflow.node.started",
            {"node_id": "one", "run_id": "run_done"},
        ),
        (
            "evt_wf_done_node_completed",
            "ses_v1",
            "wf_done",
            3,
            "workflow.node.completed",
            {"node_id": "one", "run_id": "run_done"},
        ),
        ("evt_wf_done_completed", "ses_v1", "wf_done", 4, "workflow.completed", {}),
        (
            "evt_context_compacted",
            "ses_v1",
            "ctx_v1",
            1,
            "context.compaction.completed",
            {
                "view_id": "ctx_v1",
                "capsule_id": "cap_v1",
                "level": "L3",
                "model": "fake/model",
                "budget": context_budget.model_dump(mode="json"),
                "usage": {
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None,
                },
            },
        ),
        (
            "evt_context_view",
            "ses_v1",
            "ctx_v1",
            2,
            "context.view.created",
            {"view_id": "ctx_v1", "capsule_id": "cap_v1"},
        ),
        (
            "evt_evaluation",
            "ses_v1",
            "eval_v1",
            1,
            "evaluation.completed",
            evaluation.model_dump(mode="json"),
        ),
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(migration)
        connection.execute("INSERT INTO schema_migrations VALUES (1, 'v1')")
        connection.executemany(
            "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
            [(*row[:4], json.dumps(row[4], sort_keys=True, separators=(",", ":"))) for row in snapshots],
        )
        connection.executemany(
            """
            INSERT INTO events(
                event_id, session_id, run_id, sequence, type, schema_version,
                occurred_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, 1, '2025-01-01T00:00:00+00:00', ?)
            """,
            [(*row[:5], json.dumps(row[5], sort_keys=True, separators=(",", ":"))) for row in events],
        )


@pytest.mark.asyncio
async def test_v1_upgrade_backfills_only_nonterminal_execution_ownership(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    _create_v1_database(path)

    store = await SQLiteStore.open(path)
    try:
        async def unused_completion(**_: object) -> dict[str, object]:
            raise AssertionError("loading migrated snapshots must not execute a model")

        sdk = AgentSDK.for_test(store=store, acompletion=unused_completion)
        session_data = await store.get_snapshot("session", "ses_v1")
        assert session_data is not None
        assert session_data["version"] == 1
        assert session_data["active_run_ids"] == ["run_active"]
        assert session_data["active_workflow_run_ids"] == ["wf_active"]
        for run_id in ("run_active", "run_done"):
            run = await sdk.runs.get(run_id)
            assert run.execution_compatibility == "legacy_unknown"
            assert run.execution_descriptor is None
            assert run.tool_results == ()
        for workflow_id in ("wf_active", "wf_done"):
            workflow = await sdk.workflows.get(workflow_id)
            assert workflow.execution_compatibility == "legacy_unknown"
            assert workflow.execution_descriptor is None
        async with store._connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ) as cursor:
            assert await cursor.fetchall() == [(1,), (2,)]
        await sdk.close()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_v1_unknown_run_status_rolls_back_entire_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "corrupt-v1.db"
    _create_v1_database(path, corrupt_run_status="interrupted")
    with sqlite3.connect(path) as connection:
        before = connection.execute(
            "SELECT kind, entity_id, data_json FROM snapshots ORDER BY kind, entity_id"
        ).fetchall()

    with pytest.raises(ValueError, match="incompatible version-1"):
        await SQLiteStore.open(path)

    with sqlite3.connect(path) as connection:
        after = connection.execute(
            "SELECT kind, entity_id, data_json FROM snapshots ORDER BY kind, entity_id"
        ).fetchall()
        versions = connection.execute("SELECT version FROM schema_migrations").fetchall()
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='idempotency_records'"
        ).fetchone()
    assert after == before
    assert versions == [(1,)]
    assert table is None


@pytest.mark.asyncio
async def test_concurrent_v1_open_serializes_discovery_and_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "concurrent-v1.db"
    _create_v1_database(path)
    first_v1_discovered = asyncio.Event()
    release_first = asyncio.Event()
    second_is_competing = asyncio.Event()
    requested = 0
    discoveries: list[str] = []

    async def checkpoint(stage: str) -> None:
        nonlocal requested
        if stage == "migration-lock-requested":
            requested += 1
            if requested == 2:
                second_is_competing.set()
        elif stage.startswith("migration-schema-discovered-"):
            discoveries.append(stage.removeprefix("migration-schema-discovered-"))
            if stage == "migration-schema-discovered-v1":
                first_v1_discovered.set()
                await release_first.wait()

    monkeypatch.setattr(SQLiteStore, "_migration_checkpoint", staticmethod(checkpoint))
    tasks: list[asyncio.Task[SQLiteStore]] = []
    try:
        first_task = asyncio.create_task(SQLiteStore.open(path))
        tasks.append(first_task)
        await asyncio.wait_for(first_v1_discovered.wait(), timeout=1)
        second_task = asyncio.create_task(SQLiteStore.open(path))
        tasks.append(second_task)
        await asyncio.wait_for(second_is_competing.wait(), timeout=1)
        assert discoveries == ["v1"]
        release_first.set()
        first, second = await asyncio.wait_for(
            asyncio.gather(first_task, second_task),
            timeout=2,
        )
        assert discoveries == ["v1", "v2"]
        for store in (first, second):
            async with store._connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ) as cursor:
                assert await cursor.fetchall() == [(1,), (2,)]
    finally:
        release_first.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, SQLiteStore):
                await result.close()


@pytest.mark.asyncio
async def test_migration_never_uses_aiosqlite_executescript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("executescript changes transaction boundaries")

    monkeypatch.setattr(aiosqlite.Connection, "executescript", forbidden)
    store = await SQLiteStore.open(tmp_path / "empty.db")
    await store.close()


def _v1_state(path: Path) -> tuple[list[tuple[str, str, str]], list[tuple[int]], object, object]:
    with sqlite3.connect(path) as connection:
        snapshots = connection.execute(
            "SELECT kind, entity_id, data_json FROM snapshots ORDER BY kind, entity_id"
        ).fetchall()
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='idempotency_records'"
        ).fetchone()
        index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idempotency_records_session'"
        ).fetchone()
    return snapshots, versions, table, index


@pytest.mark.parametrize(
    "stage",
    [
        "migration-2-statement-1",
        "migration-2-statement-2",
        "migration-2-backfill-run-run_active",
        "migration-2-backfill-run-run_done",
        "migration-2-backfill-session-ses_v1",
        "migration-2-backfill-workflow-wf_active",
        "migration-2-backfill-workflow-wf_done",
        "migration-2-version-inserted",
        "migration-2-final-validation",
    ],
)
@pytest.mark.asyncio
async def test_v1_upgrade_faults_rollback_ddl_backfill_and_version(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / f"fault-{stage}.db"
    _create_v1_database(path)
    before = _v1_state(path)

    async def fail_at(checkpoint: str) -> None:
        if checkpoint == stage:
            raise RuntimeError("injected migration fault")

    with monkeypatch.context() as fault:
        fault.setattr(SQLiteStore, "_migration_checkpoint", staticmethod(fail_at))
        with pytest.raises(RuntimeError, match="injected migration fault"):
            await SQLiteStore.open(path)

    assert _v1_state(path) == before
    reopened = await SQLiteStore.open(path)
    await reopened.close()


@pytest.mark.asyncio
async def test_v1_upgrade_commit_failure_rolls_back_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "commit-fault.db"
    _create_v1_database(path)
    before = _v1_state(path)

    async def fail_commit(_: aiosqlite.Connection) -> None:
        raise RuntimeError("injected commit fault")

    with monkeypatch.context() as fault:
        fault.setattr(aiosqlite.Connection, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected commit fault"):
            await SQLiteStore.open(path)
    assert _v1_state(path) == before


@pytest.mark.asyncio
async def test_cancel_racing_migration_commit_observes_only_complete_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cancel-commit.db"
    _create_v1_database(path)
    original_commit = aiosqlite.Connection.commit
    committed = asyncio.Event()
    release = asyncio.Event()

    async def commit_then_wait(connection: aiosqlite.Connection) -> None:
        await original_commit(connection)
        committed.set()
        await release.wait()

    task: asyncio.Task[SQLiteStore] | None = None
    with monkeypatch.context() as race:
        race.setattr(aiosqlite.Connection, "commit", commit_then_wait)
        task = asyncio.create_task(SQLiteStore.open(path))
        await asyncio.wait_for(committed.wait(), timeout=2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    reopened = await SQLiteStore.open(path)
    try:
        async with reopened._connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ) as cursor:
            assert await cursor.fetchall() == [(1,), (2,)]
        assert (await reopened.get_snapshot("session", "ses_v1"))["active_run_ids"] == [
            "run_active"
        ]
    finally:
        await reopened.close()


@pytest.mark.parametrize(
    "kind",
    [
        "session",
        "run",
        "workflow",
        "workflow_node",
        "context_capsule",
        "context_view",
        "evaluation",
    ],
)
@pytest.mark.asyncio
async def test_v1_upgrade_rejects_cross_owner_row_for_every_snapshot_kind(
    kind: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"cross-owner-{kind}.db"
    _create_v1_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE snapshots SET session_id='ses_missing' WHERE kind=?",
            (kind,),
        )
    with pytest.raises(ValueError, match="incompatible version-1"):
        await SQLiteStore.open(path)
    assert _v1_state(path)[1:] == ([(1,)], None, None)


@pytest.mark.asyncio
async def test_v1_upgrade_rejects_unknown_snapshot_kind(tmp_path: Path) -> None:
    path = tmp_path / "unknown-kind.db"
    _create_v1_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO snapshots VALUES ('unknown', 'unknown_1', 'ses_v1', 1, '{}')"
        )
    with pytest.raises(ValueError, match="snapshot kind"):
        await SQLiteStore.open(path)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("DELETE FROM events WHERE event_id='evt_run_active'", "run start"),
        ("DELETE FROM events WHERE event_id='evt_wf_active'", "workflow start"),
        ("DELETE FROM events WHERE event_id='evt_session'", "session facts"),
        ("DELETE FROM events WHERE event_id='evt_context_view'", "context facts"),
        ("DELETE FROM events WHERE event_id='evt_context_compacted'", "context capsule facts"),
        ("DELETE FROM events WHERE event_id='evt_evaluation'", "evaluation facts"),
        ("DELETE FROM snapshots WHERE kind='run' AND entity_id='run_done'", "orphan run"),
        (
            "UPDATE events SET session_id='ses_missing' WHERE event_id='evt_evaluation'",
            "event owner",
        ),
        (
            "UPDATE events SET type='run.failed' WHERE event_id='evt_run_done_completed'",
            "run terminal",
        ),
        (
            "UPDATE events SET type='workflow.failed' WHERE event_id='evt_wf_done_completed'",
            "workflow terminal",
        ),
        (
            """
            UPDATE events SET payload_json=json_set(payload_json, '$.run_id', 'run_other')
            WHERE event_id='evt_wf_done_node_completed'
            """,
            "workflow node event",
        ),
    ],
)
@pytest.mark.asyncio
async def test_v1_upgrade_rejects_missing_or_contradictory_event_facts(
    mutation: str,
    expected: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "bad-event-facts.db"
    _create_v1_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(mutation)
    with pytest.raises(ValueError, match=expected):
        await SQLiteStore.open(path)


@pytest.mark.asyncio
async def test_v1_upgrade_rejects_malformed_extra_compaction_event_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "malformed-extra-context-event.db"
    _create_v1_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO events(
                event_id, session_id, run_id, sequence, type, schema_version,
                occurred_at, payload_json
            ) VALUES (
                'evt_context_malformed', 'ses_v1', 'ctx_missing', 1,
                'context.compaction.completed', 1,
                '2025-01-01T00:00:00+00:00', '{"view_id":"ctx_missing"}'
            )
            """
        )
    before = _v1_state(path)

    with pytest.raises(ValueError, match="context event"):
        await SQLiteStore.open(path)

    assert _v1_state(path) == before


@pytest.mark.asyncio
async def test_v1_upgrade_rejects_cross_owner_duplicate_compaction_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cross-owner-duplicate-context-event.db"
    _create_v1_database(path)
    other = SessionSnapshot(session_id="ses_other", workspaces=()).model_dump(mode="json")
    other.pop("active_run_ids")
    other.pop("active_workflow_run_ids")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO snapshots VALUES ('session', 'ses_other', 'ses_other', 1, ?)",
            (json.dumps(other, sort_keys=True, separators=(",", ":")),),
        )
        connection.execute(
            """
            INSERT INTO events(
                event_id, session_id, run_id, sequence, type, schema_version,
                occurred_at, payload_json
            ) VALUES (
                'evt_session_other', 'ses_other', NULL, 1, 'session.created', 1,
                '2025-01-01T00:00:00+00:00', ?
            )
            """,
            (json.dumps(other, sort_keys=True, separators=(",", ":")),),
        )
        payload = connection.execute(
            "SELECT payload_json FROM events WHERE event_id='evt_context_compacted'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO events(
                event_id, session_id, run_id, sequence, type, schema_version,
                occurred_at, payload_json
            ) VALUES (
                'evt_context_cross_owner', 'ses_other', 'ctx_other', 1,
                'context.compaction.completed', 1,
                '2025-01-01T00:00:00+00:00', ?
            )
            """,
            (payload,),
        )
    before = _v1_state(path)

    with pytest.raises(ValueError, match="context event"):
        await SQLiteStore.open(path)

    assert _v1_state(path) == before


@pytest.mark.parametrize(
    "mutation",
    [
        """
        UPDATE snapshots SET data_json=json_set(data_json, '$.capsule_id', 'cap_missing')
        WHERE kind='context_view'
        """,
        """
        UPDATE snapshots SET data_json=json_set(data_json, '$.subject_run_id', 'run_missing')
        WHERE kind='evaluation'
        """,
        """
        UPDATE snapshots SET data_json=json_set(data_json, '$.evidence_event_ids', json('[\"evt_missing\"]'))
        WHERE kind='evaluation'
        """,
        """
        UPDATE snapshots SET entity_id='run_other'
        WHERE kind='run' AND entity_id='run_active'
        """,
        """
        UPDATE snapshots SET version=2
        WHERE kind='run' AND entity_id='run_active'
        """,
    ],
)
@pytest.mark.asyncio
async def test_v1_upgrade_rejects_missing_references_and_row_identity(
    mutation: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "bad-reference.db"
    _create_v1_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(mutation)
    with pytest.raises(ValueError, match="incompatible version-1"):
        await SQLiteStore.open(path)


@pytest.mark.parametrize("target_sql", ["PRAGMA journal_mode=WAL", "BEGIN IMMEDIATE"])
@pytest.mark.asyncio
async def test_open_retries_transient_busy_for_wal_and_writer_lock(
    target_sql: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "transient-busy.db"
    connection = await aiosqlite.connect(path)
    original_execute = connection.execute
    attempts = 0

    def execute_with_busy(sql: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        if sql == target_sql and attempts < 2:
            attempts += 1

            async def busy() -> Any:
                raise sqlite3.OperationalError("database is locked")

            return Result(busy())
        return original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(connection, "execute", execute_with_busy)

    async def connect_existing(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        del args, kwargs
        return connection

    monkeypatch.setattr(aiosqlite, "connect", connect_existing)
    store = await SQLiteStore.open(path)
    try:
        assert attempts == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_busy_deadline_is_bounded_and_leaves_database_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "busy-deadline.db"
    connection = await aiosqlite.connect(path)
    original_execute = connection.execute

    def always_busy(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql == "BEGIN IMMEDIATE":
            async def busy() -> Any:
                raise sqlite3.OperationalError("database is locked")

            return Result(busy())
        return original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(connection, "execute", always_busy)

    async def connect_existing(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        del args, kwargs
        return connection

    monkeypatch.setattr(aiosqlite, "connect", connect_existing)
    monkeypatch.setattr(sqlite_storage, "_OPEN_RETRY_SECONDS", 0.0)
    with pytest.raises(RuntimeError, match="SQLite open conflict"):
        await SQLiteStore.open(path)
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall() == []


@pytest.mark.asyncio
async def test_v1_upgrade_requires_old_writer_to_be_quiesced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "old-writer.db"
    _create_v1_database(path)
    old_writer = sqlite3.connect(path, timeout=0)
    old_writer.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(sqlite_storage, "_OPEN_RETRY_SECONDS", 0.05)
    try:
        with pytest.raises(RuntimeError, match="open conflict"):
            await SQLiteStore.open(path)
    finally:
        old_writer.rollback()
        old_writer.close()

    store = await SQLiteStore.open(path)
    await store.close()


@pytest.mark.parametrize("corruption", ["table_check", "partial_index"])
@pytest.mark.asyncio
async def test_v1_upgrade_rejects_same_columns_with_unexpected_sql_shape(
    corruption: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"unexpected-{corruption}.db"
    script = (
        Path(__file__).parents[3]
        / "src"
        / "agent_sdk"
        / "storage"
        / "migrations"
        / "0001_initial.sql"
    ).read_text(encoding="utf-8")
    if corruption == "table_check":
        script = script.replace(
            "session_id TEXT NOT NULL,\n    run_id TEXT,",
            "session_id TEXT NOT NULL CHECK(length(session_id) > 0),\n    run_id TEXT,",
        )
    else:
        script = script.replace(
            "CREATE INDEX snapshots_session ON snapshots(session_id);",
            "CREATE INDEX snapshots_session ON snapshots(session_id) WHERE session_id <> '';",
        )
    with sqlite3.connect(path) as connection:
        connection.executescript(script)
        connection.execute("INSERT INTO schema_migrations VALUES (1, 'v1')")

    with pytest.raises(ValueError, match="incompatible database schema"):
        await SQLiteStore.open(path)
    assert _v1_state(path)[1:] == ([(1,)], None, None)


@pytest.mark.asyncio
async def test_session_and_run_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = await SQLiteStore.open(path)
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[tmp_path])
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="hello",
    )
    await store.close()

    reopened = await SQLiteStore.open(path)
    assert (await reopened.get_snapshot("session", session.session_id))["status"] == "active"
    assert (await reopened.get_snapshot("run", run.run_id))["status"] == "created"
    events = await reopened.read_events(after_cursor=0, session_id=session.session_id)
    assert [stored.event.type for stored in events] == [
        "session.created",
        "session.run.attached",
        "run.created",
    ]
    await reopened.close()


@pytest.mark.asyncio
async def test_sqlite_stale_snapshot_rolls_back_event_and_cursor(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    created = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={},
    )
    await store.commit(
        CommitBatch(
            events=(created,),
            snapshots=(
                SnapshotWrite("run", "run_1", "ses_1", 1, {"status": "created"}),
            ),
        )
    )
    stale = EventEnvelope.new(
        type="run.failed",
        session_id="ses_1",
        run_id="run_1",
        sequence=2,
        payload={},
    )

    with pytest.raises(ValueError, match="snapshot version"):
        await store.commit(
            CommitBatch(
                events=(stale,),
                snapshots=(
                    SnapshotWrite("run", "run_1", "ses_1", 1, {"status": "failed"}),
                ),
            )
        )

    assert [item.cursor for item in await store.read_events(after_cursor=0)] == [1]
    await store.close()


@pytest.mark.asyncio
async def test_sqlite_delete_leaves_global_cursor_hole(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="session.created",
                    session_id="ses_1",
                    run_id=None,
                    sequence=1,
                    payload={},
                ),
            )
        )
    )
    await store.delete_session("ses_1")
    result = await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="session.created",
                    session_id="ses_2",
                    run_id=None,
                    sequence=1,
                    payload={},
                ),
            )
        )
    )

    assert result.last_cursor == 2
    await store.close()


@pytest.mark.asyncio
async def test_sqlite_rejects_incompatible_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1, 'existing');
            CREATE TABLE events(
                cursor INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT,
                sequence INTEGER NOT NULL,
                type TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE snapshots(
                kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                data_json TEXT NOT NULL
            );
            """
        )

    with pytest.raises(ValueError, match="incompatible database schema"):
        await SQLiteStore.open(path)

    with sqlite3.connect(path) as connection:
        final_journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        migration = connection.execute("SELECT version, applied_at FROM schema_migrations").fetchone()
        events_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
    assert migration == (1, "existing")
    # Open arbitration establishes WAL before schema discovery, even when the
    # later exact-schema validation fails closed.
    assert final_journal_mode == ("wal",)
    assert events_sql is not None
    assert "AUTOINCREMENT" not in events_sql[0]


@pytest.mark.asyncio
async def test_open_enables_and_verifies_sqlite_pragmas(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    async with store._connection.execute("PRAGMA journal_mode") as cursor:
        journal_mode = await cursor.fetchone()
    async with store._connection.execute("PRAGMA foreign_keys") as cursor:
        foreign_keys = await cursor.fetchone()

    assert journal_mode == ("wal",)
    assert foreign_keys == (1,)
    await store.close()


@pytest.mark.asyncio
async def test_open_rejects_and_closes_when_wal_cannot_be_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = await aiosqlite.connect(":memory:")

    async def connect_to_memory(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        return connection

    monkeypatch.setattr(aiosqlite, "connect", connect_to_memory)

    with pytest.raises(RuntimeError, match="journal_mode"):
        await SQLiteStore.open(tmp_path / "state.db")

    with pytest.raises(ValueError, match="no active connection"):
        await connection.execute("SELECT 1")


@pytest.mark.asyncio
async def test_open_rejects_and_closes_when_foreign_keys_cannot_be_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "state.db"
    connection = await aiosqlite.connect(database_path)

    def ignore_foreign_keys(
        action: int,
        arg1: str | None,
        arg2: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del database_name, trigger_name
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "foreign_keys" and arg2 == "ON":
            return sqlite3.SQLITE_IGNORE
        return sqlite3.SQLITE_OK

    await connection.set_authorizer(ignore_foreign_keys)

    async def connect_with_authorizer(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        return connection

    monkeypatch.setattr(aiosqlite, "connect", connect_with_authorizer)

    with pytest.raises(RuntimeError, match="foreign_keys"):
        await SQLiteStore.open(database_path)

    with pytest.raises(ValueError, match="no active connection"):
        await connection.execute("SELECT 1")


@pytest.mark.parametrize("pragma", ["foreign_keys", "journal_mode"])
@pytest.mark.asyncio
async def test_open_converts_pragma_setter_errors_stably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pragma: str,
) -> None:
    database_path = tmp_path / "state.db"
    connection = await aiosqlite.connect(database_path)

    def deny_pragma_setter(
        action: int,
        arg1: str | None,
        arg2: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del database_name, trigger_name
        if action == sqlite3.SQLITE_PRAGMA and arg1 == pragma and arg2 is not None:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    await connection.set_authorizer(deny_pragma_setter)

    async def connect_with_authorizer(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        return connection

    monkeypatch.setattr(aiosqlite, "connect", connect_with_authorizer)

    with pytest.raises(RuntimeError, match=pragma):
        await SQLiteStore.open(database_path)

    with pytest.raises(ValueError, match="no active connection"):
        await connection.execute("SELECT 1")


def _cancel_after_begin(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = store._connection.execute

    def execute_with_cancel(sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql != "BEGIN IMMEDIATE":
            return execute(sql, *args, **kwargs)

        async def begin_then_cancel() -> None:
            await execute(sql, *args, **kwargs)
            raise asyncio.CancelledError

        return begin_then_cancel()

    monkeypatch.setattr(store._connection, "execute", execute_with_cancel)


def _block_rollback(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, asyncio.Event]:
    rollback = store._connection.rollback
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_rollback() -> None:
        started.set()
        await release.wait()
        await rollback()

    monkeypatch.setattr(store._connection, "rollback", blocked_rollback)
    return started, release


@pytest.mark.asyncio
async def test_cancelled_commit_rolls_back_open_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    event = EventEnvelope.new(
        type="session.created",
        session_id="ses_1",
        run_id=None,
        sequence=1,
        payload={},
    )
    try:
        with monkeypatch.context() as cancelled:
            _cancel_after_begin(store, cancelled)
            with pytest.raises(asyncio.CancelledError):
                await store.commit(CommitBatch(events=(event,)))

        result = await store.commit(CommitBatch(events=(event,)))
        assert result.last_cursor == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancelled_delete_rolls_back_open_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    event = EventEnvelope.new(
        type="session.created",
        session_id="ses_1",
        run_id=None,
        sequence=1,
        payload={},
    )
    await store.commit(CommitBatch(events=(event,)))
    try:
        with monkeypatch.context() as cancelled:
            _cancel_after_begin(store, cancelled)
            with pytest.raises(asyncio.CancelledError):
                await store.delete_session("ses_1")

        await store.delete_session("ses_1")
        assert await store.read_events(after_cursor=0) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_commit_propagates_cancellation_received_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    created = EventEnvelope.new(
        type="run.created",
        session_id="ses_1",
        run_id="run_1",
        sequence=1,
        payload={},
    )
    await store.commit(
        CommitBatch(
            events=(created,),
            snapshots=(
                SnapshotWrite("run", "run_1", "ses_1", 1, {"status": "created"}),
            ),
        )
    )
    rejected = EventEnvelope.new(
        type="run.failed",
        session_id="ses_1",
        run_id="run_1",
        sequence=2,
        payload={},
    )
    task: asyncio.Task[object] | None = None
    release: asyncio.Event | None = None
    try:
        with monkeypatch.context() as race:
            rollback_started, release = _block_rollback(store, race)
            task = asyncio.create_task(
                store.commit(
                    CommitBatch(
                        events=(rejected,),
                        snapshots=(
                            SnapshotWrite(
                                "run",
                                "run_1",
                                "ses_1",
                                1,
                                {"status": "failed"},
                            ),
                        ),
                    )
                )
            )
            await asyncio.wait_for(rollback_started.wait(), timeout=1)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        result = await store.commit(CommitBatch(events=(rejected,)))
        assert result.last_cursor == 2
        snapshot = await store.get_snapshot("run", "run_1")
        assert snapshot is not None
        assert snapshot["status"] == "created"
    finally:
        if release is not None:
            release.set()
        if task is not None and not task.done():
            task.cancel()
            with suppress(BaseException):
                await task
        await store.close()


@pytest.mark.asyncio
async def test_delete_propagates_cancellation_received_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    first = EventEnvelope.new(
        type="session.created",
        session_id="ses_1",
        run_id=None,
        sequence=1,
        payload={},
    )
    await store.commit(CommitBatch(events=(first,)))
    task: asyncio.Task[object] | None = None
    release: asyncio.Event | None = None
    try:
        with monkeypatch.context() as race:
            execute = store._connection.execute

            def fail_snapshot_delete(sql: str, *args: Any, **kwargs: Any) -> Any:
                if "DELETE FROM snapshots" not in sql:
                    return execute(sql, *args, **kwargs)

                async def fail() -> None:
                    raise ValueError("original failure")

                return fail()

            race.setattr(store._connection, "execute", fail_snapshot_delete)
            rollback_started, release = _block_rollback(store, race)
            task = asyncio.create_task(store.delete_session("ses_1"))
            await asyncio.wait_for(rollback_started.wait(), timeout=1)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        second = EventEnvelope.new(
            type="session.created",
            session_id="ses_2",
            run_id=None,
            sequence=1,
            payload={},
        )
        result = await store.commit(CommitBatch(events=(second,)))
        assert result.last_cursor == 2
        assert [item.event.session_id for item in await store.read_events(after_cursor=0)] == [
            "ses_1",
            "ses_2",
        ]
    finally:
        if release is not None:
            release.set()
        if task is not None and not task.done():
            task.cancel()
            with suppress(BaseException):
                await task
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_rejects_wrong_aggregate_index_expression(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1, 'existing');
            CREATE TABLE events(
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT,
                sequence INTEGER NOT NULL,
                type TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE snapshots(
                kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                PRIMARY KEY(kind, entity_id)
            );
            CREATE INDEX events_session_cursor ON events(session_id, cursor);
            CREATE UNIQUE INDEX events_aggregate_sequence
                ON events(COALESCE(session_id, run_id), sequence);
            CREATE INDEX snapshots_session ON snapshots(session_id);
            """
        )

    opened: SQLiteStore | None = None
    try:
        with pytest.raises(ValueError, match="incompatible database schema"):
            opened = await SQLiteStore.open(path)
    finally:
        if opened is not None:
            await opened.close()


@pytest.mark.asyncio
async def test_closed_store_rejects_public_operations_stably(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    await store.close()

    operations: tuple[Callable[[], Awaitable[object]], ...] = (
        lambda: store.commit(CommitBatch(events=())),
        lambda: store.read_events(after_cursor=0),
        lambda: store.get_snapshot("session", "ses_1"),
        lambda: store.delete_session("ses_1"),
    )
    for operation in operations:
        with pytest.raises(RuntimeError, match="SQLiteStore is closed"):
            await operation()


@pytest.mark.asyncio
async def test_cancelled_close_keeps_store_stably_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await SQLiteStore.open(tmp_path / "state.db")
    close_finished = asyncio.Event()
    release = asyncio.Event()
    task: asyncio.Task[None] | None = None
    try:
        with monkeypatch.context() as race:
            close = store._connection.close

            async def close_then_block() -> None:
                await close()
                close_finished.set()
                await release.wait()

            race.setattr(store._connection, "close", close_then_block)
            task = asyncio.create_task(store.close())
            await asyncio.wait_for(close_finished.wait(), timeout=1)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        with pytest.raises(RuntimeError, match="SQLiteStore is closed"):
            await store.read_events(after_cursor=0)
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            with suppress(BaseException):
                await task
        await store.close()
