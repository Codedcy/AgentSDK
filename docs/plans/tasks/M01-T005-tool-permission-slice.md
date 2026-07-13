# M01-T005 Tool and Permission Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute one registered user Tool through JSON Schema validation and a durable ask/resolve permission flow inside the normal two-Step Agent Loop.

**Architecture:** LiteLLMGateway assembles fragmented model tool-call deltas into SDK `ToolCallCompleted` values. ToolRegistry resolves immutable specs, ToolExecutor validates and invokes handlers, and PolicyEngine returns allow/deny/ask. The headless in-process Permission bridge exposes requests through `sdk.permissions.next_request`; an ask atomically persists a waiting Run snapshot before the application resolves it, then the engine appends the normalized ToolResult to messages and performs the next model Step.

**Tech Stack:** Pydantic, JSON Schema Draft 2020-12, asyncio, Runtime events/store.

## Global Constraints

- No Tool handler runs before authorization.
- Missing PermissionBridge behavior is deny, not allow.
- Tool result/error is normalized to `ToolResult`.
- The normal `sdk.runs.start` path is used; no public/test-only `start_tool_fixture` shortcut may bypass model normalization, policy, or the durable Agent Loop.
- An `ask` commit changes the Run to `waiting_permission`; resolution changes it back to `running`. Each permission event and snapshot version is atomic and uses the Run emitter's single sequence lock.
- The SDK is headless: it exposes a cursor/request API and persists the decision, while the application alone decides when and how to display the request.
- The M01 slice supports one completed ToolCall per model Step and repeats model inference after the ToolResult; later hardening adds parallel calls and scoped grant caching.

---

### Task 1: Add Tool contracts and permission wait

**Files:**
- Create: `src/agent_sdk/tools/models.py`
- Create: `src/agent_sdk/tools/registry.py`
- Create: `src/agent_sdk/tools/executor.py`
- Create: `src/agent_sdk/permissions/models.py`
- Create: `src/agent_sdk/permissions/policy.py`
- Create: `src/agent_sdk/permissions/broker.py`
- Modify: `src/agent_sdk/api.py`
- Modify: `src/agent_sdk/config.py`
- Modify: `src/agent_sdk/models/litellm_gateway.py`
- Modify: `src/agent_sdk/runtime/engine.py`
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/__init__.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/integration/tools/test_permissioned_tool_slice.py`

**Interfaces:**
- Produces: `ToolSpec`, `ToolContext`, `ToolResult`, `ToolRegistry.register/get/list`, `ToolExecutor.execute`; `PermissionRequest`, `PermissionDecision`, `PolicyEngine.evaluate`, fail-closed `PermissionBroker.authorize/next_request/resolve`; public `sdk.tools` registration and headless `sdk.permissions` request/resolve APIs.
- Consumes: `RunEngine`, `StateStore`, Model tool-call events.

- [ ] **Step 1: Write a test proving the handler waits**

```python
class AddInput(BaseModel):
    a: int
    b: int

@pytest.mark.asyncio
async def test_tool_waits_for_permission(store: InMemoryStore) -> None:
    called = asyncio.Event()
    calls = 0
    async def scripted_acompletion(**_: object):
        nonlocal calls
        calls += 1
        async def chunks():
            if calls == 1:
                yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_add", "function": {"name": "add", "arguments": "{\\\"a\\\":2,"}}]}}]}
                yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\\\"b\\\":3}"}}]}, "finish_reason": "tool_calls"}]}
            else:
                yield {"choices": [{"delta": {"content": "5"}, "finish_reason": "stop"}]}
        return chunks()
    async def add(_: ToolContext, a: int, b: int) -> int:
        called.set()
        return a + b

    sdk = AgentSDK.for_test(store=store, acompletion=scripted_acompletion, permission_default="ask")
    sdk.tools.register(
        ToolSpec(name="add", description="Add two integers", input_schema=AddInput.model_json_schema(), effects=("execute",)),
        add,
    )
    session = await sdk.sessions.create(workspaces=[])
    run = await sdk.runs.start(session.session_id, AgentSpec(name="test", model="fake/model"), "add 2 and 3")
    request = await sdk.permissions.next_request(run.run_id)
    assert not called.is_set()
    assert (await sdk.runs.get(run.run_id)).status is RunStatus.WAITING_PERMISSION
    await sdk.permissions.resolve(request.request_id, PermissionDecision.allow_once())
    assert (await run.result()).tool_results[0].value == 5
    assert (await sdk.runs.get(run.run_id)).status is RunStatus.COMPLETED
