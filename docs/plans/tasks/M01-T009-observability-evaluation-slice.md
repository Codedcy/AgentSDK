# M01-T009 Observability, Evaluation, and Analytics Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILLS: use `superpowers:test-driven-development` while implementing and `superpowers:verification-before-completion` before reporting completion. Execute this task in the current worktree; do not create another worktree.

**Goal:** Expose durable current Run state, timeline and Child execution-tree queries; resume event consumption from a global cursor; append one evidence-backed deterministic Run evaluation; and compute deletion-aware success and Tool failure aggregates from durable facts.

**Architecture:** `QueryService` combines immutable Run snapshots with the Store's durable high-water cursor. `SubscriptionService` is a stateless polling async iterator over the same cursor-ordered event log, so applications decide when and how to display Trace data. `EvaluationEngine` executes an application-supplied `Evaluator`, validates its evidence against the subject timeline, and appends an immutable Session-owned evaluation aggregate. `AnalyticsQueries` computes small M01 aggregates directly from immutable evaluation/tool events at a captured cursor; it reports observations, never causality.

**Tech Stack:** Pydantic v2 frozen contracts, asyncio async iterators, existing StateStore/EventEnvelope/RunSnapshot, SQLite and InMemory stores, pytest-asyncio.

## Global Constraints

- Runtime truth remains durable events plus snapshots. Observability never creates a second hidden trace store and never makes Runtime depend on an exporter or UI.
- Every query/aggregate exposes `as_of_cursor`. The Store exposes a durable `latest_cursor()` high-water mark that does not move backwards when a Session is deleted and cursor holes appear.
- Every event query/aggregate first captures high-water `H`, reads bounded durable pages with `cursor <= H`, and accepts only rows through `H`; a concurrent later commit belongs to the next observation. A caller cursor greater than the captured Store high-water is rejected as INVALID_STATE rather than silently moved backwards or allowed to skip future events.
- Query results are immutable and detached from Store-owned dictionaries. Missing/deleted records return stable NOT_FOUND; corrupt records and ordinary extension failures cross the public boundary only as sanitized `AgentSDKError` values.
- Event subscription is at-least-once from the caller's acknowledged cursor. It advances over nonmatching events, tolerates deletion-created cursor holes, creates no background task, and propagates `asyncio.CancelledError` without swallowing or replacing it.
- A long-lived subscription never holds `_SDKLifecycle.admit()` and never keeps SDK close waiting indefinitely. Lifecycle exposes a shared close signal: idle polling waits interruptibly for poll timeout or close; once closing is observable the iterator stops without any later Store read. A newly consumed iterator after closing fails with stable INVALID_STATE.
- Evaluations are append-only facts separate from Run completion. An evaluator cannot change Run status, forge subject/evaluator identity, cite another Run's events, or persist after its Session/Run was deleted.
- Analytics success is based only on explicit evaluation verdicts. Tool failure is based only on terminal `tool.call.completed` statuses. Unknown/malformed samples are counted as missing and never silently guessed.
- This slice provides success rate and Tool failure count/rate with optional exact evaluator/tool filters. Failure taxonomy, failure stage/root-cause attribution, Tool usefulness, comparisons and improvement insights remain M05 and must not be represented as causal here.
- Evaluation events/snapshots and their analytics contribution are Session-owned. Existing `delete_session` removes them automatically; recomputing analytics after deletion cannot retain a deleted Session's contribution.
- Existing LiteLLM-only execution, Workflow/Child recovery, permissions, context compaction and public APIs remain compatible.

---

### Task 1: Add cursor-aware event and Run query contracts

**Files:**
- Modify: `src/agent_sdk/storage/base.py`
- Modify: `src/agent_sdk/storage/memory.py`
- Modify: `src/agent_sdk/storage/sqlite.py`
- Create: `src/agent_sdk/observability/__init__.py`
- Create: `src/agent_sdk/observability/models.py`
- Create: `src/agent_sdk/observability/queries.py`
- Create: `tests/integration/observability/test_queries.py`
- Modify: Store test doubles only where their exercised surface requires `latest_cursor()`.

**Public interfaces:**
- `StateStore.latest_cursor()`
- `EventFilter`, `ObservedEvent`, `EventQueryResult`, `ObservedRun`, `RunTimeline`
- `ExecutionTreeNode`, `ExecutionTree`
- `QueryService.get_run`, `QueryService.timeline`, `QueryService.execution_tree`
- `QueryService.query_events(*, after_cursor=0, limit=100)`

- [ ] **Step 1: Write high-water and immutable query RED tests**

Cover InMemory and SQLite parity. Create a Run, move it through several states, and assert:

- `get_run(run_id)` returns the exact `RunSnapshot` plus a positive `as_of_cursor`;
- `timeline(run_id)` contains only that Run's events in cursor order and exposes the same-or-newer durable cursor;
- `query_events(EventFilter(...), after_cursor=...)` filters by exact Session, Run and event types while its next cursor advances over unrelated records;
- `query_events` reads at most the validated public `limit` (1..1000) raw cursor records, so an empty filtered page may still advance `next_cursor`; the caller continues until `next_cursor == as_of_cursor`;
- returned models/tuples/payloads cannot be mutated through aliases;
- `latest_cursor()` remains at the allocated high-water after deleting the Session that owned the last event.

The public result shapes are composition-based rather than copies of `RunSnapshot` fields:

```python
observed = await sdk.queries.get_run(run_id)
assert observed.snapshot.status == RunStatus.COMPLETED
assert observed.as_of_cursor >= 1

timeline = await sdk.queries.timeline(run_id)
assert timeline.events[-1].event.type == "run.completed"
```

- [ ] **Step 2: Implement durable Store high-water and bounded reads**

Add `latest_cursor()` to the StateStore contract, InMemoryStore, SQLiteStore and the SDK's lazy SQLite adapter. InMemory returns its monotonic allocation counter under the Store lock. SQLite reads `sqlite_sequence` under its existing lock, preserving the allocated cursor even if rows were deleted. Do not derive this value only from `MAX(events.cursor)`.

Extend `StateStore.read_events` with optional keyword-only `up_to_cursor` and `limit` parameters, defaulting to the existing unbounded behavior for source compatibility. InMemory iterates in cursor order and stops as soon as `limit` qualifying rows have been copied under its lock (it must not first allocate the full backlog); SQLite applies `cursor <= ?`, ordered `LIMIT ?` in SQL. Internal callers in this slice use a fixed bounded page size. Validate nonpositive limits/inverted cursor windows before touching storage.

- [ ] **Step 3: Implement bounded, stable Run observations**

`get_run` validates a `RunSnapshot`, captures a cursor and confirms the snapshot still exists and is byte-for-byte equivalent before returning. This detects a concurrent transition or Session deletion; retry a small bounded number of times, then return retryable CONFLICT rather than mixing state from different moments. A transition after the confirmation is allowed: the result is linearized at the confirmation read.

`timeline` first proves the Run exists, captures the high-water cursor, reads bounded pages only up to that cursor, filters by exact `run_id`, then confirms the exact same Run identity, Session ownership and immutable relationship fields still exist. A Session delete creates no durable event, so final identity confirmation—not only a tail event read—is required. It never leaks another Run or a deleted Session. Corrupt snapshot/event data becomes a context-free INTERNAL public error.

- [ ] **Step 4: Implement the M01 descendant execution tree**

For this slice `execution_tree(root_run_id)` means the requested Run plus the transitive Runs whose persisted `run.created` payload has `parent_run_id` pointing into that tree. It is sufficient for the parent/Child Workflow slice; joining arbitrary Workflow sibling nodes and external trace/span formats remains M05.

Return a flat, deterministic tuple of `ExecutionTreeNode(snapshot, parent_run_id, created_cursor)` in creation-cursor order. Validate exact Session ownership and every relationship against the current Run snapshots. Detect relevant Run transitions/new Child creation during assembly with a bounded re-read and fail closed on inconsistent/cross-Session records. Do not enumerate mutable in-process task state.

Do not require the global high-water to remain equal during tree assembly: unrelated Sessions/Runs may continue committing forever. Capture one finite `tail_H`, then re-read bounded pages only in `(H, tail_H]`; retry when those events transition an already-selected Run or create a Child whose parent is in the selected tree. Never chase a moving high-water while scanning the tail. After the tail check, re-read root and every selected Run snapshot and require exact equality/session ownership with the assembled observation; this catches Session deletion or same-id replacement that emits no retained event. Ignore malformed records that provably belong to an unrelated Session/tree, while a cross-Session record that claims a selected parent is an integrity failure.

- [ ] **Step 5: Verify Task 1**

```powershell
uv run --python 3.13 pytest tests/integration/observability/test_queries.py -v
```

Expected: snapshot/timeline/tree values are cursor-qualified, immutable, Session-safe, and equivalent across SQLite/InMemory including deletion-created cursor holes.

---

### Task 2: Implement resumable filtered subscriptions

**Files:**
- Create: `src/agent_sdk/observability/subscriptions.py`
- Create: `tests/integration/observability/test_subscriptions.py`

