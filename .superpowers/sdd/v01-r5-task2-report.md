# Agent SDK v0.1 R5 Task 2 Report

## Status

Implemented deterministic, evidence-linked single-Run attribution on top of the
Task 1 Trace stable-read path. Attribution performs no model call and describes
contribution/correlation only.

## TDD Evidence

### Baseline

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; $env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/observability tests/integration/observability -q
```

Result before Task 2 changes: `84 passed in 4.84s`.

### Initial RED

After creating `tests/unit/observability/test_attribution.py` with the required
successful Tool consumption/unused, denial/failure, Child failure/consumption,
Context fallback, Workflow loop limit, repeated Tool failure, interrupted external
work, and evaluation verdict fixtures:

```powershell
.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/observability/test_attribution.py -q
```

Result: collection failed with
`ImportError: cannot import name 'AttributionContributor' from 'agent_sdk.observability'`.
This was the expected missing-contract/projector failure.

### Integration REDs

The real SDK Tool/Context integration first failed with:

```text
AttributeError: 'TraceAPI' object has no attribute 'attribution'
```

After public API wiring, a real Prompt Manifest evidence assertion first failed
because the Context contributor contained only the Context View event id. The root
cause was that `TraceService._select` did not select the referenced
`prompt.manifest.created` fact. The shared stable selection path now selects it and
the Context contributor preserves its real event id.

A real Workflow-node Run integration first failed with `StopIteration` because no
Workflow contributor was present. The root cause was that Workflow node events bind
the Run in `payload.run_id`, not the envelope `run_id`. The shared selector now
recognizes that existing relation.

### Final GREEN

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; $env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/observability/test_attribution.py tests/integration/observability/test_attribution_evidence.py -q
```

Result: `11 passed in 3.14s`.

```powershell
.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/observability tests/integration/observability -q
```

Result: `95 passed in 4.85s`.

```powershell
.\.venv\Scripts\python.exe -m ruff check src/agent_sdk/observability src/agent_sdk/api.py src/agent_sdk/__init__.py tests/unit/observability tests/integration/observability
.\.venv\Scripts\python.exe -m mypy --strict src/agent_sdk/observability src/agent_sdk/api.py src/agent_sdk/__init__.py
```

Results: `All checks passed!` and
`Success: no issues found in 9 source files`.

```powershell
git diff --check
```

Result: exit `0`; Git emitted only the repository's Windows LF-to-CRLF notices.

## Files

- `src/agent_sdk/observability/attribution.py`: deterministic joins, failure choice,
  dispositions, fixed hints, and evidence ordering.
- `src/agent_sdk/observability/models.py`: public Attribution models.
- `src/agent_sdk/observability/trace.py`: shared stable load used by timeline and
  attribution; Context/Manifest and Workflow-node evidence selection.
- `src/agent_sdk/observability/__init__.py`: observability exports.
- `src/agent_sdk/api.py`: lifecycle-admitted `TraceAPI.attribution`.
- `src/agent_sdk/__init__.py`: top-level public Attribution model exports.
- `tests/unit/observability/test_attribution.py`: nine deterministic rule tests
  covering all eight required fixture categories plus parent Context consumption of
  a Child message.
- `tests/integration/observability/test_attribution_evidence.py`: real Tool result to
  later Context/Prompt Manifest evidence and real Workflow-node evidence.

## Self-review

- Reuses `TraceService._load/_select/_stable`; no second tree reader or high-water.
- A completed root Run always has `failure=None`; failed/interrupted roots choose the
  first terminal failing stage by terminal cursor, falling back to the root terminal
  stage only when needed.
- Tool results are consumed only by a later same-Run Context reference. Child results
  or messages are consumed only by a later parent Context reference.
- Model outputs before a later request are `consumed`; the final successful root
  Model output is `terminal`. Other records remain supporting unless selected as the
  terminal failure.
- Contributor order follows first evidence cursor. Every returned evidence id is
  filtered through the selected event set and ordered by cursor; no evidence id is
  synthesized.
- Hints use one fixed package summary per code and are deduplicated by code.
- Explicit evaluation `pass`, `fail`, and `unknown` are preserved without inference.
- No LLM call, cross-Run ranking, multi-dimensional taxonomy, attribution confidence,
  generated insight, or causal claim was added.
- The attribution module remains one distinct capability file from the brief; its
  extraction, join, failure, hint, and ordering rules are separated into pure helper
  functions rather than additional out-of-scope modules.

## Concerns

None. The pre-R5 prompt/recovery/one-second-timeout debts were not encountered by the
Task 2 commands and were not modified.

## Independent Review Fix

The first independent review reported `Critical 0 / Important 4 / Minor 1` and did
not approve the task. All five findings are addressed in the independent follow-up
commit.

