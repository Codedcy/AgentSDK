# M03-T005 Prompt and Context Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete versioned prompt composition and L0-L4 context compaction while preserving the immutable Session ledger.

**Architecture:** `PromptComposer` produces a manifest-backed system prompt from ordered layers or a complete override. `ContextPlanner` budgets a model-specific view and selects compaction strategies; summaries/rebases are validated capsules stored as derived artifacts, never replacements for raw history.

**Tech Stack:** LiteLLM token counters and structured outputs, Jinja2 sandbox, Pydantic v2, pytest/Hypothesis.

## Global Constraints

- Default profiles are package data: `general` and `coding`.
- Prompt text, layer order, template variables, and hashes are captured in provenance.
- Compaction mutates only Context View; raw events/messages remain recoverable until Session deletion.
- Protected instructions, active workflow state, permissions, unresolved tool calls, and pinned evidence cannot be summarized away.

---

### Task 1: Complete prompt profiles and manifests

**Files:**
- Modify: `src/agent_sdk/prompts/composer.py`
- Modify: `src/agent_sdk/prompts/models.py`
- Create: `src/agent_sdk/prompts/profiles/general/system.md`
- Create: `src/agent_sdk/prompts/profiles/coding/system.md`
- Create: `tests/unit/prompts/test_composer.py`
- Create: `tests/snapshots/prompts/`

- [ ] **Step 1: Write failing composition tests**

```python
def test_prompt_layers_have_stable_order_and_hash(prompt_composer) -> None:
    built = prompt_composer.compose(profile="coding", application="APP", run="RUN")
    assert built.manifest.layer_names == ("sdk", "profile:coding", "application", "run")
    assert sha256(built.text.encode()).hexdigest() == built.manifest.sha256

def test_complete_override_excludes_sdk_defaults(prompt_composer) -> None:
    built = prompt_composer.compose(override="ONLY THIS", replace_all=True)
    assert built.text == "ONLY THIS"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/prompts/test_composer.py -v`

Expected: full profile/manifests are incomplete.

- [ ] **Step 3: Implement ordered composition and sandboxed variables**

```python
LAYER_ORDER = ("sdk", "profile", "application", "session", "run", "workflow", "skill")

def compose(self, request: PromptRequest) -> BuiltPrompt:
    layers = (PromptLayer("override", request.override),) if request.replace_all else self._ordered_layers(request)
    rendered = tuple(self._render_sandboxed(layer, request.variables) for layer in layers if layer.text)
    text = "\n\n".join(layer.text.strip() for layer in rendered)
    return BuiltPrompt(text=text, manifest=PromptManifest.from_layers(rendered, text))
```

Use strict undefined variables, a fixed filter allowlist, size limits, and no filesystem/process access from templates.

- [ ] **Step 4: Verify snapshots and packaging**

Run: `uv run pytest tests/unit/prompts/test_composer.py -v`

Expected: general/coding snapshots, overrides, hash stability, and invalid template failures pass.

- [ ] **Step 5: Commit**

```powershell
git add src/agent_sdk/prompts tests/unit/prompts tests/snapshots/prompts
git commit -m "feat: complete versioned prompt composition"
```

---

### Task 2: Implement L0-L4 planning and protected-state selection

**Files:**
- Modify: `src/agent_sdk/context/models.py`
- Modify: `src/agent_sdk/context/budget.py`
- Modify: `src/agent_sdk/context/planner.py`
- Modify: `src/agent_sdk/context/strategies.py`
- Create: `tests/unit/context/test_planner.py`
- Create: `tests/property/test_context_invariants.py`

- [ ] **Step 1: Write failing threshold and invariant tests**

```python
@pytest.mark.parametrize(("ratio", "expected"), [(0.69, "L0"), (0.70, "L1"), (0.80, "L2"), (0.90, "L3"), (0.96, "L4")])
def test_default_strategy_thresholds(context_planner, ratio, expected) -> None:
    assert context_planner.strategy_for_ratio(ratio).level == expected

@given(context_cases())
def test_protected_items_are_never_removed(case) -> None:
    view = case.planner.plan(case)
    assert case.protected_ids <= set(view.source_ids)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/context/test_planner.py tests/property/test_context_invariants.py -v`

