from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import litellm
import pytest
from pydantic import BaseModel

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    ErrorCode,
    PermissionDecision,
    PermissionEffect,
    PermissionRequest,
    RunStatus,
    TokenUsage,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from agent_sdk.models.litellm_gateway import (
    LiteLLMGateway,
    ModelCompleted,
    ModelRequest,
    ToolCallCompleted,
    UsageReported,
)
from agent_sdk.permissions.broker import InProcessPermissionBridge
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.storage.base import CommitBatch, CommitResult
from agent_sdk.storage.memory import InMemoryStore


class AddInput(BaseModel):
    a: int
    b: int


def _tool_call_chunks(
    arguments: str,
    *,
    name: str = "add",
    call_id: str = "call_add",
    index: int = 0,
) -> tuple[dict[str, object], ...]:
    return (
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call_id,
                                "function": {"name": name, "arguments": arguments},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )


class _TwoStepModel:
    def __init__(
        self,
        first_chunks: tuple[object, ...],
        *,
        final_text: str = "done",
    ) -> None:
        self.first_chunks = first_chunks
        self.final_text = final_text
        self.requests: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> AsyncIterator[object]:
        self.requests.append(kwargs)

        async def chunks() -> AsyncIterator[object]:
            if len(self.requests) == 1:
                for chunk in self.first_chunks:
                    yield chunk
            else:
                yield {
                    "choices": [
                        {
                            "delta": {"content": self.final_text},
                            "finish_reason": "stop",
                        }
                    ]
                }

        return chunks()


class _FailOncePermissionResolvedStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def commit(self, batch: CommitBatch) -> CommitResult:
        if not self.failed and any(
            event.type == "permission.resolved" for event in batch.events
        ):
            self.failed = True
            raise RuntimeError("raw permission resolved storage secret")
        return await super().commit(batch)


async def _add(_: ToolContext, a: int, b: int) -> int:
    return a + b


def _register_add(sdk: AgentSDK, handler: Any = _add, **spec: Any) -> None:
    sdk.tools.register(
        ToolSpec(
            name="add",
            description="Add two integers",
            input_schema=AddInput.model_json_schema(),
            **spec,
        ),
        handler,
    )


