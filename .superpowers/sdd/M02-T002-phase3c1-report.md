# M02-T002 Phase 3C1 Implementation Report

## Status

IMPLEMENTED. Phase 3C1 adds the private, no-external-I/O substrate for abandoned
Run discovery and interruption. `INTERRUPTED` and `WAITING_RECONCILIATION` are
strictly nonterminal; Memory, SQLite, and lazy SQLite expose exact abandoned-Run
and Run-event-tail queries; `RunProgressBatch` can atomically create or resolve
one reconciliation request; and an explicitly invoked `RecoveryScanner.scan()`
claims a fresh lease generation and writes one exact `run.interrupted` boundary.

The implementation is based on
`25db10a29545c427108d3a515b570a9c0274de83`. It does not expose or schedule a
public recovery command, resume a checkpoint, invoke or register a provider,
Tool, MCP, Workflow, or application callback, or change RunEngine/Workflow/schema
behavior.

## Delivered behavior

### Nonterminal recovery-owned Run states

- `RunStatus.INTERRUPTED` and `RunStatus.WAITING_RECONCILIATION` require version
  3 or later and reject output, usage, error, and durable Tool results.
- `RUN_LIFECYCLE_FINAL_STATUSES` remains exactly `COMPLETED` and `FAILED`.
- Scanner interruption changes only the Run snapshot and Run event. It leaves
  the Session snapshot and active ownership exact, so a closing Session remains
  closing and normal deletion remains busy.

### Abandoned discovery and exact event tails

- `StateStore.list_abandoned_run_ids(now=...)` has Memory/SQLite/lazy parity.
  It normalizes an aware timestamp to UTC and selects only authoritative
  `RUNNING`/`WAITING_PERMISSION` Runs with no active, unreleased lease whose
  expiry is strictly after `now`.
- Results are detached, unique, and Run-id sorted. Missing Memory leases,
  released SQLite leases, and leases expiring exactly at `now` are abandoned.
- Every stored Run is validated before status filtering. Its exact wrapper,
  model representation, owning Session, and terminal/nonterminal ownership are
  checked. All Memory leases and all SQLite lease rows are validated, including
  exact generation, released representation, and canonical timestamps.
- `latest_run_event_sequence` reads the exact Run-owned event rows and rejects
  malformed sequences, cross-Session ownership, noncanonical JSON, or orphan
  events. Snapshot version, other Run sequences, and the global cursor are not
  used. No snapshot and no Run event returns `None`; an event without its
  authoritative Run fails closed.
- Session deletion removes Run snapshots, events, and leases so abandoned
  discovery is empty and the event tail is `None`.
- Event-tail reads enforce the ownership invariant in both directions:
  `session_owns_run` is true exactly when the Run is nonterminal. A COMPLETED or
  FAILED Run that remains in `active_run_ids` is therefore corruption, just as a
  nonterminal Run missing from `active_run_ids` is corruption.
- All corruption/conflict boundaries reconstruct constant context-free recovery
  errors. Lazy forwarding discards query arguments before rethrow, and tests
  require nonempty SDK traceback frames without injected secrets.

### Atomic reconciliation request target

- `ReconciliationRequestWrite(expected, updated)` is an optional final field on
  `RunProgressBatch`; all existing fields and positional compatibility remain
  unchanged.
- A first application requires the exact active lease and authoritative Run and
  Session scope. Create requires `expected=None`, a pending request, no existing
  request id, and an exact same-Run/Session operation link when present.
- Update requires full-record expected CAS and the existing Phase 2 immutable
  pending-to-resolved contract, including the exact same-batch audit event,
  actor, evidence, action, decision time, and event id.
- Memory publishes copied events, snapshots, operations, checkpoints, requests,
  and cursor only after every check succeeds. SQLite checks and applies all
  targets in one existing `BEGIN IMMEDIATE` transaction and commits once.
- Exact all-target replay is read-only before lease validation. Create replay
  after release and update replay at expiry return `applied=False`; partial,
  different, illegal-shape, stale-CAS, cross-scope, foreign-operation, and
  oversized-event invocations are constant conflicts with zero mutation.
- Before that lease-free exact return, Memory verifies the canonical durable
  request and its map-key identity, while SQLite verifies request id, Session,
  Run, operation, and status columns against canonical `data_json`. A linked
  operation must still exist and own the same Run/Session. If the operation is
  also a same-batch target, its current durable value must be the exact updated
  target; the batch object alone is not treated as evidence.
