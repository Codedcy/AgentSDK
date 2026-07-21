from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any

from agent_sdk import (
    AgentSDK,
    AgentSDKConfig,
    AgentSDKError,
    AgentSpec,
    ContextRuntimeConfig,
    ErrorCode,
    EventFilter,
    ExactOutputEvaluator,
)


WORKFLOW = """\
api_version: agent-sdk/v1
kind: Workflow
name: v01-reference
inputs: {enabled: true}
steps:
  - id: choose
    kind: condition
    when: {path: inputs.enabled, op: eq, value: true}
    then_steps:
      - {id: selected, kind: agent, agent_revision: workflow:1, input: selected}
    else_steps:
      - {id: skipped, kind: agent, agent_revision: workflow:1, input: skipped}
  - id: improve
    kind: loop
    until: {path: outputs.review.done, op: exists}
    max_iterations: 3
    body:
      - {id: review, kind: agent, agent_revision: workflow:1, input: review}
  - {id: finish, kind: agent, agent_revision: workflow:1, input: finish}
"""

_CONTEXT_STAGES = (
    ("stage-l0", 100),
    ("stage-l1", 600),
    ("stage-l2", 80),
    ("stage-l3-a", 80),
    ("stage-l3-b", 1),
    ("stage-l4", 80),
)
_PARENT_TOOLS = (
    "list_children",
    "send_message",
    "spawn_agent",
    "wait_child",
)
_CHILD_OUTPUT = "verified child finding"
_PARENT_OUTPUT = f"parent used child result: {_CHILD_OUTPUT}"


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


def _tool_stream(
    *,
    call_id: str,
    name: str,
    arguments: dict[str, object],
) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
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
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    return chunks()


def _messages(params: dict[str, object]) -> tuple[dict[str, object], ...]:
    raw = params["messages"]
    if not isinstance(raw, (list, tuple)) or not all(isinstance(item, dict) for item in raw):
        raise AssertionError("provider messages must be mappings")
    return tuple(raw)


def _tool_names(params: dict[str, object]) -> tuple[str, ...]:
    raw_tools = params.get("tools", ())
    if not isinstance(raw_tools, (list, tuple)):
        raise AssertionError("provider tools must be a sequence")
    names: list[str] = []
    for raw in raw_tools:
        if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
            raise AssertionError("provider tool schema must be a mapping")
        names.append(str(raw["function"]["name"]))
    return tuple(names)


def _last_tool_value(params: dict[str, object], expected_name: str) -> object:
    for message in reversed(_messages(params)):
        if message.get("role") == "tool" and message.get("name") == expected_name:
            return json.loads(str(message["content"]))
    raise AssertionError(f"missing Tool result for {expected_name}")