```

Also add focused tests for fragmented mapping/attribute tool-call chunks, invalid JSON/JSON Schema rejection before authorization, direct allow and deny, missing bridge fail-closed without waiting or invoking the handler, duplicate/unknown resolution, handler exception/timeout normalization, a second model request containing the assistant ToolCall and ToolResult, and exact request/resolution/authorization/start/completion event order. Prove that Run events and snapshots expose `waiting_permission` while the handler remains uncalled.

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/tools/test_permissioned_tool_slice.py -v`

Expected: missing Tool/Permission imports.

- [ ] **Step 3: Implement Tool and permission models**

```python
class PermissionEffect(BaseModel):
    action: str; resource: str
class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str; description: str; input_schema: dict[str, Any]
    version: str = "1"; source: str = "application"; effects: tuple[str, ...] = ()
    timeout_seconds: float | None = None
class PermissionDecision(BaseModel):
    action: Literal["allow", "deny", "ask"]
    scope: Literal["once", "run", "session", "persistent"] | None = None
```

Use frozen, `extra="forbid"` SDK models and detach nested schema/result inputs at boundaries. `ToolResult` has a stable status (`succeeded|denied|failed|timed_out|invalid_arguments`), model-facing content, JSON-compatible value when safe, and a sanitized error/reason. `RunResult` gains an immutable ordered `tool_results` tuple without changing text-only behavior. Add `RunStatus.WAITING_PERMISSION`.

- [ ] **Step 4: Implement Registry, policy, and Broker**

Registry rejects duplicate names and emits OpenAI-compatible function schemas in deterministic name order. Slice Policy accepts exact `default_outcome` (`allow|deny|ask`); `AgentSDKConfig.permission_default` defaults to `ask` and `AgentSDK.for_test` can override it. The in-process bridge owns per-Run request queues and request-id futures. Broker delegates event/snapshot writes through callbacks supplied by the Run emitter, so it never allocates Run sequence numbers independently. It publishes a request only after `permission.requested` plus the waiting snapshot commits, accepts exactly one valid resolution, persists `permission.resolved` plus the running snapshot before waking execution, and removes/cancels pending futures if the Run task is cancelled. With no bridge, `ask` immediately becomes a sanitized deny and never hangs.

```python
async def authorize(self, request: PermissionRequest) -> PermissionDecision:
    decision = self._policy.evaluate(request)
    if decision.action != "ask":
        return decision
    if self._bridge is None:
        return PermissionDecision.deny("permission bridge unavailable")
    await self._on_requested(request)
    response = await self._bridge.wait(request)
    await self._on_resolved(request, response)
    return response
```

- [ ] **Step 5: Extend gateway/engine for one ToolCall**

Normalize tool id/name/argument fragments by call index to one detached `ToolCallCompleted` per call after the stream finishes. Gateway still yields usage and exactly one `ModelCompleted`; LiteLLM types never leave it. The engine includes registered schemas in each `ModelRequest`, commits `model.call.completed`, then `tool.call.proposed`. ToolExecutor parses JSON and validates Draft 2020-12 before policy. It emits/returns a normalized invalid/denied/failed/timed-out result without running the handler; for allow, emit `tool.call.authorized`, then `tool.call.started`, invoke the async handler under its timeout, and commit `tool.call.completed`. Append an assistant tool-call message and a tool result message, finish the current Step, and begin the next Step/model call. The final Run result includes all ToolResults and aggregated token usage across model calls.

```python
registered = self._tools.get(call.name)
arguments = registered.validate_json(call.arguments_json)
spec = registered.spec
decision = await self._permissions.authorize(spec.permission_request(arguments, run))
if not decision.allowed:
    outcome = ToolResult.denied(decision.reason)
else:
    async with asyncio.timeout(spec.timeout_seconds):
        value = await registered.handler(ToolContext.for_run(run), **arguments)
    outcome = ToolResult.succeeded(value)
await self._commands.complete_tool_call(run.run_id, call.call_id, outcome)
```

All tool/permission lifecycle events use the existing `_RunEmitter` and its emission lock. Generalize snapshot version transitions from the emitter's current Run snapshot: text-only Runs remain v1→v2→v3, while each waiting/running permission transition increments once before the terminal version. Never persist handler/provider exception text or a raw Python object. A ToolResult sent back to the model must be bounded JSON text.

- [ ] **Step 6: Verify ordered authorization**

Run: `uv run pytest tests/integration/tools/test_permissioned_tool_slice.py -v && uv run pytest -q && uv run ruff check src tests && uv run mypy src`

Expected: handler is not called before resolve; waiting/running snapshots are observable; final result is 5; the second model request receives the ToolResult; events contain proposed/requested/resolved/authorized/started/completed in order; all failure/deny/timeout paths are normalized and leave no pending bridge future or task.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/tools src/agent_sdk/permissions src/agent_sdk/runtime src/agent_sdk/models tests/integration/tools
git commit -m "feat: add permissioned tool slice"
```