- Memory cancellation, SQLite precommit failure, cancellation/commit race,
  lazy exact-object forwarding, secret traceback sanitization, and Session
  deletion are covered. Standalone Phase 2 request APIs and schema are unchanged.

### Private RecoveryScanner

- `RecoveryScanner` accepts only a `StateStore`, optional `LeaseManager`, and
  deterministic clock. Phase 3C1 calls `scan()` explicitly; AgentSDK construction
  does not know about or schedule it.
- Scans are locally serialized. Each Store-reported id uses a fresh `coord_*`
  owner and generation; a current live owner is safely skipped.
- After acquisition the scanner reloads and validates the exact Run and Session,
  reads the exact event tail, and submits one generation-fenced batch containing
  only `run.interrupted`, the adjacent Run snapshot, and exact Run/Session
  preconditions. No checkpoint, operation, request, or Session write is made.
- No prior tail produces sequence 1; an existing tail produces exact tail + 1.
  Repeated and simultaneous scanners append only one interruption event.
- The commit coordinator shields and settles the one commit task, retries an
  ambiguous error with the identical batch object, and preserves repeated owner
  cancellation even when the Store suppresses cancellation. Exact lease release
  is performed once and settled through repeated cancellation; late release
  failure is consumed without a loop error or leaked task.
- Barrier tests cover scan versus terminal transition, Session deletion, and a
  newly active live owner. A stale pre-scan writer is fenced by the higher
  generation. Malformed tails and corrupt ownership fail closed without partial
  state or retained secrets.

## Strict TDD evidence

All commands used the worktree Python 3.13 environment.

1. Status and abandoned-query surface
   - RED: `5 failed in 3.51s`; both status values were absent.
   - Minimal status GREEN: `2 passed, 3 deselected in 2.84s`.
   - Query RED: `3 failed, 2 deselected in 3.30s`; Memory, SQLite, and lazy
     SQLite lacked `list_abandoned_run_ids`.
   - Initial GREEN: `5 passed in 3.54s`.
2. Corrupt lease representation
   - RED: `2 failed, 7 passed, 11 deselected in 5.59s`; SQLite/lazy coerced an
     illegal `released=2` row to true.
   - GREEN: `9 passed, 11 deselected in 3.67s` after exact 0/1 validation.
3. Exact event-tail surface and corruption
   - Surface RED: `3 failed, 20 deselected in 3.32s`; all three methods absent.
   - Minimal GREEN: `3 passed, 20 deselected in 3.12s`.
   - Corruption RED: `6 failed, 23 deselected in 4.19s`; cross-Session events and
     sequence zero were accepted.
   - GREEN: `6 passed, 23 deselected in 4.52s`.
4. Atomic reconciliation target
   - Create surface RED: `2 failed in 3.46s`; the write type was absent.
   - Create GREEN: `2 passed in 3.97s`.
   - Update RED: `2 failed, 2 deselected in 3.73s`; create-only shape rejected a
     legal pending-to-resolved transition.
   - Update GREEN: `2 passed, 2 deselected in 3.10s`.
   - Replay/CAS/scope/fault/cancel/race/deletion/lazy strengthening reached
     `25 passed in 4.51s`, then exact event matching, int64 legality, and foreign
     operation scope reached the final focused count below.
5. Lazy query sanitization
   - RED: `1 failed, 29 deselected in 3.37s`; a secret Run id remained in the
     lazy forwarding traceback.
   - GREEN: `1 passed, 29 deselected in 4.22s` after a context-free lazy boundary.
6. Scanner surface
   - RED: `6 failed in 3.42s`; the private recovery module/scanner was absent for
     three Stores and both abandoned statuses.
   - Initial GREEN: `6 passed in 4.06s`.
7. Scanner ambiguous commit and suppressed cancellation
   - RED: `2 failed, 22 deselected in 3.12s`; a durable ambiguous commit was not
     confirmed, and a Store suppressing two cancels made scan appear successful.
   - GREEN: `2 passed, 22 deselected in 2.75s` with identical replay and shielded
     settling.
8. Deletion tail semantics
   - RED: `3 failed, 30 deselected in 3.91s`; deletion returned conflict instead
     of `None` when both Run and events were absent.
   - GREEN: `3 passed, 30 deselected in 2.97s`; orphan events still conflict.
