# M01-T004 LiteLLM Agent Loop Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream one LiteLLM response through a durable Agent Loop and expose a RunHandle.

**Architecture:** LiteLLMGateway is an internal adapter that yields SDK ModelEvents. RunEngine persists started/delta/completed events and updates the Run snapshot; tests inject a scripted `acompletion` callable.

**Tech Stack:** LiteLLM async streaming, Pydantic, asyncio, pytest-asyncio.

## Global Constraints

- No public model provider Protocol.
- LiteLLM response objects do not cross the gateway.
- Run terminal state is committed before RunHandle returns a result.

---

### Task 1: Implement model normalization and text-only Loop

**Files:**
- Create: `src/agent_sdk/models/litellm_gateway.py`
- Create: `src/agent_sdk/runtime/engine.py`
- Create: `src/agent_sdk/runtime/handles.py`
- Create: `src/agent_sdk/api.py`
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/__init__.py`
- Create: `tests/integration/runtime/test_text_agent_loop.py`

**Interfaces:**
- Produces: `ModelRequest`, `TextDelta`, `UsageReported`, `ModelCompleted`, `LiteLLMGateway.stream`, `RunEngine.execute`, `RunHandle.result/events`, `AgentSDK`.
- Consumes: `RuntimeCommands`, `StateStore`, `EventEnvelope`, `AgentSDKConfig`.

- [ ] **Step 1: Write a scripted streaming test**

```python
async def fake_acompletion(**_: object):
    async def chunks():
        yield {"choices": [{"delta": {"content": "hel"}}]}
        yield {"choices": [{"delta": {"content": "lo"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
    return chunks()

@pytest.mark.asyncio
async def test_agent_loop_persists_stream_and_result(store: InMemoryStore) -> None:
    sdk = AgentSDK.for_test(store=store, acompletion=fake_acompletion)
    session = await sdk.sessions.create(workspaces=[])
    run = await sdk.runs.start(session.session_id, AgentSpec(name="test", model="fake/model"), "say hello")
    assert (await run.result()).output_text == "hello"
    assert (await sdk.runs.get(run.run_id)).status is RunStatus.COMPLETED
    assert [e.event.type for e in await store.read_events(after_cursor=0)][-1] == "run.completed"
```

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

@dataclass(frozen=True)
class TextDelta: text: str
@dataclass(frozen=True)
class UsageReported: prompt_tokens: int | None; completion_tokens: int | None
@dataclass(frozen=True)
class ModelCompleted: finish_reason: str | None
```

`LiteLLMGateway.stream` calls injected `acompletion(model=..., messages=list(...), tools=..., stream=True)` and maps chunks without returning LiteLLM types.

- [ ] **Step 4: Implement the durable text Loop**

RunEngine transition sequence is exactly `run.started`, `step.started`, `model.call.started`, zero or more `model.text.delta`, optional `model.usage.reported`, `model.call.completed`, `step.completed`, `run.completed`. Commit each lifecycle boundary and batch adjacent deltas up to 50 ms/4 KiB.

```python
async def execute(self, run_id: str, request: ModelRequest) -> RunResult:
    await self._commit(run_id, "run.started")
    await self._commit(run_id, "step.started")
    chunks: list[str] = []
    async for event in self._models.stream(request):
        if isinstance(event, TextDelta):
            chunks.append(event.text)
            await self._delta_batcher.add(run_id, event.text)
        elif isinstance(event, UsageReported):
            await self._commit(run_id, "model.usage.reported", event.to_payload())
    await self._delta_batcher.flush(run_id)
    return await self._complete_text_run(run_id, "".join(chunks))
```

- [ ] **Step 5: Implement AgentSDK and RunHandle**

```python
class RunHandle:
    run_id: str
    async def result(self) -> RunResult: ...
    async def events(self, cursor: int = 0) -> AsyncIterator[EventEnvelope]: ...

class AgentSDK:
    sessions: SessionAPI
    runs: RunAPI
    async def close(self) -> None: ...
```

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/runtime/test_text_agent_loop.py -v && uv run mypy src`

Expected: text result is `hello`; ordered events and usage are persisted.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk tests/integration/runtime
git commit -m "feat: add litellm agent loop slice"
```