def test_tool_permission_models_detach_nested_json_and_registry_is_stable() -> None:
    effect = PermissionEffect(action="execute", resource="tool:zeta")
    schema_source = {
        "type": "object",
        "properties": {"values": {"type": "array", "items": {"type": "integer"}}},
    }
    argument_source = {"values": [1, 2]}
    result_source = {"nested": ["safe"]}
    spec = ToolSpec(name="zeta", description="z", input_schema=schema_source)
    request = PermissionRequest(
        request_id="prm_test",
        run_id="run_test",
        session_id="ses_test",
        tool_name="zeta",
        arguments=argument_source,
    )
    result = ToolResult.succeeded("call_test", "zeta", result_source)

    schema_source["properties"]["external"] = {"type": "string"}
    argument_source["values"].append(3)
    result_source["nested"].append("external")

    assert "external" not in spec.input_schema["properties"]
    assert request.arguments["values"] == (1, 2)
    assert result.value["nested"] == ("safe",)
    assert effect.action == "execute"
    assert ToolExecutor.__name__ == "ToolExecutor"
    with pytest.raises(TypeError):
        spec.input_schema["properties"]["new"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        request.arguments["values"][0] = 9  # type: ignore[index]

    async def handler(_: ToolContext, **__: object) -> None:
        return None

    registry = ToolRegistry()
    registry.register(spec, handler)
    registry.register(
        ToolSpec(name="alpha", description="a", input_schema={"type": "object"}),
        handler,
    )
    assert [registered.name for registered in registry.list()] == ["alpha", "zeta"]
    with pytest.raises(AgentSDKError) as duplicate:
        registry.register(spec, handler)
    assert duplicate.value.code is ErrorCode.CONFLICT
    assert duplicate.value.message == "tool already registered"


def test_policy_rejects_unknown_default_with_stable_sdk_error() -> None:
    with pytest.raises(AgentSDKError) as raised:
        PolicyEngine("unknown")  # type: ignore[arg-type]

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "invalid permission default"


@pytest.mark.asyncio
async def test_gateway_assembles_real_litellm_attribute_tool_fragments() -> None:
    chunks = (
        litellm.ModelResponseStream(
            id="chunk_1",
            created=1,
            model="fake/model",
            object="chat.completion.chunk",
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_",
                                "type": "function",
                                "function": {
                                    "name": "ad",
                                    "arguments": '{"a":2,',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        ),
        litellm.ModelResponseStream(
            id="chunk_2",
            created=2,
            model="fake/model",
            object="chat.completion.chunk",
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "add",
                                "type": "function",
                                "function": {"name": "d", "arguments": '"b":3}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            usage={
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        ),
    )

    async def acompletion(**_: object) -> AsyncIterator[object]:
        async def response() -> AsyncIterator[object]:
            for chunk in chunks:
                yield chunk

        return response()

    events = [
        event
        async for event in LiteLLMGateway._for_test(acompletion).stream(
            ModelRequest(model="fake/model", messages=({"role": "user"},))
        )
    ]

    assert events == [
        ToolCallCompleted(
            index=0,
            call_id="call_add",
            name="add",
            arguments_json='{"a":2,"b":3}',
        ),
        UsageReported(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        ModelCompleted(finish_reason="tool_calls"),
    ]
    assert sum(isinstance(event, ModelCompleted) for event in events) == 1
    assert not any(isinstance(event, litellm.ModelResponseStream) for event in events)


@pytest.mark.asyncio
async def test_text_only_run_persists_empty_ordered_tool_results() -> None:
    store = InMemoryStore()

    async def acompletion(**_: object) -> AsyncIterator[dict[str, object]]:
        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {
                "choices": [
                    {"delta": {"content": "plain"}, "finish_reason": "stop"}
                ]
            }

        return chunks()

    sdk = AgentSDK.for_test(store=store, acompletion=acompletion)
    try:
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "plain",
        )
        result = await run.result()
        snapshot = await sdk.runs.get(run.run_id)
        terminal = next(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
            and stored.event.type == "run.completed"
        )

        assert result.tool_results == ()
        assert snapshot.model_dump(mode="json")["tool_results"] == []
        assert terminal.payload == {
            "output_text": "plain",
            "usage": TokenUsage().model_dump(),
        }
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_tool_waits_for_permission_then_runs_second_model_step() -> None:
    store = InMemoryStore()
    called = asyncio.Event()
    model_requests: list[dict[str, object]] = []

    async def scripted_acompletion(
        **kwargs: object,
    ) -> AsyncIterator[dict[str, object]]:
        model_requests.append(kwargs)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if len(model_requests) == 1:
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_add",
                                        "function": {
                                            "name": "add",
                                            "arguments": '{"a":2,',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": '"b":3}'},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            else:
                yield {
                    "choices": [
                        {"delta": {"content": "5"}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                }

        return chunks()

    async def add(_: ToolContext, a: int, b: int) -> int:
        called.set()
        return a + b

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=scripted_acompletion,
        permission_default="ask",
    )
    try:
        sdk.tools.register(
            ToolSpec(
                name="add",
                description="Add two integers",
                input_schema=AddInput.model_json_schema(),
                effects=("execute",),
            ),
            add,
        )
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "add 2 and 3",
        )

        request = await asyncio.wait_for(
            sdk.permissions.next_request(run.run_id),
            timeout=1,
        )

        assert request.tool_name == "add"
        assert not called.is_set()
        waiting = await sdk.runs.get(run.run_id)
        assert waiting.status is RunStatus.WAITING_PERMISSION
        assert waiting.version == 3

        await sdk.permissions.resolve(
            request.request_id,
            PermissionDecision.allow_once(),
        )
        result = await asyncio.wait_for(run.result(), timeout=1)

        assert called.is_set()
        assert result.output_text == "5"
        assert result.usage == TokenUsage(
            prompt_tokens=3,
            completion_tokens=2,
            total_tokens=5,
        )
        assert len(result.tool_results) == 1
        assert result.tool_results[0].value == 5
        assert (await sdk.runs.get(run.run_id)).status is RunStatus.COMPLETED
        assert len(model_requests) == 2
        assert model_requests[0]["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "add",
                    "description": "Add two integers",
                    "parameters": AddInput.model_json_schema(),
                },
            }
        ]
        assert model_requests[1]["messages"] == [
            {"role": "user", "content": "add 2 and 3"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_add",
                        "type": "function",
                        "function": {
                            "name": "add",
                            "arguments": '{"a":2,"b":3}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_add",
                "name": "add",
                "content": "5",
            },
        ]
        events = [
            stored
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]
        assert [stored.event.type for stored in events] == [
            "run.created",
            "run.started",
            "step.started",
            "model.call.started",
            "model.usage.reported",
            "model.call.completed",
            "tool.call.proposed",
            "permission.requested",
            "permission.resolved",
            "tool.call.authorized",
            "tool.call.started",
            "tool.call.completed",
            "step.completed",
            "step.started",
            "model.call.started",
            "model.text.delta",
            "model.usage.reported",
            "model.call.completed",
            "step.completed",
            "run.completed",
        ]
        assert [stored.event.sequence for stored in events] == list(range(1, 21))
        assert (await sdk.runs.get(run.run_id)).version == 5
    finally:
        await sdk.close()


@pytest.mark.parametrize("arguments", ("{bad json", '{"a":"bad","b":3}'))
@pytest.mark.asyncio
async def test_invalid_arguments_do_not_request_permission_or_call_handler(
    arguments: str,
) -> None:
    model = _TwoStepModel(_tool_call_chunks(arguments))
    handler_called = False

    async def handler(_: ToolContext, a: int, b: int) -> int:
        nonlocal handler_called
        handler_called = True
        return a + b

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=model,
        permission_default="ask",
    )
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "invalid",
        )
        result = await asyncio.wait_for(run.result(), timeout=1)

        assert handler_called is False
        assert result.tool_results[0].status is ToolResultStatus.INVALID_ARGUMENTS
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                sdk.permissions.next_request(run.run_id),
                timeout=0.05,
            )
    finally:
        await sdk.close()


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
@pytest.mark.asyncio
async def test_nonfinite_json_constants_are_invalid_before_permission(
    constant: str,
) -> None:
    store = InMemoryStore()
    model = _TwoStepModel(
        _tool_call_chunks(
            f'{{"value":{constant}}}',
            name="measure",
            call_id="call_measure",
        )
    )
    handler_calls = 0

    async def handler(_: ToolContext, value: float) -> float:
        nonlocal handler_calls
        handler_calls += 1
        return value

    sdk = AgentSDK.for_test(store=store, acompletion=model, permission_default="ask")
    try:
        sdk.tools.register(
            ToolSpec(
                name="measure",
                description="Measure a number",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "number"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            handler,
        )
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "reject nonfinite",
        )
        result = await asyncio.wait_for(run.result(), timeout=1)

        assert result.tool_results[0].status is ToolResultStatus.INVALID_ARGUMENTS
        assert handler_calls == 0
        assert (await sdk.runs.get(run.run_id)).status is RunStatus.COMPLETED
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]
        assert "permission.requested" not in event_types
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                sdk.permissions.next_request(run.run_id),
                timeout=0.05,
            )
    finally:
        await sdk.close()


