# M01-T010 Integrated Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the complete M01 capability set composes through public APIs, survives a quiescent SQLite reopen without re-execution, and removes every SDK-managed Session fact on deletion.

**Architecture:** Add only the two missing composition seams: an owned SQLite-path option on the existing deterministic test constructor and a lifecycle-aware Context façade. A standard-library reference CLI owns permission/Workflow confirmation and event presentation. The E2E test drives the same public composition with a scripted LiteLLM callable, a real stdio MCP fixture, a filesystem Skill, and the existing SQLite/Event/Workflow/Evaluation components.

**Tech Stack:** Python 3.12/3.13, asyncio, argparse, Pydantic v2, LiteLLM, SQLite/aiosqlite, MCP Python SDK stdio transport, pytest/pytest-asyncio.

## Global Constraints

- Follow `docs/design/features/12-integrated-vertical-slice.md`; M01 restart occurs only after active work and MCP connections are quiescent.
- Do not implement in-flight permission recovery, leases, reconciliation, built-in coding Tools, SDK-owned dynamic Workflow proposals, a server, or a dashboard; those remain M02/M03/M04/M06 work.
- The reference CLI imports SDK names only from the package root `agent_sdk`; tests may use private test fixtures but the example may not.
- LiteLLM remains the only model integration. Deterministic tests inject only the existing `acompletion` seam.
- MCP uses a real local stdio process and protocol revision `2025-11-25`; the E2E test performs no network call and needs no model credentials.
- The CLI and SDK never auto-approve a permission or generated Workflow. The application supplies explicit decisions.
- The application owns `MCPManager`, `SkillRegistry`, and `PromptComposer`; `sdk.close()` does not silently close application-owned components.
- Context, Workflow, Evaluation, analytics, and observability remain backed by the Session's durable Store facts. No example-only trace database or hidden scenario state is allowed.
- Session deletion removes SDK-managed facts but never deletes the application workspace file written by the example Tool.
- Preserve all existing public APIs and all 442 tests. No acceptance assertion may be xfailed, skipped by default, or replaced with a private Store assertion.

---

### Task 1: Add the owned SQLite deterministic-test constructor path

**Files:**
- Modify: `src/agent_sdk/api.py`
- Create: `tests/integration/test_sdk_sqlite_test_constructor.py`

**Interfaces:**
- Consumes: `_LazySQLiteStore`, `AgentSDK._initialize`, and existing named `store`/`acompletion` constructor arguments.
- Produces: `AgentSDK.for_test(*, acompletion, store=None, database_path=None, permission_default="ask", permission_bridge=DEFAULT_BRIDGE)` with exactly-one-of Store/path validation and correct Store ownership.

- [ ] **Step 1: Write RED tests for path reopen, ownership, and argument exclusivity**

Create `tests/integration/test_sdk_sqlite_test_constructor.py` with these behaviors:

```python
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import AgentSDK, AgentSDKError, ErrorCode, EventFilter
from agent_sdk.storage.memory import InMemoryStore


async def _unused_provider(**_: Any) -> object:
    raise AssertionError("reopen-only test must not call LiteLLM")


@pytest.mark.asyncio
async def test_for_test_database_path_is_owned_lazy_and_reopenable(tmp_path: Path) -> None:
    database = tmp_path / "owned.db"
    sdk = AgentSDK.for_test(database_path=database, acompletion=_unused_provider)
    session = await sdk.sessions.create(workspaces=[])
    await sdk.close()
    await sdk.close()

    reopened = AgentSDK.for_test(database_path=database, acompletion=_unused_provider)
    result = await reopened.queries.query_events(
        EventFilter(session_id=session.session_id)
    )
    assert [item.event.type for item in result.events] == ["session.created"]
    await reopened.close()


@pytest.mark.asyncio
async def test_for_test_does_not_own_injected_store() -> None:
    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=_unused_provider)
    session = await sdk.sessions.create(workspaces=[])
    await sdk.close()

    reused = AgentSDK.for_test(store=store, acompletion=_unused_provider)
    result = await reused.queries.query_events(
        EventFilter(session_id=session.session_id)
    )
    assert [item.event.type for item in result.events] == ["session.created"]
    await reused.close()


@pytest.mark.parametrize("case", ("neither", "both"))
def test_for_test_requires_exactly_one_store_source(case: str, tmp_path: Path) -> None:
    kwargs: dict[str, object] = {"acompletion": _unused_provider}
    if case == "both":
        kwargs.update(store=InMemoryStore(), database_path=tmp_path / "bad.db")
    with pytest.raises(AgentSDKError) as captured:
        AgentSDK.for_test(**kwargs)  # type: ignore[arg-type]
    assert captured.value.code is ErrorCode.INVALID_STATE
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
```

- [ ] **Step 2: Run the constructor tests and observe RED**

Run:

```powershell
uv run --python 3.13 pytest tests/integration/test_sdk_sqlite_test_constructor.py -v
```

Expected: collection or calls fail because `database_path` is not accepted and `store` is still required.

- [ ] **Step 3: Implement exactly-one selection without opening SQLite eagerly**

Change the `for_test` signature and source selection in `src/agent_sdk/api.py` to this shape:

```python
@classmethod
def for_test(
    cls,
    *,
    acompletion: _ACompletion,
    store: StateStore | None = None,
    database_path: str | Path | None = None,
    permission_default: _PermissionDefault = "ask",
    permission_bridge: InProcessPermissionBridge | None | object = (
        _DEFAULT_PERMISSION_BRIDGE
    ),
) -> AgentSDK:
    if (store is None) == (database_path is None):
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "exactly one test Store or database path is required",
            retryable=False,
        )
    selected_store: StateStore
    owned_close: Callable[[], Awaitable[None]] | None
    if database_path is not None:
        lazy_store = _LazySQLiteStore(Path(database_path))
        selected_store = lazy_store
        owned_close = lazy_store.close
    else:
        assert store is not None
        selected_store = store
        owned_close = None
    sdk = cls.__new__(cls)
    bridge = (
        InProcessPermissionBridge()
        if permission_bridge is _DEFAULT_PERMISSION_BRIDGE
        else cast(InProcessPermissionBridge | None, permission_bridge)
    )
    sdk._initialize(
        selected_store,
        LiteLLMGateway._for_test(acompletion),
        permission_default=permission_default,
        permission_bridge=bridge,
        owned_close=owned_close,
    )
    return sdk
```

