from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    ErrorCode,
    EvaluationVerdict,
    EventFilter,
    PermissionDecision,
    PermissionRequest,
    WorkflowRunStatus,
)
from examples.reference_cli.main import build_parser, run_application


WORKFLOW_YAML = """\
api_version: agent-sdk/v1
kind: Workflow
name: generated-verification
nodes:
  - id: plan
    kind: agent
    agent_revision: planner:1
    input: plan verification
  - id: verify
    kind: agent
    agent_revision: worker:1
    input: verify result.txt
    run_as: child
    success_criteria:
      - return verified
    evidence_refs:
      - workspace:result.txt
edges:
  - source: plan
    target: verify
"""


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
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        }

    return generate()


def _tool_stream(
    call_id: str,
    name: str,
    arguments: str,
) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {
                                    "name": name,
                                    "arguments": arguments,
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    return generate()


class ScriptedVerticalModel:
    def __init__(self) -> None:
        self.main_calls = 0
        self.total_calls = 0
        self.first_user_message = ""

    async def __call__(self, **params: Any) -> object:
        self.total_calls += 1
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
                                "objective": "complete the vertical slice",
                                "constraints": ["preserve durable evidence"],
                                "decisions": ["run the approved workflow"],
                                "facts": ["application and MCP tools succeeded"],
                                "next_actions": ["verify after reopen"],
                                "artifact_refs": ["workspace:result.txt"],
                                "source_event_ids": source_ids,
                            }
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                },
            }
        model = str(params["model"])
        if model == "fake/main":
            self.main_calls += 1
            if self.main_calls == 1:
                self.first_user_message = str(params["messages"][0]["content"])
                return _tool_stream(
                    "call_write",
                    "write_note",
                    '{"content":"hello"}',
                )
            if self.main_calls == 2:
                return _tool_stream(
                    "call_echo",
                    "mcp.demo.echo",
                    '{"text":"verified"}',
                )
            return _text_stream(WORKFLOW_YAML)
        if model == "fake/planner":
            return _text_stream("plan complete")
        if model == "fake/worker":
            return _text_stream("verified")
        raise AssertionError(f"unexpected model: {model}")


async def _record_session_until_evaluation(
    sdk: AgentSDK,
    session_id: str,
    records: list[dict[str, object]],
) -> None:
    async for item in sdk.events.subscribe(
        filters=EventFilter(session_id=session_id),
        cursor=0,
    ):
        records.append(
            {
                "cursor": item.cursor,
                "type": item.event.type,
                "run_id": item.event.run_id,
            }
        )
        if item.event.type == "evaluation.completed":
            return