@pytest.mark.parametrize(
    ("permission_default", "expected_status", "expected_calls"),
    (
        ("allow", ToolResultStatus.SUCCEEDED, 1),
        ("deny", ToolResultStatus.DENIED, 0),
    ),
)
@pytest.mark.asyncio
async def test_direct_policy_allow_and_deny(
    permission_default: str,
    expected_status: ToolResultStatus,
    expected_calls: int,
) -> None:
    store = InMemoryStore()
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    handler_calls = 0

    async def handler(_: ToolContext, a: int, b: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return a + b

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=model,
        permission_default=permission_default,  # type: ignore[arg-type]
    )
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "direct policy",
        )
        result = await asyncio.wait_for(run.result(), timeout=1)
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]

        assert handler_calls == expected_calls
        assert result.tool_results[0].status is expected_status
        assert "permission.requested" not in event_types
        assert "permission.resolved" not in event_types
        assert ("tool.call.started" in event_types) is (expected_calls == 1)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_missing_permission_bridge_denies_without_waiting_or_handler() -> None:
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    handler_called = False

    async def handler(_: ToolContext, a: int, b: int) -> int:
        nonlocal handler_called
        handler_called = True
        return a + b

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=model,
        permission_default="ask",
        permission_bridge=None,
    )
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "headless fail closed",
        )
        result = await asyncio.wait_for(run.result(), timeout=1)

        assert handler_called is False
        assert result.tool_results[0].status is ToolResultStatus.DENIED
        assert result.tool_results[0].error == "permission bridge unavailable"
        with pytest.raises(AgentSDKError) as unavailable:
            await sdk.permissions.next_request(run.run_id)
        assert unavailable.value.code is ErrorCode.INVALID_STATE
        assert unavailable.value.message == "permission bridge unavailable"
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_permission_resolution_commits_running_before_handler_starts() -> None:
    store = InMemoryStore()
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def handler(_: ToolContext, a: int, b: int) -> int:
        handler_started.set()
        await release_handler.wait()
        return a + b

    sdk = AgentSDK.for_test(store=store, acompletion=model, permission_default="ask")
    run = None
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "resolve atomically",
        )
        request = await asyncio.wait_for(
            sdk.permissions.next_request(run.run_id),
            timeout=1,
        )
        resolve_task = asyncio.create_task(
            sdk.permissions.resolve(request.request_id, PermissionDecision.allow_once())
        )
        await asyncio.wait_for(handler_started.wait(), timeout=1)
        await asyncio.wait_for(resolve_task, timeout=1)

        running = await sdk.runs.get(run.run_id)
        assert running.status is RunStatus.RUNNING
        assert running.version == 4
        events = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]
        assert events.index("permission.resolved") < events.index("tool.call.authorized")
        assert events.index("tool.call.authorized") < events.index("tool.call.started")

        release_handler.set()
        assert (await asyncio.wait_for(run.result(), timeout=1)).tool_results[
            0
        ].status is ToolResultStatus.SUCCEEDED
    finally:
        release_handler.set()
        if run is not None:
            await run.result()
        await sdk.close()


