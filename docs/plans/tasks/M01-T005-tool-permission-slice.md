# M01-T005 Tool and Permission Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute one user Tool through JSON Schema validation and a durable ask/resolve permission flow.

**Architecture:** Model tool-call events become ToolCall records. ToolRegistry resolves specs; PolicyEngine returns allow/deny/ask; an ask persists a PermissionRequest and pauses the Run until the application resolves it.

**Tech Stack:** Pydantic, asyncio, Runtime events/store.

## Global Constraints

- No Tool handler runs before authorization.
- Missing PermissionBridge behavior is deny, not allow.
- Tool result/error is normalized to `ToolResult`.

---

### Task 1: Add Tool contracts and permission wait

**Files:**
- Create: `src/agent_sdk/tools/models.py`
- Create: `src/agent_sdk/tools/registry.py`
- Create: `src/agent_sdk/tools/executor.py`
- Create: `src/agent_sdk/permissions/models.py`
- Create: `src/agent_sdk/permissions/policy.py`
- Create: `src/agent_sdk/permissions/broker.py`
- Modify: `src/agent_sdk/models/litellm_gateway.py`
- Modify: `src/agent_sdk/runtime/engine.py`
- Create: `tests/integration/tools/test_permissioned_tool_slice.py`

**Interfaces:**
- Produces: `ToolSpec`, `ToolContext`, `ToolResult`, `ToolRegistry.register/get`, `PermissionRequest`, `PermissionDecision`, `PolicyEngine.evaluate`, `PermissionBroker.resolve`.
- Consumes: `RunEngine`, `StateStore`, Model tool-call events.

- [ ] **Step 1: Write a test proving the handler waits**

```python
class AddInput(BaseModel): a: int; b: int
called = False
async def add(_: ToolContext, a: int, b: int) -> int:
    global called; called = True; return a + b

@pytest.mark.asyncio
async def test_tool_waits_for_permission(sdk: AgentSDK) -> None:
    sdk.tools.register(
        ToolSpec(name="add", description="Add two integers", input_schema=AddInput.model_json_schema(), effects=("execute",)),
        add,
    )
    run = await sdk.runs.start_tool_fixture("add", {"a": 2, "b": 3}, policy="ask")
    request = await sdk.permissions.next_request(run.run_id)
    assert called is False
    await sdk.permissions.resolve(request.request_id, PermissionDecision.allow_once())
    assert (await run.result()).tool_results[0].value == 5
```

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

- [ ] **Step 4: Implement Registry, policy, and Broker**

Registry rejects duplicate names. Slice Policy accepts exact `default_outcome` config. Broker persists `permission.requested`, changes Run to `waiting_permission`, and on one resolution persists `permission.resolved` before waking the engine.

```python
async def authorize(self, request: PermissionRequest) -> PermissionDecision:
    decision = self._policy.evaluate(request)
    if decision.action != "ask":
        return decision
    await self._commands.wait_for_permission(request)
    response = await self._responses.wait(request.request_id)
    await self._commands.resolve_permission(request.request_id, response)
    return response
```

- [ ] **Step 5: Extend gateway/engine for one ToolCall**

Normalize tool name/argument deltas to one `ToolCallCompleted`. Engine validates JSON with the registered schema, authorizes, executes with timeout, persists `tool.call.completed`, appends tool result to messages, and performs the next model Step.

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

- [ ] **Step 6: Verify ordered authorization**

Run: `uv run pytest tests/integration/tools/test_permissioned_tool_slice.py -v`

Expected: handler is not called before resolve; final result is 5; events contain requested/resolved/started/completed in order.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/tools src/agent_sdk/permissions src/agent_sdk/runtime src/agent_sdk/models tests/integration/tools
git commit -m "feat: add permissioned tool slice"
```