**Public interfaces:**
- `SubscriptionService.subscribe(*, filters=None, cursor=0)`

- [ ] **Step 1: Write cursor recovery and cancellation RED tests**

Start a subscription after a known cursor, append an unrelated event followed by a matching event, and prove the iterator yields the matching event once without rereading/spinning on the filtered event. Recreate the iterator from the last yielded cursor and prove the next matching event is delivered.

Also cover:

- global and Session/Run/type filters;
- a cursor whose event was removed by Session deletion;
- no events yet followed by a later commit;
- a large backlog of nonmatching events is processed in fixed pages, allows cancellation/close between pages, and does not allocate the whole backlog at once;
- consumer `aclose()` and task cancellation with no leaked polling/background task;
- cancellation while waiting propagates the same `CancelledError` instance where Python task semantics expose it;
- invalid negative cursors fail before any Store read.
- a cursor greater than the Store's current durable high-water fails with stable INVALID_STATE before polling.

- [ ] **Step 2: Implement a stateless polling async iterator**

Read durable batches with the Store `limit` set to a small fixed page size. For every stored event, update the local cursor before deciding whether it matches; yield only matches. Check cancellation/close between pages. If the batch is empty, wait on the SDK lifecycle close signal with a short configurable timeout (or the equivalent cancellation-safe primitive), then poll again. Do not create a producer task or unbounded queue in M01. If close races with a Store call, suppress a closed-Store error only after confirming the close signal; otherwise sanitize it as an SDK error.

The yielded `ObservedEvent.cursor` is the application acknowledgement token. Delivery after reconnect is at least once: the application resumes from the last cursor it durably acknowledged and may deduplicate by immutable `event_id`.

- [ ] **Step 3: Verify Task 2**

```powershell
uv run --python 3.13 pytest tests/integration/observability/test_subscriptions.py -v
```

Expected: matching delivery resumes by cursor, unrelated/deleted events cannot stall progress, idle waiting is cancellable, and there are no background-task lifecycle leaks.

---

### Task 3: Append an evidence-backed deterministic Run evaluation

**Files:**
- Create: `src/agent_sdk/evaluation/__init__.py`
- Create: `src/agent_sdk/evaluation/models.py`
- Create: `src/agent_sdk/evaluation/evaluators.py`
- Create: `src/agent_sdk/evaluation/engine.py`
- Create: `tests/integration/evaluation/test_evaluation_slice.py`

**Public interfaces:**
- `Evaluator` protocol and `EvaluationDecision`
- `EvaluationSubject`, `EvaluationVerdict`, `EvaluationResult`
- `ExactOutputEvaluator`
- `EvaluationEngine.evaluate(run_id, evaluator)`

- [ ] **Step 1: Write evaluator and immutable-persistence RED tests**

Evaluate a terminal Run with `ExactOutputEvaluator(expected="ok")` and assert a pass/fail verdict, `exact_match` metric, deterministic method, evaluator id/version, terminal Run event evidence, creation time and immutable record version. Assert `evaluation.completed` and the matching `evaluation` snapshot are committed atomically while the original Run snapshot/status remain unchanged.

An evaluation event is its own aggregate: use `evaluation_id` in the current envelope's aggregate `run_id` slot, `sequence=1`, and retain `subject_run_id` in the typed payload. This avoids competing for the already-terminal Run's event sequence and permits multiple evaluators/results for one Run.

- [ ] **Step 2: Write extension-boundary and race RED tests**

Cover:

- CREATED/RUNNING/WAITING Run rejection; COMPLETED and FAILED Runs are eligible terminal subjects;
- evaluator metadata/result schema validation before persistence;
- evidence ids must be unique members of the subject timeline at its captured cursor;
- user evaluator ordinary exception, invalid return and malicious evidence are sanitized with no cause/context/traceback-local leak and zero evaluation writes;
- parameterized `CancelledError` propagates unchanged and creates no evaluation record;
- concurrent Session deletion or changed Run version before commit cannot resurrect data;
- multiple evaluations append independent immutable records;
- an evaluation id collision/duplicate aggregate commit is rolled back atomically and mapped to stable retryable CONFLICT rather than a raw Store `ValueError`;
- SQLite reopen loads the same evaluation record.

- [ ] **Step 3: Implement frozen contracts and the built-in evaluator**

`EvaluationDecision` contains only evaluator-controlled claims: verdict, metrics, reason, confidence and evidence event ids. `EvaluationResult` adds SDK-controlled `evaluation_id`, Session/subject identity, subject type, evaluator id/version, method, `created_at`, schema/record version and the captured subject cursor. Freeze and detach metric mappings; reject non-finite values, duplicate evidence and extra fields.

