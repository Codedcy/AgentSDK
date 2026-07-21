from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from agent_sdk import AgentSDK, AgentSDKError, AgentSpec, PermissionDecision
from agent_sdk.runtime.models import RunStatus
from agent_sdk.runtime.reconciliation import ToolCallOperation
from agent_sdk.storage.base import CommitResult, RunProgressBatch
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools import ToolResultStatus
from agent_sdk.tools.builtins import files as builtin_files
from agent_sdk.tools.models import ToolContext, ToolSpec, thaw_json


def _tool_stream(
    *,
    name: str,
    arguments: dict[str, object],
) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_builtin_recovery",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    return generate()


def _text_stream(text: str) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {"content": text},
                    "finish_reason": "stop",
                }
            ]
        }

    return generate()


@pytest.mark.asyncio
async def test_sqlite_reopen_restores_the_same_pending_builtin_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "pending.sqlite3"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "value.txt"
    target.write_text("durable", encoding="utf-8")
    spec = AgentSpec(name="pending-built-in", model="fake/pending-built-in")
    read_calls = 0
    original_read_prefix = builtin_files._read_prefix

    def counted_read_prefix(path: Path, limit: int) -> tuple[bytes, bool]:
        nonlocal read_calls
        if path == target.resolve():
            read_calls += 1
        return original_read_prefix(path, limit)

    monkeypatch.setattr(builtin_files, "_read_prefix", counted_read_prefix)

    async def first_model(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _tool_stream(name="read", arguments={"path": "value.txt"})

    first = AgentSDK.for_test(
        database_path=database,
        acompletion=first_model,
        permission_default="ask",
    )
    first.agents.define(spec)
    session = await first.sessions.create(workspaces=(workspace,))
    handle = await first.runs.start(session.session_id, spec, "read it")
    original_request = await asyncio.wait_for(
        first.permissions.next_request(handle.run_id),
        timeout=2,
    )
    assert original_request.arguments["path"] == str(target.resolve())
    handle._task.cancel()  # type: ignore[attr-defined]
    with pytest.raises(AgentSDKError):
        await handle.result()
    await first.close()

    reopened_model_calls = 0

    async def reopened_model(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal reopened_model_calls
        reopened_model_calls += 1
        return _text_stream("done")

    reopened = AgentSDK.for_test(
        database_path=database,
        acompletion=reopened_model,
        permission_default="ask",
    )
    reopened.agents.define(spec)
    async def unrelated_tool(_: ToolContext) -> dict[str, bool]:
        return {"ok": True}

    reopened.tools.register(
        ToolSpec(
            name="registered_after_run_creation",
            description="unrelated",
            input_schema={"type": "object", "additionalProperties": False},
            source="test",
            effects=(),
        ),
        unrelated_tool,
    )
    try:
        await reopened.recovery.scan()
        assert (await reopened.runs.get(handle.run_id)).status is RunStatus.INTERRUPTED
        recovered = await reopened.recovery.recover_run(handle.run_id)
        restored_request = await asyncio.wait_for(
            reopened.permissions.next_request(handle.run_id),
            timeout=2,
        )
        assert restored_request == original_request
        recovered._task.cancel()  # type: ignore[attr-defined]
        with pytest.raises(AgentSDKError):
            await recovered.result()
    finally:
        await reopened.close()

    second_reopen = AgentSDK.for_test(
        database_path=database,
        acompletion=reopened_model,
        permission_default="ask",
    )
    second_reopen.agents.define(spec)
    try:
        await second_reopen.recovery.scan()
        assert (
            await second_reopen.runs.get(handle.run_id)
        ).status is RunStatus.INTERRUPTED
        recovered_again = await second_reopen.recovery.recover_run(handle.run_id)
        restored_again = await asyncio.wait_for(
            second_reopen.permissions.next_request(handle.run_id),
            timeout=2,
        )
        assert restored_again == original_request
        await second_reopen.permissions.resolve(
            restored_again.request_id,
            PermissionDecision.allow_once(),
        )

        result = await asyncio.wait_for(recovered_again.result(), timeout=5)
        assert result.output_text == "done"
        assert result.tool_results[0].status is ToolResultStatus.SUCCEEDED
        assert thaw_json(result.tool_results[0].value)["content"] == "durable"
        assert reopened_model_calls == 1
        assert read_calls == 1
    finally:
        await second_reopen.close()


@pytest.mark.asyncio
async def test_sqlite_reopen_reuses_completed_builtin_result_without_io_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completed.sqlite3"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "value.txt"
    target.write_text("before", encoding="utf-8")
    spec = AgentSpec(name="completed-built-in", model="fake/completed-built-in")
    store = await SQLiteStore.open(database)

    async def first_model(**_: Any) -> AsyncIterator[dict[str, object]]:
        return _tool_stream(name="read", arguments={"path": "value.txt"})

    original_commit = store.commit_run_progress
    completed_committed = False

    async def fail_after_completed_tool(
        batch: RunProgressBatch,
    ) -> CommitResult:
        nonlocal completed_committed
        if completed_committed:
            raise RuntimeError("private simulated process loss")
        result = cast(CommitResult, await original_commit(batch))
        if any(event.type == "tool.call.completed" for event in batch.events):
            completed_committed = True
            raise RuntimeError("private ambiguous completion response")
        return result

    store.commit_run_progress = fail_after_completed_tool
    first = AgentSDK.for_test(
        store=store,
        acompletion=first_model,
        permission_default="ask",
    )
    first.agents.define(spec)
    session = await first.sessions.create(workspaces=(workspace,))
    handle = await first.runs.start(session.session_id, spec, "read once")
    request = await asyncio.wait_for(
        first.permissions.next_request(handle.run_id),
        timeout=2,
    )
    await first.permissions.resolve(request.request_id, PermissionDecision.allow_once())
    with pytest.raises(AgentSDKError):
        await asyncio.wait_for(handle.result(), timeout=5)
    assert completed_committed
    operations = await store.list_external_operations(handle.run_id)
    tool_operation = next(
        operation
        for operation in operations
        if isinstance(operation, ToolCallOperation)
    )
    assert dict(tool_operation.recovery_metadata) == {
        "safe_retry": False,
        "retry_class": "unsafe",
        "permission_arguments": {"path": str(target.resolve())},
    }
    assert "before" not in repr(tool_operation.recovery_metadata)
    store.commit_run_progress = original_commit
    await first.close()
    await store.close()

    target.unlink()
    reopened_model_calls = 0

    async def reopened_model(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal reopened_model_calls
        reopened_model_calls += 1
        return _text_stream("done")

    reopened = AgentSDK.for_test(
        database_path=database,
        acompletion=reopened_model,
        permission_default="ask",
    )
    reopened.agents.define(spec)
    try:
        await reopened.recovery.scan()
        assert (await reopened.runs.get(handle.run_id)).status is RunStatus.INTERRUPTED
        result = await asyncio.wait_for(
            (await reopened.recovery.recover_run(handle.run_id)).result(),
            timeout=5,
        )
        assert result.output_text == "done"
        assert result.tool_results[0].status is ToolResultStatus.SUCCEEDED
        assert thaw_json(result.tool_results[0].value)["content"] == "before"
        assert reopened_model_calls == 1
    finally:
        await reopened.close()
