from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ProviderRecoveryAdapter,
    ProviderRecoveryDisposition,
    ProviderRecoveryRequest,
    ProviderRecoveryResult,
    ToolRetryPolicy,
    ToolSpec,
    TokenUsage,
)
from agent_sdk.runtime.engine import _model_request_fingerprint
from agent_sdk.runtime import reconciliation
from agent_sdk.runtime.reconciliation import ModelCallOperation
from agent_sdk.storage.base import SnapshotWrite, StateStore
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.tools.models import ToolContext, thaw_json


_GENERAL_SYSTEM_PROMPT = (
    resources.files("agent_sdk.prompts.profiles")
    .joinpath("general", "system.md")
    .read_text(encoding="utf-8")
)


@pytest.mark.asyncio
async def test_in_flight_model_operation_stores_exact_prepared_request() -> None:
    store = InMemoryStore()
    accepted = asyncio.Event()
    release = asyncio.Event()
    observed: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> AsyncIterator[dict[str, object]]:
        observed.append(kwargs)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            accepted.set()
            await release.wait()
            yield {
                "choices": [
                    {"delta": {"content": "done"}, "finish_reason": "stop"}
                ]
            }

        return chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=provider,
        enable_builtin_tools=False,
    )
    handle = None
    try:
        session = await sdk.sessions.create(workspaces=[])
        handle = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="recoverable", model="test/model"),
            "Persist this exact request.",
        )
        await asyncio.wait_for(accepted.wait(), timeout=2)

        operations = await store.list_unresolved_external_operations(handle.run_id)
        assert len(operations) == 1
        operation = operations[0]
        assert isinstance(operation, ModelCallOperation)
        assert operation.context_view_id is not None
        assert operation.prompt_manifest_id is not None
        assert operation.prepared_request is not None
        prepared = thaw_json(operation.prepared_request)
        assert isinstance(prepared, dict)
        request = reconciliation.deserialize_model_request(prepared)
        assert request.messages == tuple(observed[0]["messages"])
        assert request.messages == (
            {"role": "system", "content": _GENERAL_SYSTEM_PROMPT},
            {"role": "user", "content": "Persist this exact request."},
        )
        assert request.tools == tuple(observed[0]["tools"])
        assert _model_request_fingerprint(request) == operation.request_fingerprint

        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        started = next(
            item.event
            for item in events
            if item.event.type == "model.call.started"
        )
        public_payload = started.model_dump_json()
        assert started.payload == {
            "model": "test/model",
            "context_view_id": operation.context_view_id,
            "prompt_manifest_id": operation.prompt_manifest_id,
            "request_fingerprint": operation.request_fingerprint,
            "operation_id": operation.operation_id,
            "step_id": operation.operation_id,
        }
        assert started.schema_version == 2
        assert "Persist this exact request." not in public_payload
        assert _GENERAL_SYSTEM_PROMPT not in public_payload
    finally:
        release.set()
        if handle is not None:
            await handle.result()
        await sdk.close()


