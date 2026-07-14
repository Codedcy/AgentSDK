from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agent_sdk.events.models import EventEnvelope
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.storage.idempotency import (
    IdempotencyConflictError,
    IdempotencyCorruptionError,
    IdempotencyReplay,
    IdempotencyReplayMissError,
    IdempotencyValidationError,
    IdempotencyWrite,
    fingerprint_command,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


@asynccontextmanager
async def _store(kind: str, tmp_path: Path) -> AsyncIterator[StateStore]:
    if kind == "memory":
        yield InMemoryStore()
        return
    store = await SQLiteStore.open(tmp_path / f"{kind}.db")
    try:
        yield store
    finally:
        await store.close()


def _write(
    fingerprint: str | None = None,
    *,
    scope: str = "session.create",
    key: str = "request-1",
    session_id: str = "ses_first",
    result: dict[str, object] | None = None,
) -> IdempotencyWrite:
    return IdempotencyWrite(
        scope=scope,
        key=key,
        request_fingerprint=fingerprint
        or fingerprint_command("session.create", {"workspaces": ["workspace"]}),
        session_id=session_id,
        result=result or {"session_id": session_id},
    )


def _session_batch(session_id: str, idempotency: IdempotencyWrite | IdempotencyReplay) -> CommitBatch:
    data = {
        "session_id": session_id,
        "status": "active",
        "workspaces": ["workspace"],
        "version": 1,
        "active_run_ids": [],
        "active_workflow_run_ids": [],
    }
    return CommitBatch(
        events=(
            EventEnvelope.new(
                type="session.created",
                session_id=session_id,
                run_id=None,
                sequence=1,
                payload=data,
            ),
        ),
        snapshots=(SnapshotWrite("session", session_id, session_id, 1, data),),
        idempotency=idempotency,
    )


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_matching_replay_returns_first_result_without_writes(
    kind: str,
    tmp_path: Path,
) -> None:
    async with _store(kind, tmp_path) as store:
        write = _write()
        first = await store.commit(_session_batch("ses_first", write))
        replay = await store.commit(_session_batch("ses_second", write))

        assert first.applied is True
        assert replay.applied is False
        assert replay.idempotency == first.idempotency
        assert await store.get_snapshot("session", "ses_second") is None
        assert await store.latest_cursor() == first.last_cursor


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_mismatched_reuse_is_atomic(kind: str, tmp_path: Path) -> None:
    secret_request = "request-secret-do-not-leak"
    secret_result = "result-secret-do-not-leak"
    first_fingerprint = fingerprint_command(
        "session.create", {"secret": secret_request, "attempt": 1}
    )
    second_fingerprint = fingerprint_command(
        "session.create", {"secret": secret_request, "attempt": 2}
    )
    async with _store(kind, tmp_path) as store:
        await store.commit(
            _session_batch(
                "ses_first",
                _write(first_fingerprint, result={"secret": secret_result}),
            )
        )
        before = await store.latest_cursor()
        with pytest.raises(IdempotencyConflictError, match="reused") as captured:
            await store.commit(
                _session_batch(
                    "ses_second",
                    _write(second_fingerprint, result={"secret": "incoming-secret"}),
                )
            )
        error_text = f"{captured.value!s} {captured.value!r}"
        assert secret_request not in error_text
        assert secret_result not in error_text
        assert "incoming-secret" not in error_text
        assert await store.latest_cursor() == before
        assert await store.get_snapshot("session", "ses_second") is None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    ("key", "fingerprint", "result"),
    [
        ("", "a" * 64, {"ok": True}),
        ("x" * 257, "a" * 64, {"ok": True}),
        ("key", "A" * 64, {"ok": True}),
        ("key", "a" * 63, {"ok": True}),
        ("key", "a" * 64, {"bad": float("nan")}),
        ("key", "a" * 64, {"bad": b"bytes"}),
        ("key", "a" * 64, {1: "non-string-key"}),
    ],
)
async def test_invalid_request_is_rejected_before_state_mutation(
    kind: str,
    key: str,
    fingerprint: str,
    result: dict[object, object],
    tmp_path: Path,
) -> None:
    async with _store(kind, tmp_path) as store:
        write = IdempotencyWrite(
            scope="session.create",
            key=key,
            request_fingerprint=fingerprint,
            session_id="ses_bad",
            result=result,  # type: ignore[arg-type]
        )
        with pytest.raises(IdempotencyValidationError):
            await store.commit(_session_batch("ses_bad", write))
        assert await store.latest_cursor() == 0
        assert await store.get_snapshot("session", "ses_bad") is None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_results_are_defensively_detached(kind: str, tmp_path: Path) -> None:
    original = {"nested": {"values": [1, 2]}}
    async with _store(kind, tmp_path) as store:
        first = await store.commit(
            _session_batch("ses_first", _write(result=original))
        )
        original["nested"]["values"].append(3)  # type: ignore[index,union-attr]
        assert first.idempotency is not None
        returned = first.idempotency.model_dump(mode="json")
        returned["result"]["nested"]["values"].append(4)
        durable = await store.get_idempotency("session.create", "request-1")
        assert durable is not None
        assert durable.model_dump(mode="json")["result"] == {
            "nested": {"values": [1, 2]}
        }


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_concurrent_matching_commits_apply_once(kind: str, tmp_path: Path) -> None:
    async with _store(kind, tmp_path) as store:
        write = _write()
        results = await asyncio.gather(
            store.commit(_session_batch("ses_first", write)),
            store.commit(_session_batch("ses_second", write)),
        )
        assert sorted(result.applied for result in results) == [False, True]
        assert await store.latest_cursor() == 1


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_scope_is_part_of_the_key_and_delete_removes_owned_records(
    kind: str,
    tmp_path: Path,
) -> None:
    async with _store(kind, tmp_path) as store:
        await store.commit(_session_batch("ses_first", _write()))
        second = _write(scope="other.scope", session_id="ses_second")
        await store.commit(_session_batch("ses_second", second))
        await store.delete_session("ses_first")
        assert await store.get_idempotency("session.create", "request-1") is None
        assert await store.get_idempotency("other.scope", "request-1") is not None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_matching_replay_checks_exact_replay_precondition(
    kind: str,
    tmp_path: Path,
) -> None:
    async with _store(kind, tmp_path) as store:
        write = _write()
        first = await store.commit(_session_batch("ses_first", write))
        original = await store.get_snapshot("session", "ses_first")
        assert original is not None
        changed = {**original, "version": 2}
        await store.commit(
            CommitBatch(
                events=(),
                snapshots=(SnapshotWrite("session", "ses_first", "ses_first", 2, changed),),
            )
        )
        before = await store.latest_cursor()
        replay_batch = _session_batch("ses_second", write)._replace(
            replay_preconditions=(
                SnapshotPrecondition(
                    "session", "ses_first", version=1, session_id="ses_first", data=original
                ),
            )
        )
        with pytest.raises(SnapshotPreconditionError):
            await store.commit(replay_batch)
        assert await store.latest_cursor() == before == first.last_cursor
        assert await store.get_snapshot("session", "ses_second") is None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_atomic_replay_miss_never_inserts(kind: str, tmp_path: Path) -> None:
    async with _store(kind, tmp_path) as store:
        replay = IdempotencyReplay(
            scope="session.create",
            key="missing",
            request_fingerprint="a" * 64,
        )
        with pytest.raises(IdempotencyReplayMissError):
            await store.commit(_session_batch("ses_new", replay))
        assert await store.latest_cursor() == 0
        assert await store.get_snapshot("session", "ses_new") is None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_replay_preconditions_require_idempotency(kind: str, tmp_path: Path) -> None:
    async with _store(kind, tmp_path) as store:
        with pytest.raises(IdempotencyValidationError):
            await store.commit(
                CommitBatch(
                    events=(),
                    replay_preconditions=(SnapshotPrecondition("session", "ses_1"),),
                )
            )


