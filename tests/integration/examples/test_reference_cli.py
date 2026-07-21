from __future__ import annotations

import asyncio
import ast
from collections.abc import AsyncIterator
from importlib import resources
import json
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSpec,
    EvaluationVerdict,
    PermissionDecision,
    PermissionRequest,
    ToolContext,
    ToolSpec,
    WorkflowRunStatus,
)
from agent_sdk.storage.memory import InMemoryStore
from examples.reference_cli.main import build_parser, main, run_application
from examples.reference_cli.runner import (
    _settle_permission_waiter,
    execute_run,
    run_workflow_if_approved,
)


_GENERAL_SYSTEM_PROMPT = (
    resources.files("agent_sdk.prompts.profiles")
    .joinpath("general", "system.md")
    .read_text(encoding="utf-8")
)


WORKFLOW_YAML = """\
api_version: agent-sdk/v1
kind: Workflow
name: runner-test
nodes:
  - id: plan
    kind: agent
    agent_revision: planner:1
    input: make a plan
  - id: verify
    kind: agent
    agent_revision: worker:1
    input: verify the plan
    run_as: child
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
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        }

    return generate()


def _tool_stream(
    name: str,
    arguments: str,
    *,
    call_id: str,
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


class ReferenceRunnerModel:
    def __init__(self) -> None:
        self.entry_calls = 0

    async def __call__(self, **params: Any) -> AsyncIterator[dict[str, object]]:
        model = str(params["model"])
        if model == "fake/entry":
            self.entry_calls += 1
            if self.entry_calls == 1:
                return _tool_stream(
                    "add",
                    '{"a":2,"b":3}',
                    call_id="call_add",
                )
            return _text_stream(WORKFLOW_YAML)
        return _text_stream("done")


class ReferenceApplicationModel:
    def __init__(self) -> None:
        self.main_calls = 0
        self.first_request_messages: tuple[dict[str, Any], ...] = ()
        self.structured_request_messages: tuple[dict[str, Any], ...] = ()

    async def __call__(self, **params: Any) -> object:
        if params["stream"] is False:
            self.structured_request_messages = tuple(
                dict(message) for message in params["messages"]
            )
            document = json.loads(params["messages"][1]["content"])
            return {
                "choices": [
                    {
                        "message": {
                            "parsed": {
                                "objective": "run the reference application",
                                "constraints": [],
                                "decisions": ["execute an approved workflow"],
                                "facts": ["result.txt was written"],
                                "next_actions": ["inspect analytics"],
                                "artifact_refs": ["workspace:result.txt"],
                                "source_event_ids": [
                                    item["event_id"] for item in document["sources"]
                                ],
                            }
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            }
        model = str(params["model"])
        if model == "fake/main":
            self.main_calls += 1
            if self.main_calls == 1:
                self.first_request_messages = tuple(
                    dict(message) for message in params["messages"]
                )
                return _tool_stream(
                    "write_note",
                    '{"content":"hello"}',
                    call_id="call_write",
                )
            return _text_stream(WORKFLOW_YAML)
        if model == "fake/planner":
            return _text_stream("planned")
        if model == "fake/worker":
            return _text_stream("verified")
        raise AssertionError(f"unexpected model: {model}")


def _register_add_tool(sdk: AgentSDK) -> None:
    async def add(_: ToolContext, a: int, b: int) -> int:
        return a + b

    sdk.tools.register(
        ToolSpec(
            name="add",
            description="Add two integers",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        ),
        add,
    )


async def _approve(_: object) -> bool:
    return True


def test_reference_cli_uses_only_package_root_sdk_imports() -> None:
    root = Path(__file__).parents[3] / "examples" / "reference_cli"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("agent_sdk")
        }
        assert modules <= {"agent_sdk"}


def test_parser_requires_prompt_database_workspace_and_model(tmp_path: Path) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as missing:
        parser.parse_args([])
    assert missing.value.code == 2

    parsed = parser.parse_args(
        [
            "do the work",
            "--database",
            str(tmp_path / "state.db"),
            "--workspace",
            str(tmp_path),
            "--model",
            "fake/main",
        ]
    )
    assert parsed.prompt == "do the work"


def test_main_reports_stable_sdk_error_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "do the work",
            "--database",
            str(tmp_path / "state.db"),
            "--workspace",
            str(tmp_path / "missing"),
            "--model",
            "fake/main",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload == {
        "type": "error",
        "code": "invalid_state",
        "message": "workspace must be an existing directory",
        "retryable": False,
    }
    assert "Traceback" not in captured.err


@pytest.mark.asyncio
async def test_runner_resolves_permission_collects_events_and_approves_workflow() -> None:
    model = ReferenceRunnerModel()
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=model)
    sdk.agents.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    _register_add_tool(sdk)
    session = await sdk.sessions.create(workspaces=[])
    permission_names: list[str] = []
    displayed: list[dict[str, object]] = []

    async def allow(request: PermissionRequest) -> PermissionDecision:
        permission_names.append(request.tool_name)
        return PermissionDecision.allow_once()

    execution = await execute_run(
        sdk,
        session.session_id,
        sdk.agents.define(
            AgentSpec(name="entry", revision="1", model="fake/entry")
        ),
        "produce a workflow",
        resolve_permission=allow,
        emit=displayed.append,
    )
    workflow = await run_workflow_if_approved(
        sdk,
        session.session_id,
        execution.result.output_text,
        approve=_approve,
        emit=displayed.append,
    )

    assert permission_names == ["add"]
    assert execution.events[0].event.type == "run.created"
    assert execution.events[-1].event.type == "run.completed"
    assert workflow is not None
    assert workflow.status is WorkflowRunStatus.COMPLETED
    assert any(item["type"] == "workflow.completed" for item in displayed)
    await sdk.close()


@pytest.mark.asyncio
async def test_runner_cancellation_denies_delivered_permission_and_settles() -> None:
    model = ReferenceRunnerModel()
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=model)
    _register_add_tool(sdk)
    session = await sdk.sessions.create(workspaces=[])
    entered = asyncio.Event()

    async def wait_for_cancellation(_: PermissionRequest) -> PermissionDecision:
        entered.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    execution = asyncio.create_task(
        execute_run(
            sdk,
            session.session_id,
            sdk.agents.define(
                AgentSpec(name="entry", revision="1", model="fake/entry")
            ),
            "produce a workflow",
            resolve_permission=wait_for_cancellation,
            emit=lambda _: None,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    await asyncio.wait_for(sdk.close(), timeout=1)


@pytest.mark.asyncio
async def test_runner_cancellation_before_permission_delivery_leaves_no_waiter() -> None:
    entered_model = asyncio.Event()
    release_model = asyncio.Event()

    async def blocked_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
        entered_model.set()
        await release_model.wait()
        return _text_stream("done")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=blocked_provider,
    )
    session = await sdk.sessions.create(workspaces=[])

    async def allow(_: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.allow_once()

    execution = asyncio.create_task(
        execute_run(
            sdk,
            session.session_id,
            sdk.agents.define(AgentSpec(name="blocked", model="fake/blocked")),
            "wait before a tool request",
            resolve_permission=allow,
            emit=lambda _: None,
        )
    )
    await asyncio.wait_for(entered_model.wait(), timeout=1)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    release_model.set()
    await asyncio.wait_for(sdk.close(), timeout=1)


@pytest.mark.asyncio
async def test_permission_waiter_cleanup_recovers_ready_and_cancels_pending() -> None:
    request = PermissionRequest(
        request_id="perm_test",
        run_id="run_test",
        session_id="ses_test",
        tool_name="add",
        arguments={},
    )

    async def ready() -> PermissionRequest:
        return request

    waiter = asyncio.create_task(ready())
    await asyncio.sleep(0)
    assert await _settle_permission_waiter(waiter) == request

    async def never() -> PermissionRequest:
        await asyncio.Future()
        raise AssertionError("unreachable")

    pending = asyncio.create_task(never())
    await asyncio.sleep(0)
    assert await _settle_permission_waiter(pending) is None
    assert pending.cancelled()


@pytest.mark.asyncio
async def test_run_application_composes_public_reference_scenario(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_root = tmp_path / "skills"
    skill = skill_root / "temporary-skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        """---