The `Evaluator` protocol declares read-only `id`, `version` and `method` metadata plus `evaluate(subject)`. The engine validates all three strings in the same private extension helper and copies them into `EvaluationResult`; it never guesses a custom evaluator's method. `ExactOutputEvaluator.method` is the stable deterministic label `deterministic_exact_match`.

`ExactOutputEvaluator` compares the terminal `output_text` exactly, emits confidence `1.0`, cites the terminal Run event, and never invokes LiteLLM. It is the one deterministic best-practice validation included in M01; applications may supply their own protocol implementation.

- [ ] **Step 4: Implement fail-closed evaluation persistence**

Load `EvaluationSubject(snapshot, timeline, as_of_cursor)` as one stable observation: capture `H`, read only subject events through `H`, and confirm the same terminal snapshot/Session still exists. Do not compose two independently timed public query results. Obtain evaluator metadata and invoke/await the evaluator inside a private helper; metadata getters, invocation, await and return validation all belong to the extension boundary. Catch ordinary extension exceptions there and expose only stable SDK errors with no cause/context/extension traceback locals; never catch `BaseException`/`CancelledError`.

Commit `evaluation.completed` plus snapshot with atomic preconditions on owning Session existence, the exact terminal Run observation, and the durable lifetime of every referenced evidence event (always including the terminal event). Extend `CommitBatch` with backward-compatible event preconditions that bind `event_id`, cursor, Session/Run ownership, type and sequence. Memory validates them under its commit lock and SQLite validates them inside the same `BEGIN IMMEDIATE` transaction before any write; deletion followed by an identical Session/Run snapshot recreation therefore cannot satisfy the evaluation commit, even if ids, versions and canonical snapshot data are identical. Keep exact snapshot ownership/data preconditions for subject mutation detection, and map missing evidence to NOT_FOUND while identity/position or snapshot replacement conflicts become retryable CONFLICT. Do not update the Run or execute any model. The public result is returned only after commit succeeds.

The evaluation id is random and both its event aggregate (`sequence=1`) and snapshot are written in one Store transaction. Any id/aggregate collision must roll back the whole batch and become retryable CONFLICT; it must never overwrite an earlier immutable evaluation.

- [ ] **Step 5: Verify Task 3**

```powershell
uv run --python 3.13 pytest tests/integration/evaluation/test_evaluation_slice.py -v
```

Expected: deterministic evaluation is immutable, evidence-backed, append-only, recovery-safe and isolated from application evaluator failures/cancellation.

---

### Task 4: Add deletion-aware success and Tool failure aggregates

**Files:**
- Create: `src/agent_sdk/analytics/__init__.py`
- Create: `src/agent_sdk/analytics/models.py`
- Create: `src/agent_sdk/analytics/queries.py`
- Create: `tests/integration/analytics/test_analytics_slice.py`

**Public interfaces:**
- `AnalyticsResult`
- `AnalyticsQueries.success_rate(*, evaluator_id=None)`
- `AnalyticsQueries.tool_failures(*, tool_name=None)`
- `AnalyticsQueries.tool_failure_rate(*, tool_name=None)`

- [ ] **Step 1: Write explicit-success and Tool terminal-fact RED tests**

Create terminal Runs with pass/fail/unknown evaluation records and successful/failed/timed-out/denied Tool terminal events. Assert:

- success rate is `pass / (pass + fail)`, with unknown verdicts in `missing_count`;
- Tool failure count includes every known terminal non-`succeeded` status;
- Tool failure rate is `failures / known terminal calls`;
- evaluator/tool exact filters affect both numerator and denominator;
- no known denominator returns `value=None`, never a fabricated zero-rate conclusion;
- each result includes metric name, sample/missing counts, method, filters, evidence ids and `as_of_cursor`.
- a copied evaluation payload under a different event aggregate/Session cannot survive deletion of its real owner and continue contributing.

Define aggregation units exactly:

- each immutable `EvaluationResult` event is one success-rate candidate, even when one Run has multiple results from the same evaluator;
- `sample_count` is the known denominator (`pass + fail` for success, known terminal calls for Tool metrics); `missing_count` contains unknown verdicts and malformed attributable candidates;
- success rate and Tool failure rate return `value=None` when `sample_count == 0`; Tool failure *count* returns `0.0` when no known failures exist;
- with an exact evaluator/tool filter, a malformed candidate missing the filter identity cannot be attributed and is ignored; if identity matches but verdict/status is invalid it increments missing;
- cursor holes created by Session deletion are absence, never missing samples;
- `evidence_event_ids` contains every durable candidate event counted as known or missing, including the event id of an attributable malformed fact.

