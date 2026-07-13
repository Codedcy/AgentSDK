# M03-T001 Tool Registry and Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the vertical-slice tool path into a typed, cancellable, observable executor for built-in and user-registered tools.

**Architecture:** Tool definitions are immutable `ToolSpec` values stored in a deterministic registry. `ToolExecutor` owns validation, permission delegation, timeout/cancellation, output bounding, artifact offload, and lifecycle events; handlers only implement tool behavior.

**Tech Stack:** Python 3.12, Pydantic v2/JSON Schema, asyncio, pytest-asyncio.

## Global Constraints

- Tool schemas and versions are captured in every Run fingerprint.
- Registry ordering and schema hashes are deterministic.
- A timed-out or cancelled tool cannot later publish a successful result.
- Large or binary values become artifacts; model-facing output remains bounded.

---

### Task 1: Complete tool metadata and registry semantics

**Files:**
- Modify: `src/agent_sdk/tools/models.py`
- Modify: `src/agent_sdk/tools/registry.py`
- Create: `tests/unit/tools/test_registry.py`

**Interfaces:**
- Produces: `ToolSpec`, `ToolEffects`, `RegisteredTool`, `ToolRegistry.register/list/get/fingerprint`.
- Consumes: JSON-compatible schemas and async or sync Python handlers.

- [ ] **Step 1: Write failing registry tests**

```python
def test_registry_fingerprint_is_order_independent() -> None:
    first = ToolRegistry([tool("b"), tool("a")]).fingerprint()
    second = ToolRegistry([tool("a"), tool("b")]).fingerprint()
    assert first == second

def test_duplicate_name_requires_explicit_replace() -> None:
    registry = ToolRegistry([tool("read")])
    with pytest.raises(DuplicateToolError):
        registry.register(tool("read"))
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/tools/test_registry.py -v`

Expected: complete registry metadata and fingerprint behavior are missing.

- [ ] **Step 3: Implement immutable metadata and deterministic registry**

```python
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    version: str = "1"
    source: str = "application"
    effects: ToolEffects = ToolEffects.READ_ONLY
    idempotent: bool = False
    parallel_safe: bool = False
    timeout_seconds: float | None = None
    max_output_bytes: int = 64 * 1024

def fingerprint(self) -> str:
    payload = [entry.spec.canonical_dict() for entry in sorted(self._entries.values(), key=lambda e: e.spec.name)]
    return sha256(canonical_json(payload)).hexdigest()
```

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/tools/test_registry.py -v`

Expected: all registry tests pass.

```powershell
git add src/agent_sdk/tools/models.py src/agent_sdk/tools/registry.py tests/unit/tools/test_registry.py
git commit -m "feat: complete tool registry metadata"
```

---

### Task 2: Complete executor lifecycle, cancellation, and bounded results

**Files:**
- Modify: `src/agent_sdk/tools/executor.py`
- Modify: `src/agent_sdk/tools/models.py`
- Modify: `src/agent_sdk/storage/artifacts.py`
- Create: `tests/integration/tools/test_executor.py`

- [ ] **Step 1: Write failing executor tests**

```python
@pytest.mark.asyncio
async def test_timeout_wins_over_late_handler_return(executor, slow_tool) -> None:
    result = await executor.execute(call(slow_tool, timeout_seconds=0.01))
    assert result.status == "timed_out"
    assert executor.events.names()[-1] == "tool.completed"

@pytest.mark.asyncio
async def test_large_output_is_saved_as_artifact(executor, large_tool) -> None:
    result = await executor.execute(call(large_tool))
    assert result.artifact_refs
    assert len(result.model_content.encode()) <= large_tool.spec.max_output_bytes
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/tools/test_executor.py -v`

Expected: timeout terminality and artifact offload are not implemented.

- [ ] **Step 3: Implement one terminal-result arbiter**

```python
async def _invoke(self, registered: RegisteredTool, context: ToolContext, arguments: dict[str, Any]) -> Any:
    value = registered.handler(context, **arguments)
    return await value if inspect.isawaitable(value) else value

async def execute(self, request: ToolExecutionRequest) -> ToolResult:
    await self._events.append(tool_requested(request))
    decision = await self._permissions.authorize(request.permission_request())
    if not decision.allowed:
        return await self._finish(request, ToolResult.denied(decision.reason))
    try:
        async with asyncio.timeout(request.timeout_seconds):
            value = await self._invoke(request.tool, request.context, request.arguments)
            return await self._finish(request, await self._bound_output(value, request.tool.spec))
    except TimeoutError:
        return await self._finish(request, ToolResult.timed_out())
    except asyncio.CancelledError:
        await self._finish(request, ToolResult.cancelled())
        raise
```

- [ ] **Step 4: Add stable argument validation and secret-safe events**

Validate against the registered input schema before permission checks. Emit arguments through the redaction boundary and include tool name, version, schema hash, call id, timing, outcome, byte counts, and artifact references.

```python
def validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
    validator = Draft202012Validator(spec.input_schema)
    errors = sorted(validator.iter_errors(arguments), key=lambda item: list(item.path))
    if errors:
        raise ToolArgumentsError.from_jsonschema(errors)
    return dict(arguments)

await self._events.append(tool_started(request, arguments=self._redactor.apply(arguments)))
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/integration/tools/test_executor.py -v`

Expected: sync/async handlers, validation, denial, timeout, cancellation, artifacts, and lifecycle events pass.

```powershell
git add src/agent_sdk/tools src/agent_sdk/storage/artifacts.py tests/integration/tools/test_executor.py
git commit -m "feat: harden tool execution lifecycle"
```
