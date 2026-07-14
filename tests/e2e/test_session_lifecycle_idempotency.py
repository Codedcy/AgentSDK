from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    CompactionLevel,
    ErrorCode,
    EvaluationVerdict,
    EventFilter,
    ExactOutputEvaluator,
    ToolContext,
    ToolSpec,
    WorkflowDefinition,
)


WORKFLOW = WorkflowDefinition.model_validate(
    {
        "api_version": "agent-sdk/v1",
        "kind": "Workflow",
        "name": "descriptor-proof",
        "nodes": [
            {
                "id": "work",
                "kind": "agent",
                "agent_revision": "worker:1",
                "input": "complete workflow",
            }
        ],
        "edges": [],
    }
)

TOOL = ToolSpec(
    name="inspect",
    description="Inspect application state",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
    },
    version="1",
    source="application",
    effects=("read",),
    timeout_seconds=3,
)


def _text_stream(text: str) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {"delta": {"content": text}, "finish_reason": "stop"}
            ]
        }
        yield {
            "choices": [],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    return generate()


@pytest.mark.asyncio
async def test_session_run_lifecycle_replays_after_sqlite_reopen(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_file = workspace / "application.txt"
    workspace_file.write_text("application-owned", encoding="utf-8")
    agent = AgentSpec(name="main", model="fake/main", revision="1")
    worker = AgentSpec(name="worker", model="fake/worker", revision="1")
    provider_calls: dict[str, int] = {
        "fake/main": 0,
        "fake/worker": 0,
        "fake/context": 0,
    }
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def tool_handler(_: ToolContext, **values: object) -> object:
        return values

    async def script(**params: Any) -> object:
        model = str(params["model"])
        provider_calls[model] += 1
        if params["stream"] is False:
            source_document = json.loads(params["messages"][1]["content"])
            source_ids = [
                item["event_id"] for item in source_document["sources"]
            ]
            return {
                "choices": [
                    {
                        "message": {
                            "parsed": {
                                "objective": "retain acceptance evidence",
                                "constraints": ["preserve workspace"],
                                "decisions": ["delete durable session state"],
                                "facts": ["workflow completed"],
                                "next_actions": ["verify cursor high-water"],
                                "artifact_refs": ["workspace:application.txt"],
                                "source_event_ids": source_ids,
                            }
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }
        if model == "fake/main":
            provider_started.set()
            await release_provider.wait()
        return _text_stream("done")

    first = AgentSDK.for_test(database_path=database, acompletion=script)
    first.agents.define(worker)
    first.tools.register(TOOL, tool_handler)
    try:
        session = await first.sessions.create(
            workspaces=[workspace],
            idempotency_key="create-session",
        )
        workflow_handle = await first.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="start-workflow",
        )
        assert (await workflow_handle.result()).output_text == "done"
        view = await first.context.build(
            session.session_id,
            model="fake/context",
            model_window=8_192,
            force_level=CompactionLevel.L3,
        )
        assert view.capsule_id is not None
        run_handles = await asyncio.gather(
            *(
                first.runs.start(
                    session.session_id,
                    agent,
                    "main",
                    idempotency_key="start-main",
                )
                for _ in range(16)
            )
        )
        assert len({handle.run_id for handle in run_handles}) == 1
        await asyncio.wait_for(provider_started.wait(), timeout=5)
        assert (await first.sessions.close(session.session_id)).status == "closing"
        release_provider.set()
        results = await asyncio.gather(*(handle.result() for handle in run_handles))
        assert {result.output_text for result in results} == {"done"}
        assert provider_calls == {
            "fake/main": 1,
            "fake/worker": 1,
            "fake/context": 1,
        }
        evaluation = await first.evaluations.evaluate(
            run_handles[0].run_id,
            ExactOutputEvaluator(expected="done"),
        )
        assert evaluation.verdict is EvaluationVerdict.PASS
        success = await first.analytics.success_rate(evaluator_id="exact_output")
        assert success.value == 1.0
        assert success.sample_count == 1
        assert (await first.sessions.get(session.session_id)).status == "closed"
    finally:
        release_provider.set()
        await first.close()

    reopen_calls = 0

    async def must_not_call(**_: Any) -> object:
        nonlocal reopen_calls
        reopen_calls += 1
        raise AssertionError("durable replay must not execute the provider")

    reopened = AgentSDK.for_test(database_path=database, acompletion=must_not_call)
    reopened.agents.define(worker)
    reopened.tools.register(TOOL, tool_handler)
    try:
        same = await reopened.sessions.create(
            workspaces=[workspace],
            idempotency_key="create-session",
        )
        assert same.session_id == session.session_id
        replayed = await reopened.runs.start(
            session.session_id,
            agent,
            "main",
            idempotency_key="start-main",
        )
        assert replayed.run_id == run_handles[0].run_id
        assert (await replayed.result()).output_text == "done"
        replayed_workflow = await reopened.workflows.start(
            session.session_id,
            WORKFLOW,
            idempotency_key="start-workflow",
        )
        assert replayed_workflow.workflow_run_id == workflow_handle.workflow_run_id
        assert (await replayed_workflow.result()).output_text == "done"

        run_snapshot = await reopened.runs.get(replayed.run_id)
        run_descriptor = run_snapshot.execution_descriptor
        assert run_snapshot.execution_compatibility == "current"
        assert run_descriptor is not None
        assert run_descriptor.agent.model_dump(mode="json") == agent.model_dump(
            mode="json"
        )
        assert [capability.spec for capability in run_descriptor.tools] == [TOOL]
        assert len(run_descriptor.agent_hash) == 64
        assert len(run_descriptor.tools[0].capability_hash) == 64
        assert len(run_descriptor.policy.policy_hash) == 64
        run_descriptor_json = json.dumps(run_descriptor.model_dump(mode="json"))
        assert "handler" not in run_descriptor_json
        assert "credential" not in run_descriptor_json

        workflow_snapshot = await reopened.workflows.get(
            replayed_workflow.workflow_run_id
        )
        workflow_descriptor = workflow_snapshot.execution_descriptor
        assert workflow_snapshot.execution_compatibility == "current"
        assert workflow_descriptor is not None
        assert workflow_descriptor.workflow_definition_hash == (
            workflow_snapshot.workflow.definition_hash
        )
        assert [capability.spec for capability in workflow_descriptor.tools] == [TOOL]
        assert workflow_descriptor.agents[0].execution.agent.model_dump(
            mode="json"
        ) == worker.model_dump(mode="json")
        assert len(workflow_descriptor.agents[0].execution.agent_hash) == 64
        assert len(workflow_descriptor.tools[0].capability_hash) == 64
        assert len(workflow_descriptor.policy.policy_hash) == 64
        workflow_descriptor_json = json.dumps(
            workflow_descriptor.model_dump(mode="json")
        )
        assert "handler" not in workflow_descriptor_json
        assert "credential" not in workflow_descriptor_json

        conflict_calls = 0

        async def conflict_provider(**_: Any) -> object:
            nonlocal conflict_calls
            conflict_calls += 1
            raise AssertionError("capability conflict must not execute provider")

        changed = AgentSDK.for_test(
            database_path=database,
            acompletion=conflict_provider,
        )
        changed.agents.define(worker)
        changed.tools.register(TOOL.model_copy(update={"version": "2"}), tool_handler)
        try:
            with pytest.raises(AgentSDKError) as conflict:
                await changed.workflows.start(
                    session.session_id,
                    WORKFLOW,
                    idempotency_key="start-workflow",
                )
            assert conflict.value.code is ErrorCode.CONFLICT
            assert conflict_calls == 0
        finally:
            await changed.close()

        capsule = await reopened.context.get_capsule(
            view.capsule_id,
            session_id=session.session_id,
        )
        sources = await reopened.context.read_sources(
            view.capsule_id,
            session_id=session.session_id,
        )
        assert capsule.source_event_ids == tuple(
            source.event.event_id for source in sources
        )
        reopened_success = await reopened.analytics.success_rate(
            evaluator_id="exact_output"
        )
        assert reopened_success.value == 1.0
        assert reopened_success.sample_count == 1
        session_events = await reopened.queries.query_events(
            EventFilter(session_id=session.session_id),
            limit=100,
        )
        assert session_events.events
        cursor_high_water = max(event.cursor for event in session_events.events)

        with sqlite3.connect(database) as connection:
            snapshot_kinds = {
                row[0]
                for row in connection.execute(
                    "SELECT kind FROM snapshots WHERE session_id = ?",
                    (session.session_id,),
                )
            }
            assert {
                "session",
                "run",
                "workflow",
                "workflow_node",
                "context_capsule",
                "context_view",
                "evaluation",
            } <= snapshot_kinds
            assert connection.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0] > 0
            assert connection.execute(
                "SELECT COUNT(*) FROM idempotency_records WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0] == 3

        assert reopen_calls == 0
        await reopened.sessions.delete(session.session_id)
        assert workspace_file.read_text(encoding="utf-8") == "application-owned"
        with pytest.raises(AgentSDKError) as missing_session:
            await reopened.sessions.get(session.session_id)
        assert missing_session.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(AgentSDKError) as missing_run:
            await reopened.runs.get(replayed.run_id)
        assert missing_run.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(AgentSDKError) as missing_workflow:
            await reopened.workflows.get(replayed_workflow.workflow_run_id)
        assert missing_workflow.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(AgentSDKError) as missing_capsule:
            await reopened.context.get_capsule(
                view.capsule_id,
                session_id=session.session_id,
            )
        assert missing_capsule.value.code is ErrorCode.NOT_FOUND
        deleted_events = await reopened.queries.query_events(
            EventFilter(session_id=session.session_id)
        )
        assert deleted_events.events == ()
        deleted_success = await reopened.analytics.success_rate(
            evaluator_id="exact_output"
        )
        assert deleted_success.value is None
        assert deleted_success.sample_count == 0

        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM snapshots WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM idempotency_records WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0] == 0

        replacement = await reopened.sessions.create(
            workspaces=[workspace],
            idempotency_key="create-session",
        )
        assert replacement.session_id != session.session_id
        replacement_events = await reopened.queries.query_events(
            EventFilter(session_id=replacement.session_id)
        )
        assert len(replacement_events.events) == 1
        assert replacement_events.events[0].cursor > cursor_high_water
        assert workspace_file.read_text(encoding="utf-8") == "application-owned"
        await reopened.sessions.close(replacement.session_id)
        await reopened.sessions.delete(replacement.session_id)
        assert reopen_calls == 0
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_retained_deleting_session_resumes_real_sqlite_cleanup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "retained-deleting.db"
    workspace = tmp_path / "retained-workspace"
    workspace.mkdir()
    workspace_file = workspace / "application.txt"
    workspace_file.write_text("application-owned", encoding="utf-8")

    async def provider(**_: Any) -> object:
        return _text_stream("done")

    sdk = AgentSDK.for_test(database_path=database, acompletion=provider)
    try:
        session = await sdk.sessions.create(
            workspaces=[workspace],
            idempotency_key="retained-session",
        )
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="retained", model="fake/retained", revision="1"),
            "finish before deletion",
            idempotency_key="retained-run",
        )
        assert (await run.result()).output_text == "done"
        assert (await sdk.sessions.close(session.session_id)).status == "closed"

        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TRIGGER fail_session_cleanup "
                "BEFORE DELETE ON snapshots "
                "BEGIN SELECT RAISE(ABORT, 'injected cleanup failure'); END"
            )
            connection.commit()

        with pytest.raises(AgentSDKError) as cleanup_failed:
            await sdk.sessions.delete(session.session_id)
        assert cleanup_failed.value.code is ErrorCode.INTERNAL
        assert cleanup_failed.value.__cause__ is None
        assert cleanup_failed.value.__context__ is None

        for operation in (
            sdk.sessions.get(session.session_id),
            sdk.sessions.close(session.session_id),
        ):
            with pytest.raises(AgentSDKError) as deleting:
                await operation
            assert deleting.value.code is ErrorCode.INVALID_STATE
            assert deleting.value.message == "session is deleting"

        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT data_json FROM snapshots "
                "WHERE kind = 'session' AND entity_id = ?",
                (session.session_id,),
            ).fetchone()
            assert row is not None
            assert json.loads(row[0])["status"] == "deleting"
            assert connection.execute(
                "SELECT COUNT(*) FROM snapshots WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0] > 1
            assert connection.execute(
                "SELECT COUNT(*) FROM idempotency_records WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0] == 2
            connection.execute("DROP TRIGGER fail_session_cleanup")
            connection.commit()

        await sdk.sessions.delete(session.session_id)
        with sqlite3.connect(database) as connection:
            for table in ("snapshots", "events", "idempotency_records"):
                assert connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id = ?",  # noqa: S608
                    (session.session_id,),
                ).fetchone()[0] == 0
        assert workspace_file.read_text(encoding="utf-8") == "application-owned"
    finally:
        await sdk.close()