Do not expose `_LazySQLiteStore`, do not open the file until the first Store call, and do not close an injected Store.

- [ ] **Step 4: Run focused and compatibility tests**

Run:

```powershell
uv run --python 3.13 pytest tests/integration/test_sdk_sqlite_test_constructor.py tests/integration/runtime tests/integration/observability -q
uv run --python 3.13 ruff check src/agent_sdk/api.py tests/integration/test_sdk_sqlite_test_constructor.py
uv run --python 3.13 mypy src
```

Expected: all commands pass and every existing `for_test(store=...)` caller remains unchanged.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/agent_sdk/api.py tests/integration/test_sdk_sqlite_test_constructor.py
git commit -m "feat: add recoverable SQLite test SDK"
```

---

### Task 2: Add a lifecycle-aware public Context façade

**Files:**
- Modify: `src/agent_sdk/api.py`
- Modify: `src/agent_sdk/__init__.py`
- Create: `tests/integration/context/test_public_context_api.py`

**Interfaces:**
- Consumes: `ContextPlanner`, `ContextRetrieval`, the SDK's shared `StateStore`, `LiteLLMGateway`, and `_SDKLifecycle`.
- Produces: root-exported `ContextAPI`; `sdk.context.build`, `sdk.context.get_capsule`, and `sdk.context.read_sources`.

- [ ] **Step 1: Write RED public-only Context façade tests**

Create `tests/integration/context/test_public_context_api.py`. Use only root SDK imports for public contracts and `InMemoryStore` as the injected test adapter. The provider returns text for the Run and a structured Capsule when `stream=False`:

```python
from collections.abc import AsyncIterator
import json
from typing import Any

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    AgentSpec,
    CompactionLevel,
    ContextAPI,
    ErrorCode,
    ObservedEvent,
)
from agent_sdk.storage.memory import InMemoryStore


async def _provider(**params: Any) -> object:
    if params["stream"] is False:
        document = json.loads(params["messages"][1]["content"])
        source_ids = [item["event_id"] for item in document["sources"]]
        return {
            "choices": [{"message": {"parsed": {
                "objective": "retain the completed run",
                "constraints": [],
                "decisions": [],
                "facts": ["run completed"],
                "next_actions": ["verify after reopen"],
                "artifact_refs": [],
                "source_event_ids": source_ids,
            }}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        }

    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}
        yield {"choices": [], "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
        }}
    return chunks()


@pytest.mark.asyncio
async def test_context_facade_builds_retrieves_and_deletes_session_capsule() -> None:
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=_provider)
    assert isinstance(sdk.context, ContextAPI)
    session = await sdk.sessions.create(workspaces=[])
    agent = sdk.agents.define(AgentSpec(name="main", model="fake/main"))
    run = await sdk.runs.start(session.session_id, agent, "retain this input")
    await run.result()

    view = await sdk.context.build(
        session.session_id,
        model="gpt-4o-mini",
        model_window=8_192,
        force_level=CompactionLevel.L3,
    )
    assert view.applied_level is CompactionLevel.L3
    assert view.capsule_id is not None
    capsule = await sdk.context.get_capsule(
        view.capsule_id,
        session_id=session.session_id,
    )
    sources = await sdk.context.read_sources(
        view.capsule_id,
        session_id=session.session_id,
    )
    assert capsule.source_event_ids == tuple(item.event.event_id for item in sources)
    assert all(isinstance(item, ObservedEvent) for item in sources)

    await sdk.sessions.delete(session.session_id)
    with pytest.raises(AgentSDKError) as missing:
        await sdk.context.get_capsule(view.capsule_id, session_id=session.session_id)
    assert missing.value.code is ErrorCode.NOT_FOUND
    await sdk.close()
```

Add a second test that closes the SDK before `context.build` and asserts stable `INVALID_STATE` with message `SDK is closing` and zero provider calls.

- [ ] **Step 2: Run the Context façade test and observe RED**

Run:

```powershell
uv run --python 3.13 pytest tests/integration/context/test_public_context_api.py -v
```

Expected: import or attribute access fails because `ContextAPI` and `sdk.context` do not exist.

- [ ] **Step 3: Implement the thin façade**

Add this class in `src/agent_sdk/api.py` using the existing failure behavior of Planner/Retrieval:

```python
class ContextAPI:
    def __init__(
        self,
        store: StateStore,
        models: LiteLLMGateway,
        lifecycle: _SDKLifecycle,
    ) -> None:
        self._store = store
        self._models = models
        self._lifecycle = lifecycle
        self._retrieval = ContextRetrieval(store)

    async def build(
        self,
        session_id: str,
        *,
        model: str,
        model_window: int,
        output_reserve: int = 0,
        tool_schema_tokens: int = 0,
        safety_reserve: int = 0,
        policy: CompactionPolicy | None = None,
        force_level: CompactionLevel | str | None = None,
        protected_event_ids: Iterable[str] = (),
    ) -> ContextView:
        async with self._lifecycle.admit():
            planner = ContextPlanner(
                self._store,
                self._models,
                model=model,
                model_window=model_window,
                output_reserve=output_reserve,
                tool_schema_tokens=tool_schema_tokens,
                safety_reserve=safety_reserve,
                policy=policy,
            )
            return await planner.build(
                session_id,
                force_level=force_level,
                protected_event_ids=protected_event_ids,
            )

    async def get_capsule(
        self,
        capsule_id: str,
        *,
        session_id: str,
    ) -> ContextCapsule:
        async with self._lifecycle.admit():
            return await self._retrieval.get_capsule(
                capsule_id,
                session_id=session_id,
            )

    async def read_sources(
        self,
        capsule_id: str,
        *,
        session_id: str,
    ) -> tuple[ObservedEvent, ...]:
        async with self._lifecycle.admit():
            stored = await self._retrieval.read_sources(
                capsule_id,
                session_id=session_id,
            )
            return tuple(
                ObservedEvent(cursor=item.cursor, event=item.event)
                for item in stored
            )
