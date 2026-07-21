# Agent SDK v0.1 R5 Task 3 Report

## Status

Contract-lock PASS. The existing analytics, attribution, and Session deletion
implementation already satisfies the Task 3 contract. No production code was
changed; the task adds one Memory/SQLite integration contract test. Independent
review fixes also remain test/report-only.

## Scope Locked

The contract exercises only the existing v0.1 cross-Run metrics:

- `success_rate = pass_evaluations / known_evaluations`;
- `tool_failures = non_succeeded_tool_results`;
- `tool_failure_rate = non_succeeded_tool_results / known_tool_results`.

It does not add Tool ranking, statistical confidence, multi-dimensional failure
taxonomy, generated insights, or causal analysis.

## Test-First Evidence

The test was created before any production change at
`tests/integration/analytics/test_v01_analysis_contract.py`.

### Literal first run

```text
2 failed in 4.20s
```

Both Memory and SQLite failed because the scripted provider selected the first
user message from the complete Session history. The later failed-stage Run was
therefore executed with an earlier Run's script and completed. This was a test
fixture defect, not a product contract failure.

### Fixture corrections and diagnostic REDs

After selecting the latest user message, both backends again failed because the
fixture treated a Tool message from an earlier Run in the same Session as the
current Run's Tool result. The observed `always_fail` count was consequently zero.
The fixture was narrowed to Tool messages after the current Run's last user
message.

The analytics and attribution assertions then passed through the failed-evidence
check, where both backends failed because the test used
`query_events(limit=100)` from global cursor zero. Four full runtime Runs and their
evaluations had already consumed about 88 cursors, so the failed Run's later
`step.started` and `step.failed` evidence fell outside that page. Systematic
comparison showed:

- failure stage: `TraceStageKind.STEP` with status `FAILED`;
- evidence types: real `step.started` and `step.failed` events;
- both evidence envelopes use the failed root Run id;
- the evidence is present in the stable public per-Run timeline.

The assertion was corrected to use `sdk.queries.timeline(failed_run_id)`. No Task 2
evidence boundary defect and no production consistency defect were found.

### Valid contract GREEN

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; $env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/integration/analytics/test_v01_analysis_contract.py -q
```

Result: `2 passed in 4.23s`.

## Independent Review RED/GREEN

The first review reported `Critical 0 / Important 2 / Minor 0` and correctly
rejected two test-contract weaknesses.

### Complete fixed-high-water pagination

The review regression first produced `2 failed`: on both backends the single
global page had `next_cursor=100` while `as_of_cursor=108`. A test helper now
captures the first page's `as_of_cursor`, reads 17-event pages until
`next_cursor == as_of_cursor`, requires every page to keep the same high-water,
and requires each page to advance. Metric evidence, deleted-Session event absence,
and remaining evaluation evidence all use this complete stable event set.

### Runtime-valid unused Tool boundary

The lifecycle regression first produced `2 failed`: the old unused Tool fixture's
timeline ended in `tool.call.completed`, after the existing `run.completed`.

The replacement uses a real runtime boundary on both Stores:

1. the registered `unused_probe` handler succeeds and its real
   `tool.call.completed` is committed by Run progress;
2. a test Store proxy rejects the next Context commit that would reference that
   completion id;
3. the execution task exits and releases its lease while the durable Run remains
   `RUNNING`;
4. the proxy stops rejecting commits and public `recovery.scan()` writes the
   authenticated `run.interrupted` transition.

The contract asserts continuous Run event sequences, Tool completion before the
terminal cursor, `run.interrupted` as the timeline tail, an `INTERRUPTED` snapshot,
no later Context reference, and `unused` attribution backed by the real completion
event id. The focused contract then passed: `2 passed in 5.09s`.

## Contract Coverage

On both `InMemoryStore` and `SQLiteStore`, the test uses `AgentSDK` public APIs to
create two Sessions, four completed Runs with real Tool executions, four real
evaluations, one failed Run, and one runtime-interrupted Run with a real successful
Tool completion. It verifies:

- overall success `3/4` and exact-evaluator success `2/3`;
- after deleting one Session, overall and exact-evaluator success both become
  `1/2`;
- exact Tool-name counts and rates for `lookup` and `always_fail`;
- `always_fail` has two known failed samples and rate `1.0`, then one known failed
  retained sample and rate `1.0` after deletion;
- malformed matching evaluation/Tool facts increase `missing_count` without
  changing known denominators;
- metric evidence ids resolve to real `evaluation.completed` or
  `tool.call.completed` events;
- public Session deletion removes that Session's events and makes its Session and
  Run snapshots return `NOT_FOUND`;
- the deleted Session no longer contributes analytics samples or evidence;
- an unreferenced persisted Tool result on a real Run is attributed `unused`;
- a real failed Run selects its failed STEP stage and real evidence ids.

All cross-Run Tool counts use actual runtime Tool outcomes. The deterministic
unused-result fact is a real runtime Tool completion committed before a real
`run.interrupted` terminal transition; the failed Context boundary prevents a
later same-Run Context View from consuming it.

## Verification

```text
New contract + analytics base:                 19 passed in 4.68s
Analytics + evaluation + attribution integ.:   56 passed in 6.56s
Ruff:                                           All checks passed!
Strict mypy src/agent_sdk/analytics:            no issues in 3 source files
git diff --check:                               exit 0
```

All pytest commands used the project `.venv`, disabled plugin autoload, and loaded
`pytest_asyncio.plugin` explicitly. No `uv` command was used.

## Files

- `tests/integration/analytics/test_v01_analysis_contract.py`: new Memory/SQLite
  public-path cross-Run, evidence, attribution, and deletion contract.
- `.superpowers/sdd/v01-r5-task3-report.md`: this report.

No files under `src/` were modified.

## Self-review

- The formulas and existing `sample_count`, `missing_count`, and method strings are
  asserted without changing their implementation.
- Evaluator and Tool filters use exact public names.
- Evidence assertions resolve returned ids back to persisted events rather than
  accepting synthetic ids.
- Complete event helpers paginate to one fixed high-water and reject cursor or
  high-water drift.
- Session deletion is performed through `sessions.close` and `sessions.delete`;
  event and snapshot absence is checked through public APIs.
- The failed-stage evidence assertion uses the complete stable per-Run timeline,
  not an accidentally truncated global query page.
- The unused Tool fixture preserves lifecycle order: completion precedes the
  authenticated interrupted terminal, which remains the timeline tail.
- Existing Task 1 stable scans and Task 2 attribution semantics are reused.
- No unrelated prompt/recovery timeout debt was touched.

## Concerns

None. The review REDs exposed contract-test weaknesses, not production failures;
both were corrected without changing production code.