@pytest.mark.asyncio
async def test_complete_vertical_slice_survives_reopen_and_delete(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).parents[2]
    mcp_fixture = repository_root / "tests" / "fixtures" / "mcp_server.py"
    skill_fixture = (
        repository_root / "tests" / "fixtures" / "skills" / "coding-demo"
    )
    database = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    expected_output = tmp_path / "expected-workflow.yaml"
    expected_output.write_text(WORKFLOW_YAML, encoding="utf-8")
    scripted = ScriptedVerticalModel()
    displayed: list[dict[str, object]] = []
    permission_names: list[str] = []
    approved_workflows: list[str] = []
    sdk = AgentSDK.for_test(database_path=database, acompletion=scripted)
    session = await sdk.sessions.create(workspaces=[workspace])
    session_monitor = asyncio.create_task(
        _record_session_until_evaluation(
            sdk,
            session.session_id,
            displayed,
        )
    )

    async def allow_and_record(
        request: PermissionRequest,
    ) -> PermissionDecision:
        permission_names.append(request.tool_name)
        return PermissionDecision.allow_once()

    async def approve_and_record(workflow: object) -> bool:
        approved_workflows.append(str(getattr(workflow, "definition_hash")))
        return True

    try:
        args = build_parser().parse_args(
            [
                "Write result.txt, call the MCP echo Tool, then return Workflow YAML",
                "--database",
                str(database),
                "--workspace",
                str(workspace),
                "--model",
                "fake/main",
                "--planner-model",
                "fake/planner",
                "--worker-model",
                "fake/worker",
                "--context-model",
                "gpt-4o-mini",
                "--model-window",
                "16384",
                "--skill-root",
                str(skill_fixture.parent),
                "--skill-name",
                "coding-demo",
                "--skill-resource",
                "references/checklist.md",
                "--mcp-command",
                sys.executable,
                "--mcp-arg",
                str(mcp_fixture),
                "--mcp-name",
                "demo",
                "--expected-output-file",
                str(expected_output),
            ]
        )
        application = await run_application(
            args,
            sdk=sdk,
            session_id=session.session_id,
            resolve_permission=allow_and_record,
            approve_workflow=approve_and_record,
            emit=displayed.append,
        )
        execution = application.execution
        view = application.context_view
        workflow = application.workflow
        evaluation = application.evaluation

        assert permission_names == ["write_note", "mcp.demo.echo"]
        assert len(approved_workflows) == 1
        assert (workspace / "result.txt").read_text(encoding="utf-8") == "hello"
        assert "# Coding Demo" in scripted.first_user_message
        assert "Confirm result.txt exists" in scripted.first_user_message
        assert view.capsule_id is not None
        capsule = await sdk.context.get_capsule(
            view.capsule_id,
            session_id=session.session_id,
        )
        assert application.prompt.manifest.context_view_id == view.view_id
        assert workflow is not None
        assert workflow.status is WorkflowRunStatus.COMPLETED
        assert workflow.nodes[1].run_id is not None
        assert workflow.nodes[1].node_id == "verify"
        tree = await sdk.queries.execution_tree(workflow.nodes[0].run_id or "")
        assert [node.snapshot.run_id for node in tree.nodes] == [
            workflow.nodes[0].run_id,
            workflow.nodes[1].run_id,
        ]
        assert evaluation is not None
        assert evaluation.verdict is EvaluationVerdict.PASS
        assert application.success_rate.value == 1.0
        assert application.success_rate.sample_count == 1
        assert application.tool_failures.value == 0.0
        assert application.tool_failures.sample_count == 2
        await asyncio.wait_for(session_monitor, timeout=5)
        event_types = {str(record["type"]) for record in displayed}
        assert {
            "permission.requested",
            "permission.resolved",
            "tool.call.completed",
            "context.compaction.completed",
            "workflow.node.started",
            "workflow.node.completed",
            "model.usage.reported",
            "evaluation.completed",
        } <= event_types
        child_run_id = workflow.nodes[1].run_id
        assert any(
            record["type"] == "run.created" and record["run_id"] == child_run_id
            for record in displayed
        )
        workspace_mtime = (workspace / "result.txt").stat().st_mtime_ns
    finally:
        if not session_monitor.done():
            session_monitor.cancel()
        with suppress(BaseException):
            await session_monitor
        await sdk.close()

    reopen_calls = 0

    async def unused_after_reopen(**_: object) -> object:
        nonlocal reopen_calls
        reopen_calls += 1
        raise AssertionError("durable reopen must not execute LiteLLM")

    reopened = AgentSDK.for_test(
        database_path=database,
        acompletion=unused_after_reopen,
    )
    try:
        observed = await reopened.queries.get_run(execution.run_id)
        timeline = await reopened.queries.timeline(execution.run_id)
        reopened_tree = await reopened.queries.execution_tree(
            workflow.nodes[0].run_id or ""
        )
        workflow_snapshot = await reopened.workflows.get(
            workflow.workflow_run_id
        )
        reopened_capsule = await reopened.context.get_capsule(
            view.capsule_id or "",
            session_id=session.session_id,
        )
        reopened_sources = await reopened.context.read_sources(
            view.capsule_id or "",
            session_id=session.session_id,
        )
        reopened_success = await reopened.analytics.success_rate(
            evaluator_id="exact_output"
        )
        evaluation_events = await reopened.queries.query_events(
            EventFilter(
                session_id=session.session_id,
                event_types=("evaluation.completed",),
            )
        )

        assert observed.snapshot.output_text == WORKFLOW_YAML
        assert timeline.events[-1].event.type == "run.completed"
        assert reopened_tree.root_run_id == tree.root_run_id
        assert reopened_tree.nodes == tree.nodes
        assert reopened_tree.as_of_cursor >= tree.as_of_cursor
        assert workflow_snapshot.status is WorkflowRunStatus.COMPLETED
        assert reopened_capsule == capsule
        assert reopened_capsule.source_event_ids == tuple(
            item.event.event_id for item in reopened_sources
        )
        assert reopened_success.value == 1.0
        assert [
            item.event.event_id for item in evaluation_events.events
        ] == list(reopened_success.evidence_event_ids)
        assert reopen_calls == 0
        assert (workspace / "result.txt").stat().st_mtime_ns == workspace_mtime

        await reopened.sessions.close(session.session_id)
        await reopened.sessions.delete(session.session_id)
        deleted_events = await reopened.queries.query_events(
            EventFilter(session_id=session.session_id)
        )
        assert deleted_events.events == ()
        with pytest.raises(AgentSDKError) as missing_run:
            await reopened.queries.get_run(execution.run_id)
        assert missing_run.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(AgentSDKError) as missing_workflow:
            await reopened.workflows.get(workflow.workflow_run_id)
        assert missing_workflow.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(AgentSDKError) as missing_capsule:
            await reopened.context.get_capsule(
                view.capsule_id or "",
                session_id=session.session_id,
            )
        assert missing_capsule.value.code is ErrorCode.NOT_FOUND
        after_delete = await reopened.analytics.success_rate(
            evaluator_id="exact_output"
        )
        assert after_delete.value is None
        assert after_delete.sample_count == 0
        assert after_delete.evidence_event_ids == ()
        assert (workspace / "result.txt").read_text(encoding="utf-8") == "hello"
        assert reopen_calls == 0
    finally:
        await reopened.close()