name: temporary-skill
description: Exercise the reference application.
---
# Temporary Skill

temporary skill instructions
""",
        encoding="utf-8",
    )
    (skill / "references" / "note.md").write_text(
        "temporary reference",
        encoding="utf-8",
    )
    expected = tmp_path / "expected.yaml"
    expected.write_text(WORKFLOW_YAML, encoding="utf-8")
    args = build_parser().parse_args(
        [
            "write the result and propose a workflow",
            "--database",
            str(tmp_path / "unused.db"),
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
            "8192",
            "--skill-root",
            str(skill_root),
            "--skill-name",
            "temporary-skill",
            "--skill-resource",
            "references/note.md",
            "--expected-output-file",
            str(expected),
        ]
    )
    model = ReferenceApplicationModel()
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=model)
    displayed: list[dict[str, object]] = []
    permission_names: list[str] = []

    async def allow(request: PermissionRequest) -> PermissionDecision:
        permission_names.append(request.tool_name)
        return PermissionDecision.allow_once()

    try:
        result = await run_application(
            args,
            sdk=sdk,
            resolve_permission=allow,
            approve_workflow=_approve,
            emit=displayed.append,
        )

        assert (workspace / "result.txt").read_text(encoding="utf-8") == "hello"
        assert [message["role"] for message in model.first_request_messages] == [
            "system",
            "user",
        ]
        assert model.first_request_messages[0] == {
            "role": "system",
            "content": _GENERAL_SYSTEM_PROMPT,
        }
        first_user_message = str(model.first_request_messages[1]["content"])
        assert "temporary skill instructions" in first_user_message
        assert "temporary reference" in first_user_message
        assert first_user_message.endswith("\n\nwrite the result and propose a workflow")
        assert permission_names == ["write_note"]
        assert result.context_view.capsule_id is None
        assert result.context_view.applied_level.value == "L2"
        assert result.context_view.fallback_from is not None
        assert result.context_view.fallback_from.value == "L3"
        assert model.structured_request_messages == ()
        assert result.prompt.manifest.context_view_id == result.context_view.view_id
        assert result.workflow is not None
        assert result.workflow.status is WorkflowRunStatus.COMPLETED
        assert result.evaluation is not None
        assert result.evaluation.verdict is EvaluationVerdict.PASS
        assert result.success_rate.value == 1.0
        assert result.tool_failures.value == 0.0
        assert {
            "context.view",
            "prompt.manifest",
            "workflow.completed",
            "evaluation.result",
            "analytics.success_rate",
            "analytics.tool_failures",
        } <= {str(record["type"]) for record in displayed}
        for record in displayed:
            json.dumps(record, ensure_ascii=False, allow_nan=False)
    finally:
        await sdk.close()