@pytest.mark.asyncio
async def test_permission_resolve_rejects_unknown_and_duplicate_ids() -> None:
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=model,
        permission_default="ask",
    )
    try:
        _register_add(sdk)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "resolve once",
        )
        with pytest.raises(AgentSDKError) as unknown:
            await sdk.permissions.resolve(
                "prm_unknown",
                PermissionDecision.allow_once(),
            )
        assert unknown.value.code is ErrorCode.NOT_FOUND
        assert unknown.value.message == "permission request not found"

        request = await asyncio.wait_for(
            sdk.permissions.next_request(run.run_id),
            timeout=1,
        )
        await sdk.permissions.resolve(
            request.request_id,
            PermissionDecision.allow_once(),
        )
        await asyncio.wait_for(run.result(), timeout=1)

        with pytest.raises(AgentSDKError) as duplicate:
            await sdk.permissions.resolve(
                request.request_id,
                PermissionDecision.deny(),
            )
        assert duplicate.value.code is ErrorCode.CONFLICT
        assert duplicate.value.message == "permission request already resolved"
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_in_flight_permission_resolutions_remain_duplicate_conflicts() -> None:
    bridge = InProcessPermissionBridge()
    request_ids = [f"prm_in_flight_{index}" for index in range(65)]
    waiting: list[asyncio.Task[PermissionDecision]] = []

    for request_id in request_ids:
        request = PermissionRequest(
            request_id=request_id,
            run_id="run_in_flight",
            session_id="ses_in_flight",
            tool_name="in_flight",
            arguments={},
        )
        waiting.append(asyncio.create_task(bridge.wait(request)))
        assert (
            await bridge.next_request("run_in_flight")
        ).request_id == request_id

    resolving = [
        asyncio.create_task(
            bridge.resolve(request_id, PermissionDecision.allow_once())
        )
        for request_id in request_ids
    ]
    try:
        decisions = await asyncio.gather(*waiting)
        assert all(decision.allowed for decision in decisions)
        assert all(not task.done() for task in resolving)

        with pytest.raises(AgentSDKError) as duplicate:
            await bridge.resolve(
                request_ids[0],
                PermissionDecision.deny(),
            )
        assert duplicate.value.code is ErrorCode.CONFLICT
        assert duplicate.value.message == "permission request already resolved"
    finally:
        for request_id in request_ids:
            await bridge.mark_committed(request_id)
        await asyncio.gather(*resolving, return_exceptions=True)

    assert all(task.done() for task in waiting)
    assert all(task.done() for task in resolving)
    assert all(task.result() is None for task in resolving)
    assert not bridge._pending  # type: ignore[attr-defined]
    assert not bridge._queues  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_permission_resolution_history_is_bounded_without_re_resolve() -> None:
    bridge = InProcessPermissionBridge()
    history_limit = 64
    request_ids = [
        f"prm_history_{index}"
        for index in range(history_limit + 1)
    ]

    for request_id in request_ids:
        request = PermissionRequest(
            request_id=request_id,
            run_id="run_history",
            session_id="ses_history",
            tool_name="history",
            arguments={},
        )
        waiting = asyncio.create_task(bridge.wait(request))
        assert (await bridge.next_request("run_history")).request_id == request_id
        resolving = asyncio.create_task(
            bridge.resolve(request_id, PermissionDecision.allow_once())
        )
        assert (await waiting).allowed
        await bridge.mark_committed(request_id)
        await resolving

    assert len(bridge._resolved_history) == history_limit  # type: ignore[attr-defined]

    with pytest.raises(AgentSDKError) as recent_duplicate:
        await bridge.resolve(request_ids[-1], PermissionDecision.deny())
    assert recent_duplicate.value.code is ErrorCode.CONFLICT

    with pytest.raises(AgentSDKError) as evicted_duplicate:
        await bridge.resolve(request_ids[0], PermissionDecision.allow_once())
    assert evicted_duplicate.value.code is ErrorCode.NOT_FOUND

    with pytest.raises(AgentSDKError) as still_not_resolvable:
        await bridge.resolve(request_ids[0], PermissionDecision.deny())
    assert still_not_resolvable.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_permission_request_queues_do_not_grow_for_unknown_waiters() -> None:
    bridge = InProcessPermissionBridge()
    unknown_waiters = [
        asyncio.create_task(bridge.next_request(f"run_unknown_{index}"))
        for index in range(100)
    ]
    await asyncio.sleep(0)
    for waiter in unknown_waiters:
        waiter.cancel()
    await asyncio.gather(*unknown_waiters, return_exceptions=True)

    assert not bridge._queues  # type: ignore[attr-defined]

    request = PermissionRequest(
        request_id="prm_queue_cleanup",
        run_id="run_queue_cleanup",
        session_id="ses_queue_cleanup",
        tool_name="cleanup",
        arguments={},
    )
    waiting = asyncio.create_task(bridge.wait(request))
    try:
        assert (
            await bridge.next_request("run_queue_cleanup")
        ).request_id == request.request_id
        assert "run_queue_cleanup" not in bridge._queues  # type: ignore[attr-defined]
    finally:
        await bridge.cancel(request.request_id)
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_permission_resolved_commit_failure_unblocks_and_fails_run() -> None:
    store = _FailOncePermissionResolvedStore()
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    handler_called = False

    async def handler(_: ToolContext, a: int, b: int) -> int:
        nonlocal handler_called
        handler_called = True
        return a + b

    sdk = AgentSDK.for_test(store=store, acompletion=model, permission_default="ask")
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "fail permission commit",
        )
        request = await asyncio.wait_for(
            sdk.permissions.next_request(run.run_id),
            timeout=1,
        )

        with pytest.raises(AgentSDKError) as resolve_error:
            await asyncio.wait_for(
                sdk.permissions.resolve(
                    request.request_id,
                    PermissionDecision.allow_once(),
                ),
                timeout=1,
            )
        assert resolve_error.value.code is ErrorCode.INTERNAL
        assert resolve_error.value.message == "permission resolution failed"

        with pytest.raises(AgentSDKError) as run_error:
            await asyncio.wait_for(run.result(), timeout=1)
        assert run_error.value.code is ErrorCode.INTERNAL
        assert run_error.value.message == "permission resolution failed"
        assert handler_called is False

        snapshot = await sdk.runs.get(run.run_id)
        assert snapshot.status is RunStatus.FAILED
        assert snapshot.version == 4
        events = [
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]
        assert [event.type for event in events][-2:] == ["step.failed", "run.failed"]
        assert "raw permission resolved storage secret" not in str(
            [event.payload for event in events]
        )

        with pytest.raises(AgentSDKError) as duplicate:
            await sdk.permissions.resolve(
                request.request_id,
                PermissionDecision.deny(),
            )
        assert duplicate.value.code is ErrorCode.CONFLICT
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                sdk.permissions.next_request(run.run_id),
                timeout=0.05,
            )
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_asked_permission_can_be_denied_without_authorizing_handler() -> None:
    store = InMemoryStore()
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    handler_called = False

    async def handler(_: ToolContext, a: int, b: int) -> int:
        nonlocal handler_called
        handler_called = True
        return a + b

    sdk = AgentSDK.for_test(store=store, acompletion=model, permission_default="ask")
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "deny request",
        )
        request = await asyncio.wait_for(
            sdk.permissions.next_request(run.run_id),
            timeout=1,
        )
        await sdk.permissions.resolve(
            request.request_id,
            PermissionDecision.deny("application denied"),
        )
        result = await asyncio.wait_for(run.result(), timeout=1)
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]

        assert handler_called is False
        assert result.tool_results[0].status is ToolResultStatus.DENIED
        assert result.tool_results[0].error == "application denied"
        assert "permission.requested" in event_types
        assert "permission.resolved" in event_types
        assert "tool.call.authorized" not in event_types
        assert "tool.call.started" not in event_types
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_permission_reason_and_tool_error_are_utf8_byte_bounded() -> None:
    store = InMemoryStore()
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    long_reason = "😀" * 512
    sdk = AgentSDK.for_test(store=store, acompletion=model, permission_default="ask")
    try:
        _register_add(sdk)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "unicode deny",
        )
        request = await asyncio.wait_for(
            sdk.permissions.next_request(run.run_id),
            timeout=1,
        )
        await sdk.permissions.resolve(
            request.request_id,
            PermissionDecision.deny(long_reason),
        )
        result = await asyncio.wait_for(run.result(), timeout=1)

        tool_result = result.tool_results[0]
        assert tool_result.status is ToolResultStatus.DENIED
        assert tool_result.error is not None
        assert len(tool_result.error.encode("utf-8")) <= 512
        assert len(tool_result.content.encode("utf-8")) <= 16 * 1024
        assert (await sdk.runs.get(run.run_id)).status is RunStatus.COMPLETED

        resolved = next(
            stored.event
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
            and stored.event.type == "permission.resolved"
        )
        persisted_reason = resolved.payload["decision"]["reason"]
        assert isinstance(persisted_reason, str)
        assert len(persisted_reason.encode("utf-8")) <= 512
        assert persisted_reason != long_reason

        direct = PermissionDecision(action="deny", reason=long_reason)
        assert direct.reason == persisted_reason
        direct_result = ToolResult(
            call_id="call_direct",
            tool_name="add",
            status=ToolResultStatus.DENIED,
            content=long_reason * 20,
            error=long_reason,
        )
        assert len(direct_result.error.encode("utf-8")) <= 512  # type: ignore[union-attr]
        assert len(direct_result.content.encode("utf-8")) <= 16 * 1024
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_cancelling_permission_wait_removes_pending_request() -> None:
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=model,
        permission_default="ask",
    )
    try:
        _register_add(sdk)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "cancel permission",
        )
        request = await asyncio.wait_for(
            sdk.permissions.next_request(run.run_id),
            timeout=1,
        )

        run._task.cancel()  # type: ignore[attr-defined]
        with pytest.raises(AgentSDKError) as cancelled:
            await run.result()
        assert cancelled.value.message == "run execution failed"

        with pytest.raises(AgentSDKError) as removed:
            await sdk.permissions.resolve(
                request.request_id,
                PermissionDecision.allow_once(),
            )
        assert removed.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(AgentSDKError) as still_removed:
            await sdk.permissions.resolve(
                request.request_id,
                PermissionDecision.deny(),
            )
        assert still_removed.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                sdk.permissions.next_request(run.run_id),
                timeout=0.05,
            )
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_handler_timeout_is_normalized_without_success() -> None:
    store = InMemoryStore()
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    handler_started = asyncio.Event()

    async def handler(_: ToolContext, a: int, b: int) -> int:
        del a, b
        handler_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    sdk = AgentSDK.for_test(store=store, acompletion=model, permission_default="allow")
    try:
        _register_add(sdk, handler, timeout_seconds=0.01)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "timeout",
        )
        result = await asyncio.wait_for(run.result(), timeout=1)

        assert handler_started.is_set()
        assert result.tool_results[0].status is ToolResultStatus.TIMED_OUT
        assert result.tool_results[0].error == "tool execution timed out"
        event_payloads = [
            stored.event.payload
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]
        assert "succeeded" not in str(event_payloads)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_timeout_ignores_handler_late_success_after_cancel() -> None:
    store = InMemoryStore()
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    handler_cancelled = asyncio.Event()

    async def handler(_: ToolContext, a: int, b: int) -> int:
        del a, b
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            return 999

    sdk = AgentSDK.for_test(store=store, acompletion=model, permission_default="allow")
    try:
        _register_add(sdk, handler, timeout_seconds=0.01)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "late timeout success",
        )
        result = await asyncio.wait_for(run.result(), timeout=1)
        await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
        completed = [
            stored.event.payload
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
            and stored.event.type == "tool.call.completed"
        ]

        assert result.tool_results[0].status is ToolResultStatus.TIMED_OUT
        assert len(completed) == 1
        assert completed[0]["status"] == ToolResultStatus.TIMED_OUT.value
        assert len(model.requests) == 2
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_run_cancel_cannot_be_swallowed_by_handler_late_success() -> None:
    store = InMemoryStore()
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))
    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def handler(_: ToolContext, a: int, b: int) -> int:
        del a, b
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            return 999

    sdk = AgentSDK.for_test(store=store, acompletion=model, permission_default="allow")
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "cancel run",
        )
        await asyncio.wait_for(handler_started.wait(), timeout=1)

        run._task.cancel()  # type: ignore[attr-defined]
        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(run.result(), timeout=1)
        await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
        await asyncio.sleep(0)

        assert raised.value.message == "run execution failed"
        assert len(model.requests) == 1
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]
        assert "tool.call.completed" not in event_types
        assert "run.completed" not in event_types
    finally:
        await sdk.close()


