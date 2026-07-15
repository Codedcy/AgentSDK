from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError

from agent_sdk.api import _LazySQLiteStore
from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.models import (
    RunFailure,
    RunSnapshot,
    RunStatus,
    SessionSnapshot,
    TokenUsage,
)
from agent_sdk.runtime.reconciliation import RecoveryStateConflictError
from agent_sdk.runtime.session_lifecycle import RUN_LIFECYCLE_FINAL_STATUSES
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


NOW = datetime(2026, 7, 15, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    "status_value",
    ("interrupted", "waiting_reconciliation"),
)
def test_recovery_owned_run_statuses_are_strictly_nonterminal(
    status_value: str,
) -> None:
    status = RunStatus(status_value)
    snapshot = RunSnapshot(
        run_id="run_1",
        session_id="ses_1",
        agent_revision="agent:1",
        status=status,
        user_input="hello",
        version=3,
    )

    assert snapshot.status is status
    assert RUN_LIFECYCLE_FINAL_STATUSES == frozenset(
        {RunStatus.COMPLETED, RunStatus.FAILED}
    )

    with pytest.raises(ValidationError, match="nonterminal"):
        snapshot.model_copy(update={"output_text": "must not persist"})

    for forbidden in (
        {"usage": TokenUsage()},
        {
            "error": RunFailure(
                code="FAILED",
                message="must not persist",
                retryable=False,
            )
        },
        {
            "tool_results": (
                {
                    "call_id": "call_1",
                    "tool_name": "tool_1",
                    "status": "succeeded",
                    "content": "ok",
                },
            )
        },
        {"version": 2},
    ):
        with pytest.raises(ValidationError):
            snapshot.model_copy(update=forbidden)


@pytest_asyncio.fixture(params=("memory", "sqlite", "lazy_sqlite"))
async def abandoned_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield InMemoryStore()
        return
    store: Any
    if request.param == "sqlite":
        store = await SQLiteStore.open(tmp_path / "abandoned.db")
    else:
        store = _LazySQLiteStore(tmp_path / "lazy-abandoned.db")
    try:
        yield store
    finally:
        await store.close()


def _run(run_id: str, status: RunStatus) -> RunSnapshot:
    values: dict[str, object] = {
        "run_id": run_id,
        "session_id": "ses_1",
        "agent_revision": "agent:1",
        "status": status,
        "user_input": "hello",
        "version": 1 if status is RunStatus.CREATED else 3,
    }
    if status is RunStatus.RUNNING:
        values["version"] = 2
    elif status is RunStatus.COMPLETED:
        values.update(output_text="done", usage=TokenUsage())
    elif status is RunStatus.FAILED:
        values.update(
            output_text="",
            usage=TokenUsage(),
            error=RunFailure(code="FAILED", message="failed", retryable=False),
        )
    return RunSnapshot.model_validate(values)


def _snapshot_write(snapshot: RunSnapshot | SessionSnapshot) -> SnapshotWrite:
    if isinstance(snapshot, RunSnapshot):
        return SnapshotWrite(
            "run",
            snapshot.run_id,
            snapshot.session_id,
            snapshot.version,
            snapshot.model_dump(mode="json"),
        )
    return SnapshotWrite(
        "session",
        snapshot.session_id,
        snapshot.session_id,
        snapshot.version,
        snapshot.model_dump(mode="json"),
    )


async def _seed_abandoned_run(store: Any, *, expires_at: datetime | None = None) -> None:
    run = _run("run_1", RunStatus.RUNNING)
    session = SessionSnapshot(
        session_id=run.session_id,
        workspaces=("workspace",),
        active_run_ids=(run.run_id,),
    )
    await store.commit(
        CommitBatch(
            events=(),
            snapshots=(_snapshot_write(session), _snapshot_write(run)),
        )
    )
    if expires_at is not None:
        await store.acquire_lease(
            run_id=run.run_id,
            owner="worker-secret",
            now=expires_at - timedelta(seconds=30),
            expires_at=expires_at,
        )