@pytest.mark.asyncio
async def test_reopen_reuses_in_flight_prepared_request_without_new_context() -> None:
    store = InMemoryStore()
    accepted = asyncio.Event()

    async def hanging_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            accepted.set()
            await asyncio.Event().wait()
            yield {"choices": []}

        return chunks()

    spec = AgentSpec(name="recoverable", model="test/model")
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=hanging_provider,
        enable_builtin_tools=False,
    )
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(
        session.session_id,
        spec,
        "Crash after model acceptance.",
    )
    await asyncio.wait_for(accepted.wait(), timeout=2)
    original = (await store.list_unresolved_external_operations(handle.run_id))[0]
    assert isinstance(original, ModelCallOperation)
    events_before = await store.read_events(
        after_cursor=0,
        session_id=session.session_id,
    )
    counts_before = {
        event_type: sum(
            item.event.type == event_type for item in events_before
        )
        for event_type in (
            "context.view.created",
            "context.compaction.completed",
            "prompt.manifest.created",
            "model.call.started",
        )
    }
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle._task
    await sdk.close()

    provider_calls = 0

    async def must_not_call(**_: Any) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("unknown model outcome must not be replayed")

    reopened = AgentSDK.for_test(
        store=store,
        acompletion=must_not_call,
        enable_builtin_tools=False,
    )
    reopened.agents.define(spec)
    try:
        with pytest.raises(AgentSDKError, match="recovery required"):
            await (await reopened.recovery.recover_run(handle.run_id)).result()

        pending = await reopened.recovery.pending_requests(handle.run_id)
        assert len(pending) == 1
        assert pending[0].reason == "model_call_unknown_outcome"
        recovered = await store.get_external_operation(original.operation_id)
        assert recovered == original
        assert isinstance(recovered, ModelCallOperation)
        assert recovered.context_view_id == original.context_view_id
        assert recovered.prompt_manifest_id == original.prompt_manifest_id
        assert recovered.prepared_request == original.prepared_request
        assert recovered.request_fingerprint == original.request_fingerprint

        events_after = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        counts_after = {
            event_type: sum(
                item.event.type == event_type for item in events_after
            )
            for event_type in counts_before
        }
        assert counts_after == counts_before
        assert provider_calls == 0
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_authoritative_recovery_receives_exact_stored_prepared_request() -> None:
    store = InMemoryStore()
    accepted = asyncio.Event()
    observed: list[ProviderRecoveryRequest] = []

    async def hanging_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            accepted.set()
            await asyncio.Event().wait()
            yield {"choices": []}

        return chunks()

    async def query(
        request: ProviderRecoveryRequest,
    ) -> ProviderRecoveryResult:
        observed.append(request)
        return ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="recovered",
            usage=TokenUsage(
                prompt_tokens=3,
                completion_tokens=1,
                total_tokens=4,
            ),
        )

    adapter = ProviderRecoveryAdapter(
        provider_identity="test/model",
        adapter_id="test.authoritative",
        version="1",
        authoritative_status=True,
        same_operation_id_resend=False,
        query_status=query,
    )
    spec = AgentSpec(name="recoverable", model="test/model")
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=hanging_provider,
        enable_builtin_tools=False,
    )
    sdk.recovery.register_adapter(adapter)
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(
        session.session_id,
        spec,
        "Recover the exact prepared request.",
    )
    await asyncio.wait_for(accepted.wait(), timeout=2)
    original = (await store.list_unresolved_external_operations(handle.run_id))[0]
    assert isinstance(original, ModelCallOperation)
    assert original.prepared_request is not None
    exact_request = reconciliation.deserialize_model_request(
        original.prepared_request
    )
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle._task
    await sdk.close()

    async def must_not_call(**_: Any) -> object:
        raise AssertionError("certified recovery must not call LiteLLM")

    reopened = AgentSDK.for_test(
        store=store,
        acompletion=must_not_call,
        enable_builtin_tools=False,
    )
    reopened.agents.define(spec)
    reopened.recovery.register_adapter(adapter)
    try:
        await reopened.recovery.scan()
        result = await (
            await reopened.recovery.recover_run(handle.run_id)
        ).result()

        assert result.output_text == "recovered"
        assert len(observed) == 1
        assert observed[0].model_request == exact_request
        assert observed[0].request_fingerprint == original.request_fingerprint
        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        assert sum(
            item.event.type == "context.view.created" for item in events
        ) == 1
        assert sum(
            item.event.type == "prompt.manifest.created" for item in events
        ) == 1
        assert sum(
            item.event.type == "model.call.started" for item in events
        ) == 1
    finally:
        await reopened.close()


def _recovery_tool() -> ToolSpec:
    return ToolSpec(
        name="recovery_probe",
        description="Must remain side-effect free during reference rejection.",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )


