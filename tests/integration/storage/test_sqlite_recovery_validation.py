from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_sdk.events.models import EventEnvelope
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot
from agent_sdk.runtime.reconciliation import (
    ExternalOperationStatus,
    ModelCallOperation,
    ReconciliationRequest,
    RunCheckpoint,
    RunCheckpointPhase,
    ToolCallOperation,
)
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.sqlite import SQLiteStore


NOW = datetime(2026, 7, 14, 8, tzinfo=UTC)


async def _create_valid_database(path: Path) -> None:
    store = await SQLiteStore.open(path)
    session = SessionSnapshot(session_id="ses_1", workspaces=("workspace",))
    run = RunSnapshot(
        run_id="run_1",
        session_id=session.session_id,
        agent_revision="agent:1",
        status=RunStatus.CREATED,
        user_input="hello",
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope(
                    event_id="evt_session_created",
                    type="session.created",
                    session_id=session.session_id,
                    run_id=None,
                    sequence=1,
                    payload=session.model_dump(mode="json"),
                    occurred_at=NOW,
                ),
                EventEnvelope(
                    event_id="evt_run_created",
                    type="run.created",
                    session_id=run.session_id,
                    run_id=run.run_id,
                    sequence=1,
                    payload=run.model_dump(mode="json"),
                    occurred_at=NOW,
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    kind="session",
                    entity_id=session.session_id,
                    session_id=session.session_id,
                    version=session.version,
                    data=session.model_dump(mode="json"),
                ),
                SnapshotWrite(
                    kind="run",
                    entity_id=run.run_id,
                    session_id=run.session_id,
                    version=run.version,
                    data=run.model_dump(mode="json"),
                ),
            ),
        )
    )
    lease = await store.acquire_lease(
        run_id="run_1",
        owner="worker_1",
        now=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    model_operation = ModelCallOperation(
        operation_id="op_model",
        session_id="ses_1",
        run_id="run_1",
        turn=0,
        request_fingerprint="sha256:model",
        lease_generation=lease.generation,
        status=ExternalOperationStatus.STARTED,
        provider_identity="provider:model",
    )
    tool_operation = ToolCallOperation(
        operation_id="op_tool",
        session_id="ses_1",
        run_id="run_1",
        turn=1,
        request_fingerprint="sha256:tool",
        lease_generation=lease.generation,
        status=ExternalOperationStatus.STARTED,
        tool_identity="tool:search",
    )
    checkpoint = RunCheckpoint(
        run_id="run_1",
        session_id="ses_1",
        checkpoint_version=1,
        turn=0,
        phase=RunCheckpointPhase.READY_FOR_MODEL,
        messages=({"role": "user", "content": "hello"},),
    )
    request = ReconciliationRequest(
        request_id="rec_1",
        session_id="ses_1",
        run_id="run_1",
        operation_id=model_operation.operation_id,
        reason="operation outcome is unknown",
    )
    try:
        await store.create_external_operation(
            model_operation, lease=lease, now=NOW
        )
        await store.create_external_operation(tool_operation, lease=lease, now=NOW)
        await store.put_run_checkpoint(
            checkpoint, expected=None, lease=lease, now=NOW
        )
        await store.create_reconciliation_request(request)
    finally:
        await store.close()


def _json_row(connection: sqlite3.Connection, table: str, key: str) -> dict[str, Any]:
    key_column = {
        "external_operations": "operation_id",
        "run_checkpoints": "run_id",
        "reconciliation_requests": "request_id",
    }[table]
    row = connection.execute(
        f"SELECT data_json FROM {table} WHERE {key_column} = ?", (key,)
    ).fetchone()
    assert row is not None
    decoded = json.loads(row[0])
    assert isinstance(decoded, dict)
    return decoded


def _persisted_recovery_rows(path: Path) -> tuple[tuple[Any, ...], ...]:
    with sqlite3.connect(path) as connection:
        return tuple(
            connection.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
            for table in (
                "external_operations",
                "run_checkpoints",
                "reconciliation_requests",
            )
        )


def _corrupt_database(path: Path, corruption: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        if corruption == "recovery_missing_run_snapshot":
            connection.execute(
                "DELETE FROM snapshots WHERE kind = 'run' AND entity_id = 'run_1'"
            )
            connection.execute("DELETE FROM events WHERE run_id = 'run_1'")
        elif corruption == "recovery_wrong_authoritative_session":
            for operation_id, data_json in connection.execute(
                "SELECT operation_id, data_json FROM external_operations"
            ).fetchall():
                data = json.loads(data_json)
                data["session_id"] = "ses_other"
                connection.execute(
                    "UPDATE external_operations SET session_id = 'ses_other', "
                    "data_json = ? WHERE operation_id = ?",
                    (
                        json.dumps(data, sort_keys=True, separators=(",", ":")),
                        operation_id,
                    ),
                )
            checkpoint = _json_row(connection, "run_checkpoints", "run_1")
            checkpoint["session_id"] = "ses_other"
            connection.execute(
                "UPDATE run_checkpoints SET session_id = 'ses_other', data_json = ? "
                "WHERE run_id = 'run_1'",
                (json.dumps(checkpoint, sort_keys=True, separators=(",", ":")),),
            )
            request = _json_row(connection, "reconciliation_requests", "rec_1")
            request["session_id"] = "ses_other"
            connection.execute(
                "UPDATE reconciliation_requests SET session_id = 'ses_other', "
                "data_json = ? WHERE request_id = 'rec_1'",
                (json.dumps(request, sort_keys=True, separators=(",", ":")),),
            )
        elif corruption.startswith("operation_"):
            data = _json_row(connection, "external_operations", "op_model")
            if corruption == "operation_terminal_missing_outcome":
                data["status"] = "completed"
                connection.execute(
                    "UPDATE external_operations SET status = ?, data_json = ? "
                    "WHERE operation_id = 'op_model'",
                    ("completed", json.dumps(data, sort_keys=True, separators=(",", ":"))),
                )
            elif corruption == "operation_wrong_identity_branch":
                data["provider_identity"] = None
                data["tool_identity"] = "tool:wrong"
                connection.execute(
                    "UPDATE external_operations SET provider_identity = NULL, "
                    "tool_identity = ?, data_json = ? WHERE operation_id = 'op_model'",
                    (
                        "tool:wrong",
                        json.dumps(data, sort_keys=True, separators=(",", ":")),
                    ),
                )
            elif corruption == "operation_extra_field":
                data["unexpected"] = True
                connection.execute(
                    "UPDATE external_operations SET data_json = ? "
                    "WHERE operation_id = 'op_model'",
                    (json.dumps(data, sort_keys=True, separators=(",", ":")),),
                )
            else:
                connection.execute(
                    "UPDATE external_operations SET data_json = '[]' "
                    "WHERE operation_id = 'op_model'"
                )
        elif corruption.startswith("checkpoint_"):
            data = _json_row(connection, "run_checkpoints", "run_1")
            if corruption == "checkpoint_forbidden_operation":
                data["operation_id"] = "op_model"
                connection.execute(
                    "UPDATE run_checkpoints SET operation_id = ?, data_json = ? "
                    "WHERE run_id = 'run_1'",
                    (
                        "op_model",
                        json.dumps(data, sort_keys=True, separators=(",", ":")),
                    ),
                )
            elif corruption == "checkpoint_wrong_operation_kind":
                data["phase"] = "tool_in_flight"
                data["operation_id"] = "op_model"
                connection.execute(
                    "UPDATE run_checkpoints SET phase = ?, operation_id = ?, "
                    "data_json = ? WHERE run_id = 'run_1'",
                    (
                        "tool_in_flight",
                        "op_model",
                        json.dumps(data, sort_keys=True, separators=(",", ":")),
                    ),
                )
            elif corruption == "checkpoint_cross_record_session":
                data["session_id"] = "ses_other"
                connection.execute(
                    "UPDATE run_checkpoints SET session_id = 'ses_other', "
                    "data_json = ? WHERE run_id = 'run_1'",
                    (json.dumps(data, sort_keys=True, separators=(",", ":")),),
                )
            else:
                data["checkpoint_version"] = 0
                connection.execute(
                    "UPDATE run_checkpoints SET checkpoint_version = 0, data_json = ? "
                    "WHERE run_id = 'run_1'",
                    (json.dumps(data, sort_keys=True, separators=(",", ":")),),
                )
        else:
            data = _json_row(connection, "reconciliation_requests", "rec_1")
            if corruption == "reconciliation_resolved_without_audit":
                data["status"] = "resolved"
                data["resolution"] = {
                    "action": "terminate",
                    "actor": {"type": "user"},
                    "evidence": {"reason": "unknown"},
                    "decided_at": NOW.isoformat(),
                    "event_id": "evt_missing",
                }
                connection.execute(
                    "UPDATE reconciliation_requests SET status = 'resolved', "
                    "data_json = ? WHERE request_id = 'rec_1'",
                    (json.dumps(data, sort_keys=True, separators=(",", ":")),),
                )
            elif corruption == "reconciliation_resolved_without_resolution":
                data["status"] = "resolved"
                connection.execute(
                    "UPDATE reconciliation_requests SET status = 'resolved', "
                    "data_json = ? WHERE request_id = 'rec_1'",
                    (json.dumps(data, sort_keys=True, separators=(",", ":")),),
                )
            elif corruption == "reconciliation_pending_with_resolution":
                data["resolution"] = {
                    "action": "terminate",
                    "actor": {"type": "user"},
                    "evidence": {"reason": "unknown"},
                    "decided_at": NOW.isoformat(),
                    "event_id": "evt_1",
                }
                connection.execute(
                    "UPDATE reconciliation_requests SET data_json = ? "
                    "WHERE request_id = 'rec_1'",
                    (json.dumps(data, sort_keys=True, separators=(",", ":")),),
                )
            else:
                data["session_id"] = "ses_other"
                data["run_id"] = "run_other"
                connection.execute(
                    "UPDATE reconciliation_requests SET session_id = 'ses_other', "
                    "run_id = 'run_other', data_json = ? WHERE request_id = 'rec_1'",
                    (json.dumps(data, sort_keys=True, separators=(",", ":")),),
                )


@pytest.mark.parametrize(
    "corruption",
    [
        "operation_terminal_missing_outcome",
        "operation_wrong_identity_branch",
        "operation_extra_field",
        "operation_non_object_json",
        "checkpoint_forbidden_operation",
        "checkpoint_wrong_operation_kind",
        "checkpoint_cross_record_session",
        "checkpoint_zero_version",
        "reconciliation_resolved_without_audit",
        "reconciliation_resolved_without_resolution",
        "reconciliation_pending_with_resolution",
        "reconciliation_cross_owner_operation",
        "recovery_missing_run_snapshot",
        "recovery_wrong_authoritative_session",
    ],
)
@pytest.mark.asyncio
async def test_sqlite_open_rejects_malformed_typed_v3_recovery_rows_without_mutation(
    corruption: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"malformed-{corruption}.db"
    await _create_valid_database(path)
    _corrupt_database(path, corruption)
    corrupted_rows = _persisted_recovery_rows(path)

    with pytest.raises(ValueError, match="incompatible"):
        await SQLiteStore.open(path)

    assert _persisted_recovery_rows(path) == corrupted_rows