```

Wire `self.context = ContextAPI(store, models, self._lifecycle)` in `_initialize` and export `ContextAPI` from `agent_sdk.__init__`. Do not expose the Store or gateway.

- [ ] **Step 4: Verify Context deletion, cancellation, and existing behavior**

Run:

```powershell
uv run --python 3.13 pytest tests/integration/context -q
uv run --python 3.13 pytest tests/integration/observability/test_public_observability_api.py -q
uv run --python 3.13 ruff check src tests
uv run --python 3.13 mypy src
```

Expected: all pass; the façade adds no alternate persistence path and close rejects new Context calls.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/agent_sdk/api.py src/agent_sdk/__init__.py tests/integration/context/test_public_context_api.py
git commit -m "feat: expose context facade"
```

---

### Task 3: Support bounded sequential Tool steps in the Agent Loop

**Files:**
- Modify: `src/agent_sdk/runtime/engine.py`
- Modify: `tests/integration/tools/test_permissioned_tool_slice.py`

**Interfaces:**
- Consumes: the existing one-Tool-per-model-step loop, durable Tool events,
  accumulated `RunResult.tool_results`, and usage aggregation.
- Produces: up to eight sequential Tool-bearing model steps in one Run while retaining
  the rejection of multiple ToolCalls in one model response.

- [ ] **Step 1: Turn the existing second-step rejection into a RED success contract**

Rename `test_second_model_tool_call_fails_before_second_handler` to
`test_two_sequential_model_tool_calls_complete_in_order`. Keep its three-turn provider
(first ToolCall, second ToolCall, final text), then replace the failure assertions with:

```python
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
snapshot = await sdk.runs.get(run.run_id)
assert snapshot.status is RunStatus.COMPLETED
event_types = [
    stored.event.type
    for stored in await store.read_events(after_cursor=0)
    if stored.event.run_id == run.run_id
]
assert event_types.count("tool.call.started") == 2
assert event_types[-1] == "run.completed"
```

Also assert the third recorded LiteLLM request contains two assistant ToolCall messages
and two matching Tool-result messages in chronological order. This proves the second
decision sees the first result and the final decision sees both; do not merely count
handler calls.

- [ ] **Step 2: Add a RED fixed-ceiling contract**

Add `test_ninth_sequential_tool_call_fails_before_handler` with a provider that returns
one uniquely identified `add` ToolCall on every request. Run with
`permission_default="allow"` and assert:

```python
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
```

Keep `test_multiple_tool_calls_in_one_step_fail_stably` unchanged and green.

- [ ] **Step 3: Run the focused tests and observe RED**

Run:

```powershell
uv run --python 3.13 pytest tests/integration/tools/test_permissioned_tool_slice.py `
  -k "two_sequential or ninth_sequential or multiple_tool_calls_in_one_step" -v
```

Expected: the two sequential-call contracts fail because the second call is still
rejected as `additional tool calls are not supported`; the same-step rejection passes.

- [ ] **Step 4: Implement the smallest bounded loop change**

In `src/agent_sdk/runtime/engine.py`, add:

```python
_MAX_TOOL_STEPS = 8
```

Replace the `if calls and tool_results` rejection with:

```python
if calls and len(tool_results) >= _MAX_TOOL_STEPS:
    failure = AgentSDKError(
        ErrorCode.INVALID_STATE,
        "tool step limit exceeded",
        retryable=False,
    )
    await self._fail_run(
        emitter,
        failure,
        chunks,
        usage,
        tool_results,
    )
    await emitter.close()
    raise failure
```

Do not change the `len(calls) > 1` branch. Reuse the existing message append,
ToolExecutor, permission, event, failure, usage, and terminal persistence paths; do not
introduce parallel Tool execution or another scheduler.

- [ ] **Step 5: Verify sequential permission and regression behavior**

Run:

```powershell
uv run --python 3.13 pytest tests/integration/runtime tests/integration/tools -q
uv run --python 3.13 pytest tests/integration/mcp/test_mcp_tool_slice.py -q
uv run --python 3.13 ruff check src/agent_sdk/runtime/engine.py tests/integration/tools/test_permissioned_tool_slice.py
uv run --python 3.13 mypy src
```

Expected: sequential calls preserve ordered ToolResults and accumulated usage, the
ninth call never invokes a handler, permission semantics remain unchanged, and a single
model response with multiple ToolCalls is still rejected.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/agent_sdk/runtime/engine.py tests/integration/tools/test_permissioned_tool_slice.py
git commit -m "feat: support bounded sequential tool steps"
```

---

### Task 4: Build the public-only reference CLI runner

**Files:**
- Create: `examples/__init__.py`
- Create: `examples/reference_cli/__init__.py`
- Create: `examples/reference_cli/runner.py`
- Create: `examples/reference_cli/main.py`
- Create: `tests/integration/examples/test_reference_cli.py`
- Create: `README.md`

**Interfaces:**
- Consumes: `AgentSDK`, `AgentSpec`, `EventFilter`, `PermissionRequest`, `PermissionDecision`, `WorkflowCompiler`, and `WorkflowResult` from the package root.
- Produces: `RunExecution`, `execute_run`, `run_workflow_if_approved`,
  `register_workspace_write`, `ReferenceApplicationResult`, `run_application`, and
  `python -m examples.reference_cli.main`.

- [ ] **Step 1: Write RED runner tests and a private-import contract**

Create `tests/integration/examples/test_reference_cli.py` with:

```python
import asyncio
import ast
from pathlib import Path

import pytest

from agent_sdk import (
    AgentSDK,
    AgentSpec,
    PermissionDecision,
    PermissionRequest,
    WorkflowRunStatus,
)
from agent_sdk.storage.memory import InMemoryStore
from examples.reference_cli.runner import (
    _settle_permission_waiter,
    execute_run,
    run_workflow_if_approved,
)


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


@pytest.mark.asyncio
async def test_runner_resolves_permission_collects_events_and_approves_workflow() -> None:
    model = ReferenceRunnerModel()
    sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=model)
    sdk.agents.define(AgentSpec(name="main", revision="1", model="fake/main"))
    sdk.agents.define(AgentSpec(name="planner", revision="1", model="fake/planner"))
    sdk.agents.define(AgentSpec(name="worker", revision="1", model="fake/worker"))
    register_add_tool(sdk)
    session = await sdk.sessions.create(workspaces=[])
    permission_names: list[str] = []
    displayed: list[dict[str, object]] = []

    async def allow(request):
        permission_names.append(request.tool_name)
        return PermissionDecision.allow_once()

    execution = await execute_run(
        sdk,
        session.session_id,
        sdk.agents.define(AgentSpec(name="entry", revision="1", model="fake/entry")),
        "produce a workflow",
        resolve_permission=allow,
        emit=displayed.append,
    )
    workflow = await run_workflow_if_approved(
        sdk,
        session.session_id,
        execution.result.output_text,
        approve=lambda _: async_true(),
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
    register_add_tool(sdk)
    session = await sdk.sessions.create(workspaces=[])
    entered = asyncio.Event()

    async def wait_for_cancellation(_):
        entered.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    execution = asyncio.create_task(execute_run(
        sdk,
        session.session_id,
        sdk.agents.define(AgentSpec(name="entry", revision="1", model="fake/entry")),
        "produce a workflow",
        resolve_permission=wait_for_cancellation,
        emit=lambda _: None,
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    await asyncio.wait_for(sdk.close(), timeout=1)


@pytest.mark.asyncio
async def test_runner_cancellation_before_permission_delivery_leaves_no_waiter() -> None:
    entered_model = asyncio.Event()
    release_model = asyncio.Event()

    async def blocked_provider(**_):
        entered_model.set()
        await release_model.wait()
        return _text_stream("done")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=blocked_provider,
    )
    session = await sdk.sessions.create(workspaces=[])

    async def allow(_) -> PermissionDecision:
        return PermissionDecision.allow_once()

    execution = asyncio.create_task(execute_run(
        sdk,
        session.session_id,
        sdk.agents.define(AgentSpec(name="blocked", model="fake/blocked")),
        "wait before a tool request",
        resolve_permission=allow,
        emit=lambda _: None,
    ))
    await asyncio.wait_for(entered_model.wait(), timeout=1)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    release_model.set()
    await asyncio.wait_for(sdk.close(), timeout=1)


@pytest.mark.asyncio
async def test_completed_permission_waiter_is_recovered_for_cleanup() -> None:
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
```

Define the test helpers in that file rather than importing another test module:

```python
from collections.abc import AsyncIterator
from typing import Any

from agent_sdk import ToolContext, ToolSpec

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
        yield {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
        yield {"choices": [], "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
        }}
    return generate()


def _add_stream() -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call_add",
                "function": {"name": "add", "arguments": '{"a":2,"b":3}'},
            }]},
            "finish_reason": "tool_calls",
        }]}
    return generate()


class ReferenceRunnerModel:
    def __init__(self) -> None:
        self.entry_calls = 0

    async def __call__(self, **params: Any) -> AsyncIterator[dict[str, object]]:
        model = str(params["model"])
        if model == "fake/entry":
            self.entry_calls += 1
            if self.entry_calls == 1:
                return _add_stream()
            return _text_stream(WORKFLOW_YAML)
        return _text_stream("done")


def register_add_tool(sdk: AgentSDK) -> None:
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


async def async_true() -> bool:
    return True
```

In the same RED file, add
`test_run_application_activates_skill_executes_workflow_and_emits_evaluation_analytics`.
Create a temporary valid Skill with one reference, an expected-output file containing
`WORKFLOW_YAML`, and an argparse Namespace from `build_parser().parse_args(argv)` with a
concrete argument list. Use a scripted provider whose main model requests `write_note`
then returns `WORKFLOW_YAML`, whose structured branch returns a valid Capsule, and whose
planner/worker models return terminal text. Inject an in-memory SDK into
`run_application` with allow/approve callbacks and assert:

```python
assert (workspace / "result.txt").read_text(encoding="utf-8") == "hello"
assert "temporary skill instructions" in model.first_user_message
assert "temporary reference" in model.first_user_message
assert permission_names == ["write_note"]
assert {
    "context.view",
    "prompt.manifest",
    "workflow.completed",
    "evaluation.result",
    "analytics.success_rate",
    "analytics.tool_failures",
} <= {str(record["type"]) for record in displayed}
assert next(
    record for record in displayed if record["type"] == "evaluation.result"
)["verdict"] == "pass"
```

Also add a parser test proving the positional prompt and three required options are
required. This is an injected application test, not a subprocess/provider protocol;
the real stdio MCP path remains covered by Task 5.

- [ ] **Step 2: Run the example tests and observe RED**

Run:

```powershell
uv run --python 3.13 pytest tests/integration/examples/test_reference_cli.py -v
```

Expected: import fails because the example package and runner do not exist.

- [ ] **Step 3: Implement a cancellation-safe run/permission/event coordinator**

Create `examples/reference_cli/runner.py` with package-root imports and these complete public contracts:

