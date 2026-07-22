# v0.1 R5 Task 1 — Normalize Events into Trace Stages

## Outcome

Implemented normalized, sanitized trace stages and the public `sdk.trace` facade.
Run and Workflow roots are read at one bounded high-water cursor, include related
Run/Child, Context, message, and evaluation evidence, and are revalidated before
return. Stages are table-driven, ordered by first evidence, correlated by stable
entity/operation IDs, and expose status, duration, parent, usage, optional cost,
and bounded evidence references without copying event payload text or errors.

Runtime stage events use schema v2 where old recovery proofs require exact v1
payloads. One central contract validates all v2 references against durable
operations and projects them to the historical v1 shape for existing recovery
certification. Historical v1 evidence remains exact-compatible; mixed v1/v2
recovery reconstructs the active step from the certified model operation, and
forged v2 references fail closed.

## RED

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/observability/test_stage_projection.py tests/integration/observability/test_trace_timeline.py -q
```

Initial result: collection failed twice because `TraceStageKind` and the trace
projector did not exist.

Focused follow-up REDs established:

- legacy v1 empty Step/Model payloads initially failed projection;
- the real v1 Tool recovery `operation` alias initially failed projection;
- Context initially parented to Run instead of its Model;
- Workflow node Runs initially had no Workflow-node parent;
- a mixed legacy-v1/v2 recovery cycle newly failed before active-step recovery.

Each focused RED was observed before its bounded implementation.

## GREEN / verification

Final focused, integration, contract, and static gate:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/observability/test_stage_projection.py tests/unit/runtime/test_stage_event_contracts.py tests/integration/observability/test_trace_timeline.py tests/integration/observability -q
.\.venv\Scripts\python.exe -m mypy --strict src/agent_sdk/observability src/agent_sdk/models/litellm_gateway.py src/agent_sdk/runtime/models.py
.\.venv\Scripts\python.exe -m ruff check [changed Task 1 sources and tests]
git diff --check
```

Result: `13 passed in 3.00s`; strict mypy passed for 8 source files; Ruff and
diff-check passed. No temporary diagnostic output remains.

The recovery contract tests cover legacy v1 exact preservation, valid v2-to-v1
normalization, forged v2 reference rejection, and a v2 terminal following a
certified legacy v1 Step. A baseline-green end-to-end provider resend case passes
under both baseline v1 and current v2 emission (`1 passed` each).

Provider recovery regression comparison, same 106-test file and command:

- baseline `9c09380`: `68 passed, 38 failed`;
- current Task 1: `88 passed, 18 failed`;
- current failures are all members of the baseline failure set; no new failure.

The exact mixed provider-to-Tool recovery case that exposed lost Step correlation
passes after the central compatibility fix: `1 passed`.

Broader runtime/recovery/tool/workflow/context gate: `219 passed, 8 failed`.
All eight failures reproduce at baseline: five recovery assertions expect the
provider message list without the already-injected ContextMiddleware system
prompt; two Tool assertions have the same expectation; one ninth-Tool-call test
exceeds its one-second timeout. Task 1 does not change prompt composition or the
Tool-step limit timing.

## Public behavior

- `sdk.trace.timeline(root_id)` returns sanitized `TraceTimeline` history for a
  Run or Workflow execution tree.
- `sdk.trace.subscribe(...)` delegates to the existing raw cursor subscription.
- Run, Step, Context, Model, Tool, permission, Workflow, Workflow node, Child,
  message, evaluation, and recovery stage kinds are supported.
- completed/failed/denied/timed-out/interrupted/waiting/running truth is retained;
  missing starts and clock skew do not fabricate duration.
- provider/LiteLLM finite non-negative cost is captured as `cost_usd`; invalid or
  absent cost remains `None` and does not affect model-call success.

## Attention points

- The 18 remaining provider-recovery failures and eight broader-suite failures
  are baseline debt documented above, not hidden with production fallbacks.
- Historical schema-v1 Step/Model correlation is deterministic from durable Run
  order and certified operation turns; schema-v2 evidence always requires exact
  stable IDs and rejects tampering.

## Independent-review fixes

Resolved all six Important findings and Minor 1 from the independent review;
Minor 2 is covered by real public/runtime paths rather than projector-only
fixtures:

- the stable read now authenticates selected `run.created` events, validates a
  bounded post-high-water tail, retries for new Child/workflow Runs and selected
  snapshot transitions, and fails closed on invalid selected tail schemas;
- known stage-event schemas are allow-listed, and `model.usage.reported` now
  participates in first/last cursor and bounded evidence ordering;
- hashed recovery permission transitions remain explicit schema v1, while real
  Tool starts carry a validated v2 Step reference;
- authenticated child Run lifecycle evidence deterministically projects public
  Child stages without adding a second durable lifecycle grammar;
- cost parsing treats overflow, non-finite, negative, and bool values as invalid
  and continues to the next valid provider fallback;
- `ChildUsage` is constructed from token fields so trace-only cost data cannot
  leak into or break the public Child result contract.

The expanded regression gate passed `315 passed in 41.82s` across observability,
stage contracts, real Child paths, complete Tool recovery, and compaction. A
separate observability tail-schema gate passed `83 passed in 4.42s`. The two
access-denial recovery regressions exposed during expansion were fixed by
preserving the schema-v1 `ToolResult` terminal payload while using the v2 Tool
start for Step parenting; both memory and SQLite cases pass.

Full `mypy --strict src/agent_sdk` passed for 100 source files. Ruff, debug-output
scan, and `git diff --check` passed. Baseline comparisons are unchanged:
provider recovery remains `88 passed, 18 failed`, and permissioned Tool remains
`30 passed, 3 failed`; those exact failure sets are the pre-recorded baseline
prompt-composition assertions and one one-second timeout described above.

## Re-review fixes

Resolved the remaining Important and Minor findings with strict RED/GREEN tests.
The focused RED showed `first_cursor == 2` for terminal-only Model usage and a
sanitized INTERNAL from a real completed recovery permission timeline. The same
focused pair is now GREEN (`2 passed in 3.61s`).

Exact schema-v1 recovery hash-reference payloads remain byte-for-byte logical
mappings; the projector validates bounded lowercase SHA-256 references and uses
the request digest as the deterministic Permission entity ID for both requested
and resolved evidence. The real path verifies a COMPLETED public Permission
stage with both evidence records while the recovery handler is active. The same
strict extractor also reads the preceding real Tool-recovery operation hash so
the required public path reaches the Permission lifecycle without rewriting any
durable event.

When Model usage precedes a terminal event with no start, its cursor and event ID
now become the stage's first bounded evidence while `started_at` remains absent.
Final verification: complete observability `84 passed in 5.00s`; related real
recovery-permission nodes `6 passed in 5.03s`; strict mypy passed for 100 source
files; Ruff and `git diff --check` passed.