async def _tamper_prepared_reference(
    store: InMemoryStore | SQLiteStore,
    operation: ModelCallOperation,
    corruption: str,
) -> None:
    assert operation.context_view_id is not None
    assert operation.prompt_manifest_id is not None
    target = (
        ("context_view", operation.context_view_id)
        if corruption.startswith("view_")
        else ("prompt_manifest", operation.prompt_manifest_id)
    )
    if isinstance(store, InMemoryStore):
        snapshot = store._snapshots[target]
        if corruption.endswith("_missing"):
            del store._snapshots[target]
            return
        data = dict(snapshot.data)
        session_id = snapshot.session_id
        if corruption.endswith("_owner"):
            session_id = "ses_other"
        elif corruption == "view_identity":
            data["view_id"] = "view_other"
        elif corruption == "manifest_identity":
            data["manifest_id"] = "pmf_other"
        elif corruption == "manifest_link":
            data["context_view_id"] = "view_other"
        elif corruption == "manifest_tools_sha256":
            data["tools_sha256"] = "f" * 64
        elif corruption == "manifest_sha256":
            data["sha256"] = "f" * 64
        elif corruption == "manifest_layer_sha256":
            data["layers"][0]["sha256"] = "f" * 64
        elif corruption == "manifest_layer_version":
            data["layers"][0]["version"] = "tampered"
        elif corruption == "manifest_layer_order":
            data["layers"] = list(reversed(data["layers"]))
        elif corruption == "view_level":
            data["recommended_level"] = "L1"
            data["applied_level"] = "L1"
        elif corruption == "view_refs":
            data["source_refs"] = [*data["source_refs"], "evt_tampered"]
        elif corruption == "view_transformations":
            data["transformations"] = [
                *data["transformations"],
                "tampered:evt_tampered",
            ]
        elif corruption == "view_consumed_message_ids":
            data["consumed_message_ids"] = ["msg_tampered"]
        elif corruption == "view_budget":
            budget = data["budget"]
            budget["output_reserve"] += 1
            budget["available_input_tokens"] -= 1
            budget["watermark_ratio"] = (
                budget["projected_source_tokens"]
                / budget["available_input_tokens"]
            )
        else:
            raise AssertionError(f"unknown corruption: {corruption}")
        store._snapshots[target] = SnapshotWrite(
            snapshot.kind,
            snapshot.entity_id,
            session_id,
            snapshot.version,
            data,
        )
        return

    snapshot_data = (
        await store.get_snapshot(*target)
        if not (
            corruption.endswith("_missing")
            or corruption.endswith("_owner")
        )
        else None
    )
    async with store._lock:
        if corruption.endswith("_missing"):
            await store._connection.execute(
                "DELETE FROM snapshots WHERE kind = ? AND entity_id = ?",
                target,
            )
        elif corruption.endswith("_owner"):
            await store._connection.execute(
                """
                UPDATE snapshots SET session_id = ?
                WHERE kind = ? AND entity_id = ?
                """,
                ("ses_other", *target),
            )
        else:
            assert snapshot_data is not None
            if corruption == "view_identity":
                snapshot_data["view_id"] = "view_other"
            elif corruption == "manifest_identity":
                snapshot_data["manifest_id"] = "pmf_other"
            elif corruption == "manifest_link":
                snapshot_data["context_view_id"] = "view_other"
            elif corruption == "manifest_tools_sha256":
                snapshot_data["tools_sha256"] = "f" * 64
            elif corruption == "manifest_sha256":
                snapshot_data["sha256"] = "f" * 64
            elif corruption == "manifest_layer_sha256":
                snapshot_data["layers"][0]["sha256"] = "f" * 64
            elif corruption == "manifest_layer_version":
                snapshot_data["layers"][0]["version"] = "tampered"
            elif corruption == "manifest_layer_order":
                snapshot_data["layers"] = list(
                    reversed(snapshot_data["layers"])
                )
            elif corruption == "view_level":
                snapshot_data["recommended_level"] = "L1"
                snapshot_data["applied_level"] = "L1"
            elif corruption == "view_refs":
                snapshot_data["source_refs"] = [
                    *snapshot_data["source_refs"],
                    "evt_tampered",
                ]
            elif corruption == "view_transformations":
                snapshot_data["transformations"] = [
                    *snapshot_data["transformations"],
                    "tampered:evt_tampered",
                ]
            elif corruption == "view_consumed_message_ids":
                snapshot_data["consumed_message_ids"] = ["msg_tampered"]
            elif corruption == "view_budget":
                budget = snapshot_data["budget"]
                budget["output_reserve"] += 1
                budget["available_input_tokens"] -= 1
                budget["watermark_ratio"] = (
                    budget["projected_source_tokens"]
                    / budget["available_input_tokens"]
                )
            else:
                raise AssertionError(f"unknown corruption: {corruption}")
            await store._connection.execute(
                """
                UPDATE snapshots SET data_json = ?
                WHERE kind = ? AND entity_id = ?
                """,
                (
                    json.dumps(
                        snapshot_data,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    *target,
                ),
            )
        await store._connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "corruption",
    [
        "view_missing",
        "manifest_missing",
        "view_owner",
        "manifest_owner",
        "view_identity",
        "manifest_identity",
        "manifest_link",
        "manifest_tools_sha256",
        "manifest_sha256",
        "manifest_layer_sha256",
        "manifest_layer_version",
        "manifest_layer_order",
        "view_level",
        "view_refs",
        "view_transformations",
        "view_consumed_message_ids",
        "view_budget",
    ],
)
async def test_recovery_rejects_unauthenticated_prepared_references(
    backend: str,
    corruption: str,
    tmp_path: Path,
) -> None:
    store: InMemoryStore | SQLiteStore = (
        InMemoryStore()
        if backend == "memory"
        else await SQLiteStore.open(
            tmp_path / f"prepared-ref-{corruption}.sqlite3"
        )
    )
    accepted = asyncio.Event()
    tool_calls = 0

    async def hanging_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            accepted.set()
            await asyncio.Event().wait()
            yield {"choices": []}

        return chunks()

    async def tool_handler(_: ToolContext) -> None:
        nonlocal tool_calls
        tool_calls += 1

    spec = AgentSpec(
        name="reference-auth",
        model="test/model",
        system_prompt="Application recovery constraints.",
    )
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=hanging_provider,
        enable_builtin_tools=False,
    )
    sdk.tools.register(_recovery_tool(), tool_handler)
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(
        session.session_id,
        spec,
        "Authenticate prepared references.",
    )
    await asyncio.wait_for(accepted.wait(), timeout=2)
    operation = (await store.list_unresolved_external_operations(handle.run_id))[0]
    assert isinstance(operation, ModelCallOperation)
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle._task
    await sdk.close()

    await _tamper_prepared_reference(store, operation, corruption)
    provider_calls = 0

    async def must_not_call_provider(**_: Any) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("invalid references must fail before provider recovery")

    reopened = AgentSDK.for_test(
        store=store,
        acompletion=must_not_call_provider,
        enable_builtin_tools=False,
    )
    reopened.agents.define(spec)
    reopened.tools.register(_recovery_tool(), tool_handler)
    try:
        with pytest.raises(AgentSDKError, match="recovery state conflict"):
            await reopened.recovery.recover_run(handle.run_id)
        assert provider_calls == 0
        assert tool_calls == 0
    finally:
        await reopened.close()
        if isinstance(store, SQLiteStore):
            await store.close()