class _SmokeProvider:
    """Deterministic LiteLLM-shaped provider used only by ``--smoke``."""

    def __init__(self) -> None:
        self._parent_ready = asyncio.Event()
        self._parent_message_sent = asyncio.Event()
        self._child_second_call_started = asyncio.Event()
        self._allow_child_complete = asyncio.Event()
        self.parent_run_id: str | None = None
        self.child_run_id: str | None = None
        self.parent_calls = 0
        self.child_calls = 0
        self.workflow_review_calls = 0

    def bind_parent(self, run_id: str) -> None:
        self.parent_run_id = run_id
        self._parent_ready.set()

    def release(self) -> None:
        self._parent_ready.set()
        self._parent_message_sent.set()
        self._child_second_call_started.set()
        self._allow_child_complete.set()

    async def __call__(self, **raw_params: Any) -> object:
        params = dict(raw_params)
        if params.get("stream") is False:
            return self._compaction_response(params)
        tools = _tool_names(params)
        if tools == _PARENT_TOOLS:
            return await self._parent_response(params)
        if tools == ("send_message",):
            return await self._child_response(params)

        prompt = str(_messages(params)[-1]["content"])
        if prompt == "review":
            self.workflow_review_calls += 1
            return _text_stream(
                '{"done":true}'
                if self.workflow_review_calls == 2
                else '{"progress":1}'
            )
        if prompt == "selected":
            return _text_stream("selected")
        return _text_stream("reference complete")

    def _compaction_response(self, params: dict[str, object]) -> object:
        document = json.loads(str(_messages(params)[-1]["content"]))
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

    async def _parent_response(self, params: dict[str, object]) -> object:
        await self._parent_ready.wait()
        self.parent_calls += 1
        if self.parent_calls == 1:
            return _tool_stream(
                call_id="parent-spawn",
                name="spawn_agent",
                arguments={
                    "agent_revision": "researcher:1",
                    "task": {
                        "objective": "Inspect the reference evidence",
                        "success_criteria": ["return one verified finding"],
                        "allowed_tools": ["send_message"],
                    },
                },
            )
        if self.parent_calls == 2:
            spawned = _last_tool_value(params, "spawn_agent")
            if not isinstance(spawned, dict):
                raise AssertionError("spawn_agent result must be a mapping")
            self.child_run_id = str(spawned["child_run_id"])
            return _tool_stream(
                call_id="parent-message",
                name="send_message",
                arguments={
                    "target_run_id": self.child_run_id,
                    "content": "Use the reference evidence",
                },
            )
        if self.parent_calls == 3:
            sent = _last_tool_value(params, "send_message")
            if not isinstance(sent, dict) or sent.get("recipient_run_id") != self.child_run_id:
                raise AssertionError("parent message was not delivered to the Child")
            self._parent_message_sent.set()
            return _tool_stream(
                call_id="parent-list",
                name="list_children",
                arguments={},
            )
        if self.parent_calls == 4:
            listed = _last_tool_value(params, "list_children")
            if not isinstance(listed, list) or listed[0].get("run_id") != self.child_run_id:
                raise AssertionError("Child was not returned by list_children")
            await self._child_second_call_started.wait()
            self._allow_child_complete.set()
            return _tool_stream(
                call_id="parent-wait",
                name="wait_child",
                arguments={
                    "child_run_id": self.child_run_id,
                    "timeout_seconds": 5,
                },
            )
        if self.parent_calls != 5:
            raise AssertionError("unexpected parent model call")
        terminal = _last_tool_value(params, "wait_child")
        if (
            not isinstance(terminal, dict)
            or terminal.get("status") != "completed"
            or not isinstance(terminal.get("result"), dict)
            or terminal["result"].get("output_text") != _CHILD_OUTPUT
        ):
            raise AssertionError("parent did not receive the completed Child result")
        return _text_stream(_PARENT_OUTPUT)

    async def _child_response(self, params: dict[str, object]) -> object:
        self.child_calls += 1
        if self.child_calls == 1:
            await self._parent_message_sent.wait()
            if self.parent_run_id is None:
                raise AssertionError("parent Run was not bound")
            return _tool_stream(
                call_id="child-message",
                name="send_message",
                arguments={
                    "target_run_id": self.parent_run_id,
                    "content": "Child verified the reference evidence",
                },
            )
        if self.child_calls != 2:
            raise AssertionError("unexpected Child model call")
        if not any(
            "Use the reference evidence" in str(message.get("content"))
            for message in _messages(params)
        ):
            raise AssertionError("Child did not consume the parent message")
        self._child_second_call_started.set()
        await self._allow_child_complete.wait()
        return _text_stream(_CHILD_OUTPUT)


class _NoReplayProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_: Any) -> object:
        self.calls += 1
        raise AssertionError("completed-boundary reopen must not call LiteLLM")


def _open_sdk(
    args: argparse.Namespace,
    *,
    smoke_provider: Any | None = None,
) -> AgentSDK:
    if args.smoke:
        if smoke_provider is None:
            raise AssertionError("smoke mode requires a deterministic provider")
        return AgentSDK.for_test(
            database_path=args.database,
            acompletion=smoke_provider,
            permission_default="allow",
        )
    return AgentSDK(
        AgentSDKConfig(
            database_path=args.database,
            permission_default="allow",
        )
    )


async def _collect_live_trace(
    sdk: AgentSDK,
    session_id: str,
    target_run_id: asyncio.Future[str],
    ready: asyncio.Event,
    observed: list[tuple[str | None, str]],
) -> None:
    async for item in sdk.trace.subscribe(
        filters=EventFilter(session_id=session_id),
        cursor=0,
    ):
        observed.append((item.event.run_id, item.event.type))
        ready.set()
        if (
            target_run_id.done()
            and item.event.run_id == target_run_id.result()
            and item.event.type == "run.completed"
        ):
            return