```python
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from agent_sdk import (
    AgentSDK,
    AgentSpec,
    EventFilter,
    ObservedEvent,
    PermissionDecision,
    PermissionRequest,
    RunResult,
    ToolContext,
    ToolSpec,
    WorkflowCompiler,
    WorkflowIR,
    WorkflowResult,
)

PermissionResolver = Callable[[PermissionRequest], Awaitable[PermissionDecision]]
WorkflowApprover = Callable[[WorkflowIR], Awaitable[bool]]
EventSink = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class RunExecution:
    run_id: str
    result: RunResult
    events: tuple[ObservedEvent, ...]


async def _collect_run_events(
    sdk: AgentSDK,
    run_id: str,
    emit: EventSink,
) -> tuple[ObservedEvent, ...]:
    collected: list[ObservedEvent] = []
    async for item in sdk.events.subscribe(
        filters=EventFilter(run_id=run_id),
        cursor=0,
    ):
        collected.append(item)
        emit({
            "cursor": item.cursor,
            "type": item.event.type,
            "run_id": item.event.run_id,
            "payload": item.event.model_dump(mode="json")["payload"],
        })
        if item.event.type in {"run.completed", "run.failed"}:
            return tuple(collected)
    return tuple(collected)


async def _settle_permission_waiter(
    waiter: asyncio.Task[PermissionRequest],
) -> PermissionRequest | None:
    if waiter.done():
        if waiter.cancelled():
            return None
        return waiter.result()
    waiter.cancel()
    with suppress(asyncio.CancelledError):
        await waiter
    return None


async def execute_run(
    sdk: AgentSDK,
    session_id: str,
    agent: AgentSpec,
    user_input: str,
    *,
    resolve_permission: PermissionResolver,
    emit: EventSink,
) -> RunExecution:
    handle = await sdk.runs.start(session_id, agent, user_input)
    monitor = asyncio.create_task(_collect_run_events(sdk, handle.run_id, emit))
    result_waiter = asyncio.create_task(handle.result())
    pending_request: PermissionRequest | None = None
    permission_waiter: asyncio.Task[PermissionRequest] | None = None
    try:
        while not result_waiter.done():
            permission_waiter = asyncio.create_task(
                sdk.permissions.next_request(handle.run_id)
            )
            done, _ = await asyncio.wait(
                {result_waiter, permission_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if result_waiter in done:
                delivered = await _settle_permission_waiter(permission_waiter)
                permission_waiter = None
                if delivered is not None:
                    await sdk.permissions.resolve(
                        delivered.request_id,
                        PermissionDecision.deny("Run already terminated"),
                    )
                break
            pending_request = await permission_waiter
            permission_waiter = None
            decision = await resolve_permission(pending_request)
            await sdk.permissions.resolve(pending_request.request_id, decision)
            pending_request = None
        result = await result_waiter
        events = await monitor
        return RunExecution(run_id=handle.run_id, result=result, events=events)
    except BaseException:
        if permission_waiter is not None:
            recovered: PermissionRequest | None = None
            with suppress(BaseException):
                recovered = await _settle_permission_waiter(permission_waiter)
            if pending_request is None:
                pending_request = recovered
        if pending_request is not None:
            cleanup = asyncio.create_task(sdk.permissions.resolve(
                pending_request.request_id,
                PermissionDecision.deny("reference runner stopped"),
            ))
            with suppress(BaseException):
                await asyncio.shield(cleanup)
        if not result_waiter.done():
            result_waiter.cancel()
        with suppress(BaseException):
            await result_waiter
        if not monitor.done():
            monitor.cancel()
        with suppress(BaseException):
            await monitor
        raise


def register_workspace_write(sdk: AgentSDK, workspace: Path) -> None:
    target = (workspace / "result.txt").resolve()
    root = workspace.resolve()
    target.relative_to(root)

    async def write_note(_: ToolContext, content: str) -> dict[str, object]:
        target.write_text(content, encoding="utf-8")
        return {"path": str(target), "bytes": len(content.encode("utf-8"))}

    sdk.tools.register(
        ToolSpec(
            name="write_note",
            description="Write result.txt inside the configured workspace",
            input_schema={
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
                "additionalProperties": False,
            },
            effects=("filesystem.write",),
        ),
        write_note,
    )
```

The broad `BaseException` block first cancels and settles a request waiter that has not
delivered anything. If that waiter completed in the race before assignment, cleanup
extracts and denies its request. It then denies any request already delivered to the
application, settles the result/display waiter tasks, and immediately re-raises. It
must not translate `CancelledError`, `KeyboardInterrupt`, or `SystemExit`. Because M01
has no public Run-cancel command and SDK close waits for active Runs, cancellation of
this coordinator does not claim to cancel an in-flight provider call. The caller must
let that Run quiesce (or terminate the process); resumable/cancellable Run control is
M02 scope.

- [ ] **Step 4: Implement explicit generated-Workflow approval**

Add to the same runner:

```python
async def run_workflow_if_approved(
    sdk: AgentSDK,
    session_id: str,
    document: str,
    *,
    approve: WorkflowApprover,
    emit: EventSink,
) -> WorkflowResult | None:
    try:
        workflow = WorkflowCompiler().compile_yaml(document)
    except (TypeError, ValueError):
        emit({"type": "workflow.candidate.invalid"})
        return None
    emit({
        "type": "workflow.candidate.valid",
        "name": workflow.name,
        "definition_hash": workflow.definition_hash,
    })
    if not await approve(workflow):
        emit({"type": "workflow.candidate.rejected"})
        return None
    handle = await sdk.workflows.start(session_id, workflow)
    async for stored in handle.events():
        emit({
            "cursor": stored.cursor,
            "type": stored.event.type,
            "run_id": stored.event.run_id,
            "payload": stored.event.model_dump(mode="json")["payload"],
        })
    return await handle.result()
```

The invalid candidate path persists no Workflow fact. Do not add `workflow.proposed` or approval events to the SDK in M01.

- [ ] **Step 5: Implement the argparse application and README quickstart**

Create `examples/reference_cli/main.py` with:

- one required positional `prompt` plus required `--database`, `--workspace`, and
  `--model` options;
- optional `--planner-model`, `--worker-model`, and `--context-model`, each defaulting
  to `--model`, plus `--model-window` defaulting to `128000`;
