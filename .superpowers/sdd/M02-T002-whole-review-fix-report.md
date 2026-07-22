# M02-T002 Whole-Review Quality Fix Report

Date: 2026-07-17 (Asia/Shanghai)

Source brief: `.superpowers/sdd/M02-T002-whole-review-fix-brief.md`

Fix baseline: `c3dfe30`

Implementation head before this report: `052b77f`

## Outcome

The two Important quality findings from the fresh whole-M02-T002 review were
reproduced with test-first evidence and closed without changing any public API,
Store protocol, dependency, migration, or SQLite schema. The default cross-SDK
follower no longer busy-polls a slow owner, and recovery evidence reads are now
bounded to the target Session at the same fixed upper cursor.

The final Python 3.13 suite passed all 2,159 tests with no failures or skips.
The broad recovery/reconciliation/Workflow/Session/fault/MCP regression gate
passed 1,267 tests. Task and progress ledgers remain unchanged pending the
required fresh independent whole-task re-review.

Implementation commits:

- `82b8cb6` - `fix(recovery): bound follower polling interval`
- `052b77f` - `fix(recovery): scope evidence reads to session`

The approved repair brief is commit `c3dfe30`.

## I1 - bounded cross-SDK follower polling

### RED

The new Memory/SQLite test starts a real recovery owner, blocks its Provider,
starts a second SDK as the follower, waits until that follower observes the
durable lease, resets the instrumented read counters, and observes a fixed
0.18-second window without injecting a custom yield function.

With the old `asyncio.sleep(0)` default, the test failed its per-read upper
bound with:

```text
Memory run snapshot reads: 2926
SQLite run snapshot reads: 247
required upper bound: 4
2 failed in 3.68s
```

This demonstrated Store pressure through the production default rather than a
synthetic unit loop.

### GREEN

One private `_FOLLOWER_POLL_INTERVAL_SECONDS = 0.05` constant now backs the
existing default awaitable. The existing injectable `_yield` seam is unchanged.
Both durable Run followers and reconciliation-resolution followers already use
that same default, so the bounded interval applies consistently without a new
public configuration surface.

Across both Memory and SQLite, the same test now proves no more than four Run
snapshot reads and four lease reads during the 0.18-second window. It then
releases the owner and proves that the follower returns the exact same durable
`RunResult`, with only one Provider call. The follower result is bounded by a
0.2-second test timeout after the owner result, while the production loop adds
only one 50ms observation interval.

```text
uv.exe run --python 3.13 pytest `
  tests/integration/runtime/test_recovery_api.py::test_default_cross_sdk_follower_polling_is_bounded -q
2 passed in 3.41s

uv.exe run --python 3.13 pytest `
  tests/integration/runtime/test_recovery_api.py `
  tests/integration/runtime/test_reconciliation_resolution.py `
  -k "cross_sdk or two_sdk or follower" -q
20 passed, 457 deselected in 4.62s
```

Existing cancellation, SDK close, owner disappearance, lease expiry, takeover,
terminal failure, waiting-reconciliation, and resolution-follower behavior is
retained by the neighboring gate and the later broad/full gates.

## I2 - Session-scoped recovery evidence

### RED

The instrumented evidence delegate seeds a valid interrupted target Run, records
its target-Session evidence, then adds 64 unrelated Sessions/events. Recovery
planning uses one fixed upper cursor. Before the fix, both Memory and SQLite
recorded the call as:

```text
read_events(after_cursor=0, session_id=None, up_to_cursor=77, limit=None)
```

The implementation therefore materialized all 77 events rather than preserving
the unchanged target-Session evidence count. Both positive backend cases failed.

Two fail-closed injections also failed before implementation: a delegate that
returned one foreign-Session event for a scoped request, and a delegate that
returned a duplicate target-Session event id. Because the old implementation
never issued a scoped request, neither injection was activated and both invalid
plans incorrectly remained `resume`. The exact RED command produced four
failures.

### GREEN

`RunRecoveryService._load_evidence` now passes
`session_id=run.session_id` while retaining the same `latest_cursor()` upper
bound. Run records and Session detach/close lifecycle records are derived from
that bounded materialization. The implementation also verifies that every
materialized event belongs to the requested Session and keeps event-id
uniqueness validation over the complete target-Session result. Global event-id
uniqueness remains the existing Store invariant.

With 64 unrelated Sessions present, both backends now record exactly:

```text
read_events(after_cursor=0, session_id=<target>, up_to_cursor=77, limit=None)
```

The delegate return count equals the target-only baseline and does not grow with
the unrelated history. Both injected corruptions now fail closed as
`reconcile / recovery_state_invalid`.