def _first_occurrences(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


async def _event_facts(
    sdk: AgentSDK,
    session_id: str,
) -> tuple[list[object], tuple[int, int]]:
    page = await sdk.queries.query_events(
        EventFilter(session_id=session_id),
        after_cursor=0,
        limit=1_000,
    )
    events = [stored.event for stored in page.events]
    starts = (
        sum(event.type == "model.call.started" for event in events),
        sum(event.type == "tool.call.started" for event in events),
    )
    return events, starts


async def run_reference(args: argparse.Namespace) -> dict[str, object]:
    args.workspace.mkdir(parents=True, exist_ok=True)
    marker = args.workspace / "v01-reference-marker.txt"
    marker.write_text("application-owned", encoding="utf-8")
    smoke_provider = _SmokeProvider() if args.smoke else None
    sdk = _open_sdk(args, smoke_provider=smoke_provider)
    live_task: asyncio.Task[None] | None = None
    target_run_id: asyncio.Future[str] | None = None
    session_id = ""
    workflow_run_id = ""
    parent_run_id = ""
    before_starts = (0, 0)
    result: dict[str, object] | None = None

    try:
        session = await sdk.sessions.create(workspaces=(args.workspace,))
        session_id = session.session_id
        ready = asyncio.Event()
        observed_live: list[tuple[str | None, str]] = []
        target_run_id = asyncio.get_running_loop().create_future()
        live_task = asyncio.create_task(
            _collect_live_trace(
                sdk,
                session_id,
                target_run_id,
                ready,
                observed_live,
            )
        )
        await asyncio.wait_for(ready.wait(), timeout=5)

        context_agent = sdk.agents.define(
            AgentSpec(
                name="context",
                revision="1",
                model=args.model,
                system_prompt="Return exactly: reference complete",
                context=ContextRuntimeConfig(
                    model_window=1_000,
                    output_reserve=0,
                    safety_reserve=0,
                    recent_messages=2,
                ),
                tool_allowlist=(),
            )
        )
        sdk.agents.define(
            AgentSpec(
                name="workflow",
                revision="1",
                model=args.model,
                system_prompt=(
                    "For selected return selected. For the first review return "
                    '{\"progress\":1}; after that evidence, return {\"done\":true}. '
                    "For finish return exactly: reference complete."
                ),
                tool_allowlist=(),
            )
        )
        sdk.agents.define(
            AgentSpec(
                name="researcher",
                revision="1",
                model=args.model,
                system_prompt=(
                    "Read the parent Agent message, send one progress message back "
                    f"with send_message, then return exactly: {_CHILD_OUTPUT}"
                ),
                tool_allowlist=("send_message",),
            )
        )
        parent_agent = sdk.agents.define(
            AgentSpec(
                name="parent",
                revision="1",
                model=args.model,
                system_prompt=(
                    "Use spawn_agent once for researcher:1, send the Child one message, "
                    "list Children, wait for that Child, and only after reading its "
                    f"result return exactly: {_PARENT_OUTPUT}"
                ),
                tool_allowlist=_PARENT_TOOLS,
            )
        )

        context_run_ids: list[str] = []
        for stage, evidence_words in _CONTEXT_STAGES:
            handle = await sdk.runs.start(
                session_id,
                context_agent,
                f"{stage} " + ("evidence " * evidence_words),
            )
            await handle.result()
            context_run_ids.append(handle.run_id)

        candidate = sdk.workflows.compile(WORKFLOW)
        workflow_handle = await sdk.workflows.start(session_id, candidate)
        workflow = await workflow_handle.result()
        workflow_run_id = workflow_handle.workflow_run_id
        workflow_events = [item async for item in workflow_handle.events()]
        condition_event = next(
            item.event
            for item in workflow_events
            if item.event.type == "workflow.condition.selected"
        )
        loop_iterations = sum(
            item.event.type == "workflow.loop.iteration"
            for item in workflow_events
        )

        parent = await sdk.runs.start(
            session_id,
            parent_agent,
            "Coordinate the Child and consume its verified result",
        )
        parent_run_id = parent.run_id
        if smoke_provider is not None:
            smoke_provider.bind_parent(parent_run_id)
        target_run_id.set_result(parent_run_id)
        parent_result = await parent.result()
        run_snapshot = await sdk.runs.get(parent_run_id)
        children = await sdk.children.list(parent_run_id)
        if len(children) != 1:
            raise AssertionError("reference flow must create exactly one Child")
        child = await sdk.children.wait(children[0].run_id, timeout_seconds=5)
        await asyncio.wait_for(live_task, timeout=5)

        evaluation = await sdk.evaluations.evaluate(
            parent_run_id,
            ExactOutputEvaluator(expected=_PARENT_OUTPUT),
        )
        trace = await sdk.trace.timeline(parent_run_id)
        attribution = await sdk.trace.attribution(parent_run_id)
        events, before_starts = await _event_facts(sdk, session_id)
        context_view_ids = {
            str(event.payload["context_view_id"])
            for event in events
            if event.type == "model.call.started"
            and event.run_id in context_run_ids
        }
        context_levels = _first_occurrences(
            str(event.payload["applied_level"])
            for event in events
            if event.type == "context.view.created"
            and event.payload.get("view_id") in context_view_ids
        )
        messages = [event for event in events if event.type == "agent.message.sent"]
        child_message_ids = {
            str(event.payload["message_id"])
            for event in messages
            if event.payload.get("sender_run_id") == children[0].run_id
            and event.payload.get("recipient_run_id") == parent_run_id
        }
        parent_view_ids = {
            str(event.payload["context_view_id"])
            for event in events
            if event.type == "model.call.started"
            and event.run_id == parent_run_id
        }
        parent_consumed_child_message = any(
            event.type == "context.view.created"
            and event.payload.get("view_id") in parent_view_ids
            and bool(
                child_message_ids.intersection(
                    str(item)
                    for item in event.payload.get("consumed_message_ids", [])
                )
            )
            for event in events
        )

        result = {
            "run_status": run_snapshot.status.value,
            "workflow_status": workflow.status.value,
            "child_status": child.status,
            "context_levels": context_levels,
            "trace_stage_count": len(trace.stages),
            "evaluation_verdict": evaluation.verdict.value,
            "attribution_method": attribution.method,
            "condition_selection": str(condition_event.payload["branch"]),
            "loop_iterations": loop_iterations,
            "message_count": len(messages),
            "child_result_consumed": (
                parent_consumed_child_message
                and child.result is not None
                and child.result.output_text in parent_result.output_text
            ),
            "live_subscription_observed": (
                (parent_run_id, "run.completed") in observed_live
            ),
        }
    finally:
        if smoke_provider is not None:
            smoke_provider.release()
        if live_task is not None and not live_task.done():
            live_task.cancel()
            with suppress(asyncio.CancelledError):
                await live_task
        await sdk.close()

    if result is None:
        raise AssertionError("reference flow did not produce a result")

    replay_guard = _NoReplayProvider() if args.smoke else None
    reopened = _open_sdk(args, smoke_provider=replay_guard)
    try:
        reopened_run = await reopened.runs.get(parent_run_id)
        reopened_workflow = await reopened.workflows.get(workflow_run_id)
        _, after_starts = await _event_facts(reopened, session_id)
        result["safe_reopen_no_replay"] = (
            reopened_run.status.value == "completed"
            and reopened_workflow.status.value == "completed"
            and after_starts == before_starts
            and (replay_guard is None or replay_guard.calls == 0)
        )
        await reopened.sessions.close(session_id)
        await reopened.sessions.delete(session_id)
        try:
            await reopened.sessions.get(session_id)
        except AgentSDKError as exc:
            result["session_deleted"] = exc.code is ErrorCode.NOT_FOUND
        else:
            result["session_deleted"] = False
        result["workspace_preserved"] = (
            marker.read_text(encoding="utf-8") == "application-owned"
        )
    finally:
        await reopened.close()

    return result


def main() -> None:
    result = asyncio.run(run_reference(build_parser().parse_args()))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
