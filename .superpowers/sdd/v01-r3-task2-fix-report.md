# v0.1 R3 Task 2 Review Fix Report

## Status

PASS. Independent review findings `C0 / I2 / M0` from `36e0b91` are fixed.

## Scope

The fix changes only:

- `src/agent_sdk/context/compactor.py`
- `src/agent_sdk/context/planner.py`
- `tests/unit/context/test_compaction_levels.py`
- `tests/integration/context/test_context_compaction.py`
- this report

No Task 3 prompt, Task 4 middleware, Agent Loop, dependency, public API, or
unrelated Context behavior was changed.

## Root-cause verification

### I1: over-budget structured output

The successful compaction path estimated capsule-plus-retained tokens while
constructing the persisted L3/L4 view. It did not compare that estimate with
`ContextBudget.available_input_tokens`, so oversized output reached the
`context.compaction.completed` commit.

### I2: L3 citation boundary

The summarize prompt correctly excluded retained recent/protected messages, but
validation used every source id as `allowed_refs`. That allowed model output to
cite ids it was explicitly told not to summarize. The empty closed-slice case
also reached the provider with only retained ids exposed.

## TDD evidence

Four regression tests were added before production edits:

- `test_l3_rejects_citation_of_retained_message`
- `test_l3_over_budget_output_falls_back_to_l2_with_usage`
- `test_forced_l3_with_empty_closed_slice_skips_model_and_falls_back`
- `test_l4_over_budget_output_falls_back_to_l2_with_usage`

Initial focused result:

```text
4 failed, 8 passed
```

The failures showed the exact review symptoms: retained citation accepted,
oversized L3 persisted as L3, empty-slice L3 called the provider, and oversized
L4 persisted as L4.

After the minimal fixes:

```text
12 passed in 2.74s
```

## Fix

- L3 `allowed_refs` and `required_refs` now both equal the closed older slice.
- An empty closed older slice returns a failed compaction result without a
  provider request; planner persists the existing deterministic L2 fallback.
- Planner estimates the validated capsule plus retained messages before
  successful L3/L4 persistence.
- If that estimate exceeds the current available input budget, planner invokes
  the exact Task 1 L2 renderer and uses the existing atomic failure path.
- The fallback stores only the Context View, sets `fallback_from` to the
  requested L3/L4 level, emits `context.compaction.failed` then
  `context.view.created`, and preserves the structured model usage.
- The successful path reuses the already computed estimate instead of counting
  tokens a second time.
- `CancelledError` remains outside all caught `AgentSDKError` paths.

## Verification

- Focused Task 2 review-fix suite: `12 passed`.
- Complete Context gate: `102 passed in 3.76s`.
- Ruff over Context source and tests: passed.
- Strict mypy over `src/agent_sdk/context`: passed.
- `git diff --check`: passed.
- Scope check: only the two Task 2 Context modules, two Task 2 tests, and this
  report.