- repeatable `--skill-root`, optional `--skill-name`, and optional `--skill-resource`;
- optional `--mcp-command`, repeatable `--mcp-arg`, and `--mcp-name` defaulting to `demo`;
- optional `--expected-output-file`; when present, evaluate the main Run with
  `ExactOutputEvaluator`, display its immutable result, and then display success-rate
  analytics; when absent, display the explicit no-sample success-rate result;
- `asyncio.to_thread(input, prompt)` callbacks that return `PermissionDecision.allow_once()` only for an explicit `y`, otherwise `PermissionDecision.deny()`; and that approve a validated Workflow only for explicit `y`;
- one `AgentSDK(AgentSDKConfig(database_path=args.database,
  permission_default="ask"))`, one `MCPManager(sdk.tools)` when configured, and one
  `SkillRegistry(args.skill_root)`;
- registration of `write_note` plus `main:1`, `planner:1`, and `worker:1` Agent
  revisions using their configured models; the CLI states those available revisions in
  the application text supplied to the main Run;
- optional Skill activation/resource loading only when `--skill-name` is supplied,
  followed by `execute_run` with the combined application text and positional prompt;
- display of the final model text as the bounded Workflow candidate, explicit approval
  through `run_workflow_if_approved`, one forced L3 Context View, its coding Prompt
  Manifest, then the public Evaluation (when configured), success-rate, and Tool-failure
  analytics records;
- `finally` ordering: close MCP manager if created, then close SDK;
- JSON-line output through `json.dumps(record, ensure_ascii=False, default=str)`.
- a top-level `AgentSDKError` boundary that emits only its stable `code`, `message`,
  and `retryable` fields and exits nonzero, without printing a traceback or third-party
  response; argparse usage errors keep argparse's normal exit code.

Keep construction separate from orchestration so the application path is testable
without adding a fake-provider option to the CLI. Define this immutable example result:

```python
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
```

The orchestration signature is `run_application(args: argparse.Namespace, *, sdk:
AgentSDK, session_id: str | None = None,
resolve_permission: PermissionResolver, approve_workflow: WorkflowApprover,
emit: EventSink) -> ReferenceApplicationResult`. The optional Session id is an injected
test/E2E seam; the real CLI omits it and therefore creates its own Session. The owning
entry point is:

```python
async def async_main(args: argparse.Namespace) -> int:
    sdk = AgentSDK(AgentSDKConfig(
        database_path=args.database,
        permission_default="ask",
    ))
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
```

Implement `run_application` in this exact order:

1. Resolve the workspace, register `write_note`, and create an application-owned
   `MCPManager`; connect the configured stdio server before starting a Run.
2. Discover the configured Skill roots. If `--skill-name` is present, activate exactly
   that Skill and read the optional resource through `ActivatedSkill.read_text`; append
   only those application-selected strings to the user input.
3. Define `main:1`, `planner:1`, and `worker:1` with the main/planner/worker model
   options. Use the injected Session or create one scoped to the workspace; include the
   available Workflow revisions plus the positional prompt in the main input.
4. Call `execute_run`. Build one forced L3 Context View with the effective context model
   and `args.model_window`; compose the `coding` Prompt with `PromptComposer`, the
   application-selected Skill text/resource, the Context View, and current public Tool
   schemas. Emit `context.view` and `prompt.manifest` using JSON-mode model dumps. This
   records the M01 composition boundary; it does not claim the BuiltPrompt was injected
   into the already-completed Run.
5. Emit a `workflow.candidate.text` record containing the final
   text, and call `run_workflow_if_approved`. A rejected/invalid candidate returns
   `None` without starting a Workflow and is not converted to SDK state; the application
   still proceeds to its configured Evaluation and analytics display.
6. When `--expected-output-file` is present, read it as UTF-8, call
   `sdk.evaluations.evaluate(execution.run_id,
   ExactOutputEvaluator(expected=expected_output))`, and emit
   `evaluation.result` from `model_dump(mode="json")`. Always query and emit
   `analytics.success_rate` and `analytics.tool_failures`, including `value=None` when
   no Evaluation exists.
7. Return `ReferenceApplicationResult` containing those exact immutable records. In a
   `finally`, close the `MCPManager` before returning or propagating any error.
   `run_application` never closes its injected SDK; `async_main` owns that lifecycle.

No `ContextPlanner`, Store, engine, or other private import is permitted in this file.
Do not add BuiltPrompt injection to the M01 Run API; that integration remains the M03
prompt-runtime work.

Create a minimal `README.md` that labels the example as an M01 reference consumer, gives this command, and states the quiescent-restart boundary:

```powershell
uv run --python 3.13 python -m examples.reference_cli.main `
  "Write result.txt and return an approved two-node Workflow YAML" `
  --database .agent-sdk/state.db `
  --workspace . `
  --model openai/gpt-4o-mini
```

- [ ] **Step 6: Verify the CLI runner and root-import boundary**

Run:

```powershell
uv run --python 3.13 pytest tests/integration/examples/test_reference_cli.py -v
uv run --python 3.13 python -m examples.reference_cli.main --help
uv run --python 3.13 ruff check examples tests/integration/examples
uv run --python 3.13 mypy src
```

Expected: tests pass, help exits 0 without opening SQLite or LiteLLM, and the AST assertion finds no private SDK import.

- [ ] **Step 7: Commit Task 4**

```powershell
git add examples README.md tests/integration/examples
git commit -m "feat: add public reference CLI"
```

---

### Task 5: Prove the complete SQLite/MCP/Skill/Context/Workflow/Evaluation slice

**Files:**
- Create: `tests/e2e/test_vertical_slice.py`
- Create: `tests/fixtures/mcp_server.py`
- Create: `tests/fixtures/skills/coding-demo/SKILL.md`
- Create: `tests/fixtures/skills/coding-demo/references/checklist.md`
- Modify: `docs/plans/milestones/M01-vertical-slice.md`
- Modify: `docs/plans/00-roadmap.md`

