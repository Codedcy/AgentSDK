# v0.1 R3 Task 2 Independent Review

## Verdict

- Spec: **FAIL**
- Quality: **FAIL**
- Critical: **0**
- Important: **2**
- Minor: **0**
- Summary: **C0 / I2 / M0**

The implementation has the intended L0-L4 selection, lossless cap, distinct
LiteLLM operations, recursive same-Session evidence recovery, atomic
capsule/view persistence, and a valid migration of the two legacy integration
tests. It is not ready to close Task 2 because two required L3/L4 safety
properties are not enforced.

## Findings

### I1 - Over-budget L3/L4 output is persisted as successful compaction

- Path: `src/agent_sdk/context/planner.py`
- Lines: 387-402, especially 392-396
- Requirement: the R3 global constraints require L3/L4 model, validation,
  timeout, **or over-budget output** to fall back to deterministic L2 without
  failing the main Run.
- Evidence: `_persist_compacted()` stores
  `_estimate_compacted_tokens(...)` directly in `ContextView.estimated_tokens`
  and never compares that result with `budget.available_input_tokens`. A
  read-only reproduction used an available input budget of 100 tokens, an L3
  recommendation, and a structured capsule whose rendered estimate was 200.
  The result was:

  ```text
  L3 L3 200 ['context.compaction.completed', 'context.view.created']
  ```

  Thus the oversized result is labeled `applied_level=L3` and emits
  `context.compaction.completed` rather than the required L2 fallback and
  `context.compaction.failed`.
- Recommendation: estimate the rendered capsule plus retained messages before
  constructing/persisting a successful L3/L4 view. If it exceeds the applicable
  input budget, call the exact Task 1 L2 renderer and persist the same atomic
  fallback shape used for invalid structured output, preserving reported model
  usage. Add focused L3 and L4 over-budget regression tests.

### I2 - L3 validation accepts citations of retained recent/protected messages

- Path: `src/agent_sdk/context/compactor.py`
- Lines: 38-57, especially 52-53
- Requirement: L3 must summarize only a closed older slice while retaining
  recent/protected messages exactly.
- Evidence: `summarized` correctly excludes retained events, but
  `allowed_refs` is built from every source event. Consequently a capsule may
  cite both the closed older slice and retained messages and still pass
  validation. A read-only reproduction summarized `evt_old` while retaining
  `evt_recent`; the provider returned both refs and the compactor accepted:

  ```text
  ('evt_old', 'evt_recent')
  ```

  The same rule also permits a nominal L3 capsule when the closed older slice is
  empty if the model cites an id exposed through `retained_event_ids`. The
  migrated public API test falls back only because its fake returns an empty
  citation list in that case; the production invariant is not enforced.
- Recommendation: for `summarize`, make the allowed citation set exactly the
  closed older slice, require all of those refs, and treat an empty closed slice
  as a compaction failure that routes to L2. Add tests that reject extra
  retained citations and that force L3 with no closed older slice.

## Requirement-by-requirement assessment

1. L3 input excludes recent/protected sources and retains those messages in the
   view, but output citation validation does not enforce the same boundary:
   **FAIL (I2)**.
2. L4 loads Session-owned validated capsule snapshots, requires prior capsule
   ids in the new capsule, recursively resolves original events, detects cycles,
   and fails closed on cross-Session ownership: **PASS**.
3. Automatic recommendation applies L0-L4, and `allow_lossy=False` caps L3/L4
   at L2 without a model call: **PASS**.
4. Both structured requests use
   `ModelRequest.purpose="context_compaction"`: **PASS**.
5. Provider/schema/reference/input-size failures use the exact deterministic L2
   renderer and atomically persist failure evidence; output-budget failure is
   missing: **FAIL (I1)**.
6. Context View defaults preserve legacy snapshot compatibility; successful
   capsule/view events and fallback view/events are committed atomically:
   **PASS**, subject to I1/I2.
7. The legacy test migration constructs a real closed older slice and preserves
   retrieval/deletion and atomic-persistence assertions; it does not merely
   weaken expected levels: **PASS**.
8. The diff is limited to the four Task 2 Context modules and four authorized
   Task 2/migration test files; no Task 3 prompt or Task 4 middleware behavior
   was introduced: **PASS**.

## Fresh verification

Executed from `D:\code\AgentSDK\.worktrees\agent-sdk-implementation`:

```text
pytest tests/unit/context tests/integration/context -q
98 passed in 3.86s

ruff check src/agent_sdk/context tests/unit/context tests/integration/context
All checks passed!

mypy --strict src/agent_sdk/context
Success: no issues found in 9 source files

git diff --check 285364d..f187176
clean
```

The green gate confirms the existing behavior is internally consistent; the
two findings above are uncovered specification gaps rather than current-suite
regressions.
