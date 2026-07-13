# M01-T007 Context and Compaction Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build packaged default Prompt Profiles and a reproducible Prompt Manifest, then force one LiteLLM-generated, source-validated Context Capsule from immutable Session events without mutating or replacing the source ledger.

**Architecture:** ContextPlanner projects an explicit allowlist of immutable Session event types into detached ContextItems, computes a reserve-aware ContextBudget with LiteLLM token counting, and delegates forced L3/L4 compaction to ContextCompactor. LiteLLMGateway gains one non-streaming structured-completion path that still calls only `litellm.acompletion`. Valid capsules and ContextViews are persisted as Session-owned snapshots plus lifecycle events; invalid model output falls back to raw references. PromptComposer loads packaged General/Coding Markdown layers, hashes canonical ordered content and Tool schemas, and returns messages plus a reproducible manifest.

**Tech Stack:** Pydantic, LiteLLM gateway, importlib.resources, StateStore.

## Global Constraints

- Original ledger messages remain readable after compaction.
- Capsule source event ids must exist.
- Default thresholds are 0.70/0.80/0.90/0.96.
- The available input budget is `model_window - output_reserve - tool_schema_tokens - safety_reserve`; zero/negative capacity fails before a model call.
- A caller-protected id and the most recent user item remain as raw `message_refs`; a capsule must cite every protected id and may cite only projected source ids.
- Model/validation/persistence failures never inject an invalid capsule. Model/validation failures persist a sanitized `context.compaction.failed` event and an L0 raw-reference fallback View; cancellation is never rewritten.
- Context snapshots/events are Session-owned so `delete_session` removes them. M01 uses the unique Context View id as the derived event aggregate in the current envelope; it never consumes a live Run's event sequence.
- M01 implements real L0 and the shared structured Capsule path for forced L3/L4. It exposes threshold recommendation for L1/L2 but does not claim their deduplication/selection strategies are complete; those are M03-T005 scope.
- The only model runtime is LiteLLM. Tests may inject the existing private gateway seam, but no public provider/compactor backend protocol is introduced.

---

### Task 1: Add ContextView, PromptManifest, and forced compaction

**Files:**
- Create: `src/agent_sdk/context/models.py`
- Create: `src/agent_sdk/context/budget.py`
- Create: `src/agent_sdk/context/planner.py`
- Create: `src/agent_sdk/context/compactor.py`
- Create: `src/agent_sdk/context/retrieval.py`
- Create: `src/agent_sdk/context/__init__.py`
- Create: `src/agent_sdk/prompts/models.py`
- Create: `src/agent_sdk/prompts/composer.py`
- Create: `src/agent_sdk/prompts/__init__.py`
- Create: `src/agent_sdk/prompts/profiles/__init__.py`
- Create: `src/agent_sdk/prompts/profiles/general/__init__.py`
- Create: `src/agent_sdk/prompts/profiles/general/system.md`
- Create: `src/agent_sdk/prompts/profiles/coding/__init__.py`
- Create: `src/agent_sdk/prompts/profiles/coding/system.md`
- Modify: `src/agent_sdk/models/litellm_gateway.py`
- Modify: `src/agent_sdk/__init__.py`
- Create: `tests/integration/context/test_compaction_slice.py`
- Create: `tests/integration/prompts/test_prompt_slice.py`

**Interfaces:**
- Produces: `ContextItem`, `ContextBudget`, `ContextView`, `ContextCapsule`, `CompactionLevel`, `CompactionPolicy`, `ContextPlanner.build`, `ContextRetrieval.get_capsule/read_sources`, `PromptComposer.compose`, `BuiltPrompt`, `PromptManifest`; `LiteLLMGateway.complete_structured` and a `ModelRequest.purpose="compaction"` trace dimension.
- Consumes: `StateStore` Session events/snapshots, `LiteLLMGateway`, model/window/reserve configuration, profile/application text and Tool schemas.

- [ ] **Step 1: Write a preservation and source-reference test**

```python
@pytest.mark.asyncio
async def test_forced_compaction_preserves_ledger_and_sources(context_fixture) -> None:
    planner = context_fixture.planner(max_input_tokens=40)
    view = await planner.build(
        context_fixture.session_id,
        force_level="L3",
        protected_event_ids={context_fixture.latest_user_event_id},
    )
    assert view.capsule_id is not None
    capsule = await context_fixture.get_capsule(view.capsule_id)
    assert set(capsule.source_event_ids) <= set(context_fixture.original_event_ids)
    assert context_fixture.latest_user_event_id in view.message_refs
    assert context_fixture.latest_user_event_id in capsule.source_event_ids
    assert await context_fixture.read_originals() == context_fixture.original_messages
```

Seed sources with the actual M01 projection allowlist: `run.created.payload.user_input`, `model.text.delta.payload.text`, `tool.call.completed.payload.content`, and application-authored `context.message.appended` with strict role/content. Unknown lifecycle events and previously derived Context events are not model input. Test detached inputs and deterministic cursor order.

Add focused tests for: exact default thresholds and strict increasing validation; reserve arithmetic and no-call capacity failure; automatic recommendation without pretending L1/L2 strategies were applied; valid L3/L4 structured output, unique source ids, unknown source rejection, missing protected-id rejection, sanitized model/malformed-response failure fallback, no capsule snapshot on failure, cancellation propagation, atomic capsule/View persistence, SQLite reopen recovery, `delete_session` cleanup, exact ordered retrieval and corrupted/missing-source detection. Every compaction call must reach the private fake through `LiteLLMGateway.complete_structured` with purpose `compaction` and the `ContextCapsule` response schema.