**Interfaces:**
- Consumes: Tasks 1-4, `MCPManager`, `MCPServerConfig`, `StdioMCPTransport`, `SkillRegistry`, `PromptComposer`, `ExactOutputEvaluator`, and all public observability/analytics facades.
- Produces: one deterministic `tests/e2e/test_vertical_slice.py` acceptance gate and a real local stdio MCP fixture.

- [ ] **Step 1: Create the real stdio MCP and Skill fixtures**

Create `tests/fixtures/mcp_server.py`:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agent-sdk-vertical-slice")


@mcp.tool()
def echo(text: str) -> dict[str, str]:
    """Echo deterministic text for the Agent SDK vertical slice."""
    return {"echo": text}


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Create `tests/fixtures/skills/coding-demo/SKILL.md`:

```markdown
---
name: coding-demo
description: Validate the Agent SDK vertical slice workspace result.
metadata:
  fixture: e2e
allowed-tools: write_note mcp.demo.echo
---
# Coding Demo

Write the requested result, echo the verification marker, then use
`references/checklist.md` before approving the generated workflow.
```

Create `references/checklist.md` with the exact text `Confirm result.txt exists and the Child returns verified.`

- [ ] **Step 2: Write the full RED E2E test with a zero-call reopen provider**

Create `tests/e2e/test_vertical_slice.py` using root `agent_sdk` imports plus
`build_parser`/`run_application` from the Task 4 example. Define:

- canonical Workflow YAML with `plan` (`run_as: parent`) and `verify` (`run_as: child`), agent revisions `planner:1` and `worker:1`, and one `plan -> verify` edge;
- `ScriptedVerticalModel.__call__` that returns async stream chunks for three `fake/main` turns (`write_note`, `mcp.demo.echo`, Workflow YAML), text for planner/worker, and a structured Capsule for `stream=False` by parsing the supplied compaction source document;
- an `unused_after_reopen` provider that increments a counter and raises if called;
- permission and event recorders.

Use this deterministic provider shape in the E2E file:

```python
from collections.abc import AsyncIterator
import json
from typing import Any

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
        yield {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
        yield {"choices": [], "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        }}
    return generate()


def _tool_stream(
    call_id: str,
    name: str,
    arguments: str,
) -> AsyncIterator[dict[str, object]]:
    async def generate() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{
            "delta": {"tool_calls": [{
                "index": 0,
                "id": call_id,
                "function": {"name": name, "arguments": arguments},
            }]},
            "finish_reason": "tool_calls",
        }]}
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
                "choices": [{"message": {"parsed": {
                    "objective": "complete the vertical slice",
                    "constraints": ["preserve durable evidence"],
                    "decisions": ["run the approved workflow"],
                    "facts": ["application and MCP tools succeeded"],
                    "next_actions": ["verify after reopen"],
                    "artifact_refs": ["workspace:result.txt"],
                    "source_event_ids": source_ids,
                }}}],
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
        records.append({
            "cursor": item.cursor,
            "type": item.event.type,
            "run_id": item.event.run_id,
        })
        if item.event.type == "evaluation.completed":
            return
```

The central test must follow this public sequence:

```python
sdk = AgentSDK.for_test(database_path=database, acompletion=scripted)
session = await sdk.sessions.create(workspaces=[workspace])
session_monitor = asyncio.create_task(
    _record_session_until_evaluation(sdk, session.session_id, displayed)
)
expected_output = tmp_path / "expected-workflow.yaml"
expected_output.write_text(WORKFLOW_YAML, encoding="utf-8")
args = build_parser().parse_args([
    "Write result.txt, call the MCP echo Tool, then return Workflow YAML",
    "--database", str(database),
    "--workspace", str(workspace),
    "--model", "fake/main",
    "--planner-model", "fake/planner",
    "--worker-model", "fake/worker",
    "--context-model", "gpt-4o-mini",
    "--model-window", "16384",
    "--skill-root", str(skill_fixture.parent),
    "--skill-name", "coding-demo",
    "--skill-resource", "references/checklist.md",
    "--mcp-command", sys.executable,
    "--mcp-arg", str(mcp_fixture),
    "--mcp-name", "demo",
    "--expected-output-file", str(expected_output),
])
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
prompt = application.prompt
workflow = application.workflow
evaluation = application.evaluation
success = application.success_rate
tool_failures = application.tool_failures

assert permission_names == ["write_note", "mcp.demo.echo"]
assert (workspace / "result.txt").read_text(encoding="utf-8") == "hello"
assert "# Coding Demo" in scripted.first_user_message
assert "Confirm result.txt exists" in scripted.first_user_message
assert view.capsule_id is not None
capsule = await sdk.context.get_capsule(
    view.capsule_id,
    session_id=session.session_id,
)
assert prompt.manifest.context_view_id == view.view_id
assert workflow is not None
assert workflow.nodes[1].run_id is not None
assert workflow.nodes[1].node_id == "verify"
tree = await sdk.queries.execution_tree(workflow.nodes[0].run_id or "")
assert [node.snapshot.run_id for node in tree.nodes] == [
    workflow.nodes[0].run_id,
    workflow.nodes[1].run_id,
]

assert evaluation is not None
assert evaluation.verdict is EvaluationVerdict.PASS
assert success.value == 1.0 and success.sample_count == 1
assert tool_failures.value == 0.0 and tool_failures.sample_count == 2
await asyncio.wait_for(session_monitor, timeout=5)
workspace_mtime = (workspace / "result.txt").stat().st_mtime_ns
```

Before closing phase one, assert that the combined session monitor and runner display
contains at least these durable types: `permission.requested`,
`permission.resolved`, `tool.call.completed`, `context.compaction.completed`,
`workflow.node.started`, `workflow.node.completed`, `model.usage.reported`, and
`evaluation.completed`. Prove Child progress separately through the non-null child
`run_id`, its `run.created` event in the session display, and the execution-tree
assertion above. Wrap the first phase in `try/finally`; if an assertion fails, cancel
and settle `session_monitor`, then close the SDK. `run_application` itself must already
have closed its application-owned MCP manager on both success and failure.