- [ ] **Step 2: Implement cursor-bounded fact aggregation**

Capture `latest_cursor()` as `H`, page through the durable log with `up_to_cursor=H` and a fixed `limit`, and aggregate in constant memory. Parse only `evaluation.completed` and `tool.call.completed` typed payloads using the exact counting/filter rules above. For every parsed evaluation require `event.run_id == result.evaluation_id` and `event.session_id == result.session_id`; an identity mismatch is an attributable malformed/missing candidate, never a known verdict. This envelope/payload cross-check preserves Session-owned deletion semantics. Tool terminal payloads contain no duplicated Session/Run identity and have no equivalent cross-check. Count other attributable malformed/unknown candidate facts as missing rather than crashing or treating them as success/failure. Preserve evidence event ids so applications can drill down (the returned evidence tuple is result-sized by definition; event scanning itself is bounded).

These methods report deterministic counting methods such as `explicit_evaluation_verdict` and `terminal_tool_status`. Do not emit failure stage, root cause, usefulness, attribution or recommendations in this slice.

- [ ] **Step 3: Prove restart and deletion behavior**

Run the same aggregates on InMemory and SQLite, close/reopen SQLite and compare results. Delete one contributing Session and assert its evaluation snapshot/event and Tool events are absent and all aggregate numerators/denominators/missing counts are recomputed without that Session. Inject an evaluation event whose envelope Session/aggregate disagrees with an otherwise valid payload; prove it is missing rather than known and cannot retain the payload Session's contribution after deletion.

- [ ] **Step 4: Verify Task 4**

```powershell
uv run --python 3.13 pytest tests/integration/analytics/test_analytics_slice.py -v
```

Expected: results are explicit, cursor-qualified, filterable, missing-aware, restart-safe and deletion-aware, with no causal inference.

---

### Task 5: Wire public SDK façades, lifecycle and package exports

**Files:**
- Modify: `src/agent_sdk/api.py`
- Modify: `src/agent_sdk/__init__.py`
- Modify: package `__init__.py` files above
- Create: `tests/integration/observability/test_public_observability_api.py`

**Public interfaces:**
- `sdk.queries.get_run/timeline/execution_tree/query_events`
- `sdk.events.subscribe`
- `sdk.evaluations.evaluate`
- `sdk.analytics.success_rate/tool_failures/tool_failure_rate`
- Package-root exports for all public contracts named in Tasks 1-4, including `EventQueryResult` returned by the public event query.

- [ ] **Step 1: Write public-only façade RED tests**

Use only imports from `agent_sdk` and an `AgentSDK.for_test` instance. Run, query, subscribe, evaluate and aggregate through the public façade. Verify default SQLite construction exposes the same APIs without eager database opening.

Mutation methods participate in `_SDKLifecycle`: a newly-started evaluation is rejected once closing begins, and close cannot cut through an admitted evaluation commit. An already-open subscription terminates cleanly when SDK close begins rather than touching a closed owned Store; a new subscription/evaluation after closing yields stable INVALID_STATE. Read-only query behavior should match existing `runs.get`/`workflows.get` conventions and never leak raw closed-Store exceptions.

All new public queries, event reads, subscriptions, evaluations and analytics invoked after closing begins use `ErrorCode.INVALID_STATE` with stable sanitized messages. Ordinary exceptions from a custom StateStore are normalized in private helpers with no cause/context/Store traceback locals; their `CancelledError` propagates unchanged. Short query/analytics/evaluation calls may use lifecycle admission so close waits for the admitted operation, but subscriptions use only the shared close signal described above.

- [ ] **Step 2: Keep application presentation outside the SDK**

The façade returns query records, cursor streams, evaluation records and aggregate values only. Do not add an HTTP server, dashboard, logging side effects or automatic console output. This preserves the product decision that applications choose when/how Trace and analysis are displayed; M06 provides a reference monitor as a best-practice validation.

- [ ] **Step 3: Run the task gate**

```powershell
uv run --python 3.13 pytest tests/integration/observability tests/integration/evaluation tests/integration/analytics -v
uv run --python 3.13 pytest -q
uv run --python 3.13 ruff check src tests
uv run --python 3.13 mypy src
git diff --check
```

Expected: all existing and new tests pass; there are no ignored/xfailed acceptance paths; public models are immutable; custom evaluators/cancellation are isolated; SQLite restart and Session deletion are proven.

- [ ] **Step 4: Commit**

```powershell
git add docs/plans/tasks/M01-T009-observability-evaluation-slice.md src/agent_sdk tests
git commit -m "feat: add observability evaluation slice"
```