For prompts, test General-only and General→Coding order, application layer append, per-layer/whole-prompt SHA-256 stability, canonical Tool schema fingerprinting independent of dict insertion order, Context View/model provenance, frozen/deep-detached models, unknown profile rejection, UTF-8 resources, and wheel/sdist inclusion of both Markdown files. The default General resource must cover identity/goal, persistence/termination, tool protocol, permission waits, evidence, transparent errors, Context/Capsule semantics, Workflow/Child collaboration and final reporting; Coding adds Explore→Plan→Approve→Implement→Verify→Report and workspace/change preservation.

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/context/test_compaction_slice.py tests/integration/prompts/test_prompt_slice.py -v`

Expected: missing context/prompts modules.

- [ ] **Step 3: Implement models and budget calculation**

```python
class CompactionPolicy(BaseModel):
    l1_reference: float = 0.70; l2_selective: float = 0.80
    l3_summary: float = 0.90; l4_rebase: float = 0.96; recovery_target: float = 0.75
class ContextCapsule(BaseModel):
    objective: str; constraints: list[str]; decisions: list[str]; facts: list[str]
    next_actions: list[str]; artifact_refs: list[str]; source_event_ids: list[str]
class ContextView(BaseModel):
    view_id: str; session_id: str; message_refs: list[str]; capsule_id: str | None; estimated_tokens: int
```

Use frozen, extra-forbid, recursively detached models and tuples for stored collections. Validate `0 < L1 < L2 < L3 < L4 < 1`, `0 < recovery_target < L2`, unique ids, non-negative reserves and positive window. `ContextBudget` records every reserve, available input, projected source tokens and watermark ratio. The default estimator calls `litellm.token_counter(model=..., messages=...)`; tests inject only a private deterministic counter.

`ContextPlanner` reads the Session events once, projects only the allowlisted source types, verifies all caller-protected ids are projected sources, and automatically protects the newest user item. L0 uses all raw refs. Forced L3/L4 attempt a capsule and retain protected refs raw. On model/schema/source failure it records an L0 fallback rather than labeling unchanged raw history as successful compression.

- [ ] **Step 4: Implement compactor and source validation**

Extend the existing LiteLLMGateway rather than importing LiteLLM in Context code. `complete_structured(request, schema)` calls the same injected `acompletion` with `stream=False` and `response_format=schema`, accepts either a parsed Pydantic value/mapping or JSON message content, validates with the requested schema, normalizes usage, and turns missing/malformed choices/content into a stable sanitized SDK error. It never exposes the raw provider response.

Call it with `ModelRequest(purpose="compaction")` and a bounded JSON source prompt matching `ContextCapsule`. Reject duplicate/unknown source ids or missing protected ids. Model and validation failures are sanitized into `context.compaction.failed` and fall back to the full raw selection; `asyncio.CancelledError` propagates and must not leave orphan tasks/Futures or partial snapshots.

```python
async def compact(self, source: Sequence[ContextItem], protected: set[str]) -> ContextCapsule | None:
    try:
        capsule = await self._models.complete_structured(self._messages(source), ContextCapsule)
        source_ids = {item.event_id for item in source}
        if not set(capsule.source_event_ids) <= source_ids or not protected <= set(capsule.source_event_ids):
            raise InvalidCapsule("invalid or missing source ids")
        return capsule
    except (AgentSDKError, ValidationError):
        return None
```

Planner persists a successful capsule snapshot, ContextView snapshot, `context.compaction.completed`, and `context.view.created` in one `StateStore.commit`. Failure fallback persists no capsule and atomically commits `context.compaction.failed`, the fallback View snapshot, and `context.view.created`. Payloads contain stable codes/ids/budget/usage only, never raw exception text or full prompt content. `ContextRetrieval` validates the stored record, re-reads the owning Session ledger, and returns exact cited `StoredEvent`s in citation order; missing or cross-Session refs fail closed.

- [ ] **Step 5: Implement Prompt resources and Manifest**

Composer loads Markdown resources using `importlib.resources`, hashes each layer, and returns immutable ordered system messages plus a Manifest containing layer ids/versions/hashes, whole-prompt hash, ContextView id, model, and canonical Tool schema hash. `profile="general"` loads General; `profile="coding"` loads General then Coding; optional application text is last. Profile resources are package data and no filesystem path supplied by a caller is opened.

```python
def load_profile(name: str) -> PromptLayer:
    resource = files("agent_sdk.prompts.profiles").joinpath(name, "system.md")
    text = resource.read_text(encoding="utf-8")
    return PromptLayer(id=f"profile:{name}", text=text, sha256=sha256(text.encode()).hexdigest())

def manifest(layers: Sequence[PromptLayer], view: ContextView, model: str, tools_sha256: str) -> PromptManifest:
    return PromptManifest(layers=tuple((item.id, item.sha256) for item in layers), context_view_id=view.id, model=model, tools_sha256=tools_sha256)
```

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/context/test_compaction_slice.py tests/integration/prompts/test_prompt_slice.py -v && uv run pytest -q && uv run ruff check src tests && uv run mypy src`

Expected: valid compaction and fallback events are durable; Capsule/View and exact sources survive SQLite reopen; originals are unchanged; Session deletion removes derived state; no invalid capsule or raw provider failure is exposed; Prompt Manifest has reproducible General/Coding/Tool hashes and built distributions contain both default profile resources; no task/Future remains open.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/context src/agent_sdk/prompts src/agent_sdk/models/litellm_gateway.py src/agent_sdk/__init__.py tests/integration/context tests/integration/prompts
git commit -m "feat: add context compaction slice"
```
