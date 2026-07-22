# Review package: aa2d410..HEAD

## Commits
72dd259 docs: approve v0.1 R3 checkpoint fixes
1e44ee0 docs: correct R3 checkpoint handoff
66107cb docs: review v0.1 R3 checkpoint
fcc8829 docs: record v0.1 R3 checkpoint
ab1d082 test: approve R3 task 4 recovery fixes
79996db fix: authenticate prepared model recovery evidence
4d0bb5b test: review R3 task 4 recovery boundaries
2f2048c feat: apply durable context to every model call
85f0e0e test: review R3 task 3 transition
83a8b4d docs: advance R3 task 3 checkpoint
c94ea77 test: approve R3 task 3 prompt manifests
774ae6c fix: authenticate legacy snapshot preconditions
8825897 test: final review R3 task 3 compatibility
37e4698 fix: authenticate legacy run creation
9c2abb8 test: rereview R3 task 3 fixes
7f33d89 fix: secure run creation and skill preflight
8f85363 test: review R3 task 3 prompt manifests
f80a956 feat: compose runtime prompt manifests
794739f test: approve R3 task 2 transition
43b8c60 test: advance R3 release ledger contract
7058cf2 test: review R3 task 2 transition
0f02efd docs: advance v0.1 R3 task 2 checkpoint
e5c646f test: approve R3 task 2 fixes
3f23363 fix: enforce context compaction safety
d016fcf test: review R3 task 2 context levels
f187176 test: migrate context compaction expectations
70b091e feat: implement automatic context levels
285364d docs: advance v0.1 R3 context checkpoint
93505aa fix: validate context tool calls and refs
38e7d2d fix: harden deterministic context strategies
dd93fb2 feat: add deterministic context strategies

## Files changed
 .superpowers/sdd/progress.md                       |  28 +-
 .superpowers/sdd/v01-r3-task2-fix-report.md        |  85 ++
 .superpowers/sdd/v01-r3-task2-rereview.md          |  86 ++
 .superpowers/sdd/v01-r3-task2-review.md            | 117 +++
 .../sdd/v01-r3-task2-transition-rereview.md        |  47 +
 .superpowers/sdd/v01-r3-task2-transition-review.md |  77 ++
 .superpowers/sdd/v01-r3-task3-approval.md          | 101 +++
 .superpowers/sdd/v01-r3-task3-final-review.md      | 156 ++++
 .superpowers/sdd/v01-r3-task3-fix-report.md        | 164 ++++
 .superpowers/sdd/v01-r3-task3-report.md            | 111 +++
 .superpowers/sdd/v01-r3-task3-rereview.md          | 159 ++++
 .superpowers/sdd/v01-r3-task3-review.md            | 175 ++++
 .superpowers/sdd/v01-r3-task3-transition-review.md |  48 +
 .superpowers/sdd/v01-r3-task4-fix-report.md        | 270 ++++++
 .superpowers/sdd/v01-r3-task4-report.md            | 144 +++
 .superpowers/sdd/v01-r3-task4-rereview.md          | 131 +++
 .superpowers/sdd/v01-r3-task4-review.md            | 187 ++++
 .superpowers/sdd/v01-r3-task5-report.md            |  66 ++
 .superpowers/sdd/v01-r3-task5-rereview.md          |  99 +++
 .superpowers/sdd/v01-r3-task5-review.md            | 115 +++
 docs/plans/releases/v0.1.md                        |  75 +-
 src/agent_sdk/__init__.py                          |  10 +-
 src/agent_sdk/api.py                               |  12 +-
 src/agent_sdk/config.py                            |   1 +
 src/agent_sdk/context/__init__.py                  |  22 +
 src/agent_sdk/context/compactor.py                 | 158 +++-
 src/agent_sdk/context/middleware.py                | 101 +++
 src/agent_sdk/context/models.py                    | 264 ++++--
 src/agent_sdk/context/planner.py                   | 615 +++++++++++--
 src/agent_sdk/context/rendering.py                 |  27 +
 src/agent_sdk/context/retrieval.py                 |  81 +-
 src/agent_sdk/context/sources.py                   | 200 +++++
 src/agent_sdk/context/strategies.py                | 230 +++++
 src/agent_sdk/context_runtime.py                   |  85 ++
 src/agent_sdk/observability/queries.py             |  53 +-
 src/agent_sdk/prompts/__init__.py                  |   2 +
 src/agent_sdk/prompts/composer.py                  |  19 +
 src/agent_sdk/prompts/models.py                    |   1 +
 src/agent_sdk/prompts/persistence.py               |  84 ++
 src/agent_sdk/runtime/commands.py                  |  18 +-
 src/agent_sdk/runtime/engine.py                    |  93 +-
 src/agent_sdk/runtime/execution.py                 |  50 ++
 src/agent_sdk/runtime/models.py                    | 117 +++
 src/agent_sdk/runtime/reconciliation.py            | 251 +++++-
 src/agent_sdk/runtime/recovery.py                  | 378 +++++---
 src/agent_sdk/skills/registry.py                   |  13 +
 src/agent_sdk/storage/sqlite.py                    |  96 +-
 tests/docs/test_v01_release_ledger.py              |  81 +-
 tests/e2e/test_v01_release.py                      | 188 ++++
 tests/integration/context/test_compaction_slice.py |  39 +-
 .../integration/context/test_context_compaction.py | 449 ++++++++++
 tests/integration/context/test_context_recovery.py | 693 +++++++++++++++
 .../integration/context/test_public_context_api.py |  20 +-
 .../integration/context/test_runtime_middleware.py | 232 +++++
 tests/integration/prompts/test_prompt_slice.py     |  35 +
 tests/integration/prompts/test_runtime_prompt.py   | 964 +++++++++++++++++++++
 tests/integration/runtime/test_text_agent_loop.py  |  21 +-
 .../runtime/test_tool_recovery_execution.py        |  20 +
 tests/unit/context/test_compaction_levels.py       | 156 ++++
 .../unit/context/test_deterministic_strategies.py  | 869 +++++++++++++++++++
 tests/unit/runtime/test_execution_descriptors.py   | 134 +++
 tests/unit/runtime/test_reconciliation_models.py   | 386 ++++++++-
 tests/unit/test_core_config.py                     |   3 +
 63 files changed, 9314 insertions(+), 398 deletions(-)

## Diff
diff --git a/.superpowers/sdd/progress.md b/.superpowers/sdd/progress.md
index 0912d83..1801356 100644
--- a/.superpowers/sdd/progress.md
+++ b/.superpowers/sdd/progress.md
@@ -236,21 +236,21 @@ M02-T003 Phase A fourth-fix checkpoint: leading-empty Tcl variable code/tests im
 M02-T003 Phase A fourth-fix pending gates: complete storage, full Python3.13, Ruff, strict mypy, py_compile, build/wheel/import/resources/scope; then append report, commit final evidence, and run a fresh independent C0/I0 review
 Next action on resume: read M02-T003-phaseA-leading-empty-variable-fix-brief.md, verify checkpoint commit, start with complete tests/integration/storage; do not redo the completed RED/GREEN or enter Phase B

 v0.1 release convergence decision (2026-07-17): written specification approved; detailed implementation planning complete
 v0.1 design: docs/superpowers/specs/2026-07-17-agent-sdk-v0.1-release-design.md
 v0.1 implementation index: docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-implementation-index.md
 v0.1 executable plans: R0 release harness; R1 built-in Tools/policy; R2 Workflow control; R3 automatic Context; R4 Child mailbox/tools; R5 Trace attribution/release
 v0.1 goal: release a usable functional closed loop before further production-grade hardening
 v0.1 recovery contract: resume from the last committed safe boundary; unknown in-flight Model/Tool work becomes interrupted and is never automatically replayed
 v0.1 required slices: R0 scope reset/release harness; R1 built-in read/write/bash and basic policy; R2 Workflow conditions/bounded loops; R3 automatic L0-L4 Context; R4 spawn/message/wait/list Child tools and mailbox; R5 Trace attribution/package/release
-v0.1 current implementation status: R0-R2 completed; R3 pending and unstarted
+v0.1 current implementation status: R0-R3 completed; R4 pending
 v0.1 M02-T003 decision: freeze after the committed Phase A focused checkpoint; absorb its pending full storage/project/build gates into the one release-candidate gate
 v0.1 deferred work: M02-T003 Artifact Phases B-D, M02-T004 advanced controls/sync, multi-worker exact recovery, complex Workflow scheduling, advanced Child scheduling, vector retrieval, advanced analytics/exporters, compatibility/performance/conformance hardening
 v0.1 R0 Task 1: complete (commits 2e0d164 and 6ff31b0; review Spec approved / Quality approved; fresh 2 tests passed and Ruff clean)
 v0.1 R0 plan ordering correction: e94b18c
 v0.1 R0 Task 2: complete (commits bd12f29 and 1ce4980; review Spec approved / Quality approved; fresh 3 tests passed and Ruff clean)
 v0.1 R0 checkpoint: complete (2026-07-17; commit: ef0e4da)
 v0.1 R0 checkpoint exact fresh evidence:
 ```text
 $ .\.venv\Scripts\python.exe -m pytest tests\docs\test_v01_release_ledger.py tests\e2e\test_v01_release.py tests\e2e\test_vertical_slice.py -q
 ....                                                                     [100%]
@@ -329,15 +329,31 @@ $ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\integratio

 $ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk\workflow tests\unit\workflow tests\integration\workflow tests\e2e\test_v01_release.py
 All checks passed!

 $ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk\workflow src\agent_sdk\runtime\execution.py
 Success: no issues found in 10 source files

 $ git diff --check f9beb63..826a32b
 clean
 ```
-v0.1 active next plan: docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md
-v0.1 resume command: `Get-Content docs\superpowers\plans\2026-07-17-agent-sdk-v0.1-r3-auto-context.md`
-v0.1 next required action: R3 Task 1 Step 1, creating `tests/unit/context/test_deterministic_strategies.py`
-v0.1 first RED command after that file exists: `.\.venv\Scripts\python.exe -m pytest tests/unit/context/test_deterministic_strategies.py -q`
-v0.1 R3 remains pending; R3 implementation has not started
+v0.1 active next plan: docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r4-child-mailbox.md
+v0.1 resume command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\runtime\test_capability_intersection.py -q`
+v0.1 R3 Task 1 deterministic L0-L2 is complete (commits dd93fb2, 38e7d2d, and 93505aa; began with `tests/unit/context/test_deterministic_strategies.py`)
+v0.1 R3 Task 1 final review: Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS
+v0.1 R3 Task 1 controller gates: 42 deterministic strategy tests; 48 context integration tests; Ruff clean; strict mypy clean across 4 files; diff-check clean
+v0.1 R3 Task 2: complete (automatic L0-L4 recommendation/application; `allow_lossy=False` caps L3/L4 at exact L2; distinct LiteLLM L3 summary and L4 rebase with purpose `context_compaction`; same-Session recursive evidence; atomic Context View/capsule/event persistence)
+v0.1 R3 Task 2 fallback contract: invalid, timeout, schema, reference, input-bound, or output-budget L3/L4 results use the exact deterministic L2 renderer without failing the main Run
+v0.1 R3 Task 2 final safety fix: `3f23363`; final independent re-review: `e5c646f`, Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS
+v0.1 R3 Task 2 fresh gates: Context 102 passed; Ruff clean; strict mypy clean; diff-check clean
+v0.1 R3 Task 3: complete (implementation `774ae6c`; final approval `c94ea77`; Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS)
+v0.1 R3 Task 3 delivered durable `AgentSpec`/`DurableAgentSpec` prompt and Context fields; public `SkillRegistry` exposure with one shared direct/Workflow/subagent preflight; ordered default/application/Skill prompt layers with persisted manifest; redacted public `run.created` schema v2; and authenticated genuine R2 schema-v1 recovery compatibility.
+v0.1 R3 Task 3 effective evidence: controller mainline 201 passed; implementer gate 521 passed, 1 skipped; Workflow/recovery/release gate 25 passed; Ruff clean; strict mypy clean across 92 source files.
+v0.1 R3 Task 4: complete (implementation `2f2048c`; recovery-evidence fix `79996db`; final approval `ab1d082`; Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS)
+v0.1 R3 Task 4 final approval: Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS
+v0.1 R3 Task 4 delivered ContextMiddleware preparation before each new model call, durable exact prepared requests, authenticated Context View/Prompt Manifest bindings, strict provider request validation, and no-side-effect failure for corrupted recovery evidence.
+v0.1 R3 checkpoint: complete (2026-07-20; Tasks 1-4 approved)
+v0.1 R3 checkpoint fresh evidence: 221 passed, 1 skipped in 25.32s across unit/context, integration/context, integration/prompts, reconciliation models, and v0.1 E2E; Ruff clean; strict mypy clean across 93 source files.
+v0.1 current implementation status: R0-R3 completed; R4 pending
+v0.1 active next plan: docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r4-child-mailbox.md
+v0.1 `tests/unit/runtime/test_capability_intersection.py` is created by R4 Task 1; it does not exist yet and the resume command is the first expected RED, not a current code failure.
+v0.1 resume command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\runtime\test_capability_intersection.py -q`
diff --git a/.superpowers/sdd/v01-r3-task2-fix-report.md b/.superpowers/sdd/v01-r3-task2-fix-report.md
new file mode 100644
index 0000000..a380022
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task2-fix-report.md
@@ -0,0 +1,85 @@
+# v0.1 R3 Task 2 Review Fix Report
+
+## Status
+
+PASS. Independent review findings `C0 / I2 / M0` from `d016fcf` are fixed.
+
+## Scope
+
+The fix changes only:
+
+- `src/agent_sdk/context/compactor.py`
+- `src/agent_sdk/context/planner.py`
+- `tests/unit/context/test_compaction_levels.py`
+- `tests/integration/context/test_context_compaction.py`
+- this report
+
+No Task 3 prompt, Task 4 middleware, Agent Loop, dependency, public API, or
+unrelated Context behavior was changed.
+
+## Root-cause verification
+
+### I1: over-budget structured output
+
+The successful compaction path estimated capsule-plus-retained tokens while
+constructing the persisted L3/L4 view. It did not compare that estimate with
+`ContextBudget.available_input_tokens`, so oversized output reached the
+`context.compaction.completed` commit.
+
+### I2: L3 citation boundary
+
+The summarize prompt correctly excluded retained recent/protected messages, but
+validation used every source id as `allowed_refs`. That allowed model output to
+cite ids it was explicitly told not to summarize. The empty closed-slice case
+also reached the provider with only retained ids exposed.
+
+## TDD evidence
+
+Four regression tests were added before production edits:
+
+- `test_l3_rejects_citation_of_retained_message`
+- `test_l3_over_budget_output_falls_back_to_l2_with_usage`
+- `test_forced_l3_with_empty_closed_slice_skips_model_and_falls_back`
+- `test_l4_over_budget_output_falls_back_to_l2_with_usage`
+
+Initial focused result:
+
+```text
+4 failed, 8 passed
+```
+
+The failures showed the exact review symptoms: retained citation accepted,
+oversized L3 persisted as L3, empty-slice L3 called the provider, and oversized
+L4 persisted as L4.
+
+After the minimal fixes:
+
+```text
+12 passed in 2.74s
+```
+
+## Fix
+
+- L3 `allowed_refs` and `required_refs` now both equal the closed older slice.
+- An empty closed older slice returns a failed compaction result without a
+  provider request; planner persists the existing deterministic L2 fallback.
+- Planner estimates the validated capsule plus retained messages before
+  successful L3/L4 persistence.
+- If that estimate exceeds the current available input budget, planner invokes
+  the exact Task 1 L2 renderer and uses the existing atomic failure path.
+- The fallback stores only the Context View, sets `fallback_from` to the
+  requested L3/L4 level, emits `context.compaction.failed` then
+  `context.view.created`, and preserves the structured model usage.
+- The successful path reuses the already computed estimate instead of counting
+  tokens a second time.
+- `CancelledError` remains outside all caught `AgentSDKError` paths.
+
+## Verification
+
+- Focused Task 2 review-fix suite: `12 passed`.
+- Complete Context gate: `102 passed in 3.76s`.
+- Ruff over Context source and tests: passed.
+- Strict mypy over `src/agent_sdk/context`: passed.
+- `git diff --check`: passed.
+- Scope check: only the two Task 2 Context modules, two Task 2 tests, and this
+  report.
diff --git a/.superpowers/sdd/v01-r3-task2-rereview.md b/.superpowers/sdd/v01-r3-task2-rereview.md
new file mode 100644
index 0000000..5cf9f57
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task2-rereview.md
@@ -0,0 +1,86 @@
+# v0.1 R3 Task 2 Independent Re-review
+
+## Verdict
+
+- Spec: **PASS**
+- Quality: **PASS**
+- Critical: **0**
+- Important: **0**
+- Minor: **0**
+- Summary: **C0 / I0 / M0**
+- Approval: **APPROVED**
+
+The fix commit `3f23363` resolves both Important findings from the independent
+review at `d016fcf`. No remaining Critical or Important issue blocks Task 2.
+
+## Original finding verification
+
+### I1 - Over-budget L3/L4 output
+
+**Resolved.**
+
+- `ContextPlanner.build()` now estimates the validated capsule plus retained
+  messages before calling the successful persistence path.
+- An estimate greater than `budget.available_input_tokens` invokes the same
+  Task 1 deterministic L2 renderer and the existing atomic fallback
+  persistence path.
+- The fallback view has `applied_level=L2`, `fallback_from` set to the requested
+  L3/L4 level, and no capsule id or capsule snapshot.
+- The one fallback commit contains `context.compaction.failed` followed by
+  `context.view.created` and only a Context View snapshot.
+- Structured model usage is preserved in both the failure event and the view
+  event.
+- The successful path receives the already validated estimate, so no
+  `context.compaction.completed` event or capsule snapshot can be committed
+  before the budget decision.
+- Separate L3 and L4 regression tests exercise estimates of 101 against an
+  available budget of 100 and assert L2 fallback, no new completion event,
+  failure evidence, `fallback_from`, and usage.
+
+### I2 - L3 citation boundary and empty closed slice
+
+**Resolved.**
+
+- `ContextCompactor.summarize()` now derives both `allowed_refs` and
+  `required_refs` exclusively from the closed older slice.
+- A capsule that additionally cites a retained recent/protected message is
+  rejected while preserving the reported structured-completion usage.
+- An empty closed older slice returns a failed compaction result before
+  `complete_structured`, so the provider is not called and the planner persists
+  the normal L2 fallback.
+- Regression tests cover both the retained-citation rejection and the
+  empty-slice no-model-call fallback.
+
+## Additional checks
+
+- `asyncio.CancelledError` remains outside the caught `AgentSDKError` paths and
+  the existing cancellation regression remains green.
+- The new tests are additive. No prior assertion was removed or weakened.
+- The production diff is limited to
+  `src/agent_sdk/context/compactor.py` and
+  `src/agent_sdk/context/planner.py`; test changes are limited to the two Task 2
+  test files. There is no Task 3 prompt, Task 4 middleware, public API,
+  dependency, or unrelated behavior expansion.
+- Existing invalid-schema/reference/input-bound fallback, recursive L4
+  evidence, cross-Session isolation, snapshot compatibility, and legacy test
+  migration behavior remain green.
+
+## Fresh verification
+
+Executed from `D:\code\AgentSDK\.worktrees\agent-sdk-implementation`:
+
+```text
+pytest tests/unit/context tests/integration/context -q
+102 passed in 4.04s
+
+ruff check src/agent_sdk/context tests/unit/context tests/integration/context
+All checks passed!
+
+mypy --strict src/agent_sdk/context
+Success: no issues found in 9 source files
+
+git diff --check d016fcf..3f23363
+clean
+```
+
+Task 2 is approved to proceed to its durable progress transition.
diff --git a/.superpowers/sdd/v01-r3-task2-review.md b/.superpowers/sdd/v01-r3-task2-review.md
new file mode 100644
index 0000000..62ce6cd
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task2-review.md
@@ -0,0 +1,117 @@
+# v0.1 R3 Task 2 Independent Review
+
+## Verdict
+
+- Spec: **FAIL**
+- Quality: **FAIL**
+- Critical: **0**
+- Important: **2**
+- Minor: **0**
+- Summary: **C0 / I2 / M0**
+
+The implementation has the intended L0-L4 selection, lossless cap, distinct
+LiteLLM operations, recursive same-Session evidence recovery, atomic
+capsule/view persistence, and a valid migration of the two legacy integration
+tests. It is not ready to close Task 2 because two required L3/L4 safety
+properties are not enforced.
+
+## Findings
+
+### I1 - Over-budget L3/L4 output is persisted as successful compaction
+
+- Path: `src/agent_sdk/context/planner.py`
+- Lines: 387-402, especially 392-396
+- Requirement: the R3 global constraints require L3/L4 model, validation,
+  timeout, **or over-budget output** to fall back to deterministic L2 without
+  failing the main Run.
+- Evidence: `_persist_compacted()` stores
+  `_estimate_compacted_tokens(...)` directly in `ContextView.estimated_tokens`
+  and never compares that result with `budget.available_input_tokens`. A
+  read-only reproduction used an available input budget of 100 tokens, an L3
+  recommendation, and a structured capsule whose rendered estimate was 200.
+  The result was:
+
+  ```text
+  L3 L3 200 ['context.compaction.completed', 'context.view.created']
+  ```
+
+  Thus the oversized result is labeled `applied_level=L3` and emits
+  `context.compaction.completed` rather than the required L2 fallback and
+  `context.compaction.failed`.
+- Recommendation: estimate the rendered capsule plus retained messages before
+  constructing/persisting a successful L3/L4 view. If it exceeds the applicable
+  input budget, call the exact Task 1 L2 renderer and persist the same atomic
+  fallback shape used for invalid structured output, preserving reported model
+  usage. Add focused L3 and L4 over-budget regression tests.
+
+### I2 - L3 validation accepts citations of retained recent/protected messages
+
+- Path: `src/agent_sdk/context/compactor.py`
+- Lines: 38-57, especially 52-53
+- Requirement: L3 must summarize only a closed older slice while retaining
+  recent/protected messages exactly.
+- Evidence: `summarized` correctly excludes retained events, but
+  `allowed_refs` is built from every source event. Consequently a capsule may
+  cite both the closed older slice and retained messages and still pass
+  validation. A read-only reproduction summarized `evt_old` while retaining
+  `evt_recent`; the provider returned both refs and the compactor accepted:
+
+  ```text
+  ('evt_old', 'evt_recent')
+  ```
+
+  The same rule also permits a nominal L3 capsule when the closed older slice is
+  empty if the model cites an id exposed through `retained_event_ids`. The
+  migrated public API test falls back only because its fake returns an empty
+  citation list in that case; the production invariant is not enforced.
+- Recommendation: for `summarize`, make the allowed citation set exactly the
+  closed older slice, require all of those refs, and treat an empty closed slice
+  as a compaction failure that routes to L2. Add tests that reject extra
+  retained citations and that force L3 with no closed older slice.
+
+## Requirement-by-requirement assessment
+
+1. L3 input excludes recent/protected sources and retains those messages in the
+   view, but output citation validation does not enforce the same boundary:
+   **FAIL (I2)**.
+2. L4 loads Session-owned validated capsule snapshots, requires prior capsule
+   ids in the new capsule, recursively resolves original events, detects cycles,
+   and fails closed on cross-Session ownership: **PASS**.
+3. Automatic recommendation applies L0-L4, and `allow_lossy=False` caps L3/L4
+   at L2 without a model call: **PASS**.
+4. Both structured requests use
+   `ModelRequest.purpose="context_compaction"`: **PASS**.
+5. Provider/schema/reference/input-size failures use the exact deterministic L2
+   renderer and atomically persist failure evidence; output-budget failure is
+   missing: **FAIL (I1)**.
+6. Context View defaults preserve legacy snapshot compatibility; successful
+   capsule/view events and fallback view/events are committed atomically:
+   **PASS**, subject to I1/I2.
+7. The legacy test migration constructs a real closed older slice and preserves
+   retrieval/deletion and atomic-persistence assertions; it does not merely
+   weaken expected levels: **PASS**.
+8. The diff is limited to the four Task 2 Context modules and four authorized
+   Task 2/migration test files; no Task 3 prompt or Task 4 middleware behavior
+   was introduced: **PASS**.
+
+## Fresh verification
+
+Executed from `D:\code\AgentSDK\.worktrees\agent-sdk-implementation`:
+
+```text
+pytest tests/unit/context tests/integration/context -q
+98 passed in 3.86s
+
+ruff check src/agent_sdk/context tests/unit/context tests/integration/context
+All checks passed!
+
+mypy --strict src/agent_sdk/context
+Success: no issues found in 9 source files
+
+git diff --check 285364d..f187176
+clean
+```
+
+The green gate confirms the existing behavior is internally consistent; the
+two findings above are uncovered specification gaps rather than current-suite
+regressions.
diff --git a/.superpowers/sdd/v01-r3-task2-transition-rereview.md b/.superpowers/sdd/v01-r3-task2-transition-rereview.md
new file mode 100644
index 0000000..b0f4744
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task2-transition-rereview.md
@@ -0,0 +1,47 @@
+# v0.1 R3 Task 2 Durable Transition Re-review
+
+## Verdict
+
+- Spec: **PASS**
+- Quality: **PASS**
+- Critical: **0**
+- Important: **0**
+- Minor: **0**
+- Summary: **C0 / I0 / M0**
+- Approval: **APPROVED**
+
+Commit `43b8c60` closes the sole Important finding from the transition review at
+`7058cf2`.
+
+## I1 verification
+
+**Resolved.**
+
+- The documentation contract now requires Task 2 implementation/review commits
+  `3f23363` and `e5c646f`, the 102-test evidence, and Task 2 completion.
+- It requires the Task 3 Step 1 resume point,
+  `tests/integration/prompts/test_runtime_prompt.py`, and the exact Windows
+  pytest command with disabled plugin autoload plus explicit
+  `pytest_asyncio.plugin`.
+- It explicitly rejects the superseded Task 2 Step 1 marker, old Task 2 test
+  path, and `pending/unstarted` status.
+- The exact progress assertion now requires R3 to remain in progress with Tasks
+  1-2 complete and Task 3 pending.
+- The migration replaces stale assertions with the new durable contract; it
+  does not remove the R3 status, evidence, resume-command, or absence guards.
+- The diff changes only `tests/docs/test_v01_release_ledger.py`.
+
+## Fresh verification
+
+```text
+pytest -p pytest_asyncio.plugin tests/docs -q
+2 passed in 0.01s
+
+ruff check tests/docs/test_v01_release_ledger.py
+All checks passed!
+
+git diff --check 7058cf2..43b8c60
+clean
+```
+
+The durable Task 2 transition and its executable ledger contract are approved.
diff --git a/.superpowers/sdd/v01-r3-task2-transition-review.md b/.superpowers/sdd/v01-r3-task2-transition-review.md
new file mode 100644
index 0000000..022ae79
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task2-transition-review.md
@@ -0,0 +1,77 @@
+# v0.1 R3 Task 2 Durable Transition Review
+
+## Verdict
+
+- Spec: **FAIL**
+- Quality: **FAIL**
+- Critical: **0**
+- Important: **1**
+- Minor: **0**
+- Summary: **C0 / I1 / M0**
+- Approval: **NOT APPROVED**
+
+The two changed transition documents accurately record Task 2 implementation
+commit `3f23363`, final re-review commit `e5c646f`, C0/I0/M0, the 102-test
+Context gate, and R3 as still `in_progress`. The Task 3 Step 1 file and the
+Windows pytest command with explicit `pytest_asyncio.plugin` are also accurate.
+One required durable-contract migration is missing.
+
+## Finding
+
+### I1 - The release-ledger contract test still requires the superseded Task 2 pending resume point
+
+- Path: `tests/docs/test_v01_release_ledger.py`
+- Lines: 80-83, 131-134, 183-187
+- Evidence: the transition correctly replaces the Task 2 pending text in both
+  durable documents, but the tracked documentation contract still asserts:
+
+  - `R3 Task 2 Step 1`;
+  - `tests/unit/context/test_compaction_levels.py`;
+  - the old Task 2 RED command;
+  - `R3 Task 2 remains pending/unstarted`;
+  - the exact old progress status saying Task 2 is pending.
+
+  Fresh verification after `0f02efd`:
+
+  ```text
+  pytest tests/docs/test_v01_release_ledger.py -q
+  1 failed, 1 passed
+  ```
+
+  The first failure is the obsolete exact progress-status assertion at lines
+  183-187; after that is migrated, the helper assertions at lines 131-134 would
+  still require the removed Task 2 resume point.
+- Impact: the durable transition leaves the repository's release-ledger gate
+  red and no longer has executable protection for the new Task 3 resume point.
+  This is an omission rather than an error in the two document facts.
+- Recommendation: in a narrow follow-up, migrate
+  `tests/docs/test_v01_release_ledger.py` to assert:
+
+  - R3 remains `in_progress`;
+  - Tasks 1-2 are complete;
+  - commits `3f23363` and `e5c646f`;
+  - C0/I0/M0 and the 102-test evidence;
+  - Task 3 Step 1,
+    `tests/integration/prompts/test_runtime_prompt.py`, and the exact
+    environment-specific pytest command;
+  - absence of the superseded Task 2 pending/resume markers.
+
+  Re-run the full documentation test file before approving the transition.
+
+## Fact and scope checks
+
+- `3f23363` is the Task 2 safety-fix commit: **PASS**.
+- `e5c646f` is the final C0/I0/M0 independent re-review: **PASS**.
+- `102 passed`, Ruff clean, and strict mypy clean match the final evidence:
+  **PASS**.
+- Both documents keep R3 `in_progress` and do not mark R3 complete: **PASS**.
+- The resume point is Task 3 Step 1 and the named test file does not yet exist,
+  as expected before RED: **PASS**.
+- The Task 3 command matches this environment's disabled plugin autoload and
+  explicit asyncio plugin requirement: **PASS**.
+- Commit `0f02efd` changes only
+  `docs/plans/releases/v0.1.md` and `.superpowers/sdd/progress.md`: **PASS**.
+- `git diff --check e5c646f..0f02efd`: **clean**.
+
+The transition can be approved after the stale documentation contract test is
+migrated and passes.
diff --git a/.superpowers/sdd/v01-r3-task3-approval.md b/.superpowers/sdd/v01-r3-task3-approval.md
new file mode 100644
index 0000000..8a6693f
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task3-approval.md
@@ -0,0 +1,101 @@
+# v0.1 R3 Task 3 Final Approval
+
+Review range: `8825897..774ae6c`
+
+Verdict: **APPROVED**
+
+- Spec: **PASS**
+- Quality: **PASS**
+- Critical: **0**
+- Important: **0**
+- Minor: **0**
+
+The last open legacy precondition finding is closed. The previously closed
+public Trace and Skill-preflight findings remain closed, and this patch does
+not enter Task 4 scope.
+
+## Legacy precondition finding — CLOSED
+
+`SQLiteStore._legacy_v1_run_snapshot_matches` now permits normalized semantic
+equality only after authenticating complete legacy creation evidence:
+
+- the raw stored snapshot must be canonical JSON;
+- it must validate as a complete `RunSnapshot`;
+- its `run_id` must match the precondition entity;
+- exactly one `run.created` event may exist for that Run;
+- the event Session must equal the stored Run Session;
+- the event sequence must be exactly 1;
+- the event schema version must be exactly 1;
+- the raw event payload must be canonical JSON;
+- `run_created_event_matches(..., schema_version=1)` must authenticate the
+  complete historical payload, including original R2 descriptor hashes and
+  normalized identity/state;
+- the expected precondition data must validate as a complete `RunSnapshot` and
+  equal the fully normalized stored snapshot.
+
+The compatibility exception therefore applies only to a genuine, uniquely
+owned schema-v1 creation event. Current schema-v2 Runs and all other snapshot
+preconditions retain byte-exact comparison; the v2 event matcher and public
+payload were not relaxed.
+
+## Positive and negative evidence
+
+The focused compatibility tests prove:
+
+- a genuine R2 raw private snapshot plus its authenticated v1 creation event
+  accepts the normalized snapshot precondition;
+- the same R2 data survives SQLite reopen, builds an execution tree, produces
+  a recovery plan, resumes provider execution, and completes;
+- each of the following fails closed:
+  - wrong event Session;
+  - wrong event sequence;
+  - wrong event schema version;
+  - forged event payload;
+  - noncanonical event payload JSON;
+  - wrong original legacy descriptor hash;
+  - multiple `run.created` events.
+
+The tests exercise the real SQLite persistence/precondition implementation
+rather than a duplicated validation helper.
+
+## Previously closed findings remain closed
+
+- Schema-v2 `run.created` remains an explicit minimal public payload containing
+  creation identity, ordinary user input, and hashes only.
+- Public events contain no raw application system prompt, Skill/profile
+  instructions, model parameters, or Tool schemas.
+- Full execution descriptors remain private in Run snapshots and idempotency
+  results.
+- Direct, Workflow-node, and subagent execution still share the injected
+  `SkillRegistry.validate_agent` preflight and fail before Run persistence,
+  provider execution, or child task creation when a Skill is unavailable.
+- Genuine schema-v1 historical descriptors validate their original hashes
+  before safe default upgrade; malformed, cross-Session, or forged evidence is
+  rejected by recovery and execution-tree authentication.
+- No Context middleware, prepared model-request, or other Task 4 behavior was
+  introduced.
+
+## Fresh verification
+
+```text
+pytest tests/integration/prompts/test_runtime_prompt.py
+       -k "normalized_snapshot_precondition or authenticated_event_allows"
+8 passed, 14 deselected in 6.04s
+
+pytest tests/integration/prompts/test_runtime_prompt.py
+       tests/unit/runtime/test_execution_descriptors.py
+48 passed in 6.16s
+
+ruff check src/agent_sdk
+           tests/integration/prompts/test_runtime_prompt.py
+           tests/unit/runtime/test_execution_descriptors.py
+All checks passed!
+
+mypy --strict src/agent_sdk
+Success: no issues found in 92 source files
+
+git diff --check 8825897..774ae6c
+clean
+```
+
+Task 3 may proceed to its transition/checkpoint gate.
diff --git a/.superpowers/sdd/v01-r3-task3-final-review.md b/.superpowers/sdd/v01-r3-task3-final-review.md
new file mode 100644
index 0000000..43696fd
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task3-final-review.md
@@ -0,0 +1,156 @@
+# v0.1 R3 Task 3 Final Compatibility Review
+
+Review range: `9c2abb8..37e4698`
+
+Verdict: **CHANGES_REQUIRED**
+
+- Spec: **FAIL**
+- Quality: **FAIL**
+- Critical: **0**
+- Important: **1**
+- Minor: **0**
+
+The genuine R2 schema-v1 recovery fix and the previous review findings are
+closed on their tested paths. Task 3 is not yet approved because the SQLite
+legacy snapshot-precondition exception does not verify that its qualifying v1
+creation event is legal.
+
+## Confirmed closures
+
+### Original C1 — CLOSED
+
+- Current `run.created` events retain the explicit, minimal schema-v2 public
+  payload.
+- Public events do not expose application system prompts, Skill/profile
+  instructions, model parameters, or raw Tool schemas.
+- Full execution descriptors remain private in Run snapshots and idempotency
+  results.
+- Schema-v2 recovery and execution-tree authentication still derive and
+  compare the exact public payload and hashes from the authoritative private
+  snapshot. This final compatibility patch does not relax the v2 branch.
+
+### Original I1 — CLOSED
+
+- The production SDK still injects `SkillRegistry.validate_agent` at the shared
+  `RuntimeCommands.start_run` boundary.
+- Direct, Workflow-node, and subagent paths fail with normalized,
+  non-retryable `invalid_state` before Run persistence, provider execution, or
+  child task creation when a configured Skill is unavailable.
+
+### Previous re-review I1 — CLOSED
+
+- `run_created_event_matches(..., schema_version=1)` now validates the complete
+  raw historical `RunSnapshot`. Nested descriptor validation authenticates
+  the original R2 `agent_hash` and `descriptor_hash`, applies the safe legacy
+  defaults, and compares the complete normalized created state.
+- A genuine R2 raw descriptor/event/private snapshot survives SQLite close and
+  reopen, builds an execution tree, produces a recovery plan, resumes provider
+  execution, and reaches a completed Run.
+- Wrong event agent/descriptor hashes, identity changes, cross-Session event
+  ownership, wrong private snapshot hashes, and noncanonical private snapshot
+  JSON fail closed in the added tests.
+- Multiple or non-v1 `run.created` versions do not qualify for the legacy
+  snapshot-precondition fallback.
+
+### Previous M1 — CLOSED
+
+`git diff --check 9c2abb8..37e4698` is clean.
+
+## Finding
+
+### I1 — Legacy exact-precondition fallback accepts an invalid v1 creation event
+
+`SQLiteStore._legacy_v1_run_snapshot_matches` queries only:
+
+```sql
+SELECT schema_version FROM events
+WHERE run_id = ? AND type = 'run.created'
+```
+
+It requires the resulting tuple to equal `(1,)`, then compares the complete
+normalized stored and expected Run snapshots. It does **not** load or validate
+the qualifying event's:
+
+- `session_id`;
+- `sequence`;
+- payload shape and canonical JSON;
+- original legacy descriptor hashes;
+- semantic equality with the stored Run snapshot.
+
+Consequently, the exception is limited to one schema-v1 event, but not to one
+**legal** schema-v1 event as required.
+
+Fresh minimal reproduction:
+
+1. Create a valid current Run.
+2. Convert its private snapshot to a genuine R2 raw descriptor with valid old
+   hashes.
+3. Convert its creation event to schema v1, but change the event Session to
+   `ses_forged` and replace the entire payload with `{"forged": "payload"}`.
+4. Submit a `SnapshotPrecondition` containing the complete normalized
+   `RunSnapshot`.
+
+Observed result:
+
+```text
+malformed_v1_event_precondition_accepted=True
+```
+
+Impact:
+
+The SQLite optimistic-concurrency compatibility path accepts semantic
+snapshot equality under corrupted or cross-Session historical evidence. Other
+recovery and execution-tree checks reject this event, but the exact
+precondition boundary itself is deliberately weakened and does not satisfy the
+required fail-closed contract.
+
+Required fix:
+
+- Load the complete set of `run.created` rows for the target Run.
+- Require exactly one event with schema version 1, sequence 1, the same Session
+  as the stored/precondition Run, and canonical payload JSON.
+- Validate/authenticate the raw historical payload using the schema-v1
+  `run_created_event_matches` path before allowing normalized snapshot
+  equality.
+- Retain byte-exact matching for schema-v2 Runs and every non-Run snapshot.
+- Add a focused test proving wrong event Session, sequence, payload, and
+  original descriptor hashes reject the normalized legacy precondition.
+
+## Scope
+
+- The compatibility patch does not add Context middleware or otherwise enter
+  Task 4 scope.
+- No further Critical or Minor finding was identified in the requested final
+  review scope.
+
+## Fresh verification
+
+```text
+pytest tests/integration/prompts/test_runtime_prompt.py
+       tests/unit/runtime/test_execution_descriptors.py
+40 passed in 5.04s
+
+pytest tests/integration/prompts
+       tests/integration/observability/test_queries.py
+       tests/unit/context tests/integration/context
+       tests/integration/runtime/test_provider_recovery_execution.py
+277 passed, 1 skipped in 16.63s
+
+ruff check src/agent_sdk
+           tests/integration/prompts/test_runtime_prompt.py
+           tests/unit/runtime/test_execution_descriptors.py
+All checks passed!
+
+mypy --strict src/agent_sdk
+Success: no issues found in 92 source files
+
+git diff --check 9c2abb8..37e4698
+clean
+```
+
+The skipped test is the existing package-build check when `uv` is unavailable.
+An additional exploratory broad runtime selection was not used as passing
+evidence: it produced 408 passes, 1 skip, and 115 failures concentrated in
+existing recovery tests whose empty seeded Tool descriptors conflict with the
+SDK's pre-existing default built-in Tool set. The reviewed range does not
+change that default or those seed helpers.
diff --git a/.superpowers/sdd/v01-r3-task3-fix-report.md b/.superpowers/sdd/v01-r3-task3-fix-report.md
new file mode 100644
index 0000000..5822aa6
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task3-fix-report.md
@@ -0,0 +1,164 @@
+# v0.1 R3 Task 3 Review Fix Report
+
+## Status
+
+PASS. Independent review findings `C1 / I1` from `8f85363` are fixed.
+
+## Scope
+
+The fix changes only the Task 3 prompt/runtime creation boundary, the shared
+Skills preflight, the existing recovery and execution-tree consumers of
+`run.created`, Task 3 integration tests, and this report. Task 4 middleware and
+unrelated Agent Loop behavior were not added.
+
+## Root-cause verification
+
+### C1: private execution descriptors in public events
+
+`RuntimeCommands.start_run` used the complete private `RunSnapshot` for the
+public `run.created` event as well as for the private snapshot and idempotency
+result. The event therefore exposed application system prompts, model
+parameters, and raw tool schemas.
+
+### I1: Skills preflight covered only direct runs
+
+The Skills activation check lived in `RunAPI.start`. Workflow nodes and
+subagents call the shared `RuntimeCommands.start_run` entry directly, so an
+unavailable configured Skill reached durable run creation and the provider on
+those paths.
+
+## TDD evidence
+
+Three regression tests were added before production edits:
+
+- `test_public_run_events_never_expose_prompt_or_tool_sentinels`
+- `test_workflow_missing_skill_fails_before_node_run_or_provider_call`
+- `test_subagent_missing_skill_fails_before_child_run_or_provider_call`
+
+Initial focused result:
+
+```text
+3 failed
+```
+
+The failures showed the exact review symptoms: private descriptor text in
+`run.created`, Workflow execution reaching the provider, and no shared
+subagent preflight entry.
+
+After the fixes:
+
+```text
+3 passed
+```
+
+## Fix
+
+- New `run.created` writes use schema version 2 and a dedicated
+  `RunCreatedEventPayload`.
+- The public payload keeps ordinary creation identity and user input needed by
+  Context and trace consumers, plus descriptor/agent/tool and private-envelope
+  hashes. It does not contain an execution descriptor, system prompt, Skill or
+  packaged-profile instructions, raw model parameters, or raw tool schemas.
+- The complete descriptor remains in the private Run snapshot and idempotency
+  result, preserving recovery and replay evidence.
+- Recovery authenticates schema-v2 creation events against the private Run
+  snapshot and hashes. Schema-v1 full-snapshot creation events remain readable.
+- Execution-tree assembly accepts both schema-v1 and schema-v2 creation events,
+  loads authoritative private snapshots, and verifies their creation identity.
+- `SkillRegistry.validate_agent` is injected into the single
+  `RuntimeCommands.start_run` creation boundary used by direct runs, Workflow
+  nodes, and subagents.
+- Low-level `RuntimeCommands` remains backward compatible: callers that do not
+  inject a preflight callback retain the prior no-op behavior.
+- A failed Skills preflight raises the constant public `INVALID_STATE` error
+  before any Run event, snapshot, provider call, or child task is created. A
+  Workflow container may already exist, but no node Run is created.
+
+## Verification
+
+- Task 3 prompt tests: `6 passed`.
+- Combined prompt/subagent/workflow/observability/context/provider-recovery
+  gate: `235 passed`.
+- Workflow recovery, child workflow, and subprocess recovery gate:
+  `22 passed`.
+- Release vertical slices: `3 passed`.
+- Ruff over all source plus the changed Task 3 test: passed.
+- Strict mypy over all 92 source files: passed.
+
+## Re-review fix
+
+Re-review `9c2abb8` found that the schema-v1 compatibility branch compared an
+immutable raw R2 `run.created` payload with the upgraded current serialization.
+It also exposed two SQLite recovery reads that treated a safely upgraded raw
+R2 private Run snapshot as non-authoritative.
+
+The second-round fix is limited to schema-v1 compatibility:
+
+- The v1 matcher first validates the complete raw historical `RunSnapshot`.
+  Nested `ExecutionDescriptor` validation authenticates the original R2
+  `agent_hash` and `descriptor_hash`, applies the existing safe legacy prompt
+  defaults, and rejects malformed or extra fields.
+- It then compares the complete normalized created snapshot, including the
+  normalized execution descriptor, with the authoritative private snapshot.
+- Schema-v2 matching remains the existing exact `RunCreatedEventPayload`
+  derivation and equality check.
+- SQLite recovery validates the stored raw snapshot JSON as canonical and
+  validates it as a complete `RunSnapshot`; it no longer requires the upgraded
+  serialization to be byte-identical to immutable R2 JSON.
+- Exact Run preconditions may use normalized semantic equality only when the
+  Run has exactly one schema-v1 `run.created` event. Current schema-v2 Runs
+  retain byte-exact precondition matching.
+
+Second-round TDD evidence:
+
+- Genuine R2 matcher test: `1 failed` before the production change, then
+  `1 passed`.
+- SQLite close/reopen recovery, execution-tree, old-hash, identity,
+  cross-Session, and noncanonical-storage gate: `8 passed`.
+- Wrong original event and private-snapshot hashes fail closed.
+- Cross-Session event/snapshot pairs fail execution-tree authentication.
+- Runtime descriptor, Task 3 prompt, observability, Context,
+  provider-recovery, and SQLite recovery/progress gate:
+  `513 passed, 1 skipped`.
+- Workflow recovery, child workflow, subprocess recovery, and release slices:
+  `25 passed`.
+- Ruff passed; strict mypy passed over all 92 source files.
+- Both working-tree and range `git diff --check` passed.
+
+## Final-review fix
+
+Final review `8825897` found that the SQLite schema-v1 semantic precondition
+fallback checked only for one schema-v1 `run.created` event. A malformed event
+could therefore authorize normalized equality against a legacy private Run
+snapshot.
+
+The final fix keeps the fallback limited to legacy schema-v1 Runs and requires
+complete creation evidence:
+
+- The raw stored snapshot must be canonical JSON, validate as a complete
+  `RunSnapshot`, and match the precondition Run identity.
+- There must be exactly one `run.created` event for the Run.
+- The event Session must match the snapshot Session, its sequence must be 1,
+  and its schema version must be 1.
+- The raw event payload must be canonical JSON and must authenticate the
+  complete stored snapshot through `run_created_event_matches`.
+- The normalized expected snapshot must validate and equal the normalized
+  stored snapshot.
+- Schema-v2 preconditions retain the existing byte-exact behavior.
+
+Final-round TDD evidence:
+
+- Direct precondition negatives initially produced `4 failed, 2 passed`;
+  forged event Session, sequence, payload, and legacy descriptor hash were
+  incorrectly accepted.
+- After the fix, the genuine R2 positive plus event Session, sequence,
+  schema-version, forged payload, noncanonical payload, legacy descriptor
+  hash, and duplicate-creation negatives produced `8 passed`.
+- Complete prompt and execution-descriptor gate: `48 passed`.
+- Runtime descriptor, Task 3 prompt, observability, Context,
+  provider-recovery, and SQLite recovery/progress gate:
+  `521 passed, 1 skipped`.
+- Workflow recovery, child workflow, subprocess recovery, and release slices:
+  `25 passed`.
+- Ruff passed; strict mypy passed over all 92 source files.
+- Both working-tree and `8825897` range `git diff --check` passed.
diff --git a/.superpowers/sdd/v01-r3-task3-report.md b/.superpowers/sdd/v01-r3-task3-report.md
new file mode 100644
index 0000000..6d7fa70
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task3-report.md
@@ -0,0 +1,111 @@
+# v0.1 R3 Task 3 Implementation Report
+
+Status: DONE_WITH_CONCERNS
+
+## Scope
+
+Implemented only the Task 3 prompt/Skill/descriptor and persistence seam. The
+RunEngine still sends its legacy checkpoint messages; ContextMiddleware and
+per-model-call prompt preparation remain Task 4.
+
+## TDD evidence
+
+Initial RED:
+
+```text
+$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\integration\prompts\test_runtime_prompt.py tests\unit\runtime\test_execution_descriptors.py tests\integration\prompts\test_prompt_slice.py -q
+ERROR tests/integration/prompts/test_runtime_prompt.py
+  PromptManifestPersistence is absent
+ERROR tests/unit/runtime/test_execution_descriptors.py
+  ContextRuntimeConfig is absent
+2 errors during collection
+```
+
+The first implementation run exposed two additional real boundaries:
+
+- importing Context configuration through the eager `agent_sdk.context`
+  package caused a runtime/context circular import;
+- a Session-owned manifest event at sequence 1 collided with
+  `session.created`.
+
+The fixes place pure Context runtime configuration in
+`agent_sdk.context_runtime` (re-exported through the public Context APIs) and
+give each manifest event its own `manifest_id` event aggregate.
+
+SQLite RED:
+
+```text
+test_prompt_manifest_survives_sqlite_reopen
+ValueError: current snapshot kind is invalid
+```
+
+The current SQLite projection validator now recognizes a version-1
+`prompt_manifest`, validates its identity, and requires its Context View to
+belong to the same Session.
+
+## Implemented behavior
+
+- Added `ContextRuntimeConfig` with the planned defaults and bounds.
+- Added `prompt_profile`, `system_prompt`, ordered unique nonempty `skills`,
+  and `context` to `AgentSpec` and `DurableAgentSpec`.
+- New fields participate in `agent_hash` and `descriptor_hash`.
+- Canonically hashed legacy descriptors missing the Task 3 fields are
+  validated first, upgraded to declared defaults, and rehashed in memory.
+- Added `AgentSDKConfig.skill_roots`; SDK initialization creates and discovers
+  one `SkillRegistry`, exposed as `sdk.skills`.
+- Direct Run start activates every configured Skill before creating a Run or
+  invoking the provider. Any Skill activation failure is normalized to
+  non-retryable `invalid_state`.
+- `PromptComposer` preserves Agent Skill order, rejects duplicate names, and
+  adds immutable `skill:<name>` layers whose version is the Skill content hash
+  and whose SHA-256 covers the activated instructions.
+- Added `PromptManifest.manifest_id`.
+- Added `PromptManifestPersistence`: atomically writes snapshot kind
+  `prompt_manifest` and a `prompt.manifest.created` event containing only
+  manifest/context/model/tool/layer identifiers and hashes.
+- Public package exports include `ContextRuntimeConfig` and
+  `PromptManifestPersistence`.
+
+## Final verification
+
+```text
+$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\integration\prompts tests\unit\runtime\test_execution_descriptors.py tests\unit\test_core_config.py tests\integration\skills\test_skill_slice.py tests\integration\test_sdk_sqlite_test_constructor.py -q
+89 passed, 1 skipped in 4.16s
+
+$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\context tests\integration\context -q
+102 passed in 3.62s
+
+$ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk tests\integration\prompts tests\unit\runtime\test_execution_descriptors.py tests\unit\test_core_config.py
+All checks passed!
+
+$ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk\context src\agent_sdk\context_runtime.py src\agent_sdk\runtime\models.py src\agent_sdk\runtime\execution.py src\agent_sdk\prompts src\agent_sdk\config.py src\agent_sdk\api.py src\agent_sdk\storage\sqlite.py
+Success: no issues found in 22 source files
+
+$ git diff --check
+clean (line-ending notices only)
+```
+
+The skipped test is the existing package-build test when `uv` is unavailable.
+
+The Task 3 integration tests explicitly prove:
+
+- ordered general/coding/application/Skill layers followed by Context
+  messages at the compose/persist seam;
+- full manifest snapshot provenance and SQLite reopen;
+- public manifest events contain no profile, application, or Skill
+  instruction text;
+- missing configured Skill yields `invalid_state`, creates no Run, and makes
+  zero provider calls;
+- discovery runs exactly once during SDK initialization.
+
+## Concern
+
+An extra, non-required run of
+`tests/integration/runtime/test_text_agent_loop.py` produced 1 failure and 32
+passes: its first legacy assertion expects `tools=[]`, while the current SDK
+default (already present at Task 3 base) enables the R1 built-in tools. The
+failure is unrelated to this Task 3 diff and was not changed.
+
+The actual RunEngine provider request is intentionally not rewired here.
+Task 4 owns ContextMiddleware, exact request persistence, per-call preparation,
+and recovery reuse.
diff --git a/.superpowers/sdd/v01-r3-task3-rereview.md b/.superpowers/sdd/v01-r3-task3-rereview.md
new file mode 100644
index 0000000..bc29311
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task3-rereview.md
@@ -0,0 +1,159 @@
+# v0.1 R3 Task 3 Fix Re-review
+
+Review range: `8f85363..7f33d89`
+
+Verdict: **CHANGES_REQUIRED**
+
+- Spec: **FAIL**
+- Quality: **FAIL**
+- Critical: **0**
+- Important: **1**
+- Minor: **1**
+
+The original C1 and I1 are closed on current schema-v2 runs. Task 3 is not yet
+approved because the claimed schema-v1 compatibility fails for a real R2
+descriptor shape.
+
+## Original finding closure
+
+### Original C1 — CLOSED
+
+The fix separates public creation evidence from private recovery state:
+
+- New `run.created` events use schema version 2 and the extra-forbid
+  `RunCreatedEventPayload`.
+- The public payload contains creation identity, ordinary user input, and
+  hashes only. It has no execution descriptor, system prompt, Skill/profile
+  instructions, model parameters, Tool specification, or Tool schema.
+- The authoritative Run snapshot and idempotency result still contain the full
+  descriptor required by recovery/replay.
+- Schema-v2 recovery and execution-tree assembly load that authoritative
+  snapshot and require the public payload to equal a freshly derived payload,
+  including descriptor, agent, Tool-capability, user-input, and task-envelope
+  hashes. A forged hash or cross-Session claim therefore does not authenticate.
+- The new sentinel integration test starts a real Run, proves private values
+  remain in the private snapshot, and scans every public event for application
+  prompt, model-parameter, Skill, packaged-profile, and Tool-schema markers.
+
+### Original I1 — CLOSED
+
+The production SDK now injects `SkillRegistry.validate_agent` into the shared
+`RuntimeCommands.start_run` boundary. Preflight runs before Session loading,
+idempotency handling, event/snapshot persistence, task creation, or provider
+execution. Direct, Workflow-node, and subagent paths all use that boundary.
+Activation failures are normalized to non-retryable `invalid_state`.
+
+Focused tests prove:
+
+- direct missing Skill: no `run.created`, zero provider calls;
+- Workflow missing Skill: the Workflow container may exist, but no node Run is
+  persisted and the provider is not called;
+- subagent missing Skill: no child Run is persisted and the provider is not
+  called.
+
+The optional no-op callback on the internal low-level `RuntimeCommands`
+constructor preserves existing internal/test callers; all production
+`AgentSDK` construction injects the validating boundary. This is a reasonable
+backward-compatible low-level default.
+
+## New findings
+
+### I1 — Genuine R2 schema-v1 descriptors fail recovery and execution-tree authentication
+
+`run_created_event_matches(..., schema_version=1)` reconstructs a current
+`RunSnapshot` from the already-loaded authoritative snapshot and compares its
+current serialized form byte-for-byte with the raw v1 event payload.
+
+That is incompatible with the Task 3 legacy-descriptor upgrader:
+
+1. R2 descriptors legitimately lack `prompt_profile`, `system_prompt`,
+   `skills`, and `context`.
+2. Loading the private Run snapshot validates the original legacy
+   `agent_hash`/`descriptor_hash`, adds those defaults, and canonically rehashes
+   the descriptor in memory.
+3. The immutable v1 `run.created` event still contains the original R2
+   descriptor and hashes.
+4. The new helper compares that raw event to the upgraded serialization and
+   returns false.
+
+Fresh minimal reproduction:
+
+```text
+upgraded_fields= True
+v1_matches= False
+```
+
+This reaches all three claimed compatibility paths:
+
+- `RunRecoveryService._is_pristine_created`;
+- `RunRecoveryService._is_valid_run_event_envelope`;
+- `QueryService._assemble_tree_unchecked`.
+
+After SQLite reopen, the private snapshot is normalized by Pydantic while the
+stored event remains raw, so a valid historical Run can no longer be recovered
+or placed in an execution tree. Context projection still reads its user input,
+but that does not repair recovery/tree compatibility.
+
+Required fix:
+
+- For schema v1, validate/authenticate the raw event in its historical shape,
+  including its original descriptor hashes, then compare normalized semantic
+  creation state after applying the same safe legacy upgrade to both sides.
+- Continue rejecting malformed fields, wrong original hashes, mismatched
+  Run/Session/parent/workflow identity, and cross-Session event/snapshot pairs.
+- Add SQLite-reopen recovery and execution-tree tests seeded with a genuine R2
+  descriptor missing all four Task 3 fields. Include wrong-hash and
+  cross-Session negative cases.
+- Keep schema-v2 exact-shape comparison unchanged.
+
+### M1 — Diff-check is not clean
+
+Fresh `git diff --check 8f85363..7f33d89` reports:
+
+```text
+.superpowers/sdd/v01-r3-task3-fix-report.md:87: new blank line at EOF.
+```
+
+Remove the extra blank line before the next review.
+
+## Compatibility and scope notes
+
+- Current schema-v2 Run creation, recovery authentication, execution-tree
+  assembly, Context user-input projection, and same-Session checks are
+  internally consistent.
+- The v2 event shape is explicit and minimal for the stated public Trace
+  boundary; private snapshot/idempotency evidence remains complete.
+- The change does not wire Task 4 Context middleware or alter model-call
+  preparation.
+- Existing schema-v1 events using a current/full descriptor remain readable;
+  the failure is specifically the real pre-Task3 descriptor shape that the fix
+  report claims to support.
+
+## Fresh verification
+
+```text
+pytest tests/integration/prompts/test_runtime_prompt.py
+6 passed in 4.04s
+
+pytest tests/integration/prompts
+       tests/integration/observability/test_queries.py
+       tests/unit/context tests/integration/context
+       tests/integration/runtime/test_provider_recovery_execution.py
+269 passed, 1 skipped in 14.88s
+
+pytest tests/integration/workflow/test_workflow_recovery.py
+       tests/integration/workflow/test_workflow_child_slice.py
+       tests/faults/test_subprocess_recovery.py
+28 passed in 24.31s
+
+ruff check src/agent_sdk tests/integration/prompts/test_runtime_prompt.py
+All checks passed!
+
+mypy --strict src/agent_sdk
+Success: no issues found in 92 source files
+
+git diff --check 8f85363..7f33d89
+FAILED: extra blank line at EOF in the fix report
+```
+
+The skipped test is the existing package-build check when `uv` is unavailable.
diff --git a/.superpowers/sdd/v01-r3-task3-review.md b/.superpowers/sdd/v01-r3-task3-review.md
new file mode 100644
index 0000000..af029c8
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task3-review.md
@@ -0,0 +1,175 @@
+# v0.1 R3 Task 3 Independent Review
+
+Review range: `794739f..f80a956`
+
+Verdict: **CHANGES_REQUIRED**
+
+- Spec: **FAIL**
+- Quality: **FAIL**
+- Critical: **1**
+- Important: **1**
+- Minor: **0**
+
+Task 3 is not approved. The focused implementation is otherwise coherent, but
+the two findings below violate explicit R3 contracts and must be fixed and
+independently re-reviewed before Task 4 proceeds.
+
+## Findings
+
+### C1 — Raw application prompt is exposed through the public Trace event
+
+Evidence:
+
+- `AgentSpec.system_prompt` is copied into `DurableAgentSpec`
+  (`src/agent_sdk/runtime/models.py`, `src/agent_sdk/runtime/execution.py`).
+- `RuntimeCommands.start_run` serializes the entire `RunSnapshot` as
+  `run_data`, then publishes that object unchanged as the payload of the public
+  `run.created` event (`src/agent_sdk/runtime/commands.py:529-565`).
+- Therefore the new durable descriptor places the raw application system
+  prompt at
+  `run.created.payload.execution_descriptor.agent.system_prompt`. The same
+  public payload also continues to contain full Tool capability/spec/schema
+  objects rather than only their hashes.
+- A fresh in-memory SDK reproduction using
+  `system_prompt="SECRET_SYSTEM_PROMPT"` printed:
+
+  ```text
+  secret_in_run_created= True
+  path_value= SECRET_SYSTEM_PROMPT
+  ```
+
+- `tests/integration/prompts/test_runtime_prompt.py` only searches the
+  `prompt.manifest.created` payload. It does not inspect all public events, so
+  it misses the actual leak.
+
+Impact:
+
+This directly violates the global R3 constraint that public Trace events
+contain ids/hashes rather than raw prompt text, and the Task 3 requirement that
+raw prompt/profile/Skill/Tool-schema material be absent from public Trace
+payloads. Application system prompts commonly contain confidential operating
+instructions or credentials, so this is a data-exposure boundary rather than a
+cosmetic trace-shape issue.
+
+Required direction:
+
+- Keep recovery-required private data in durable snapshots/references, but
+  publish a redacted/hash-only `run.created` projection (or a versioned
+  equivalent that cannot expose prompt or Tool-schema bodies).
+- Add a sentinel-based integration test that starts a real Run and scans
+  **every** public event payload for application prompt text, packaged profile
+  text, activated Skill instructions, and distinctive Tool-schema content.
+- Preserve current recovery and SQLite compatibility while changing the public
+  event shape.
+
+### I1 — Skill preflight protects only direct Runs; Workflow/child Runs bypass it
+
+Evidence:
+
+- The new preflight is private to `RunAPI.start`
+  (`src/agent_sdk/api.py:518-567`).
+- `WorkflowExecutor` resolves an `AgentSpec`, builds its descriptor, and calls
+  `RuntimeCommands.start_run` directly
+  (`src/agent_sdk/workflow/executor.py:580-640`, `:830-868`).
+- `SubagentService.spawn` likewise calls `RuntimeCommands.start_run` directly
+  (`src/agent_sdk/subagents/service.py:53-106`).
+- Neither path receives the initialized `SkillRegistry` or activates configured
+  Skills before durable writes/model execution.
+- A fresh one-node Workflow reproduction registered
+  `skills=("missing-skill",)` and produced:
+
+  ```text
+  provider_calls= 1
+  run_created_count= 1
+  workflow_started_count= 1
+  ```
+
+Impact:
+
+The same Agent specification has different validity depending on which public
+execution path starts it. A missing or changed Skill is correctly rejected
+before persistence for `sdk.runs.start`, but is silently ignored by Workflow
+and child execution, allowing a persisted Run and a provider call. This
+violates the explicit Task 3 fail-before-Run/model contract and will make Task
+4 prompt behavior path-dependent.
+
+Required direction:
+
+- Centralize configured-Skill activation/preflight and use it for direct,
+  Workflow, and child Run creation.
+- Workflow start must validate every referenced Agent before its node Run can
+  be persisted or its provider called; child spawn must validate before
+  `start_run`.
+- Add direct/Workflow/child tests proving missing and invalidated Skills yield
+  normalized non-retryable `invalid_state`, zero provider calls, and no
+  `run.created` event.
+
+## Confirmed conforming behavior
+
+- `AgentSpec` and `DurableAgentSpec` have the planned defaults, profile type,
+  ordered unique nonempty Skill validation, and `ContextRuntimeConfig`.
+- New prompt/Skill/context fields participate in `agent_hash` and
+  `descriptor_hash`.
+- Legacy descriptors missing the new fields are accepted only after their raw
+  legacy agent and descriptor hashes validate, then upgraded to declared
+  defaults and canonically rehashed. Invalid/noncanonical values remain
+  rejected.
+- Moving pure Context runtime configuration to
+  `agent_sdk.context_runtime` avoids the runtime/context import cycle. The
+  complete Context regression gate remained green.
+- `AgentSDKConfig.skill_roots` round-trips; SDK initialization constructs and
+  discovers one registry and exposes it as `sdk.skills`.
+- For the direct Run path, missing Skill activation occurs before
+  `RuntimeCommands.start_run`, creates no Run, and invokes no provider.
+- `PromptComposer` orders general, coding, application, then Skill layers;
+  preserves Skill order; rejects duplicate Skill names; uses Skill
+  `content_hash` as the layer version; hashes instruction text; canonicalizes
+  Tool-schema hashing; and returns frozen prompt messages without mutating
+  inputs.
+- `PromptManifest` has a generated `manifest_id`. Persistence atomically writes
+  the full manifest snapshot and the minimal manifest event, with Session and
+  same-Session Context View preconditions.
+- SQLite recognizes the new snapshot kind, requires the referenced Context
+  View to belong to the same Session, survives reopen, and existing
+  Session-scoped deletion removes its event/snapshot data.
+- Public root exports for `ContextRuntimeConfig` and
+  `PromptManifestPersistence` are present.
+- Task 3 does not prematurely wire Context preparation into `RunEngine`; that
+  remains Task 4 scope.
+- The implementation report's text-loop concern is not caused by this diff:
+  the reviewed range does not change the existing built-in Tool default or the
+  legacy provider request construction.
+
+## Fresh verification
+
+```text
+pytest tests/integration/prompts
+       tests/unit/runtime/test_execution_descriptors.py
+       tests/unit/test_core_config.py
+       tests/integration/skills/test_skill_slice.py
+       tests/integration/test_sdk_sqlite_test_constructor.py
+89 passed, 1 skipped in 4.81s
+
+pytest tests/unit/context tests/integration/context
+102 passed in 3.75s
+
+ruff check src/agent_sdk tests/integration/prompts
+           tests/unit/runtime/test_execution_descriptors.py
+           tests/unit/test_core_config.py
+All checks passed!
+
+mypy --strict src/agent_sdk/context src/agent_sdk/context_runtime.py
+              src/agent_sdk/runtime/models.py
+              src/agent_sdk/runtime/execution.py src/agent_sdk/prompts
+              src/agent_sdk/config.py src/agent_sdk/api.py
+              src/agent_sdk/storage/sqlite.py
+Success: no issues found in 22 source files
+
+git diff --check 794739f..f80a956
+clean
+```
+
+The skipped test is the existing package-build check when `uv` is unavailable.
+A wider runtime run was not used as passing evidence because the controller
+reported it exceeded its 120-second environment window; it had emitted no
+failure before termination.
diff --git a/.superpowers/sdd/v01-r3-task3-transition-review.md b/.superpowers/sdd/v01-r3-task3-transition-review.md
new file mode 100644
index 0000000..7e820c0
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task3-transition-review.md
@@ -0,0 +1,48 @@
+# v0.1 R3 Task 3 Transition Review
+
+Review range: `c94ea77..83a8b4d`
+
+Verdict: **APPROVED**
+
+- Spec: **PASS**
+- Quality: **PASS**
+- Critical: **0**
+- Important: **0**
+- Minor: **0**
+
+## Transition facts
+
+- The range changes exactly the three declared transition files:
+  `.superpowers/sdd/progress.md`, `docs/plans/releases/v0.1.md`, and
+  `tests/docs/test_v01_release_ledger.py`.
+- Both operational records mark R3 Task 3 complete and identify final
+  implementation/fix checkpoint `774ae6c` and final approval `c94ea77`.
+- The recorded Task 3 approval is Critical 0 / Important 0 / Minor 0, Spec
+  PASS, and Quality PASS. The retained implementation evidence
+  (`521 passed, 1 skipped`; the 25-test Workflow/recovery/release gate; Ruff;
+  strict mypy across 92 source files) agrees with the Task 3 fix and approval
+  records.
+- R3 remains `in_progress`; Tasks 1-3 are complete and Task 4 remains pending.
+  R4 and R5 remain pending.
+- The sole active next action is R3 Task 4 Step 1. The command names both
+  planned Task 4 integration files and matches Step 3 of
+  `docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md`.
+- The former Task 3 next-action/first-command recovery point is absent.
+- The ledger test is strengthened to require Task 3 completion evidence, the
+  Task 4 recovery files and command, and removal of the former Task 3 recovery
+  point; no prior R0-R2 assertion was removed.
+
+## Fresh verification
+
+```text
+pytest -p pytest_asyncio.plugin tests/docs -q
+2 passed in 0.01s
+
+ruff check tests/docs/test_v01_release_ledger.py
+All checks passed!
+
+git diff --check c94ea77..83a8b4d
+clean
+```
+
+The transition is ready to proceed to R3 Task 4.
diff --git a/.superpowers/sdd/v01-r3-task4-fix-report.md b/.superpowers/sdd/v01-r3-task4-fix-report.md
new file mode 100644
index 0000000..6442b20
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task4-fix-report.md
@@ -0,0 +1,270 @@
+# v0.1 R3 Task 4 Review Fix Report
+
+## Scope
+
+This change fixes both Important findings from
+`v01-r3-task4-review.md` (review commit `4d0bb5b`) without changing release or
+progress documents:
+
+- I1: strictly validate every durable prepared provider message and Tool schema.
+- I2: authenticate durable Context View and Prompt Manifest references before
+  recovery trusts or executes a prepared model request.
+
+It also closes the prepare-to-start race by requiring both prepared snapshots
+to exist and belong to the Run Session in the same commit that records
+`model.call.started`.
+
+## I1 - Closed prepared-request protocol
+
+### RED
+
+The new parameterized negative test exercised 17 malformed request cases:
+
+- missing, empty, or invalid message roles;
+- role-specific missing fields;
+- invalid Tool-result messages;
+- empty or malformed assistant Tool calls;
+- unknown Tool-call fields;
+- empty/malformed provider Tool schemas;
+- non-JSON nested values.
+
+Before the fix, the focused run produced:
+
+```text
+16 failed, 2 passed, 47 deselected
+```
+
+The already-existing recursive JSON freeze rejected one nested case, and the
+positive runtime-shape case passed. The other 16 malformed protocol shapes
+were accepted.
+
+### GREEN
+
+`_ModelRequestPayload` now applies closed, role-specific validators:
+
+- roles are limited to `system`, `user`, `assistant`, and `tool`;
+- every role has an exact allowed field set;
+- Tool-result messages require a non-empty `tool_call_id`;
+- assistant Tool calls have the exact provider function-call shape;
+- provider Tool entries have the exact `{"type": "function", "function": ...}`
+  envelope and require a non-empty name plus mapping parameters;
+- the existing recursive freeze still rejects non-string keys, non-finite
+  numbers, and other non-JSON values.
+
+The positive test covers every message shape emitted by the runtime, optional
+message names, assistant function calls, Tool results, and the provider Tool
+schema. The focused result after the fix was:
+
+```text
+18 passed, 47 deselected
+```
+
+Legacy operations remain loadable when `prepared_request`,
+`context_view_id`, and `prompt_manifest_id` are all absent.
+
+## I2 - Authenticated Context View and Prompt Manifest references
+
+### RED
+
+The corruption matrix covers both Memory and SQLite backends for:
+
+- missing Context View;
+- missing Prompt Manifest;
+- cross-Session Context View ownership;
+- cross-Session Prompt Manifest ownership;
+- mismatched Context View identity;
+- mismatched Prompt Manifest identity;
+- Manifest linked to a different Context View.
+
+Before reference authentication, all 14 cases failed because recovery did not
+raise:
+
+```text
+14 failed
+```
+
+A separate completed-model crash test proved that recovery did not read the
+old completed model's Context View or Prompt Manifest.
+
+The completed-model/Tool-in-flight corruption variant then exposed a second
+boundary: the compatibility path converted invalid old references into a
+reconciliation plan instead of failing closed:
+
+```text
+1 failed, 1 passed
+```
+
+### GREEN
+
+For every `ModelCallOperation` with a prepared request, recovery now:
+
+1. verifies the operation belongs to the recovered Run and Session;
+2. loads the referenced Context View and Prompt Manifest;
+3. validates their durable model shapes;
+4. verifies View identity and Session identity;
+5. verifies Manifest identity, its View link, and its provider model identity;
+6. executes a no-write atomic commit with exact snapshot-data and Session-owner
+   preconditions.
+
+The exact-data preconditions detect a replacement between the snapshot reads
+and the atomic authentication check. Existing closed-world model-event
+certification also requires the public `model.call.started` payload to carry
+the same authenticated pair. Authentication occurs:
+
+- at the recovery planning boundary, before compatibility fallback;
+- when reconstructing a prepared request;
+- when validating pending reconciliation requests.
+
+Provider and Tool side-effect counters remain zero for every rejected
+corruption. The Memory/SQLite matrix plus completed-model positive recovery
+passed:
+
+```text
+15 passed
+```
+
+After moving authentication ahead of the Tool-in-flight compatibility path,
+the focused positive/corruption pair passed:
+
+```text
+2 passed
+```
+
+### Completed-model crash/recovery evidence
+
+The positive recovery test completes the first model call, crashes while its
+safe-retry Tool is in flight, scans the interrupted Run, and recovers it. It
+asserts:
+
+- the completed model operation id and its two reference ids remain unchanged;
+- recovery reads and authenticates the old View and Manifest;
+- the recovered Tool executes exactly once;
+- the following new model call executes exactly once;
+- the following call receives a different View and Manifest;
+- durable event counts move from exactly one View/Manifest pair to exactly two.
+
+Thus recovery creates no duplicate evidence for the completed call and exactly
+one fresh pair for the subsequent call.
+
+## Prepare-to-start race
+
+### RED
+
+A middleware test deletes the just-created Context View immediately after the
+Prompt Manifest commit. Before the fix, model start proceeded:
+
+```text
+Failed: DID NOT RAISE
+```
+
+### GREEN
+
+`start_model` now includes Session-owned `SnapshotPrecondition`s for the
+prepared Context View and Prompt Manifest in the same progress commit that
+records the model operation and public start event. The provider is not called,
+and neither `model.call.started` nor a model operation is persisted when either
+snapshot is missing. The combined focused suite passed:
+
+```text
+85 passed
+```
+
+## Fresh verification
+
+All commands were run from
+`D:\code\AgentSDK\.worktrees\agent-sdk-implementation`.
+
+### Task 4, Context, Prompt, and release E2E
+
+```powershell
+$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
+.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin `
+  tests\unit\context `
+  tests\unit\runtime\test_reconciliation_models.py `
+  tests\integration\context `
+  tests\integration\prompts `
+  tests\e2e\test_v01_release.py -q
+```
+
+```text
+221 passed, 1 skipped in 23.19s
+```
+
+### Provider, Tool, text-loop, and recovery regression
+
+```powershell
+$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
+.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin `
+  tests\unit\runtime\test_provider_recovery.py `
+  tests\integration\runtime\test_text_agent_loop.py `
+  tests\integration\runtime\test_provider_recovery_execution.py `
+  tests\integration\runtime\test_tool_recovery_execution.py -q
+```
+
+```text
+294 passed in 73.92s
+```
+
+### Task 3 security and legacy compatibility
+
+```powershell
+$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
+.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin `
+  tests\integration\prompts\test_runtime_prompt.py `
+  tests\unit\runtime\test_execution_descriptors.py -q
+```
+
+```text
+48 passed in 14.08s
+```
+
+### Workflow and subagent smoke
+
+```powershell
+$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
+.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin `
+  tests\integration\subagents\test_child_run_slice.py `
+  tests\integration\workflow\test_control_child_parent.py `
+  tests\integration\workflow\test_workflow_child_slice.py `
+  tests\integration\workflow\test_workflow_recovery.py -q
+```
+
+```text
+38 passed in 15.64s
+```
+
+### Static quality
+
+```powershell
+.\.venv\Scripts\python.exe -m ruff check src `
+  tests\unit\runtime\test_reconciliation_models.py `
+  tests\integration\context\test_context_recovery.py `
+  tests\integration\context\test_runtime_middleware.py
+```
+
+```text
+All checks passed!
+```
+
+```powershell
+.\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk
+```
+
+```text
+Success: no issues found in 93 source files
+```
+
+```powershell
+git diff --check
+```
+
+```text
+clean
+```
+
+## Remaining project-level concern
+
+The independent review documented a pre-existing built-in-Tool capability
+mismatch in `tests/integration/runtime/test_recovery_api.py`. This Task 4 fix
+does not modify that capability gate and does not present the repository-wide
+suite as fully green. It remains release-suite debt outside this review fix.
diff --git a/.superpowers/sdd/v01-r3-task4-report.md b/.superpowers/sdd/v01-r3-task4-report.md
new file mode 100644
index 0000000..57df3b1
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task4-report.md
@@ -0,0 +1,144 @@
+# v0.1 R3 Task 4 Report
+
+## Status
+
+PASS. Every new model call now prepares and persists its Context View and Prompt
+Manifest before provider execution. In-flight model recovery uses the exact
+stored prepared request, while legacy operations retain the historical
+reconstruction path.
+
+## Scope
+
+- Added the runtime Context middleware and checkpoint-aware Context planning.
+- Wired the middleware once at the new-model-call boundary.
+- Added strict canonical prepared-request persistence and fingerprints.
+- Updated every certified recovery/history validator to understand both prepared
+  and legacy model operations.
+- Added integration and release acceptance coverage.
+- Did not modify the R3 release ledger or progress files; those remain Task 5.
+
+## TDD evidence
+
+The initial Task 4 integration tests failed as expected:
+
+- no Context View existed before provider execution;
+- `ModelCallOperation` had no prepared-request identity.
+
+After the implementation, the focused middleware/recovery gate produced:
+
+```text
+4 passed
+```
+
+Additional recovery work found remaining legacy-only event/request comparisons.
+The full Tool recovery gate initially produced `130 passed, 3 failed`. Two
+failures were prepared-request compatibility gaps and were fixed by routing all
+certified history checks through the shared request/payload helpers. The third
+test deliberately corrupted a prepared request fingerprint. The strict persisted
+model now rejects that corruption earlier as `recovery state conflict`; the test
+was migrated while preserving fail-closed, zero-provider-call, zero-tool-call,
+and no-secret-leak assertions. Final result:
+
+```text
+133 passed
+```
+
+## Implementation
+
+### Context before each new model call
+
+`ContextMiddleware.prepare`:
+
+1. plans from the durable Session event stream and ordinary Run checkpoint;
+2. automatically recommends/applies L0-L4 through the configured policy;
+3. activates pinned Skills;
+4. composes the default, application, and Skill system layers;
+5. persists the Prompt Manifest;
+6. returns the exact provider message sequence.
+
+The Run checkpoint continues to contain only ordinary user, assistant, and Tool
+ledger messages. Recovered completed or in-flight model operations do not invoke
+the middleware again.
+
+### Durable exact request
+
+Prepared model operations persist:
+
+- `context_view_id`;
+- `prompt_manifest_id`;
+- the canonical model request;
+- the SHA-256 fingerprint of that exact canonical request.
+
+The public `model.call.started` event contains only the model, durable reference
+ids, and fingerprint. It does not contain system prompts, Skill instructions,
+context text, Tool schemas, or model parameters.
+
+Canonical request parsing is strict and fail-closed for extra fields, malformed
+sequences, non-finite JSON numbers, incomplete references, and fingerprint
+mismatches. Legacy operation JSON without the new optional fields still loads.
+
+### Recovery
+
+Provider recovery, reconciliation, terminal certification, safe checkpoint
+certification, Tool history certification, and historical replay validation all
+use the exact stored request when present. The descriptor/checkpoint
+reconstruction path remains available only for legacy operations.
+
+An authoritative provider recovery adapter receives the exact stored request.
+A crash after `model.call.started` creates no duplicate Context View, capsule,
+Prompt Manifest, or model operation.
+
+## L0-L4 acceptance
+
+The release acceptance scenario runs six real SDK Runs in one Session with a
+small model window and deterministic token estimates. It proves:
+
+- automatic recommendations `L0, L1, L2, L3, L3, L4`;
+- applied levels `L0, L1, L2, L2, L3, L4`;
+- an invalid first L3 response persists an L3-to-L2 fallback and the Run still
+  completes;
+- later valid L3 and L4 compactions persist capsules;
+- the first Run's original source event remains queryable through the final L4
+  capsule;
+- the final Prompt Manifest contains exactly
+  `profile:general`, `application`, and `skill:demo`.
+
+## Verification
+
+```text
+R3 core:
+186 passed, 1 skipped
+
+Provider, Tool, text-loop, and recovery:
+294 passed
+
+Task 3 public-event, Skill-preflight, and legacy SQLite compatibility:
+48 passed
+
+Workflow and subagent smoke:
+38 passed
+
+Release E2E:
+3 passed
+
+Ruff:
+All checks passed
+
+Strict mypy:
+Success: no issues found in 93 source files
+
+git diff --check:
+clean
+```
+
+The skipped test is the existing optional tokenizer-backend case.
+
+## Non-Task-4 failure classification
+
+`tests/integration/runtime/test_recovery_api.py` was sampled separately and its
+legacy seed helpers produced 115 failures because their stored execution
+descriptors declare no Tools while `AgentSDK.for_test` enables built-ins by
+default. The same default and recovery capability check predate Task 4. This
+suite is not a Task 4 gate, and the capability validation was intentionally not
+weakened. Dedicated provider and Tool recovery suites are fully green as
+recorded above.
diff --git a/.superpowers/sdd/v01-r3-task4-rereview.md b/.superpowers/sdd/v01-r3-task4-rereview.md
new file mode 100644
index 0000000..ca14073
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task4-rereview.md
@@ -0,0 +1,131 @@
+# v0.1 R3 Task 4 Fix Re-review
+
+## Verdict
+
+- Reviewed range: `4d0bb5b..79996db`
+- Spec: PASS
+- Quality: PASS
+- Critical: 0
+- Important: 0
+- Minor: 0
+- Approval: APPROVED
+
+Both Important findings in `v01-r3-task4-review.md` are closed. No new
+Critical, Important, or Minor finding was identified in the fix range.
+
+## I1 closure — strict prepared-request protocol
+
+Status: CLOSED.
+
+Independent code inspection confirmed:
+
+- every durable request requires at least one message;
+- roles are closed to `system`, `user`, `assistant`, and `tool`;
+- role-specific allowed and required fields are enforced;
+- ordinary assistant text, nullable assistant content paired with Tool calls,
+  exact assistant function Tool-call envelopes, and Tool-result correlation ids
+  match the shapes emitted by the runtime;
+- Tool definitions require the exact function envelope, a non-empty name, and
+  mapping parameters;
+- recursive canonical JSON validation still rejects non-string keys,
+  non-finite values, unsupported values, top-level extras, and alias mutation;
+- legacy operations remain valid when all three prepared-request fields are
+  absent.
+
+The positive test covers the four runtime roles, optional names, assistant Tool
+calls, Tool results, and a registered provider Tool schema. The negative matrix
+contains independent malformed variants rather than asserting implementation
+internals. Fresh focused execution passed all 86 Task 4 model/recovery tests.
+
+The durable SQLite loader's strict decoder and canonical-record comparison
+continue to reject duplicate JSON keys. Direct Pydantic parsing is not the
+supported persistence boundary and does not weaken that path.
+
+## I2 closure — authenticated Context and Prompt references
+
+Status: CLOSED.
+
+Independent code inspection confirmed:
+
+- prepared model operations authenticate their Run and Session ownership;
+- the Context View and Prompt Manifest snapshots must both exist and validate;
+- snapshot ids must equal the operation references;
+- the Context View must belong to the recovered Session;
+- the Prompt Manifest must link to that exact Context View and model;
+- an atomic no-write commit applies exact snapshot-data and Session-owner
+  preconditions, detecting replacement between read and authentication;
+- `start_model` adds Session-owned Context View and Prompt Manifest snapshot
+  preconditions to the same progress commit that records the operation and
+  public `model.call.started`;
+- legacy model operations without prepared fields do not require these
+  snapshots.
+
+Authentication is centralized at the recovery planning boundary, the validated
+request boundary, and pending-reconciliation loading. Thus provider recovery,
+resend, reconciliation resolution/replay, terminal certification, safe
+checkpoint certification, and completed model history all receive evidence
+that has passed the same authentication. Importantly, authentication precedes
+the Tool-in-flight compatibility fallback, so corrupt completed model evidence
+cannot be converted into a reconciliation request.
+
+The Memory/SQLite corruption matrix exercises missing snapshots, owner
+mismatch, internal id mismatch, and Manifest-to-View mismatch. It verifies
+recovery conflict with zero provider and Tool calls. These are real StateStore
+backends and service entry points, not mocked authentication results.
+
+The completed-model recovery test uses a real completed model followed by a
+cancelled safe-retry Tool. On reopen it proves:
+
+- the old model operation and references are preserved and read;
+- corrupt old evidence fails before the Tool or provider;
+- valid old evidence permits exactly one recovered Tool execution;
+- the subsequent new model call creates a different View and Manifest;
+- durable View/Manifest event counts increase from one pair to exactly two.
+
+This closes both the old-reference recovery gap and the missing
+crash-after-completed-call acceptance branch.
+
+## Regression and safety assessment
+
+- Public `model.call.started` and `prompt.manifest.created` shapes were not
+  expanded with raw messages, prompts, model parameters, or Tool schemas.
+- Existing closed-world event checks still require the public started-event
+  references and fingerprint to match the authenticated operation.
+- The Task 3 prompt, Skill preflight, and legacy run-created v1/v2 suite remains
+  green.
+- Workflow and subagent integration smoke remains green.
+- The fix does not add Task 5 release-ledger behavior or broaden R3 scope.
+- The previously documented `test_recovery_api.py` built-in-Tool capability
+  mismatch is unchanged and remains project-level release-suite debt, not a
+  Task 4 fix regression.
+
+## Fresh independent verification
+
+```text
+Task 4 focused model/recovery tests:
+86 passed
+
+R3 Context, Prompt, reconciliation, and release E2E:
+221 passed, 1 skipped
+
+Provider, Tool, text-loop, and recovery regressions:
+294 passed
+
+Task 3 compatibility plus Workflow/subagent smoke:
+86 passed
+
+Ruff:
+All checks passed
+
+Strict mypy:
+Success: no issues found in 93 source files
+
+git diff --check:
+clean
+
+worktree before review artifact:
+clean
+```
+
+The single skip is the existing optional tokenizer-backend test.
+
diff --git a/.superpowers/sdd/v01-r3-task4-review.md b/.superpowers/sdd/v01-r3-task4-review.md
new file mode 100644
index 0000000..223298f
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task4-review.md
@@ -0,0 +1,187 @@
+# v0.1 R3 Task 4 Independent Review
+
+## Verdict
+
+- Spec: FAIL
+- Quality: FAIL
+- Critical: 0
+- Important: 2
+- Minor: 0
+- Approval: BLOCKED until both Important findings are fixed and independently
+  re-reviewed.
+
+Reviewed range: `85f0e0e..2f2048c`.
+
+## Important findings
+
+### I1 — Stored prepared requests do not reject malformed message and Tool shapes
+
+The new `_ModelRequestPayload` validates only that `messages` and `tools` are
+sequences of mappings. It does not validate a provider-message shape or a Tool
+schema shape. Consequently all of the following deserialize successfully:
+
+```python
+{"model": "m", "messages": [{}], "tools": [], "params": {}, "purpose": "agent_loop"}
+{"model": "m", "messages": [{"role": "bogus", "content": "x"}], "tools": [], "params": {}, "purpose": "agent_loop"}
+{"model": "m", "messages": [{"role": "tool", "content": "x"}], "tools": [{}], "params": {}, "purpose": "agent_loop"}
+```
+
+Evidence:
+
+- `src/agent_sdk/runtime/reconciliation.py:133-162` declares arbitrary
+  `Mapping[str, Any]` entries without a shape validator.
+- `src/agent_sdk/runtime/reconciliation.py:186-211` reconstructs a
+  `ModelRequest` after only that shallow validation.
+- `tests/unit/runtime/test_reconciliation_models.py:137-181` covers an extra
+  top-level field, a non-sequence container, and NaN, but not malformed
+  message/Tool entries.
+
+The durable SQLite path does reject duplicate JSON keys and non-canonical
+records via strict decoding/canonical comparison; the unsupported direct
+`BaseModel.model_validate_json` behavior is therefore not a separate finding.
+Non-string keys, non-finite numbers, top-level extras, alias mutation, and exact
+canonical fingerprinting are otherwise covered.
+
+Impact: a malformed but canonical stored prepared request can pass operation
+validation and enter provider recovery/reconciliation. This violates Task 4's
+strict malformed-payload fail-closed requirement at the exact-recovery trust
+boundary.
+
+Required fix:
+
+- add a closed validator for every persisted message and Tool entry while
+  preserving the provider protocol shapes the runtime actually emits;
+- add negative tests for missing/invalid roles, invalid Tool protocol fields,
+  malformed Tool schema entries, and nested non-string/non-finite values;
+- retain legacy operation loading when all three new fields are absent.
+
+### I2 — Context View and Prompt Manifest references are not authenticated during recovery
+
+`ModelCallOperation` requires the three new fields to be all present or all
+absent, but it validates only that both ids are non-empty. The request
+fingerprint covers the exact prepared model request, not either reference.
+Recovery uses the stored request and validates model, Tools, params, purpose,
+and fingerprint, but never loads the referenced snapshots to prove:
+
+- the Context View exists and belongs to the operation Session;
+- the Prompt Manifest exists and belongs to the operation Session;
+- the Manifest's `context_view_id` equals the operation's `context_view_id`;
+- the public `model.call.started` provenance references the same authenticated
+  pair.
+
+Evidence:
+
+- `src/agent_sdk/runtime/reconciliation.py:285-343` performs all-or-none,
+  non-empty, model, and request-fingerprint checks only.
+- `src/agent_sdk/runtime/recovery.py:4392-4441` reconstructs and authenticates
+  the request but never resolves either reference.
+- There are no Context View / Prompt Manifest lookups in
+  `src/agent_sdk/runtime/recovery.py`.
+- `tests/integration/context/test_context_recovery.py` proves stable ids and an
+  exact stored request, but has no missing, cross-Session, swapped-Manifest, or
+  mismatched-View corruption case.
+
+Impact: exact provider execution can be recovered, but its public provenance
+can be reassigned to unrelated durable context/prompt evidence while all
+current operation and event checks still pass. That makes the trace attribution
+untrustworthy, contrary to Task 4's id-binding requirement.
+
+Required fix:
+
+- authenticate the referenced View and Manifest, their Session ownership, and
+  the Manifest-to-View link before any prepared-request recovery,
+  reconciliation, resend, or terminal certification;
+- fail closed without a provider or Tool call on missing/cross-owner/mismatched
+  references;
+- add both memory and SQLite corruption tests;
+- add the planned crash-after-completed-model recovery test and assert that the
+  subsequent new model call creates exactly one new View and Manifest while
+  the completed call creates neither duplicate.
+
+## Requirements evidence
+
+### Runtime middleware and protocol
+
+- `ContextMiddleware.prepare` plans from the durable checkpoint, composes
+  prompt layers, persists the Manifest, and returns detached messages.
+- `RunEngine._execute_owned` invokes it only in the new-model branch immediately
+  before `start_model`; recovered completed model results bypass it.
+- The two-call Tool integration test proves two distinct Views, View-before-
+  model ordering, Tool-result consumption by the second request, and a clean
+  user/assistant/Tool checkpoint.
+- Normal completed-call progression proves a new View on the following call,
+  but the plan's corresponding crash/recovery branch still lacks the explicit
+  assertion required under I2.
+
+### Context levels and evidence
+
+- The runtime planner applies automatic L0-L4 selection, `allow_lossy` capping,
+  L3/L4 fallback to deterministic L2, over-budget events, and protected/current
+  retention through the Task 1/2 implementation.
+- Source extraction is Session-filtered and excludes current-Run event copies
+  before appending checkpoint messages, preventing the reviewed same-Session
+  duplicate path.
+- Capsule retrieval remains Session-scoped and recursive.
+
+### Prompt and public-event safety
+
+- The Prompt Manifest is persisted before `model.call.started`.
+- Public `prompt.manifest.created` and `model.call.started` payloads contain
+  ids/hashes/model metadata only; no raw system prompt, prepared messages,
+  model params, or Tool schemas were added.
+- Prepared request snapshots are frozen and detached; canonical fingerprints
+  include model, messages, Tools, params, and purpose.
+- Task 3 legacy run-created compatibility tests remain green.
+
+### Recovery compatibility
+
+- Prepared operations use the exact stored request; legacy operations without
+  the three fields retain descriptor/checkpoint reconstruction.
+- Provider authoritative recovery receives the exact request.
+- Closed-world model/Tool history validators use the new public started payload
+  shape and prepared-first request reconstruction.
+- In-flight recovery does not create a second View, capsule, Manifest, or model
+  operation.
+- The known prepare-before-start orphan window remains the documented v0.1
+  limitation and does not weaken exact recovery after `model.call.started`.
+
+### Acceptance scope
+
+- The E2E scenario drives recommendations L0, L1, L2, L3, L3, L4 and applied
+  levels L0, L1, L2, L2, L3, L4.
+- It proves invalid L3 fallback, valid L3/L4 capsules, recursive evidence back
+  to the first Run, and final general/application/Skill Manifest layers.
+- No Task 5 release-ledger implementation is present in the reviewed range.
+
+## Fresh verification
+
+```text
+Task 4 focused + reconciliation + release E2E:
+54 passed
+
+Context and prompt suites:
+136 passed, 1 skipped
+
+Provider, Tool recovery, and text loop:
+168 passed
+
+Workflow/subagent integration smoke:
+21 passed
+
+Ruff:
+All checks passed
+
+Strict mypy:
+Success: no issues found in 22 source files
+
+git diff --check:
+clean
+```
+
+The sampled `tests/integration/runtime/test_recovery_api.py` still has the
+pre-existing built-in-Tool capability mismatch described in the Task 4 report
+(`5 passed` before the first three failures under `--maxfail=3`). The Task 4
+diff does not change the capability gate that raises those failures, so it is
+not counted as a Task 4 finding. It remains a project-level release-suite debt
+that Task 5 must not silently present as a fully green repository.
+
diff --git a/.superpowers/sdd/v01-r3-task5-report.md b/.superpowers/sdd/v01-r3-task5-report.md
new file mode 100644
index 0000000..dcaba98
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task5-report.md
@@ -0,0 +1,66 @@
+# v0.1 R3 Task 5 Checkpoint Report
+
+## Scope
+
+This checkpoint advances the durable v0.1 ledger from R3 in progress to R3
+complete. It records no production-code change and does not start R4.
+
+## Approval history verified
+
+- Task 1 final transition review: Critical 0 / Important 0 / Minor 0; Spec
+  PASS; Quality PASS.
+- Task 2 transition re-review: Critical 0 / Important 0 / Minor 0; Spec PASS;
+  Quality PASS.
+- Task 3 transition review: Critical 0 / Important 0 / Minor 0; Spec PASS;
+  Quality PASS.
+- Task 4 implementation: `2f2048c`; recovery-evidence fix: `79996db`; final
+  approval: `ab1d082`. The final re-review reports Critical 0 / Important 0 /
+  Minor 0; Spec PASS; Quality PASS.
+
+## Ledger facts
+
+- R3 is complete and R4 is pending.
+- The Task 5 fresh R3 checkpoint evidence is 221 passed, 1 skipped in 25.32s;
+  Ruff clean; strict mypy clean across 93
+  source files.
+- The next plan is
+  `docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r4-child-mailbox.md`.
+- The resume command targets `tests/unit/runtime/test_capability_intersection.py`.
+  R4 Task 1 creates that file, so its first execution is intentionally expected
+  to be RED; its absence is not an R3 checkpoint failure. The mailbox test is
+  introduced by R4 Task 2.
+
+## Checkpoint correction
+
+The independent Task 5 review found that the original handoff skipped R4 Task
+1 by naming the later mailbox test, and that it attributed an unsupported
+duration to the Task 4 approval. The durable ledgers and their
+executable contract now use the Task 1 capability-intersection test and the
+actual Task 5 checkpoint result of 25.32s.
+
+## Fresh Task 5 verification
+
+```text
+$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\context tests\integration\context tests\integration\prompts tests\unit\runtime\test_reconciliation_models.py tests\e2e\test_v01_release.py -q
+221 passed, 1 skipped in 25.32s
+
+$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\docs\test_v01_release_ledger.py -q
+2 passed in 0.01s
+
+$ .\.venv\Scripts\python.exe -m ruff check src tests\unit\context tests\integration\context tests\integration\prompts tests\unit\runtime\test_reconciliation_models.py tests\e2e\test_v01_release.py tests\docs\test_v01_release_ledger.py
+All checks passed!
+
+$ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk
+Success: no issues found in 93 source files
+
+$ git diff --check
+clean
+```
+
+## Concern carried forward
+
+`tests/integration/runtime/test_recovery_api.py` has the pre-existing
+built-in-Tool capability mismatch documented by the Task 4 re-review. It is
+outside the R3 checkpoint command, unchanged by this checkpoint, and remains
+release-suite debt for subsequent release hardening rather than a claim that
+the entire repository suite is green.
diff --git a/.superpowers/sdd/v01-r3-task5-rereview.md b/.superpowers/sdd/v01-r3-task5-rereview.md
new file mode 100644
index 0000000..a6ec9bb
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task5-rereview.md
@@ -0,0 +1,99 @@
+# v0.1 R3 Task 5 Checkpoint Re-review
+
+## Verdict
+
+- Reviewed fix commit: `1e44ee0`
+- Reviewed range: `66107cb..1e44ee0`
+- Spec: **PASS**
+- Quality: **PASS**
+- Critical: **0**
+- Important: **0**
+- Minor: **0**
+- Summary: **C0 / I0 / M0**
+- Approval: **APPROVED**
+
+The fix closes both findings from
+`.superpowers/sdd/v01-r3-task5-review.md`. No new Critical, Important, or Minor
+finding was identified in the documentation-only fix range.
+
+## I1 closure - exact R4 Task 1 handoff
+
+Status: **CLOSED**.
+
+- The active next plan remains
+  `docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r4-child-mailbox.md`.
+- That plan defines Task 1 as capability selection and creates
+  `tests/unit/runtime/test_capability_intersection.py` before its focused RED.
+- The release ledger, progress record, and Task 5 report now use that exact
+  Task 1 test in the PowerShell resume command.
+- The records explicitly say Task 1 creates the file and its first execution is
+  expected RED; the file is correctly absent at the R3 checkpoint.
+- The Task 5 report identifies `tests/unit/subagents/test_mailbox.py` as an R4
+  Task 2 test, and neither durable ledger uses it as the first R4 command.
+- The executable ledger contract requires the Task 1 path and command, requires
+  the Task 1 / expected-RED markers, rejects the mailbox path from the durable
+  ledgers, and retains the old R3 Task 4 handoff absence guards.
+
+The corrected resume command is syntactically valid and agrees with R4 Task 1
+Step 2 after that task creates its test file. It no longer skips the capability
+work required before mailbox/control behavior.
+
+## M1 closure - checkpoint evidence provenance
+
+Status: **CLOSED**.
+
+- The release ledger, progress record, and Task 5 report now consistently use
+  `221 passed, 1 skipped in 25.32s` as the canonical Task 5 fresh checkpoint
+  result.
+- `25.32s` is the exact duration retained in Task 5's original fresh-
+  verification block.
+- The unsupported `13.65s` duration and its incorrect Task 4 re-review
+  attribution are absent from both durable ledgers and the Task 5 report.
+- The executable ledger contract requires the canonical Task 5 result and
+  rejects `13.65s`.
+
+A later rerun may naturally have a different duration; it corroborates the
+stable pass/skip count and does not replace the recorded original evidence.
+
+## Consistency and regression review
+
+- The range changes only `docs/plans/releases/v0.1.md`,
+  `.superpowers/sdd/progress.md`, `.superpowers/sdd/v01-r3-task5-report.md`, and
+  `tests/docs/test_v01_release_ledger.py`.
+- R3 remains complete; R4 and R5 remain pending. The fix does not start R4 or
+  mark v0.1 released.
+- The three checkpoint documents agree on status, next plan, resume command,
+  Task 1 test ownership, expected-RED intent, and canonical Task 5 evidence.
+- The old mailbox-first handoff is absent from both durable records. The old R3
+  Task 4 paths and command remain absent.
+- The pre-existing `tests/integration/runtime/test_recovery_api.py` debt remains
+  disclosed and is not presented as resolved.
+- The docs test retains the earlier R0-R3 history, approval, evidence, and stale
+  marker assertions while adding exact guards for both review fixes.
+
+## Fresh independent verification
+
+```text
+Release-ledger documentation contract:
+2 passed in 0.01s
+
+R3 Context, Prompt, reconciliation, and release E2E:
+221 passed, 1 skipped in 14.44s
+
+Combined fresh test result:
+223 passed, 1 skipped
+
+Ruff:
+All checks passed!
+
+Strict mypy:
+Success: no issues found in 93 source files
+
+git diff --check 66107cb..1e44ee0:
+clean
+```
+
+## Decision
+
+**Approved: Yes.** Task 5 is C0/I0/M0 and the R3 checkpoint is ready to hand
+off to R4 Task 1 at the corrected capability-intersection RED boundary.
diff --git a/.superpowers/sdd/v01-r3-task5-review.md b/.superpowers/sdd/v01-r3-task5-review.md
new file mode 100644
index 0000000..91fa3a3
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-task5-review.md
@@ -0,0 +1,115 @@
+# v0.1 R3 Task 5 Independent Checkpoint Review
+
+## Verdict
+
+- Reviewed commit: `fcc8829`
+- Reviewed range: `ab1d082..fcc8829`
+- Spec: **FAIL**
+- Quality: **FAIL**
+- Critical: **0**
+- Important: **1**
+- Minor: **1**
+- Summary: **C0 / I1 / M1**
+- Approval: **NOT APPROVED**
+
+The checkpoint correctly closes R3 and preserves its approved implementation
+facts, but its active resume handoff contradicts the R4 plan it names. Approval
+requires Critical 0 / Important 0, so Task 5 cannot be approved at this commit.
+
+## Findings
+
+### I1 - The resume point skips R4 Task 1 and misidentifies a Task 2 test
+
+The active R4 plan defines Task 1 as **Select and Persist Effective Run
+Capabilities**. It creates and first runs
+`tests/unit/runtime/test_capability_intersection.py`
+(`docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r4-child-mailbox.md`, lines
+103-145). The mailbox test is created by Task 2, not Task 1 (lines 229-264).
+
+In contrast, all three Task 5 records state or imply that
+`tests/unit/subagents/test_mailbox.py` is the R4 Task 1 / first-RED resume point:
+
+- `docs/plans/releases/v0.1.md`, lines 35 and 72-75;
+- `.superpowers/sdd/progress.md`, lines 358-359;
+- `.superpowers/sdd/v01-r3-task5-report.md`, lines 28-30.
+
+`tests/docs/test_v01_release_ledger.py` then makes the incorrect mapping durable
+by requiring the mailbox command and the phrase `created by R4 Task 1`.
+
+The PowerShell command itself is syntactically valid, but the named file does
+not exist at this checkpoint and the active plan does not create it until Task
+2. Running it now therefore produces a collection/path error, not Task 1's
+planned TDD failure. More importantly, following this handoff bypasses R4 Task
+1 capability work, on which the later mailbox/control behavior depends.
+
+Required correction: align the durable resume point with R4 Task 1 and its
+planned first test, or deliberately reorder/amend the R4 plan and then update
+all three records and the executable documentation contract together.
+
+### M1 - The recorded 13.65-second checkpoint duration has no matching source
+
+The release ledger and progress file call `221 passed, 1 skipped in 13.65s`
+fresh checkpoint evidence. The Task 5 report says this exact timing came from
+the approved Task 4 re-review, but that re-review records only `221 passed, 1
+skipped` without a duration. The Task 4 fix report records the same count in
+`23.19s`, while Task 5's own fresh-verification block records `25.32s`.
+
+The behavior and counts are independently reproducible, so this does not
+invalidate R3. The durable evidence should nevertheless either retain the
+actual fresh Task 5 output, omit volatile timing, or accurately identify the
+source of the 13.65-second run.
+
+## Verified checkpoint facts
+
+- The range changes only the two checkpoint ledgers, their executable docs
+  contract, and the Task 5 report. It contains no production-code change and
+  does not start R4.
+- R3 is consistently complete; R4 and R5 remain pending. Nothing marks v0.1 or
+  the installed release complete.
+- The active next-plan path correctly names
+  `docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r4-child-mailbox.md`.
+- Task 1's final chain (`dd93fb2`, `38e7d2d`, `93505aa`) and C0/I0/M0 approval
+  agree with its final transition review.
+- Task 2's final implementation/review commits (`3f23363`, `e5c646f`) and
+  C0/I0/M0 approval agree with its transition re-review.
+- Task 3's implementation/approval commits (`774ae6c`, `c94ea77`) and
+  C0/I0/M0 approval agree with its transition review.
+- Task 4's implementation, recovery fix, and final approval commits
+  (`2f2048c`, `79996db`, `ab1d082`) agree with the final re-review, including
+  C0/I0/M0, Spec PASS, and Quality PASS.
+- The old R3 Task 4 recovery paths and command are absent from both durable
+  records and are explicitly rejected by the docs contract.
+- The `tests/integration/runtime/test_recovery_api.py` built-in-Tool capability
+  mismatch remains explicitly disclosed. Task 5 does not claim a repository-
+  wide green suite or hide this release-suite debt.
+- The docs-test migration retains the R0-R2 checkpoint/history assertions and
+  strengthens the R3 completion and stale-marker guards; it does not weaken
+  prior release-ledger protection.
+
+## Fresh independent verification
+
+```text
+R3 Context, Prompt, reconciliation, and release E2E:
+221 passed, 1 skipped in 16.71s
+
+Release-ledger documentation contract:
+2 passed in 0.01s
+
+Combined fresh test result:
+223 passed, 1 skipped
+
+Ruff:
+All checks passed!
+
+Strict mypy:
+Success: no issues found in 93 source files
+
+git diff --check ab1d082..fcc8829:
+clean
+```
+
+## Decision
+
+**Approved: No.** Resolve I1, update the executable docs contract to match the
+chosen R4 ordering, and re-review the corrected checkpoint. M1 should be
+corrected in the same documentation-only fix.
diff --git a/docs/plans/releases/v0.1.md b/docs/plans/releases/v0.1.md
index 2029ce3..300fe61 100644
--- a/docs/plans/releases/v0.1.md
+++ b/docs/plans/releases/v0.1.md
@@ -9,42 +9,78 @@ roadmap or its hardening work.
 - [Release convergence design](../../superpowers/specs/2026-07-17-agent-sdk-v0.1-release-design.md)
 - [v0.1 implementation plan index](../../superpowers/plans/2026-07-17-agent-sdk-v0.1-implementation-index.md)

 ## Release Slices

 | Slice | Status | Acceptance extension | Evidence |
 |---|---|---|---|
 | R0 | completed | SQLite baseline/reopen/delete | 2026-07-17 checkpoint: 4 passed in 4.74s; Ruff: All checks passed! |
 | R1 | completed | built-in Tool authorization | 2026-07-17 final checkpoint: 97 passed, 3 skipped in 7.94s; Ruff/mypy clean |
 | R2 | completed | condition and bounded loop | 2026-07-20 final checkpoint: 403 passed in 43.03s; Ruff/mypy clean |
-| R3 | pending | automatic L0-L4 | pending |
+| R3 | completed | automatic L0-L4 | 2026-07-20 checkpoint: 221 passed, 1 skipped in 25.32s; Ruff/strict mypy clean |
 | R4 | pending | Child Tool/mailbox exchange | pending |
 | R5 | pending | attribution and installed wheel | pending |

 ## Current Resume Point

 - R0 is complete at checkpoint commit `ef0e4da`.
 - R1 is complete through final hardening commit `704db69`.
 - R1 final review approved.
 - R1 Tasks 1-3 are complete and independently approved.
 - R2 Tasks 1-4 are complete and independently approved through implementation
   checkpoint `f9beb63`.
 - R2 final hardening commits `4bdd433` and `826a32b` are complete; the
   final independent review found Critical 0 / Important 0 / Minor 0, Spec
   compliance PASS, Code quality PASS, and Ready to proceed to R3: Yes.
-- Active next plan: `docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md`.
-- Resume command: `Get-Content docs\superpowers\plans\2026-07-17-agent-sdk-v0.1-r3-auto-context.md`.
-- Next required action: R3 Task 1 Step 1, creating
+- Active next plan: `docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r4-child-mailbox.md`.
+- Resume command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\runtime\test_capability_intersection.py -q`.
+- R3 Task 1 deterministic L0-L2 is complete (commits
+  `dd93fb2`, `38e7d2d`, and `93505aa`); it began with
   `tests/unit/context/test_deterministic_strategies.py`.
-- After that file exists, the first RED command is
-  `.\.venv\Scripts\python.exe -m pytest tests/unit/context/test_deterministic_strategies.py -q`.
-- R3 remains pending; R3 implementation has not started.
+- R3 Task 1 final review: Critical 0 / Important 0 / Minor 0.
+- Spec PASS; Quality PASS. Controller gates: 42 deterministic strategy tests,
+  48 context integration tests, Ruff clean, strict mypy clean across 4 files, and
+  diff-check clean.
+- R3 Task 2 is complete: automatic L0-L4 recommendation and application,
+  `allow_lossy=False` capping L3/L4 at exact L2, distinct L3 summary and L4
+  rebase operations using LiteLLM purpose `context_compaction`, same-Session
+  recursive capsule evidence, and atomic Context View/capsule/event persistence.
+- Invalid, timeout, schema, reference, input-bound, or output-budget L3/L4
+  results fall back to the exact deterministic L2 renderer without failing the
+  main Run.
+- Task 2 safety fix commit `3f23363`; final independent re-review commit
+  `e5c646f`: Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS.
+  Fresh Context gate: 102 passed; Ruff and strict mypy clean.
+- R3 Task 3 is complete (implementation `774ae6c`, final approval `c94ea77`):
+  `AgentSpec`/`DurableAgentSpec` persist the prompt and Context runtime fields;
+  `SkillRegistry` is exposed publicly and its shared preflight covers direct,
+  Workflow-node, and subagent Run creation; default/application/Skill prompt
+  layers and their manifest are persisted; public `run.created` schema v2 is
+  redacted; and genuine R2 schema-v1 descriptor/recovery compatibility remains
+  authenticated.
+- Task 3 final approval: Critical 0 / Important 0 / Minor 0; Spec PASS;
+  Quality PASS. Effective evidence: controller mainline 201 passed; implementer
+  gate 521 passed, 1 skipped; Workflow/recovery/release gate 25 passed; Ruff
+  clean; strict mypy clean across 92 source files.
+- R3 Task 4 is complete: context preparation runs before every new model call;
+  the durable prepared request, Context View, and Prompt Manifest are
+  authenticated for exact recovery. Implementation/fix/approval commits are
+  `2f2048c`, `79996db`, and `ab1d082`.
+- Task 4 final approval: Critical 0 / Important 0 / Minor 0; Spec PASS;
+  Quality PASS. Its independent re-review covered strict provider request
+  shapes, View/Manifest ownership and linkage, no-side-effect corruption
+  rejection, and completed-call recovery.
+- R3 is complete. R4 remains pending.
+  `tests/unit/runtime/test_capability_intersection.py` is created by R4 Task 1;
+  the resume command above is the
+  first expected RED, so that file is intentionally not required to exist at
+  this checkpoint.

 ## Release Blockers

 The approved design blocks the release if a release-slice behavior or the shared
 acceptance scenario fails, SQLite data cannot be safely reopened, a workspace or
 permission boundary can be bypassed, or credentials/raw secrets can be exposed.
 Each R0-R5 checkpoint must stay green before the next slice begins.

 ## Chronological Checkpoint Log

@@ -169,18 +205,43 @@ Each R0-R5 checkpoint must stay green before the next slice begins.
   $ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk\workflow tests\unit\workflow tests\integration\workflow tests\e2e\test_v01_release.py
   All checks passed!

   $ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk\workflow src\agent_sdk\runtime\execution.py
   Success: no issues found in 10 source files

   $ git diff --check f9beb63..826a32b
   clean
   ```

+- 2026-07-20 - R3 checkpoint completed. Tasks 1-4 are independently approved:
+  Task 1 deterministic L0-L2 (`dd93fb2`, `38e7d2d`, `93505aa`); Task 2
+  automatic L0-L4 and recovery-safe capsule evidence (`3f23363`, `e5c646f`);
+  Task 3 prompt/Skill manifests and descriptor compatibility (`774ae6c`,
+  `c94ea77`); and Task 4 per-call Context preparation and exact recovery
+  (`2f2048c`, `79996db`, `ab1d082`). All final approvals report Critical 0 /
+  Important 0 / Minor 0, Spec PASS, and Quality PASS. Fresh checkpoint
+  evidence:
+
+  ```text
+  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
+  $ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\context tests\integration\context tests\integration\prompts tests\unit\runtime\test_reconciliation_models.py tests\e2e\test_v01_release.py -q
+  221 passed, 1 skipped in 25.32s
+
+  $ .\.venv\Scripts\python.exe -m ruff check src tests\unit\context tests\integration\context tests\integration\prompts tests\unit\runtime\test_reconciliation_models.py tests\e2e\test_v01_release.py tests\docs\test_v01_release_ledger.py
+  All checks passed!
+
+  $ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk
+  Success: no issues found in 93 source files
+  ```
+
+  The next plan is R4 Child mailbox/control Tools. Its Task 1 creates
+  `tests/unit/runtime/test_capability_intersection.py`; running that targeted test is the
+  first expected RED rather than a missing-test failure at this checkpoint.
+
 ## post-v0.1 Hardening Backlog

 The [previous roadmap](../00-roadmap.md) and its milestone/task history remain
 the hardening backlog unless a v0.1 release slice explicitly references an item.
 Deferred M02 work includes M02-T003 Artifact Lifecycle Phases B-D and M02-T004
 advanced controls/sync; the approved design also defers multi-worker recovery,
 complex Workflow scheduling, advanced Child scheduling, retrieval, advanced
 analytics/exporters, and compatibility/performance/conformance hardening.
diff --git a/src/agent_sdk/__init__.py b/src/agent_sdk/__init__.py
index 10d97ae..e468944 100644
--- a/src/agent_sdk/__init__.py
+++ b/src/agent_sdk/__init__.py
@@ -15,20 +15,21 @@ from agent_sdk.api import (
 )
 from agent_sdk.config import AgentSDKConfig, CaptureLevel
 from agent_sdk.context import (
     CompactionLevel,
     CompactionPolicy,
     ContextBudget,
     ContextCapsule,
     ContextItem,
     ContextPlanner,
     ContextRetrieval,
+    ContextRuntimeConfig,
     ContextView,
 )
 from agent_sdk.errors import AgentSDKError, ErrorCode, SessionBusyError
 from agent_sdk.evaluation import (
     EvaluationDecision,
     EvaluationEngine,
     EvaluationResult,
     EvaluationSubject,
     EvaluationVerdict,
     Evaluator,
@@ -46,21 +47,26 @@ from agent_sdk.observability import (
     EventFilter,
     EventQueryResult,
     ExecutionTree,
     ExecutionTreeNode,
     ObservedEvent,
     ObservedRun,
     QueryService,
     RunTimeline,
     SubscriptionService,
 )
-from agent_sdk.prompts import BuiltPrompt, PromptComposer, PromptManifest
+from agent_sdk.prompts import (
+    BuiltPrompt,
+    PromptComposer,
+    PromptManifest,
+    PromptManifestPersistence,
+)
 from agent_sdk.runtime.handles import RunHandle
 from agent_sdk.runtime.execution import (
     ExecutionDescriptor,
     ExecutionPolicyDescriptor,
     ToolCapabilityDescriptor,
     WorkflowAgentDescriptor,
     WorkflowExecutionDescriptor,
 )
 from agent_sdk.runtime.models import (
     AgentSpec,
@@ -124,20 +130,21 @@ __all__ = [
     "ChildResult",
     "ChildUsage",
     "CompactionLevel",
     "CompactionPolicy",
     "ContextAPI",
     "ContextBudget",
     "ContextCapsule",
     "ContextItem",
     "ContextPlanner",
     "ContextRetrieval",
+    "ContextRuntimeConfig",
     "ContextView",
     "ErrorCode",
     "EvaluationAPI",
     "EvaluationDecision",
     "EvaluationEngine",
     "EvaluationResult",
     "EvaluationSubject",
     "EvaluationVerdict",
     "Evaluator",
     "EventAPI",
@@ -155,20 +162,21 @@ __all__ = [
     "PermissionEffect",
     "PermissionRequest",
     "ProviderRecoveryAdapter",
     "ProviderRecoveryDisposition",
     "ProviderRecoveryRequest",
     "ProviderRecoveryResult",
     "ObservedEvent",
     "ObservedRun",
     "PromptComposer",
     "PromptManifest",
+    "PromptManifestPersistence",
     "BuiltPrompt",
     "RunAPI",
     "RunFailure",
     "RunHandle",
     "RunResult",
     "RunSnapshot",
     "RunStatus",
     "RunTimeline",
     "QueryAPI",
     "QueryService",
diff --git a/src/agent_sdk/api.py b/src/agent_sdk/api.py
index 89257a4..613ff68 100644
--- a/src/agent_sdk/api.py
+++ b/src/agent_sdk/api.py
@@ -8,20 +8,21 @@ from enum import Enum
 from functools import partial
 from pathlib import Path
 from typing import Any, AsyncIterator, Literal, cast

 from agent_sdk.analytics import AnalyticsQueries, AnalyticsResult
 from agent_sdk.config import AgentSDKConfig
 from agent_sdk.context import (
     CompactionLevel,
     CompactionPolicy,
     ContextCapsule,
+    ContextMiddleware,
     ContextPlanner,
     ContextRetrieval,
     ContextView,
 )
 from agent_sdk.evaluation import EvaluationEngine, EvaluationResult, Evaluator
 from agent_sdk.errors import AgentSDKError, ErrorCode
 from agent_sdk.events.models import EventEnvelope
 from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest
 from agent_sdk.permissions.broker import InProcessPermissionBridge
 from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
@@ -63,20 +64,21 @@ from agent_sdk.runtime.recovery import (
     RunRecoveryService,
 )
 from agent_sdk.runtime.reconciliation import (
     ExternalOperation,
     ReconciliationAction,
     ReconciliationRequest,
     ReconciliationService,
     RunCheckpoint,
     _context_free_recovery_errors,
 )
+from agent_sdk.skills import SkillRegistry
 from agent_sdk.storage.base import (
     CommitBatch,
     CommitResult,
     RunProgressBatch,
     StateStore,
     StoredEvent,
 )
 from agent_sdk.storage.idempotency import IdempotencyRecord
 from agent_sdk.storage.sqlite import SQLiteStore
 from agent_sdk.tools.registry import ToolRegistry
@@ -996,36 +998,38 @@ class PermissionAPI:


 class AgentSDK:
     def __init__(self, config: AgentSDKConfig) -> None:
         store = _LazySQLiteStore(config.database_path)
         self._initialize(
             store,
             LiteLLMGateway(),
             permission_default=config.permission_default,
             permission_rules=config.permission_rules,
+            skill_roots=config.skill_roots,
             enable_builtin_tools=config.enable_builtin_tools,
             builtin_tool_output_bytes=config.builtin_tool_output_bytes,
             permission_bridge=InProcessPermissionBridge(),
             owned_close=store.close,
             provider_recovery_timeout_seconds=30.0,
         )

     @classmethod
     def for_test(
         cls,
         *,
         acompletion: _ACompletion,
         store: StateStore | None = None,
         database_path: str | Path | None = None,
         permission_default: _PermissionDefault = "ask",
         permission_rules: tuple[PermissionRule, ...] = (),
+        skill_roots: tuple[str | Path, ...] = (),
         enable_builtin_tools: bool = True,
         builtin_tool_output_bytes: int = 64 * 1024,
         permission_bridge: InProcessPermissionBridge | None | object = (
             _DEFAULT_PERMISSION_BRIDGE
         ),
         provider_recovery_timeout_seconds: float = 30.0,
     ) -> AgentSDK:
         if (store is None) == (database_path is None):
             raise AgentSDKError(
                 ErrorCode.INVALID_STATE,
@@ -1046,78 +1050,84 @@ class AgentSDK:
         bridge = (
             InProcessPermissionBridge()
             if permission_bridge is _DEFAULT_PERMISSION_BRIDGE
             else cast(InProcessPermissionBridge | None, permission_bridge)
         )
         sdk._initialize(
             selected_store,
             LiteLLMGateway._for_test(acompletion),
             permission_default=permission_default,
             permission_rules=permission_rules,
+            skill_roots=tuple(Path(root) for root in skill_roots),
             enable_builtin_tools=enable_builtin_tools,
             builtin_tool_output_bytes=builtin_tool_output_bytes,
             permission_bridge=bridge,
             owned_close=owned_close,
             provider_recovery_timeout_seconds=provider_recovery_timeout_seconds,
         )
         return sdk

     def _initialize(
         self,
         store: StateStore,
         models: LiteLLMGateway,
         *,
         permission_default: _PermissionDefault,
         permission_rules: tuple[PermissionRule, ...],
+        skill_roots: tuple[Path, ...],
         enable_builtin_tools: bool,
         builtin_tool_output_bytes: int,
         permission_bridge: InProcessPermissionBridge | None,
         owned_close: Callable[[], Awaitable[None]] | None,
         provider_recovery_timeout_seconds: float,
     ) -> None:
         self._active_tasks: set[asyncio.Task[Any]] = set()
         self._owned_close = owned_close
         self._lifecycle = _SDKLifecycle()
         self._startup_scan_lock = asyncio.Lock()
         self._startup_scan_task: asyncio.Task[None] | None = None
-        commands = RuntimeCommands(store)
+        skills = SkillRegistry(skill_roots)
+        skills.discover()
+        commands = RuntimeCommands(store, agent_preflight=skills.validate_agent)
         tools = ToolRegistry()
         if enable_builtin_tools:
             register_builtin_tools(
                 registry=tools,
                 store=store,
                 output_limit=builtin_tool_output_bytes,
             )
         provider_recovery = ProviderRecoveryRegistry()
         policy = PolicyEngine(permission_default, permission_rules)
         engine = RunEngine(
             store,
             models,
             tools,
             policy,
             permission_bridge,
             provider_recovery=provider_recovery,
+            context_middleware=ContextMiddleware(store, models, skills),
         )
         agents = AgentRegistry()
         recovery_scanner = RecoveryScanner(store)
         workflows = WorkflowExecutor(
             store,
             commands,
             engine,
             agents,
             tool_schemas=tools.schemas,
             tool_specs=tools.list,
             policy=policy,
             track_run_task=self._track_task,
             track_workflow_task=self._track_task,
         )
         self.tools = tools
+        self.skills = skills
         self.agents = AgentAPI(agents)
         self.permissions = PermissionAPI(permission_bridge)
         self.sessions = SessionAPI(commands, self._lifecycle)
         self.runs = RunAPI(
             store,
             commands,
             engine,
             self._track_task,
             self._lifecycle,
             tools,
diff --git a/src/agent_sdk/config.py b/src/agent_sdk/config.py
index 4eca0de..1a166a2 100644
--- a/src/agent_sdk/config.py
+++ b/src/agent_sdk/config.py
@@ -13,12 +13,13 @@ class CaptureLevel(StrEnum):
     FULL = "full"


 class AgentSDKConfig(BaseModel):
     model_config = ConfigDict(frozen=True, extra="forbid")

     database_path: Path
     capture_level: CaptureLevel = CaptureLevel.PREVIEW
     permission_default: Literal["allow", "deny", "ask"] = "ask"
     permission_rules: tuple[PermissionRule, ...] = ()
+    skill_roots: tuple[Path, ...] = ()
     enable_builtin_tools: bool = True
     builtin_tool_output_bytes: int = Field(default=64 * 1024, ge=1024)
diff --git a/src/agent_sdk/context/__init__.py b/src/agent_sdk/context/__init__.py
index 3c1cb83..c5491b4 100644
--- a/src/agent_sdk/context/__init__.py
+++ b/src/agent_sdk/context/__init__.py
@@ -1,21 +1,43 @@
 from agent_sdk.context.models import (
     CompactionLevel,
     CompactionPolicy,
     ContextBudget,
     ContextCapsule,
     ContextItem,
+    ContextRuntimeConfig,
     ContextView,
+    SourceMessage,
 )
+from agent_sdk.context.middleware import ContextMiddleware, PreparedContext
 from agent_sdk.context.planner import ContextPlanner
+from agent_sdk.context.rendering import render_level
 from agent_sdk.context.retrieval import ContextRetrieval
+from agent_sdk.context.sources import checkpoint_ref, extract_sources
+from agent_sdk.context.strategies import (
+    StrategyResult,
+    apply_l0,
+    apply_l1,
+    apply_l2,
+)

 __all__ = [
     "CompactionLevel",
     "CompactionPolicy",
     "ContextBudget",
     "ContextCapsule",
     "ContextItem",
+    "ContextMiddleware",
     "ContextPlanner",
     "ContextRetrieval",
+    "ContextRuntimeConfig",
     "ContextView",
+    "PreparedContext",
+    "SourceMessage",
+    "StrategyResult",
+    "apply_l0",
+    "apply_l1",
+    "apply_l2",
+    "checkpoint_ref",
+    "extract_sources",
+    "render_level",
 ]
diff --git a/src/agent_sdk/context/compactor.py b/src/agent_sdk/context/compactor.py
index b76c74c..2d0835f 100644
--- a/src/agent_sdk/context/compactor.py
+++ b/src/agent_sdk/context/compactor.py
@@ -1,92 +1,198 @@
 from __future__ import annotations

 import json
 from collections.abc import Sequence, Set
 from dataclasses import dataclass
+from typing import Any

 from agent_sdk.context.models import ContextCapsule, ContextItem
 from agent_sdk.errors import AgentSDKError, ErrorCode
 from agent_sdk.models.litellm_gateway import (
     LiteLLMGateway,
     ModelRequest,
     UsageReported,
 )

 _MAX_COMPACTION_PROMPT_BYTES = 256 * 1024
+_MAX_COMPACTION_SOURCES = 128


 @dataclass(frozen=True)
-class _CompactionResult:
+class CompactionResult:
     capsule: ContextCapsule | None
     usage: UsageReported


 class ContextCompactor:
     def __init__(self, models: LiteLLMGateway, *, model: str) -> None:
         self._models = models
         self._model = model

+    async def summarize(
+        self,
+        source: tuple[ContextItem, ...],
+        protected: set[str],
+    ) -> CompactionResult:
+        try:
+            retained = set(protected)
+            summarized = tuple(
+                item for item in source if item.event_id not in retained
+            )
+            summarized_refs = {item.event_id for item in summarized}
+            if not summarized_refs:
+                return CompactionResult(
+                    capsule=None,
+                    usage=UsageReported(None, None, None),
+                )
+            return await self._complete(
+                document={
+                    "schema": "ContextCapsule",
+                    "operation": "summarize",
+                    "retained_event_ids": [
+                        item.event_id
+                        for item in source
+                        if item.event_id in retained
+                    ],
+                    "sources": self._bounded_sources(summarized),
+                },
+                allowed_refs=summarized_refs,
+                required_refs=summarized_refs,
+                instruction=(
+                    "Summarize only the supplied closed older sources into a "
+                    "ContextCapsule. Do not summarize retained messages. Cite "
+                    "every supplied source event id and no other id."
+                ),
+            )
+        except AgentSDKError:
+            return CompactionResult(
+                capsule=None,
+                usage=UsageReported(None, None, None),
+            )
+
+    async def rebase(
+        self,
+        capsules: tuple[ContextCapsule, ...],
+        source: tuple[ContextItem, ...],
+        protected: set[str],
+        *,
+        capsule_ids: tuple[str, ...] = (),
+    ) -> CompactionResult:
+        try:
+            if capsule_ids and len(capsule_ids) != len(capsules):
+                raise ValueError("capsule ids must correspond to capsules")
+            retained_source = tuple(
+                item for item in source if item.event_id in protected
+            )
+            prior_source_refs = {
+                ref for capsule in capsules for ref in capsule.source_event_ids
+            }
+            prior_refs = set(capsule_ids) if capsule_ids else prior_source_refs
+            capsule_documents: list[dict[str, Any]] = []
+            for index, capsule in enumerate(capsules):
+                value = capsule.model_dump(mode="json")
+                if capsule_ids:
+                    value["capsule_id"] = capsule_ids[index]
+                capsule_documents.append(value)
+            return await self._complete(
+                document={
+                    "schema": "ContextCapsule",
+                    "operation": "rebase",
+                    "capsule_ids": list(capsule_ids),
+                    "capsules": capsule_documents,
+                    "sources": self._bounded_sources(retained_source),
+                },
+                allowed_refs=(
+                    {item.event_id for item in source}
+                    | prior_source_refs
+                    | set(capsule_ids)
+                ),
+                required_refs=prior_refs
+                | {item.event_id for item in retained_source},
+                instruction=(
+                    "Rebase the validated prior capsules with only the supplied "
+                    "active, recent, or protected sources. Cite every prior "
+                    "capsule reference and retained source id, and cite no "
+                    "unknown id."
+                ),
+            )
+        except AgentSDKError:
+            return CompactionResult(
+                capsule=None,
+                usage=UsageReported(None, None, None),
+            )
+
     async def compact(
         self,
         source: Sequence[ContextItem],
         protected: Set[str],
-    ) -> _CompactionResult:
+    ) -> CompactionResult:
+        return await self.summarize(tuple(source), set(protected))
+
+    async def _complete(
+        self,
+        *,
+        document: dict[str, Any],
+        allowed_refs: set[str],
+        required_refs: set[str],
+        instruction: str,
+    ) -> CompactionResult:
         try:
             completion = await self._models.complete_structured(
                 ModelRequest(
                     model=self._model,
-                    messages=self._messages(source, protected),
-                    purpose="compaction",
+                    messages=self._messages(document, instruction),
+                    purpose="context_compaction",
                 ),
                 ContextCapsule,
             )
             capsule = completion.parsed
-            source_ids = {item.event_id for item in source}
-            cited_ids = set(capsule.source_event_ids)
-            if not cited_ids <= source_ids or not set(protected) <= cited_ids:
-                return _CompactionResult(
+            cited = set(capsule.source_event_ids)
+            if not cited <= allowed_refs or not required_refs <= cited:
+                return CompactionResult(
                     capsule=None,
                     usage=completion.usage,
                 )
-            return _CompactionResult(capsule=capsule, usage=completion.usage)
+            return CompactionResult(
+                capsule=capsule,
+                usage=completion.usage,
+            )
         except AgentSDKError:
-            return _CompactionResult(
+            return CompactionResult(
                 capsule=None,
                 usage=UsageReported(None, None, None),
             )

+    @staticmethod
+    def _bounded_sources(
+        source: tuple[ContextItem, ...],
+    ) -> list[dict[str, Any]]:
+        if len(source) > _MAX_COMPACTION_SOURCES:
+            raise AgentSDKError(
+                ErrorCode.INVALID_STATE,
+                "context compaction source count exceeds limit",
+                retryable=False,
+            )
+        return [item.model_dump(mode="json") for item in source]
+
     @staticmethod
     def _messages(
-        source: Sequence[ContextItem],
-        protected: Set[str],
+        document: dict[str, Any],
+        instruction: str,
     ) -> tuple[dict[str, object], ...]:
-        document = {
-            "schema": "ContextCapsule",
-            "protected_event_ids": [
-                item.event_id for item in source if item.event_id in protected
-            ],
-            "sources": [item.model_dump(mode="json") for item in source],
-        }
         text = json.dumps(
             document,
             ensure_ascii=False,
             allow_nan=False,
             sort_keys=True,
             separators=(",", ":"),
         )
         if len(text.encode("utf-8")) > _MAX_COMPACTION_PROMPT_BYTES:
             raise AgentSDKError(
                 ErrorCode.INVALID_STATE,
                 "context compaction input exceeds size limit",
                 retryable=False,
             )
         return (
-            {
-                "role": "system",
-                "content": (
-                    "Create a ContextCapsule that cites only supplied event ids. "
-                    "Include every protected source conveyed by the caller."
-                ),
-            },
+            {"role": "system", "content": instruction},
             {"role": "user", "content": text},
         )
diff --git a/src/agent_sdk/context/middleware.py b/src/agent_sdk/context/middleware.py
new file mode 100644
index 0000000..473d65f
--- /dev/null
+++ b/src/agent_sdk/context/middleware.py
@@ -0,0 +1,101 @@
+from __future__ import annotations
+
+from copy import deepcopy
+from dataclasses import dataclass
+from typing import Any
+
+from agent_sdk.context.models import ContextView
+from agent_sdk.context.planner import ContextPlanner
+from agent_sdk.errors import AgentSDKError, ErrorCode
+from agent_sdk.models.litellm_gateway import LiteLLMGateway
+from agent_sdk.prompts.composer import PromptComposer
+from agent_sdk.prompts.models import BuiltPrompt
+from agent_sdk.prompts.persistence import PromptManifestPersistence
+from agent_sdk.runtime.models import RunSnapshot
+from agent_sdk.runtime.reconciliation import RunCheckpoint
+from agent_sdk.skills.registry import SkillRegistry
+from agent_sdk.storage.base import StateStore
+
+
+@dataclass(frozen=True)
+class PreparedContext:
+    view: ContextView
+    messages: tuple[dict[str, Any], ...]
+    prompt: BuiltPrompt
+
+
+class ContextMiddleware:
+    def __init__(
+        self,
+        store: StateStore,
+        models: LiteLLMGateway,
+        skills: SkillRegistry,
+    ) -> None:
+        self._store = store
+        self._models = models
+        self._skills = skills
+        self._prompts = PromptComposer()
+        self._persistence = PromptManifestPersistence(store)
+
+    async def prepare(
+        self,
+        *,
+        run: RunSnapshot,
+        checkpoint: RunCheckpoint,
+        tools: tuple[dict[str, Any], ...],
+    ) -> PreparedContext:
+        descriptor = run.execution_descriptor
+        if descriptor is None:
+            raise AgentSDKError(
+                ErrorCode.INVALID_STATE,
+                "run execution descriptor is required for context",
+                retryable=False,
+            )
+        agent = descriptor.agent
+        config = agent.context
+        planner = ContextPlanner(
+            self._store,
+            self._models,
+            model=agent.model,
+            model_window=config.model_window,
+            output_reserve=config.output_reserve,
+            safety_reserve=config.safety_reserve,
+            policy=config.policy,
+            recent_messages=config.recent_messages,
+            tool_preview_bytes=config.tool_preview_bytes,
+        )
+        planned = await planner.prepare(
+            session_id=run.session_id,
+            run_id=run.run_id,
+            checkpoint=checkpoint,
+            config=config,
+        )
+        activated = tuple(self._skills.activate(name) for name in agent.skills)
+        prompt = self._prompts.compose(
+            profile=agent.prompt_profile,
+            application=agent.system_prompt,
+            skills=activated,
+            context_view=planned.view,
+            model=agent.model,
+            tools=tools,
+        )
+        await self._persistence.persist(
+            prompt.manifest,
+            session_id=run.session_id,
+        )
+        prompt_messages = tuple(
+            deepcopy(dict(message))
+            for message in prompt.messages
+        )
+        context_messages = tuple(
+            deepcopy(dict(message))
+            for message in planned.messages
+        )
+        return PreparedContext(
+            view=planned.view,
+            messages=(*prompt_messages, *context_messages),
+            prompt=prompt,
+        )
+
+
+__all__ = ["ContextMiddleware", "PreparedContext"]
diff --git a/src/agent_sdk/context/models.py b/src/agent_sdk/context/models.py
index e2a00c4..21686c0 100644
--- a/src/agent_sdk/context/models.py
+++ b/src/agent_sdk/context/models.py
@@ -1,99 +1,249 @@
 from __future__ import annotations

+import json
 import math
 from collections.abc import Mapping
-from enum import StrEnum
-from typing import Any, Literal, Self
+from types import MappingProxyType
+from typing import Any, Literal, Self, cast

 from pydantic import (
     BaseModel,
     ConfigDict,
     Field,
+    StrictBool,
     StrictFloat,
     StrictInt,
     StrictStr,
+    field_serializer,
+    field_validator,
     model_validator,
 )

+from agent_sdk.context_runtime import (
+    CompactionLevel as CompactionLevel,
+    CompactionPolicy as CompactionPolicy,
+    ContextRuntimeConfig as ContextRuntimeConfig,
+)
+from agent_sdk.tools.models import freeze_json, thaw_json
+
+type JsonValue = (
+    None
+    | bool
+    | int
+    | float
+    | str
+    | tuple[JsonValue, ...]
+    | Mapping[str, JsonValue]
+)
+
+_SOURCE_MESSAGE_MAX_DEPTH = 32
+_SOURCE_MESSAGE_MAX_ENTRIES = 20_000
+_SOURCE_MESSAGE_MAX_BYTES = 256 * 1024
+
+
+def _bounded_json(
+    value: Any,
+    *,
+    depth: int,
+    entries: list[int],
+    active: set[int],
+) -> JsonValue:
+    if isinstance(value, (Mapping, list, tuple)):
+        if depth > _SOURCE_MESSAGE_MAX_DEPTH:
+            raise ValueError("message nesting exceeds 32")
+        identity = id(value)
+        if identity in active:
+            raise ValueError("message contains a cycle")
+        active.add(identity)
+        try:
+            entries[0] += len(value)
+            if entries[0] > _SOURCE_MESSAGE_MAX_ENTRIES:
+                raise ValueError("message exceeds 20000 container entries")
+            if isinstance(value, Mapping):
+                frozen: dict[str, JsonValue] = {}
+                for key, item in value.items():
+                    if not isinstance(key, str):
+                        raise ValueError("JSON object keys must be strings")
+                    frozen[key] = _bounded_json(
+                        item,
+                        depth=depth + 1,
+                        entries=entries,
+                        active=active,
+                    )
+                return MappingProxyType(frozen)
+            return tuple(
+                _bounded_json(
+                    item,
+                    depth=depth + 1,
+                    entries=entries,
+                    active=active,
+                )
+                for item in value
+            )
+        finally:
+            active.remove(identity)
+    if value is None or isinstance(value, (bool, str)):
+        return value
+    if isinstance(value, int):
+        return value
+    if isinstance(value, float):
+        if not math.isfinite(value):
+            raise ValueError("JSON numbers must be finite")
+        return value
+    raise ValueError("value must be JSON-compatible")
+
+
+def _valid_tool_calls(value: JsonValue) -> bool:
+    if not isinstance(value, tuple) or not value:
+        return False
+    for call in value:
+        if not isinstance(call, Mapping) or set(call) != {
+            "id",
+            "type",
+            "function",
+        }:
+            return False
+        call_id = call["id"]
+        function = call["function"]
+        if (
+            not isinstance(call_id, str)
+            or not call_id
+            or call["type"] != "function"
+            or not isinstance(function, Mapping)
+            or set(function) != {"name", "arguments"}
+        ):
+            return False
+        name = function["name"]
+        arguments = function["arguments"]
+        if (
+            not isinstance(name, str)
+            or not name
+            or not isinstance(arguments, str)
+        ):
+            return False
+    return True
+

 class _DetachedModel(BaseModel):
     model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

     def model_copy(
         self,
         *,
         update: Mapping[str, Any] | None = None,
         deep: bool = False,
     ) -> Self:
         del deep
         data = self.model_dump(mode="json")
         if update is not None:
             data.update(update)
         return type(self).model_validate(data)


-class CompactionLevel(StrEnum):
-    L0 = "L0"
-    L1 = "L1"
-    L2 = "L2"
-    L3 = "L3"
-    L4 = "L4"
-
-
-class CompactionPolicy(_DetachedModel):
-    l1_reference: StrictFloat = Field(default=0.70, gt=0, lt=1)
-    l2_selective: StrictFloat = Field(default=0.80, gt=0, lt=1)
-    l3_summary: StrictFloat = Field(default=0.90, gt=0, lt=1)
-    l4_rebase: StrictFloat = Field(default=0.96, gt=0, lt=1)
-    recovery_target: StrictFloat = Field(default=0.75, gt=0, lt=1)
-
-    @model_validator(mode="after")
-    def _validate_threshold_order(self) -> CompactionPolicy:
-        if not (
-            self.l1_reference
-            < self.l2_selective
-            < self.l3_summary
-            < self.l4_rebase
-        ):
-            raise ValueError("compaction thresholds must be strictly increasing")
-        if self.recovery_target >= self.l2_selective:
-            raise ValueError("recovery target must be below L2")
-        return self
-
-    def recommend(self, watermark_ratio: float) -> CompactionLevel:
-        if (
-            isinstance(watermark_ratio, bool)
-            or not isinstance(watermark_ratio, (int, float))
-            or not math.isfinite(watermark_ratio)
-            or watermark_ratio < 0
-        ):
-            raise ValueError("watermark ratio must be a finite non-negative number")
-        if watermark_ratio >= self.l4_rebase:
-            return CompactionLevel.L4
-        if watermark_ratio >= self.l3_summary:
-            return CompactionLevel.L3
-        if watermark_ratio >= self.l2_selective:
-            return CompactionLevel.L2
-        if watermark_ratio >= self.l1_reference:
-            return CompactionLevel.L1
-        return CompactionLevel.L0
-
-
 class ContextItem(_DetachedModel):
     event_id: StrictStr = Field(min_length=1)
     cursor: StrictInt = Field(ge=1)
     event_type: StrictStr = Field(min_length=1)
     role: Literal["system", "user", "assistant", "tool"]
     content: StrictStr


+class SourceMessage(_DetachedModel):
+    model_config = ConfigDict(
+        frozen=True,
+        extra="forbid",
+        validate_default=True,
+        arbitrary_types_allowed=True,
+        strict=True,
+    )
+
+    ref: StrictStr = Field(min_length=1, max_length=64)
+    role: Literal["system", "user", "assistant", "tool"]
+    message: Mapping[str, JsonValue]
+    event_type: StrictStr = Field(min_length=1, max_length=128)
+    protected: StrictBool = False
+    current: StrictBool = False
+
+    @field_validator("ref", mode="before")
+    @classmethod
+    def _validate_ref_bytes(cls, value: Any) -> Any:
+        if isinstance(value, str) and len(value.encode("utf-8")) > 64:
+            raise ValueError("ref must not exceed 64 UTF-8 bytes")
+        return value
+
+    @field_validator("message", mode="before")
+    @classmethod
+    def _validate_message(cls, value: Any) -> Mapping[str, JsonValue]:
+        if not isinstance(value, Mapping):
+            raise ValueError("source message must be a JSON object")
+        entries = [0]
+        frozen = _bounded_json(
+            value,
+            depth=0,
+            entries=entries,
+            active=set(),
+        )
+        assert isinstance(frozen, Mapping)
+        encoded = json.dumps(
+            thaw_json(frozen),
+            ensure_ascii=False,
+            allow_nan=False,
+            separators=(",", ":"),
+        ).encode("utf-8")
+        if len(encoded) > _SOURCE_MESSAGE_MAX_BYTES:
+            raise ValueError("serialized message exceeds 262144 bytes")
+        return frozen
+
+    @field_validator("message", mode="after")
+    @classmethod
+    def _freeze_message(
+        cls,
+        value: Mapping[str, JsonValue],
+    ) -> Mapping[str, JsonValue]:
+        return cast(Mapping[str, JsonValue], freeze_json(value))
+
+    @field_serializer("message")
+    def _serialize_message(
+        self,
+        value: Mapping[str, JsonValue],
+    ) -> dict[str, Any]:
+        thawed = thaw_json(value)
+        assert isinstance(thawed, dict)
+        return thawed
+
+    @model_validator(mode="after")
+    def _validate_provider_message(self) -> SourceMessage:
+        message_role = self.message.get("role")
+        if message_role != self.role:
+            raise ValueError("message role must match source role")
+        content = self.message.get("content")
+        if self.role in {"system", "user", "tool"}:
+            if not isinstance(content, str):
+                raise ValueError(f"{self.role} content must be a string")
+        else:
+            has_tool_calls = "tool_calls" in self.message
+            tool_calls = self.message.get("tool_calls")
+            if has_tool_calls and not _valid_tool_calls(tool_calls):
+                raise ValueError(
+                    "tool_calls must be a nonempty sequence of exact "
+                    "function-call protocol objects"
+                )
+            if content is None and not has_tool_calls:
+                raise ValueError(
+                    "assistant null content requires tool-call protocol data"
+                )
+            if content is not None and not isinstance(content, str):
+                raise ValueError("assistant content must be a string or null")
+        return self
+
+
 class _BudgetInputs(_DetachedModel):
     model_window: StrictInt = Field(gt=0)
     output_reserve: StrictInt = Field(ge=0)
     tool_schema_tokens: StrictInt = Field(ge=0)
     safety_reserve: StrictInt = Field(ge=0)
     projected_source_tokens: StrictInt = Field(ge=0)


 class ContextBudget(_DetachedModel):
     model_window: StrictInt = Field(gt=0)
@@ -177,29 +327,45 @@ class ContextCapsule(_DetachedModel):

 class ContextView(_DetachedModel):
     view_id: StrictStr = Field(min_length=1)
     session_id: StrictStr = Field(min_length=1)
     message_refs: tuple[StrictStr, ...]
     capsule_id: StrictStr | None = Field(min_length=1)
     estimated_tokens: StrictInt = Field(ge=0)
     recommended_level: CompactionLevel = CompactionLevel.L0
     applied_level: CompactionLevel = CompactionLevel.L0
     budget: ContextBudget | None = None
+    source_refs: tuple[StrictStr, ...] = ()
+    transformations: tuple[StrictStr, ...] = ()
+    fallback_from: CompactionLevel | None = None
+    consumed_message_ids: tuple[StrictStr, ...] = ()

     @model_validator(mode="after")
     def _validate_unique_message_refs(self) -> ContextView:
         if len(set(self.message_refs)) != len(self.message_refs):
             raise ValueError("context message references must be unique")
+        if len(set(self.source_refs)) != len(self.source_refs):
+            raise ValueError("context source references must be unique")
+        if len(set(self.consumed_message_ids)) != len(
+            self.consumed_message_ids
+        ):
+            raise ValueError("consumed message ids must be unique")
         has_capsule = self.capsule_id is not None
         applied_capsule = self.applied_level in {
             CompactionLevel.L3,
             CompactionLevel.L4,
         }
         if has_capsule != applied_capsule:
             raise ValueError(
                 "context capsule and applied level must describe the same state"
             )
+        if self.fallback_from is not None and (
+            self.fallback_from
+            not in {CompactionLevel.L3, CompactionLevel.L4}
+            or self.applied_level is not CompactionLevel.L2
+        ):
+            raise ValueError("context fallback must describe an L3/L4 to L2 path")
         return self

     @property
     def id(self) -> str:
         return self.view_id
diff --git a/src/agent_sdk/context/planner.py b/src/agent_sdk/context/planner.py
index fd3cebe..38d0310 100644
--- a/src/agent_sdk/context/planner.py
+++ b/src/agent_sdk/context/planner.py
@@ -1,76 +1,387 @@
 from __future__ import annotations

 import json
 from collections.abc import Iterable, Mapping
 from copy import deepcopy
+from dataclasses import dataclass
 from typing import Any, Literal, cast

 from pydantic import ValidationError

 from agent_sdk.context.budget import TokenCounter, default_token_counter
 from agent_sdk.context.compactor import ContextCompactor
 from agent_sdk.context.models import (
     CompactionLevel,
     CompactionPolicy,
     ContextBudget,
     ContextCapsule,
     ContextItem,
+    ContextRuntimeConfig,
     ContextView,
+    SourceMessage,
 )
+from agent_sdk.context.rendering import render_level
+from agent_sdk.context.retrieval import ContextRetrieval
+from agent_sdk.context.sources import extract_sources
+from agent_sdk.context.strategies import StrategyResult
 from agent_sdk.errors import AgentSDKError, ErrorCode
 from agent_sdk.events.models import EventEnvelope
 from agent_sdk.ids import new_id
 from agent_sdk.models.litellm_gateway import LiteLLMGateway, UsageReported
+from agent_sdk.runtime.reconciliation import RunCheckpoint
 from agent_sdk.storage.base import (
     CommitBatch,
     SnapshotPrecondition,
     SnapshotPreconditionError,
     SnapshotWrite,
     StateStore,
     StoredEvent,
 )
+from agent_sdk.tools.models import thaw_json

 _Role = Literal["system", "user", "assistant", "tool"]
 _APPLICATION_ROLES = frozenset({"system", "user", "assistant", "tool"})


+@dataclass(frozen=True)
+class PlannedContext:
+    view: ContextView
+    messages: tuple[dict[str, Any], ...]
+
+
 class ContextPlanner:
     def __init__(
         self,
         store: StateStore,
         models: LiteLLMGateway,
         *,
         model: str,
         model_window: int,
         output_reserve: int = 0,
         tool_schema_tokens: int = 0,
         safety_reserve: int = 0,
         policy: CompactionPolicy | None = None,
+        recent_messages: int = 2,
+        tool_preview_bytes: int = 4_096,
         _token_counter: TokenCounter = default_token_counter,
     ) -> None:
+        if (
+            isinstance(recent_messages, bool)
+            or not isinstance(recent_messages, int)
+            or recent_messages < 0
+        ):
+            raise ValueError("recent_messages must be a non-negative integer")
+        if (
+            isinstance(tool_preview_bytes, bool)
+            or not isinstance(tool_preview_bytes, int)
+            or tool_preview_bytes < 0
+        ):
+            raise ValueError("tool_preview_bytes must be a non-negative integer")
         self._store = store
         self._model = model
         self._model_window = model_window
         self._output_reserve = output_reserve
         self._tool_schema_tokens = tool_schema_tokens
         self._safety_reserve = safety_reserve
         self._policy = policy or CompactionPolicy()
+        self._recent_messages = recent_messages
+        self._tool_preview_bytes = tool_preview_bytes
         self._token_counter = _token_counter
         self._compactor = ContextCompactor(models, model=model)
+        self._retrieval = ContextRetrieval(store)
+
+    async def prepare(
+        self,
+        *,
+        session_id: str,
+        run_id: str,
+        checkpoint: RunCheckpoint,
+        config: ContextRuntimeConfig,
+    ) -> PlannedContext:
+        if checkpoint.session_id != session_id or checkpoint.run_id != run_id:
+            raise AgentSDKError(
+                ErrorCode.INVALID_STATE,
+                "context checkpoint owner mismatch",
+                retryable=False,
+            )
+        session = await self._store.get_snapshot("session", session_id)
+        if session is None:
+            raise AgentSDKError(
+                ErrorCode.NOT_FOUND,
+                "session not found",
+                retryable=False,
+            )
+        try:
+            stored_events = await self._store.read_events(
+                after_cursor=0,
+                session_id=session_id,
+            )
+            sources = extract_sources(stored_events, checkpoint)
+        except AgentSDKError:
+            raise
+        except Exception as error:
+            raise AgentSDKError(
+                ErrorCode.INVALID_STATE,
+                "context sources are invalid",
+                retryable=False,
+            ) from error
+        items = self._context_items(sources)
+        budget = self._budget_messages(
+            [
+                cast(dict[str, Any], thaw_json(source.message))
+                for source in sources
+            ]
+        )
+        if budget.available_input_tokens <= 0:
+            raise AgentSDKError(
+                ErrorCode.INVALID_STATE,
+                "context budget has no input capacity",
+                retryable=False,
+            )
+        recommended = config.policy.recommend(budget.watermark_ratio)
+        requested = self._requested_level(config.force_level, recommended)
+        if not config.allow_lossy and requested in {
+            CompactionLevel.L3,
+            CompactionLevel.L4,
+        }:
+            requested = CompactionLevel.L2
+
+        if requested in {
+            CompactionLevel.L0,
+            CompactionLevel.L1,
+            CompactionLevel.L2,
+        }:
+            rendered = render_level(
+                requested,
+                sources,
+                recent_messages=config.recent_messages,
+                tool_preview_bytes=config.tool_preview_bytes,
+            )
+            view = await self._persist_runtime_deterministic(
+                session_id=session_id,
+                rendered=rendered,
+                budget=budget,
+                recommended=recommended,
+                applied=requested,
+            )
+            messages = self._strategy_messages(rendered)
+            await self._record_over_budget_if_needed(view)
+            return PlannedContext(view=view, messages=messages)
+
+        retained = {source.ref for source in sources if source.protected}
+        retained.update(source.ref for source in sources[-config.recent_messages :])
+        if requested is CompactionLevel.L3:
+            result = await self._compactor.summarize(items, retained)
+            prior_refs: tuple[str, ...] = ()
+        else:
+            records = await self._retrieval.list_capsule_records(
+                session_id=session_id
+            )
+            prior_refs = tuple(record[0] for record in records)
+            result = await self._compactor.rebase(
+                tuple(record[1] for record in records),
+                items,
+                retained,
+                capsule_ids=prior_refs,
+            )
+        if result.capsule is not None:
+            estimated_tokens = self._estimate_runtime_compacted_tokens(
+                sources,
+                retained,
+                result.capsule,
+            )
+            if estimated_tokens <= budget.available_input_tokens:
+                view = await self._persist_compacted(
+                    session_id=session_id,
+                    source=items,
+                    retained=retained,
+                    prior_refs=prior_refs,
+                    capsule=result.capsule,
+                    usage=result.usage,
+                    budget=budget,
+                    recommended=recommended,
+                    applied=requested,
+                    estimated_tokens=estimated_tokens,
+                )
+                return PlannedContext(
+                    view=view,
+                    messages=self._compacted_messages(
+                        sources,
+                        retained,
+                        result.capsule,
+                    ),
+                )
+
+        fallback = render_level(
+            CompactionLevel.L2,
+            sources,
+            recent_messages=config.recent_messages,
+            tool_preview_bytes=config.tool_preview_bytes,
+        )
+        view = await self._persist_fallback(
+            session_id=session_id,
+            rendered=fallback,
+            usage=result.usage,
+            budget=budget,
+            recommended=recommended,
+            requested=requested,
+        )
+        messages = self._strategy_messages(fallback)
+        await self._record_over_budget_if_needed(view)
+        return PlannedContext(view=view, messages=messages)
+
+    @staticmethod
+    def _context_items(
+        sources: tuple[SourceMessage, ...],
+    ) -> tuple[ContextItem, ...]:
+        items: list[ContextItem] = []
+        for cursor, source in enumerate(sources, start=1):
+            message = thaw_json(source.message)
+            assert isinstance(message, dict)
+            content = message.get("content")
+            if not isinstance(content, str):
+                content = json.dumps(
+                    message,
+                    ensure_ascii=False,
+                    allow_nan=False,
+                    sort_keys=True,
+                    separators=(",", ":"),
+                )
+            items.append(
+                ContextItem(
+                    event_id=source.ref,
+                    cursor=cursor,
+                    event_type=source.event_type,
+                    role=source.role,
+                    content=content,
+                )
+            )
+        return tuple(items)
+
+    @staticmethod
+    def _strategy_messages(
+        rendered: StrategyResult,
+    ) -> tuple[dict[str, Any], ...]:
+        messages: list[dict[str, Any]] = []
+        for source in rendered.items:
+            message = thaw_json(source.message)
+            assert isinstance(message, dict)
+            messages.append(message)
+        return tuple(messages)
+
+    @staticmethod
+    def _compacted_messages(
+        sources: tuple[SourceMessage, ...],
+        retained: set[str],
+        capsule: ContextCapsule,
+    ) -> tuple[dict[str, Any], ...]:
+        messages: list[dict[str, Any]] = [
+            {
+                "role": "assistant",
+                "content": json.dumps(
+                    capsule.model_dump(mode="json"),
+                    ensure_ascii=False,
+                    allow_nan=False,
+                    sort_keys=True,
+                    separators=(",", ":"),
+                ),
+            }
+        ]
+        for source in sources:
+            if source.ref not in retained:
+                continue
+            message = thaw_json(source.message)
+            assert isinstance(message, dict)
+            messages.append(message)
+        return tuple(messages)
+
+    def _estimate_runtime_compacted_tokens(
+        self,
+        sources: tuple[SourceMessage, ...],
+        retained: set[str],
+        capsule: ContextCapsule,
+    ) -> int:
+        return self._estimate_messages(
+            list(self._compacted_messages(sources, retained, capsule))
+        )
+
+    async def _persist_runtime_deterministic(
+        self,
+        *,
+        session_id: str,
+        rendered: StrategyResult,
+        budget: ContextBudget,
+        recommended: CompactionLevel,
+        applied: CompactionLevel,
+    ) -> ContextView:
+        view = self._rendered_view(
+            session_id=session_id,
+            rendered=rendered,
+            budget=budget,
+            recommended=recommended,
+            applied=applied,
+            fallback_from=None,
+        )
+        await self._persist_view(view, usage=None)
+        return view
+
+    async def _record_over_budget_if_needed(self, view: ContextView) -> None:
+        budget = view.budget
+        if budget is None or view.estimated_tokens <= budget.available_input_tokens:
+            return
+        sequence = 3 if view.fallback_from is not None else 2
+        await self._commit(
+            CommitBatch(
+                events=(
+                    self._event(
+                        view,
+                        sequence=sequence,
+                        event_type="context.over_budget",
+                        payload={
+                            "view_id": view.view_id,
+                            "applied_level": view.applied_level.value,
+                            "estimated_tokens": view.estimated_tokens,
+                            "available_input_tokens": budget.available_input_tokens,
+                        },
+                    ),
+                ),
+                preconditions=(
+                    SnapshotPrecondition(
+                        "context_view",
+                        view.view_id,
+                        session_id=view.session_id,
+                    ),
+                ),
+            )
+        )
+
+    def _budget_messages(
+        self,
+        messages: list[dict[str, Any]],
+    ) -> ContextBudget:
+        projected = self._estimate_messages(messages) if messages else 0
+        return ContextBudget.calculate(
+            model_window=self._model_window,
+            output_reserve=self._output_reserve,
+            tool_schema_tokens=self._tool_schema_tokens,
+            safety_reserve=self._safety_reserve,
+            projected_source_tokens=projected,
+        )

     async def build(
         self,
         session_id: str,
         *,
         force_level: CompactionLevel | str | None = None,
         protected_event_ids: Iterable[str] = (),
+        allow_lossy: bool = True,
     ) -> ContextView:
         session = await self._store.get_snapshot("session", session_id)
         if session is None:
             raise AgentSDKError(
                 ErrorCode.NOT_FOUND,
                 "session not found",
                 retryable=False,
             )
         if session.get("session_id") != session_id:
             raise AgentSDKError(
@@ -99,60 +410,111 @@ class ContextPlanner:
             protected.add(latest_user.event_id)

         budget = self._budget(source)
         if budget.available_input_tokens <= 0:
             raise AgentSDKError(
                 ErrorCode.INVALID_STATE,
                 "context budget has no input capacity",
                 retryable=False,
             )
         recommended = self._policy.recommend(budget.watermark_ratio)
-        requested = self._forced_level(force_level)
-        if requested in (CompactionLevel.L1, CompactionLevel.L2):
+        requested = self._requested_level(force_level, recommended)
+        if not isinstance(allow_lossy, bool):
             raise AgentSDKError(
                 ErrorCode.INVALID_STATE,
-                "compaction level is not implemented",
+                "allow_lossy must be a boolean",
                 retryable=False,
             )
-        if requested in (CompactionLevel.L3, CompactionLevel.L4) and not source:
+        if not allow_lossy and requested in {
+            CompactionLevel.L3,
+            CompactionLevel.L4,
+        }:
+            requested = CompactionLevel.L2
+        if requested in {CompactionLevel.L3, CompactionLevel.L4} and not source:
             raise AgentSDKError(
                 ErrorCode.INVALID_STATE,
                 "context sources are empty",
                 retryable=False,
             )

-        if requested in (CompactionLevel.L3, CompactionLevel.L4):
-            result = await self._compactor.compact(source, protected)
-            if result.capsule is not None:
-                return await self._persist_compacted(
-                    session_id=session_id,
-                    source=source,
-                    protected=protected,
-                    capsule=result.capsule,
-                    usage=result.usage,
-                    budget=budget,
-                    recommended=recommended,
-                    applied=requested,
-                )
+        sources = self._source_messages(source, protected)
+        if requested in {
+            CompactionLevel.L0,
+            CompactionLevel.L1,
+            CompactionLevel.L2,
+        }:
+            rendered = self._render(requested, sources)
+            return await self._persist_deterministic(
+                session_id=session_id,
+                rendered=rendered,
+                budget=budget,
+                recommended=recommended,
+                applied=requested,
+            )
+
+        retained = set(protected)
+        if self._recent_messages:
+            retained.update(
+                item.event_id for item in source[-self._recent_messages :]
+            )
+        if requested is CompactionLevel.L3:
+            result = await self._compactor.summarize(source, retained)
+            prior_refs: tuple[str, ...] = ()
+        else:
+            records = await self._retrieval.list_capsule_records(
+                session_id=session_id
+            )
+            capsule_ids = tuple(record[0] for record in records)
+            capsules = tuple(record[1] for record in records)
+            result = await self._compactor.rebase(
+                capsules,
+                source,
+                retained,
+                capsule_ids=capsule_ids,
+            )
+            prior_refs = capsule_ids
+        if result.capsule is None:
+            fallback = self._render(CompactionLevel.L2, sources)
             return await self._persist_fallback(
                 session_id=session_id,
-                source=source,
+                rendered=fallback,
                 usage=result.usage,
                 budget=budget,
                 recommended=recommended,
                 requested=requested,
             )
-        return await self._persist_l0(
+        estimated_tokens = self._estimate_compacted_tokens(
+            source,
+            retained,
+            result.capsule,
+        )
+        if estimated_tokens > budget.available_input_tokens:
+            fallback = self._render(CompactionLevel.L2, sources)
+            return await self._persist_fallback(
+                session_id=session_id,
+                rendered=fallback,
+                usage=result.usage,
+                budget=budget,
+                recommended=recommended,
+                requested=requested,
+            )
+        return await self._persist_compacted(
             session_id=session_id,
             source=source,
+            retained=retained,
+            prior_refs=prior_refs,
+            capsule=result.capsule,
+            usage=result.usage,
             budget=budget,
             recommended=recommended,
+            applied=requested,
+            estimated_tokens=estimated_tokens,
         )

     def _budget(self, source: tuple[ContextItem, ...]) -> ContextBudget:
         messages: list[dict[str, Any]] = [
             {"role": item.role, "content": item.content} for item in source
         ]
         try:
             baseline = ContextBudget.calculate(
                 model_window=self._model_window,
                 output_reserve=self._output_reserve,
@@ -161,58 +523,43 @@ class ContextPlanner:
                 projected_source_tokens=0,
             )
         except (TypeError, ValueError, ValidationError) as error:
             raise AgentSDKError(
                 ErrorCode.INVALID_STATE,
                 "context budget configuration invalid",
                 retryable=False,
             ) from error
         if baseline.available_input_tokens <= 0:
             return baseline
-        try:
-            projected = self._token_counter(
-                model=self._model,
-                messages=deepcopy(messages),
-            )
-            if (
-                isinstance(projected, bool)
-                or not isinstance(projected, int)
-                or projected < 0
-            ):
-                raise ValueError("token counter returned an invalid count")
-        except Exception as error:
-            raise AgentSDKError(
-                ErrorCode.INTERNAL,
-                "context token estimation failed",
-                retryable=False,
-            ) from error
+        projected = self._estimate_messages(messages)
         try:
             return ContextBudget.calculate(
                 model_window=self._model_window,
                 output_reserve=self._output_reserve,
                 tool_schema_tokens=self._tool_schema_tokens,
                 safety_reserve=self._safety_reserve,
                 projected_source_tokens=projected,
             )
         except Exception as error:
             raise AgentSDKError(
                 ErrorCode.INTERNAL,
                 "context token estimation failed",
                 retryable=False,
             ) from error

     @staticmethod
-    def _forced_level(
+    def _requested_level(
         force_level: CompactionLevel | str | None,
+        recommended: CompactionLevel,
     ) -> CompactionLevel:
         if force_level is None:
-            return CompactionLevel.L0
+            return recommended
         try:
             return CompactionLevel(force_level)
         except ValueError as error:
             raise AgentSDKError(
                 ErrorCode.INVALID_STATE,
                 "unknown compaction level",
                 retryable=False,
             ) from error

     @classmethod
@@ -261,66 +608,125 @@ class ContextPlanner:
             "role",
             "content",
         }:
             return None
         role = payload.get("role")
         content = payload.get("content")
         if role not in _APPLICATION_ROLES or not isinstance(content, str):
             return None
         return cast(_Role, role), content

+    @staticmethod
+    def _source_messages(
+        source: tuple[ContextItem, ...],
+        protected: set[str],
+    ) -> tuple[SourceMessage, ...]:
+        return tuple(
+            SourceMessage(
+                ref=item.event_id,
+                role=item.role,
+                message={"role": item.role, "content": item.content},
+                event_type=item.event_type,
+                protected=item.event_id in protected,
+            )
+            for item in source
+        )
+
+    def _render(
+        self,
+        level: CompactionLevel,
+        source: tuple[SourceMessage, ...],
+    ) -> StrategyResult:
+        return render_level(
+            level,
+            source,
+            recent_messages=self._recent_messages,
+            tool_preview_bytes=self._tool_preview_bytes,
+        )
+
+    async def _persist_deterministic(
+        self,
+        *,
+        session_id: str,
+        rendered: StrategyResult,
+        budget: ContextBudget,
+        recommended: CompactionLevel,
+        applied: CompactionLevel,
+    ) -> ContextView:
+        view = self._rendered_view(
+            session_id=session_id,
+            rendered=rendered,
+            budget=budget,
+            recommended=recommended,
+            applied=applied,
+            fallback_from=None,
+        )
+        await self._persist_view(view, usage=None)
+        return view
+
     async def _persist_compacted(
         self,
         *,
         session_id: str,
         source: tuple[ContextItem, ...],
-        protected: set[str],
+        retained: set[str],
+        prior_refs: tuple[str, ...],
         capsule: ContextCapsule,
         usage: UsageReported,
         budget: ContextBudget,
         recommended: CompactionLevel,
         applied: CompactionLevel,
+        estimated_tokens: int,
     ) -> ContextView:
         view_id = new_id("view")
         capsule_id = new_id("cap")
         message_refs = tuple(
-            item.event_id for item in source if item.event_id in protected
+            item.event_id for item in source if item.event_id in retained
+        )
+        current_refs = tuple(item.event_id for item in source)
+        source_refs = tuple(dict.fromkeys((*prior_refs, *current_refs)))
+        transformed = tuple(
+            f"{applied.value.lower()}:{ref}"
+            for ref in source_refs
+            if ref not in message_refs
         )
         view = ContextView(
             view_id=view_id,
             session_id=session_id,
             message_refs=message_refs,
             capsule_id=capsule_id,
-            estimated_tokens=self._estimate_compacted_tokens(
-                source,
-                protected,
-                capsule,
-            ),
+            estimated_tokens=estimated_tokens,
             recommended_level=recommended,
             applied_level=applied,
             budget=budget,
+            source_refs=source_refs,
+            transformations=transformed,
         )
         events = (
             self._event(
                 view,
                 sequence=1,
                 event_type="context.compaction.completed",
                 payload={
                     "view_id": view_id,
                     "capsule_id": capsule_id,
                     "level": applied.value,
                     "model": self._model,
                     "budget": budget.model_dump(mode="json"),
+                    "estimated_tokens": view.estimated_tokens,
+                    "message_refs": list(view.message_refs),
+                    "source_refs": list(view.source_refs),
+                    "transformations": list(view.transformations),
                     "usage": usage.to_payload(),
                 },
             ),
-            self._view_event(view, sequence=2),
+            self._view_event(view, sequence=2, usage=usage),
         )
         snapshots = (
             SnapshotWrite(
                 "context_capsule",
                 capsule_id,
                 session_id,
                 1,
                 {
                     "session_id": session_id,
                     "capsule": capsule.model_dump(mode="json"),
@@ -331,123 +737,159 @@ class ContextPlanner:
                 view_id,
                 session_id,
                 1,
                 view.model_dump(mode="json"),
             ),
         )
         await self._commit(
             CommitBatch(
                 events=events,
                 snapshots=snapshots,
-                preconditions=(
-                    SnapshotPrecondition("session", session_id),
-                ),
+                preconditions=(SnapshotPrecondition("session", session_id),),
             )
         )
         return view

     async def _persist_fallback(
         self,
         *,
         session_id: str,
-        source: tuple[ContextItem, ...],
+        rendered: StrategyResult,
         usage: UsageReported,
         budget: ContextBudget,
         recommended: CompactionLevel,
         requested: CompactionLevel,
     ) -> ContextView:
-        view = self._raw_view(session_id, source, budget, recommended)
+        view = self._rendered_view(
+            session_id=session_id,
+            rendered=rendered,
+            budget=budget,
+            recommended=recommended,
+            applied=CompactionLevel.L2,
+            fallback_from=requested,
+        )
         events = (
             self._event(
                 view,
                 sequence=1,
                 event_type="context.compaction.failed",
                 payload={
                     "view_id": view.view_id,
                     "requested_level": requested.value,
+                    "applied_level": CompactionLevel.L2.value,
                     "code": "context_compaction_failed",
                     "budget": budget.model_dump(mode="json"),
+                    "estimated_tokens": view.estimated_tokens,
+                    "message_refs": list(view.message_refs),
+                    "source_refs": list(view.source_refs),
+                    "transformations": list(view.transformations),
                     "usage": usage.to_payload(),
                 },
             ),
-            self._view_event(view, sequence=2),
+            self._view_event(view, sequence=2, usage=usage),
         )
         await self._commit(
             CommitBatch(
                 events=events,
                 snapshots=(
                     SnapshotWrite(
                         "context_view",
                         view.view_id,
                         session_id,
                         1,
                         view.model_dump(mode="json"),
                     ),
                 ),
-                preconditions=(
-                    SnapshotPrecondition("session", session_id),
-                ),
+                preconditions=(SnapshotPrecondition("session", session_id),),
             )
         )
         return view

-    async def _persist_l0(
+    def _rendered_view(
         self,
         *,
         session_id: str,
-        source: tuple[ContextItem, ...],
+        rendered: StrategyResult,
         budget: ContextBudget,
         recommended: CompactionLevel,
+        applied: CompactionLevel,
+        fallback_from: CompactionLevel | None,
     ) -> ContextView:
-        view = self._raw_view(session_id, source, budget, recommended)
+        messages = []
+        for item in rendered.items:
+            message = thaw_json(item.message)
+            assert isinstance(message, dict)
+            messages.append(message)
+        return ContextView(
+            view_id=new_id("view"),
+            session_id=session_id,
+            message_refs=tuple(item.ref for item in rendered.items),
+            capsule_id=None,
+            estimated_tokens=self._estimate_messages(messages),
+            recommended_level=recommended,
+            applied_level=applied,
+            budget=budget,
+            source_refs=rendered.source_refs,
+            transformations=rendered.transformations,
+            fallback_from=fallback_from,
+        )
+
+    async def _persist_view(
+        self,
+        view: ContextView,
+        *,
+        usage: UsageReported | None,
+    ) -> None:
         await self._commit(
             CommitBatch(
-                events=(self._view_event(view, sequence=1),),
+                events=(self._view_event(view, sequence=1, usage=usage),),
                 snapshots=(
                     SnapshotWrite(
                         "context_view",
                         view.view_id,
-                        session_id,
+                        view.session_id,
                         1,
                         view.model_dump(mode="json"),
                     ),
                 ),
                 preconditions=(
-                    SnapshotPrecondition("session", session_id),
+                    SnapshotPrecondition("session", view.session_id),
                 ),
             )
         )
-        return view

     def _estimate_compacted_tokens(
         self,
         source: tuple[ContextItem, ...],
-        protected: set[str],
+        retained: set[str],
         capsule: ContextCapsule,
     ) -> int:
         messages: list[dict[str, Any]] = [
             {
                 "role": "assistant",
                 "content": json.dumps(
                     capsule.model_dump(mode="json"),
                     ensure_ascii=False,
                     allow_nan=False,
                     sort_keys=True,
                     separators=(",", ":"),
                 ),
             }
         ]
         messages.extend(
             {"role": item.role, "content": item.content}
             for item in source
-            if item.event_id in protected
+            if item.event_id in retained
         )
+        return self._estimate_messages(messages)
+
+    def _estimate_messages(self, messages: list[dict[str, Any]]) -> int:
         try:
             count = self._token_counter(
                 model=self._model,
                 messages=deepcopy(messages),
             )
             if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                 raise ValueError("token counter returned an invalid count")
         except Exception as error:
             raise AgentSDKError(
                 ErrorCode.INTERNAL,
@@ -459,68 +901,71 @@ class ContextPlanner:
     async def _commit(self, batch: CommitBatch) -> None:
         failure: AgentSDKError | None = None
         try:
             await self._store.commit(batch)
         except SnapshotPreconditionError:
             failure = AgentSDKError(
                 ErrorCode.NOT_FOUND,
                 "context session no longer exists",
                 retryable=False,
             )
-        except Exception as error:
-            del error
+        except Exception:
             failure = AgentSDKError(
                 ErrorCode.INTERNAL,
                 "context persistence failed",
                 retryable=False,
             )
         if failure is not None:
             raise failure

-    @staticmethod
-    def _raw_view(
-        session_id: str,
-        source: tuple[ContextItem, ...],
-        budget: ContextBudget,
-        recommended: CompactionLevel,
-    ) -> ContextView:
-        return ContextView(
-            view_id=new_id("view"),
-            session_id=session_id,
-            message_refs=tuple(item.event_id for item in source),
-            capsule_id=None,
-            estimated_tokens=budget.projected_source_tokens,
-            recommended_level=recommended,
-            applied_level=CompactionLevel.L0,
-            budget=budget,
-        )
-
     @staticmethod
     def _event(
         view: ContextView,
         *,
         sequence: int,
         event_type: str,
         payload: dict[str, Any],
     ) -> EventEnvelope:
         return EventEnvelope.new(
             type=event_type,
             session_id=view.session_id,
             run_id=view.view_id,
             sequence=sequence,
             payload=payload,
         )

     @classmethod
-    def _view_event(cls, view: ContextView, *, sequence: int) -> EventEnvelope:
+    def _view_event(
+        cls,
+        view: ContextView,
+        *,
+        sequence: int,
+        usage: UsageReported | None,
+    ) -> EventEnvelope:
         return cls._event(
             view,
             sequence=sequence,
             event_type="context.view.created",
             payload={
                 "view_id": view.view_id,
                 "capsule_id": view.capsule_id,
                 "recommended_level": view.recommended_level.value,
                 "applied_level": view.applied_level.value,
+                "fallback_from": (
+                    view.fallback_from.value
+                    if view.fallback_from is not None
+                    else None
+                ),
                 "estimated_tokens": view.estimated_tokens,
+                "budget": (
+                    view.budget.model_dump(mode="json")
+                    if view.budget is not None
+                    else None
+                ),
+                "message_refs": list(view.message_refs),
+                "source_refs": list(view.source_refs),
+                "transformations": list(view.transformations),
+                "compaction_usage": (
+                    usage.to_payload() if usage is not None else None
+                ),
             },
         )
diff --git a/src/agent_sdk/context/rendering.py b/src/agent_sdk/context/rendering.py
new file mode 100644
index 0000000..469540c
--- /dev/null
+++ b/src/agent_sdk/context/rendering.py
@@ -0,0 +1,27 @@
+from agent_sdk.context.models import CompactionLevel, SourceMessage
+from agent_sdk.context.strategies import (
+    StrategyResult,
+    apply_l0,
+    apply_l1,
+    apply_l2,
+)
+
+
+def render_level(
+    level: CompactionLevel,
+    sources: tuple[SourceMessage, ...],
+    *,
+    recent_messages: int,
+    tool_preview_bytes: int,
+) -> StrategyResult:
+    if level is CompactionLevel.L0:
+        return apply_l0(sources)
+    if level is CompactionLevel.L1:
+        return apply_l1(sources, tool_preview_bytes=tool_preview_bytes)
+    if level is CompactionLevel.L2:
+        return apply_l2(
+            sources,
+            recent_messages=recent_messages,
+            tool_preview_bytes=tool_preview_bytes,
+        )
+    raise ValueError("deterministic renderer supports L0-L2 only")
diff --git a/src/agent_sdk/context/retrieval.py b/src/agent_sdk/context/retrieval.py
index f9f9584..ad035a8 100644
--- a/src/agent_sdk/context/retrieval.py
+++ b/src/agent_sdk/context/retrieval.py
@@ -55,31 +55,102 @@ class ContextRetrieval:
                 "stored context capsule is invalid",
                 retryable=False,
             ) from error

     async def read_sources(
         self,
         capsule_id: str,
         *,
         session_id: str,
     ) -> tuple[StoredEvent, ...]:
-        capsule = await self.get_capsule(capsule_id, session_id=session_id)
         try:
             events = await self._store.read_events(
                 after_cursor=0,
                 session_id=session_id,
             )
         except Exception as error:
             raise AgentSDKError(
                 ErrorCode.INTERNAL,
                 "context retrieval failed",
                 retryable=False,
             ) from error
         by_id = {stored.event.event_id: stored for stored in events}
+        resolved: list[StoredEvent] = []
+        seen_events: set[str] = set()
+        active_capsules: set[str] = set()
+
+        async def resolve(ref: str) -> None:
+            event = by_id.get(ref)
+            if event is not None:
+                if ref not in seen_events:
+                    resolved.append(event)
+                    seen_events.add(ref)
+                return
+            if ref in active_capsules:
+                raise AgentSDKError(
+                    ErrorCode.INTERNAL,
+                    "stored context capsule cycle detected",
+                    retryable=False,
+                )
+            active_capsules.add(ref)
+            try:
+                nested = await self.get_capsule(ref, session_id=session_id)
+                for nested_ref in nested.source_event_ids:
+                    await resolve(nested_ref)
+            except AgentSDKError as error:
+                if error.code is ErrorCode.NOT_FOUND:
+                    raise AgentSDKError(
+                        ErrorCode.NOT_FOUND,
+                        "context source not found",
+                        retryable=False,
+                    ) from error
+                raise
+            finally:
+                active_capsules.remove(ref)
+
+        capsule = await self.get_capsule(capsule_id, session_id=session_id)
+        for source_ref in capsule.source_event_ids:
+            await resolve(source_ref)
+        return tuple(resolved)
+
+    async def list_capsule_records(
+        self,
+        *,
+        session_id: str,
+    ) -> tuple[tuple[str, ContextCapsule], ...]:
         try:
-            return tuple(by_id[event_id] for event_id in capsule.source_event_ids)
-        except KeyError as error:
+            events = await self._store.read_events(
+                after_cursor=0,
+                session_id=session_id,
+            )
+        except Exception as error:
             raise AgentSDKError(
-                ErrorCode.NOT_FOUND,
-                "context source not found",
+                ErrorCode.INTERNAL,
+                "context retrieval failed",
                 retryable=False,
             ) from error
+        capsule_ids: list[str] = []
+        seen: set[str] = set()
+        for stored in events:
+            if stored.event.type != "context.compaction.completed":
+                continue
+            capsule_id = stored.event.payload.get("capsule_id")
+            if (
+                not isinstance(capsule_id, str)
+                or not capsule_id
+                or capsule_id in seen
+            ):
+                continue
+            capsule_ids.append(capsule_id)
+            seen.add(capsule_id)
+        records: list[tuple[str, ContextCapsule]] = []
+        for capsule_id in capsule_ids:
+            records.append(
+                (
+                    capsule_id,
+                    await self.get_capsule(
+                        capsule_id,
+                        session_id=session_id,
+                    ),
+                )
+            )
+        return tuple(records)
diff --git a/src/agent_sdk/context/sources.py b/src/agent_sdk/context/sources.py
new file mode 100644
index 0000000..8267604
--- /dev/null
+++ b/src/agent_sdk/context/sources.py
@@ -0,0 +1,200 @@
+from __future__ import annotations
+
+from collections import defaultdict, deque
+from collections.abc import Iterable, Mapping
+from typing import Any, Literal, cast
+
+from agent_sdk.context.models import SourceMessage
+from agent_sdk.runtime.reconciliation import RunCheckpoint
+from agent_sdk.storage.base import StoredEvent
+
+type _Role = Literal["system", "user", "assistant", "tool"]
+
+
+def checkpoint_ref(run_id: str, checkpoint_version: int, index: int) -> str:
+    if not isinstance(run_id, str) or not run_id:
+        raise ValueError("run_id must be a nonempty string")
+    if (
+        isinstance(checkpoint_version, bool)
+        or not isinstance(checkpoint_version, int)
+        or checkpoint_version < 1
+    ):
+        raise ValueError("checkpoint_version must be a positive integer")
+    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
+        raise ValueError("checkpoint message index must be a non-negative integer")
+    return f"checkpoint:{run_id}:{checkpoint_version}:{index}"
+
+
+def extract_sources(
+    events: Iterable[StoredEvent],
+    checkpoint: RunCheckpoint,
+    *,
+    protected_event_ids: Iterable[str] = (),
+    unresolved_event_ids: Iterable[str] = (),
+    active_state_summaries: Iterable[SourceMessage] = (),
+) -> tuple[SourceMessage, ...]:
+    ordered = tuple(sorted(events, key=lambda item: item.cursor))
+    protected_refs = set(protected_event_ids) | set(unresolved_event_ids)
+    historical: list[SourceMessage] = []
+    current_events: list[StoredEvent] = []
+    for stored in ordered:
+        if stored.event.run_id == checkpoint.run_id:
+            current_events.append(stored)
+            continue
+        message = _historical_message(stored)
+        if message is None:
+            continue
+        historical.append(
+            SourceMessage(
+                ref=stored.event.event_id,
+                role=cast(_Role, message.get("role")),
+                message=message,
+                event_type=stored.event.type,
+                protected=stored.event.event_id in protected_refs,
+            )
+        )
+
+    dumped_messages = checkpoint.model_dump(mode="json")["messages"]
+    checkpoint_messages = cast(tuple[dict[str, Any], ...], tuple(dumped_messages))
+    correlated = _correlated_checkpoint_refs(
+        tuple(current_events),
+        checkpoint_messages,
+    )
+    latest_user = max(
+        (
+            index
+            for index, message in enumerate(checkpoint_messages)
+            if message.get("role") == "user"
+        ),
+        default=-1,
+    )
+    current: list[SourceMessage] = []
+    for index, message in enumerate(checkpoint_messages):
+        ref, event_type = correlated.get(
+            index,
+            (
+                checkpoint_ref(
+                    checkpoint.run_id,
+                    checkpoint.checkpoint_version,
+                    index,
+                ),
+                "checkpoint.message",
+            ),
+        )
+        role = message.get("role")
+        protocol_message = role == "tool" or (
+            role == "assistant" and "tool_calls" in message
+        )
+        current.append(
+            SourceMessage(
+                ref=ref,
+                role=cast(_Role, role),
+                message=message,
+                event_type=event_type,
+                protected=(
+                    index == latest_user
+                    or protocol_message
+                    or ref in protected_refs
+                ),
+                current=True,
+            )
+        )
+
+    states = tuple(
+        state.model_copy(update={"protected": True})
+        for state in active_state_summaries
+    )
+    result = (*historical, *current, *states)
+    refs = tuple(item.ref for item in result)
+    if len(refs) != len(set(refs)):
+        raise ValueError("source message refs must be unique")
+    if protected_refs - set(refs):
+        raise ValueError("protected context source not found")
+    return result
+
+
+def _historical_message(stored: StoredEvent) -> Mapping[str, Any] | None:
+    event = stored.event
+    payload = event.payload
+    if event.type == "run.created":
+        content = payload.get("user_input")
+        return (
+            {"role": "user", "content": content}
+            if isinstance(content, str)
+            else None
+        )
+    if event.type == "model.text.delta":
+        content = payload.get("text")
+        return (
+            {"role": "assistant", "content": content}
+            if isinstance(content, str)
+            else None
+        )
+    if event.type == "tool.call.completed":
+        content = payload.get("content")
+        call_id = payload.get("call_id")
+        name = payload.get("tool_name")
+        if (
+            isinstance(content, str)
+            and isinstance(call_id, str)
+            and isinstance(name, str)
+        ):
+            return {
+                "role": "tool",
+                "tool_call_id": call_id,
+                "name": name,
+                "content": content,
+            }
+        return None
+    if event.type == "context.message.appended":
+        role = payload.get("role")
+        if isinstance(role, str) and "content" in payload:
+            return payload
+    return None
+
+
+def _correlated_checkpoint_refs(
+    current_events: tuple[StoredEvent, ...],
+    checkpoint_messages: tuple[dict[str, Any], ...],
+) -> dict[int, tuple[str, str]]:
+    run_created = next(
+        (
+            stored.event
+            for stored in current_events
+            if stored.event.type == "run.created"
+        ),
+        None,
+    )
+    model_completed = iter(
+        (stored.event.event_id, stored.event.type)
+        for stored in current_events
+        if stored.event.type == "model.call.completed"
+    )
+    tool_completed: dict[str, deque[tuple[str, str]]] = defaultdict(deque)
+    for stored in current_events:
+        call_id = stored.event.payload.get("call_id")
+        if stored.event.type == "tool.call.completed" and isinstance(call_id, str):
+            tool_completed[call_id].append(
+                (stored.event.event_id, stored.event.type)
+            )
+    refs: dict[int, tuple[str, str]] = {}
+    user_correlated = False
+    for index, message in enumerate(checkpoint_messages):
+        role = message.get("role")
+        if (
+            role == "user"
+            and not user_correlated
+            and run_created is not None
+            and message.get("content") == run_created.payload.get("user_input")
+        ):
+            refs[index] = (run_created.event_id, run_created.type)
+            user_correlated = True
+        elif role == "assistant":
+            correlated = next(model_completed, None)
+            if correlated is not None:
+                refs[index] = correlated
+        elif role == "tool":
+            call_id = message.get("tool_call_id")
+            if isinstance(call_id, str) and tool_completed[call_id]:
+                refs[index] = tool_completed[call_id].popleft()
+    return refs
diff --git a/src/agent_sdk/context/strategies.py b/src/agent_sdk/context/strategies.py
new file mode 100644
index 0000000..0708375
--- /dev/null
+++ b/src/agent_sdk/context/strategies.py
@@ -0,0 +1,230 @@
+from __future__ import annotations
+
+import hashlib
+import json
+import math
+from dataclasses import dataclass
+from typing import Any, Never
+
+from agent_sdk.context.models import SourceMessage
+from agent_sdk.tools.models import thaw_json
+
+
+@dataclass(frozen=True)
+class StrategyResult:
+    items: tuple[SourceMessage, ...]
+    source_refs: tuple[str, ...]
+    transformations: tuple[str, ...]
+
+
+def apply_l0(sources: tuple[SourceMessage, ...]) -> StrategyResult:
+    refs = _source_refs(sources)
+    return StrategyResult(sources, refs, ())
+
+
+def apply_l1(
+    sources: tuple[SourceMessage, ...],
+    *,
+    tool_preview_bytes: int,
+) -> StrategyResult:
+    refs = _source_refs(sources)
+    _validate_non_negative_int(tool_preview_bytes, "tool_preview_bytes")
+    seen_tools: dict[str, str] = {}
+    rendered: list[SourceMessage] = []
+    transformations: list[str] = []
+    for source in sources:
+        if _role(source) != "tool" or source.current or source.protected:
+            rendered.append(source)
+            continue
+        digest = _tool_digest(source.message.get("content"))
+        first_ref = seen_tools.get(digest)
+        if first_ref is not None:
+            rendered.append(
+                _replace_content(source, f"[duplicate:{first_ref}]")
+            )
+            transformations.append(f"dedupe:{source.ref}")
+            continue
+        seen_tools[digest] = source.ref
+        content = source.message.get("content")
+        if (
+            isinstance(content, str)
+            and len(content.encode("utf-8")) > tool_preview_bytes
+        ):
+            rendered.append(
+                _replace_content(
+                    source,
+                    _tool_preview(
+                        content,
+                        ref=source.ref,
+                        preview_bytes=tool_preview_bytes,
+                    ),
+                )
+            )
+            transformations.append(f"tool_preview:{source.ref}")
+            continue
+        rendered.append(source)
+    return StrategyResult(tuple(rendered), refs, tuple(transformations))
+
+
+def apply_l2(
+    sources: tuple[SourceMessage, ...],
+    *,
+    recent_messages: int,
+    tool_preview_bytes: int,
+) -> StrategyResult:
+    _validate_non_negative_int(recent_messages, "recent_messages")
+    _validate_non_negative_int(tool_preview_bytes, "tool_preview_bytes")
+    l1 = apply_l1(sources, tool_preview_bytes=tool_preview_bytes)
+    refs = l1.source_refs
+    recent_start = max(0, len(sources) - recent_messages)
+    rendered: list[SourceMessage] = []
+    transformations = list(l1.transformations)
+    for index, (source, l1_source) in enumerate(
+        zip(sources, l1.items, strict=True)
+    ):
+        if source.protected or source.current or index >= recent_start:
+            rendered.append(l1_source)
+            continue
+        role = _role(source)
+        kind = "tool_result" if role == "tool" else "exchange"
+        summary = (
+            "Tool result detail omitted; retrieve it by source reference."
+            if role == "tool"
+            else "Older completed message omitted; retrieve it by source reference."
+        )
+        outcome = {
+            "kind": kind,
+            "role": role,
+            "source_refs": [source.ref],
+            "status": "completed",
+            "summary": summary,
+        }
+        rendered.append(
+            _replace_content(
+                l1_source,
+                json.dumps(
+                    outcome,
+                    ensure_ascii=False,
+                    allow_nan=False,
+                    sort_keys=True,
+                    separators=(",", ":"),
+                ),
+            )
+        )
+        transformations.append(f"outcome:{source.ref}")
+    return StrategyResult(tuple(rendered), refs, tuple(transformations))
+
+
+def _source_refs(sources: tuple[SourceMessage, ...]) -> tuple[str, ...]:
+    refs = tuple(source.ref for source in sources)
+    if len(refs) != len(set(refs)):
+        raise ValueError("source message refs must be unique")
+    return refs
+
+
+def _validate_non_negative_int(value: int, name: str) -> None:
+    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
+        raise ValueError(f"{name} must be a non-negative integer")
+
+
+def _role(source: SourceMessage) -> str:
+    return source.role
+
+
+def _replace_content(source: SourceMessage, content: str) -> SourceMessage:
+    message = thaw_json(source.message)
+    assert isinstance(message, dict)
+    message["content"] = content
+    return source.model_copy(update={"message": message})
+
+
+def _tool_digest(content: Any) -> str:
+    if isinstance(content, str):
+        try:
+            canonical_value = json.loads(
+                content,
+                parse_constant=_reject_json_constant,
+                object_pairs_hook=_unique_object,
+            )
+            _validate_canonical_json(
+                canonical_value,
+                depth=0,
+                entries=[0],
+            )
+            canonical = json.dumps(
+                canonical_value,
+                ensure_ascii=False,
+                allow_nan=False,
+                sort_keys=True,
+                separators=(",", ":"),
+            ).encode("utf-8")
+            return hashlib.sha256(b"json\0" + canonical).hexdigest()
+        except (ValueError, RecursionError):
+            return hashlib.sha256(
+                b"raw\0" + content.encode("utf-8")
+            ).hexdigest()
+    raw = json.dumps(
+        content,
+        ensure_ascii=False,
+        allow_nan=False,
+        sort_keys=True,
+        separators=(",", ":"),
+    ).encode("utf-8")
+    return hashlib.sha256(b"raw\0" + raw).hexdigest()
+
+
+def _reject_json_constant(_: str) -> Never:
+    raise ValueError("nonstandard JSON constant")
+
+
+def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
+    value: dict[str, Any] = {}
+    for key, item in pairs:
+        if key in value:
+            raise ValueError("duplicate JSON object key")
+        value[key] = item
+    return value
+
+
+def _validate_canonical_json(
+    value: Any,
+    *,
+    depth: int,
+    entries: list[int],
+) -> None:
+    if isinstance(value, (dict, list)):
+        if depth > 32:
+            raise ValueError("JSON nesting exceeds canonicalization limit")
+        entries[0] += len(value)
+        if entries[0] > 20_000:
+            raise ValueError("JSON entries exceed canonicalization limit")
+        items = value.values() if isinstance(value, dict) else value
+        for item in items:
+            _validate_canonical_json(
+                item,
+                depth=depth + 1,
+                entries=entries,
+            )
+        return
+    if isinstance(value, float) and not math.isfinite(value):
+        raise ValueError("JSON numbers must be finite")
+
+
+def _tool_preview(content: str, *, ref: str, preview_bytes: int) -> str:
+    head_bytes = (preview_bytes + 1) // 2
+    tail_bytes = preview_bytes // 2
+    head = _utf8_prefix(content, head_bytes)
+    tail = _utf8_suffix(content, tail_bytes)
+    marker = f"[source:{ref}]"
+    return f"{head}\n…\n{tail}\n{marker}"
+
+
+def _utf8_prefix(value: str, limit: int) -> str:
+    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
+
+
+def _utf8_suffix(value: str, limit: int) -> str:
+    if limit == 0:
+        return ""
+    encoded = value.encode("utf-8")
+    return encoded[-limit:].decode("utf-8", errors="ignore")
diff --git a/src/agent_sdk/context_runtime.py b/src/agent_sdk/context_runtime.py
new file mode 100644
index 0000000..4878475
--- /dev/null
+++ b/src/agent_sdk/context_runtime.py
@@ -0,0 +1,85 @@
+from __future__ import annotations
+
+import math
+from collections.abc import Mapping
+from enum import StrEnum
+from typing import Any, Self
+
+from pydantic import BaseModel, ConfigDict, Field, StrictFloat, model_validator
+
+
+class _ContextRuntimeModel(BaseModel):
+    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)
+
+    def model_copy(
+        self,
+        *,
+        update: Mapping[str, Any] | None = None,
+        deep: bool = False,
+    ) -> Self:
+        del deep
+        data = self.model_dump(mode="json")
+        if update is not None:
+            data.update(update)
+        return type(self).model_validate(data)
+
+
+class CompactionLevel(StrEnum):
+    L0 = "L0"
+    L1 = "L1"
+    L2 = "L2"
+    L3 = "L3"
+    L4 = "L4"
+
+
+class CompactionPolicy(_ContextRuntimeModel):
+    l1_reference: StrictFloat = Field(default=0.70, gt=0, lt=1)
+    l2_selective: StrictFloat = Field(default=0.80, gt=0, lt=1)
+    l3_summary: StrictFloat = Field(default=0.90, gt=0, lt=1)
+    l4_rebase: StrictFloat = Field(default=0.96, gt=0, lt=1)
+    recovery_target: StrictFloat = Field(default=0.75, gt=0, lt=1)
+
+    @model_validator(mode="after")
+    def _validate_threshold_order(self) -> CompactionPolicy:
+        if not (
+            self.l1_reference
+            < self.l2_selective
+            < self.l3_summary
+            < self.l4_rebase
+        ):
+            raise ValueError("compaction thresholds must be strictly increasing")
+        if self.recovery_target >= self.l2_selective:
+            raise ValueError("recovery target must be below L2")
+        return self
+
+    def recommend(self, watermark_ratio: float) -> CompactionLevel:
+        if (
+            isinstance(watermark_ratio, bool)
+            or not isinstance(watermark_ratio, (int, float))
+            or not math.isfinite(watermark_ratio)
+            or watermark_ratio < 0
+        ):
+            raise ValueError("watermark ratio must be a finite non-negative number")
+        if watermark_ratio >= self.l4_rebase:
+            return CompactionLevel.L4
+        if watermark_ratio >= self.l3_summary:
+            return CompactionLevel.L3
+        if watermark_ratio >= self.l2_selective:
+            return CompactionLevel.L2
+        if watermark_ratio >= self.l1_reference:
+            return CompactionLevel.L1
+        return CompactionLevel.L0
+
+
+class ContextRuntimeConfig(_ContextRuntimeModel):
+    model_window: int = Field(default=128_000, gt=0)
+    output_reserve: int = Field(default=4_096, ge=0)
+    safety_reserve: int = Field(default=1_024, ge=0)
+    policy: CompactionPolicy = Field(default_factory=CompactionPolicy)
+    force_level: CompactionLevel | None = None
+    allow_lossy: bool = True
+    recent_messages: int = Field(default=12, ge=2)
+    tool_preview_bytes: int = Field(default=4_096, ge=256)
+
+
+__all__ = ["CompactionLevel", "CompactionPolicy", "ContextRuntimeConfig"]
diff --git a/src/agent_sdk/observability/queries.py b/src/agent_sdk/observability/queries.py
index 0eb7bfe..5d3431a 100644
--- a/src/agent_sdk/observability/queries.py
+++ b/src/agent_sdk/observability/queries.py
@@ -1,17 +1,21 @@
 from __future__ import annotations

 from enum import Enum
 from typing import Any, NoReturn

 from agent_sdk.errors import AgentSDKError, ErrorCode
-from agent_sdk.runtime.models import RunSnapshot
+from agent_sdk.runtime.models import (
+    RunCreatedEventPayload,
+    RunSnapshot,
+    run_created_event_matches,
+)
 from agent_sdk.storage.base import StateStore, StoredEvent
 from agent_sdk.storage.validation import validate_event_page, validate_latest_cursor

 from .models import (
     EventFilter,
     EventQueryResult,
     ExecutionTree,
     ExecutionTreeNode,
     ObservedEvent,
     ObservedRun,
@@ -196,40 +200,39 @@ class QueryService:
     ) -> tuple[ExecutionTreeNode, ...]:
         created = [
             stored
             for stored in stored_events
             if stored.cursor <= cursor
             and stored.event.type == "run.created"
             and stored.event.session_id == root.session_id
         ]
         descendants = {root.run_id}
         selected_ids: set[str] = set()
-        selected: list[tuple[StoredEvent, RunSnapshot]] = []
+        selected: list[tuple[StoredEvent, RunCreatedEventPayload | RunSnapshot]] = []
         pending = created
         while pending:
             progressed = False
             remaining: list[StoredEvent] = []
             for stored in pending:
-                initial = _run_snapshot(stored.event.payload)
+                initial = _run_creation(
+                    stored.event.payload,
+                    schema_version=stored.event.schema_version,
+                )
                 if isinstance(initial, _ReadFailure):
                     self._internal("failed to load execution tree")
                 if initial.run_id in descendants:
-                    if stored.event.schema_version != 1:
-                        self._internal("failed to load execution tree")
                     if initial.run_id in selected_ids:
                         self._internal("failed to load execution tree")
                     selected_ids.add(initial.run_id)
                     selected.append((stored, initial))
                     progressed = True
                 elif initial.parent_run_id in descendants:
-                    if stored.event.schema_version != 1:
-                        self._internal("failed to load execution tree")
                     if initial.session_id != root.session_id:
                         self._internal("failed to load execution tree")
                     descendants.add(initial.run_id)
                     selected_ids.add(initial.run_id)
                     selected.append((stored, initial))
                     progressed = True
                 else:
                     remaining.append(stored)
             if not progressed:
                 break
@@ -242,21 +245,25 @@ class QueryService:
         ):
             self._internal("failed to load execution tree")
         by_id: dict[str, ExecutionTreeNode] = {}
         for stored, initial in sorted(selected, key=lambda item: item[0].cursor):
             current = await self._load_run(initial.run_id)
             if (
                 current.session_id != root.session_id
                 or current.parent_run_id != initial.parent_run_id
                 or stored.event.session_id != current.session_id
                 or stored.event.run_id != current.run_id
-                or not _same_creation_identity(initial, current)
+                or not run_created_event_matches(
+                    current,
+                    stored.event.payload,
+                    schema_version=stored.event.schema_version,
+                )
             ):
                 self._internal("failed to load execution tree")
             by_id[current.run_id] = ExecutionTreeNode(
                 snapshot=current,
                 parent_run_id=current.parent_run_id,
                 created_cursor=stored.cursor,
             )
         if root.run_id not in by_id:
             self._internal("failed to load execution tree")
         for stored in stored_events:
@@ -401,34 +408,20 @@ def _matches(stored: StoredEvent, filters: EventFilter) -> bool:
     )


 def _parent_claim(payload: dict[str, Any]) -> str | None | _InvalidParent:
     value = payload.get("parent_run_id")
     if value is None or isinstance(value, str):
         return value
     return _InvalidParent.INVALID


-def _same_creation_identity(created: RunSnapshot, current: RunSnapshot) -> bool:
-    return (
-        created.run_id == current.run_id
-        and created.session_id == current.session_id
-        and created.agent_revision == current.agent_revision
-        and created.user_input == current.user_input
-        and created.parent_run_id == current.parent_run_id
-        and created.workflow_run_id == current.workflow_run_id
-        and created.workflow_node_id == current.workflow_node_id
-        and created.workflow_node_execution == current.workflow_node_execution
-        and created.task_envelope == current.task_envelope
-    )
-
-
 async def _stored_run(
     store: StateStore,
     run_id: str,
 ) -> RunSnapshot | None | _ReadFailure:
     try:
         data = await store.get_snapshot("run", run_id)
         if data is None:
             return None
         return RunSnapshot.model_validate(data)
     except Exception:
@@ -457,25 +450,33 @@ async def _events(
                 limit=limit,
             ),
             after_cursor=after_cursor,
             up_to_cursor=up_to_cursor,
             limit=limit,
         )
     except Exception:
         return _ReadFailure.FAILED


-def _run_snapshot(data: dict[str, Any]) -> RunSnapshot | _ReadFailure:
+def _run_creation(
+    data: dict[str, Any],
+    *,
+    schema_version: int,
+) -> RunCreatedEventPayload | RunSnapshot | _ReadFailure:
     try:
-        return RunSnapshot.model_validate(data)
+        if schema_version == 1:
+            return RunSnapshot.model_validate(data)
+        if schema_version == 2:
+            return RunCreatedEventPayload.model_validate(data)
     except Exception:
-        return _ReadFailure.FAILED
+        pass
+    return _ReadFailure.FAILED


 def _observed_event(stored: StoredEvent) -> ObservedEvent | _ReadFailure:
     try:
         return ObservedEvent(cursor=stored.cursor, event=stored.event)
     except Exception:
         return _ReadFailure.FAILED


 def _timeline_events(
@@ -512,21 +513,21 @@ def _tree_tail_status(
             if event.run_id in descendants and event.session_id != session_id:
                 return _TreeTailStatus.INVALID
             if event.type == "run.created":
                 if event.run_id in descendants:
                     return _TreeTailStatus.INVALID
                 parent_run_id = _parent_claim(event.payload)
                 if parent_run_id is _InvalidParent.INVALID:
                     continue
                 if parent_run_id not in descendants:
                     continue
-                if event.schema_version != 1 or event.session_id != session_id:
+                if event.schema_version not in {1, 2} or event.session_id != session_id:
                     return _TreeTailStatus.INVALID
                 return _TreeTailStatus.CHANGED
             if (
                 event.run_id in descendants
                 and event.type in _RUN_SNAPSHOT_TRANSITIONS
             ):
                 if event.schema_version != 1 or event.session_id != session_id:
                     return _TreeTailStatus.INVALID
                 return _TreeTailStatus.CHANGED
         return _TreeTailStatus.STABLE
diff --git a/src/agent_sdk/prompts/__init__.py b/src/agent_sdk/prompts/__init__.py
index 7e8d280..2722dfe 100644
--- a/src/agent_sdk/prompts/__init__.py
+++ b/src/agent_sdk/prompts/__init__.py
@@ -1,15 +1,17 @@
 from agent_sdk.prompts.composer import PromptComposer
 from agent_sdk.prompts.models import (
     BuiltPrompt,
     PromptLayer,
     PromptLayerManifest,
     PromptManifest,
 )
+from agent_sdk.prompts.persistence import PromptManifestPersistence

 __all__ = [
     "BuiltPrompt",
     "PromptComposer",
     "PromptLayer",
     "PromptLayerManifest",
     "PromptManifest",
+    "PromptManifestPersistence",
 ]
diff --git a/src/agent_sdk/prompts/composer.py b/src/agent_sdk/prompts/composer.py
index 3d520ba..d638bea 100644
--- a/src/agent_sdk/prompts/composer.py
+++ b/src/agent_sdk/prompts/composer.py
@@ -7,54 +7,73 @@ from importlib import resources
 from typing import Any

 from agent_sdk.context.models import ContextView
 from agent_sdk.errors import AgentSDKError, ErrorCode
 from agent_sdk.prompts.models import (
     BuiltPrompt,
     PromptLayer,
     PromptLayerManifest,
     PromptManifest,
 )
+from agent_sdk.ids import new_id
+from agent_sdk.skills.models import ActivatedSkill
 from agent_sdk.tools.models import freeze_json, thaw_json

 _PROFILE_ORDER = {
     "general": ("general",),
     "coding": ("general", "coding"),
 }
 _PROFILE_VERSION = "1"


 class PromptComposer:
     def compose(
         self,
         *,
         profile: str,
         context_view: ContextView,
         model: str,
         application: str | None = None,
+        skills: Sequence[ActivatedSkill] = (),
         tools: Sequence[Mapping[str, Any]] = (),
     ) -> BuiltPrompt:
         profile_names = _PROFILE_ORDER.get(profile)
         if profile_names is None:
             raise AgentSDKError(
                 ErrorCode.INVALID_STATE,
                 "unknown prompt profile",
                 retryable=False,
             )
         layers = [self._load_profile(name) for name in profile_names]
         if application:
             layers.append(self._layer("application", _PROFILE_VERSION, application))
+        skill_names = tuple(skill.metadata.name for skill in skills)
+        if len(set(skill_names)) != len(skill_names):
+            raise AgentSDKError(
+                ErrorCode.INVALID_STATE,
+                "duplicate prompt skill",
+                retryable=False,
+            )
+        layers.extend(
+            self._layer(
+                f"skill:{skill.metadata.name}",
+                skill.metadata.content_hash,
+                skill.instructions,
+            )
+            for skill in skills
+        )
         messages = tuple(
             {"role": "system", "content": layer.text} for layer in layers
         )
         text = "\n\n".join(layer.text for layer in layers)
         manifest = PromptManifest(
+            manifest_id=new_id("pmf"),
             layers=tuple(
                 PromptLayerManifest(
                     layer_id=layer.layer_id,
                     version=layer.version,
                     sha256=layer.sha256,
                 )
                 for layer in layers
             ),
             sha256=self._sha256(text),
             context_view_id=context_view.view_id,
diff --git a/src/agent_sdk/prompts/models.py b/src/agent_sdk/prompts/models.py
index d7ed45d..6b96b74 100644
--- a/src/agent_sdk/prompts/models.py
+++ b/src/agent_sdk/prompts/models.py
@@ -42,20 +42,21 @@ class PromptLayer(_PromptModel):
     sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


 class PromptLayerManifest(_PromptModel):
     layer_id: StrictStr = Field(min_length=1)
     version: StrictStr = Field(min_length=1)
     sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


 class PromptManifest(_PromptModel):
+    manifest_id: StrictStr = Field(min_length=1)
     layers: tuple[PromptLayerManifest, ...]
     sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
     context_view_id: StrictStr = Field(min_length=1)
     model: StrictStr = Field(min_length=1)
     tools_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

     @property
     def layer_names(self) -> tuple[str, ...]:
         return tuple(layer.layer_id for layer in self.layers)

diff --git a/src/agent_sdk/prompts/persistence.py b/src/agent_sdk/prompts/persistence.py
new file mode 100644
index 0000000..7e6c32e
--- /dev/null
+++ b/src/agent_sdk/prompts/persistence.py
@@ -0,0 +1,84 @@
+from __future__ import annotations
+
+from agent_sdk.errors import AgentSDKError, ErrorCode
+from agent_sdk.events.models import EventEnvelope
+from agent_sdk.prompts.models import PromptManifest
+from agent_sdk.storage.base import (
+    CommitBatch,
+    SnapshotPrecondition,
+    SnapshotPreconditionError,
+    SnapshotWrite,
+    StateStore,
+)
+
+
+class PromptManifestPersistence:
+    def __init__(self, store: StateStore) -> None:
+        self._store = store
+
+    async def persist(
+        self,
+        manifest: PromptManifest,
+        *,
+        session_id: str,
+    ) -> None:
+        event = EventEnvelope.new(
+            type="prompt.manifest.created",
+            session_id=session_id,
+            run_id=manifest.manifest_id,
+            sequence=1,
+            payload={
+                "manifest_id": manifest.manifest_id,
+                "context_view_id": manifest.context_view_id,
+                "sha256": manifest.sha256,
+                "model": manifest.model,
+                "tools_sha256": manifest.tools_sha256,
+                "layers": [
+                    {
+                        "layer_id": layer.layer_id,
+                        "sha256": layer.sha256,
+                    }
+                    for layer in manifest.layers
+                ],
+            },
+        )
+        try:
+            await self._store.commit(
+                CommitBatch(
+                    events=(event,),
+                    snapshots=(
+                        SnapshotWrite(
+                            "prompt_manifest",
+                            manifest.manifest_id,
+                            session_id,
+                            1,
+                            manifest.model_dump(mode="json"),
+                        ),
+                    ),
+                    preconditions=(
+                        SnapshotPrecondition("session", session_id),
+                        SnapshotPrecondition(
+                            "context_view",
+                            manifest.context_view_id,
+                            session_id=session_id,
+                        ),
+                    ),
+                )
+            )
+        except SnapshotPreconditionError as error:
+            raise AgentSDKError(
+                ErrorCode.NOT_FOUND,
+                "prompt manifest owner no longer exists",
+                retryable=False,
+            ) from error
+        except AgentSDKError:
+            raise
+        except Exception as error:
+            raise AgentSDKError(
+                ErrorCode.INTERNAL,
+                "prompt manifest persistence failed",
+                retryable=False,
+            ) from error
+
+
+__all__ = ["PromptManifestPersistence"]
diff --git a/src/agent_sdk/runtime/commands.py b/src/agent_sdk/runtime/commands.py
index fbaaf22..10955f3 100644
--- a/src/agent_sdk/runtime/commands.py
+++ b/src/agent_sdk/runtime/commands.py
@@ -1,26 +1,27 @@
 import asyncio
-from collections.abc import Iterable, Mapping
+from collections.abc import Callable, Iterable, Mapping
 from dataclasses import dataclass
 from pathlib import Path
 from typing import Any, Generic, Literal, TypeVar

 from agent_sdk.events.models import EventEnvelope
 from agent_sdk.errors import AgentSDKError, ErrorCode, SessionBusyError
 from agent_sdk.ids import new_id
 from agent_sdk.runtime.idempotency import _idempotency_public_error
-from agent_sdk.runtime.execution import ExecutionDescriptor
+from agent_sdk.runtime.execution import DurableAgentSpec, ExecutionDescriptor
 from agent_sdk.runtime.models import (
     RunSnapshot,
     RunStatus,
     SessionSnapshot,
     SessionStatus,
+    run_created_event_payload,
 )
 from agent_sdk.runtime.session_lifecycle import (
     close_session_transition,
     exact_session_precondition,
     load_session,
     session_transition_batch,
     session_write,
     transition_session,
 )
 from agent_sdk.storage.base import (
@@ -83,22 +84,28 @@ def validate_session_result(result: Mapping[str, Any]) -> SessionSnapshot:
         raise AgentSDKError(
             ErrorCode.INTERNAL,
             "session command result is invalid",
             retryable=False,
         )
     assert snapshot is not None
     return snapshot


 class RuntimeCommands:
-    def __init__(self, store: StateStore) -> None:
+    def __init__(
+        self,
+        store: StateStore,
+        *,
+        agent_preflight: Callable[[DurableAgentSpec], None] | None = None,
+    ) -> None:
         self._store = store
+        self._agent_preflight = agent_preflight

     async def create_session(
         self,
         *,
         workspaces: Iterable[str | Path],
         idempotency_key: str | None = None,
     ) -> SessionSnapshot:
         try:
             normalized_workspaces = tuple(
                 str(Path(workspace).resolve(strict=False))
@@ -430,20 +437,22 @@ class RuntimeCommands:
         execution_descriptor: ExecutionDescriptor | None = None,
         idempotency_key: str | None = None,
         related_preconditions: tuple[SnapshotPrecondition, ...] = (),
     ) -> CommandOutcome[RunSnapshot]:
         if execution_descriptor is None and idempotency_key is not None:
             raise AgentSDKError(
                 ErrorCode.INVALID_STATE,
                 "legacy run cannot use idempotency",
                 retryable=False,
             ) from None
+        if execution_descriptor is not None and self._agent_preflight is not None:
+            self._agent_preflight(execution_descriptor.agent)
         scope = f"session/{session_id}/run.start"
         invalid_key: AgentSDKError | None = None
         if idempotency_key is not None:
             try:
                 validate_replay(
                     IdempotencyReplay(scope, idempotency_key, "0" * 64)
                 )
             except IdempotencyError as error:
                 invalid_key = _idempotency_public_error(error)
         if invalid_key is not None:
@@ -555,21 +564,22 @@ class RuntimeCommands:
                     session_id=session_id,
                     run_id=None,
                     sequence=updated_session.version,
                     payload={"run_id": snapshot.run_id},
                 )
                 run_event = EventEnvelope.new(
                     type="run.created",
                     session_id=session_id,
                     run_id=snapshot.run_id,
                     sequence=1,
-                    payload=run_data,
+                    payload=run_created_event_payload(snapshot),
+                    schema_version=2,
                 )
                 request = None
                 if idempotency_key is not None:
                     assert fingerprint is not None
                     request = IdempotencyWrite(
                         scope=scope,
                         key=idempotency_key,
                         request_fingerprint=fingerprint,
                         session_id=session_id,
                         result=run_data,
diff --git a/src/agent_sdk/runtime/engine.py b/src/agent_sdk/runtime/engine.py
index 6bd1d1f..a087c87 100644
--- a/src/agent_sdk/runtime/engine.py
+++ b/src/agent_sdk/runtime/engine.py
@@ -3,20 +3,21 @@ from __future__ import annotations
 import asyncio
 import json
 import sys
 from collections.abc import Awaitable, Callable, Mapping
 from copy import deepcopy
 from contextlib import suppress
 from datetime import UTC, datetime, timedelta
 from hashlib import sha256
 from typing import Any

+from agent_sdk.context.middleware import ContextMiddleware
 from agent_sdk.errors import AgentSDKError, ErrorCode
 from agent_sdk.events.models import EventEnvelope
 from agent_sdk.ids import new_id
 from agent_sdk.models.litellm_gateway import (
     LiteLLMGateway,
     ModelCompleted,
     ModelRequest,
     TextDelta,
     ToolCallCompleted,
     UsageReported,
@@ -50,20 +51,22 @@ from agent_sdk.runtime.session_lifecycle import (
     load_session,
     session_write,
 )
 from agent_sdk.runtime.reconciliation import (
     ExternalOperationStatus,
     ModelCallOperation,
     RunCheckpoint,
     RunCheckpointPhase,
     RecoveryStateConflictError,
     ToolCallOperation,
+    model_request_fingerprint,
+    serialize_model_request,
 )
 from agent_sdk.storage.base import (
     CommitResult,
     ExternalOperationWrite,
     RunCheckpointWrite,
     RunProgressBatch,
     SnapshotPrecondition,
     SnapshotWrite,
     StateStore,
 )
@@ -208,69 +211,111 @@ class _RunEmitter:
                     preconditions=(
                         exact_session_precondition(session),
                         exact_run_precondition(self._run),
                     ),
                     checkpoint_precondition=self._checkpoint,
                 ),
             )
             self._run = snapshot
             self._sequence += 1

-    async def start_model(self, request: ModelRequest) -> ModelCallOperation:
+    async def start_model(
+        self,
+        request: ModelRequest,
+        *,
+        context_view_id: str | None = None,
+        prompt_manifest_id: str | None = None,
+    ) -> ModelCallOperation:
         async with self._lock:
             self._ensure_lease_current()
             assert self._checkpoint is not None
             adapter = self._provider_recovery.resolve(request.model)
             recovery_metadata: dict[str, object]
             if adapter is None:
                 recovery_metadata = {
                     "authoritative_status": False,
                     "same_operation_id_resend": False,
                 }
             else:
                 recovery_metadata = {
                     "adapter_id": adapter.adapter_id,
                     "adapter_version": adapter.version,
                     "authoritative_status": adapter.authoritative_status,
                     "same_operation_id_resend": adapter.same_operation_id_resend,
                 }
+            prepared_request = (
+                None
+                if context_view_id is None and prompt_manifest_id is None
+                else serialize_model_request(request)
+            )
             operation = ModelCallOperation(
                 operation_id=new_id("op_model"),
                 session_id=self._run.session_id,
                 run_id=self._run.run_id,
                 turn=self._checkpoint.turn,
                 request_fingerprint=_model_request_fingerprint(request),
                 lease_generation=self._lease.generation,
                 status=ExternalOperationStatus.STARTED,
                 provider_identity=request.model,
                 recovery_metadata=recovery_metadata,
+                context_view_id=context_view_id,
+                prompt_manifest_id=prompt_manifest_id,
+                prepared_request=prepared_request,
             )
             checkpoint = self._checkpoint.model_copy(
                 update={
                     "checkpoint_version": self._checkpoint.checkpoint_version + 1,
                     "phase": RunCheckpointPhase.MODEL_IN_FLIGHT,
                     "operation_id": operation.operation_id,
                 }
             )
+            started_payload: dict[str, Any] = {"model": request.model}
+            if prepared_request is not None:
+                assert context_view_id is not None
+                assert prompt_manifest_id is not None
+                started_payload.update(
+                    {
+                        "context_view_id": context_view_id,
+                        "prompt_manifest_id": prompt_manifest_id,
+                        "request_fingerprint": operation.request_fingerprint,
+                    }
+                )
             events = (
                 self._new_event("step.started", {}),
-                self._new_event("model.call.started", {"model": request.model}, offset=1),
+                self._new_event("model.call.started", started_payload, offset=1),
             )
+            prepared_preconditions: tuple[SnapshotPrecondition, ...] = ()
+            if prepared_request is not None:
+                assert context_view_id is not None
+                assert prompt_manifest_id is not None
+                prepared_preconditions = (
+                    SnapshotPrecondition(
+                        "context_view",
+                        context_view_id,
+                        session_id=self._run.session_id,
+                    ),
+                    SnapshotPrecondition(
+                        "prompt_manifest",
+                        prompt_manifest_id,
+                        session_id=self._run.session_id,
+                    ),
+                )
             await _commit_progress(
                 self._store,
                 RunProgressBatch(
                     lease=self._lease,
                     now=self._clock(),
                     events=events,
                     preconditions=(
                         SnapshotPrecondition("session", self._run.session_id),
                         exact_run_precondition(self._run),
+                        *prepared_preconditions,
                     ),
                     operation=ExternalOperationWrite(None, operation),
                     checkpoint=RunCheckpointWrite(self._checkpoint, checkpoint),
                 ),
             )
             self._checkpoint = checkpoint
             self._sequence += len(events)
             return operation

     async def complete_model(
@@ -1004,31 +1049,33 @@ class RunEngine:
         models: LiteLLMGateway,
         tools: ToolRegistry | None = None,
         policy: PolicyEngine | None = None,
         permission_bridge: InProcessPermissionBridge | None = None,
         *,
         lease_manager: LeaseManager | None = None,
         _clock: Callable[[], datetime] | None = None,
         _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
         _heartbeat_interval: float = _RUN_LEASE_TTL.total_seconds() / 3,
         provider_recovery: ProviderRecoveryRegistry | None = None,
+        context_middleware: ContextMiddleware | None = None,
     ) -> None:
         self._store = store
         self._models = models
         self._tools = tools or ToolRegistry()
         self._policy = policy or PolicyEngine()
         self._permission_bridge = permission_bridge
         self._leases = lease_manager or LeaseManager(store, ttl=_RUN_LEASE_TTL)
         self._clock = _clock or (lambda: datetime.now(UTC))
         self._sleep = _sleep
         self._heartbeat_interval = _heartbeat_interval
         self._provider_recovery = provider_recovery or ProviderRecoveryRegistry()
+        self._context = context_middleware

     async def execute(self, run_id: str, request: ModelRequest) -> RunResult:
         public_error: tuple[ErrorCode, str, bool] | None = None
         lease_held = False
         try:
             return await self._execute_private(run_id, request)
         except LeaseHeldError:
             lease_held = True
         except AgentSDKError as error:
             public_error = (error.code, error.message, error.retryable)
@@ -1514,22 +1561,47 @@ class RunEngine:
                     usage_payload = step_usage.model_dump(mode="json")
                     if recovered_result.tool_call is not None:
                         calls.append(recovered_result.tool_call)
                     model_completed = ModelCompleted(recovered_result.finish_reason)
                 else:
                     model_request = ModelRequest(
                         model=request.model,
                         messages=tuple(deepcopy(messages)),
                         tools=request.tools,
                         params=dict(request.params),
+                        purpose=request.purpose,
                     )
-                    operation = await emitter.start_model(model_request)
+                    if self._context is not None:
+                        prepared = await self._context.prepare(
+                            run=emitter.current_snapshot,
+                            checkpoint=emitter.current_checkpoint,
+                            tools=model_request.tools,
+                        )
+                        model_request = ModelRequest(
+                            model=model_request.model,
+                            messages=tuple(
+                                deepcopy(message)
+                                for message in prepared.messages
+                            ),
+                            tools=model_request.tools,
+                            params=dict(model_request.params),
+                            purpose=model_request.purpose,
+                        )
+                        operation = await emitter.start_model(
+                            model_request,
+                            context_view_id=prepared.view.view_id,
+                            prompt_manifest_id=(
+                                prepared.prompt.manifest.manifest_id
+                            ),
+                        )
+                    else:
+                        operation = await emitter.start_model(model_request)
                     try:
                         async for event in self._models.stream(model_request):
                             if isinstance(event, TextDelta):
                                 chunks.append(event.text)
                                 step_chunks.append(event.text)
                                 await emitter.add_delta(event.text)
                             elif isinstance(event, ToolCallCompleted):
                                 calls.append(event)
                             elif isinstance(event, UsageReported):
                                 await emitter.flush_delta()
@@ -2123,34 +2195,21 @@ def _add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
         return first + second

     return TokenUsage(
         prompt_tokens=add(left.prompt_tokens, right.prompt_tokens),
         completion_tokens=add(left.completion_tokens, right.completion_tokens),
         total_tokens=add(left.total_tokens, right.total_tokens),
     )


 def _model_request_fingerprint(request: ModelRequest) -> str:
-    encoded = json.dumps(
-        {
-            "model": request.model,
-            "messages": request.messages,
-            "tools": request.tools,
-            "params": request.params,
-            "purpose": request.purpose,
-        },
-        ensure_ascii=False,
-        allow_nan=False,
-        sort_keys=True,
-        separators=(",", ":"),
-    )
-    return sha256(encoded.encode("utf-8")).hexdigest()
+    return model_request_fingerprint(request)


 def _tool_request_fingerprint(
     call: ToolCallCompleted,
     capability: ToolCapabilityDescriptor,
     arguments: Mapping[str, Any],
 ) -> str:
     encoded = json.dumps(
         {
             "call_id": call.call_id,
diff --git a/src/agent_sdk/runtime/execution.py b/src/agent_sdk/runtime/execution.py
index e112feb..755945a 100644
--- a/src/agent_sdk/runtime/execution.py
+++ b/src/agent_sdk/runtime/execution.py
@@ -2,20 +2,21 @@ from __future__ import annotations

 import json
 import math
 from collections.abc import Mapping
 from hashlib import sha256
 from types import MappingProxyType
 from typing import Any, Literal, Self, cast

 from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

+from agent_sdk.context_runtime import ContextRuntimeConfig
 from agent_sdk.tools.models import ToolSpec
 from agent_sdk._workflow_validation import validate_canonical_workflow_program


 def _freeze_json(value: Any) -> Any:
     if isinstance(value, Mapping):
         frozen: dict[str, Any] = {}
         for key, item in value.items():
             if not isinstance(key, str):
                 raise ValueError("JSON object keys must be strings")
@@ -75,34 +76,47 @@ class _RevalidatedDescriptor(BaseModel):

 class DurableAgentSpec(_RevalidatedDescriptor):
     """Cycle-free, strict durable representation of ``AgentSpec``."""

     model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

     name: str
     model: str
     model_params: Mapping[str, Any] = Field(default_factory=dict)
     revision: str = "1"
+    prompt_profile: Literal["general", "coding"] = "general"
+    system_prompt: str | None = None
+    skills: tuple[str, ...] = ()
+    context: ContextRuntimeConfig = Field(default_factory=ContextRuntimeConfig)

     @field_validator("model_params", mode="after")
     @classmethod
     def _model_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
         frozen = _freeze_json(value)
         assert isinstance(frozen, Mapping)
         return frozen

     @field_serializer("model_params")
     def _serialize_model_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
         result = _thaw_json(value)
         assert isinstance(result, dict)
         return result

+    @field_validator("skills")
+    @classmethod
+    def _validate_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
+        if any(not name.strip() for name in value):
+            raise ValueError("skills must contain nonempty names")
+        if len(set(value)) != len(value):
+            raise ValueError("skills must be unique")
+        return value
+

 class DurableAgentNode(_RevalidatedDescriptor):
     model_config = ConfigDict(frozen=True, extra="forbid")

     id: str = Field(min_length=1, max_length=128)
     kind: Literal["agent"] = "agent"
     agent_revision: str = Field(min_length=1, max_length=256)
     input: str = Field(min_length=1, max_length=32_768)
     run_as: Literal["parent", "child"] = "parent"
     success_criteria: tuple[str, ...] = ()
@@ -359,20 +373,56 @@ class ExecutionPolicyDescriptor(_RevalidatedDescriptor):
 class ExecutionDescriptor(_RevalidatedDescriptor):
     model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

     agent: DurableAgentSpec
     agent_hash: str
     messages: tuple[Mapping[str, Any], ...]
     tools: tuple[ToolCapabilityDescriptor, ...]
     policy: ExecutionPolicyDescriptor
     descriptor_hash: str

+    @model_validator(mode="before")
+    @classmethod
+    def _upgrade_legacy_agent_fields(cls, value: Any) -> Any:
+        if not isinstance(value, Mapping):
+            return value
+        agent = value.get("agent")
+        if not isinstance(agent, Mapping):
+            return value
+        new_fields = {"prompt_profile", "system_prompt", "skills", "context"}
+        if new_fields <= set(agent):
+            return value
+        raw_agent = dict(agent)
+        if value.get("agent_hash") != _hash(raw_agent):
+            return value
+        raw_content = {
+            key: _thaw_json(item)
+            for key, item in value.items()
+            if key != "descriptor_hash"
+        }
+        if value.get("descriptor_hash") != _hash(raw_content):
+            return value
+        upgraded_agent = DurableAgentSpec.model_validate(raw_agent).model_dump(
+            mode="json"
+        )
+        upgraded = {key: _thaw_json(item) for key, item in value.items()}
+        upgraded["agent"] = upgraded_agent
+        upgraded["agent_hash"] = _hash(upgraded_agent)
+        upgraded["descriptor_hash"] = _hash(
+            {
+                key: item
+                for key, item in upgraded.items()
+                if key != "descriptor_hash"
+            }
+        )
+        return upgraded
+
     @field_validator("messages", mode="after")
     @classmethod
     def _messages(
         cls, value: tuple[Mapping[str, Any], ...]
     ) -> tuple[Mapping[str, Any], ...]:
         return tuple(cast(Mapping[str, Any], _freeze_json(message)) for message in value)

     @field_serializer("messages")
     def _serialize_messages(
         self, value: tuple[Mapping[str, Any], ...]
diff --git a/src/agent_sdk/runtime/models.py b/src/agent_sdk/runtime/models.py
index fd4bbab..34a8ce3 100644
--- a/src/agent_sdk/runtime/models.py
+++ b/src/agent_sdk/runtime/models.py
@@ -1,25 +1,28 @@
+import hashlib
+import json
 from enum import StrEnum
 from types import MappingProxyType
 from typing import Any, Literal, Self

 from collections.abc import Mapping

 from pydantic import (
     BaseModel,
     ConfigDict,
     Field,
     field_serializer,
     field_validator,
     model_validator,
 )

+from agent_sdk.context_runtime import ContextRuntimeConfig
 from agent_sdk.tools.models import ToolResult
 from agent_sdk.subagents.models import TaskEnvelope
 from agent_sdk.runtime.execution import ExecutionDescriptor


 class RunStatus(StrEnum):
     CREATED = "created"
     RUNNING = "running"
     WAITING_PERMISSION = "waiting_permission"
     INTERRUPTED = "interrupted"
@@ -58,32 +61,45 @@ def mutable_model_params(value: Mapping[str, Any]) -> dict[str, Any]:
     return {key: thaw(item) for key, item in value.items()}


 class AgentSpec(BaseModel):
     model_config = ConfigDict(frozen=True, extra="forbid")

     name: str
     model: str
     model_params: Mapping[str, Any] = Field(default_factory=dict)
     revision: str = "1"
+    prompt_profile: Literal["general", "coding"] = "general"
+    system_prompt: str | None = None
+    skills: tuple[str, ...] = ()
+    context: ContextRuntimeConfig = Field(default_factory=ContextRuntimeConfig)

     @field_validator("model_params", mode="after")
     @classmethod
     def _freeze_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
         frozen = _freeze_model_param(value)
         assert isinstance(frozen, Mapping)
         return frozen

     @field_serializer("model_params")
     def _serialize_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
         return mutable_model_params(value)

+    @field_validator("skills")
+    @classmethod
+    def _validate_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
+        if any(not name.strip() for name in value):
+            raise ValueError("skills must contain nonempty names")
+        if len(set(value)) != len(value):
+            raise ValueError("skills must be unique")
+        return value
+
     def model_copy(
         self,
         *,
         update: Mapping[str, Any] | None = None,
         deep: bool = False,
     ) -> Self:
         del deep
         data = self.model_dump(mode="json")
         if update is not None:
             data.update(update)
@@ -243,10 +259,111 @@ class RunSnapshot(BaseModel):
         self,
         *,
         update: Mapping[str, Any] | None = None,
         deep: bool = False,
     ) -> Self:
         del deep
         data = self.model_dump(mode="json")
         if update is not None:
             data.update(update)
         return type(self).model_validate(data)
+
+
+class RunCreatedEventPayload(BaseModel):
+    model_config = ConfigDict(frozen=True, extra="forbid")
+
+    run_id: str
+    session_id: str
+    agent_revision: str
+    status: Literal["created"] = "created"
+    version: Literal[1] = 1
+    parent_run_id: str | None = None
+    workflow_run_id: str | None = None
+    workflow_node_id: str | None = None
+    workflow_node_execution: int | None = None
+    execution_compatibility: Literal["legacy_unknown", "current"]
+    user_input: str
+    user_input_sha256: str
+    task_envelope_sha256: str | None = None
+    execution_descriptor_hash: str | None = None
+    agent_hash: str | None = None
+    tool_capability_hashes: tuple[str, ...] = ()
+
+    @classmethod
+    def from_snapshot(cls, snapshot: RunSnapshot) -> Self:
+        descriptor = snapshot.execution_descriptor
+        return cls(
+            run_id=snapshot.run_id,
+            session_id=snapshot.session_id,
+            agent_revision=snapshot.agent_revision,
+            parent_run_id=snapshot.parent_run_id,
+            workflow_run_id=snapshot.workflow_run_id,
+            workflow_node_id=snapshot.workflow_node_id,
+            workflow_node_execution=snapshot.workflow_node_execution,
+            execution_compatibility=snapshot.execution_compatibility,
+            user_input=snapshot.user_input,
+            user_input_sha256=_canonical_sha256(snapshot.user_input),
+            task_envelope_sha256=(
+                None
+                if snapshot.task_envelope is None
+                else _canonical_sha256(
+                    snapshot.task_envelope.model_dump(mode="json")
+                )
+            ),
+            execution_descriptor_hash=(
+                None if descriptor is None else descriptor.descriptor_hash
+            ),
+            agent_hash=None if descriptor is None else descriptor.agent_hash,
+            tool_capability_hashes=(
+                ()
+                if descriptor is None
+                else tuple(tool.capability_hash for tool in descriptor.tools)
+            ),
+        )
+
+
+def run_created_event_payload(snapshot: RunSnapshot) -> dict[str, Any]:
+    return RunCreatedEventPayload.from_snapshot(snapshot).model_dump(mode="json")
+
+
+def run_created_event_matches(
+    snapshot: RunSnapshot,
+    payload: Mapping[str, Any],
+    *,
+    schema_version: int,
+) -> bool:
+    try:
+        if schema_version == 1:
+            created = RunSnapshot(
+                run_id=snapshot.run_id,
+                session_id=snapshot.session_id,
+                agent_revision=snapshot.agent_revision,
+                status=RunStatus.CREATED,
+                user_input=snapshot.user_input,
+                parent_run_id=snapshot.parent_run_id,
+                workflow_run_id=snapshot.workflow_run_id,
+                workflow_node_id=snapshot.workflow_node_id,
+                workflow_node_execution=snapshot.workflow_node_execution,
+                task_envelope=snapshot.task_envelope,
+                execution_compatibility=snapshot.execution_compatibility,
+                execution_descriptor=snapshot.execution_descriptor,
+            )
+            historical = RunSnapshot.model_validate(dict(payload))
+            return historical == created
+        if schema_version == 2:
+            return (
+                RunCreatedEventPayload.model_validate(dict(payload))
+                == RunCreatedEventPayload.from_snapshot(snapshot)
+            )
+    except Exception:
+        return False
+    return False
+
+
+def _canonical_sha256(value: Any) -> str:
+    canonical = json.dumps(
+        value,
+        ensure_ascii=False,
+        sort_keys=True,
+        separators=(",", ":"),
+    )
+    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
diff --git a/src/agent_sdk/runtime/reconciliation.py b/src/agent_sdk/runtime/reconciliation.py
index a84275f..8fc9f10 100644
--- a/src/agent_sdk/runtime/reconciliation.py
+++ b/src/agent_sdk/runtime/reconciliation.py
@@ -1,31 +1,34 @@
 """Durable recovery records shared by runtime and storage."""

 from __future__ import annotations

 import json
+from hashlib import sha256
 from collections.abc import Awaitable, Callable, Coroutine, Mapping
 from datetime import UTC, datetime
 from enum import StrEnum
 from functools import wraps
-from typing import Any, Literal, ParamSpec, Protocol, Self, TypeAlias, TypeVar
+from typing import Any, Literal, ParamSpec, Protocol, Self, TypeAlias, TypeVar, cast

 from pydantic import (
     BaseModel,
     ConfigDict,
     Field,
+    StrictStr,
     field_serializer,
     field_validator,
     model_validator,
 )

 from agent_sdk.errors import AgentSDKError, ErrorCode
+from agent_sdk.models.litellm_gateway import ModelRequest
 from agent_sdk.runtime.models import (
     RunFailure,
     RunSnapshot,
     RunStatus,
     SessionSnapshot,
     SessionStatus,
     TokenUsage,
 )
 from agent_sdk.runtime.provider_recovery import (
     ProviderRecoveryDisposition,
@@ -120,20 +123,220 @@ class _RecoveryModel(BaseModel):
             data.update(update)
         return type(self).model_validate(data)


 def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
     frozen = freeze_json(value)
     assert isinstance(frozen, Mapping)
     return frozen


+def _validate_prepared_tool_call(value: Any) -> None:
+    if not isinstance(value, Mapping) or set(value) != {
+        "id",
+        "type",
+        "function",
+    }:
+        raise ValueError("prepared assistant Tool call shape is invalid")
+    function = value["function"]
+    if (
+        not isinstance(value["id"], str)
+        or not value["id"]
+        or value["type"] != "function"
+        or not isinstance(function, Mapping)
+        or set(function) != {"name", "arguments"}
+        or not isinstance(function["name"], str)
+        or not function["name"]
+        or not isinstance(function["arguments"], str)
+    ):
+        raise ValueError("prepared assistant Tool call fields are invalid")
+
+
+def _validate_prepared_message(value: Mapping[str, Any]) -> None:
+    role = value.get("role")
+    if role not in {"system", "user", "assistant", "tool"}:
+        raise ValueError("prepared message role is invalid")
+    allowed = {
+        "system": {"role", "content", "name"},
+        "user": {"role", "content", "name"},
+        "assistant": {"role", "content", "name", "tool_calls"},
+        "tool": {"role", "content", "name", "tool_call_id"},
+    }[role]
+    if not {"role", "content"} <= set(value) or not set(value) <= allowed:
+        raise ValueError("prepared message fields are invalid")
+    name = value.get("name")
+    if name is not None and (not isinstance(name, str) or not name):
+        raise ValueError("prepared message name is invalid")
+    content = value["content"]
+    if role in {"system", "user"}:
+        if not isinstance(content, str):
+            raise ValueError("prepared message content is invalid")
+        return
+    if role == "tool":
+        call_id = value.get("tool_call_id")
+        if (
+            not isinstance(content, str)
+            or not isinstance(call_id, str)
+            or not call_id
+        ):
+            raise ValueError("prepared Tool result fields are invalid")
+        return
+    if content is not None and not isinstance(content, str):
+        raise ValueError("prepared assistant content is invalid")
+    calls = value.get("tool_calls")
+    if calls is None:
+        if content is None:
+            raise ValueError("prepared assistant content is missing")
+        return
+    if not isinstance(calls, (list, tuple)) or not calls:
+        raise ValueError("prepared assistant Tool calls are invalid")
+    for call in calls:
+        _validate_prepared_tool_call(call)
+
+
+def _validate_prepared_tool(value: Mapping[str, Any]) -> None:
+    if set(value) != {"type", "function"} or value.get("type") != "function":
+        raise ValueError("prepared Tool schema shape is invalid")
+    function = value.get("function")
+    if not isinstance(function, Mapping):
+        raise ValueError("prepared Tool function is invalid")
+    allowed = {"name", "description", "parameters"}
+    if (
+        not {"name", "parameters"} <= set(function)
+        or not set(function) <= allowed
+    ):
+        raise ValueError("prepared Tool function fields are invalid")
+    name = function["name"]
+    description = function.get("description")
+    if (
+        not isinstance(name, str)
+        or not name
+        or (description is not None and not isinstance(description, str))
+        or not isinstance(function["parameters"], Mapping)
+    ):
+        raise ValueError("prepared Tool function values are invalid")
+
+
+class _ModelRequestPayload(_RecoveryModel):
+    model: StrictStr = Field(min_length=1)
+    messages: tuple[Mapping[str, Any], ...]
+    tools: tuple[Mapping[str, Any], ...] = ()
+    params: Mapping[str, Any] = Field(default_factory=dict)
+    purpose: StrictStr | None = None
+
+    @field_validator("messages", mode="after")
+    @classmethod
+    def _validate_messages(
+        cls,
+        value: tuple[Mapping[str, Any], ...],
+    ) -> tuple[Mapping[str, Any], ...]:
+        if not value:
+            raise ValueError("prepared model request messages are empty")
+        for message in value:
+            _validate_prepared_message(message)
+        return value
+
+    @field_validator("tools", mode="after")
+    @classmethod
+    def _validate_tools(
+        cls,
+        value: tuple[Mapping[str, Any], ...],
+    ) -> tuple[Mapping[str, Any], ...]:
+        for tool in value:
+            _validate_prepared_tool(tool)
+        return value
+
+    @field_validator("messages", "tools", mode="after")
+    @classmethod
+    def _freeze_sequence(
+        cls,
+        value: tuple[Mapping[str, Any], ...],
+    ) -> tuple[Mapping[str, Any], ...]:
+        return tuple(_frozen_mapping(item) for item in value)
+
+    @field_validator("params", mode="after")
+    @classmethod
+    def _freeze_params(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
+        return _frozen_mapping(value)
+
+    @field_serializer("messages", "tools")
+    def _serialize_sequence(
+        self,
+        value: tuple[Mapping[str, Any], ...],
+    ) -> list[dict[str, Any]]:
+        return [thaw_json(item) for item in value]
+
+    @field_serializer("params")
+    def _serialize_params(self, value: Mapping[str, Any]) -> dict[str, Any]:
+        return cast(dict[str, Any], thaw_json(value))
+
+
+def serialize_model_request(request: ModelRequest) -> dict[str, Any]:
+    try:
+        payload = _ModelRequestPayload(
+            model=request.model,
+            messages=request.messages,
+            tools=request.tools,
+            params=request.params,
+            purpose=request.purpose,
+        ).model_dump(mode="json")
+        frozen = freeze_json(payload)
+        thawed = thaw_json(frozen)
+    except Exception as error:
+        raise AgentSDKError(
+            ErrorCode.INVALID_STATE,
+            "model request must be canonical JSON",
+            retryable=False,
+        ) from error
+    assert isinstance(thawed, dict)
+    return thawed
+
+
+def deserialize_model_request(value: Mapping[str, Any]) -> ModelRequest:
+    try:
+        raw = thaw_json(freeze_json(value))
+        if not isinstance(raw, dict):
+            raise ValueError("model request payload must be an object")
+        messages = raw.get("messages")
+        tools = raw.get("tools")
+        if not isinstance(messages, list) or not isinstance(tools, list):
+            raise ValueError("model request sequences are invalid")
+        raw["messages"] = tuple(messages)
+        raw["tools"] = tuple(tools)
+        payload = _ModelRequestPayload.model_validate(raw)
+        data = payload.model_dump(mode="json")
+    except Exception as error:
+        raise AgentSDKError(
+            ErrorCode.INVALID_STATE,
+            "stored model request is invalid",
+            retryable=False,
+        ) from error
+    return ModelRequest(
+        model=payload.model,
+        messages=tuple(dict(message) for message in data["messages"]),
+        tools=tuple(dict(tool) for tool in data["tools"]),
+        params=dict(data["params"]),
+        purpose=payload.purpose,
+    )
+
+
+def model_request_fingerprint(request: ModelRequest) -> str:
+    encoded = json.dumps(
+        serialize_model_request(request),
+        ensure_ascii=False,
+        allow_nan=False,
+        sort_keys=True,
+        separators=(",", ":"),
+    )
+    return sha256(encoded.encode("utf-8")).hexdigest()
+
+
 class _ExternalOperationBase(_RecoveryModel):
     operation_id: str
     operation_kind: ExternalOperationKind
     session_id: str
     run_id: str
     turn: int = Field(ge=0)
     request_fingerprint: str
     lease_generation: int = Field(ge=1)
     status: ExternalOperationStatus
     provider_identity: str | None
@@ -186,28 +389,74 @@ class _ExternalOperationBase(_RecoveryModel):
             raise ValueError("terminal operation requires an outcome")
         return self


 class ModelCallOperation(_ExternalOperationBase):
     operation_kind: Literal[ExternalOperationKind.MODEL_CALL] = (
         ExternalOperationKind.MODEL_CALL
     )
     provider_identity: str
     tool_identity: None = None
+    context_view_id: str | None = None
+    prompt_manifest_id: str | None = None
+    prepared_request: Mapping[str, Any] | None = None

     @field_validator("provider_identity")
     @classmethod
     def _validate_provider_identity(cls, value: str) -> str:
         if not value.strip():
             raise ValueError("provider identity must be nonempty")
         return value

+    @field_validator("context_view_id", "prompt_manifest_id")
+    @classmethod
+    def _validate_context_identity(cls, value: str | None) -> str | None:
+        if value is not None and not value.strip():
+            raise ValueError("model context identity must be nonempty")
+        return value
+
+    @field_validator("prepared_request", mode="after")
+    @classmethod
+    def _freeze_prepared_request(
+        cls,
+        value: Mapping[str, Any] | None,
+    ) -> Mapping[str, Any] | None:
+        if value is None:
+            return None
+        request = deserialize_model_request(value)
+        return _frozen_mapping(serialize_model_request(request))
+
+    @field_serializer("prepared_request")
+    def _serialize_prepared_request(
+        self,
+        value: Mapping[str, Any] | None,
+    ) -> Any:
+        return None if value is None else thaw_json(value)
+
+    @model_validator(mode="after")
+    def _validate_prepared_request_identity(self) -> Self:
+        populated = (
+            self.context_view_id is not None,
+            self.prompt_manifest_id is not None,
+            self.prepared_request is not None,
+        )
+        if any(populated) and not all(populated):
+            raise ValueError("prepared model request references are incomplete")
+        if self.prepared_request is not None:
+            request = deserialize_model_request(self.prepared_request)
+            if (
+                request.model != self.provider_identity
+                or model_request_fingerprint(request) != self.request_fingerprint
+            ):
+                raise ValueError("prepared model request fingerprint mismatch")
+        return self
+

 class ToolCallOperation(_ExternalOperationBase):
     operation_kind: Literal[ExternalOperationKind.TOOL_CALL] = (
         ExternalOperationKind.TOOL_CALL
     )
     provider_identity: None = None
     tool_identity: str

     @field_validator("tool_identity")
     @classmethod
diff --git a/src/agent_sdk/runtime/recovery.py b/src/agent_sdk/runtime/recovery.py
index 9a2ce3f..75858a8 100644
--- a/src/agent_sdk/runtime/recovery.py
+++ b/src/agent_sdk/runtime/recovery.py
@@ -8,27 +8,29 @@ import sys
 from collections.abc import Awaitable, Callable, Mapping
 from dataclasses import dataclass, replace
 from datetime import UTC, datetime, timedelta
 from time import monotonic
 from pathlib import Path
 from typing import Any, Literal

 from jsonschema import Draft202012Validator
 from jsonschema.exceptions import ValidationError as JSONSchemaValidationError

+from agent_sdk.context.models import ContextView
 from agent_sdk.errors import AgentSDKError, ErrorCode
 from agent_sdk.events.models import EventEnvelope
 from agent_sdk.ids import new_id
 from agent_sdk.models.litellm_gateway import ModelRequest, ToolCallCompleted
 from agent_sdk.permissions.models import PermissionDecision, PermissionRequest
 from agent_sdk.permissions.policy import PolicyEngine
 from agent_sdk.permissions.rules import PermissionRule
+from agent_sdk.prompts.models import PromptManifest
 from agent_sdk.runtime._recovery_observability import hashed_identity
 from agent_sdk.runtime.agents import AgentRegistry
 from agent_sdk.runtime.engine import (
     RunEngine,
     _add_usage,
     _model_request_fingerprint,
     _tool_base_recovery_metadata,
     _tool_request_fingerprint,
 )
 from agent_sdk.runtime.execution import (
@@ -44,20 +46,21 @@ from agent_sdk.runtime.leases import (
 )
 from agent_sdk.runtime.models import (
     RunFailure,
     RunResult,
     RunSnapshot,
     RunStatus,
     SessionSnapshot,
     SessionStatus,
     TokenUsage,
     mutable_model_params,
+    run_created_event_matches,
 )
 from agent_sdk.runtime.provider_recovery import (
     ProviderRecoveryAdapter,
     ProviderRecoveryDisposition,
     ProviderRecoveryRegistry,
     ProviderRecoveryRequest,
     ProviderRecoveryResult,
 )
 from agent_sdk.runtime.reconciliation import (
     ExternalOperation,
@@ -65,36 +68,40 @@ from agent_sdk.runtime.reconciliation import (
     ExternalOperationStatus,
     ModelCallOperation,
     ReconciliationAction,
     ReconciliationRequest,
     ReconciliationResolution,
     ReconciliationStatus,
     RecoveryStateConflictError,
     RunCheckpoint,
     RunCheckpointPhase,
     ToolCallOperation,
+    deserialize_model_request,
 )
 from agent_sdk.runtime.session_lifecycle import (
     detach_run_transition,
     exact_run_precondition,
     exact_session_precondition,
     session_write,
 )
 from agent_sdk.storage.base import (
     canonical_snapshot_data,
+    CommitBatch,
     CommitResult,
     EventPrecondition,
     ExternalOperationWrite,
     ReconciliationRequestWrite,
     RunCheckpointWrite,
     RunProgressBatch,
     RunRecoveryEvidencePrecondition,
+    SnapshotPrecondition,
+    SnapshotPreconditionError,
     SnapshotWrite,
     StateStore,
 )
 from agent_sdk.tools.models import ToolResult, ToolResultStatus, ToolRetryPolicy, thaw_json
 from agent_sdk.tools.registry import (
     RegisteredTool,
     ToolRegistry,
     builtin_permission_argument_names,
 )

@@ -253,20 +260,21 @@ class RunRecoveryService:

     async def _plan_private(self, run_id: str) -> RecoveryPlan:
         run = await self._load_run(run_id)
         if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
             return RecoveryPlan("detached", run_id)
         if run.status is RunStatus.WAITING_RECONCILIATION:
             await self._validated_pending_requests(run)
             return RecoveryPlan("detached", run_id)
         evidence = await self._load_evidence(run)
         checkpoint = evidence.checkpoint
+        await self._authenticate_prepared_references(evidence)
         try:
             request = await self._validated_request(evidence)
         except AgentSDKError:
             if (
                 run.status is RunStatus.INTERRUPTED
                 and checkpoint is not None
                 and checkpoint.phase is RunCheckpointPhase.TOOL_IN_FLIGHT
             ):
                 return RecoveryPlan(
                     "reconcile",
@@ -1810,29 +1818,26 @@ class RunRecoveryService:
             return False
         requested_index = requested_positions[0]
         resolved_index = resolved_positions[0]
         messages_before = self._messages_before_turn(
             evidence,
             base_request,
             operation.turn,
         )
         if messages_before is None:
             return False
-        try:
-            reconstructed = ModelRequest(
-                model=base_request.model,
-                messages=messages_before,
-                tools=base_request.tools,
-                params=dict(base_request.params),
-                purpose=base_request.purpose,
-            )
-        except Exception:
+        reconstructed = self._request_for_model_operation(
+            base_request=base_request,
+            messages=messages_before,
+            operation=operation,
+        )
+        if reconstructed is None:
             return False
         metadata = dict(operation.recovery_metadata)
         metadata_valid = metadata == {
             "authoritative_status": False,
             "same_operation_id_resend": False,
         } or (
             set(metadata)
             == {
                 "adapter_id",
                 "adapter_version",
@@ -2759,29 +2764,26 @@ class RunRecoveryService:
             or not self._is_valid_run_event_envelope(evidence)
             or not self._is_valid_certified_lifecycle_positions(
                 evidence,
                 current_kind=operation.operation_kind,
             )
         ):
             return False
         if isinstance(operation, ModelCallOperation):
             if not self._is_valid_certified_provider_history(evidence):
                 return False
-            try:
-                reconstructed = ModelRequest(
-                    model=base_request.model,
-                    messages=tuple(checkpoint.model_dump(mode="json")["messages"]),
-                    tools=base_request.tools,
-                    params=base_request.params,
-                    purpose=base_request.purpose,
-                )
-            except Exception:
+            reconstructed = self._request_for_model_operation(
+                base_request=base_request,
+                messages=tuple(checkpoint.model_dump(mode="json")["messages"]),
+                operation=operation,
+            )
+            if reconstructed is None:
                 return False
             metadata = dict(operation.recovery_metadata)
             metadata_valid = metadata == {
                 "authoritative_status": False,
                 "same_operation_id_resend": False,
             } or (
                 set(metadata)
                 == {
                     "adapter_id",
                     "adapter_version",
@@ -3055,21 +3057,28 @@ class RunRecoveryService:
             != tuple(range(1, len(events) + 1))
             or any(event.type not in _CERTIFIED_RUN_EVENT_TYPES for event in events)
             or sum(event.type == "run.created" for event in events) != 1
             or sum(event.type == "run.started" for event in events) != 1
         ):
             return False
         if any(
             not isinstance(event.event_id, str)
             or not event.event_id.strip()
             or type(event.schema_version) is not int
-            or event.schema_version != 1
+            or (
+                event.type == "run.created"
+                and event.schema_version not in {1, 2}
+            )
+            or (
+                event.type != "run.created"
+                and event.schema_version != 1
+            )
             or not isinstance(event.type, str)
             or not event.type.strip()
             or event.session_id != run.session_id
             or event.run_id != run.run_id
             or type(event.sequence) is not int
             or not isinstance(event.payload, dict)
             or not isinstance(event.occurred_at, datetime)
             or event.occurred_at.tzinfo is None
             or event.occurred_at.utcoffset() is None
             for event in events
@@ -3117,37 +3126,27 @@ class RunRecoveryService:
             or any(
                 not RunRecoveryService._is_valid_tool_recovery_audit(
                     event,
                     evidence.operations,
                 )
                 for event in events
                 if event.type == "tool.recovery.retry.started"
             )
         ):
             return False
-        created = RunSnapshot(
-            run_id=run.run_id,
-            session_id=run.session_id,
-            agent_revision=run.agent_revision,
-            status=RunStatus.CREATED,
-            user_input=run.user_input,
-            parent_run_id=run.parent_run_id,
-            workflow_run_id=run.workflow_run_id,
-            workflow_node_id=run.workflow_node_id,
-            workflow_node_execution=run.workflow_node_execution,
-            task_envelope=run.task_envelope,
-            execution_compatibility=run.execution_compatibility,
-            execution_descriptor=run.execution_descriptor,
-        )
         return (
             events[0].type == "run.created"
-            and events[0].payload == created.model_dump(mode="json")
+            and run_created_event_matches(
+                run,
+                events[0].payload,
+                schema_version=events[0].schema_version,
+            )
             and events[1].type == "run.started"
             and events[1].payload == {"status": RunStatus.RUNNING.value}
         )

     @staticmethod
     def _is_valid_model_recovery_audit(
         event: EventEnvelope,
         operations: tuple[ExternalOperation, ...],
     ) -> bool:
         action = event.type.removeprefix("model.recovery.").removesuffix(".started")
@@ -3804,21 +3803,25 @@ class RunRecoveryService:
                 permission_allowed = None
                 model_deltas = []
                 model_usage = None
                 state = "model_starting"
                 continue
             if event_type == "model.call.started":
                 current_model = model_operations.get(turn)
                 if (
                     state != "model_starting"
                     or current_model is None
-                    or payload != {"model": descriptor.agent.model}
+                    or payload
+                    != RunRecoveryService._model_started_payload(
+                        current_model,
+                        model=descriptor.agent.model,
+                    )
                 ):
                     return False
                 state = "model_in_flight"
                 continue
             if event_type == "model.text.delta":
                 if (
                     state != "model_in_flight"
                     or set(payload) != {"text"}
                     or not isinstance(payload["text"], str)
                 ):
@@ -4345,46 +4348,111 @@ class RunRecoveryService:
             "tool.call.authorized": len(tool_operations),
             "tool.call.started": len(tool_operations),
             "tool.call.completed": len(checkpoint.tool_results),
             "step.completed": checkpoint.turn,
         }
         if any(
             sum(event.type == event_type for event in logical_events) != expected
             for event_type, expected in expected_counts.items()
         ):
             return False
+        started_events = tuple(
+            event for event in logical_events if event.type == "model.call.started"
+        )
+        ordered_model_operations = tuple(
+            sorted(model_operations, key=lambda operation: operation.turn)
+        )
         if (
             any(
                 event.payload != {}
                 for event in logical_events
                 if event.type == "step.started"
             )
             or any(
-                event.payload != {"model": descriptor.agent.model}
-                for event in logical_events
-                if event.type == "model.call.started"
+                event.payload
+                != RunRecoveryService._model_started_payload(
+                    operation,
+                    model=descriptor.agent.model,
+                )
+                for event, operation in zip(
+                    started_events,
+                    ordered_model_operations,
+                    strict=True,
+                )
             )
             or sum(
                 event.type == "permission.requested" for event in logical_events
             )
             != sum(event.type == "permission.resolved" for event in logical_events)
         ):
             return False
         last_interrupted = max(
             index for index, event in enumerate(events) if event.type == "run.interrupted"
         )
         return all(
             event.type
             in {"model.recovery.query.started", "model.recovery.resend.started"}
             for event in events[last_interrupted + 1 :]
         )

+    @staticmethod
+    def _model_started_payload(
+        operation: ModelCallOperation,
+        *,
+        model: str,
+    ) -> dict[str, Any]:
+        if operation.prepared_request is None:
+            return {"model": model}
+        assert operation.context_view_id is not None
+        assert operation.prompt_manifest_id is not None
+        return {
+            "model": model,
+            "context_view_id": operation.context_view_id,
+            "prompt_manifest_id": operation.prompt_manifest_id,
+            "request_fingerprint": operation.request_fingerprint,
+        }
+
+    @staticmethod
+    def _request_for_model_operation(
+        *,
+        base_request: ModelRequest,
+        messages: tuple[dict[str, Any], ...],
+        operation: ModelCallOperation,
+    ) -> ModelRequest | None:
+        try:
+            if operation.prepared_request is not None:
+                request = deserialize_model_request(operation.prepared_request)
+                if (
+                    request.model != base_request.model
+                    or request.tools != base_request.tools
+                    or request.params != base_request.params
+                    or request.purpose != base_request.purpose
+                ):
+                    return None
+            else:
+                request = ModelRequest(
+                    model=base_request.model,
+                    messages=messages,
+                    tools=base_request.tools,
+                    params=dict(base_request.params),
+                    purpose=base_request.purpose,
+                )
+            if (
+                operation.provider_identity != base_request.model
+                or operation.request_fingerprint
+                != _model_request_fingerprint(request)
+            ):
+                return None
+            return request
+        except Exception:
+            return None
+
     @staticmethod
     def _is_valid_certified_terminal_provider_turns(
         evidence: _RecoveryEvidence,
         *,
         base_request: ModelRequest,
         terminal_status: RunStatus,
     ) -> bool:
         checkpoint = evidence.checkpoint
         if checkpoint is None or evidence.run.execution_descriptor is None:
             return False
@@ -4409,35 +4477,26 @@ class RunRecoveryService:
         ):
             return False
         messages_before = RunRecoveryService._messages_before_turn(
             evidence,
             base_request,
             checkpoint.turn,
         )
         if messages_before is None:
             return False
         final_operation = model_operations[checkpoint.turn]
-        try:
-            final_request = ModelRequest(
-                model=base_request.model,
-                messages=messages_before,
-                tools=base_request.tools,
-                params=dict(base_request.params),
-                purpose=base_request.purpose,
-            )
-        except Exception:
-            return False
-        if (
-            final_operation.provider_identity != base_request.model
-            or final_operation.request_fingerprint
-            != _model_request_fingerprint(final_request)
-        ):
+        final_request = RunRecoveryService._request_for_model_operation(
+            base_request=base_request,
+            messages=messages_before,
+            operation=final_operation,
+        )
+        if final_request is None:
             return False

         output_parts: list[str] = []
         usage = TokenUsage()
         for turn in range(checkpoint.turn):
             completed = RunRecoveryService._completed_model_outcome(
                 model_operations[turn]
             )
             if completed is None or len(completed[2]) != 1:
                 return False
@@ -4508,20 +4567,21 @@ class RunRecoveryService:
             and terminal_event.payload.get("error", {}).get("code")
             == final_error.get("code")
             and terminal_event.payload.get("error", {}).get("message")
             == final_error.get("message")
         )

     async def _validated_request(
         self,
         evidence: _RecoveryEvidence,
     ) -> ModelRequest | None:
+        await self._authenticate_prepared_references(evidence)
         run = evidence.run
         descriptor = run.execution_descriptor
         if run.execution_compatibility != "current" or descriptor is None:
             return None
         try:
             registered_agent = self._agents.resolve(run.agent_revision)
         except AgentSDKError:
             raise self._capability_error() from None
         policy_config = self._policy.execution_config()
         live_policy = ExecutionPolicyDescriptor.create(
@@ -4542,35 +4602,113 @@ class RunRecoveryService:
         if live_descriptor != descriptor:
             raise self._capability_error() from None
         request = ModelRequest(
             model=registered_agent.model,
             messages=descriptor_messages,
             tools=self._tools.schemas(),
             params=mutable_model_params(registered_agent.model_params),
         )
         return request

+    async def _authenticate_prepared_references(
+        self,
+        evidence: _RecoveryEvidence,
+    ) -> None:
+        for operation in evidence.operations:
+            if (
+                isinstance(operation, ModelCallOperation)
+                and operation.prepared_request is not None
+            ):
+                await self._authenticate_prepared_operation(
+                    operation,
+                    session_id=evidence.run.session_id,
+                    run_id=evidence.run.run_id,
+                )
+
+    async def _authenticate_prepared_operation(
+        self,
+        operation: ModelCallOperation,
+        *,
+        session_id: str,
+        run_id: str,
+    ) -> None:
+        context_view_id = operation.context_view_id
+        prompt_manifest_id = operation.prompt_manifest_id
+        if (
+            operation.session_id != session_id
+            or operation.run_id != run_id
+            or context_view_id is None
+            or prompt_manifest_id is None
+        ):
+            raise RecoveryStateConflictError
+        try:
+            raw_view = await self._store.get_snapshot(
+                "context_view",
+                context_view_id,
+            )
+            raw_manifest = await self._store.get_snapshot(
+                "prompt_manifest",
+                prompt_manifest_id,
+            )
+            view = ContextView.model_validate(raw_view)
+            manifest = PromptManifest.model_validate(raw_manifest)
+            if (
+                view.view_id != context_view_id
+                or view.session_id != session_id
+                or manifest.manifest_id != prompt_manifest_id
+                or manifest.context_view_id != context_view_id
+                or manifest.model != operation.provider_identity
+            ):
+                raise ValueError("prepared reference identity mismatch")
+            await self._store.commit(
+                CommitBatch(
+                    events=(),
+                    preconditions=(
+                        SnapshotPrecondition(
+                            "context_view",
+                            context_view_id,
+                            session_id=session_id,
+                            data=view.model_dump(mode="json"),
+                        ),
+                        SnapshotPrecondition(
+                            "prompt_manifest",
+                            prompt_manifest_id,
+                            session_id=session_id,
+                            data=manifest.model_dump(mode="json"),
+                        ),
+                    ),
+                )
+            )
+        except RecoveryStateConflictError:
+            raise
+        except (AgentSDKError, SnapshotPreconditionError, TypeError, ValueError):
+            raise RecoveryStateConflictError from None
+
     @staticmethod
     def _is_pristine_created(evidence: _RecoveryEvidence) -> bool:
         run = evidence.run
         return (
             run.status is RunStatus.CREATED
             and run.version == 1
             and evidence.checkpoint is None
             and not evidence.operations
             and not evidence.pending
             and len(evidence.run_events) == 1
             and evidence.run_events[0].type == "run.created"
             and evidence.run_events[0].sequence == 1
             and evidence.run_events[0].session_id == run.session_id
             and evidence.run_events[0].run_id == run.run_id
-            and evidence.run_events[0].payload == run.model_dump(mode="json")
+            and run_created_event_matches(
+                run,
+                evidence.run_events[0].payload,
+                schema_version=evidence.run_events[0].schema_version,
+            )
         )

     def _effective_resolved_evidence(
         self,
         evidence: _RecoveryEvidence,
         base_request: ModelRequest,
     ) -> _RecoveryEvidence | None:
         resolved = tuple(
             request
             for request in evidence.reconciliations
@@ -5084,32 +5222,28 @@ class RunRecoveryService:
                     "authoritative_status",
                     "same_operation_id_resend",
                 }
                 and all(
                     isinstance(metadata[field], str) and bool(metadata[field])
                     for field in ("adapter_id", "adapter_version")
                 )
                 and type(metadata["authoritative_status"]) is bool
                 and type(metadata["same_operation_id_resend"]) is bool
             )
-            try:
-                expected_fingerprint = _model_request_fingerprint(
-                    ModelRequest(
-                        model=base_request.model,
-                        messages=messages,
-                        tools=base_request.tools,
-                        params=dict(base_request.params),
-                        purpose=base_request.purpose,
-                    )
-                )
-            except Exception:
+            reconstructed = RunRecoveryService._request_for_model_operation(
+                base_request=base_request,
+                messages=messages,
+                operation=operation,
+            )
+            if reconstructed is None:
                 return False
+            expected_fingerprint = _model_request_fingerprint(reconstructed)
             return (
                 metadata_valid
                 and operation.provider_identity == base_request.model
                 and operation.request_fingerprint == expected_fingerprint
             )

         assert isinstance(operation, ToolCallOperation)
         proposed = evidence.run_events[attempt_start]
         if (
             proposed.type != "tool.call.proposed"
@@ -5202,32 +5336,26 @@ class RunRecoveryService:
                 completed_operations = tuple(
                     operation
                     for operation in evidence.operations
                     if isinstance(operation, ModelCallOperation)
                     and operation.turn == turn
                     and operation.status is ExternalOperationStatus.COMPLETED
                 )
                 if len(completed_operations) != 1:
                     return None
                 operation = completed_operations[0]
-                request = ModelRequest(
-                    model=base_request.model,
+                request = RunRecoveryService._request_for_model_operation(
+                    base_request=base_request,
                     messages=tuple(messages),
-                    tools=base_request.tools,
-                    params=dict(base_request.params),
-                    purpose=base_request.purpose,
+                    operation=operation,
                 )
-                if (
-                    operation.provider_identity != base_request.model
-                    or operation.request_fingerprint
-                    != _model_request_fingerprint(request)
-                ):
+                if request is None:
                     return None
                 completed = RunRecoveryService._completed_model_outcome(operation)
                 if completed is None or len(completed[2]) != 1:
                     return None
                 _finish_reason, text, calls, _usage = completed
                 call = calls[0]
                 messages.append(
                     {
                         "role": "assistant",
                         "content": text or None,
@@ -5508,31 +5636,26 @@ class RunRecoveryService:
                     if isinstance(item, ToolCallOperation)
                 )
                 if (
                     operation.run_id != evidence.run.run_id
                     or operation.session_id != evidence.run.session_id
                     or operation.provider_identity != base_request.model
                     or len(tool_operations) > (0 if turn == checkpoint.turn else 1)
                     or len(turn_operations) != 1 + len(tool_operations)
                 ):
                     return False
-                request = ModelRequest(
-                    model=base_request.model,
+                request = RunRecoveryService._request_for_model_operation(
+                    base_request=base_request,
                     messages=tuple(reconstructed_messages),
-                    tools=base_request.tools,
-                    params=dict(base_request.params),
-                    purpose=base_request.purpose,
+                    operation=operation,
                 )
-                if (
-                    _model_request_fingerprint(request)
-                    != operation.request_fingerprint
-                ):
+                if request is None:
                     return False
                 outcome = RunRecoveryService._completed_model_outcome(operation)
                 if outcome is None or len(outcome[2]) != 1:
                     return False
                 finish_reason, text, calls, usage = outcome
                 call = calls[0]
                 reconstructed_messages.append(
                     {
                         "role": "assistant",
                         "content": text or None,
@@ -5608,21 +5731,25 @@ class RunRecoveryService:
             )
             terminal = tuple(
                 index
                 for index, event in enumerate(segment)
                 if event.type == "model.call.completed"
             )
             if (
                 len(started) != 1
                 or len(terminal) != 1
                 or started[0] >= terminal[0]
-                or segment[started[0]].payload != {"model": base_request.model}
+                or segment[started[0]].payload
+                != RunRecoveryService._model_started_payload(
+                    operation,
+                    model=base_request.model,
+                )
                 or segment[terminal[0]].payload
                 != {"finish_reason": finish_reason}
             ):
                 return False
             between = segment[started[0] + 1 : terminal[0]]
             deltas = tuple(
                 event.payload.get("text")
                 for event in between
                 if event.type == "model.text.delta"
             )
@@ -5737,31 +5864,26 @@ class RunRecoveryService:
                     or operation.session_id != evidence.run.session_id
                     or operation.provider_identity != base_request.model
                     or turn_operations
                     != (
                         (operation,)
                         if tool_operation is None
                         else (operation, tool_operation)
                     )
                 ):
                     return False
-                request = ModelRequest(
-                    model=base_request.model,
+                request = RunRecoveryService._request_for_model_operation(
+                    base_request=base_request,
                     messages=tuple(reconstructed_messages),
-                    tools=base_request.tools,
-                    params=dict(base_request.params),
-                    purpose=base_request.purpose,
+                    operation=operation,
                 )
-                if (
-                    operation.request_fingerprint
-                    != _model_request_fingerprint(request)
-                ):
+                if request is None:
                     return False
                 outcome = RunRecoveryService._completed_model_outcome(operation)
                 if outcome is None or len(outcome[2]) != 1:
                     return False
                 finish_reason, text, calls, usage = outcome
                 call = calls[0]
                 reconstructed_messages.append(
                     {
                         "role": "assistant",
                         "content": text or None,
@@ -5852,21 +5974,25 @@ class RunRecoveryService:
             )
             terminal = tuple(
                 index
                 for index, event in enumerate(segment)
                 if event.type == "model.call.completed"
             )
             if (
                 len(started) != 1
                 or len(terminal) != 1
                 or started[0] >= terminal[0]
-                or segment[started[0]].payload != {"model": base_request.model}
+                or segment[started[0]].payload
+                != RunRecoveryService._model_started_payload(
+                    operation,
+                    model=base_request.model,
+                )
                 or segment[terminal[0]].payload
                 != {"finish_reason": finish_reason}
             ):
                 return False
             between = segment[started[0] + 1 : terminal[0]]
             deltas = tuple(
                 event.payload.get("text")
                 for event in between
                 if event.type == "model.text.delta"
             )
@@ -6110,30 +6236,28 @@ class RunRecoveryService:
             "same_operation_id_resend": adapter.same_operation_id_resend,
         }
         if (
             dict(metadata) != expected_metadata
             or type(metadata.get("authoritative_status")) is not bool
             or type(metadata.get("same_operation_id_resend")) is not bool
             or not (adapter.authoritative_status or adapter.same_operation_id_resend)
         ):
             return None
         checkpoint_data = checkpoint.model_dump(mode="json")
-        reconstructed = ModelRequest(
-            model=base_request.model,
+        reconstructed = self._request_for_model_operation(
+            base_request=base_request,
             messages=tuple(checkpoint_data["messages"]),
-            tools=base_request.tools,
-            params=base_request.params,
-            purpose=base_request.purpose,
+            operation=operation,
         )
+        if reconstructed is None:
+            return None
         try:
-            if _model_request_fingerprint(reconstructed) != operation.request_fingerprint:
-                return None
             return ProviderRecoveryRequest(
                 session_id=operation.session_id,
                 run_id=operation.run_id,
                 turn=operation.turn,
                 operation_id=operation.operation_id,
                 provider_identity=operation.provider_identity,
                 request_fingerprint=operation.request_fingerprint,
                 model_request=reconstructed,
             )
         except Exception:
@@ -6247,31 +6371,26 @@ class RunRecoveryService:
                     or model_operation.session_id != evidence.run.session_id
                     or (
                         tool_operation is not None
                         and (
                             tool_operation.run_id != evidence.run.run_id
                             or tool_operation.session_id != evidence.run.session_id
                         )
                     )
                 ):
                     return None
-                request = ModelRequest(
-                    model=base_request.model,
+                request = self._request_for_model_operation(
+                    base_request=base_request,
                     messages=tuple(reconstructed_messages),
-                    tools=base_request.tools,
-                    params=dict(base_request.params),
-                    purpose=base_request.purpose,
+                    operation=model_operation,
                 )
-                if (
-                    _model_request_fingerprint(request)
-                    != model_operation.request_fingerprint
-                ):
+                if request is None:
                     return None
                 completed = self._completed_model_outcome(model_operation)
                 if completed is None:
                     return None
                 finish_reason, text, raw_calls, operation_usage = completed
                 if len(raw_calls) != 1:
                     return None
                 raw_call = raw_calls[0]
                 call = ToolCallCompleted(
                     index=0,
@@ -6678,49 +6797,53 @@ class RunRecoveryService:
             expected_identity = {
                 "call_id": turn.call.call_id,
                 "tool_name": turn.call.name,
             }
             base_positions = (
                 start,
                 model_started[0][0],
                 model_completed[0][0],
                 proposed[0][0],
             )
+            model_operation = next(
+                (
+                    operation
+                    for operation in evidence.operations
+                    if isinstance(operation, ModelCallOperation)
+                    and operation.turn == index
+                ),
+                None,
+            )
             if base_positions != tuple(sorted(base_positions)) or len(
                 set(base_positions)
             ) != 4:
                 return False
             if (
                 events[start].payload != {}
-                or model_started[0][1].payload != {"model": descriptor.agent.model}
+                or model_operation is None
+                or model_started[0][1].payload
+                != self._model_started_payload(
+                    model_operation,
+                    model=descriptor.agent.model,
+                )
                 or model_completed[0][1].payload
                 != {"finish_reason": turn.finish_reason}
                 or proposed[0][1].payload != expected_identity
             ):
                 return False
             deltas = tuple(
                 event.payload.get("text")
                 for event in events[model_started[0][0] + 1 : model_completed[0][0]]
                 if event.type == "model.text.delta"
             )
-            model_operation = next(
-                (
-                    operation
-                    for operation in evidence.operations
-                    if isinstance(operation, ModelCallOperation)
-                    and operation.turn == index
-                ),
-                None,
-            )
             recovered_model = (
-                model_operation is not None
-                and model_operation.operation_id in recovered_model_operation_ids
+                model_operation.operation_id in recovered_model_operation_ids
             )
             if any(not isinstance(delta, str) for delta in deltas):
                 return False
             text_deltas = tuple(
                 delta for delta in deltas if isinstance(delta, str)
             )
             if (
                 recovered_model
                 and not self._is_exact_durable_text_prefix(
                     text_deltas,
@@ -6916,20 +7039,29 @@ class RunRecoveryService:
         if request.run_id != run.run_id or request.session_id != run.session_id:
             raise self._state_error() from None
         if request.operation_id is not None:
             operation = await self._store.get_external_operation(request.operation_id)
             if (
                 operation is None
                 or operation.run_id != run.run_id
                 or operation.session_id != run.session_id
             ):
                 raise self._state_error() from None
+            if (
+                isinstance(operation, ModelCallOperation)
+                and operation.prepared_request is not None
+            ):
+                await self._authenticate_prepared_operation(
+                    operation,
+                    session_id=run.session_id,
+                    run_id=run.run_id,
+                )
         return tuple(
             ReconciliationRequest.model_validate_json(item.model_dump_json())
             for item in requests
         )

     async def _coordinate_reconciliation(
         self,
         run_id: str,
         *,
         reason: str,
diff --git a/src/agent_sdk/skills/registry.py b/src/agent_sdk/skills/registry.py
index c98ff91..657e400 100644
--- a/src/agent_sdk/skills/registry.py
+++ b/src/agent_sdk/skills/registry.py
@@ -1,17 +1,19 @@
 from __future__ import annotations

 from collections.abc import Iterable
 from dataclasses import dataclass
 from pathlib import Path

 from agent_sdk.errors import AgentSDKError, ErrorCode
+from agent_sdk.runtime.execution import DurableAgentSpec
+from agent_sdk.runtime.models import AgentSpec
 from agent_sdk.skills.loader import load_skill
 from agent_sdk.skills.models import (
     ActivatedSkill,
     PathIdentity,
     SkillMetadata,
     _path_identity,
 )


 @dataclass(frozen=True)
@@ -110,20 +112,31 @@ class SkillRegistry:
                 "skill identity changed during activation",
                 retryable=False,
             )
         return ActivatedSkill._from_pinned(
             metadata=metadata,
             instructions=parsed.instructions,
             root=entry.skill_root,
             root_identity=entry.skill_root_identity,
         )

+    def validate_agent(self, agent: AgentSpec | DurableAgentSpec) -> None:
+        for name in agent.skills:
+            try:
+                self.activate(name)
+            except AgentSDKError:
+                raise AgentSDKError(
+                    ErrorCode.INVALID_STATE,
+                    "configured agent skill unavailable",
+                    retryable=False,
+                ) from None
+
     @classmethod
     def _verify_entry(cls, entry: _CatalogEntry) -> None:
         cls._verify_directory(
             entry.configured_root,
             entry.configured_root_identity,
         )
         cls._verify_directory(entry.skill_root, entry.skill_root_identity)
         try:
             entry.skill_root.relative_to(entry.configured_root)
             entry.metadata.location.relative_to(entry.skill_root)
diff --git a/src/agent_sdk/storage/sqlite.py b/src/agent_sdk/storage/sqlite.py
index 329d142..9a358d7 100644
--- a/src/agent_sdk/storage/sqlite.py
+++ b/src/agent_sdk/storage/sqlite.py
@@ -15,21 +15,26 @@ import aiosqlite
 if TYPE_CHECKING:
     from agent_sdk.storage.migrations import Migration

 from agent_sdk.events.models import EventEnvelope
 from agent_sdk.runtime.leases import (
     Lease,
     LeaseHeldError,
     LeaseLostError,
     canonical_lease_timestamp,
 )
-from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot
+from agent_sdk.runtime.models import (
+    RunSnapshot,
+    RunStatus,
+    SessionSnapshot,
+    run_created_event_matches,
+)
 from agent_sdk.runtime.reconciliation import (
     ExternalOperation,
     ExternalOperationStatus,
     ModelCallOperation,
     ReconciliationAction,
     ReconciliationRequest,
     ReconciliationStatus,
     RecoveryStateConflictError,
     RunCheckpoint,
     RunCheckpointPhase,
@@ -1630,32 +1635,97 @@ class SQLiteStore:
                 """
                 SELECT version, session_id, data_json
                 FROM snapshots WHERE kind = ? AND entity_id = ?
                 """,
                 (precondition.kind, precondition.entity_id),
             ) as cursor:
                 row = await cursor.fetchone()
             if row is None:
                 raise SnapshotPreconditionError("snapshot precondition failed")
             version = cast(int, row[0])
+            data_matches = True
+            if (
+                precondition.data is not None
+                and cast(str, row[2]) != _canonical_json(precondition.data)
+            ):
+                data_matches = await self._legacy_v1_run_snapshot_matches(
+                    precondition,
+                    cast(str, row[2]),
+                )
             if (
                 precondition.version is not None
                 and version != precondition.version
             ) or (
                 precondition.session_id is not None
                 and cast(str, row[1]) != precondition.session_id
             ) or (
-                precondition.data is not None
-                and cast(str, row[2]) != _canonical_json(precondition.data)
+                precondition.data is not None and not data_matches
             ):
                 raise SnapshotPreconditionError("snapshot precondition failed")

+    async def _legacy_v1_run_snapshot_matches(
+        self,
+        precondition: SnapshotPrecondition,
+        stored_json: str,
+    ) -> bool:
+        if precondition.kind != "run" or precondition.data is None:
+            return False
+        try:
+            stored_data = _strict_json_object(stored_json)
+            if _canonical_json(stored_data) != stored_json:
+                return False
+            stored = RunSnapshot.model_validate(stored_data)
+        except (TypeError, ValueError):
+            return False
+        if stored.run_id != precondition.entity_id:
+            return False
+        async with self._connection.execute(
+            """
+            SELECT session_id, sequence, schema_version, payload_json
+            FROM events
+            WHERE run_id = ? AND type = 'run.created'
+            """,
+            (precondition.entity_id,),
+        ) as cursor:
+            creation_rows = tuple(await cursor.fetchall())
+        if len(creation_rows) != 1:
+            return False
+        creation = creation_rows[0]
+        event_session_id = creation[0]
+        sequence = creation[1]
+        schema_version = creation[2]
+        payload_json = creation[3]
+        try:
+            if (
+                not isinstance(event_session_id, str)
+                or event_session_id != stored.session_id
+                or type(sequence) is not int
+                or sequence != 1
+                or type(schema_version) is not int
+                or schema_version != 1
+                or not isinstance(payload_json, str)
+            ):
+                return False
+            event_payload = _strict_json_object(payload_json)
+            if _canonical_json(event_payload) != payload_json:
+                return False
+            if not run_created_event_matches(
+                stored,
+                event_payload,
+                schema_version=1,
+            ):
+                return False
+            expected = RunSnapshot.model_validate(precondition.data)
+        except (TypeError, ValueError):
+            return False
+        return stored == expected
+
     async def _check_run_recovery_evidence_precondition(
         self,
         expected: RunRecoveryEvidencePrecondition | None,
     ) -> None:
         if expected is None:
             return
         message = "run recovery evidence precondition failed"
         try:
             async with self._connection.execute(
                 "SELECT data_json FROM run_checkpoints WHERE run_id = ?",
@@ -2157,22 +2227,21 @@ class SQLiteStore:
                 raise RecoveryStateConflictError
             try:
                 run_data = _strict_json_object(cast(str, run_row[2]))
                 run = RunSnapshot.model_validate(run_data)
             except (TypeError, ValueError):
                 raise RecoveryStateConflictError from None
             if (
                 run.run_id != run_id
                 or run.session_id != cast(str, run_row[0])
                 or run.version != cast(int, run_row[1])
-                or _canonical_json(run.model_dump(mode="json"))
-                != cast(str, run_row[2])
+                or _canonical_json(run_data) != cast(str, run_row[2])
             ):
                 raise RecoveryStateConflictError
             async with self._connection.execute(
                 """
                 SELECT session_id, version, data_json FROM snapshots
                 WHERE kind = 'session' AND entity_id = ?
                 """,
                 (run.session_id,),
             ) as cursor:
                 session_row = await cursor.fetchone()
@@ -4415,32 +4484,34 @@ async def _validate_v1_events(
             evaluation_event is None
             or evaluation_event.session_id != evaluation.session_id
             or evaluation_event.run_id != evaluation_id
         ):
             raise ValueError("incompatible version-1 evaluation facts")


 async def _validate_current_projection_rows(connection: aiosqlite.Connection) -> None:
     from agent_sdk.context.models import ContextCapsule, ContextView
     from agent_sdk.evaluation.models import EvaluationResult
+    from agent_sdk.prompts.models import PromptManifest
     from agent_sdk.runtime.models import RunSnapshot, SessionSnapshot
     from agent_sdk.workflow.models import WorkflowNodeSnapshot, WorkflowRunSnapshot

     try:
         rows = await _snapshot_rows(connection)
         decoded = {row: _strict_json_object(row.data_json) for row in rows}
         sessions: dict[str, SessionSnapshot] = {}
         runs: dict[str, RunSnapshot] = {}
         workflows: dict[str, WorkflowRunSnapshot] = {}
         nodes: dict[str, WorkflowNodeSnapshot] = {}
         capsules: dict[str, tuple[str, ContextCapsule]] = {}
         views: dict[str, ContextView] = {}
+        prompt_manifests: dict[str, tuple[str, PromptManifest]] = {}
         evaluations: dict[str, EvaluationResult] = {}
         for row in rows:
             if row.kind != "session":
                 continue
             session = SessionSnapshot.model_validate(decoded[row])
             if (
                 session.session_id != row.entity_id
                 or row.session_id != session.session_id
                 or row.version != session.version
             ):
@@ -4488,20 +4559,31 @@ async def _validate_current_projection_rows(connection: aiosqlite.Connection) ->
                 capsules[row.entity_id] = (row.session_id, capsule_value)
             elif row.kind == "context_view":
                 view_value = ContextView.model_validate(data)
                 if (
                     view_value.view_id != row.entity_id
                     or view_value.session_id != row.session_id
                     or row.version != 1
                 ):
                     raise ValueError("current context view identity is invalid")
                 views[view_value.view_id] = view_value
+            elif row.kind == "prompt_manifest":
+                manifest_value = PromptManifest.model_validate(data)
+                if (
+                    manifest_value.manifest_id != row.entity_id
+                    or row.version != 1
+                ):
+                    raise ValueError("current prompt manifest identity is invalid")
+                prompt_manifests[manifest_value.manifest_id] = (
+                    row.session_id,
+                    manifest_value,
+                )
             elif row.kind == "evaluation":
                 evaluation_value = EvaluationResult.model_validate(data)
                 if (
                     evaluation_value.evaluation_id != row.entity_id
                     or evaluation_value.session_id != row.session_id
                     or evaluation_value.record_version != row.version
                 ):
                     raise ValueError("current evaluation identity is invalid")
                 evaluations[evaluation_value.evaluation_id] = evaluation_value
             else:
@@ -4512,20 +4594,24 @@ async def _validate_current_projection_rows(connection: aiosqlite.Connection) ->
                     raise ValueError("current workflow node projection is invalid")
         for node in nodes.values():
             owner_workflow = workflows.get(node.workflow_run_id)
             if owner_workflow is None or owner_workflow.session_id != node.session_id:
                 raise ValueError("current workflow node owner is invalid")
         for view in views.values():
             if view.capsule_id is not None:
                 capsule = capsules.get(view.capsule_id)
                 if capsule is None or capsule[0] != view.session_id:
                     raise ValueError("current context reference is invalid")
+        for session_id, manifest in prompt_manifests.values():
+            manifest_view = views.get(manifest.context_view_id)
+            if manifest_view is None or manifest_view.session_id != session_id:
+                raise ValueError("current prompt manifest context is invalid")
         for evaluation in evaluations.values():
             run = runs.get(evaluation.subject_run_id)
             if run is None or run.session_id != evaluation.session_id:
                 raise ValueError("current evaluation subject is invalid")
         async with connection.execute(
             """
             SELECT scope, key, request_fingerprint, session_id, result_json
             FROM idempotency_records ORDER BY scope, key
             """
         ) as cursor:
diff --git a/tests/docs/test_v01_release_ledger.py b/tests/docs/test_v01_release_ledger.py
index 019ce32..2fb2ada 100644
--- a/tests/docs/test_v01_release_ledger.py
+++ b/tests/docs/test_v01_release_ledger.py
@@ -63,29 +63,34 @@ $ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\integratio
 47 passed in 7.31s

 $ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk\workflow tests\unit\workflow tests\integration\workflow tests\e2e\test_v01_release.py
 All checks passed!

 $ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk\workflow src\agent_sdk\runtime\execution.py
 Success: no issues found in 10 source files

 $ git diff --check f9beb63..826a32b
 clean"""
-R3_PLAN = "docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md"
-R3_RESUME_COMMAND = (
-    r"Get-Content docs\superpowers\plans"
-    r"\2026-07-17-agent-sdk-v0.1-r3-auto-context.md"
-)
 R3_FIRST_TEST = "tests/unit/context/test_deterministic_strategies.py"
-R3_FIRST_RED = (
-    rf".\.venv\Scripts\python.exe -m pytest {R3_FIRST_TEST} -q"
+R3_TASK1_COMMITS = ("dd93fb2", "38e7d2d", "93505aa")
+R3_TASK2_COMMITS = ("3f23363", "e5c646f")
+R3_TASK3_COMMITS = ("774ae6c", "c94ea77")
+R3_TASK4_COMMITS = ("2f2048c", "79996db", "ab1d082")
+R4_PLAN = "docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r4-child-mailbox.md"
+R4_TASK1_TEST = "tests/unit/runtime/test_capability_intersection.py"
+R4_FIRST_COMMAND = (
+    "$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; "
+    r".\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin "
+    r"tests\unit\runtime\test_capability_intersection.py -q"
 )
+R4_TASK2_MAILBOX_TEST = "tests/unit/subagents/test_mailbox.py"
+R3_TASK5_FRESH_RESULT = "221 passed, 1 skipped in 25.32s"


 def _assert_release_checkpoint_and_r3_resume(document: str) -> None:
     for commit in R1_COMMITS:
         assert commit in document
     normalized_document = "\n".join(
         line[2:] if line.startswith("  ") else line
         for line in document.splitlines()
     )
     assert R1_INITIAL_CHECKPOINT in normalized_document
@@ -109,28 +114,70 @@ def _assert_release_checkpoint_and_r3_resume(document: str) -> None:
     ]
     assert "380 passed in 44.02s" not in normalized_document[canonical_r2_index:]
     assert R2_FINAL_CHECKPOINT in normalized_document
     assert "Critical 0 / Important 0 / Minor 0" in document
     assert "Spec compliance PASS" in document
     assert "Code quality PASS" in document
     assert "Ready to proceed to R2: Yes" in document
     assert "Ready to proceed to R3: Yes" in document
     assert "R2 Task 4" in document
     assert "final review Spec approved / Quality approved" in document
-    assert R3_PLAN in document
-    assert R3_RESUME_COMMAND in document
-    assert "R3 Task 1 Step 1" in document
+    for commit in R3_TASK1_COMMITS:
+        assert commit in document
     assert R3_FIRST_TEST in document
-    assert R3_FIRST_RED in document
-    assert "R3 remains pending" in document
-    assert "R3 implementation has not started" in document
+    assert "R3 Task 1 deterministic L0-L2 is complete" in document
+    assert "R3 Task 1 final review: Critical 0 / Important 0 / Minor 0" in document
+    assert "Spec PASS; Quality PASS" in document
+    assert "42 deterministic strategy tests" in document
+    assert "48 context integration tests" in normalized_document
+    for commit in R3_TASK2_COMMITS:
+        assert commit in document
+    assert "102 passed" in document
+    assert "R3 Task 2 is complete" in document or "v0.1 R3 Task 2: complete" in document
+    assert "Critical 0 / Important 0 / Minor 0" in document
+    for commit in R3_TASK3_COMMITS:
+        assert commit in document
+    assert "R3 Task 3 is complete" in document or "v0.1 R3 Task 3: complete" in document
+    assert "AgentSpec" in document
+    assert "DurableAgentSpec" in document
+    assert "SkillRegistry" in document
+    assert "run.created" in document
+    assert "schema v2" in document or "schema-v2" in document
+    assert "schema-v1" in document
+    assert "Critical 0 / Important 0 / Minor 0" in document
+    assert "201 passed" in document
+    assert "521 passed, 1 skipped" in document
+    assert "25 passed" in document
+    assert "92 source files" in document
+    for commit in R3_TASK4_COMMITS:
+        assert commit in document
+    assert "R3 Task 4 is complete" in document or "v0.1 R3 Task 4: complete" in document
+    assert "Task 4 final approval: Critical 0 / Important 0 / Minor 0" in document
+    assert "Spec PASS; Quality PASS" in document
+    assert R3_TASK5_FRESH_RESULT in document
+    assert "13.65s" not in document
+    assert R4_PLAN in document
+    assert R4_TASK1_TEST in document
+    assert R4_FIRST_COMMAND in normalized_document
+    assert "first expected RED" in document
+    assert "created by R4 Task 1" in document
+    assert R4_TASK2_MAILBOX_TEST not in document
+    assert "R3 Task 2 Step 1" not in document
+    assert "tests/unit/context/test_compaction_levels.py" not in document
+    assert "R3 Task 2 remains pending/unstarted" not in document
+    assert "R3 remains pending" not in document
+    assert "R3 is in progress" not in document
+    assert "R3 implementation has not started" not in document
     assert "Tasks 4-5 have not started" not in document
+    assert "R3 Task 4 Step 1" not in document
+    assert "tests/integration/context/test_runtime_middleware.py" not in document
+    assert "tests/integration/context/test_context_recovery.py" not in document


 def test_v01_release_ledger_names_every_required_slice() -> None:
     root = Path(__file__).parents[2]
     ledger = (root / "docs/plans/releases/v0.1.md").read_text(encoding="utf-8")
     progress = (root / ".superpowers/sdd/progress.md").read_text(encoding="utf-8")
     for slice_id in ("R0", "R1", "R2", "R3", "R4", "R5"):
         assert f"| {slice_id} |" in ledger
     assert "0.1.0" in ledger
     assert "post-v0.1" in ledger
@@ -140,44 +187,42 @@ def test_v01_release_ledger_names_every_required_slice() -> None:
         "| R1 | completed | built-in Tool authorization | "
         "2026-07-17 final checkpoint: 97 passed, 3 skipped in 7.94s; "
         "Ruff/mypy clean |"
     ) in ledger
     assert "R1 is complete through final hardening commit `704db69`" in ledger
     assert "final review approved" in ledger
     assert (
         "| R2 | completed | condition and bounded loop | "
         "2026-07-20 final checkpoint: 403 passed in 43.03s; Ruff/mypy clean |"
     ) in ledger
-    for slice_id in ("R3", "R4", "R5"):
+    for slice_id in ("R4", "R5"):
         assert f"| {slice_id} | pending |" in ledger
+    assert "| R3 | completed | automatic L0-L4 | " in ledger
     assert "4 passed in 4.74s" in ledger
     assert "5.05s" not in ledger
     assert "ef0e4da" in ledger
     assert "R1 Tasks 1-3 are complete" in ledger
     historical_marker = "Historical initial checkpoint evidence:"
     canonical_marker = "Current canonical checkpoint evidence:"
     assert ledger.count(historical_marker) == 1
     assert ledger.count(canonical_marker) == 1
     historical_index = ledger.index(historical_marker)
     canonical_index = ledger.index(canonical_marker)
     assert historical_index < canonical_index
     assert "85 passed, 1 skipped in 6.12s" in ledger[
         historical_index:canonical_index
     ]
     assert "85 passed, 1 skipped in 6.12s" not in ledger[canonical_index:]
     assert "97 passed, 3 skipped in 7.94s" in ledger[canonical_index:]
     assert "v0.1 R1 checkpoint: complete" in progress
     assert "v0.1 R1 initial checkpoint historical evidence:" in progress
     assert "v0.1 R1 final checkpoint exact fresh evidence:" in progress
-    assert (
-        "v0.1 current implementation status: R0-R2 completed; "
-        "R3 pending and unstarted"
-    ) in progress
+    assert "v0.1 current implementation status: R0-R3 completed; R4 pending" in progress
     _assert_release_checkpoint_and_r3_resume(ledger)
     _assert_release_checkpoint_and_r3_resume(progress)


 def test_active_roadmap_links_the_v01_plan_index() -> None:
     root = Path(__file__).parents[2]
     expected = "2026-07-17-agent-sdk-v0.1-implementation-index.md"
     assert expected in (root / "docs/plans/00-roadmap.md").read_text(encoding="utf-8")
     assert expected in (root / "docs/plans/tasks/index.md").read_text(encoding="utf-8")
diff --git a/tests/e2e/test_v01_release.py b/tests/e2e/test_v01_release.py
index 506114e..c5be84c 100644
--- a/tests/e2e/test_v01_release.py
+++ b/tests/e2e/test_v01_release.py
@@ -1,24 +1,29 @@
 from __future__ import annotations

 import asyncio
+import json
 from collections.abc import AsyncIterator
+from pathlib import Path
 from typing import TYPE_CHECKING, Any

 import pytest

 from agent_sdk import (
     AgentSDK,
     AgentSDKError,
     AgentSpec,
+    ContextPlanner,
+    ContextRuntimeConfig,
     ErrorCode,
     PermissionDecision,
+    PromptManifest,
     RunStatus,
     ToolResultStatus,
     WorkflowRunStatus,
 )
 from agent_sdk.tools.models import thaw_json
 from agent_sdk.storage.base import CommitBatch, CommitResult, StateStore
 from agent_sdk.storage.memory import InMemoryStore

 if TYPE_CHECKING:
     from tests.fixtures.v01_runtime import V01Harness
@@ -236,10 +241,193 @@ steps:
     assert result.status is WorkflowRunStatus.COMPLETED
     assert calls == ["selected", "review", "review", "finish"]
     async def collect_events() -> list[str]:
         return [item.event.type async for item in recovered.events()]

     event_types = await asyncio.wait_for(collect_events(), timeout=5)
     assert "workflow.condition.selected" in event_types
     assert event_types.count("workflow.loop.iteration") == 2
     assert event_types[-1] == "workflow.completed"
     await asyncio.wait_for(reopened.close(), timeout=5)
+
+
+@pytest.mark.asyncio
+async def test_v01_runtime_automatically_compacts_l0_through_l4(
+    monkeypatch: pytest.MonkeyPatch,
+) -> None:
+    stage_tokens = {
+        "stage-l0": 10,
+        "stage-l1": 70,
+        "stage-l2": 80,
+        "stage-l3-invalid": 90,
+        "stage-l3-valid": 90,
+        "stage-l4": 96,
+    }
+
+    def controlled_estimate(
+        _planner: ContextPlanner,
+        messages: list[dict[str, Any]],
+    ) -> int:
+        serialized = json.dumps(messages, ensure_ascii=False, sort_keys=True)
+        latest = max(
+            (
+                (serialized.rfind(stage), tokens)
+                for stage, tokens in stage_tokens.items()
+            ),
+            key=lambda item: item[0],
+        )
+        return latest[1] if latest[0] >= 0 else 10
+
+    monkeypatch.setattr(
+        ContextPlanner,
+        "_estimate_messages",
+        controlled_estimate,
+    )
+
+    def text_stream() -> AsyncIterator[dict[str, object]]:
+        async def chunks() -> AsyncIterator[dict[str, object]]:
+            yield {
+                "choices": [
+                    {
+                        "delta": {"content": "completed"},
+                        "finish_reason": "stop",
+                    }
+                ]
+            }
+
+        return chunks()
+
+    compaction_operations: list[str] = []
+
+    async def provider(**params: object) -> object:
+        if params.get("stream") is not False:
+            return text_stream()
+        messages = params["messages"]
+        assert isinstance(messages, list)
+        document = json.loads(str(messages[-1]["content"]))
+        operation = str(document["operation"])
+        compaction_operations.append(operation)
+        if len(compaction_operations) == 1:
+            return {
+                "choices": [{"message": {"content": "{invalid-json"}}],
+                "usage": {
+                    "prompt_tokens": 2,
+                    "completion_tokens": 1,
+                    "total_tokens": 3,
+                },
+            }
+        source_refs = [
+            str(source["event_id"])
+            for source in document.get("sources", [])
+        ]
+        capsule_refs = [
+            str(capsule_id)
+            for capsule_id in document.get("capsule_ids", [])
+        ]
+        return {
+            "choices": [
+                {
+                    "message": {
+                        "parsed": {
+                            "objective": "preserve runtime context",
+                            "constraints": ["retain durable evidence"],
+                            "decisions": [],
+                            "facts": [],
+                            "next_actions": ["continue"],
+                            "artifact_refs": [],
+                            "source_event_ids": [*capsule_refs, *source_refs],
+                        }
+                    }
+                }
+            ],
+            "usage": {
+                "prompt_tokens": 2,
+                "completion_tokens": 1,
+                "total_tokens": 3,
+            },
+        }
+
+    store = InMemoryStore()
+    skill_root = Path(__file__).parents[1] / "fixtures" / "skills"
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=provider,
+        skill_roots=(skill_root,),
+        enable_builtin_tools=False,
+    )
+    try:
+        session = await sdk.sessions.create(workspaces=[])
+        agent = AgentSpec(
+            name="automatic-context",
+            model="test/context",
+            system_prompt="Application runtime policy.",
+            skills=("demo",),
+            context=ContextRuntimeConfig(
+                model_window=100,
+                output_reserve=0,
+                safety_reserve=0,
+                recent_messages=2,
+            ),
+        )
+        run_ids: list[str] = []
+        for stage in stage_tokens:
+            handle = await sdk.runs.start(session.session_id, agent, stage)
+            result = await handle.result()
+            assert result.output_text == "completed"
+            run_ids.append(handle.run_id)
+
+        events = await store.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        views = [
+            item.event
+            for item in events
+            if item.event.type == "context.view.created"
+        ]
+        assert [
+            event.payload["recommended_level"] for event in views
+        ] == ["L0", "L1", "L2", "L3", "L3", "L4"]
+        assert [
+            event.payload["applied_level"] for event in views
+        ] == ["L0", "L1", "L2", "L2", "L3", "L4"]
+        assert views[3].payload["fallback_from"] == "L3"
+        assert compaction_operations == ["summarize", "summarize", "rebase"]
+
+        original = next(
+            item.event
+            for item in events
+            if item.event.type == "run.created"
+            and item.event.run_id == run_ids[0]
+        )
+        assert any(item.event.event_id == original.event_id for item in events)
+        final_view_id = str(views[-1].payload["view_id"])
+        final_view = await store.get_snapshot("context_view", final_view_id)
+        assert final_view is not None
+        capsule_id = final_view["capsule_id"]
+        assert isinstance(capsule_id, str)
+        recovered_sources = await sdk.context.read_sources(
+            capsule_id,
+            session_id=session.session_id,
+        )
+        assert original.event_id in {
+            observed.event.event_id for observed in recovered_sources
+        }
+
+        last_started = next(
+            item.event
+            for item in reversed(events)
+            if item.event.type == "model.call.started"
+            and item.event.run_id == run_ids[-1]
+        )
+        manifest_id = str(last_started.payload["prompt_manifest_id"])
+        raw_manifest = await store.get_snapshot("prompt_manifest", manifest_id)
+        assert raw_manifest is not None
+        manifest = PromptManifest.model_validate(raw_manifest)
+        assert manifest.context_view_id == final_view_id
+        assert manifest.layer_names == (
+            "profile:general",
+            "application",
+            "skill:demo",
+        )
+    finally:
+        await sdk.close()
diff --git a/tests/integration/context/test_compaction_slice.py b/tests/integration/context/test_compaction_slice.py
index ac4b1e4..08c2f99 100644
--- a/tests/integration/context/test_compaction_slice.py
+++ b/tests/integration/context/test_compaction_slice.py
@@ -167,22 +167,20 @@ async def test_forced_compaction_preserves_ledger_and_sources() -> None:
                         "parsed": {
                             "objective": "ship the slice",
                             "constraints": ["keep originals"],
                             "decisions": [],
                             "facts": ["tool evidence exists"],
                             "next_actions": ["verify"],
                             "artifact_refs": [],
                             "source_event_ids": [
                                 "evt_user",
                                 "evt_assistant",
-                                "evt_tool",
-                                "evt_latest_user",
                             ],
                         }
                     }
                 }
             ],
             "usage": {
                 "prompt_tokens": 12,
                 "completion_tokens": 6,
                 "total_tokens": 18,
             },
@@ -206,22 +204,21 @@ async def test_forced_compaction_preserves_ledger_and_sources() -> None:
     )

     assert view.capsule_id is not None
     assert view.message_refs == ("evt_tool", "evt_latest_user")
     retrieval = ContextRetrieval(store)
     capsule = await retrieval.get_capsule(
         view.capsule_id,
         session_id="ses_context",
     )
     assert isinstance(capsule, ContextCapsule)
-    assert set(capsule.source_event_ids) <= {event.event_id for event in sources}
-    assert {"evt_tool", "evt_latest_user"} <= set(capsule.source_event_ids)
+    assert capsule.source_event_ids == ("evt_user", "evt_assistant")
     after = await store.read_events(
         after_cursor=0,
         session_id="ses_context",
     )
     assert after[: len(before)] == before
     assert [stored.event.type for stored in after[len(before) :]] == [
         "context.compaction.completed",
         "context.view.created",
     ]
     assert [stored.event.run_id for stored in after[len(before) :]] == [
@@ -233,29 +230,28 @@ async def test_forced_compaction_preserves_ledger_and_sources() -> None:
     assert view.budget.watermark_ratio == 0.5
     assert view.estimated_tokens == 9
     assert view.recommended_level is CompactionLevel.L0
     assert view.applied_level is CompactionLevel.L3
     assert calls[0]["stream"] is False
     assert calls[0]["response_format"] is ContextCapsule
     assert "purpose" not in calls[0]
     raw_messages = calls[0]["messages"]
     assert isinstance(raw_messages, list)
     source_document = json.loads(raw_messages[-1]["content"])
-    assert source_document["protected_event_ids"] == [
+    assert source_document["operation"] == "summarize"
+    assert source_document["retained_event_ids"] == [
         "evt_tool",
         "evt_latest_user",
     ]
     assert [item["event_id"] for item in source_document["sources"]] == [
         "evt_user",
         "evt_assistant",
-        "evt_tool",
-        "evt_latest_user",
     ]


 def _capsule_data(source_event_ids: list[str]) -> dict[str, object]:
     return {
         "objective": "objective",
         "constraints": ["constraint"],
         "decisions": ["decision"],
         "facts": ["fact"],
         "next_actions": ["next"],
@@ -674,47 +670,58 @@ def _planner(
         model="fake/compact",
         model_window=model_window,
         output_reserve=output_reserve,
         tool_schema_tokens=tool_schema_tokens,
         safety_reserve=safety_reserve,
         _token_counter=lambda **_: token_count,
     )


 @pytest.mark.asyncio
-async def test_automatic_recommendation_does_not_claim_l1_or_l2_is_applied() -> None:
+async def test_automatic_l2_recommendation_applies_deterministic_l2() -> None:
     store = _RecordingStore(InMemoryStore())
     await _seed_projection(store)
     store.batches.clear()
     model_calls = 0

     async def acompletion(**_: object) -> object:
         nonlocal model_calls
         model_calls += 1
-        raise AssertionError("automatic recommendation must not compact in M01")
+        raise AssertionError("deterministic L2 must not call the model")

     view = await _planner(store, acompletion, token_count=80).build("ses_projection")

     assert view.recommended_level is CompactionLevel.L2
-    assert view.applied_level is CompactionLevel.L0
+    assert view.applied_level is CompactionLevel.L2
+    assert view.fallback_from is None
     assert view.capsule_id is None
     assert view.message_refs == (
         "evt_projection_user",
         "evt_projection_assistant",
         "evt_projection_tool",
         "evt_projection_latest",
     )
+    assert view.source_refs == view.message_refs
+    assert view.transformations == (
+        "outcome:evt_projection_user",
+        "outcome:evt_projection_assistant",
+    )
     assert model_calls == 0
     assert len(store.batches) == 1
     assert [event.type for event in store.batches[0].events] == [
         "context.view.created"
     ]
+    payload = store.batches[0].events[0].payload
+    assert payload["recommended_level"] == "L2"
+    assert payload["applied_level"] == "L2"
+    assert payload["source_refs"] == list(view.source_refs)
+    assert payload["transformations"] == list(view.transformations)


 @pytest.mark.asyncio
 async def test_non_positive_capacity_and_unknown_protected_id_fail_before_model_call() -> None:
     store = _RecordingStore(InMemoryStore())
     await _seed_projection(store)
     store.batches.clear()
     model_calls = 0

     async def acompletion(**_: object) -> object:
@@ -920,36 +927,39 @@ async def test_invalid_capsule_or_malformed_model_response_falls_back_safely(

     async def acompletion(**_: object) -> dict[str, object]:
         return response_factory()

     view = await _planner(store, acompletion).build(
         "ses_projection",
         force_level="L4",
         protected_event_ids={"evt_projection_tool"},
     )

-    assert view.applied_level is CompactionLevel.L0
+    assert view.applied_level is CompactionLevel.L2
+    assert view.fallback_from is CompactionLevel.L4
     assert view.capsule_id is None
     assert view.message_refs == (
         "evt_projection_user",
         "evt_projection_assistant",
         "evt_projection_tool",
         "evt_projection_latest",
     )
     assert len(store.batches) == 1
     batch = store.batches[0]
     assert [event.type for event in batch.events] == [
         "context.compaction.failed",
         "context.view.created",
     ]
     assert [snapshot.kind for snapshot in batch.snapshots] == ["context_view"]
     assert batch.events[0].payload["code"] == "context_compaction_failed"
+    assert batch.events[0].payload["applied_level"] == "L2"
+    assert batch.events[1].payload["fallback_from"] == "L4"
     serialized = json.dumps(
         [event.payload for event in batch.events],
         sort_keys=True,
     )
     assert "evt_unknown" not in serialized
     assert "choices" not in serialized


 @pytest.mark.asyncio
 async def test_model_failure_fallback_is_sanitized_and_has_no_capsule_snapshot() -> None:
@@ -993,40 +1003,45 @@ async def test_cancellation_propagates_without_persistence_or_orphan_tasks() ->


 @pytest.mark.asyncio
 async def test_success_is_one_atomic_commit_with_capsule_view_and_events() -> None:
     store = _RecordingStore(InMemoryStore())
     await _seed_projection(store)
     store.batches.clear()

     async def acompletion(**_: object) -> dict[str, object]:
         return _structured_response(
-            ["evt_projection_tool", "evt_projection_latest"]
+            ["evt_projection_user", "evt_projection_assistant"]
         )

     view = await _planner(store, acompletion).build(
         "ses_projection",
         force_level="L3",
         protected_event_ids={"evt_projection_tool"},
     )

     assert len(store.batches) == 1
     batch = store.batches[0]
     assert [snapshot.kind for snapshot in batch.snapshots] == [
         "context_capsule",
         "context_view",
     ]
     assert [event.type for event in batch.events] == [
         "context.compaction.completed",
         "context.view.created",
     ]
     assert all(event.run_id == view.view_id for event in batch.events)
+    capsule_snapshot = batch.snapshots[0].data["capsule"]
+    assert capsule_snapshot["source_event_ids"] == [
+        "evt_projection_user",
+        "evt_projection_assistant",
+    ]


 async def _assert_delete_wins_blocked_compaction(
     store: _AttemptRecordingStore,
     delete_session: Callable[[], Awaitable[None]],
 ) -> None:
     await _seed_projection(store, session_id="ses_projection")
     store.attempts.clear()
     entered = asyncio.Event()
     release = asyncio.Event()
diff --git a/tests/integration/context/test_context_compaction.py b/tests/integration/context/test_context_compaction.py
new file mode 100644
index 0000000..8ee62df
--- /dev/null
+++ b/tests/integration/context/test_context_compaction.py
@@ -0,0 +1,449 @@
+from __future__ import annotations
+
+import json
+from datetime import UTC, datetime
+from typing import Any
+
+import pytest
+
+from agent_sdk.context import (
+    CompactionLevel,
+    ContextPlanner,
+    ContextRetrieval,
+)
+from agent_sdk.events.models import EventEnvelope
+from agent_sdk.models.litellm_gateway import LiteLLMGateway
+from agent_sdk.storage.base import CommitBatch, SnapshotWrite
+from agent_sdk.storage.memory import InMemoryStore
+
+
+def _event(
+    event_id: str,
+    *,
+    sequence: int,
+    role: str,
+    content: str,
+    session_id: str = "ses_task2",
+) -> EventEnvelope:
+    return EventEnvelope(
+        event_id=event_id,
+        type="context.message.appended",
+        session_id=session_id,
+        run_id=None,
+        sequence=sequence,
+        payload={"role": role, "content": content},
+        occurred_at=datetime(2026, 7, 20, tzinfo=UTC),
+    )
+
+
+async def _seed(
+    store: InMemoryStore,
+    *,
+    session_id: str = "ses_task2",
+) -> tuple[EventEnvelope, ...]:
+    events = (
+        _event("evt_old_user", sequence=1, role="user", content="old question"),
+        _event(
+            "evt_old_answer",
+            sequence=2,
+            role="assistant",
+            content="old answer",
+        ),
+        _event(
+            "evt_old_tool",
+            sequence=3,
+            role="tool",
+            content="old tool detail " * 40,
+        ),
+        _event(
+            "evt_recent_answer",
+            sequence=4,
+            role="assistant",
+            content="recent answer",
+        ),
+        _event(
+            "evt_latest_user",
+            sequence=5,
+            role="user",
+            content="latest question",
+        ),
+    )
+    await store.commit(
+        CommitBatch(
+            events=events,
+            snapshots=(
+                SnapshotWrite(
+                    "session",
+                    session_id,
+                    session_id,
+                    1,
+                    {"session_id": session_id},
+                ),
+            ),
+        )
+    )
+    return events
+
+
+def _response(*refs: str, objective: str = "ship") -> dict[str, object]:
+    return {
+        "choices": [
+            {
+                "message": {
+                    "parsed": {
+                        "objective": objective,
+                        "constraints": ["preserve evidence"],
+                        "decisions": [],
+                        "facts": [],
+                        "next_actions": ["verify"],
+                        "artifact_refs": [],
+                        "source_event_ids": list(refs),
+                    }
+                }
+            }
+        ],
+        "usage": {
+            "prompt_tokens": 12,
+            "completion_tokens": 5,
+            "total_tokens": 17,
+        },
+    }
+
+
+def _planner(
+    store: InMemoryStore,
+    acompletion: Any,
+    *,
+    token_count: int,
+) -> ContextPlanner:
+    return ContextPlanner(
+        store,
+        LiteLLMGateway._for_test(acompletion),
+        model="fake/compact",
+        model_window=100,
+        recent_messages=2,
+        tool_preview_bytes=256,
+        _token_counter=lambda **_: token_count,
+    )
+
+
+def _planner_with_counts(
+    store: InMemoryStore,
+    acompletion: Any,
+    *counts: int,
+    recent_messages: int = 2,
+) -> ContextPlanner:
+    token_counts = iter(counts)
+    return ContextPlanner(
+        store,
+        LiteLLMGateway._for_test(acompletion),
+        model="fake/compact",
+        model_window=100,
+        recent_messages=recent_messages,
+        tool_preview_bytes=256,
+        _token_counter=lambda **_: next(token_counts),
+    )
+
+
+@pytest.mark.asyncio
+@pytest.mark.parametrize(
+    ("token_count", "expected"),
+    [
+        (70, CompactionLevel.L1),
+        (80, CompactionLevel.L2),
+    ],
+)
+async def test_automatic_policy_applies_deterministic_levels(
+    token_count: int,
+    expected: CompactionLevel,
+) -> None:
+    store = InMemoryStore()
+    await _seed(store)
+    model_calls = 0
+
+    async def acompletion(**_: object) -> object:
+        nonlocal model_calls
+        model_calls += 1
+        raise AssertionError("L1/L2 must not call the model")
+
+    view = await _planner(
+        store,
+        acompletion,
+        token_count=token_count,
+    ).build("ses_task2")
+
+    assert view.recommended_level is expected
+    assert view.applied_level is expected
+    assert view.fallback_from is None
+    assert view.source_refs == (
+        "evt_old_user",
+        "evt_old_answer",
+        "evt_old_tool",
+        "evt_recent_answer",
+        "evt_latest_user",
+    )
+    assert model_calls == 0
+
+
+@pytest.mark.asyncio
+async def test_allow_lossy_false_caps_automatic_l4_at_l2() -> None:
+    store = InMemoryStore()
+    await _seed(store)
+
+    async def acompletion(**_: object) -> object:
+        raise AssertionError("lossless cap must not call the model")
+
+    view = await _planner(
+        store,
+        acompletion,
+        token_count=96,
+    ).build("ses_task2", allow_lossy=False)
+
+    assert view.recommended_level is CompactionLevel.L4
+    assert view.applied_level is CompactionLevel.L2
+    assert view.fallback_from is None
+    assert any(value.startswith("outcome:") for value in view.transformations)
+
+
+@pytest.mark.asyncio
+async def test_l3_retains_recent_and_protected_messages() -> None:
+    store = InMemoryStore()
+    await _seed(store)
+    requests: list[dict[str, object]] = []
+
+    async def acompletion(**kwargs: object) -> dict[str, object]:
+        requests.append(kwargs)
+        return _response("evt_old_user", "evt_old_answer", objective="summary")
+
+    view = await _planner(store, acompletion, token_count=90).build(
+        "ses_task2",
+        protected_event_ids={"evt_old_tool"},
+    )
+
+    assert view.applied_level is CompactionLevel.L3
+    assert view.message_refs == (
+        "evt_old_tool",
+        "evt_recent_answer",
+        "evt_latest_user",
+    )
+    assert view.capsule_id is not None
+    assert view.source_refs == (
+        "evt_old_user",
+        "evt_old_answer",
+        "evt_old_tool",
+        "evt_recent_answer",
+        "evt_latest_user",
+    )
+    document = json.loads(requests[0]["messages"][-1]["content"])
+    assert [item["event_id"] for item in document["sources"]] == [
+        "evt_old_user",
+        "evt_old_answer",
+    ]
+
+
+@pytest.mark.asyncio
+async def test_l3_over_budget_output_falls_back_to_l2_with_usage() -> None:
+    store = InMemoryStore()
+    await _seed(store)
+
+    async def acompletion(**_: object) -> dict[str, object]:
+        return _response(
+            "evt_old_user",
+            "evt_old_answer",
+            "evt_old_tool",
+            objective="oversized summary",
+        )
+
+    view = await _planner_with_counts(
+        store,
+        acompletion,
+        90,
+        101,
+        60,
+    ).build("ses_task2", force_level="L3")
+
+    assert view.applied_level is CompactionLevel.L2
+    assert view.fallback_from is CompactionLevel.L3
+    assert view.capsule_id is None
+    assert view.estimated_tokens == 60
+    events = await store.read_events(after_cursor=0, session_id="ses_task2")
+    created = [item.event for item in events if item.event.type == "context.view.created"]
+    failed = [
+        item.event for item in events if item.event.type == "context.compaction.failed"
+    ]
+    completed = [
+        item.event for item in events if item.event.type == "context.compaction.completed"
+    ]
+    assert completed == []
+    assert created[-1].payload["compaction_usage"] == {
+        "prompt_tokens": 12,
+        "completion_tokens": 5,
+        "total_tokens": 17,
+    }
+    assert failed[-1].payload["usage"] == created[-1].payload["compaction_usage"]
+
+
+@pytest.mark.asyncio
+async def test_forced_l3_with_empty_closed_slice_skips_model_and_falls_back() -> None:
+    store = InMemoryStore()
+    await _seed(store)
+    model_calls = 0
+
+    async def acompletion(**_: object) -> object:
+        nonlocal model_calls
+        model_calls += 1
+        return _response("evt_latest_user")
+
+    view = await _planner_with_counts(
+        store,
+        acompletion,
+        90,
+        60,
+        recent_messages=5,
+    ).build("ses_task2", force_level="L3")
+
+    assert model_calls == 0
+    assert view.applied_level is CompactionLevel.L2
+    assert view.fallback_from is CompactionLevel.L3
+    assert view.capsule_id is None
+
+
+@pytest.mark.asyncio
+async def test_l4_rebases_prior_capsule_evidence() -> None:
+    store = InMemoryStore()
+    await _seed(store)
+    call_count = 0
+
+    async def acompletion(**kwargs: object) -> dict[str, object]:
+        nonlocal call_count
+        call_count += 1
+        if call_count == 1:
+            return _response(
+                "evt_old_user",
+                "evt_old_answer",
+                "evt_old_tool",
+                objective="summary",
+            )
+        document = json.loads(kwargs["messages"][-1]["content"])
+        return _response(
+            document["capsule_ids"][0],
+            "evt_recent_answer",
+            "evt_latest_user",
+            objective="rebased",
+        )
+
+    first = await _planner(store, acompletion, token_count=90).build(
+        "ses_task2",
+        force_level="L3",
+    )
+    assert first.capsule_id is not None
+    second = await _planner(store, acompletion, token_count=96).build(
+        "ses_task2",
+    )
+
+    assert second.applied_level is CompactionLevel.L4
+    assert second.capsule_id is not None
+    capsule = await ContextRetrieval(store).get_capsule(
+        second.capsule_id,
+        session_id="ses_task2",
+    )
+    assert first.capsule_id in capsule.source_event_ids
+    recovered = await ContextRetrieval(store).read_sources(
+        second.capsule_id,
+        session_id="ses_task2",
+    )
+    assert {"evt_old_user", "evt_old_answer"} <= {
+        item.event.event_id for item in recovered
+    }
+
+
+@pytest.mark.asyncio
+async def test_l4_over_budget_output_falls_back_to_l2_with_usage() -> None:
+    store = InMemoryStore()
+    await _seed(store)
+    call_count = 0
+
+    async def acompletion(**kwargs: object) -> dict[str, object]:
+        nonlocal call_count
+        call_count += 1
+        if call_count == 1:
+            return _response(
+                "evt_old_user",
+                "evt_old_answer",
+                "evt_old_tool",
+                objective="summary",
+            )
+        document = json.loads(kwargs["messages"][-1]["content"])
+        return _response(
+            document["capsule_ids"][0],
+            "evt_recent_answer",
+            "evt_latest_user",
+            objective="oversized rebase",
+        )
+
+    first = await _planner(store, acompletion, token_count=90).build(
+        "ses_task2",
+        force_level="L3",
+    )
+    assert first.capsule_id is not None
+    view = await _planner_with_counts(
+        store,
+        acompletion,
+        96,
+        101,
+        60,
+    ).build("ses_task2", force_level="L4")
+
+    assert view.applied_level is CompactionLevel.L2
+    assert view.fallback_from is CompactionLevel.L4
+    assert view.capsule_id is None
+    assert view.estimated_tokens == 60
+    events = await store.read_events(after_cursor=0, session_id="ses_task2")
+    completed = [
+        item.event for item in events if item.event.type == "context.compaction.completed"
+    ]
+    failed = [
+        item.event for item in events if item.event.type == "context.compaction.failed"
+    ]
+    assert len(completed) == 1
+    assert failed[-1].payload["requested_level"] == "L4"
+    assert failed[-1].payload["usage"] == {
+        "prompt_tokens": 12,
+        "completion_tokens": 5,
+        "total_tokens": 17,
+    }
+
+
+@pytest.mark.asyncio
+async def test_invalid_l4_persists_same_l2_fallback_and_original_events() -> None:
+    store = InMemoryStore()
+    originals = await _seed(store)
+
+    async def invalid(**_: object) -> dict[str, object]:
+        return _response("evt_unknown", objective="invalid")
+
+    planner = _planner(store, invalid, token_count=96)
+    fallback = await planner.build("ses_task2")
+
+    async def forbidden(**_: object) -> object:
+        raise AssertionError("forced L2 must not call the model")
+
+    expected = await _planner(store, forbidden, token_count=96).build(
+        "ses_task2",
+        force_level="L2",
+    )
+
+    assert fallback.applied_level is CompactionLevel.L2
+    assert fallback.fallback_from is CompactionLevel.L4
+    assert fallback.capsule_id is None
+    assert fallback.message_refs == expected.message_refs
+    assert fallback.source_refs == expected.source_refs
+    assert fallback.transformations == expected.transformations
+    events = await store.read_events(after_cursor=0, session_id="ses_task2")
+    assert tuple(item.event for item in events[: len(originals)]) == originals
+    created = [item.event for item in events if item.event.type == "context.view.created"]
+    failed = [
+        item.event for item in events if item.event.type == "context.compaction.failed"
+    ]
+    assert created[-2].payload["fallback_from"] == "L4"
+    assert failed[-1].payload["requested_level"] == "L4"
diff --git a/tests/integration/context/test_context_recovery.py b/tests/integration/context/test_context_recovery.py
new file mode 100644
index 0000000..9007e6e
--- /dev/null
+++ b/tests/integration/context/test_context_recovery.py
@@ -0,0 +1,693 @@
+from __future__ import annotations
+
+import asyncio
+import json
+from collections.abc import AsyncIterator
+from pathlib import Path
+from typing import Any
+
+import pytest
+
+from agent_sdk import (
+    AgentSDK,
+    AgentSDKError,
+    AgentSpec,
+    ProviderRecoveryAdapter,
+    ProviderRecoveryDisposition,
+    ProviderRecoveryRequest,
+    ProviderRecoveryResult,
+    ToolRetryPolicy,
+    ToolSpec,
+    TokenUsage,
+)
+from agent_sdk.runtime.engine import _model_request_fingerprint
+from agent_sdk.runtime import reconciliation
+from agent_sdk.runtime.reconciliation import ModelCallOperation
+from agent_sdk.storage.base import SnapshotWrite, StateStore
+from agent_sdk.storage.memory import InMemoryStore
+from agent_sdk.storage.sqlite import SQLiteStore
+from agent_sdk.tools.models import ToolContext, thaw_json
+
+
+@pytest.mark.asyncio
+async def test_in_flight_model_operation_stores_exact_prepared_request() -> None:
+    store = InMemoryStore()
+    accepted = asyncio.Event()
+    release = asyncio.Event()
+    observed: list[dict[str, Any]] = []
+
+    async def provider(**kwargs: Any) -> AsyncIterator[dict[str, object]]:
+        observed.append(kwargs)
+
+        async def chunks() -> AsyncIterator[dict[str, object]]:
+            accepted.set()
+            await release.wait()
+            yield {
+                "choices": [
+                    {"delta": {"content": "done"}, "finish_reason": "stop"}
+                ]
+            }
+
+        return chunks()
+
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=provider,
+        enable_builtin_tools=False,
+    )
+    handle = None
+    try:
+        session = await sdk.sessions.create(workspaces=[])
+        handle = await sdk.runs.start(
+            session.session_id,
+            AgentSpec(name="recoverable", model="test/model"),
+            "Persist this exact request.",
+        )
+        await asyncio.wait_for(accepted.wait(), timeout=2)
+
+        operations = await store.list_unresolved_external_operations(handle.run_id)
+        assert len(operations) == 1
+        operation = operations[0]
+        assert isinstance(operation, ModelCallOperation)
+        assert operation.context_view_id is not None
+        assert operation.prompt_manifest_id is not None
+        assert operation.prepared_request is not None
+        prepared = thaw_json(operation.prepared_request)
+        assert isinstance(prepared, dict)
+        request = reconciliation.deserialize_model_request(prepared)
+        assert request.messages == tuple(observed[0]["messages"])
+        assert request.tools == tuple(observed[0]["tools"])
+        assert _model_request_fingerprint(request) == operation.request_fingerprint
+
+        events = await store.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        started = next(
+            item.event
+            for item in events
+            if item.event.type == "model.call.started"
+        )
+        public_payload = started.model_dump_json()
+        assert started.payload == {
+            "model": "test/model",
+            "context_view_id": operation.context_view_id,
+            "prompt_manifest_id": operation.prompt_manifest_id,
+            "request_fingerprint": operation.request_fingerprint,
+        }
+        assert "Persist this exact request." not in public_payload
+        assert "You are" not in public_payload
+    finally:
+        release.set()
+        if handle is not None:
+            await handle.result()
+        await sdk.close()
+
+
+@pytest.mark.asyncio
+async def test_reopen_reuses_in_flight_prepared_request_without_new_context() -> None:
+    store = InMemoryStore()
+    accepted = asyncio.Event()
+
+    async def hanging_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
+        async def chunks() -> AsyncIterator[dict[str, object]]:
+            accepted.set()
+            await asyncio.Event().wait()
+            yield {"choices": []}
+
+        return chunks()
+
+    spec = AgentSpec(name="recoverable", model="test/model")
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=hanging_provider,
+        enable_builtin_tools=False,
+    )
+    session = await sdk.sessions.create(workspaces=[])
+    handle = await sdk.runs.start(
+        session.session_id,
+        spec,
+        "Crash after model acceptance.",
+    )
+    await asyncio.wait_for(accepted.wait(), timeout=2)
+    original = (await store.list_unresolved_external_operations(handle.run_id))[0]
+    assert isinstance(original, ModelCallOperation)
+    events_before = await store.read_events(
+        after_cursor=0,
+        session_id=session.session_id,
+    )
+    counts_before = {
+        event_type: sum(
+            item.event.type == event_type for item in events_before
+        )
+        for event_type in (
+            "context.view.created",
+            "context.compaction.completed",
+            "prompt.manifest.created",
+            "model.call.started",
+        )
+    }
+    assert handle._task is not None
+    handle._task.cancel()
+    with pytest.raises(asyncio.CancelledError):
+        await handle._task
+    await sdk.close()
+
+    provider_calls = 0
+
+    async def must_not_call(**_: Any) -> object:
+        nonlocal provider_calls
+        provider_calls += 1
+        raise AssertionError("unknown model outcome must not be replayed")
+
+    reopened = AgentSDK.for_test(
+        store=store,
+        acompletion=must_not_call,
+        enable_builtin_tools=False,
+    )
+    reopened.agents.define(spec)
+    try:
+        with pytest.raises(AgentSDKError, match="recovery required"):
+            await (await reopened.recovery.recover_run(handle.run_id)).result()
+
+        pending = await reopened.recovery.pending_requests(handle.run_id)
+        assert len(pending) == 1
+        assert pending[0].reason == "model_call_unknown_outcome"
+        recovered = await store.get_external_operation(original.operation_id)
+        assert recovered == original
+        assert isinstance(recovered, ModelCallOperation)
+        assert recovered.context_view_id == original.context_view_id
+        assert recovered.prompt_manifest_id == original.prompt_manifest_id
+        assert recovered.prepared_request == original.prepared_request
+        assert recovered.request_fingerprint == original.request_fingerprint
+
+        events_after = await store.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        counts_after = {
+            event_type: sum(
+                item.event.type == event_type for item in events_after
+            )
+            for event_type in counts_before
+        }
+        assert counts_after == counts_before
+        assert provider_calls == 0
+    finally:
+        await reopened.close()
+
+
+@pytest.mark.asyncio
+async def test_authoritative_recovery_receives_exact_stored_prepared_request() -> None:
+    store = InMemoryStore()
+    accepted = asyncio.Event()
+    observed: list[ProviderRecoveryRequest] = []
+
+    async def hanging_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
+        async def chunks() -> AsyncIterator[dict[str, object]]:
+            accepted.set()
+            await asyncio.Event().wait()
+            yield {"choices": []}
+
+        return chunks()
+
+    async def query(
+        request: ProviderRecoveryRequest,
+    ) -> ProviderRecoveryResult:
+        observed.append(request)
+        return ProviderRecoveryResult(
+            disposition=ProviderRecoveryDisposition.COMPLETED,
+            finish_reason="stop",
+            text="recovered",
+            usage=TokenUsage(
+                prompt_tokens=3,
+                completion_tokens=1,
+                total_tokens=4,
+            ),
+        )
+
+    adapter = ProviderRecoveryAdapter(
+        provider_identity="test/model",
+        adapter_id="test.authoritative",
+        version="1",
+        authoritative_status=True,
+        same_operation_id_resend=False,
+        query_status=query,
+    )
+    spec = AgentSpec(name="recoverable", model="test/model")
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=hanging_provider,
+        enable_builtin_tools=False,
+    )
+    sdk.recovery.register_adapter(adapter)
+    session = await sdk.sessions.create(workspaces=[])
+    handle = await sdk.runs.start(
+        session.session_id,
+        spec,
+        "Recover the exact prepared request.",
+    )
+    await asyncio.wait_for(accepted.wait(), timeout=2)
+    original = (await store.list_unresolved_external_operations(handle.run_id))[0]
+    assert isinstance(original, ModelCallOperation)
+    assert original.prepared_request is not None
+    exact_request = reconciliation.deserialize_model_request(
+        original.prepared_request
+    )
+    assert handle._task is not None
+    handle._task.cancel()
+    with pytest.raises(asyncio.CancelledError):
+        await handle._task
+    await sdk.close()
+
+    async def must_not_call(**_: Any) -> object:
+        raise AssertionError("certified recovery must not call LiteLLM")
+
+    reopened = AgentSDK.for_test(
+        store=store,
+        acompletion=must_not_call,
+        enable_builtin_tools=False,
+    )
+    reopened.agents.define(spec)
+    reopened.recovery.register_adapter(adapter)
+    try:
+        await reopened.recovery.scan()
+        result = await (
+            await reopened.recovery.recover_run(handle.run_id)
+        ).result()
+
+        assert result.output_text == "recovered"
+        assert len(observed) == 1
+        assert observed[0].model_request == exact_request
+        assert observed[0].request_fingerprint == original.request_fingerprint
+        events = await store.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        assert sum(
+            item.event.type == "context.view.created" for item in events
+        ) == 1
+        assert sum(
+            item.event.type == "prompt.manifest.created" for item in events
+        ) == 1
+        assert sum(
+            item.event.type == "model.call.started" for item in events
+        ) == 1
+    finally:
+        await reopened.close()
+
+
+def _recovery_tool() -> ToolSpec:
+    return ToolSpec(
+        name="recovery_probe",
+        description="Must remain side-effect free during reference rejection.",
+        input_schema={
+            "type": "object",
+            "properties": {},
+            "additionalProperties": False,
+        },
+    )
+
+
+async def _tamper_prepared_reference(
+    store: InMemoryStore | SQLiteStore,
+    operation: ModelCallOperation,
+    corruption: str,
+) -> None:
+    assert operation.context_view_id is not None
+    assert operation.prompt_manifest_id is not None
+    target = (
+        ("context_view", operation.context_view_id)
+        if corruption.startswith("view_")
+        else ("prompt_manifest", operation.prompt_manifest_id)
+    )
+    if isinstance(store, InMemoryStore):
+        snapshot = store._snapshots[target]
+        if corruption.endswith("_missing"):
+            del store._snapshots[target]
+            return
+        data = dict(snapshot.data)
+        session_id = snapshot.session_id
+        if corruption.endswith("_owner"):
+            session_id = "ses_other"
+        elif corruption == "view_identity":
+            data["view_id"] = "view_other"
+        elif corruption == "manifest_identity":
+            data["manifest_id"] = "pmf_other"
+        elif corruption == "manifest_link":
+            data["context_view_id"] = "view_other"
+        else:
+            raise AssertionError(f"unknown corruption: {corruption}")
+        store._snapshots[target] = SnapshotWrite(
+            snapshot.kind,
+            snapshot.entity_id,
+            session_id,
+            snapshot.version,
+            data,
+        )
+        return
+
+    snapshot_data = (
+        await store.get_snapshot(*target)
+        if not (
+            corruption.endswith("_missing")
+            or corruption.endswith("_owner")
+        )
+        else None
+    )
+    async with store._lock:
+        if corruption.endswith("_missing"):
+            await store._connection.execute(
+                "DELETE FROM snapshots WHERE kind = ? AND entity_id = ?",
+                target,
+            )
+        elif corruption.endswith("_owner"):
+            await store._connection.execute(
+                """
+                UPDATE snapshots SET session_id = ?
+                WHERE kind = ? AND entity_id = ?
+                """,
+                ("ses_other", *target),
+            )
+        else:
+            assert snapshot_data is not None
+            if corruption == "view_identity":
+                snapshot_data["view_id"] = "view_other"
+            elif corruption == "manifest_identity":
+                snapshot_data["manifest_id"] = "pmf_other"
+            elif corruption == "manifest_link":
+                snapshot_data["context_view_id"] = "view_other"
+            else:
+                raise AssertionError(f"unknown corruption: {corruption}")
+            await store._connection.execute(
+                """
+                UPDATE snapshots SET data_json = ?
+                WHERE kind = ? AND entity_id = ?
+                """,
+                (
+                    json.dumps(
+                        snapshot_data,
+                        ensure_ascii=False,
+                        allow_nan=False,
+                        sort_keys=True,
+                        separators=(",", ":"),
+                    ),
+                    *target,
+                ),
+            )
+        await store._connection.commit()
+
+
+@pytest.mark.asyncio
+@pytest.mark.parametrize("backend", ["memory", "sqlite"])
+@pytest.mark.parametrize(
+    "corruption",
+    [
+        "view_missing",
+        "manifest_missing",
+        "view_owner",
+        "manifest_owner",
+        "view_identity",
+        "manifest_identity",
+        "manifest_link",
+    ],
+)
+async def test_recovery_rejects_unauthenticated_prepared_references(
+    backend: str,
+    corruption: str,
+    tmp_path: Path,
+) -> None:
+    store: InMemoryStore | SQLiteStore = (
+        InMemoryStore()
+        if backend == "memory"
+        else await SQLiteStore.open(
+            tmp_path / f"prepared-ref-{corruption}.sqlite3"
+        )
+    )
+    accepted = asyncio.Event()
+    tool_calls = 0
+
+    async def hanging_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
+        async def chunks() -> AsyncIterator[dict[str, object]]:
+            accepted.set()
+            await asyncio.Event().wait()
+            yield {"choices": []}
+
+        return chunks()
+
+    async def tool_handler(_: ToolContext) -> None:
+        nonlocal tool_calls
+        tool_calls += 1
+
+    spec = AgentSpec(name="reference-auth", model="test/model")
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=hanging_provider,
+        enable_builtin_tools=False,
+    )
+    sdk.tools.register(_recovery_tool(), tool_handler)
+    session = await sdk.sessions.create(workspaces=[])
+    handle = await sdk.runs.start(
+        session.session_id,
+        spec,
+        "Authenticate prepared references.",
+    )
+    await asyncio.wait_for(accepted.wait(), timeout=2)
+    operation = (await store.list_unresolved_external_operations(handle.run_id))[0]
+    assert isinstance(operation, ModelCallOperation)
+    assert handle._task is not None
+    handle._task.cancel()
+    with pytest.raises(asyncio.CancelledError):
+        await handle._task
+    await sdk.close()
+
+    await _tamper_prepared_reference(store, operation, corruption)
+    provider_calls = 0
+
+    async def must_not_call_provider(**_: Any) -> object:
+        nonlocal provider_calls
+        provider_calls += 1
+        raise AssertionError("invalid references must fail before provider recovery")
+
+    reopened = AgentSDK.for_test(
+        store=store,
+        acompletion=must_not_call_provider,
+        enable_builtin_tools=False,
+    )
+    reopened.agents.define(spec)
+    reopened.tools.register(_recovery_tool(), tool_handler)
+    try:
+        with pytest.raises(AgentSDKError, match="recovery state conflict"):
+            await reopened.recovery.recover_run(handle.run_id)
+        assert provider_calls == 0
+        assert tool_calls == 0
+    finally:
+        await reopened.close()
+        if isinstance(store, SQLiteStore):
+            await store.close()
+
+
+class _SnapshotReadTrackingStore:
+    def __init__(self, delegate: StateStore) -> None:
+        self.delegate = delegate
+        self.reads: list[tuple[str, str]] = []
+
+    def __getattr__(self, name: str) -> Any:
+        return getattr(self.delegate, name)
+
+    async def get_snapshot(
+        self,
+        kind: str,
+        entity_id: str,
+    ) -> dict[str, Any] | None:
+        self.reads.append((kind, entity_id))
+        return await self.delegate.get_snapshot(kind, entity_id)
+
+
+@pytest.mark.asyncio
+@pytest.mark.parametrize("tamper_old_view", [False, True])
+async def test_completed_model_recovery_authenticates_old_refs_and_adds_one_new_pair(
+    tamper_old_view: bool,
+) -> None:
+    durable = InMemoryStore()
+    store = _SnapshotReadTrackingStore(durable)
+    handler_started = asyncio.Event()
+    handler_cancelled = asyncio.Event()
+
+    async def first_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
+        async def chunks() -> AsyncIterator[dict[str, object]]:
+            yield {
+                "choices": [
+                    {
+                        "delta": {
+                            "tool_calls": [
+                                {
+                                    "index": 0,
+                                    "id": "call_recovery_probe",
+                                    "function": {
+                                        "name": "recovery_probe",
+                                        "arguments": "{}",
+                                    },
+                                }
+                            ]
+                        },
+                        "finish_reason": "tool_calls",
+                    }
+                ]
+            }
+
+        return chunks()
+
+    async def blocking_tool(_: ToolContext) -> None:
+        handler_started.set()
+        try:
+            await asyncio.Event().wait()
+        except asyncio.CancelledError:
+            handler_cancelled.set()
+            raise
+
+    tool = _recovery_tool().model_copy(
+        update={"retry_policy": ToolRetryPolicy.SAFE_RETRY}
+    )
+    spec = AgentSpec(name="completed-ref-recovery", model="test/model")
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=first_provider,
+        permission_default="allow",
+        enable_builtin_tools=False,
+    )
+    sdk.tools.register(tool, blocking_tool)
+    session = await sdk.sessions.create(workspaces=[])
+    handle = await sdk.runs.start(
+        session.session_id,
+        spec,
+        "Complete the model, then recover the Tool.",
+    )
+    await asyncio.wait_for(handler_started.wait(), timeout=2)
+    operations = await durable.list_external_operations(handle.run_id)
+    old_model = next(
+        operation
+        for operation in operations
+        if isinstance(operation, ModelCallOperation)
+    )
+    assert old_model.status is reconciliation.ExternalOperationStatus.COMPLETED
+    assert old_model.context_view_id is not None
+    assert old_model.prompt_manifest_id is not None
+    assert handle._task is not None
+    handle._task.cancel()
+    with pytest.raises(asyncio.CancelledError):
+        await handle._task
+    await asyncio.wait_for(handler_cancelled.wait(), timeout=2)
+    await sdk.close()
+
+    scanner = AgentSDK.for_test(
+        store=durable,
+        acompletion=first_provider,
+        enable_builtin_tools=False,
+    )
+    try:
+        await scanner.recovery.scan()
+    finally:
+        await scanner.close()
+
+    events_before = await durable.read_events(
+        after_cursor=0,
+        session_id=session.session_id,
+    )
+    old_pair_counts = {
+        event_type: sum(item.event.type == event_type for item in events_before)
+        for event_type in ("context.view.created", "prompt.manifest.created")
+    }
+    assert old_pair_counts == {
+        "context.view.created": 1,
+        "prompt.manifest.created": 1,
+    }
+    store.reads.clear()
+    if tamper_old_view:
+        del durable._snapshots[("context_view", old_model.context_view_id)]
+
+    provider_calls = 0
+    recovered_tool_calls = 0
+
+    async def final_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
+        nonlocal provider_calls
+        provider_calls += 1
+
+        async def chunks() -> AsyncIterator[dict[str, object]]:
+            yield {
+                "choices": [
+                    {
+                        "delta": {"content": "done"},
+                        "finish_reason": "stop",
+                    }
+                ]
+            }
+
+        return chunks()
+
+    async def recovered_tool(_: ToolContext) -> None:
+        nonlocal recovered_tool_calls
+        recovered_tool_calls += 1
+
+    reopened = AgentSDK.for_test(
+        store=store,
+        acompletion=final_provider,
+        permission_default="allow",
+        enable_builtin_tools=False,
+    )
+    reopened.agents.define(spec)
+    reopened.tools.register(tool, recovered_tool)
+    try:
+        if tamper_old_view:
+            with pytest.raises(AgentSDKError, match="recovery state conflict"):
+                await reopened.recovery.recover_run(handle.run_id)
+            assert provider_calls == 0
+            assert recovered_tool_calls == 0
+            return
+        result = await (await reopened.recovery.recover_run(handle.run_id)).result()
+        assert result.output_text == "done"
+        assert provider_calls == 1
+        assert recovered_tool_calls == 1
+        assert (
+            "context_view",
+            old_model.context_view_id,
+        ) in store.reads
+        assert (
+            "prompt_manifest",
+            old_model.prompt_manifest_id,
+        ) in store.reads
+
+        recovered_operations = await durable.list_external_operations(handle.run_id)
+        recovered_models = tuple(
+            operation
+            for operation in recovered_operations
+            if isinstance(operation, ModelCallOperation)
+        )
+        assert len(recovered_models) == 2
+        assert recovered_models[0].operation_id == old_model.operation_id
+        assert recovered_models[0].context_view_id == old_model.context_view_id
+        assert (
+            recovered_models[0].prompt_manifest_id
+            == old_model.prompt_manifest_id
+        )
+        assert recovered_models[1].context_view_id != old_model.context_view_id
+        assert (
+            recovered_models[1].prompt_manifest_id
+            != old_model.prompt_manifest_id
+        )
+
+        events_after = await durable.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        assert {
+            event_type: sum(
+                item.event.type == event_type for item in events_after
+            )
+            for event_type in old_pair_counts
+        } == {
+            "context.view.created": 2,
+            "prompt.manifest.created": 2,
+        }
+    finally:
+        await reopened.close()
diff --git a/tests/integration/context/test_public_context_api.py b/tests/integration/context/test_public_context_api.py
index bc01e2c..4403537 100644
--- a/tests/integration/context/test_public_context_api.py
+++ b/tests/integration/context/test_public_context_api.py
@@ -60,42 +60,58 @@ async def _provider(**params: Any) -> object:

     return chunks()


 @pytest.mark.asyncio
 async def test_context_facade_builds_retrieves_and_deletes_session_capsule() -> None:
     sdk = AgentSDK.for_test(store=InMemoryStore(), acompletion=_provider)
     assert isinstance(sdk.context, ContextAPI)
     session = await sdk.sessions.create(workspaces=[])
     agent = sdk.agents.define(AgentSpec(name="main", model="fake/main"))
-    run = await sdk.runs.start(session.session_id, agent, "retain this input")
-    await run.result()
+    first_run = await sdk.runs.start(
+        session.session_id,
+        agent,
+        "retain this historical input",
+    )
+    await first_run.result()
+    second_run = await sdk.runs.start(
+        session.session_id,
+        agent,
+        "keep this recent input exact",
+    )
+    await second_run.result()

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
     assert capsule.source_event_ids == tuple(
         item.event.event_id for item in sources
     )
+    assert len(sources) == 2
+    assert [item.event.type for item in sources] == [
+        "run.created",
+        "model.text.delta",
+    ]
+    assert sources[0].event.payload["user_input"] == "retain this historical input"
     assert all(isinstance(item, ObservedEvent) for item in sources)

     await sdk.sessions.close(session.session_id)
     await sdk.sessions.delete(session.session_id)
     with pytest.raises(AgentSDKError) as missing:
         await sdk.context.get_capsule(
             view.capsule_id,
             session_id=session.session_id,
         )
     assert missing.value.code is ErrorCode.NOT_FOUND
diff --git a/tests/integration/context/test_runtime_middleware.py b/tests/integration/context/test_runtime_middleware.py
new file mode 100644
index 0000000..cc8af9d
--- /dev/null
+++ b/tests/integration/context/test_runtime_middleware.py
@@ -0,0 +1,232 @@
+from __future__ import annotations
+
+import json
+from collections.abc import AsyncIterator
+from typing import Any
+
+import pytest
+
+from agent_sdk import AgentSDK, AgentSDKError, AgentSpec, ToolSpec
+from agent_sdk.runtime.reconciliation import ModelCallOperation
+from agent_sdk.storage.base import CommitBatch, CommitResult
+from agent_sdk.storage.memory import InMemoryStore
+from agent_sdk.tools.models import ToolContext
+
+
+def _tool_stream() -> AsyncIterator[dict[str, object]]:
+    async def chunks() -> AsyncIterator[dict[str, object]]:
+        yield {
+            "choices": [
+                {
+                    "delta": {
+                        "tool_calls": [
+                            {
+                                "index": 0,
+                                "id": "call_lookup",
+                                "function": {
+                                    "name": "lookup",
+                                    "arguments": json.dumps({"query": "context"}),
+                                },
+                            }
+                        ]
+                    },
+                    "finish_reason": "tool_calls",
+                }
+            ]
+        }
+
+    return chunks()
+
+
+def _text_stream(text: str) -> AsyncIterator[dict[str, object]]:
+    async def chunks() -> AsyncIterator[dict[str, object]]:
+        yield {
+            "choices": [
+                {
+                    "delta": {"content": text},
+                    "finish_reason": "stop",
+                }
+            ]
+        }
+
+    return chunks()
+
+
+class _DeleteViewAfterManifestStore:
+    def __init__(self, delegate: InMemoryStore) -> None:
+        self.delegate = delegate
+
+    def __getattr__(self, name: str) -> Any:
+        return getattr(self.delegate, name)
+
+    async def commit(self, batch: CommitBatch) -> CommitResult:
+        result = await self.delegate.commit(batch)
+        manifest = next(
+            (
+                event
+                for event in batch.events
+                if event.type == "prompt.manifest.created"
+            ),
+            None,
+        )
+        if manifest is not None:
+            view_id = manifest.payload["context_view_id"]
+            assert isinstance(view_id, str)
+            del self.delegate._snapshots[("context_view", view_id)]
+        return result
+
+
+@pytest.mark.asyncio
+async def test_context_is_prepared_before_each_new_model_call() -> None:
+    store = InMemoryStore()
+    requests: list[dict[str, Any]] = []
+
+    async def provider(**kwargs: Any) -> object:
+        requests.append(kwargs)
+        return _tool_stream() if len(requests) == 1 else _text_stream("done")
+
+    async def lookup(
+        _context: ToolContext,
+        *,
+        query: str,
+    ) -> dict[str, str]:
+        return {"query": query, "answer": "use durable context"}
+
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=provider,
+        permission_default="allow",
+        enable_builtin_tools=False,
+    )
+    sdk.tools.register(
+        ToolSpec(
+            name="lookup",
+            description="Look up context",
+            input_schema={
+                "type": "object",
+                "properties": {"query": {"type": "string"}},
+                "required": ["query"],
+                "additionalProperties": False,
+            },
+        ),
+        lookup,
+    )
+    try:
+        session = await sdk.sessions.create(workspaces=[])
+        handle = await sdk.runs.start(
+            session.session_id,
+            AgentSpec(
+                name="context-agent",
+                model="test/model",
+                system_prompt="Application constraint.",
+            ),
+            "Use the lookup tool.",
+        )
+
+        assert (await handle.result()).output_text == "done"
+        events = await store.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        views = [
+            stored
+            for stored in events
+            if stored.event.type == "context.view.created"
+        ]
+        starts = [
+            stored
+            for stored in events
+            if stored.event.type == "model.call.started"
+            and stored.event.run_id == handle.run_id
+        ]
+        assert len(views) == len(starts) == len(requests) == 2
+        assert views[0].event.payload["view_id"] != views[1].event.payload["view_id"]
+        for view, started in zip(views, starts, strict=True):
+            assert view.cursor < started.cursor
+            assert (
+                started.event.payload["context_view_id"]
+                == view.event.payload["view_id"]
+            )
+            assert started.event.payload["prompt_manifest_id"]
+
+        assert [message["role"] for message in requests[0]["messages"][:2]] == [
+            "system",
+            "system",
+        ]
+        assert requests[0]["messages"][1]["content"] == "Application constraint."
+        assert any(
+            message.get("role") == "tool"
+            and "durable context" in str(message.get("content"))
+            for message in requests[1]["messages"]
+        )
+        tool_event = next(
+            stored.event
+            for stored in events
+            if stored.event.type == "tool.call.completed"
+        )
+        assert tool_event.event_id in views[1].event.payload["source_refs"]
+
+        checkpoint = await store.get_run_checkpoint(handle.run_id)
+        assert checkpoint is not None
+        checkpoint_messages = checkpoint.model_dump(mode="json")["messages"]
+        assert all(message["role"] != "system" for message in checkpoint_messages)
+        assert [message["role"] for message in checkpoint_messages] == [
+            "user",
+            "assistant",
+            "tool",
+            "assistant",
+        ]
+
+        operations = await store.list_external_operations(handle.run_id)
+        model_operations = tuple(
+            operation
+            for operation in operations
+            if isinstance(operation, ModelCallOperation)
+        )
+        assert len(model_operations) == 2
+        assert tuple(operation.context_view_id for operation in model_operations) == (
+            views[0].event.payload["view_id"],
+            views[1].event.payload["view_id"],
+        )
+        assert all(operation.prepared_request is not None for operation in model_operations)
+    finally:
+        await sdk.close()
+
+
+@pytest.mark.asyncio
+async def test_model_start_requires_prepared_snapshots_to_still_exist() -> None:
+    durable = InMemoryStore()
+    store = _DeleteViewAfterManifestStore(durable)
+    provider_calls = 0
+
+    async def provider(**_: Any) -> object:
+        nonlocal provider_calls
+        provider_calls += 1
+        return _text_stream("must not run")
+
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=provider,
+        enable_builtin_tools=False,
+    )
+    try:
+        session = await sdk.sessions.create(workspaces=[])
+        handle = await sdk.runs.start(
+            session.session_id,
+            AgentSpec(name="context-race", model="test/model"),
+            "Require durable references.",
+        )
+
+        with pytest.raises(AgentSDKError):
+            await handle.result()
+        assert provider_calls == 0
+        events = await durable.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        assert all(
+            event.event.type != "model.call.started" for event in events
+        )
+        assert await durable.list_external_operations(handle.run_id) == ()
+    finally:
+        await sdk.close()
diff --git a/tests/integration/prompts/test_prompt_slice.py b/tests/integration/prompts/test_prompt_slice.py
index d6f49e1..62adbc9 100644
--- a/tests/integration/prompts/test_prompt_slice.py
+++ b/tests/integration/prompts/test_prompt_slice.py
@@ -7,20 +7,21 @@ import tarfile
 import zipfile
 from hashlib import sha256
 from pathlib import Path

 import pytest
 from pydantic import ValidationError

 from agent_sdk.context import CompactionLevel, ContextView
 from agent_sdk.errors import AgentSDKError, ErrorCode
 from agent_sdk.prompts import PromptComposer
+from agent_sdk.skills import SkillRegistry


 def _view() -> ContextView:
     return ContextView(
         view_id="view_prompt",
         session_id="ses_prompt",
         message_refs=("evt_user",),
         capsule_id=None,
         estimated_tokens=10,
         recommended_level=CompactionLevel.L0,
@@ -74,20 +75,54 @@ def test_general_profile_is_first_and_application_is_last() -> None:
     )
     assert tuple(layer.layer_id for layer in coding.manifest.layers) == (
         "profile:general",
         "profile:coding",
         "application",
     )
     assert coding.messages[0] == general.messages[0]
     assert coding.messages[-1]["content"] == "Application layer."


+def test_skill_layers_preserve_order_and_reject_duplicate_names() -> None:
+    root = Path(__file__).parents[2] / "fixtures" / "skills"
+    registry = SkillRegistry((root,))
+    registry.discover()
+    demo = registry.activate("demo")
+    coding = registry.activate("coding-demo")
+    composer = PromptComposer()
+
+    built = composer.compose(
+        profile="general",
+        skills=(demo, coding),
+        context_view=_view(),
+        model="fake/model",
+    )
+
+    assert tuple(layer.layer_id for layer in built.manifest.layers[-2:]) == (
+        "skill:demo",
+        "skill:coding-demo",
+    )
+    assert tuple(layer.version for layer in built.manifest.layers[-2:]) == (
+        demo.metadata.content_hash,
+        coding.metadata.content_hash,
+    )
+    with pytest.raises(AgentSDKError) as raised:
+        composer.compose(
+            profile="general",
+            skills=(demo, demo),
+            context_view=_view(),
+            model="fake/model",
+        )
+    assert raised.value.code is ErrorCode.INVALID_STATE
+    assert raised.value.message == "duplicate prompt skill"
+
+
 def test_tool_fingerprint_uses_canonical_json_independent_of_key_order() -> None:
     first = PromptComposer().compose(
         profile="general",
         context_view=_view(),
         model="fake/model",
         tools=(
             {
                 "type": "function",
                 "function": {
                     "name": "lookup",
diff --git a/tests/integration/prompts/test_runtime_prompt.py b/tests/integration/prompts/test_runtime_prompt.py
new file mode 100644
index 0000000..10d1da4
--- /dev/null
+++ b/tests/integration/prompts/test_runtime_prompt.py
@@ -0,0 +1,964 @@
+from __future__ import annotations
+
+import json
+from collections.abc import AsyncIterator
+from copy import deepcopy
+from hashlib import sha256
+from pathlib import Path
+from typing import Any, Literal
+
+import pytest
+
+from agent_sdk import (
+    AgentNode,
+    AgentSDK,
+    ContextRuntimeConfig,
+    PromptManifestPersistence,
+    ToolSpec,
+    WorkflowIR,
+)
+from agent_sdk.context import CompactionLevel, ContextView
+from agent_sdk.errors import AgentSDKError, ErrorCode
+from agent_sdk.events.models import EventEnvelope
+from agent_sdk.models.litellm_gateway import LiteLLMGateway
+from agent_sdk.observability.queries import QueryService
+from agent_sdk.permissions.policy import PolicyEngine
+from agent_sdk.prompts import PromptComposer
+from agent_sdk.runtime.agents import AgentRegistry
+from agent_sdk.runtime.commands import RuntimeCommands
+from agent_sdk.runtime.engine import RunEngine
+from agent_sdk.runtime.execution import (
+    ExecutionDescriptor,
+    ExecutionPolicyDescriptor,
+)
+from agent_sdk.runtime.models import (
+    AgentSpec,
+    RunSnapshot,
+    RunStatus,
+    run_created_event_matches,
+)
+from agent_sdk.runtime.recovery import RunRecoveryService
+from agent_sdk.runtime.reconciliation import RecoveryStateConflictError
+from agent_sdk.skills import SkillRegistry
+from agent_sdk.storage.base import (
+    CommitBatch,
+    SnapshotPrecondition,
+    SnapshotPreconditionError,
+    SnapshotWrite,
+)
+from agent_sdk.storage.memory import InMemoryStore
+from agent_sdk.storage.sqlite import SQLiteStore
+from agent_sdk.subagents import SubagentService, TaskEnvelope
+from agent_sdk.tools.registry import ToolRegistry
+
+
+def _skill_root() -> Path:
+    return Path(__file__).parents[2] / "fixtures" / "skills"
+
+
+async def _unused_provider(**_: object) -> AsyncIterator[dict[str, object]]:
+    raise AssertionError("provider must not be called")
+
+
+async def _successful_provider(**_: object) -> AsyncIterator[dict[str, object]]:
+    async def chunks() -> AsyncIterator[dict[str, object]]:
+        yield {"choices": [{"delta": {"content": "done"}}]}
+        yield {
+            "choices": [{"delta": {}, "finish_reason": "stop"}],
+            "usage": {
+                "prompt_tokens": 2,
+                "completion_tokens": 1,
+                "total_tokens": 3,
+            },
+        }
+
+    return chunks()
+
+
+def _canonical_hash(value: object) -> str:
+    return sha256(
+        json.dumps(
+            value,
+            ensure_ascii=False,
+            sort_keys=True,
+            separators=(",", ":"),
+        ).encode("utf-8")
+    ).hexdigest()
+
+
+def _r2_execution_descriptor(
+    spec: AgentSpec,
+    user_input: str,
+) -> dict[str, Any]:
+    current = ExecutionDescriptor.create(
+        agent=spec,
+        messages=({"role": "user", "content": user_input},),
+        tools=(),
+        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
+    ).model_dump(mode="json")
+    for field in ("prompt_profile", "system_prompt", "skills", "context"):
+        current["agent"].pop(field)
+    current["agent_hash"] = _canonical_hash(current["agent"])
+    current["descriptor_hash"] = _canonical_hash(
+        {
+            key: value
+            for key, value in current.items()
+            if key != "descriptor_hash"
+        }
+    )
+    return current
+
+
+async def _seed_r2_schema_v1_run(
+    store: SQLiteStore,
+    spec: AgentSpec,
+    *,
+    tamper: Literal[
+        "agent_hash",
+        "descriptor_hash",
+        "identity",
+        "cross_session",
+    ]
+    | None = None,
+) -> tuple[str, str]:
+    session = await RuntimeCommands(store).create_session(workspaces=[])
+    run_id = f"run_r2_{tamper or 'valid'}"
+    user_input = "recover genuine R2 run"
+    current_descriptor = ExecutionDescriptor.create(
+        agent=spec,
+        messages=({"role": "user", "content": user_input},),
+        tools=(),
+        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
+    )
+    current = RunSnapshot(
+        run_id=run_id,
+        session_id=session.session_id,
+        agent_revision=f"{spec.name}:{spec.revision}",
+        status=RunStatus.CREATED,
+        user_input=user_input,
+        execution_compatibility="current",
+        execution_descriptor=current_descriptor,
+    )
+    raw_snapshot = current.model_dump(mode="json")
+    raw_snapshot["execution_descriptor"] = _r2_execution_descriptor(
+        spec,
+        user_input,
+    )
+    event_payload = deepcopy(raw_snapshot)
+    event_session_id = session.session_id
+    if tamper == "agent_hash":
+        event_payload["execution_descriptor"]["agent_hash"] = "a" * 64
+    elif tamper == "descriptor_hash":
+        event_payload["execution_descriptor"]["descriptor_hash"] = "d" * 64
+    elif tamper == "identity":
+        event_payload["parent_run_id"] = "run_forged_parent"
+    elif tamper == "cross_session":
+        event_session_id = "ses_cross_session"
+
+    updated_session = session.model_copy(
+        update={
+            "active_run_ids": (run_id,),
+            "version": session.version + 1,
+        }
+    )
+    await store.commit(
+        CommitBatch(
+            events=(
+                EventEnvelope.new(
+                    type="session.run.attached",
+                    session_id=session.session_id,
+                    run_id=None,
+                    sequence=updated_session.version,
+                    payload={"run_id": run_id},
+                ),
+                EventEnvelope.new(
+                    schema_version=1,
+                    type="run.created",
+                    session_id=event_session_id,
+                    run_id=run_id,
+                    sequence=1,
+                    payload=event_payload,
+                ),
+            ),
+            snapshots=(
+                SnapshotWrite(
+                    "session",
+                    session.session_id,
+                    session.session_id,
+                    updated_session.version,
+                    updated_session.model_dump(mode="json"),
+                ),
+                SnapshotWrite(
+                    "run",
+                    run_id,
+                    session.session_id,
+                    1,
+                    raw_snapshot,
+                ),
+            ),
+        )
+    )
+    return session.session_id, run_id
+
+
+@pytest.mark.asyncio
+async def test_runtime_prompt_orders_layers_and_persists_manifest_by_reference() -> None:
+    store = InMemoryStore()
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=_unused_provider,
+        skill_roots=(_skill_root(),),
+    )
+    try:
+        session = await sdk.sessions.create(workspaces=[])
+        view = ContextView(
+            view_id="view_runtime_prompt",
+            session_id=session.session_id,
+            message_refs=("evt_user",),
+            capsule_id=None,
+            estimated_tokens=10,
+            recommended_level=CompactionLevel.L0,
+            applied_level=CompactionLevel.L0,
+        )
+        await store.commit(
+            CommitBatch(
+                events=(),
+                snapshots=(
+                    SnapshotWrite(
+                        "context_view",
+                        view.view_id,
+                        session.session_id,
+                        1,
+                        view.model_dump(mode="json"),
+                    ),
+                )
+            )
+        )
+        spec = AgentSpec(
+            name="coding-agent",
+            model="test/model",
+            prompt_profile="coding",
+            system_prompt="Application constraint.",
+            skills=("coding-demo",),
+        )
+        activated = tuple(sdk.skills.activate(name) for name in spec.skills)
+        tools = (
+            {
+                "type": "function",
+                "function": {
+                    "name": "lookup",
+                    "parameters": {"type": "object"},
+                },
+            },
+        )
+
+        built = PromptComposer().compose(
+            profile=spec.prompt_profile,
+            application=spec.system_prompt,
+            skills=activated,
+            context_view=view,
+            model=spec.model,
+            tools=tools,
+        )
+        context_messages = ({"role": "user", "content": "Current request."},)
+        provider_messages = (*built.messages, *context_messages)
+        await PromptManifestPersistence(store).persist(
+            built.manifest,
+            session_id=session.session_id,
+        )
+
+        assert tuple(layer.layer_id for layer in built.manifest.layers) == (
+            "profile:general",
+            "profile:coding",
+            "application",
+            "skill:coding-demo",
+        )
+        assert tuple(message["content"] for message in provider_messages[-2:]) == (
+            activated[0].instructions,
+            "Current request.",
+        )
+        assert built.manifest.layers[-1].version == activated[0].metadata.content_hash
+        assert built.manifest.layers[-1].sha256 == sha256(
+            activated[0].instructions.encode("utf-8")
+        ).hexdigest()
+        assert built.manifest.context_view_id == view.view_id
+        assert built.manifest.model == spec.model
+        assert built.manifest.tools_sha256
+
+        snapshot = await store.get_snapshot(
+            "prompt_manifest",
+            built.manifest.manifest_id,
+        )
+        assert snapshot == built.manifest.model_dump(mode="json")
+        events = await store.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        created = next(
+            item.event
+            for item in events
+            if item.event.type == "prompt.manifest.created"
+        )
+        assert created.payload == {
+            "manifest_id": built.manifest.manifest_id,
+            "context_view_id": view.view_id,
+            "sha256": built.manifest.sha256,
+            "model": spec.model,
+            "tools_sha256": built.manifest.tools_sha256,
+            "layers": [
+                {"layer_id": layer.layer_id, "sha256": layer.sha256}
+                for layer in built.manifest.layers
+            ],
+        }
+        public_payload = json.dumps(created.payload, sort_keys=True)
+        for raw_text in (
+            "Application constraint.",
+            activated[0].instructions,
+            built.messages[0]["content"],
+        ):
+            assert raw_text not in public_payload
+    finally:
+        await sdk.close()
+
+
+@pytest.mark.asyncio
+async def test_sdk_discovers_skills_once_and_missing_skill_blocks_model_call(
+    monkeypatch: pytest.MonkeyPatch,
+) -> None:
+    calls: list[dict[str, object]] = []
+    discovery_calls = 0
+    original_discover = SkillRegistry.discover
+
+    def discover(registry: SkillRegistry) -> object:
+        nonlocal discovery_calls
+        discovery_calls += 1
+        return original_discover(registry)
+
+    monkeypatch.setattr(SkillRegistry, "discover", discover)
+
+    async def provider(**kwargs: object) -> AsyncIterator[dict[str, object]]:
+        calls.append(kwargs)
+        raise AssertionError("provider must not be called")
+
+    store = InMemoryStore()
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=provider,
+        skill_roots=(_skill_root(),),
+    )
+    try:
+        assert discovery_calls == 1
+        assert sdk.skills.activate("coding-demo").metadata.name == "coding-demo"
+        session = await sdk.sessions.create(workspaces=[])
+
+        with pytest.raises(AgentSDKError) as raised:
+            await sdk.runs.start(
+                session.session_id,
+                AgentSpec(
+                    name="coding-agent",
+                    model="test/model",
+                    skills=("missing-skill",),
+                ),
+                "Do work.",
+            )
+
+        assert raised.value.code is ErrorCode.INVALID_STATE
+        assert raised.value.message == "configured agent skill unavailable"
+        assert calls == []
+        assert discovery_calls == 1
+        events = await store.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        assert all(item.event.type != "run.created" for item in events)
+    finally:
+        await sdk.close()
+
+
+@pytest.mark.asyncio
+async def test_public_run_events_never_expose_prompt_or_tool_sentinels(
+    tmp_path: Path,
+) -> None:
+    skill_marker = "SKILL-INSTRUCTIONS-PRIVATE-7D01"
+    application_marker = "APPLICATION-SYSTEM-PROMPT-PRIVATE-9A23"
+    model_params_marker = "MODEL-PARAMS-PRIVATE-2C44"
+    tool_marker = "TOOL-SCHEMA-PRIVATE-4B18"
+    skill_root = tmp_path / "skills"
+    skill_dir = skill_root / "private-skill"
+    skill_dir.mkdir(parents=True)
+    (skill_dir / "SKILL.md").write_text(
+        "---\n"
+        "name: private-skill\n"
+        "description: private test skill\n"
+        "---\n"
+        f"# Private\n\n{skill_marker}\n",
+        encoding="utf-8",
+    )
+    store = InMemoryStore()
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=_successful_provider,
+        skill_roots=(skill_root,),
+        enable_builtin_tools=False,
+    )
+
+    async def private_tool(**_: object) -> dict[str, object]:
+        return {"ok": True}
+
+    sdk.tools.register(
+        ToolSpec(
+            name="private_tool",
+            description="private tool",
+            input_schema={
+                "type": "object",
+                "properties": {
+                    "secret": {"type": "string", "description": tool_marker}
+                },
+            },
+        ),
+        private_tool,
+    )
+    spec = AgentSpec(
+        name="private-agent",
+        model="test/model",
+        model_params={"application_secret": model_params_marker},
+        prompt_profile="coding",
+        system_prompt=application_marker,
+        skills=("private-skill",),
+    )
+    profile_texts = tuple(
+        message["content"]
+        for message in PromptComposer()
+        .compose(
+            profile="coding",
+            context_view=ContextView(
+                view_id="view_profile_sentinel",
+                session_id="ses_profile_sentinel",
+                message_refs=(),
+                capsule_id=None,
+                estimated_tokens=0,
+            ),
+            model=spec.model,
+        )
+        .messages
+    )
+    try:
+        session = await sdk.sessions.create(workspaces=[])
+        result = await (
+            await sdk.runs.start(session.session_id, spec, "ordinary user input")
+        ).result()
+        snapshot = await store.get_snapshot("run", result.run_id)
+        assert snapshot is not None
+        private_snapshot = json.dumps(snapshot, sort_keys=True)
+        assert application_marker in private_snapshot
+        assert model_params_marker in private_snapshot
+        assert tool_marker in private_snapshot
+
+        events = await store.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        public_events = json.dumps(
+            [item.event.model_dump(mode="json") for item in events],
+            sort_keys=True,
+        )
+        created = next(
+            item.event for item in events if item.event.type == "run.created"
+        )
+        assert created.schema_version == 2
+        assert "execution_descriptor" not in created.payload
+        for raw_text in (
+            application_marker,
+            model_params_marker,
+            skill_marker,
+            tool_marker,
+            *profile_texts,
+        ):
+            assert raw_text not in public_events
+    finally:
+        await sdk.close()
+
+
+@pytest.mark.asyncio
+async def test_workflow_missing_skill_fails_before_node_run_or_provider_call(
+    tmp_path: Path,
+) -> None:
+    calls: list[dict[str, object]] = []
+
+    async def provider(**kwargs: object) -> AsyncIterator[dict[str, object]]:
+        calls.append(kwargs)
+        raise AssertionError("provider must not be called")
+
+    skill_root = tmp_path / "skills"
+    skill_root.mkdir()
+    store = InMemoryStore()
+    sdk = AgentSDK.for_test(
+        store=store,
+        acompletion=provider,
+        skill_roots=(skill_root,),
+    )
+    sdk.agents.define(
+        AgentSpec(
+            name="worker",
+            revision="1",
+            model="test/model",
+            skills=("missing-skill",),
+        )
+    )
+    try:
+        session = await sdk.sessions.create(workspaces=[])
+        workflow = WorkflowIR.create(
+            name="missing-skill",
+            nodes=(
+                AgentNode(
+                    id="work",
+                    agent_revision="worker:1",
+                    input="Do work.",
+                ),
+            ),
+            edges=(),
+        )
+        handle = await sdk.workflows.start(session.session_id, workflow)
+
+        with pytest.raises(AgentSDKError) as raised:
+            await handle.result()
+
+        assert raised.value.code is ErrorCode.INVALID_STATE
+        assert raised.value.message == "configured agent skill unavailable"
+        assert calls == []
+        events = await store.read_events(
+            after_cursor=0,
+            session_id=session.session_id,
+        )
+        assert any(item.event.type == "workflow.started" for item in events)
+        assert all(item.event.type != "run.created" for item in events)
+    finally:
+        await sdk.close()
+
+
+@pytest.mark.asyncio
+async def test_subagent_missing_skill_fails_before_child_run_or_provider_call(
+    tmp_path: Path,
+) -> None:
+    calls: list[dict[str, object]] = []
+
+    async def provider(**kwargs: object) -> AsyncIterator[dict[str, object]]:
+        calls.append(kwargs)
+        raise AssertionError("provider must not be called")
+
+    skill_root = tmp_path / "skills"
+    skill_root.mkdir()
+    skills = SkillRegistry((skill_root,))
+    skills.discover()
+    store = InMemoryStore()
+    commands = RuntimeCommands(store, agent_preflight=skills.validate_agent)
+    engine = RunEngine(store, LiteLLMGateway._for_test(provider))
+    agents = AgentRegistry()
+    agents.define(
+        AgentSpec(
+            name="worker",
+            revision="1",
+            model="test/model",
+            skills=("missing-skill",),
+        )
+    )
+    service = SubagentService(store, commands, engine, agents)
+    session = await commands.create_session(workspaces=[])
+    parent = await commands.start_run(
+        session.session_id,
+        agent_revision="parent:1",
+        user_input="parent",
+    )
+
+    with pytest.raises(AgentSDKError) as raised:
+        await service.spawn(
+            session_id=session.session_id,
+            parent_run_id=parent.run_id,
+            workflow_run_id="wfr_missing_skill",
+            workflow_node_id="work",
+            agent_revision="worker:1",
+            task=TaskEnvelope(
+                objective="Do work.",
+                success_criteria=("Complete.",),
+            ),
+        )
+
+    assert raised.value.code is ErrorCode.INVALID_STATE
+    assert raised.value.message == "configured agent skill unavailable"
+    assert calls == []
+    events = await store.read_events(
+        after_cursor=0,
+        session_id=session.session_id,
+    )
+    assert [
+        item.event
+        for item in events
+        if item.event.type == "run.created"
+    ] == [
+        next(
+            item.event
+            for item in events
+            if item.event.run_id == parent.run_id
+            and item.event.type == "run.created"
+        )
+    ]
+
+
+@pytest.mark.asyncio
+async def test_genuine_r2_schema_v1_run_recovers_and_builds_tree_after_sqlite_reopen(
+    tmp_path: Path,
+) -> None:
+    database_path = tmp_path / "r2-v1.db"
+    spec = AgentSpec(name="r2-agent", revision="1", model="test/model")
+    store = await SQLiteStore.open(database_path)
+    try:
+        _, run_id = await _seed_r2_schema_v1_run(store, spec)
+    finally:
+        await store.close()
+
+    reopened = await SQLiteStore.open(database_path)
+    agents = AgentRegistry()
+    agents.define(spec)
+    tools = ToolRegistry()
+    policy = PolicyEngine("allow")
+    engine = RunEngine(
+        reopened,
+        LiteLLMGateway._for_test(_successful_provider),
+        tools,
+        policy,
+    )
+    recovery = RunRecoveryService(
+        reopened,
+        engine,
+        agents,
+        tools,
+        policy,
+    )
+    try:
+        persisted = RunSnapshot.model_validate(
+            await reopened.get_snapshot("run", run_id)
+        )
+        assert persisted.execution_descriptor is not None
+        assert persisted.execution_descriptor.agent.prompt_profile == "general"
+        assert persisted.execution_descriptor.agent.system_prompt is None
+        assert persisted.execution_descriptor.agent.skills == ()
+        tree = await QueryService(reopened).execution_tree(run_id)
+        assert tuple(node.snapshot.run_id for node in tree.nodes) == (run_id,)
+
+        plan = await recovery.plan(run_id)
+        assert plan.request is not None
+        result = await recovery.execute(plan)
+
+        assert result.run_id == run_id
+        assert result.output_text == "done"
+        assert RunSnapshot.model_validate(
+            await reopened.get_snapshot("run", run_id)
+        ).status is RunStatus.COMPLETED
+    finally:
+        await reopened.close()
+
+
+@pytest.mark.asyncio
+@pytest.mark.parametrize(
+    "tamper",
+    ("agent_hash", "descriptor_hash", "identity", "cross_session"),
+)
+async def test_r2_schema_v1_authentication_rejects_tampered_event_after_reopen(
+    tmp_path: Path,
+    tamper: Literal[
+        "agent_hash",
+        "descriptor_hash",
+        "identity",
+        "cross_session",
+    ],
+) -> None:
+    database_path = tmp_path / f"r2-v1-{tamper}.db"
+    spec = AgentSpec(name="r2-agent", revision="1", model="test/model")
+    store = await SQLiteStore.open(database_path)
+    try:
+        session_id, run_id = await _seed_r2_schema_v1_run(
+            store,
+            spec,
+            tamper=tamper,
+        )
+    finally:
+        await store.close()
+
+    reopened = await SQLiteStore.open(database_path)
+    try:
+        snapshot = RunSnapshot.model_validate(
+            await reopened.get_snapshot("run", run_id)
+        )
+        events = await reopened.read_events(after_cursor=0)
+        created = next(
+            stored.event
+            for stored in events
+            if stored.event.type == "run.created"
+        )
+        assert created.schema_version == 1
+        payload_matches = run_created_event_matches(
+            snapshot,
+            created.payload,
+            schema_version=created.schema_version,
+        )
+        assert payload_matches is (tamper == "cross_session")
+        assert snapshot.session_id == session_id
+        with pytest.raises(AgentSDKError) as raised:
+            await QueryService(reopened).execution_tree(run_id)
+        assert raised.value.code is ErrorCode.INTERNAL
+    finally:
+        await reopened.close()
+
+
+@pytest.mark.asyncio
+@pytest.mark.parametrize(
+    "tamper",
+    ("agent_hash", "descriptor_hash", "noncanonical_json"),
+)
+async def test_r2_private_snapshot_recovery_validation_rejects_tampering(
+    tmp_path: Path,
+    tamper: Literal["agent_hash", "descriptor_hash", "noncanonical_json"],
+) -> None:
+    database_path = tmp_path / f"r2-private-{tamper}.db"
+    spec = AgentSpec(name="r2-agent", revision="1", model="test/model")
+    store = await SQLiteStore.open(database_path)
+    try:
+        _, run_id = await _seed_r2_schema_v1_run(store, spec)
+        async with store._connection.execute(
+            """
+            SELECT data_json FROM snapshots
+            WHERE kind = 'run' AND entity_id = ?
+            """,
+            (run_id,),
+        ) as cursor:
+            row = await cursor.fetchone()
+        assert row is not None
+        stored_json = str(row[0])
+        if tamper == "noncanonical_json":
+            replacement = stored_json + " "
+        else:
+            raw = json.loads(stored_json)
+            raw["execution_descriptor"][tamper] = tamper[0] * 64
+            replacement = json.dumps(
+                raw,
+                ensure_ascii=False,
+                sort_keys=True,
+                separators=(",", ":"),
+            )
+        await store._connection.execute(
+            """
+            UPDATE snapshots SET data_json = ?
+            WHERE kind = 'run' AND entity_id = ?
+            """,
+            (replacement, run_id),
+        )
+        await store._connection.commit()
+    finally:
+        await store.close()
+
+    if tamper != "noncanonical_json":
+        with pytest.raises(ValueError, match="incompatible current projections"):
+            await SQLiteStore.open(database_path)
+        return
+
+    reopened = await SQLiteStore.open(database_path)
+    try:
+        with pytest.raises(RecoveryStateConflictError):
+            await reopened.list_external_operations(run_id)
+    finally:
+        await reopened.close()
+
+
+@pytest.mark.asyncio
+async def test_r2_authenticated_event_allows_normalized_snapshot_precondition(
+    tmp_path: Path,
+) -> None:
+    store = await SQLiteStore.open(tmp_path / "r2-precondition-valid.db")
+    spec = AgentSpec(name="r2-agent", revision="1", model="test/model")
+    try:
+        session_id, run_id = await _seed_r2_schema_v1_run(store, spec)
+        normalized = RunSnapshot.model_validate(
+            await store.get_snapshot("run", run_id)
+        )
+
+        await store.commit(
+            CommitBatch(
+                events=(),
+                preconditions=(
+                    SnapshotPrecondition(
+                        "run",
+                        run_id,
+                        version=1,
+                        session_id=session_id,
+                        data=normalized.model_dump(mode="json"),
+                    ),
+                ),
+            )
+        )
+    finally:
+        await store.close()
+
+
+@pytest.mark.asyncio
+@pytest.mark.parametrize(
+    "tamper",
+    (
+        "event_session",
+        "sequence",
+        "schema_version",
+        "payload",
+        "noncanonical_payload",
+        "old_hash",
+        "multiple_created",
+    ),
+)
+async def test_r2_normalized_snapshot_precondition_rejects_invalid_creation_event(
+    tmp_path: Path,
+    tamper: Literal[
+        "event_session",
+        "sequence",
+        "schema_version",
+        "payload",
+        "noncanonical_payload",
+        "old_hash",
+        "multiple_created",
+    ],
+) -> None:
+    store = await SQLiteStore.open(tmp_path / f"r2-precondition-{tamper}.db")
+    spec = AgentSpec(name="r2-agent", revision="1", model="test/model")
+    try:
+        session_id, run_id = await _seed_r2_schema_v1_run(store, spec)
+        normalized = RunSnapshot.model_validate(
+            await store.get_snapshot("run", run_id)
+        )
+        events = await store.read_events(after_cursor=0)
+        created = next(
+            stored.event
+            for stored in events
+            if stored.event.type == "run.created"
+        )
+        if tamper == "event_session":
+            await store._connection.execute(
+                "UPDATE events SET session_id = ? WHERE event_id = ?",
+                ("ses_forged", created.event_id),
+            )
+        elif tamper == "sequence":
+            await store._connection.execute(
+                "UPDATE events SET sequence = 2 WHERE event_id = ?",
+                (created.event_id,),
+            )
+        elif tamper == "schema_version":
+            await store._connection.execute(
+                "UPDATE events SET schema_version = 2 WHERE event_id = ?",
+                (created.event_id,),
+            )
+        elif tamper == "payload":
+            await store._connection.execute(
+                "UPDATE events SET payload_json = ? WHERE event_id = ?",
+                ('{"forged":"payload"}', created.event_id),
+            )
+        elif tamper == "noncanonical_payload":
+            await store._connection.execute(
+                "UPDATE events SET payload_json = payload_json || ' ' WHERE event_id = ?",
+                (created.event_id,),
+            )
+        elif tamper == "old_hash":
+            raw_payload = deepcopy(created.payload)
+            raw_payload["execution_descriptor"]["agent_hash"] = "a" * 64
+            await store._connection.execute(
+                "UPDATE events SET payload_json = ? WHERE event_id = ?",
+                (
+                    json.dumps(
+                        raw_payload,
+                        ensure_ascii=False,
+                        sort_keys=True,
+                        separators=(",", ":"),
+                    ),
+                    created.event_id,
+                ),
+            )
+        else:
+            await store.commit(
+                CommitBatch(
+                    events=(
+                        EventEnvelope.new(
+                            schema_version=1,
+                            type="run.created",
+                            session_id=session_id,
+                            run_id=run_id,
+                            sequence=2,
+                            payload=created.payload,
+                        ),
+                    ),
+                )
+            )
+        await store._connection.commit()
+
+        with pytest.raises(SnapshotPreconditionError):
+            await store.commit(
+                CommitBatch(
+                    events=(),
+                    preconditions=(
+                        SnapshotPrecondition(
+                            "run",
+                            run_id,
+                            version=1,
+                            session_id=session_id,
+                            data=normalized.model_dump(mode="json"),
+                        ),
+                    ),
+                )
+            )
+    finally:
+        await store.close()
+
+
+@pytest.mark.asyncio
+async def test_prompt_manifest_survives_sqlite_reopen(tmp_path: Path) -> None:
+    database_path = tmp_path / "prompt.db"
+    store = await SQLiteStore.open(database_path)
+    manifest_id = ""
+    try:
+        session = await RuntimeCommands(store).create_session(workspaces=[])
+        view = ContextView(
+            view_id="view_sqlite_prompt",
+            session_id=session.session_id,
+            message_refs=(),
+            capsule_id=None,
+            estimated_tokens=0,
+        )
+        await store.commit(
+            CommitBatch(
+                events=(),
+                snapshots=(
+                    SnapshotWrite(
+                        "context_view",
+                        view.view_id,
+                        session.session_id,
+                        1,
+                        view.model_dump(mode="json"),
+                    ),
+                ),
+            )
+        )
+        built = PromptComposer().compose(
+            profile="general",
+            context_view=view,
+            model="test/model",
+        )
+        manifest_id = built.manifest.manifest_id
+        await PromptManifestPersistence(store).persist(
+            built.manifest,
+            session_id=session.session_id,
+        )
+    finally:
+        await store.close()
+
+    reopened = await SQLiteStore.open(database_path)
+    try:
+        snapshot = await reopened.get_snapshot("prompt_manifest", manifest_id)
+        assert snapshot is not None
+        assert snapshot["manifest_id"] == manifest_id
+        assert ContextRuntimeConfig().model_window == 128_000
+    finally:
+        await reopened.close()
diff --git a/tests/integration/runtime/test_text_agent_loop.py b/tests/integration/runtime/test_text_agent_loop.py
index 041809b..0843593 100644
--- a/tests/integration/runtime/test_text_agent_loop.py
+++ b/tests/integration/runtime/test_text_agent_loop.py
@@ -127,29 +127,32 @@ async def test_agent_loop_persists_stream_usage_and_result(store: InMemoryStore)
             "step.started",
             "model.call.started",
             "model.text.delta",
             "model.usage.reported",
             "model.call.completed",
             "step.completed",
             "run.completed",
         ]
         assert [stored.event.sequence for stored in events] == list(range(1, 10))
         assert events[-1].event.payload["usage"] == usage.model_dump()
-        assert calls == [
-            {
-                "model": "fake/model",
-                "messages": [{"role": "user", "content": "say hello"}],
-                "tools": [],
-                "stream": True,
-                "temperature": 0.25,
-            }
-        ]
+        assert len(calls) == 1
+        assert calls[0]["model"] == "fake/model"
+        assert calls[0]["tools"] == list(sdk.tools.schemas())
+        assert calls[0]["stream"] is True
+        assert calls[0]["temperature"] == 0.25
+        provider_messages = calls[0]["messages"]
+        assert isinstance(provider_messages, list)
+        assert provider_messages[0]["role"] == "system"
+        assert provider_messages[-1] == {
+            "role": "user",
+            "content": "say hello",
+        }
     finally:
         await sdk.close()


 def test_agent_spec_recursively_detaches_and_freezes_model_params() -> None:
     source = {"metadata": {"labels": ["original"]}}
     spec = AgentSpec(name="test", model="fake/model", model_params=source)
     same_default_revision = AgentSpec(name="other", model="fake/model")

     source["metadata"]["labels"].append("external mutation")
diff --git a/tests/integration/runtime/test_tool_recovery_execution.py b/tests/integration/runtime/test_tool_recovery_execution.py
index 6eb43ca..b035c6b 100644
--- a/tests/integration/runtime/test_tool_recovery_execution.py
+++ b/tests/integration/runtime/test_tool_recovery_execution.py
@@ -2235,20 +2235,40 @@ async def test_historical_recovery_evidence_is_reconstructed_exactly(
         handler_calls += 1
         return value

     sdk = AgentSDK.for_test(
         store=store,
         acompletion=lambda **_: _final_completion(model_calls),
         permission_default="allow",
     )
     sdk.agents.define(spec)
     sdk.tools.register(tool_spec, handler)
+    if corruption == "model_fingerprint":
+        try:
+            with pytest.raises(
+                AgentSDKError,
+                match="recovery state conflict",
+            ) as caught:
+                await sdk.recovery.recover_run(run_id)
+            assert handler_calls == 0
+            assert model_calls == []
+            assert secret not in repr(caught.value.to_dict())
+            reconciliation_events = [
+                stored.event.model_dump(mode="json")
+                for stored in await store.read_events(after_cursor=0)
+                if stored.event.run_id == run_id
+                and stored.event.type == "reconciliation.requested"
+            ]
+            assert reconciliation_events == []
+        finally:
+            await sdk.close()
+        return
     handle = await sdk.recovery.recover_run(run_id)
     try:
         with pytest.raises(AgentSDKError, match="recovery required") as caught:
             await handle.result()
         assert handler_calls == 0
         assert model_calls == []
         pending = await sdk.recovery.pending_requests(run_id)
         assert len(pending) == 1
         assert pending[0].reason == "tool_call_unknown_outcome"
         assert secret not in repr(caught.value.to_dict())
diff --git a/tests/unit/context/test_compaction_levels.py b/tests/unit/context/test_compaction_levels.py
new file mode 100644
index 0000000..4214c8b
--- /dev/null
+++ b/tests/unit/context/test_compaction_levels.py
@@ -0,0 +1,156 @@
+from __future__ import annotations
+
+import json
+from collections.abc import Sequence
+from typing import Any
+
+import pytest
+
+from agent_sdk.context.compactor import ContextCompactor
+from agent_sdk.context.models import ContextCapsule, ContextItem
+from agent_sdk.models.litellm_gateway import (
+    ModelRequest,
+    StructuredCompletion,
+    UsageReported,
+)
+
+
+def _item(
+    ref: str,
+    content: str,
+    *,
+    cursor: int,
+    role: str = "user",
+) -> ContextItem:
+    return ContextItem(
+        event_id=ref,
+        cursor=cursor,
+        event_type="context.message.appended",
+        role=role,
+        content=content,
+    )
+
+
+def _capsule(*refs: str, objective: str = "ship") -> ContextCapsule:
+    return ContextCapsule(
+        objective=objective,
+        constraints=("preserve evidence",),
+        decisions=(),
+        facts=(),
+        next_actions=("verify",),
+        artifact_refs=(),
+        source_event_ids=refs,
+    )
+
+
+class _StructuredGateway:
+    def __init__(self, responses: Sequence[ContextCapsule]) -> None:
+        self._responses = iter(responses)
+        self.requests: list[ModelRequest] = []
+
+    async def complete_structured(
+        self,
+        request: ModelRequest,
+        schema: type[ContextCapsule],
+    ) -> StructuredCompletion[ContextCapsule]:
+        assert schema is ContextCapsule
+        self.requests.append(request)
+        return StructuredCompletion(
+            parsed=next(self._responses),
+            usage=UsageReported(11, 4, 15),
+        )
+
+
+@pytest.mark.asyncio
+async def test_l3_summarize_sends_only_closed_older_slice() -> None:
+    sources = (
+        _item("evt_old_user", "old question", cursor=1),
+        _item("evt_old_answer", "old answer", cursor=2, role="assistant"),
+        _item("evt_recent", "recent question", cursor=3),
+        _item("evt_protected", "must remain exact", cursor=4),
+    )
+    gateway = _StructuredGateway(
+        [_capsule("evt_old_user", "evt_old_answer")]
+    )
+    compactor = ContextCompactor(gateway, model="fake/compact")  # type: ignore[arg-type]
+
+    result = await compactor.summarize(
+        sources,
+        {"evt_recent", "evt_protected"},
+    )
+
+    assert result.capsule == _capsule("evt_old_user", "evt_old_answer")
+    assert result.usage == UsageReported(11, 4, 15)
+    request = gateway.requests[0]
+    assert request.purpose == "context_compaction"
+    document = json.loads(request.messages[-1]["content"])
+    assert [item["event_id"] for item in document["sources"]] == [
+        "evt_old_user",
+        "evt_old_answer",
+    ]
+    assert document["retained_event_ids"] == ["evt_recent", "evt_protected"]
+
+
+@pytest.mark.asyncio
+async def test_l3_rejects_citation_of_retained_message() -> None:
+    sources = (
+        _item("evt_old", "old question", cursor=1),
+        _item("evt_recent", "recent question", cursor=2),
+    )
+    gateway = _StructuredGateway([_capsule("evt_old", "evt_recent")])
+    compactor = ContextCompactor(gateway, model="fake/compact")  # type: ignore[arg-type]
+
+    result = await compactor.summarize(sources, {"evt_recent"})
+
+    assert result.capsule is None
+    assert result.usage == UsageReported(11, 4, 15)
+    assert len(gateway.requests) == 1
+
+
+@pytest.mark.asyncio
+async def test_l4_rebase_supplies_prior_capsules_and_active_bounded_sources() -> None:
+    prior = (
+        _capsule("evt_prior_a", objective="prior A"),
+        _capsule("evt_prior_b", objective="prior B"),
+    )
+    source = (
+        _item("evt_old", "x" * 400_000, cursor=1),
+        _item("evt_active", "active constraint", cursor=2),
+        _item("evt_recent", "recent question", cursor=3),
+    )
+    gateway = _StructuredGateway(
+        [
+            _capsule(
+                "evt_prior_a",
+                "evt_prior_b",
+                "evt_active",
+                "evt_recent",
+                objective="rebased",
+            )
+        ]
+    )
+    compactor = ContextCompactor(gateway, model="fake/compact")  # type: ignore[arg-type]
+
+    result = await compactor.rebase(
+        prior,
+        source,
+        {"evt_active", "evt_recent"},
+    )
+
+    assert result.capsule is not None
+    assert {"evt_prior_a", "evt_prior_b"} <= set(
+        result.capsule.source_event_ids
+    )
+    request = gateway.requests[0]
+    assert request.purpose == "context_compaction"
+    encoded = request.messages[-1]["content"].encode("utf-8")
+    assert len(encoded) <= 256 * 1024
+    document: dict[str, Any] = json.loads(request.messages[-1]["content"])
+    assert [capsule["objective"] for capsule in document["capsules"]] == [
+        "prior A",
+        "prior B",
+    ]
+    assert [item["event_id"] for item in document["sources"]] == [
+        "evt_active",
+        "evt_recent",
+    ]
diff --git a/tests/unit/context/test_deterministic_strategies.py b/tests/unit/context/test_deterministic_strategies.py
new file mode 100644
index 0000000..cb17535
--- /dev/null
+++ b/tests/unit/context/test_deterministic_strategies.py
@@ -0,0 +1,869 @@
+from __future__ import annotations
+
+import copy
+import json
+from datetime import UTC, datetime
+from typing import Any
+
+import pytest
+from pydantic import ValidationError
+
+from agent_sdk.context.models import CompactionLevel, SourceMessage
+from agent_sdk.context.rendering import render_level
+from agent_sdk.context.sources import checkpoint_ref, extract_sources
+from agent_sdk.context.strategies import apply_l0, apply_l1, apply_l2
+from agent_sdk.events.models import EventEnvelope
+from agent_sdk.runtime.reconciliation import RunCheckpoint, RunCheckpointPhase
+from agent_sdk.storage.base import StoredEvent
+
+
+def _source(
+    ref: str,
+    role: str,
+    content: str,
+    *,
+    protected: bool = False,
+    current: bool = False,
+    **message_fields: Any,
+) -> SourceMessage:
+    event_type = {
+        "system": "context.message.appended",
+        "user": "run.created",
+        "assistant": "model.call.completed",
+        "tool": "tool.call.completed",
+    }[role]
+    return SourceMessage(
+        ref=ref,
+        role=role,
+        message={"role": role, "content": content, **message_fields},
+        event_type=event_type,
+        protected=protected,
+        current=current,
+    )
+
+
+def _strategy_sources() -> tuple[SourceMessage, ...]:
+    long_result = json.dumps(
+        {"rows": ["数据🙂" * 100, {"b": 2, "a": 1}]},
+        ensure_ascii=False,
+    )
+    return (
+        _source("evt-user-old", "user", "older request"),
+        _source("evt-assistant-old", "assistant", "older answer"),
+        _source(
+            "evt-tool",
+            "tool",
+            long_result,
+            tool_call_id="call-old",
+            name="lookup",
+        ),
+        _source(
+            "evt-tool-2",
+            "tool",
+            json.dumps(
+                {"rows": ["数据🙂" * 100, {"a": 1, "b": 2}]},
+                ensure_ascii=False,
+                separators=(",", ":"),
+            ),
+            tool_call_id="call-repeat",
+            name="lookup",
+        ),
+        _source(
+            "evt-constraint",
+            "system",
+            "Never publish secrets.",
+            protected=True,
+        ),
+        _source(
+            "checkpoint:run-current:7:0",
+            "user",
+            "current request",
+            protected=True,
+            current=True,
+        ),
+        _source(
+            "evt-current-model",
+            "assistant",
+            "",
+            protected=True,
+            current=True,
+            tool_calls=[
+                {
+                    "id": "call-current",
+                    "type": "function",
+                    "function": {"name": "lookup", "arguments": "{}"},
+                }
+            ],
+        ),
+        _source(
+            "evt-current-tool",
+            "tool",
+            '{"ok":true}',
+            protected=True,
+            current=True,
+            tool_call_id="call-current",
+            name="lookup",
+        ),
+        _source(
+            "state:workflow:wfr-active",
+            "system",
+            '{"status":"running","workflow_run_id":"wfr-active"}',
+            protected=True,
+        ),
+        _source(
+            "state:child:run-child",
+            "system",
+            '{"run_id":"run-child","status":"running"}',
+            protected=True,
+        ),
+    )
+
+
+def _outcome(item: SourceMessage) -> dict[str, Any]:
+    value = json.loads(item.message["content"])
+    assert list(value) == ["kind", "role", "source_refs", "status", "summary"]
+    return value
+
+
+def test_l0_returns_all_messages_unchanged_ordered_and_detached() -> None:
+    sources = _strategy_sources()
+    before = copy.deepcopy([item.model_dump(mode="json") for item in sources])
+
+    rendered = apply_l0(sources)
+
+    assert rendered.items == sources
+    assert rendered.source_refs == tuple(item.ref for item in sources)
+    assert rendered.transformations == ()
+    assert [item.model_dump(mode="json") for item in sources] == before
+    with pytest.raises(TypeError):
+        rendered.items[0].message["content"] = "mutated"  # type: ignore[index]
+
+
+def test_l1_previews_tools_byte_safely_and_deduplicates_canonical_json() -> None:
+    sources = _strategy_sources()
+    before = copy.deepcopy([item.model_dump(mode="json") for item in sources])
+
+    rendered = apply_l1(sources, tool_preview_bytes=256)
+
+    tool = next(item for item in rendered.items if item.ref == "evt-tool")
+    duplicate = next(item for item in rendered.items if item.ref == "evt-tool-2")
+    assert len(tool.message["content"].encode("utf-8")) <= 256 + 96
+    assert "[source:evt-tool]" in tool.message["content"]
+    assert duplicate.message["content"] == "[duplicate:evt-tool]"
+    assert rendered.source_refs == tuple(item.ref for item in sources)
+    assert rendered.transformations == (
+        "tool_preview:evt-tool",
+        "dedupe:evt-tool-2",
+    )
+    assert [item.model_dump(mode="json") for item in sources] == before
+
+
+def test_l1_treats_nonstandard_json_constants_as_plain_tool_text() -> None:
+    sources = (
+        _source("evt-nan", "tool", "NaN"),
+        _source("evt-nan-repeat", "tool", "NaN"),
+    )
+
+    rendered = apply_l1(sources, tool_preview_bytes=16)
+
+    assert rendered.items[0] == sources[0]
+    assert rendered.items[1].message["content"] == "[duplicate:evt-nan]"
+
+
+@pytest.mark.parametrize(
+    ("first", "second", "duplicate"),
+    [
+        ("alpha", '"alpha"', False),
+        ('{"a":1,"a":2}', '{"a":2}', False),
+        ('{"b":2,"a":1}', '{"a":1,"b":2}', True),
+        ("[1,2]", "[1,2]", True),
+        ("[1,2]", "[2,1]", False),
+        ("1", "1.0", False),
+        ("true", "false", False),
+        ("NaN", "NaN", True),
+        ("Infinity", '"Infinity"', False),
+    ],
+)
+def test_l1_uses_collision_safe_json_and_raw_hash_domains(
+    first: str,
+    second: str,
+    duplicate: bool,
+) -> None:
+    sources = (
+        _source("evt-first", "tool", first),
+        _source("evt-second", "tool", second),
+    )
+
+    rendered = apply_l1(sources, tool_preview_bytes=64)
+
+    if duplicate:
+        assert rendered.items[1].message["content"] == "[duplicate:evt-first]"
+        assert rendered.transformations == ("dedupe:evt-second",)
+    else:
+        assert rendered.items == sources
+        assert rendered.transformations == ()
+
+
+def test_l2_retains_protected_current_and_recent_and_structures_old_outcomes() -> None:
+    sources = _strategy_sources()
+    before = copy.deepcopy([item.model_dump(mode="json") for item in sources])
+
+    rendered = apply_l2(
+        sources,
+        recent_messages=1,
+        tool_preview_bytes=64,
+    )
+
+    by_ref = {item.ref: item for item in rendered.items}
+    for source in sources[4:]:
+        assert by_ref[source.ref].message == source.message
+    for ref, expected_role, expected_kind in (
+        ("evt-user-old", "user", "exchange"),
+        ("evt-assistant-old", "assistant", "exchange"),
+        ("evt-tool", "tool", "tool_result"),
+        ("evt-tool-2", "tool", "tool_result"),
+    ):
+        outcome = _outcome(by_ref[ref])
+        assert outcome["kind"] == expected_kind
+        assert outcome["role"] == expected_role
+        assert outcome["status"] == "completed"
+        assert outcome["source_refs"] == [ref]
+        assert outcome["summary"]
+    assert rendered.source_refs == tuple(item.ref for item in sources)
+    assert len(rendered.source_refs) == len(set(rendered.source_refs))
+    assert [item.model_dump(mode="json") for item in sources] == before
+
+
+def test_l2_layers_l1_preview_over_a_recent_unprotected_tool() -> None:
+    sources = (
+        _source("evt-old-user", "user", "older request"),
+        _source("evt-recent-tool", "tool", "数据🙂" * 300),
+    )
+
+    l1 = apply_l1(sources, tool_preview_bytes=64)
+    l2 = apply_l2(sources, recent_messages=1, tool_preview_bytes=64)
+
+    recent_l1 = l1.items[1].message["content"]
+    recent_l2 = l2.items[1].message["content"]
+    assert "[source:evt-recent-tool]" in recent_l2
+    assert len(recent_l2.encode("utf-8")) <= 64 + 96
+    assert len(recent_l2.encode("utf-8")) <= len(recent_l1.encode("utf-8"))
+    assert l2.transformations == (
+        "tool_preview:evt-recent-tool",
+        "outcome:evt-old-user",
+    )
+
+
+@pytest.mark.parametrize(
+    "ref",
+    [
+        "r" * 64,
+        ("界" * 21) + "r",
+    ],
+)
+def test_l1_and_l2_bound_complete_preview_for_maximum_byte_ref(ref: str) -> None:
+    sources = (
+        _source("evt-old-user", "user", "older request"),
+        _source(ref, "tool", "数据🙂" * 300),
+    )
+
+    rendered = (
+        apply_l1(sources, tool_preview_bytes=64),
+        apply_l2(sources, recent_messages=1, tool_preview_bytes=64),
+    )
+
+    assert len(ref.encode("utf-8")) == 64
+    for result in rendered:
+        preview = result.items[1].message["content"]
+        assert f"[source:{ref}]" in preview
+        assert len(preview.encode("utf-8")) <= 64 + 96
+
+
+@pytest.mark.parametrize(
+    "ref",
+    [
+        "r" * 65,
+        ("界" * 21) + "rr",
+    ],
+)
+def test_source_message_rejects_ref_above_64_utf8_bytes(ref: str) -> None:
+    assert len(ref.encode("utf-8")) == 65
+    with pytest.raises(ValidationError, match="ref must not exceed 64 UTF-8 bytes"):
+        _source(ref, "tool", "result")
+
+
+def test_render_level_dispatches_l0_l2_and_rejects_model_levels() -> None:
+    sources = _strategy_sources()
+    assert render_level(
+        CompactionLevel.L0,
+        sources,
+        recent_messages=2,
+        tool_preview_bytes=32,
+    ) == apply_l0(sources)
+    assert render_level(
+        CompactionLevel.L1,
+        sources,
+        recent_messages=2,
+        tool_preview_bytes=32,
+    ) == apply_l1(sources, tool_preview_bytes=32)
+    assert render_level(
+        CompactionLevel.L2,
+        sources,
+        recent_messages=2,
+        tool_preview_bytes=32,
+    ) == apply_l2(sources, recent_messages=2, tool_preview_bytes=32)
+    with pytest.raises(
+        ValueError,
+        match="deterministic renderer supports L0-L2 only",
+    ):
+        render_level(
+            CompactionLevel.L3,
+            sources,
+            recent_messages=2,
+            tool_preview_bytes=32,
+        )
+
+
+def test_source_messages_validate_detached_json_and_unique_refs() -> None:
+    message = {
+        "role": "user",
+        "content": "nested",
+        "metadata": [{"value": 1}],
+    }
+    source = SourceMessage(
+        ref="evt-detached",
+        role="user",
+        message=message,
+        event_type="run.created",
+    )
+    message["metadata"][0]["value"] = 2
+    assert source.message["metadata"][0]["value"] == 1
+
+    with pytest.raises(ValidationError):
+        SourceMessage(
+            ref="evt-invalid",
+            role="user",
+            message={"role": "user", "content": "valid", "bad": object()},
+            event_type="run.created",
+        )
+    with pytest.raises(ValueError, match="source message refs must be unique"):
+        apply_l0((source, source))
+
+
+def _source_errors(**updates: Any) -> list[dict[str, Any]]:
+    values: dict[str, Any] = {
+        "ref": "evt-valid",
+        "role": "user",
+        "message": {"role": "user", "content": "valid"},
+        "event_type": "run.created",
+    }
+    values.update(updates)
+    if values["role"] is None:
+        del values["role"]
+    with pytest.raises(ValidationError) as raised:
+        SourceMessage(**values)
+    return raised.value.errors()
+
+
+def test_source_message_exposes_strict_bounded_runtime_interface() -> None:
+    message = {
+        "role": "assistant",
+        "content": None,
+        "tool_calls": [
+            {
+                "id": "call-1",
+                "type": "function",
+                "function": {"name": "lookup", "arguments": "{}"},
+            }
+        ],
+    }
+    source = SourceMessage(
+        ref="evt-valid",
+        role="assistant",
+        message=message,
+        event_type="model.call.completed",
+        protected=False,
+        current=True,
+    )
+    message["tool_calls"][0]["function"]["name"] = "mutated"
+    assert source.role == "assistant"
+    assert source.event_type == "model.call.completed"
+    assert source.message["tool_calls"][0]["function"]["name"] == "lookup"
+    assert source.model_dump(mode="json")["message"]["tool_calls"][0]["id"] == "call-1"
+
+
+def test_source_message_rejects_missing_unsupported_or_mismatched_roles() -> None:
+    missing = _source_errors(role=None)
+    unsupported = _source_errors(
+        role="invalid",
+        message={"role": "invalid", "content": "bad"},
+    )
+    mismatch = _source_errors(
+        role="user",
+        message={"role": "assistant", "content": "bad"},
+    )
+
+    assert any(error["loc"] == ("role",) for error in missing)
+    assert any(error["type"] == "literal_error" for error in unsupported)
+    assert "message role must match source role" in mismatch[0]["msg"]
+
+
+def test_source_message_rejects_invalid_provider_content_and_coerced_flags() -> None:
+    numeric_tool = _source_errors(
+        role="tool",
+        message={"role": "tool", "content": 7},
+        event_type="tool.call.completed",
+    )
+    coerced_protected = _source_errors(protected=1)
+    coerced_current = _source_errors(current=0)
+
+    assert "tool content must be a string" in numeric_tool[0]["msg"]
+    assert any(
+        error["loc"] == ("protected",) and error["type"] == "bool_type"
+        for error in coerced_protected
+    )
+    assert any(
+        error["loc"] == ("current",) and error["type"] == "bool_type"
+        for error in coerced_current
+    )
+
+
+@pytest.mark.parametrize(
+    "entry",
+    [
+        None,
+        "not-a-call",
+        {},
+        {
+            "id": "call-1",
+            "type": "function",
+            "function": {"name": "lookup"},
+        },
+        {
+            "id": "call-1",
+            "type": "function",
+            "function": {"name": "lookup", "arguments": "{}", "extra": True},
+        },
+        {
+            "id": "call-1",
+            "type": "function",
+            "function": {"name": "lookup", "arguments": "{}"},
+            "extra": True,
+        },
+        {
+            "id": "call-1",
+            "type": "other",
+            "function": {"name": "lookup", "arguments": "{}"},
+        },
+        {
+            "id": "",
+            "type": "function",
+            "function": {"name": "lookup", "arguments": "{}"},
+        },
+        {
+            "id": 1,
+            "type": "function",
+            "function": {"name": "lookup", "arguments": "{}"},
+        },
+        {
+            "id": "call-1",
+            "type": "function",
+            "function": {"name": "", "arguments": "{}"},
+        },
+        {
+            "id": "call-1",
+            "type": "function",
+            "function": {"name": 1, "arguments": "{}"},
+        },
+        {
+            "id": "call-1",
+            "type": "function",
+            "function": {"name": "lookup", "arguments": {}},
+        },
+    ],
+)
+def test_source_message_rejects_invalid_tool_call_protocol_entries(
+    entry: Any,
+) -> None:
+    with pytest.raises(ValidationError, match="tool_calls"):
+        SourceMessage(
+            ref="evt-invalid-call",
+            role="assistant",
+            message={
+                "role": "assistant",
+                "content": None,
+                "tool_calls": [entry],
+            },
+            event_type="model.call.completed",
+        )
+
+
+def test_source_message_validates_tool_calls_even_with_text_content() -> None:
+    with pytest.raises(ValidationError, match="tool_calls"):
+        SourceMessage(
+            ref="evt-empty-calls",
+            role="assistant",
+            message={
+                "role": "assistant",
+                "content": "text",
+                "tool_calls": [],
+            },
+            event_type="model.call.completed",
+        )
+
+
+def test_source_message_rejects_identity_and_json_resource_overflows() -> None:
+    long_ref = _source_errors(ref="r" * 513)
+    long_event_type = _source_errors(event_type="e" * 129)
+    oversized = _source_errors(
+        message={"role": "user", "content": "x" * (256 * 1024)}
+    )
+    too_many = _source_errors(
+        message={
+            "role": "assistant",
+            "content": "bounded",
+            "data": {str(index): 0 for index in range(20_001)},
+        },
+        role="assistant",
+    )
+
+    assert any(error["loc"] == ("ref",) for error in long_ref)
+    assert any(error["loc"] == ("event_type",) for error in long_event_type)
+    assert "serialized message exceeds 262144 bytes" in oversized[0]["msg"]
+    assert "message exceeds 20000 container entries" in too_many[0]["msg"]
+
+
+def test_source_message_normalizes_deep_cyclic_and_non_json_validation() -> None:
+    deep: list[Any] = []
+    cursor = deep
+    for _ in range(33):
+        child: list[Any] = []
+        cursor.append(child)
+        cursor = child
+    cyclic: dict[str, Any] = {}
+    cyclic["self"] = cyclic
+
+    deep_errors = _source_errors(
+        role="assistant",
+        message={"role": "assistant", "content": "bounded", "data": deep},
+    )
+    cyclic_errors = _source_errors(
+        role="assistant",
+        message={"role": "assistant", "content": "bounded", "data": cyclic},
+    )
+    key_errors = _source_errors(
+        role="assistant",
+        message={"role": "assistant", "content": "bounded", 1: "bad"},
+    )
+    number_errors = _source_errors(
+        role="assistant",
+        message={"role": "assistant", "content": "bounded", "score": float("inf")},
+    )
+
+    assert "message nesting exceeds 32" in deep_errors[0]["msg"]
+    assert "message contains a cycle" in cyclic_errors[0]["msg"]
+    assert "JSON object keys must be strings" in key_errors[0]["msg"]
+    assert "JSON numbers must be finite" in number_errors[0]["msg"]
+
+
+def test_checkpoint_refs_are_stable() -> None:
+    assert checkpoint_ref("run-current", 7, 3) == "checkpoint:run-current:7:3"
+
+
+def _event(
+    cursor: int,
+    event_id: str,
+    event_type: str,
+    *,
+    run_id: str | None,
+    payload: dict[str, Any],
+) -> StoredEvent:
+    return StoredEvent(
+        cursor,
+        EventEnvelope(
+            event_id=event_id,
+            type=event_type,
+            session_id="ses-current",
+            run_id=run_id,
+            sequence=cursor,
+            payload=payload,
+            occurred_at=datetime(2026, 7, 20, tzinfo=UTC),
+        ),
+    )
+
+
+def test_extract_sources_correlates_checkpoint_and_protects_active_state() -> None:
+    current_messages = [
+        {"role": "user", "content": "current request"},
+        {
+            "role": "assistant",
+            "content": None,
+            "tool_calls": [
+                {
+                    "id": "call-current",
+                    "type": "function",
+                    "function": {"name": "lookup", "arguments": "{}"},
+                }
+            ],
+        },
+        {
+            "role": "tool",
+            "tool_call_id": "call-current",
+            "name": "lookup",
+            "content": '{"ok":true}',
+        },
+    ]
+    checkpoint = RunCheckpoint(
+        run_id="run-current",
+        session_id="ses-current",
+        checkpoint_version=7,
+        turn=1,
+        phase=RunCheckpointPhase.READY_FOR_MODEL,
+        messages=tuple(current_messages),
+    )
+    events = (
+        _event(
+            1,
+            "evt-old-user",
+            "run.created",
+            run_id="run-old",
+            payload={"user_input": "older request"},
+        ),
+        _event(
+            2,
+            "evt-current-user",
+            "run.created",
+            run_id="run-current",
+            payload={"user_input": "current request"},
+        ),
+        _event(
+            3,
+            "evt-current-model",
+            "model.call.completed",
+            run_id="run-current",
+            payload={"finish_reason": "tool_calls"},
+        ),
+        _event(
+            4,
+            "evt-current-tool",
+            "tool.call.completed",
+            run_id="run-current",
+            payload={
+                "call_id": "call-current",
+                "tool_name": "lookup",
+                "status": "succeeded",
+                "content": '{"ok":true}',
+                "value": {"ok": True},
+                "error": None,
+            },
+        ),
+    )
+    state = _source(
+        "state:workflow:wfr-active",
+        "system",
+        '{"status":"running"}',
+    )
+
+    sources = extract_sources(
+        events,
+        checkpoint,
+        protected_event_ids={"evt-old-user"},
+        active_state_summaries=(state,),
+    )
+
+    assert tuple(source.ref for source in sources) == (
+        "evt-old-user",
+        "evt-current-user",
+        "evt-current-model",
+        "evt-current-tool",
+        "state:workflow:wfr-active",
+    )
+    assert sources[0].protected
+    assert all(source.current for source in sources[1:4])
+    assert all(source.protected for source in sources)
+    assert [
+        source.model_dump(mode="json")["message"] for source in sources[1:4]
+    ] == current_messages
+    assert sources[-1].protected
+    events[0].event.payload["user_input"] = "mutated"
+    current_messages[0]["content"] = "mutated"
+    assert sources[0].message["content"] == "older request"
+    assert sources[1].message["content"] == "current request"
+
+
+def _assistant_call(call_id: str) -> dict[str, Any]:
+    return {
+        "role": "assistant",
+        "content": None,
+        "tool_calls": [
+            {
+                "id": call_id,
+                "type": "function",
+                "function": {"name": "lookup", "arguments": "{}"},
+            }
+        ],
+    }
+
+
+def _tool_message(call_id: str, content: str) -> dict[str, Any]:
+    return {
+        "role": "tool",
+        "tool_call_id": call_id,
+        "name": "lookup",
+        "content": content,
+    }
+
+
+def _tool_event(cursor: int, event_id: str, call_id: str, content: str) -> StoredEvent:
+    return _event(
+        cursor,
+        event_id,
+        "tool.call.completed",
+        run_id="run-current",
+        payload={
+            "call_id": call_id,
+            "tool_name": "lookup",
+            "status": "succeeded",
+            "content": content,
+            "value": {"content": content},
+            "error": None,
+        },
+    )
+
+
+def test_extract_sources_consumes_repeated_tool_call_ids_in_event_order() -> None:
+    messages = (
+        {"role": "user", "content": "current request"},
+        _assistant_call("call-reused"),
+        _tool_message("call-reused", "first"),
+        _assistant_call("call-reused"),
+        _tool_message("call-reused", "second"),
+    )
+    checkpoint = RunCheckpoint(
+        run_id="run-current",
+        session_id="ses-current",
+        checkpoint_version=5,
+        turn=2,
+        phase=RunCheckpointPhase.READY_FOR_MODEL,
+        messages=messages,
+    )
+    events = (
+        _event(
+            1,
+            "evt-user",
+            "run.created",
+            run_id="run-current",
+            payload={"user_input": "current request"},
+        ),
+        _event(
+            2,
+            "evt-model-1",
+            "model.call.completed",
+            run_id="run-current",
+            payload={"finish_reason": "tool_calls"},
+        ),
+        _tool_event(3, "evt-tool-1", "call-reused", "first"),
+        _event(
+            4,
+            "evt-model-2",
+            "model.call.completed",
+            run_id="run-current",
+            payload={"finish_reason": "tool_calls"},
+        ),
+        _tool_event(5, "evt-tool-2", "call-reused", "second"),
+    )
+
+    sources = extract_sources(events, checkpoint)
+
+    assert tuple(source.ref for source in sources) == (
+        "evt-user",
+        "evt-model-1",
+        "evt-tool-1",
+        "evt-model-2",
+        "evt-tool-2",
+    )
+    assert tuple(source.event_type for source in sources) == (
+        "run.created",
+        "model.call.completed",
+        "tool.call.completed",
+        "model.call.completed",
+        "tool.call.completed",
+    )
+
+
+def test_extract_sources_handles_interleaved_and_unmatched_tool_call_ids() -> None:
+    messages = (
+        {"role": "user", "content": "current request"},
+        _assistant_call("call-a"),
+        _tool_message("call-a", "a-first"),
+        _assistant_call("call-b"),
+        _tool_message("call-b", "b"),
+        _assistant_call("call-a"),
+        _tool_message("call-a", "a-second"),
+        _assistant_call("call-missing"),
+        _tool_message("call-missing", "synthetic"),
+    )
+    checkpoint = RunCheckpoint(
+        run_id="run-current",
+        session_id="ses-current",
+        checkpoint_version=9,
+        turn=4,
+        phase=RunCheckpointPhase.READY_FOR_MODEL,
+        messages=messages,
+    )
+    events = (
+        _event(
+            1,
+            "evt-user",
+            "run.created",
+            run_id="run-current",
+            payload={"user_input": "current request"},
+        ),
+        _event(
+            2,
+            "evt-model-1",
+            "model.call.completed",
+            run_id="run-current",
+            payload={"finish_reason": "tool_calls"},
+        ),
+        _tool_event(3, "evt-tool-a1", "call-a", "a-first"),
+        _event(
+            4,
+            "evt-model-2",
+            "model.call.completed",
+            run_id="run-current",
+            payload={"finish_reason": "tool_calls"},
+        ),
+        _tool_event(5, "evt-tool-b", "call-b", "b"),
+        _event(
+            6,
+            "evt-model-3",
+            "model.call.completed",
+            run_id="run-current",
+            payload={"finish_reason": "tool_calls"},
+        ),
+        _tool_event(7, "evt-tool-a2", "call-a", "a-second"),
+        _event(
+            8,
+            "evt-model-4",
+            "model.call.completed",
+            run_id="run-current",
+            payload={"finish_reason": "tool_calls"},
+        ),
+    )
+
+    sources = extract_sources(events, checkpoint)
+
+    assert tuple(source.ref for source in sources) == (
+        "evt-user",
+        "evt-model-1",
+        "evt-tool-a1",
+        "evt-model-2",
+        "evt-tool-b",
+        "evt-model-3",
+        "evt-tool-a2",
+        "evt-model-4",
+        "checkpoint:run-current:9:8",
+    )
+    assert len({source.ref for source in sources}) == len(sources)
+    assert sources[-1].event_type == "checkpoint.message"
diff --git a/tests/unit/runtime/test_execution_descriptors.py b/tests/unit/runtime/test_execution_descriptors.py
index baf554a..e7c1ef5 100644
--- a/tests/unit/runtime/test_execution_descriptors.py
+++ b/tests/unit/runtime/test_execution_descriptors.py
@@ -1,34 +1,37 @@
 from __future__ import annotations

 import hashlib
 import json

 import pytest
 from pydantic import ValidationError

 from agent_sdk.runtime.execution import (
+    DurableAgentSpec,
     DurableWorkflowIR,
     ExecutionDescriptor,
     ExecutionPolicyDescriptor,
     ToolCapabilityDescriptor,
     WorkflowAgentDescriptor,
     WorkflowExecutionDescriptor,
 )
 from agent_sdk.runtime.models import (
     AgentSpec,
     RunSnapshot,
     RunStatus,
     SessionSnapshot,
     SessionStatus,
     TokenUsage,
+    run_created_event_matches,
 )
+from agent_sdk.context import ContextRuntimeConfig
 from agent_sdk.tools.models import ToolSpec
 from agent_sdk.workflow.models import (
     AgentNode,
     WorkflowDefinition,
     WorkflowEdge,
     WorkflowIR,
     WorkflowNodeSnapshot,
     WorkflowNodeStatus,
     WorkflowRunSnapshot,
     WorkflowRunStatus,
@@ -167,20 +170,151 @@ def test_execution_descriptor_is_immutable_and_revalidates_hashes() -> None:
     changed = ExecutionDescriptor.create(
         agent=changed_agent,
         messages=({"role": "user", "content": "hello"},),
         tools=(capability,),
         policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
     )
     assert changed.agent_hash != descriptor.agent_hash
     assert changed.descriptor_hash != descriptor.descriptor_hash


+def test_agent_prompt_and_context_fields_are_defaulted_and_validated() -> None:
+    agent = AgentSpec(name="coder", model="openai/test")
+
+    assert agent.prompt_profile == "general"
+    assert agent.system_prompt is None
+    assert agent.skills == ()
+    assert agent.context == ContextRuntimeConfig()
+    with pytest.raises(ValidationError, match="skills"):
+        AgentSpec(name="coder", model="openai/test", skills=("",))
+    with pytest.raises(ValidationError, match="skills"):
+        AgentSpec(name="coder", model="openai/test", skills=("demo", "demo"))
+
+
+def test_execution_descriptor_hash_covers_prompt_skills_and_context() -> None:
+    def descriptor(agent: AgentSpec) -> ExecutionDescriptor:
+        return ExecutionDescriptor.create(
+            agent=agent,
+            messages=({"role": "user", "content": "hello"},),
+            tools=(),
+            policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
+        )
+
+    base = descriptor(AgentSpec(name="coder", model="openai/test"))
+    changed = (
+        AgentSpec(name="coder", model="openai/test", prompt_profile="coding"),
+        AgentSpec(
+            name="coder",
+            model="openai/test",
+            system_prompt="Application constraint.",
+        ),
+        AgentSpec(name="coder", model="openai/test", skills=("coding-demo",)),
+        AgentSpec(
+            name="coder",
+            model="openai/test",
+            context=ContextRuntimeConfig(model_window=64_000),
+        ),
+    )
+
+    for agent in changed:
+        current = descriptor(agent)
+        assert current.agent_hash != base.agent_hash
+        assert current.descriptor_hash != base.descriptor_hash
+
+
+def test_legacy_durable_agent_and_descriptor_load_prompt_defaults() -> None:
+    descriptor = ExecutionDescriptor.create(
+        agent=AgentSpec(name="coder", model="openai/test"),
+        messages=({"role": "user", "content": "hello"},),
+        tools=(),
+        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
+    )
+    legacy = descriptor.model_dump(mode="json")
+    for field in ("prompt_profile", "system_prompt", "skills", "context"):
+        legacy["agent"].pop(field)
+    legacy["agent_hash"] = _canonical_hash(legacy["agent"])
+    legacy["descriptor_hash"] = _canonical_hash(
+        {key: value for key, value in legacy.items() if key != "descriptor_hash"}
+    )
+
+    restored_agent = DurableAgentSpec.model_validate(legacy["agent"])
+    restored = ExecutionDescriptor.model_validate(legacy)
+
+    assert restored_agent.prompt_profile == "general"
+    assert restored_agent.system_prompt is None
+    assert restored_agent.skills == ()
+    assert restored_agent.context == ContextRuntimeConfig()
+    assert restored.agent == restored_agent
+    assert restored.agent_hash == _canonical_hash(
+        restored.agent.model_dump(mode="json")
+    )
+    assert restored.descriptor_hash == _canonical_hash(
+        {
+            key: value
+            for key, value in restored.model_dump(mode="json").items()
+            if key != "descriptor_hash"
+        }
+    )
+
+
+def test_schema_v1_run_creation_authenticates_genuine_legacy_descriptor_hashes() -> None:
+    descriptor = ExecutionDescriptor.create(
+        agent=AgentSpec(name="coder", model="openai/test"),
+        messages=({"role": "user", "content": "hello"},),
+        tools=(),
+        policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
+    )
+    raw_descriptor = descriptor.model_dump(mode="json")
+    for field in ("prompt_profile", "system_prompt", "skills", "context"):
+        raw_descriptor["agent"].pop(field)
+    raw_descriptor["agent_hash"] = _canonical_hash(raw_descriptor["agent"])
+    raw_descriptor["descriptor_hash"] = _canonical_hash(
+        {
+            key: value
+            for key, value in raw_descriptor.items()
+            if key != "descriptor_hash"
+        }
+    )
+    raw_v1 = RunSnapshot(
+        run_id="run_r2",
+        session_id="ses_r2",
+        agent_revision="coder:1",
+        status=RunStatus.CREATED,
+        user_input="hello",
+        execution_compatibility="current",
+        execution_descriptor=descriptor,
+    ).model_dump(mode="json")
+    raw_v1["execution_descriptor"] = raw_descriptor
+    upgraded = RunSnapshot.model_validate(raw_v1)
+
+    assert run_created_event_matches(
+        upgraded,
+        raw_v1,
+        schema_version=1,
+    )
+
+    wrong_agent_hash = json.loads(json.dumps(raw_v1))
+    wrong_agent_hash["execution_descriptor"]["agent_hash"] = "a" * 64
+    assert not run_created_event_matches(
+        upgraded,
+        wrong_agent_hash,
+        schema_version=1,
+    )
+    wrong_descriptor_hash = json.loads(json.dumps(raw_v1))
+    wrong_descriptor_hash["execution_descriptor"]["descriptor_hash"] = "d" * 64
+    assert not run_created_event_matches(
+        upgraded,
+        wrong_descriptor_hash,
+        schema_version=1,
+    )
+
+
 def test_execution_descriptor_rejects_rehashed_noncanonical_agent() -> None:
     descriptor = ExecutionDescriptor.create(
         agent=AgentSpec(name="coder", model="openai/test"),
         messages=({"role": "user", "content": "hello"},),
         tools=(),
         policy=ExecutionPolicyDescriptor.create(permission_default="ask"),
     )
     tampered = descriptor.model_dump(mode="json")
     tampered["agent"]["revision"] = 2
     tampered["agent_hash"] = _canonical_hash(tampered["agent"])
diff --git a/tests/unit/runtime/test_reconciliation_models.py b/tests/unit/runtime/test_reconciliation_models.py
index c0493fd..0c41538 100644
--- a/tests/unit/runtime/test_reconciliation_models.py
+++ b/tests/unit/runtime/test_reconciliation_models.py
@@ -1,18 +1,21 @@
-from importlib.util import find_spec
+import json
 from datetime import UTC, datetime, timedelta, timezone
+from importlib.util import find_spec
 from typing import Any

 import agent_sdk.runtime.reconciliation as reconciliation
 import pytest
 from pydantic import ValidationError

+from agent_sdk.errors import AgentSDKError
+from agent_sdk.models.litellm_gateway import ModelRequest
 from agent_sdk.runtime.models import TokenUsage
 from agent_sdk.tools.models import ToolResult


 def test_reconciliation_module_exists() -> None:
     assert find_spec("agent_sdk.runtime.reconciliation") is not None


 def test_recovery_enums_have_the_persisted_values() -> None:
     assert tuple(item.value for item in reconciliation.ExternalOperationKind) == (
@@ -77,20 +80,401 @@ def _tool_operation(**updates: Any) -> Any:
         "turn": 1,
         "request_fingerprint": "sha256:tool",
         "lease_generation": 1,
         "status": reconciliation.ExternalOperationStatus.STARTED,
         "tool_identity": "tool:search",
     }
     values.update(updates)
     return reconciliation.ToolCallOperation(**values)


+def test_model_request_payload_is_canonical_and_round_trips_exactly() -> None:
+    request = ModelRequest(
+        model="provider:model",
+        messages=(
+            {"role": "system", "content": "general"},
+            {"role": "user", "content": "ship"},
+        ),
+        tools=(
+            {
+                "type": "function",
+                "function": {
+                    "name": "lookup",
+                    "parameters": {"type": "object"},
+                },
+            },
+        ),
+        params={"temperature": 0, "metadata": {"labels": ["release"]}},
+        purpose="agent_loop",
+    )
+
+    payload = reconciliation.serialize_model_request(request)
+
+    assert payload == {
+        "model": "provider:model",
+        "messages": [
+            {"role": "system", "content": "general"},
+            {"role": "user", "content": "ship"},
+        ],
+        "tools": [
+            {
+                "type": "function",
+                "function": {
+                    "name": "lookup",
+                    "parameters": {"type": "object"},
+                },
+            }
+        ],
+        "params": {
+            "temperature": 0,
+            "metadata": {"labels": ["release"]},
+        },
+        "purpose": "agent_loop",
+    }
+    assert reconciliation.deserialize_model_request(payload) == request
+    assert (
+        reconciliation.model_request_fingerprint(request)
+        == reconciliation.model_request_fingerprint(
+            reconciliation.deserialize_model_request(payload)
+        )
+    )
+
+
+@pytest.mark.parametrize(
+    "payload",
+    [
+        {
+            "model": "provider:model",
+            "messages": [],
+            "tools": [],
+            "params": {},
+            "purpose": None,
+            "extra": True,
+        },
+        {
+            "model": "provider:model",
+            "messages": {},
+            "tools": [],
+            "params": {},
+            "purpose": None,
+        },
+        {
+            "model": "provider:model",
+            "messages": [],
+            "tools": [],
+            "params": {"temperature": float("nan")},
+            "purpose": None,
+        },
+    ],
+)
+def test_stored_model_request_rejects_noncanonical_payloads(
+    payload: dict[str, Any],
+) -> None:
+    with pytest.raises(AgentSDKError, match="stored model request is invalid"):
+        reconciliation.deserialize_model_request(payload)
+
+
+def _stored_request_payload(
+    *,
+    messages: list[dict[str, Any]],
+    tools: list[dict[str, Any]] | None = None,
+) -> dict[str, Any]:
+    return {
+        "model": "provider:model",
+        "messages": messages,
+        "tools": [] if tools is None else tools,
+        "params": {},
+        "purpose": "agent_loop",
+    }
+
+
+@pytest.mark.parametrize(
+    "payload",
+    [
+        pytest.param(
+            _stored_request_payload(messages=[]),
+            id="empty-messages",
+        ),
+        pytest.param(
+            _stored_request_payload(messages=[{}]),
+            id="empty-message",
+        ),
+        pytest.param(
+            _stored_request_payload(messages=[{"content": "missing role"}]),
+            id="missing-role",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[{"role": "bogus", "content": "invalid"}]
+            ),
+            id="invalid-role",
+        ),
+        pytest.param(
+            _stored_request_payload(messages=[{"role": "user"}]),
+            id="missing-content",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[{"role": "tool", "content": "result"}]
+            ),
+            id="tool-missing-call-id",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[
+                    {
+                        "role": "tool",
+                        "content": "result",
+                        "tool_call_id": "",
+                    }
+                ]
+            ),
+            id="tool-empty-call-id",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[
+                    {
+                        "role": "assistant",
+                        "content": None,
+                        "tool_calls": [],
+                    }
+                ]
+            ),
+            id="assistant-empty-tool-calls",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[
+                    {
+                        "role": "assistant",
+                        "content": None,
+                        "tool_calls": [
+                            {
+                                "id": "call_1",
+                                "type": "custom",
+                                "function": {
+                                    "name": "lookup",
+                                    "arguments": "{}",
+                                },
+                            }
+                        ],
+                    }
+                ]
+            ),
+            id="assistant-tool-call-type",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[
+                    {
+                        "role": "assistant",
+                        "content": None,
+                        "tool_calls": [
+                            {
+                                "id": "",
+                                "type": "function",
+                                "function": {
+                                    "name": "lookup",
+                                    "arguments": "{}",
+                                },
+                            }
+                        ],
+                    }
+                ]
+            ),
+            id="assistant-tool-call-empty-id",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[
+                    {
+                        "role": "assistant",
+                        "content": None,
+                        "tool_calls": [
+                            {
+                                "id": "call_1",
+                                "type": "function",
+                                "function": {
+                                    "name": 7,
+                                    "arguments": {},
+                                },
+                            }
+                        ],
+                    }
+                ]
+            ),
+            id="assistant-tool-call-function-fields",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[{"role": "user", "content": "run"}],
+                tools=[{}],
+            ),
+            id="empty-tool-schema",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[{"role": "user", "content": "run"}],
+                tools=[
+                    {
+                        "type": "function",
+                        "function": {
+                            "name": "lookup",
+                            "parameters": {},
+                        },
+                        "extra": True,
+                    }
+                ],
+            ),
+            id="tool-schema-extra",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[{"role": "user", "content": "run"}],
+                tools=[
+                    {
+                        "type": "custom",
+                        "function": {
+                            "name": "lookup",
+                            "parameters": {},
+                        },
+                    }
+                ],
+            ),
+            id="tool-schema-type",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[{"role": "user", "content": "run"}],
+                tools=[
+                    {
+                        "type": "function",
+                        "function": {
+                            "name": "",
+                            "parameters": {},
+                        },
+                    }
+                ],
+            ),
+            id="tool-schema-empty-name",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[{"role": "user", "content": "run"}],
+                tools=[
+                    {
+                        "type": "function",
+                        "function": {
+                            "name": "lookup",
+                            "parameters": [],
+                        },
+                    }
+                ],
+            ),
+            id="tool-schema-parameters-not-object",
+        ),
+        pytest.param(
+            _stored_request_payload(
+                messages=[{"role": "user", "content": {"bad": object()}}],
+            ),
+            id="nested-non-json",
+        ),
+    ],
+)
+def test_stored_model_request_rejects_invalid_message_and_tool_shapes(
+    payload: dict[str, Any],
+) -> None:
+    with pytest.raises(AgentSDKError, match="stored model request is invalid"):
+        reconciliation.deserialize_model_request(payload)
+
+
+def test_stored_model_request_accepts_runtime_message_and_tool_shapes() -> None:
+    payload = _stored_request_payload(
+        messages=[
+            {"role": "system", "content": "general", "name": "profile"},
+            {"role": "user", "content": "run", "name": "operator"},
+            {
+                "role": "assistant",
+                "content": None,
+                "name": "agent",
+                "tool_calls": [
+                    {
+                        "id": "call_1",
+                        "type": "function",
+                        "function": {
+                            "name": "lookup",
+                            "arguments": '{"query":"context"}',
+                        },
+                    }
+                ],
+            },
+            {
+                "role": "tool",
+                "tool_call_id": "call_1",
+                "name": "lookup",
+                "content": "result",
+            },
+        ],
+        tools=[
+            {
+                "type": "function",
+                "function": {
+                    "name": "lookup",
+                    "description": "Look up context",
+                    "parameters": {
+                        "type": "object",
+                        "properties": {"query": {"type": "string"}},
+                    },
+                },
+            }
+        ],
+    )
+
+    request = reconciliation.deserialize_model_request(payload)
+
+    assert reconciliation.serialize_model_request(request) == payload
+
+
+def test_model_operation_accepts_legacy_records_and_rejects_prepared_mismatch() -> None:
+    legacy = {
+        "operation_id": "op_model",
+        "operation_kind": "model_call",
+        "session_id": "ses_1",
+        "run_id": "run_1",
+        "turn": 0,
+        "request_fingerprint": "sha256:model",
+        "lease_generation": 1,
+        "status": "started",
+        "provider_identity": "provider:model",
+        "tool_identity": None,
+        "outcome": None,
+        "recovery_metadata": {},
+    }
+    assert reconciliation.ModelCallOperation.model_validate_json(
+        json.dumps(legacy)
+    ) == _model_operation()
+
+    request = ModelRequest(
+        model="provider:model",
+        messages=({"role": "user", "content": "ship"},),
+    )
+    prepared = reconciliation.serialize_model_request(request)
+    with pytest.raises(ValidationError, match="fingerprint mismatch"):
+        _model_operation(
+            request_fingerprint="wrong",
+            context_view_id="view_1",
+            prompt_manifest_id="pmf_1",
+            prepared_request=prepared,
+        )
+
+
 def test_external_operation_models_are_strict_frozen_detached_and_exact() -> None:
     outcome = {"response": {"parts": ["one"]}}
     metadata = {"query": {"supported": True}}
     operation = _model_operation(
         status=reconciliation.ExternalOperationStatus.COMPLETED,
         outcome=outcome,
         recovery_metadata=metadata,
     )
     outcome["response"]["parts"].append("caller mutation")
     metadata["query"]["supported"] = False
diff --git a/tests/unit/test_core_config.py b/tests/unit/test_core_config.py
index cfcd865..07728a8 100644
--- a/tests/unit/test_core_config.py
+++ b/tests/unit/test_core_config.py
@@ -4,35 +4,38 @@ import pytest

 from agent_sdk.config import AgentSDKConfig, CaptureLevel
 from agent_sdk.errors import AgentSDKError, ErrorCode
 from agent_sdk.ids import new_id
 from agent_sdk.permissions import PermissionRule


 def test_core_contracts_are_stable() -> None:
     config = AgentSDKConfig(database_path=Path("state.db"))
     assert config.capture_level is CaptureLevel.PREVIEW
+    assert config.skill_roots == ()
     assert new_id("run").startswith("run_")
     with pytest.raises(Exception):
         AgentSDKConfig(database_path=Path("x.db"), unknown=True)
     error = AgentSDKError(ErrorCode.INVALID_STATE, "bad state", retryable=False)
     assert error.to_dict()["code"] == "invalid_state"


 def test_permission_rules_round_trip_through_config_json(tmp_path: Path) -> None:
     config = AgentSDKConfig(
         database_path=tmp_path / "state.db",
         permission_default="deny",
         permission_rules=(
             PermissionRule(
                 outcome="allow",
                 tool="bash",
                 path_prefix=tmp_path / "workspace",
                 command_prefix=("git", "status"),
             ),
         ),
+        skill_roots=(tmp_path / "skills",),
     )

     restored = AgentSDKConfig.model_validate_json(config.model_dump_json())

     assert restored == config
     assert restored.permission_rules[0].command_prefix == ("git", "status")
+    assert restored.skill_roots == (tmp_path / "skills",)