@pytest.mark.parametrize(
    "arguments",
    [
        {"value": float("inf")},
        {"value": b"bytes"},
        {1: "non-string-key"},
    ],
)
def test_fingerprint_rejects_non_json_arguments(arguments: dict[object, object]) -> None:
    with pytest.raises(IdempotencyValidationError):
        fingerprint_command("command", arguments)  # type: ignore[arg-type]


async def test_sqlite_replay_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "durable.db"
    store = await SQLiteStore.open(path)
    write = _write()
    first = await store.commit(_session_batch("ses_first", write))
    await store.close()

    reopened = await SQLiteStore.open(path)
    try:
        replay = await reopened.commit(_session_batch("ses_second", write))
        assert replay.applied is False
        assert replay.idempotency == first.idempotency
        assert await reopened.latest_cursor() == first.last_cursor
    finally:
        await reopened.close()


async def test_sqlite_corrupt_record_is_typed_and_sanitized(tmp_path: Path) -> None:
    store = await SQLiteStore.open(tmp_path / "corrupt.db")
    try:
        await store.commit(_session_batch("ses_first", _write()))
        await store._connection.execute(
            "UPDATE idempotency_records SET result_json = ?",
            ("not-json",),
        )
        await store._connection.commit()
        with pytest.raises(
            IdempotencyCorruptionError,
            match="stored idempotency record is invalid",
        ) as captured:
            await store.get_idempotency("session.create", "request-1")
        assert "not-json" not in str(captured.value)
    finally:
        await store.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_read_hint_then_delete_causes_atomic_replay_miss(
    kind: str,
    tmp_path: Path,
) -> None:
    async with _store(kind, tmp_path) as store:
        write = _write()
        await store.commit(_session_batch("ses_first", write))
        assert await store.get_idempotency(write.scope, write.key) is not None
        await store.delete_session("ses_first")
        replay = IdempotencyReplay(write.scope, write.key, write.request_fingerprint)
        with pytest.raises(IdempotencyReplayMissError):
            await store.commit(_session_batch("ses_second", replay))
        assert await store.latest_cursor() == 1
        assert await store.get_snapshot("session", "ses_second") is None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_cancelled_waiting_commit_propagates_without_mutation(
    kind: str,
    tmp_path: Path,
) -> None:
    async with _store(kind, tmp_path) as store:
        lock = store._lock  # type: ignore[attr-defined]
        await lock.acquire()
        task = asyncio.create_task(store.commit(_session_batch("ses_first", _write())))
        await asyncio.sleep(0)
        task.cancel()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await store.latest_cursor() == 0