Expected: all levels and invariants are incomplete.

- [ ] **Step 3: Implement budget and strategy selection**

```python
DEFAULT_THRESHOLDS = {"L1": 0.70, "L2": 0.80, "L3": 0.90, "L4": 0.96}

def strategy_for_ratio(self, ratio: float) -> CompactionLevel:
    selected = CompactionLevel.L0
    for level, threshold in DEFAULT_THRESHOLDS.items():
        if ratio >= threshold:
            selected = CompactionLevel(level)
    return selected

def available_input_tokens(window: int, output_reserve: int, safety_reserve: int) -> int:
    return max(0, window - output_reserve - safety_reserve)
```

L1 replaces large values with artifact references; L2 selects relevant history/evidence; L3 summarizes older spans; L4 creates a fresh rebase capsule. Re-run selection until projected ratio is below 0.75 or raise a typed capacity error.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/context/test_planner.py tests/property/test_context_invariants.py -v`

Expected: levels, reserves, protected items, stable ordering, and target recovery pass.

```powershell
git add src/agent_sdk/context tests/unit/context tests/property/test_context_invariants.py
git commit -m "feat: implement multi-level context planning"
```

---

### Task 3: Add validated summary/rebase capsules and retrieval

**Files:**
- Modify: `src/agent_sdk/context/models.py`
- Modify: `src/agent_sdk/context/retrieval.py`
- Modify: `src/agent_sdk/context/compactor.py`
- Create: `tests/integration/context/test_compaction.py`

- [ ] **Step 1: Write failing capsule and fallback tests**

```python
@pytest.mark.asyncio
async def test_invalid_summary_falls_back_without_losing_ledger(compactor, invalid_model, session) -> None:
    before = await session.raw_event_count()
    view = await compactor.compact(session, level="L3", model=invalid_model)
    assert view.level in {"L1", "L2"}
    assert await session.raw_event_count() == before

@pytest.mark.asyncio
async def test_capsule_cites_source_ranges(compactor, session) -> None:
    view = await compactor.compact(session, level="L4")
    assert all(citation.start_sequence <= citation.end_sequence for citation in view.capsule.citations)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/context/test_compaction.py -v`

Expected: structured capsules and fallback are missing.

- [ ] **Step 3: Implement structured LiteLLM compaction**

```python
class ContextCapsule(BaseModel):
    objective: str
    decisions: list[str]
    constraints: list[str]
    completed: list[str]
    pending: list[str]
    evidence: list[EvidenceCitation]
    workflow_state: dict[str, Any]
    unresolved_calls: list[str]

async def build_capsule(self, sources: Sequence[ContextItem]) -> ContextCapsule:
    response = await self._models.complete_structured(self._prompt(sources), schema=ContextCapsule)
    return self._validate_citations(response, sources)
```

Persist capsule artifact, source sequence ranges, prompt/model/version fingerprint, token counts, and validation result. On validation/model failure, fall back L3→L2→L1 and emit a compaction failure event.

- [ ] **Step 4: Add retrieval into later views and child-agent views**

Retrieve by deterministic recency/type/pin filters first, then optional semantic ranker. A child receives its TaskEnvelope, shared evidence references, and a separately budgeted Context View—not the parent's hidden working history by default.

```python
def retrieve(self, items: Sequence[ContextItem], request: RetrievalRequest) -> tuple[ContextItem, ...]:
    eligible = [item for item in items if item.pinned or item.kind in request.allowed_kinds]
    ranked = self._ranker.rank(eligible, request.query) if self._ranker else sorted(eligible, key=lambda item: item.sequence, reverse=True)
    return tuple(stable_pack(ranked, token_budget=request.token_budget))

def child_request(envelope: TaskEnvelope, evidence: Sequence[ContextItem], budget: int) -> RetrievalRequest:
    return RetrievalRequest(query=envelope.objective, explicit_ids=envelope.evidence_refs, token_budget=budget, include_parent_private=False)
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/integration/context/test_compaction.py -v`

Expected: provenance, citations, fallback, retrieval, child views, metrics, and unchanged ledger tests pass.

```powershell
git add src/agent_sdk/context tests/integration/context/test_compaction.py
git commit -m "feat: add validated context capsules"
```