### Review RED evidence

- The new real bounded-loop Workflow integration initially produced
  `2 passed, 1 failed`: attribution for the public Workflow-node Run had no
  `workflow_loop_limit` hint.
- The new unit regressions exposed four independent failures in the first run:
  a future Child message was marked consumed, a completed Child's final Model was
  supporting, oversized Manifest evidence escaped publicly, and public evidence
  models accepted a 257-byte id. A further historical-event regression test showed
  that simply rejecting such an event would break stage projection.

### Root causes and fixes

- Run-root trace loading did not authenticate or load `workflow_run_id`. It now
  validates the bound Workflow snapshot and selects its control/failure envelope
  without expanding unrelated Workflow node Runs.
- Child message joins ignored recipient direction and message time. Precomputed
  route and Context-consumer indexes now require child-to-parent direction and a
  strictly later parent Context reference.
- Evidence element bounds existed only on some enclosing fields. A shared 256-byte
  evidence-id type now covers Trace stages, contributors, failures, and hints;
  oversized historical event ids remain projectable but are omitted as evidence.
- Final successful Model disposition was tied to the root. Precomputed per-Run final
  Model and completed-Run indexes now make each completed Child's final Model
  terminal, while the Child contributor independently records parent consumption.
- The same indexes remove repeated Model/event/Context scans from the projector.

### Review-fix GREEN evidence

```text
Focused attribution unit + integration: 17 passed in 3.93s
All observability unit + integration:     101 passed in 6.21s
Workflow program + control execution:     18 passed in 5.69s
Ruff:                                     All checks passed!
Strict mypy:                              no issues in 9 source files
git diff --check:                         exit 0 (line-ending notices only)
```

The broader Workflow comparison (`test_program.py`, `test_control_execution.py`,
and `test_control_recovery.py`) is `19 passed, 5 failed` both on the review-fix
worktree and an isolated detached `ade18c4` baseline. The same five pre-existing
recovery tests fail with `related terminal run recovery evidence changed after
certification`; therefore this review fix introduces no Workflow regression delta.

## Second Re-review Fix

The second independent review reported `Critical 0 / Important 2 / Minor 1` and
identified three remaining narrow issues. The follow-up stayed within Task 2.

### RED evidence

The new public two-node Workflow test, oversized Tool terminal test, and
interruption-index test initially produced `3 failed, 17 passed`:

- attribution for the first node Run also exposed the second node contributor;
- a 257-byte Tool completion id referenced by a later Context was classified
  `unused` because internal terminal parsing fell back to the public start evidence;
- two interruptions iterated the complete stage collection twice.

A separate tampered Workflow snapshot test also initially failed because a matching
node id with the wrong Run id was accepted by the public attribution API.

### Fixes

- Run-bound Workflow selection keeps Workflow-wide control/failure envelope facts,
  but admits `workflow.node.*` facts only when `payload.run_id` belongs to the
  selected Run tree. Binding authentication now requires both node id and Run id.
- An internal cursor index resolves stage terminal facts at `stage.last_cursor`.
  Public evidence remains independently bounded, so an oversized terminal can still
  determine consumed/unused semantics without escaping or substituting start
  evidence into hints.
- Running Model/Tool stages are indexed once per Run before interruptions are
  evaluated.

### GREEN evidence

```text
Focused attribution unit + integration: 21 passed in 3.61s
All observability unit + integration:     105 passed in 5.34s
Workflow program + control execution:     18 passed in 5.27s
Ruff:                                     All checks passed!
Strict mypy:                              no issues in 9 source files
git diff --check:                         exit 0 (line-ending notices only)
```

The broader Workflow command remains `19 passed, 5 failed` with the same known
pre-existing recovery-certification failures already matched against the isolated
baseline in the first review fix.

## Final Re-review Fix

The final re-review reported `Critical 0 / Important 1 / Minor 0`. The remaining
issue was isolated to non-terminal Tool disposition.

### RED and fix

An interrupted Run with an in-flight `tool.call.started` event initially failed the
new regression because its RUNNING Tool contributor was `unused` instead of
`supporting`. `_terminal_event` now resolves the internal last-cursor fact only for
terminal stage statuses (`completed`, `failed`, `denied`, `timed_out`, or
`interrupted`). RUNNING/WAITING stages therefore cannot expose a start event as a
result. Existing terminal Tool semantics remain covered by the focused and stage
projection suites.

### GREEN evidence

```text
Focused attribution unit + integration: 22 passed in 3.49s
All observability unit + integration:     106 passed in 5.73s
Ruff:                                     All checks passed!
Strict mypy:                              no issues in 9 source files
git diff --check:                         exit 0 (line-ending notices only)
```