async def test_two_sqlite_connections_arbitrate_same_key_once(tmp_path: Path) -> None:
    path = tmp_path / "two-connections.db"
    first_store = await SQLiteStore.open(path)
    second_store = await SQLiteStore.open(path)
    try:
        write = _write()
        first, second = await asyncio.gather(
            first_store.commit(_session_batch("ses_first", write)),
            second_store.commit(_session_batch("ses_second", write)),
        )
        assert sorted((first.applied, second.applied)) == [False, True]
        assert await first_store.latest_cursor() == 1
        assert await second_store.latest_cursor() == 1
    finally:
        await first_store.close()
        await second_store.close()


async def test_sqlite_replay_and_delete_linearize_without_resurrection(tmp_path: Path) -> None:
    path = tmp_path / "replay-delete.db"
    first_store = await SQLiteStore.open(path)
    second_store = await SQLiteStore.open(path)
    try:
        write = _write()
        await first_store.commit(_session_batch("ses_first", write))
        replay = IdempotencyReplay(write.scope, write.key, write.request_fingerprint)
        replay_batch = _session_batch("ses_second", replay)._replace(
            replay_preconditions=(
                SnapshotPrecondition("session", "ses_first", version=1),
            )
        )
        results = await asyncio.gather(
            first_store.commit(replay_batch),
            second_store.delete_session("ses_first"),
            return_exceptions=True,
        )
        replay_result = results[0]
        assert (
            getattr(replay_result, "applied", None) is False
            or isinstance(replay_result, (IdempotencyReplayMissError, SnapshotPreconditionError))
        )
        assert await first_store.get_snapshot("session", "ses_second") is None
        assert await first_store.get_idempotency(write.scope, write.key) is None
    finally:
        await first_store.close()
        await second_store.close()