- [ ] **Step 3: Add reopen-without-reexecution and deletion assertions**

Continue the same test with a new SDK on the same database path:

```python
reopen_calls = 0

async def unused_after_reopen(**_: object) -> object:
    nonlocal reopen_calls
    reopen_calls += 1
    raise AssertionError("durable reopen must not execute LiteLLM")

reopened = AgentSDK.for_test(
    database_path=database,
    acompletion=unused_after_reopen,
)
observed = await reopened.queries.get_run(execution.run_id)
timeline = await reopened.queries.timeline(execution.run_id)
reopened_tree = await reopened.queries.execution_tree(
    workflow.nodes[0].run_id or ""
)
workflow_snapshot = await reopened.workflows.get(workflow.workflow_run_id)
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
assert [item.event.event_id for item in evaluation_events.events] == list(
    reopened_success.evidence_event_ids
)
assert reopen_calls == 0
assert (workspace / "result.txt").stat().st_mtime_ns == workspace_mtime

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
after_delete = await reopened.analytics.success_rate(evaluator_id="exact_output")
assert after_delete.value is None
assert after_delete.sample_count == 0
assert after_delete.evidence_event_ids == ()
assert (workspace / "result.txt").read_text(encoding="utf-8") == "hello"
await reopened.close()
assert reopen_calls == 0
```

Use `try/finally` so the MCP process and both SDK instances are closed even when an assertion fails.

- [ ] **Step 4: Run the E2E test and fix only real composition gaps**

Run:

```powershell
uv run --python 3.13 pytest tests/e2e/test_vertical_slice.py -v
```

Expected before implementation is complete: RED at the first missing façade/example/fixture behavior. Expected after implementation: PASS without network, credentials, sleeps longer than bounded polling intervals, or direct Store reads.

If the real stdio fixture exposes an SDK bug, fix it in the owning MCP/runtime module with a focused regression there; do not work around it in the example.

- [ ] **Step 5: Align milestone wording with the proven boundary**

Update `docs/plans/00-roadmap.md` and `docs/plans/milestones/M01-vertical-slice.md` so M01 says:

- application-confirmed model-generated Workflow YAML, not an SDK-owned resumable dynamic proposal;
- quiescent SQLite reopen/read recovery, not in-flight permission recovery;
- application Tool in M01, with built-in coding Tools still assigned to M03;
- minimal public CLI in M01, with release-grade reference apps still assigned to M06.
- focused MCP tests may use an injected fake seam, while the integrated E2E uses a real
  local stdio MCP server and still performs no network request.
- the roadmap release gate says the E2E invokes the reference CLI's injectable active
  orchestration and then the acceptance harness verifies quiescent reopen/deletion; the
  interactive CLI never silently reopens or deletes the user's Session.

Do not change later milestone scope or mark M01 complete in the task index yet.

- [ ] **Step 6: Run the M01 completion gate**

Run from the worktree root:

```powershell
uv sync
uv run --python 3.13 pytest tests/e2e/test_vertical_slice.py -v
uv run --python 3.13 pytest -q
uv run --python 3.13 ruff check src tests examples
uv run --python 3.13 mypy src
uv build
uv run --python 3.13 python -m examples.reference_cli.main --help
git diff --check
git status --short
```

Expected: every command exits 0; the full suite has no xfail acceptance path; wheel/sdist include both packaged prompt profiles; `--help` performs no Store/model call; only the intended Task 5 files are dirty before commit.

- [ ] **Step 7: Commit Task 5**

```powershell
git add tests/e2e tests/fixtures/skills/coding-demo tests/fixtures/mcp_server.py docs/plans/00-roadmap.md docs/plans/milestones/M01-vertical-slice.md
git commit -m "test: prove complete M01 vertical slice"
```

---

### Task 6: Final review and M01 milestone ledger

**Files:**
- Modify: `docs/plans/tasks/index.md`
- Update ignored evidence: `.superpowers/sdd/M01-T010-report.md`
- Update ignored progress: `.superpowers/sdd/progress.md`

**Interfaces:**
- Consumes: clean Tasks 1-5 commits and independent task review.
- Produces: M01-T010 `done`, M01 milestone completion evidence, and M02-T001 `in_progress` only after review reports Critical 0 and Important 0.

- [ ] **Step 1: Request independent spec/code review**

The reviewer must inspect the complete T010 diff against this task and `docs/design/features/12-integrated-vertical-slice.md`, run the Task 5 completion gate, and report Critical/Important/Minor findings. Required review focus:

- no private SDK imports in examples;
- no auto-approval or hidden presentation side effects;
- no model/Tool call after reopen;
- MCP owner/process cleanup under success, failure, and cancellation;
- Context façade lifecycle and Session ownership;
- correct deletion of SDK-managed facts without workspace deletion;
- no accidental M02/M03/M04/M06 scope implementation.

- [ ] **Step 2: Fix every Critical/Important finding with RED regression evidence**

For each finding, add a focused failing test in the owning test directory, observe RED, make the smallest production change, and rerun the focused plus full gate. Do not suppress or downgrade a finding merely to close M01.

- [ ] **Step 3: Record completion only after approval**

Change `M01-T010` from `in_progress` to `done` in `docs/plans/tasks/index.md`, append the implementation/fix commit ids and final Python 3.13 test/Ruff/mypy/build/review evidence, and mark M02-T001 `in_progress`. Update the ignored SDD report/progress files with the same factual results.

- [ ] **Step 4: Commit the milestone ledger**

```powershell
git add docs/plans/tasks/index.md
git commit -m "chore: complete M01 vertical slice"
```

- [ ] **Step 5: Verify the final handoff is clean**

Run:

```powershell
git status --short
git log -8 --oneline
```

Expected: status is empty and the log ends with the reviewed T010 implementation/fix commits followed by the milestone ledger commit.