9. Validate before status filtering
   - RED: `5 failed, 1 passed, 33 deselected in 3.52s`; excluded-status Session
     ownership and SQLite/lazy lease corruption could be skipped.
   - GREEN: `6 passed, 33 deselected in 3.11s` after all-record validation.
10. Review I1: bidirectional event-tail ownership
   - Shared Memory/SQLite/lazy RED: `6 failed, 39 deselected in 3.58s`; COMPLETED
     and FAILED Runs still owned by the Session returned a tail instead of a
     constant conflict.
   - GREEN: `6 passed, 39 deselected in 5.96s` with the exact bidirectional
     terminal/nonterminal ownership invariant and secret-free tracebacks.
11. Review I2: strict lease-free reconciliation replay
   - RED matrix: `7 failed, 4 passed, 35 deselected in 4.58s`. The failures were
     Memory missing/foreign linked operations, SQLite Run/status/operation
     wrapper and noncanonical-JSON mismatches, and the lazy secret-bearing
     wrapper mismatch. Request-id/Session mismatches and exact same-batch
     operation replay were already rejected/accepted correctly and formed the
     four baseline passes.
   - GREEN: `11 passed, 35 deselected in 5.90s` after symmetric strict durable
     request and operation validation before the exact replay return.

Fresh final Phase 3C1 focused result: `115 passed in 7.31s`.

## Final-code gates

- Phase 3C1 focused: `115 passed in 7.31s`.
- Phase 3B live progress: `38 passed in 4.63s`.
- Phase 3A Run-progress transaction: `117 passed in 15.76s`.
- Phase 2 recovery models/records/SQLite validation: `136 passed in 11.66s`.
- Phase 1 + M02-T001 regressions: `188 passed in 19.94s`.
- Session lifecycle and Run/Workflow ownership regressions:
  `108 passed in 17.84s`.
- Full Python 3.13 pytest: `1179 passed, 1 skipped in 53.41s`. The sole skip is
  the pre-existing prompt integration check when the environment has no `uv`
  executable; no test was weakened or marked skipped by Phase 3C1.
- Ruff: `All checks passed!`.
- Mypy: `Success: no issues found in 73 source files`.
- `git diff --check`: exit 0; only Windows LF-to-CRLF informational warnings.

One initial Phase 1 gate command used the former integration path for the
idempotency contract and collected zero tests. It was discarded as an invalid
command; the exact real `tests/contract/test_idempotency_store_contract.py`
path was used in the fresh 188-test result above.

## Production and test scope

Production changes are limited to:

- `src/agent_sdk/runtime/models.py`
- new private `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/storage/base.py`
- `src/agent_sdk/storage/memory.py`
- `src/agent_sdk/storage/sqlite.py`
- `_LazySQLiteStore` forwarding only in `src/agent_sdk/api.py`

Focused tests are limited to:

- `tests/integration/storage/test_abandoned_runs.py`
- `tests/integration/storage/test_run_progress_reconciliation.py`
- `tests/integration/runtime/test_recovery_scanner.py`

The explicit forbidden-scope audit has zero diff for RunEngine, Workflow,
provider/model/Tool/MCP implementations, migrations, roadmap, milestones, and
task index. `api.py` changes are exactly the two lazy Store forwarding methods;
there is no AgentSDK construction or scheduling change. SQLite schema version
remains 3, and no migration, table, index, or schema SQL changed.

## Conservative decisions and concerns

- Event-tail absence is `None` only when both the authoritative Run and all of
  its events are absent. An orphan event is corruption and fails closed.
- Query validation precedes status filtering so corruption beside a CREATED,
  terminal, interrupted, or reconciliation-owned Run cannot be silently hidden.
- Scanner release failure is settled and consumed; the generation fence and
  finite TTL remain authoritative, and no external work can occur in this slice.
- The scanner intentionally does not synthesize or transition reconciliation
  requests. That admission policy, descriptor/capability comparison, checkpoint
  resume, public `recover_run`, and construction-time scheduling remain Phase 3C2.
- Provider authoritative-status and provider-enforced same-operation-id adapters
  remain Phase 3D; Workflow recovery remains out of scope.

The first post-commit fresh review returned C0/I2/M0. Both Important findings are
covered by the RED-to-GREEN evidence above and are resolved in this fix. No known
in-scope Critical or Important correctness concern remains in self-audit. Phase
3C2 must not begin until a fresh read-only re-review of the fix commit returns
C0/I0.