class _SnapshotReadTrackingStore:
    def __init__(self, delegate: StateStore) -> None:
        self.delegate = delegate
        self.reads: list[tuple[str, str]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def get_snapshot(
        self,
        kind: str,
        entity_id: str,
    ) -> dict[str, Any] | None:
        self.reads.append((kind, entity_id))
        return await self.delegate.get_snapshot(kind, entity_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper_old_view", [False, True])
async def test_completed_model_recovery_authenticates_old_refs_and_adds_one_new_pair(
    tamper_old_view: bool,
) -> None:
    durable = InMemoryStore()
    store = _SnapshotReadTrackingStore(durable)
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def first_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_recovery_probe",
                                    "function": {
                                        "name": "recovery_probe",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }

        return chunks()

    async def blocking_tool(_: ToolContext) -> None:
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise

    tool = _recovery_tool().model_copy(
        update={"retry_policy": ToolRetryPolicy.SAFE_RETRY}
    )
    spec = AgentSpec(name="completed-ref-recovery", model="test/model")
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=first_provider,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    sdk.tools.register(tool, blocking_tool)
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(
        session.session_id,
        spec,
        "Complete the model, then recover the Tool.",
    )
    await asyncio.wait_for(handler_started.wait(), timeout=2)
    operations = await durable.list_external_operations(handle.run_id)
    old_model = next(
        operation
        for operation in operations
        if isinstance(operation, ModelCallOperation)
    )
    assert old_model.status is reconciliation.ExternalOperationStatus.COMPLETED
    assert old_model.context_view_id is not None
    assert old_model.prompt_manifest_id is not None
    assert handle._task is not None
    handle._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle._task
    await asyncio.wait_for(handler_cancelled.wait(), timeout=2)
    await sdk.close()

    scanner = AgentSDK.for_test(
        store=durable,
        acompletion=first_provider,
        enable_builtin_tools=False,
    )
    try:
        await scanner.recovery.scan()
    finally:
        await scanner.close()

    events_before = await durable.read_events(
        after_cursor=0,
        session_id=session.session_id,
    )
    old_pair_counts = {
        event_type: sum(item.event.type == event_type for item in events_before)
        for event_type in ("context.view.created", "prompt.manifest.created")
    }
    assert old_pair_counts == {
        "context.view.created": 1,
        "prompt.manifest.created": 1,
    }
    store.reads.clear()
    if tamper_old_view:
        del durable._snapshots[("context_view", old_model.context_view_id)]

    provider_calls = 0
    recovered_tool_calls = 0

    async def final_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {
                        "delta": {"content": "done"},
                        "finish_reason": "stop",
                    }
                ]
            }

        return chunks()

    async def recovered_tool(_: ToolContext) -> None:
        nonlocal recovered_tool_calls
        recovered_tool_calls += 1

    reopened = AgentSDK.for_test(
        store=store,
        acompletion=final_provider,
        permission_default="allow",
        enable_builtin_tools=False,
    )
    reopened.agents.define(spec)
    reopened.tools.register(tool, recovered_tool)
    try:
        if tamper_old_view:
            with pytest.raises(AgentSDKError, match="recovery state conflict"):
                await reopened.recovery.recover_run(handle.run_id)
            assert provider_calls == 0
            assert recovered_tool_calls == 0
            return
        result = await (await reopened.recovery.recover_run(handle.run_id)).result()
        assert result.output_text == "done"
        assert provider_calls == 1
        assert recovered_tool_calls == 1
        assert (
            "context_view",
            old_model.context_view_id,
        ) in store.reads
        assert (
            "prompt_manifest",
            old_model.prompt_manifest_id,
        ) in store.reads

        recovered_operations = await durable.list_external_operations(handle.run_id)
        recovered_models = tuple(
            operation
            for operation in recovered_operations
            if isinstance(operation, ModelCallOperation)
        )
        assert len(recovered_models) == 2
        assert recovered_models[0].operation_id == old_model.operation_id
        assert recovered_models[0].context_view_id == old_model.context_view_id
        assert (
            recovered_models[0].prompt_manifest_id
            == old_model.prompt_manifest_id
        )
        assert recovered_models[1].context_view_id != old_model.context_view_id
        assert (
            recovered_models[1].prompt_manifest_id
            != old_model.prompt_manifest_id
        )

        events_after = await durable.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        assert {
            event_type: sum(
                item.event.type == event_type for item in events_after
            )
            for event_type in old_pair_counts
        } == {
            "context.view.created": 2,
            "prompt.manifest.created": 2,
        }
    finally:
        await reopened.close()