```text
uv.exe run --python 3.13 pytest `
  tests/integration/runtime/test_recovery_api.py::test_recovery_evidence_read_is_bounded_to_target_session `
  tests/integration/runtime/test_recovery_api.py::test_session_scoped_evidence_rejects_materialized_corruption -q
4 passed in 3.26s

uv.exe run --python 3.13 pytest tests/integration/runtime/test_recovery_api.py -q
123 passed in 69.05s
```

The full recovery grammar, exact cursor order, continuous per-Run sequence,
closed-Run certification, reconciliation replay, Session detach/close evidence,
and Workflow terminal evidence preconditions remain covered by the broad and
full gates below.

## Broad and full verification

The broad gate included RecoveryAPI, reconciliation resolution, Provider and
Tool recovery, scanner, Session lifecycle and ownership, recovery storage
validation, Workflow recovery/admission/projection/ownership/child behavior,
real subprocess death, MCP, and Session lifecycle E2E:

```text
uv.exe run --frozen --python 3.13 python -m pytest -q -ra `
  tests/integration/runtime/test_recovery_api.py `
  tests/integration/runtime/test_reconciliation_resolution.py `
  tests/integration/runtime/test_provider_recovery_execution.py `
  tests/integration/runtime/test_provider_recovery_live.py `
  tests/integration/runtime/test_tool_recovery_execution.py `
  tests/integration/runtime/test_recovery_scanner.py `
  tests/integration/runtime/test_session_lifecycle.py `
  tests/integration/runtime/test_run_session_ownership.py `
  tests/integration/storage/test_recovery_records.py `
  tests/integration/storage/test_run_progress_reconciliation.py `
  tests/integration/storage/test_sqlite_recovery_validation.py `
  tests/integration/workflow/test_workflow_recovery.py `
  tests/integration/workflow/test_workflow_recovery_admission.py `
  tests/integration/workflow/test_workflow_reconciliation_projection.py `
  tests/integration/workflow/test_workflow_session_ownership.py `
  tests/integration/workflow/test_workflow_child_slice.py `
  tests/faults/test_subprocess_recovery.py `
  tests/integration/mcp/test_mcp_tool_slice.py `
  tests/e2e/test_session_lifecycle_idempotency.py
1267 passed in 140.35s
```

Fresh full supported-Python gate:

```text
uv.exe run --frozen --python 3.13 python -m pytest -q -ra
2159 passed in 166.61s
```

There were no failures and no skips.

## Static, compatibility, schema, and scope gates

```text
uv.exe run --frozen --python 3.13 ruff check src tests examples
All checks passed!

uv.exe run --frozen --python 3.13 mypy src
Success: no issues found in 75 source files

git diff --check c3dfe30..HEAD
exit 0
```

A fresh compatibility smoke:

- imported all 53 `agent_sdk` modules;
- resolved all 103 unique package-root exports;
- confirmed exact matching `RecoveryAPI.resolve` and
  `ReconciliationService.resolve` signatures;
- opened and validated a fresh SQLite database with migration versions
  `(1, 2, 3)` and schema version 3.

Migration hashes remain unchanged:

- `0001_initial.sql`:
  `bbba32d3480b1a2ce4d9e0443bcd118dbaad0f9e639622040922ba5fa2d796b3`
- `0002_idempotency.sql`:
  `ab0169f70c28946a0564cc57a8bce97b9f5164819930cad71b96aaba8d0bc02c`
- `0003_leases.sql`:
  `63eaef03dcd1c10aabb6ce654374b8ae4d4bcc40477742a992ab2e26f933b7ee`

Before this report, the exact implementation diff from `c3dfe30` contained
only:

- `src/agent_sdk/runtime/recovery.py`
- `tests/integration/runtime/test_recovery_api.py`

The final repair scope adds only this report. `pyproject.toml`, `uv.lock`, all
Store implementations and migrations, roadmap/milestone/task files,
`.superpowers/sdd/progress.md`, M02-T003, and M02-T004 remain unchanged.

## Retained nonblocking Minors and handoff

The review's two nonblocking Minors remain deliberately out of this narrow
repair:

1. duplicated Memory/SQLite pure recovery validators may be consolidated in a
   later storage-maintenance task;
2. additional direct type/boundary automation for signed-int64 validation may
   be added later without changing the currently retained semantics.

No known Critical or Important repair finding remains after the fresh gates.
The branch is ready for the required independent whole-M02-T002 re-review with
Critical 0 / Important 0 as the acceptance threshold. Task/progress ledgers
must not be updated until that review is approved.
