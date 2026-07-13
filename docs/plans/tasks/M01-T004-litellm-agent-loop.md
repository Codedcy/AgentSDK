# M01-T004 LiteLLM Agent Loop Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream one LiteLLM response through a durable Agent Loop and expose a RunHandle.

**Architecture:** LiteLLMGateway is an internal adapter that yields SDK ModelEvents. RunEngine persists lifecycle boundaries, bounded text-delta batches, usage, failures, and terminal Run snapshots. `RunAPI.start` first persists `run.created`, then starts one background execution task and returns a live `RunHandle`; tests inject a scripted `acompletion` callable only through `AgentSDK.for_test`.

**Tech Stack:** LiteLLM async streaming, Pydantic, asyncio, pytest-asyncio.

## Global Constraints

- No public model provider Protocol.
- LiteLLM response objects do not cross the gateway.
- Run terminal state is committed before RunHandle returns a result.
- A Run's event sequence is strictly increasing from the existing `run.created` event; status-changing `run.started|completed|failed` events and their Run snapshot versions commit atomically.
- `RunHandle.events` can observe a Run while it is still active and retains global cursors for resume; polling is acceptable for this M01 slice because durable subscriptions arrive in M05.
- Adjacent text deltas are emitted within 50 ms or 4 KiB, whichever happens first, and are flushed before a later non-delta event.
- Provider failures become a stable `AgentSDKError`, after `model.call.failed`, `step.failed`, and `run.failed` plus a `FAILED` snapshot are durable.

---

### Task 1: Implement model normalization and text-only Loop

**Files:**
- Create: `src/agent_sdk/models/litellm_gateway.py`
- Create: `src/agent_sdk/runtime/engine.py`
- Create: `src/agent_sdk/runtime/handles.py`
- Create: `src/agent_sdk/api.py`
- Modify: `src/agent_sdk/errors.py`
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/__init__.py`
- Create: `tests/integration/runtime/test_text_agent_loop.py`

**Interfaces:**
- Produces: frozen minimal `AgentSpec`, `TokenUsage`, `RunResult`; `ModelRequest`, `TextDelta`, `UsageReported`, `ModelCompleted`, `LiteLLMGateway.stream`; `RunEngine.execute`; live `RunHandle.result/events`; `SessionAPI`, `RunAPI`, and `AgentSDK.for_test`.
- Consumes: `RuntimeCommands`, `StateStore`, `EventEnvelope`, `AgentSDKConfig`.

- [ ] **Step 1: Write a scripted streaming test**

```python
async def fake_acompletion(**_: object):
    async def chunks():
        yield {"choices": [{"delta": {"content": "hel"}}]}
        yield {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}
    return chunks()

@pytest.mark.asyncio
async def test_agent_loop_persists_stream_and_result(store: InMemoryStore) -> None:
    sdk = AgentSDK.for_test(store=store, acompletion=fake_acompletion)
    session = await sdk.sessions.create(workspaces=[])
    run = await sdk.runs.start(session.session_id, AgentSpec(name="test", model="fake/model"), "say hello")
    assert (await run.result()).output_text == "hello"
    assert (await sdk.runs.get(run.run_id)).status is RunStatus.COMPLETED
    events = await store.read_events(after_cursor=0)
    assert [e.event.type for e in events if e.event.run_id == run.run_id] == [
        "run.created", "run.started", "step.started", "model.call.started",
        "model.text.delta", "model.usage.reported", "model.call.completed",
        "step.completed", "run.completed",
    ]
    assert events[-1].event.payload["usage"]["total_tokens"] == 3
```

Also add focused tests proving: (1) `run.events()` yields durable cursor-bearing events before `result()` completes when the scripted stream pauses longer than 50 ms; (2) a scripted provider exception produces the ordered failed lifecycle and `RunStatus.FAILED` before `result()` raises `AgentSDKError`; and (3) gateway outputs contain no LiteLLM response objects.

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/runtime/test_text_agent_loop.py -v`

Expected: import failure for AgentSDK/LiteLLMGateway.

