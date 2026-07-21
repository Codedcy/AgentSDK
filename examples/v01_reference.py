from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agent_sdk import (
    AgentSDK,
    AgentSDKConfig,
    AgentSpec,
    CompactionLevel,
    ExactOutputEvaluator,
    TaskEnvelope,
    TraceStageKind,
)


WORKFLOW = """\
api_version: agent-sdk/v1
kind: Workflow
name: v01-reference
steps:
  - id: finish
    kind: agent
    agent_revision: workflow:1
    input: "Return exactly: reference complete"
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Agent SDK v0.1 reference flow")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--smoke", action="store_true")
    return parser


def _text_stream(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [{"delta": {"content": text}, "finish_reason": "stop"}]
        }
        yield {
            "choices": [],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        }

    return chunks()


async def _smoke_provider(**params: Any) -> object:
    if params.get("stream") is not False:
        return _text_stream("reference complete")

    messages = params["messages"]
    document = json.loads(str(messages[-1]["content"]))
    source_ids = [
        str(item["event_id"])
        for item in document.get("sources", [])
        if isinstance(item, dict) and "event_id" in item
    ]
    capsule_ids = [str(item) for item in document.get("capsule_ids", [])]
    return {
        "choices": [
            {
                "message": {
                    "parsed": {
                        "objective": "complete the v0.1 reference flow",
                        "constraints": [],
                        "decisions": [],
                        "facts": ["the deterministic provider completed"],
                        "next_actions": [],
                        "artifact_refs": [],
                        "source_event_ids": [*capsule_ids, *source_ids],
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


async def run_reference(args: argparse.Namespace) -> dict[str, object]:
    args.workspace.mkdir(parents=True, exist_ok=True)
    sdk = (
        AgentSDK.for_test(
            database_path=args.database,
            acompletion=_smoke_provider,
            permission_default="allow",
        )
        if args.smoke
        else AgentSDK(AgentSDKConfig(database_path=args.database))
    )
    try:
        session = await sdk.sessions.create(workspaces=(args.workspace,))
        parent_agent = sdk.agents.define(
            AgentSpec(
                name="reference",
                revision="1",
                model=args.model,
                system_prompt="Return exactly: reference complete",
            )
        )
        sdk.agents.define(
            AgentSpec(
                name="workflow",
                revision="1",
                model=args.model,
                system_prompt="Return exactly: reference complete",
            )
        )
        sdk.agents.define(
            AgentSpec(
                name="child",
                revision="1",
                model=args.model,
                system_prompt="Return exactly: reference complete",
            )
        )

        history_handle = await sdk.runs.start(
            session.session_id,
            parent_agent,
            "Create durable history for Context compaction",
        )
        await history_handle.result()
        run_handle = await sdk.runs.start(
            session.session_id,
            parent_agent,
            "Return exactly: reference complete",
        )
        await run_handle.result()
        run_snapshot = await sdk.runs.get(run_handle.run_id)

        context_levels = []
        for level in CompactionLevel:
            view = await sdk.context.build(
                session.session_id,
                model=args.model,
                model_window=8_192,
                force_level=level,
            )
            context_levels.append(view.applied_level.value)

        candidate = sdk.workflows.compile(WORKFLOW)
        workflow_handle = await sdk.workflows.start(session.session_id, candidate)
        workflow = await workflow_handle.result()

        trace = await sdk.trace.timeline(run_handle.run_id)
        trace_stage_count = sum(
            stage.kind is TraceStageKind.RUN for stage in trace.stages
        )
        evaluation = await sdk.evaluations.evaluate(
            run_handle.run_id,
            ExactOutputEvaluator(expected="reference complete"),
        )
        attribution = await sdk.trace.attribution(run_handle.run_id)

        child_snapshot = await sdk.children.spawn(
            run_handle.run_id,
            "child:1",
            TaskEnvelope(
                objective="Return exactly: reference complete",
                success_criteria=("return reference complete",),
            ),
        )
        child = await sdk.children.wait(child_snapshot.run_id)

        return {
            "run_status": run_snapshot.status.value,
            "workflow_status": workflow.status.value,
            "child_status": child.status,
            "context_levels": context_levels,
            "trace_stage_count": trace_stage_count,
            "evaluation_verdict": evaluation.verdict.value,
            "attribution_method": attribution.method,
        }
    finally:
        await sdk.close()


def main() -> None:
    result = asyncio.run(run_reference(build_parser().parse_args()))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
