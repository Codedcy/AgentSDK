# M01-T007 Context and Compaction Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Prompt Manifests and force one validated Context Capsule without mutating original messages.

**Architecture:** ContextPlanner reads ledger messages, computes a ContextView, and delegates high-water compaction to LiteLLM with a structured Capsule schema. PromptComposer adds versioned General/Coding resources.

**Tech Stack:** Pydantic, LiteLLM gateway, importlib.resources, StateStore.

## Global Constraints

- Original ledger messages remain readable after compaction.
- Capsule source event ids must exist.
- Default thresholds are 0.70/0.80/0.90/0.96.

---

### Task 1: Add ContextView, PromptManifest, and forced compaction

**Files:**
- Create: `src/agent_sdk/context/models.py`
- Create: `src/agent_sdk/context/budget.py`
- Create: `src/agent_sdk/context/planner.py`
- Create: `src/agent_sdk/context/compactor.py`
- Create: `src/agent_sdk/context/retrieval.py`
- Create: `src/agent_sdk/prompts/models.py`
- Create: `src/agent_sdk/prompts/composer.py`
- Create: `src/agent_sdk/prompts/profiles/general/system.md`
- Create: `src/agent_sdk/prompts/profiles/coding/system.md`
- Create: `tests/integration/context/test_compaction_slice.py`

**Interfaces:**
- Produces: `ContextView`, `ContextCapsule`, `CompactionPolicy`, `ContextPlanner.build`, `ContextRetrieval.read_sources`, `PromptComposer.compose`, `PromptManifest`.
- Consumes: ledger events/messages, `LiteLLMGateway`, AgentSpec/Profile.

- [ ] **Step 1: Write a preservation and source-reference test**

```python
@pytest.mark.asyncio
async def test_forced_compaction_preserves_ledger_and_sources(context_fixture) -> None:
    planner = context_fixture.planner(max_input_tokens=40)
    view = await planner.build(context_fixture.session_id, force_level="L3")
    assert view.capsule_id is not None
    capsule = await context_fixture.get_capsule(view.capsule_id)
    assert set(capsule.source_event_ids) <= set(context_fixture.original_event_ids)
    assert await context_fixture.read_originals() == context_fixture.original_messages
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/context/test_compaction_slice.py -v`

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

Validate increasing thresholds and compute ratio after reserving output/tool/safety tokens.

- [ ] **Step 4: Implement compactor and source validation**

Call LiteLLMGateway with purpose `compaction` and request JSON matching ContextCapsule. Reject unknown source ids or missing protected ids, persist `context.compaction.failed`, and fall back to uncompressed selection in this slice.

```python
async def compact(self, source: Sequence[ContextItem], protected: set[str]) -> ContextCapsule | None:
    try:
        capsule = await self._models.complete_structured(self._messages(source), ContextCapsule)
        source_ids = {item.event_id for item in source}
        if not set(capsule.source_event_ids) <= source_ids or not protected <= set(capsule.source_event_ids):
            raise InvalidCapsule("invalid or missing source ids")
        return await self._store_capsule(capsule)
    except (AgentSDKError, ValidationError) as error:
        await self._events.append(compaction_failed(error))
        return None
```

- [ ] **Step 5: Implement Prompt resources and Manifest**

Composer loads both Markdown resources using `importlib.resources`, hashes each layer, and returns ordered messages plus a Manifest containing layer ids/hashes, ContextView id, model, and Tool schema hashes.

```python
def load_profile(name: str) -> PromptLayer:
    resource = files("agent_sdk.prompts.profiles").joinpath(name, "system.md")
    text = resource.read_text(encoding="utf-8")
    return PromptLayer(id=f"profile:{name}", text=text, sha256=sha256(text.encode()).hexdigest())

def manifest(layers: Sequence[PromptLayer], view: ContextView, model: str, tools_sha256: str) -> PromptManifest:
    return PromptManifest(layers=tuple((item.id, item.sha256) for item in layers), context_view_id=view.id, model=model, tools_sha256=tools_sha256)
```

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/context/test_compaction_slice.py -v`

Expected: compaction event and Capsule exist; originals are unchanged; Prompt Manifest has General and Coding layer hashes.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/context src/agent_sdk/prompts tests/integration/context
git commit -m "feat: add context compaction slice"
```
