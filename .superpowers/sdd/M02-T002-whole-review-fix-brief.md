# M02-T002 Whole-Review Quality Fix Brief

Source review: fresh whole-task review of `24b624f..59e5ca5`.
The review found Spec C0/I0/M0 and Task Quality C0/I2/M2. This fix closes only
the two Important runtime scalability findings without entering M02-T003 or
M02-T004 and without changing public APIs, Store protocol, migrations, or
SQLite schema 3.

## I1 - Bound cross-SDK follower polling

`RunRecoveryService._follow_durable_run` and resolution followers currently use
the default `_yield_once`, which is only `asyncio.sleep(0)`. A slow owner causes
thousands of snapshot/lease reads per second and can contend with the owner's
heartbeat and terminal commit.

- Replace the zero-delay default with one explicit, private, bounded polling
  interval of 50 milliseconds. Keep the existing injectable awaitable seam for
  deterministic tests.
- Apply the bounded default consistently to durable Run and reconciliation
  followers. Do not add a new public configuration field in this task.
- Preserve prompt SDK-close response, terminal/failure observation, waiting-
  reconciliation propagation, lease expiry detection, and takeover behavior.
  The extra observation latency must be bounded by one interval.
- Add RED/GREEN regression coverage using a slow live owner and an instrumented
  Store. Across Memory and SQLite where feasible, prove reads during a fixed
  observation window have a clear small upper bound, then release the owner and
  prove the follower converges to the exact result. Retain existing multi-SDK,
  owner/follower, close, expiry, and resolution tests.

## I2 - Scope recovery evidence event reads

`RunRecoveryService._load_evidence` currently calls `read_events` without a
filter and materializes the complete database log before selecting the target
Run and Session lifecycle records.

- Use the existing `StateStore.read_events(..., session_id=run.session_id)`
  contract with the same fixed upper cursor. Derive the target Run history and
  target Session lifecycle evidence from that bounded result.
- Continue validating event-id uniqueness over all materialized target-Session
  evidence. Global event-id uniqueness remains a Store invariant: SQLite has
  its schema-level unique constraint and validation; Memory rejects duplicate
  ids on commit. Do not add a new Store protocol method or schema/index.
- Preserve exact cursor order, continuous per-Run sequence, complete closed Run
  grammar, Session detach/close evidence, reconciliation replay, corruption
  fail-closed behavior, and Workflow terminal recovery evidence preconditions.
- Add RED/GREEN regression coverage with many unrelated Sessions/events and an
  instrumented Memory/SQLite delegate. Prove recovery passes the exact target
  `session_id` filter and the returned/materialized evidence count is unchanged
  when unrelated database history is added. Include corruption negatives in
  the target Session so filtering cannot weaken strict admission.

## Scope and gates

- Strict TDD: record RED evidence for both findings before implementation.
- Production changes should be limited to `runtime/recovery.py`; tests may add
  narrowly scoped delegates/fixtures in existing recovery suites.
- Do not refactor the duplicated Memory/SQLite pure validators in this fix; it
  remains a nonblocking whole-review Minor for later storage maintenance.
- Do not change int64 validation semantics; direct type/boundary automation is a
  nonblocking test-coverage Minor and may be recorded in the final ledger.
- Run exact new tests; Run recovery, reconciliation, Provider/Tool, scanner,
  Workflow recovery, Session lifecycle, and fault/MCP neighboring suites; full
  Python 3.13; Ruff; strict mypy; import/export/signature/schema/migration,
  diff/scope, and clean-worktree gates.
- Write `.superpowers/sdd/M02-T002-whole-review-fix-report.md` with RED/GREEN,
  performance/read-count evidence, exact commands/results, and the retained
  Minors. Obtain a fresh independent whole-task re-review with C0/I0 before
  updating task/progress ledgers.
