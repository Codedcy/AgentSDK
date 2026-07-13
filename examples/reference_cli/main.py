from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Sequence

from agent_sdk import (
    AgentSDK,
    AgentSDKConfig,
    AgentSDKError,
    AgentSpec,
    AnalyticsResult,
    BuiltPrompt,
    ContextView,
    ErrorCode,
    EvaluationResult,
    ExactOutputEvaluator,
    MCPManager,
    MCPServerConfig,
    PermissionDecision,
    PermissionRequest,
    PromptComposer,
    SkillRegistry,
    StdioMCPTransport,
    WorkflowIR,
    WorkflowResult,
)

from .runner import (
    EventSink,
    PermissionResolver,
    RunExecution,
    WorkflowApprover,
    execute_run,
    register_workspace_write,
    run_workflow_if_approved,
)


@dataclass(frozen=True)
class ReferenceApplicationResult:
    session_id: str
    execution: RunExecution
    context_view: ContextView
    prompt: BuiltPrompt
    workflow: WorkflowResult | None
    evaluation: EvaluationResult | None
    success_rate: AnalyticsResult
    tool_failures: AnalyticsResult


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Agent SDK M01 public-API reference application.",
    )
    parser.add_argument("prompt")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--planner-model")
    parser.add_argument("--worker-model")
    parser.add_argument("--context-model")
    parser.add_argument("--model-window", type=_positive_int, default=128_000)
    parser.add_argument("--skill-root", action="append", type=Path, default=[])
    parser.add_argument("--skill-name")
    parser.add_argument("--skill-resource")
    parser.add_argument("--mcp-command")
    parser.add_argument("--mcp-arg", action="append", default=[])
    parser.add_argument("--mcp-name", default="demo")
    parser.add_argument("--expected-output-file", type=Path)
    return parser


def emit_json_line(record: dict[str, object]) -> None:
    print(json.dumps(record, ensure_ascii=False, default=str), flush=True)


async def prompt_for_permission(request: PermissionRequest) -> PermissionDecision:
    answer = await asyncio.to_thread(
        input,
        f"Allow Tool {request.tool_name} once? [y/N] ",
    )
    if answer.strip().lower() == "y":
        return PermissionDecision.allow_once()
    return PermissionDecision.deny("application denied")


async def prompt_for_workflow(workflow: WorkflowIR) -> bool:
    answer = await asyncio.to_thread(
        input,
        f"Execute Workflow {workflow.name} ({workflow.definition_hash})? [y/N] ",
    )
    return answer.strip().lower() == "y"


def _configuration_error(message: str) -> AgentSDKError:
    return AgentSDKError(ErrorCode.INVALID_STATE, message, retryable=False)


def _read_application_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise _configuration_error("application input file unavailable") from error


async def run_application(
    args: argparse.Namespace,
    *,
    sdk: AgentSDK,
    resolve_permission: PermissionResolver,
    approve_workflow: WorkflowApprover,
    emit: EventSink,
    session_id: str | None = None,
) -> ReferenceApplicationResult:
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        raise _configuration_error("workspace must be an existing directory")
    if args.skill_resource and not args.skill_name:
        raise _configuration_error("skill resource requires a skill name")
    if args.mcp_arg and not args.mcp_command:
        raise _configuration_error("MCP arguments require an MCP command")

    manager = MCPManager(sdk.tools)
    try:
        register_workspace_write(sdk, workspace)
        if args.mcp_command:
            await manager.connect(
                MCPServerConfig(
                    name=args.mcp_name,
                    transport=StdioMCPTransport(
                        command=args.mcp_command,
                        args=tuple(args.mcp_arg),
                        cwd=workspace,
                    ),
                )
            )

        application_parts = [
            "Available Workflow agent revisions: planner:1 and worker:1."
        ]
        registry = SkillRegistry(args.skill_root)
        registry.discover()
        if args.skill_name:
            skill = registry.activate(args.skill_name)
            application_parts.append(skill.instructions)
            if args.skill_resource:
                application_parts.append(skill.read_text(args.skill_resource))
        application_text = "\n\n".join(application_parts)
        user_input = f"{application_text}\n\n{args.prompt}"

        main_agent = sdk.agents.define(
            AgentSpec(name="main", revision="1", model=args.model)
        )
        sdk.agents.define(
            AgentSpec(
                name="planner",
                revision="1",
                model=args.planner_model or args.model,
            )
        )
        sdk.agents.define(
            AgentSpec(
                name="worker",
                revision="1",
                model=args.worker_model or args.model,
            )
        )
        if session_id is None:
            session = await sdk.sessions.create(workspaces=[workspace])
            selected_session_id = session.session_id
        else:
            selected_session_id = session_id

        execution = await execute_run(
            sdk,
            selected_session_id,
            main_agent,
            user_input,
            resolve_permission=resolve_permission,
            emit=emit,
        )
        context_view = await sdk.context.build(
            selected_session_id,
            model=args.context_model or args.model,
            model_window=args.model_window,
            force_level="L3",
        )
        prompt = PromptComposer().compose(
            profile="coding",
            context_view=context_view,
            model=args.model,
            application=application_text,
            tools=sdk.tools.schemas(),
        )
        emit(
            {
                "type": "context.view",
                "view": context_view.model_dump(mode="json"),
            }
        )
        emit(
            {
                "type": "prompt.manifest",
                "manifest": prompt.manifest.model_dump(mode="json"),
            }
        )
        emit(
            {
                "type": "workflow.candidate.text",
                "text": execution.result.output_text,
            }
        )
        workflow = await run_workflow_if_approved(
            sdk,
            selected_session_id,
            execution.result.output_text,
            approve=approve_workflow,
            emit=emit,
        )

        evaluation: EvaluationResult | None = None
        if args.expected_output_file is not None:
            expected_output = _read_application_text(args.expected_output_file)
            evaluation = await sdk.evaluations.evaluate(
                execution.run_id,
                ExactOutputEvaluator(expected=expected_output),
            )
            emit(
                {
                    "type": "evaluation.result",
                    **evaluation.model_dump(mode="json"),
                }
            )
        success_rate = await sdk.analytics.success_rate(
            evaluator_id="exact_output"
        )
        tool_failures = await sdk.analytics.tool_failures()
        emit(
            {
                "type": "analytics.success_rate",
                **success_rate.model_dump(mode="json"),
            }
        )
        emit(
            {
                "type": "analytics.tool_failures",
                **tool_failures.model_dump(mode="json"),
            }
        )
        return ReferenceApplicationResult(
            session_id=selected_session_id,
            execution=execution,
            context_view=context_view,
            prompt=prompt,
            workflow=workflow,
            evaluation=evaluation,
            success_rate=success_rate,
            tool_failures=tool_failures,
        )
    finally:
        await manager.close()


async def async_main(args: argparse.Namespace) -> int:
    sdk = AgentSDK(
        AgentSDKConfig(
            database_path=args.database,
            permission_default="ask",
        )
    )
    try:
        await run_application(
            args,
            sdk=sdk,
            resolve_permission=prompt_for_permission,
            approve_workflow=prompt_for_workflow,
            emit=emit_json_line,
        )
        return 0
    finally:
        await sdk.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(async_main(args))
    except AgentSDKError as error:
        emit_json_line({"type": "error", **error.to_dict()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