async def _corrupt_abandoned_state(store: Any, invalidity: str) -> None:
    underlying = await store._get() if isinstance(store, _LazySQLiteStore) else store
    if isinstance(underlying, InMemoryStore):
        async with underlying._lock:
            if invalidity == "run_identity":
                write = underlying._snapshots[("run", "run_1")]
                data = dict(write.data)
                data["run_id"] = "run_foreign"
                underlying._snapshots[("run", "run_1")] = write._replace(data=data)
            elif invalidity == "session_ownership":
                write = underlying._snapshots[("session", "ses_1")]
                data = dict(write.data)
                data["active_run_ids"] = []
                underlying._snapshots[("session", "ses_1")] = write._replace(
                    data=data
                )
            else:
                underlying._leases["run_1"] = {"owner": "lease-secret"}
        return

    assert isinstance(underlying, SQLiteStore)
    async with underlying._lock:
        if invalidity == "run_identity":
            async with underlying._connection.execute(
                "SELECT data_json FROM snapshots "
                "WHERE kind = 'run' AND entity_id = 'run_1'"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            data = json.loads(row[0])
            data["run_id"] = "run_foreign"
            await underlying._connection.execute(
                "UPDATE snapshots SET data_json = ? "
                "WHERE kind = 'run' AND entity_id = 'run_1'",
                (json.dumps(data, sort_keys=True, separators=(",", ":")),),
            )
        elif invalidity == "session_ownership":
            async with underlying._connection.execute(
                "SELECT data_json FROM snapshots "
                "WHERE kind = 'session' AND entity_id = 'ses_1'"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            data = json.loads(row[0])
            data["active_run_ids"] = []
            await underlying._connection.execute(
                "UPDATE snapshots SET data_json = ? "
                "WHERE kind = 'session' AND entity_id = 'ses_1'",
                (json.dumps(data, sort_keys=True, separators=(",", ":")),),
            )
        else:
            await underlying._connection.execute("PRAGMA ignore_check_constraints = ON")
            await underlying._connection.execute(
                "UPDATE leases SET released = 2 WHERE run_id = 'run_1'"
            )
        await underlying._connection.commit()


@pytest.mark.asyncio
async def test_list_abandoned_run_ids_matches_status_and_lease_contract(
    abandoned_store: Any,
) -> None:
    statuses = {
        "run_completed": RunStatus.COMPLETED,
        "run_created": RunStatus.CREATED,
        "run_failed": RunStatus.FAILED,
        "run_interrupted": RunStatus("interrupted"),
        "run_running_active": RunStatus.RUNNING,
        "run_running_expired": RunStatus.RUNNING,
        "run_running_missing": RunStatus.RUNNING,
        "run_waiting_active": RunStatus.WAITING_PERMISSION,
        "run_waiting_missing": RunStatus.WAITING_PERMISSION,
        "run_waiting_released": RunStatus.WAITING_PERMISSION,
        "run_waiting_reconciliation": RunStatus("waiting_reconciliation"),
    }
    runs = tuple(_run(run_id, status) for run_id, status in statuses.items())
    active_run_ids = tuple(
        sorted(
            run.run_id
            for run in runs
            if run.status not in RUN_LIFECYCLE_FINAL_STATUSES
        )
    )
    session = SessionSnapshot(
        session_id="ses_1",
        workspaces=("workspace",),
        active_run_ids=active_run_ids,
    )
    await abandoned_store.commit(
        CommitBatch(
            events=(),
            snapshots=(_snapshot_write(session),)
            + tuple(_snapshot_write(run) for run in runs),
        )
    )
    await abandoned_store.acquire_lease(
        run_id="run_running_active",
        owner="active-running",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    await abandoned_store.acquire_lease(
        run_id="run_running_expired",
        owner="expired-running",
        now=NOW - timedelta(seconds=60),
        expires_at=NOW - timedelta(seconds=30),
    )
    await abandoned_store.acquire_lease(
        run_id="run_waiting_active",
        owner="active-waiting",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    released = await abandoned_store.acquire_lease(
        run_id="run_waiting_released",
        owner="released-waiting",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    await abandoned_store.release_lease(released)

    assert await abandoned_store.list_abandoned_run_ids(now=NOW) == (
        "run_running_expired",
        "run_running_missing",
        "run_waiting_missing",
        "run_waiting_released",
    )


@pytest.mark.parametrize(
    "invalidity",
    ("run_identity", "session_ownership", "lease_representation"),
)
@pytest.mark.asyncio
async def test_list_abandoned_run_ids_fails_closed_on_corrupt_state(
    abandoned_store: Any,
    invalidity: str,
) -> None:
    await _seed_abandoned_run(abandoned_store, expires_at=NOW)
    await _corrupt_abandoned_state(abandoned_store, invalidity)

    with pytest.raises(RecoveryStateConflictError) as caught:
        await abandoned_store.list_abandoned_run_ids(now=NOW)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_list_abandoned_run_ids_normalizes_utc_and_observes_expiry_boundary(
    abandoned_store: Any,
) -> None:
    await _seed_abandoned_run(abandoned_store, expires_at=NOW)
    same_instant = NOW.astimezone(timezone(timedelta(hours=8)))

    assert await abandoned_store.list_abandoned_run_ids(now=same_instant) == (
        "run_1",
    )


@pytest.mark.asyncio
async def test_delete_session_removes_abandoned_run_query_state(
    abandoned_store: Any,
) -> None:
    await _seed_abandoned_run(abandoned_store)

    await abandoned_store.delete_session("ses_1")

    assert await abandoned_store.list_abandoned_run_ids(now=NOW) == ()
    assert await abandoned_store.latest_run_event_sequence("run_1") is None


@pytest.mark.asyncio
async def test_abandoned_query_rejects_naive_time(
    abandoned_store: Any,
) -> None:
    await _seed_abandoned_run(abandoned_store)

    with pytest.raises(RecoveryStateConflictError):
        await abandoned_store.list_abandoned_run_ids(now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("invalidity", ("excluded_ownership", "excluded_lease"))
@pytest.mark.asyncio
async def test_abandoned_query_validates_state_before_status_filtering(
    abandoned_store: Any,
    invalidity: str,
) -> None:
    run = _run("run_1", RunStatus.CREATED)
    session = SessionSnapshot(
        session_id=run.session_id,
        workspaces=("workspace",),
        active_run_ids=() if invalidity == "excluded_ownership" else (run.run_id,),
    )
    await abandoned_store.commit(
        CommitBatch(
            events=(),
            snapshots=(_snapshot_write(session), _snapshot_write(run)),
        )
    )
    if invalidity == "excluded_lease":
        await abandoned_store.acquire_lease(
            run_id=run.run_id,
            owner="created-owner",
            now=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )
        await _corrupt_abandoned_state(abandoned_store, "lease_representation")

    with pytest.raises(RecoveryStateConflictError):
        await abandoned_store.list_abandoned_run_ids(now=NOW)


def _event(
    event_id: str,
    *,
    run_id: str | None,
    session_id: str = "ses_1",
    sequence: int,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        type="run.progressed" if run_id is not None else "session.progressed",
        session_id=session_id,
        run_id=run_id,
        sequence=sequence,
        payload={"event_id": event_id},
        occurred_at=NOW,
    )


@pytest.mark.asyncio
async def test_latest_run_event_sequence_uses_exact_run_tail(
    abandoned_store: Any,
) -> None:
    runs = (
        _run("run_1", RunStatus.RUNNING).model_copy(update={"version": 50}),
        _run("run_other", RunStatus.RUNNING),
        _run("run_without_events", RunStatus.RUNNING),
    )
    session = SessionSnapshot(
        session_id="ses_1",
        workspaces=("workspace",),
        active_run_ids=tuple(sorted(run.run_id for run in runs)),
    )
    await abandoned_store.commit(
        CommitBatch(
            events=(
                _event("evt_run_1_first", run_id="run_1", sequence=2),
                _event("evt_run_1_tail", run_id="run_1", sequence=9),
                _event("evt_other_tail", run_id="run_other", sequence=999),
                _event("evt_global_tail", run_id=None, sequence=1000),
            ),
            snapshots=(_snapshot_write(session),)
            + tuple(_snapshot_write(run) for run in runs),
        )
    )

    assert await abandoned_store.latest_run_event_sequence("run_1") == 9
    assert (
        await abandoned_store.latest_run_event_sequence("run_without_events")
        is None
    )


@pytest.mark.parametrize("status", (RunStatus.COMPLETED, RunStatus.FAILED))
@pytest.mark.asyncio
async def test_latest_run_event_sequence_rejects_final_run_still_owned_by_session(
    abandoned_store: Any,
    status: RunStatus,
) -> None:
    secret = f"run-final-active-secret-{status.value}-3c1"
    run = _run(secret, status)
    session = SessionSnapshot(
        session_id=run.session_id,
        workspaces=("workspace",),
        active_run_ids=(run.run_id,),
    )
    await abandoned_store.commit(
        CommitBatch(
            events=(),
            snapshots=(_snapshot_write(session), _snapshot_write(run)),
        )
    )

    with pytest.raises(RecoveryStateConflictError) as caught:
        await abandoned_store.latest_run_event_sequence(secret)

    assert caught.value.to_dict() == {
        "code": "conflict",
        "message": "recovery state conflict",
        "retryable": True,
    }
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    sdk_locals = _sdk_traceback_locals(caught.value)
    assert sdk_locals
    assert all(secret not in repr(frame) for frame in sdk_locals)


async def _corrupt_run_event(store: Any, invalidity: str) -> None:
    underlying = await store._get() if isinstance(store, _LazySQLiteStore) else store
    if isinstance(underlying, InMemoryStore):
        async with underlying._lock:
            stored = underlying._events[0]
            if invalidity == "session_identity":
                event = stored.event.model_copy(update={"session_id": "ses_foreign"})
            else:
                event = stored.event.model_copy(update={"sequence": 0})
            underlying._events[0] = stored._replace(event=event)
        return

    assert isinstance(underlying, SQLiteStore)
    async with underlying._lock:
        await underlying._connection.execute("PRAGMA ignore_check_constraints = ON")
        if invalidity == "session_identity":
            await underlying._connection.execute(
                "UPDATE events SET session_id = 'ses_foreign' "
                "WHERE event_id = 'evt_run_tail'"
            )
        else:
            await underlying._connection.execute(
                "UPDATE events SET sequence = 0 WHERE event_id = 'evt_run_tail'"
            )
        await underlying._connection.commit()


@pytest.mark.parametrize("invalidity", ("session_identity", "sequence_shape"))
@pytest.mark.asyncio
async def test_latest_run_event_sequence_fails_closed_on_malformed_owned_event(
    abandoned_store: Any,
    invalidity: str,
) -> None:
    await _seed_abandoned_run(abandoned_store)
    await abandoned_store.commit(
        CommitBatch(
            events=(
                _event("evt_run_tail", run_id="run_1", sequence=2),
            )
        )
    )
    await _corrupt_run_event(abandoned_store, invalidity)

    with pytest.raises(RecoveryStateConflictError) as caught:
        await abandoned_store.latest_run_event_sequence("run_1")

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def _sdk_traceback_locals(error: BaseException) -> tuple[dict[str, Any], ...]:
    frames: list[dict[str, Any]] = []
    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/agent_sdk/" in filename:
            frames.append(dict(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return tuple(frames)


@pytest.mark.asyncio
async def test_lazy_event_tail_conflict_traceback_discards_run_id_secret(
    tmp_path: Path,
) -> None:
    store = _LazySQLiteStore(tmp_path / "lazy-tail-secret.db")
    secret = "run-secret-3c1-c296"
    try:
        await store.commit(
            CommitBatch(
                events=(
                    _event(
                        "evt_orphan_secret",
                        run_id=secret,
                        sequence=1,
                    ),
                )
            )
        )
        with pytest.raises(RecoveryStateConflictError) as caught:
            await store.latest_run_event_sequence(secret)

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        sdk_locals = _sdk_traceback_locals(caught.value)
        assert sdk_locals
        assert all(secret not in repr(frame) for frame in sdk_locals)
    finally:
        await store.close()