- [ ] **Step 3: Implement internal model event types and gateway**

```python
@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    params: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class TextDelta: text: str
@dataclass(frozen=True)
class UsageReported:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
@dataclass(frozen=True)
class ModelCompleted: finish_reason: str | None
```

`LiteLLMGateway.stream` calls injected `acompletion(model=..., messages=list(...), tools=..., stream=True, **params)` and maps both mapping-like scripted chunks and normal LiteLLM attribute objects into fresh SDK dataclasses. Capture text, top-level usage, and the latest finish reason; yield exactly one `ModelCompleted` when the stream ends, even if the provider omitted a finish reason. Do not create a public provider seam: the injected callable is an internal/test construction detail, while the default is `litellm.acompletion`.

- [ ] **Step 4: Implement the durable text Loop**

After the already durable `run.created`, the successful Run transition sequence is exactly `run.started`, `step.started`, `model.call.started`, zero or more `model.text.delta`, optional `model.usage.reported`, `model.call.completed`, `step.completed`, `run.completed`. Commit each lifecycle boundary; a single delta event may contain several adjacent provider fragments. Flush deltas before usage/completion and use a cancellation-safe timer so a stalled stream cannot hold a delta beyond 50 ms. The `run.started` commit writes snapshot version 2/`RUNNING`; `run.completed` writes version 3/`COMPLETED`, output text, and normalized token usage in the same commit. On a model exception, flush accepted deltas and durably emit `model.call.failed`, `step.failed`, and `run.failed` with snapshot version 3/`FAILED` before raising a stable SDK error.

```python
async def execute(self, run_id: str, request: ModelRequest) -> RunResult:
    await self._commit(run_id, "run.started")
    await self._commit(run_id, "step.started")
    await self._commit(run_id, "model.call.started", {"model": request.model})
    chunks: list[str] = []
    async for event in self._models.stream(request):
        if isinstance(event, TextDelta):
            chunks.append(event.text)
            await self._delta_batcher.add(run_id, event.text)
        elif isinstance(event, UsageReported):
            await self._delta_batcher.flush(run_id)
            await self._commit(run_id, "model.usage.reported", event.to_payload())
        elif isinstance(event, ModelCompleted):
            await self._delta_batcher.flush(run_id)
            await self._commit(run_id, "model.call.completed", event.to_payload())
    await self._delta_batcher.flush(run_id)
    return await self._complete_text_run(run_id, "".join(chunks))
```

All sequence allocation and timer-driven delta commits for one Run must serialize through one emission lock. The engine reads the persisted Run snapshot to obtain Session ownership and must not reach into SQLite or InMemoryStore internals.

- [ ] **Step 5: Implement AgentSDK and RunHandle**

```python
class RunHandle:
    run_id: str
    async def result(self) -> RunResult: ...
    async def events(self, cursor: int = 0) -> AsyncIterator[StoredEvent]: ...

class AgentSDK:
    sessions: SessionAPI
    runs: RunAPI
    async def close(self) -> None: ...
```

For this slice, `AgentSpec` contains `name`, LiteLLM `model`, immutable model params, and a stable revision default. `SessionAPI.create` delegates to `RuntimeCommands`. `RunAPI.start` persists `run.created`, schedules `RunEngine.execute`, and returns immediately with a handle; `RunAPI.get` validates the persisted Run snapshot. `RunHandle.events` polls the durable Store, filters to its Run, yields `StoredEvent` values in global-cursor order, and drains terminal events before stopping. `RunHandle.result` awaits the same task and therefore cannot report completion before the terminal transaction. `AgentSDK.close` waits for active tasks and closes an owned Store when applicable; the test constructor does not silently take ownership of an injected Store.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/runtime/test_text_agent_loop.py -v && uv run pytest -q && uv run ruff check src tests && uv run mypy src`

Expected: text result is `hello`; live ordered cursor events, usage, terminal snapshots, bounded delta flushing, and the failed lifecycle are persisted; no task/thread remains active.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk tests/integration/runtime
git commit -m "feat: add litellm agent loop slice"
```