@pytest.mark.parametrize("outcome", ("exception", "object", "oversized"))
@pytest.mark.asyncio
async def test_handler_failures_and_unsafe_results_are_sanitized(outcome: str) -> None:
    store = InMemoryStore()
    model = _TwoStepModel(_tool_call_chunks('{"a":2,"b":3}'))

    async def handler(_: ToolContext, a: int, b: int) -> object:
        del a, b
        if outcome == "exception":
            raise RuntimeError("handler secret token")
        if outcome == "object":
            return object()
        return "x" * (20 * 1024)

    sdk = AgentSDK.for_test(store=store, acompletion=model, permission_default="allow")
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "sanitize",
        )
        result = await asyncio.wait_for(run.result(), timeout=1)
        persisted = [
            stored.event.model_dump(mode="json")
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]

        assert result.tool_results[0].status is ToolResultStatus.FAILED
        assert result.tool_results[0].value is None
        assert "handler secret token" not in str(persisted)
        assert "object at 0x" not in str(persisted)
        assert "x" * 1024 not in str(persisted)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_multiple_tool_calls_in_one_step_fail_stably() -> None:
    store = InMemoryStore()
    chunks = (
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_one",
                                "function": {
                                    "name": "add",
                                    "arguments": '{"a":1,"b":2}',
                                },
                            },
                            {
                                "index": 1,
                                "id": "call_two",
                                "function": {
                                    "name": "add",
                                    "arguments": '{"a":3,"b":4}',
                                },
                            },
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )
    model = _TwoStepModel(chunks)
    handler_calls = 0

    async def handler(_: ToolContext, a: int, b: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return a + b

    sdk = AgentSDK.for_test(store=store, acompletion=model, permission_default="allow")
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "two calls",
        )
        with pytest.raises(AgentSDKError) as raised:
            await run.result()

        assert raised.value.code is ErrorCode.INVALID_STATE
        assert raised.value.message == "multiple tool calls are not supported"
        assert handler_calls == 0
        snapshot = await sdk.runs.get(run.run_id)
        assert snapshot.status is RunStatus.FAILED
        assert snapshot.version == 3
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_two_sequential_model_tool_calls_complete_in_order() -> None:
    requests: list[dict[str, object]] = []
    handler_calls = 0
    store = InMemoryStore()

    async def acompletion(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        requests.append(kwargs)

        async def chunks() -> AsyncIterator[dict[str, object]]:
            if len(requests) == 1:
                tool_chunks = _tool_call_chunks(
                    '{"a":1,"b":2}',
                    call_id="call_one",
                )
                usage = {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                }
            elif len(requests) == 2:
                tool_chunks = _tool_call_chunks(
                    '{"a":3,"b":4}',
                    call_id="call_two",
                )
                usage = {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                }
            else:
                yield {
                    "choices": [
                        {"delta": {"content": "ten"}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": 4,
                    },
                }
                return
            chunk = dict(tool_chunks[0])
            chunk["usage"] = usage
            yield chunk

        return chunks()

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=acompletion,
        permission_default="allow",
    )
    try:
        async def handler(_: ToolContext, a: int, b: int) -> int:
            nonlocal handler_calls
            handler_calls += 1
            return a + b

        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "two sequential calls",
            idempotency_key="ordered-tools",
        )
        result = await asyncio.wait_for(run.result(), timeout=1)

        assert handler_calls == 2
        assert len(requests) == 3
        assert [item.value for item in result.tool_results] == [3, 7]
        assert result.output_text == "ten"
        assert result.usage == TokenUsage(
            prompt_tokens=6,
            completion_tokens=3,
            total_tokens=9,
        )
        messages = requests[2]["messages"]
        assert isinstance(messages, list)
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
        ]
        assert messages[1]["tool_calls"][0]["id"] == "call_one"
        assert messages[2]["tool_call_id"] == "call_one"
        assert messages[3]["tool_calls"][0]["id"] == "call_two"
        assert messages[4]["tool_call_id"] == "call_two"
        snapshot = await sdk.runs.get(run.run_id)
        assert snapshot.status is RunStatus.COMPLETED
        assert [item.value for item in snapshot.tool_results] == [3, 7]
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]
        assert event_types.count("tool.call.started") == 2
        assert event_types[-1] == "run.completed"

        replay = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "two sequential calls",
            idempotency_key="ordered-tools",
        )
        assert replay.run_id == run.run_id
        assert await replay.result() == result
        assert [item.value for item in (await replay.result()).tool_results] == [3, 7]
        assert handler_calls == 2
        assert len(requests) == 3
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_failure_after_tool_result_replays_durable_ordered_snapshot(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "failed-tool-replay.db"
    model_calls = 0

    async def acompletion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 2:
            raise RuntimeError("second model call failed")

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield _tool_call_chunks('{"a":2,"b":3}', call_id="call_one")[0]

        return chunks()

    async def handler(_: ToolContext, a: int, b: int) -> int:
        return a + b

    sdk = AgentSDK.for_test(
        database_path=database_path,
        acompletion=acompletion,
        permission_default="allow",
    )
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "fail after a tool result",
            idempotency_key="failed-tools",
        )

        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(run.result(), timeout=1)

        assert raised.value.message == "model call failed"
        snapshot = await sdk.runs.get(run.run_id)
        assert snapshot.status is RunStatus.FAILED
        assert [item.value for item in snapshot.tool_results] == [5]
    finally:
        await sdk.close()

    replay_provider_calls = 0

    async def must_not_call(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal replay_provider_calls
        replay_provider_calls += 1
        raise AssertionError("failed durable replay must not call provider")

    reopened = AgentSDK.for_test(
        database_path=database_path,
        acompletion=must_not_call,
        permission_default="allow",
    )
    try:
        _register_add(reopened, handler)
        replay = await reopened.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "fail after a tool result",
            idempotency_key="failed-tools",
        )
        assert replay.run_id == run.run_id
        assert replay.attached is False
        with pytest.raises(AgentSDKError) as replayed_failure:
            await asyncio.wait_for(replay.result(), timeout=1)
        assert replayed_failure.value.message == "model call failed"
        replayed_snapshot = await reopened.runs.get(replay.run_id)
        assert [item.value for item in replayed_snapshot.tool_results] == [5]
        assert replay_provider_calls == 0
        assert model_calls == 2
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_sqlite_reopen_preserves_terminal_snapshot_tool_results(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "agent-sdk.db"
    model = _TwoStepModel(_tool_call_chunks('{"a":4,"b":5}'))

    async def handler(_: ToolContext, a: int, b: int) -> int:
        return a + b

    sdk = AgentSDK.for_test(
        database_path=database_path,
        acompletion=model,
        permission_default="allow",
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        _register_add(sdk, handler)
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "persist the tool result",
            idempotency_key="persist-tools",
        )
        expected = await asyncio.wait_for(run.result(), timeout=1)
        run_id = run.run_id
    finally:
        await sdk.close()

    provider_calls = 0

    async def must_not_call(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("completed durable replay must not call provider")

    reopened = AgentSDK.for_test(
        database_path=database_path,
        acompletion=must_not_call,
        permission_default="allow",
    )
    try:
        _register_add(reopened, handler)
        replay = await reopened.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "persist the tool result",
            idempotency_key="persist-tools",
        )
        assert replay.attached is False
        result = await asyncio.wait_for(replay.result(), timeout=1)
        assert result == expected
        assert [item.value for item in result.tool_results] == [9]
        assert provider_calls == 0
        snapshot = await reopened.runs.get(run_id)
        assert snapshot.status is RunStatus.COMPLETED
        assert [item.value for item in snapshot.tool_results] == [9]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_ninth_sequential_tool_call_fails_before_handler() -> None:
    store = InMemoryStore()
    model_calls = 0
    handler_calls = 0

    async def acompletion(**_: object) -> AsyncIterator[dict[str, object]]:
        nonlocal model_calls
        model_calls += 1

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield _tool_call_chunks(
                '{"a":1,"b":2}',
                call_id=f"call_{model_calls}",
            )[0]

        return chunks()

    async def handler(_: ToolContext, a: int, b: int) -> int:
        nonlocal handler_calls
        handler_calls += 1
        return a + b

    sdk = AgentSDK.for_test(
        store=store,
        acompletion=acompletion,
        permission_default="allow",
    )
    try:
        _register_add(sdk, handler)
        session = await sdk.sessions.create(workspaces=[])
        run = await sdk.runs.start(
            session.session_id,
            AgentSpec(name="test", model="fake/model"),
            "exceed the tool step limit",
        )

        with pytest.raises(AgentSDKError) as raised:
            await asyncio.wait_for(run.result(), timeout=1)

        assert raised.value.code is ErrorCode.INVALID_STATE
        assert raised.value.message == "tool step limit exceeded"
        assert handler_calls == 8
        assert model_calls == 9
        snapshot = await sdk.runs.get(run.run_id)
        assert snapshot.status is RunStatus.FAILED
        event_types = [
            stored.event.type
            for stored in await store.read_events(after_cursor=0)
            if stored.event.run_id == run.run_id
        ]
        assert event_types.count("tool.call.started") == 8
        assert event_types[-2:] == ["step.failed", "run.failed"]
    finally:
        await sdk.close()
