# M02-T002 Phase 5A Implementation Report

## Status

DONE. Phase 5A implements strict reconciliation resolution admission and the
explicit `CONFIRM_NOT_EXECUTED` and `RETRY` transitions. The work remains on
`feature/agent-sdk-implementation`, based on `5d5600b`, for independent review.
No Phase 5B, M02-T003, M02-T004, `CONFIRM_COMPLETED`, `TERMINATE`, or subprocess
release-gate behavior was implemented.

This report is included in the Phase 5A commit; the handoff records the final
commit hash.

## Implemented scope

- Added the exact `RecoveryAPI.resolve(request_id, action, *, actor, evidence)`
  and `ReconciliationService.resolve(...)` contracts. Both return detached
  `ReconciliationRequest` values.
- Exported `ReconciliationAction`, `ReconciliationRequest`,
  `ReconciliationResolution`, and `ReconciliationService` from `agent_sdk`.
- Added strict request/action/actor/evidence, lifecycle, capability, Session
  ownership, Run/checkpoint/operation, full event-envelope, and exact pending
  request admission.
- Added exact same-decision replay, constant changed-decision conflicts, and
  bounded two-SDK convergence/conflict behavior.
- Added one-lease, one-`RunProgressBatch` resolution of the request/event,
  terminalization of the old operation, rewind to a safe checkpoint,
  transition of the Run to `INTERRUPTED`, and retention of the Run by its
  Session.
- Extended Memory and SQLite validation for the exact old-generation
  resolution batch without relaxing ordinary operation, checkpoint, event, or
  reconciliation transitions.
- Extended the closed recovery grammar so resolved attempts are authenticated
  and removed from the effective history before a later explicit recovery
  creates a new operation at the same logical turn.
- Added lifecycle, cancellation, SDK-close, Session close/delete, lease loss or
  expiry, CAS, corruption, capability drift, post-commit ambiguity, Memory,
  SQLite, and two-SDK coverage.
- `CONFIRM_COMPLETED` and `TERMINATE` remain constant `INVALID_STATE` with no
  durable mutation.

## Audit conclusions

### Lifecycle and atomicity

The admitted resolution projection has no impossible intermediate snapshot:

- authoritative Run: `WAITING_RECONCILIATION` to `INTERRUPTED`, version `+1`;
- current checkpoint: matching in-flight phase to `READY_FOR_MODEL` or
  `READY_FOR_TOOL`, checkpoint version `+1`, `operation_id=None`;
- old operation: exact `STARTED` record to `FAILED` with the exact
  reconciliation request/action outcome while retaining its original lease
  generation;
- reconciliation request: exact `PENDING` record to `RESOLVED` with the paired
  resolution event;
- Session: unchanged and still owns the non-final Run;
- event history: one `reconciliation.resolved` immediately after the exact
  paired `reconciliation.requested` event.

Memory publishes copied targets only after every check succeeds. SQLite applies
the same targets inside one immediate transaction and rolls back on every
conflict/fault. Cancellation, SDK close, lease loss/expiry, and CAS tests prove
the durable outcome is either the entire decision or no decision.

### No external callbacks during resolution

`RecoveryAPI.resolve` performs lifecycle admission and the startup scanner,
then delegates to reconciliation admission. The resolution path uses the Store,
lease manager, immutable execution descriptors, and local Agent/Tool/policy
registries only. It does not call the Provider, Tool handler, MCP integration,
permission bridge, or Workflow coordinator. Public tests install forbidden
Provider/Tool callbacks and prove their call counts remain zero throughout
resolution; the external attempt occurs only after a later explicit
`recover_run`.

### Secret retention

The shared public assertion inspects the public error text, cause/context,
formatted traceback, and every retained SDK traceback frame's locals. Fresh
characterization coverage now explicitly includes:

- capability drift: `fake/capability-drift-secret`;
- post-commit partial Store failure: `partial-resolution-store-secret`;
- existing actor, evidence, replay, corrupt request/operation/event payload,
  and lazy Store wrapper secret paths.

Both requested assertions were characterization-GREEN; no production
sanitization change was required.

## RED/GREEN evidence

### Starting baseline

```text
C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe run --python 3.13 pytest tests/integration/runtime/test_reconciliation_resolution.py -q
73 passed in 8.51s
```

### Requested secret-retention characterization

Added the capability-drift and post-commit partial Store assertions before any
production change. Existing public-boundary sanitation already satisfied them:

```text
C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe run --python 3.13 pytest tests/integration/runtime/test_reconciliation_resolution.py -q -k "capability_drift or post_commit_partial_resolution"
13 passed, 60 deselected in 4.00s
```

### Exact paired-event replay gap

The audit found one genuine missing Store admission check: once every output
target was already exact after a post-commit ambiguity, a forged
`reconciliation.requested` event precondition could be ignored. A Memory/SQLite
regression test was added first.

RED:

```text
C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe run --python 3.13 pytest tests/integration/storage/test_run_progress_reconciliation.py::test_retry_resolution_exact_replay_requires_paired_requested_event -q
2 failed in 3.21s
Memory and SQLite both failed with: DID NOT RAISE RecoveryStateConflictError
```

Minimal production fix: before both fresh application and exact replay, Memory
and SQLite now authenticate the requested event precondition and its exact
request/operation/reason payload. Exact output replay still ignores a released
or expired lease, as required.

GREEN:

```text
C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe run --python 3.13 pytest tests/integration/storage/test_run_progress_reconciliation.py::test_retry_resolution_exact_replay_requires_paired_requested_event -q
2 passed in 2.94s
```

## Fresh verification evidence

All commands used the explicit executable
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe`.

### Phase 5A plus Phase 3 Provider/Tool and RecoveryAPI neighbors

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
509 passed in 85.11s
```

### Phase 2 Store, live/lease/Session/idempotency, and Phase 4 Workflow neighbors

```text
uv.exe run --python 3.13 pytest -q \
  tests/unit/runtime/test_reconciliation_models.py \
  tests/integration/storage/test_recovery_records.py \
  tests/integration/storage/test_sqlite_recovery_validation.py \
  tests/integration/runtime/test_recovery_scanner.py \
  tests/integration/runtime/test_live_run_progress.py \
  tests/integration/runtime/test_leases.py \
  tests/integration/runtime/test_session_lifecycle.py \
  tests/integration/runtime/test_run_session_ownership.py \
  tests/contract/test_memory_store_contract.py \
  tests/contract/test_idempotency_store_contract.py \
  tests/e2e/test_session_lifecycle_idempotency.py \
  tests/integration/workflow/test_workflow_recovery.py \
  tests/integration/workflow/test_workflow_recovery_admission.py \
  tests/integration/workflow/test_workflow_session_ownership.py
543 passed in 17.98s
```

### Full Python 3.13

```text
uv.exe run --python 3.13 pytest -q
1764 passed in 121.60s; zero skipped, zero failed
```

### Final focused Phase 5A public/storage gate

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
131 passed in 8.38s
```

### Static, diff, import, signature, scope, and schema gates

```text
uv.exe run --python 3.13 ruff check src tests
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files

git diff --check
exit 0 (only Windows LF-to-CRLF informational warnings)
```

The import/signature/schema smoke passed with 103 unique root exports, exact
`RecoveryAPI.resolve` and `ReconciliationService.resolve` parameter kinds and
return contracts, the existing `RecoveryAPI.recover_workflow` signature
retained, and SQLite `_SCHEMA_VERSION == 3`.

The scope check passed with exactly these implementation files before adding
this ignored report:

- `src/agent_sdk/__init__.py`
- `src/agent_sdk/api.py`
- `src/agent_sdk/runtime/reconciliation.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/storage/memory.py`
- `src/agent_sdk/storage/sqlite.py`
- `tests/integration/runtime/test_reconciliation_resolution.py`
- `tests/integration/storage/test_run_progress_reconciliation.py`

There is no dependency, lockfile, docs, roadmap, progress-ledger, migration, or
schema-version change.

## Concerns and handoff

No implementation or verification concerns remain. Phase 5A is ready for the
independent review required by the phase plan. The branch and worktree are
preserved; no merge, push, Phase 5B, M02-T003, or M02-T004 action was taken.

## Independent review closure addendum

The independent Phase 5A review returned C0/I3/M0. All three important
findings were reproduced test-first, fixed without entering Phase 5B, and
verified on `feature/agent-sdk-implementation`. This addendum supersedes the
pre-review handoff statement immediately above; Phase 5A is now ready for the
post-review handoff.

### I3: cancellation traceback retained caller secrets

The existing public Memory pre-commit cancellation test was strengthened with
secret-bearing actor and evidence values. The public assertion inspects error
text, cause/context, formatted traceback, and every retained SDK frame local.

RED:

```text
uv.exe run --python 3.13 pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_public_resolution_cancellation_at_memory_precommit_is_atomic
1 failed; the inner `_resolve_private` traceback retained both caller secrets
```

The public resolution boundary now consumes the original cancellation, deletes
its public inputs after the inner coroutine has unwound, and raises a fresh
`CancelledError from None`. Atomicity remains zero-before/one-after, callbacks
remain zero, and exact replay still succeeds.

GREEN:

```text
uv.exe run --python 3.13 pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_public_resolution_cancellation_at_memory_precommit_is_atomic
1 passed in 2.88s
```

### I3: Store resolution-batch admission was not exact

Memory and SQLite tests were added for missing Session/Run snapshot
preconditions and for inexact Session/Run preconditions, noncanonical request
reason/details, and an unrelated target Run field mutation. Every rejected
batch asserts exact zero mutation of cursor, Session, Run, operation,
checkpoint, and request.

RED:

```text
uv.exe run --python 3.13 pytest -q tests/integration/storage/test_run_progress_reconciliation.py::test_retry_resolution_batch_requires_both_snapshot_preconditions
4 failed; Memory and SQLite accepted both missing-precondition variants

uv.exe run --python 3.13 pytest -q tests/integration/storage/test_run_progress_reconciliation.py::test_retry_resolution_batch_rejects_inexact_admission_relations
10 failed; Memory and SQLite accepted all five forged relations
```

Both Store implementations now require the exact two canonical snapshot
preconditions, exact Session ownership and Run relation, the exact one-field
Run transition, and canonical request reason/details before admitting either a
fresh resolution batch or its exact replay.

GREEN:

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/storage/test_run_progress_reconciliation.py::test_retry_resolution_batch_requires_both_snapshot_preconditions \
  tests/integration/storage/test_run_progress_reconciliation.py::test_retry_resolution_batch_rejects_inexact_admission_relations \
  tests/integration/storage/test_run_progress_reconciliation.py::test_retry_resolution_batch_applies_atomically \
  tests/integration/storage/test_run_progress_reconciliation.py::test_retry_resolution_exact_replay_requires_paired_requested_event
18 passed in 3.56s
```

### I3: resolved-history discovery and authentication were incomplete

The public exact-replay orphan test first inserted a second resolved row for
the same Run/operation with no paired event.

RED and discovery GREEN:

```text
uv.exe run --python 3.13 pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_resolution_replay_rejects_orphan_resolved_request_without_event
2 failed in 3.26s; Memory and SQLite both returned the replay

uv.exe run --python 3.13 pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_resolution_replay_rejects_orphan_resolved_request_without_event
2 passed in 3.11s
```

`StateStore`, the lazy Store, Memory, and SQLite now expose a typed
`list_reconciliation_requests(run_id)` read. Evidence loading uses it to make
every reconciliation row for the Run discoverable; no schema or version change
was needed.

The lockstep corruption matrix then changed both sides of each previously
trusted relation: wrong action-specific evidence in the row and resolved
event, noncanonical reason in the row and requested event, noncanonical
details, and a forged operation request fingerprint in both SQLite's projected
column and canonical record. Each case is crossed with Memory and SQLite and
uses public recovery with a Provider callback that would complete successfully
if reached.

RED and grammar GREEN:

```text
uv.exe run --python 3.13 pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_recovery_rejects_lockstep_corrupt_resolved_history_before_external_work
8 failed in 4.02s; all forged histories normalized and reached the Provider

uv.exe run --python 3.13 pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_recovery_rejects_lockstep_corrupt_resolved_history_before_external_work
8 passed in 3.40s
```

Resolved attempts are now authenticated before normalization: exact
request/resolution/event/operation pairing, action-specific evidence,
operation-kind-specific reason/details, Run/Session/logical-turn linkage,
provider or tool identity and recovery metadata, and the original operation
fingerprint reconstructed from durable attempt context. Corrupt histories fail
closed into `recovery_state_invalid` without Provider, Tool/MCP, permission, or
Workflow work.

### Review-fix verification

The first complete covering-file run found a same-turn retry regression in the
new logical-turn check (`1 failed, 154 passed in 9.38s`). Interrupted retries
may have multiple `step.started` events for one logical turn, so the exact turn
is now derived from completed steps before the attempt. Its isolated regression
test passed in 3.93s, after which the full covering gate passed:

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
155 passed in 9.30s
```

The exact seven-file focused superset previously containing 509 tests now
contains the added review coverage:

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
533 passed in 86.54s
```

Full and static gates:

```text
uv.exe run --python 3.13 pytest -q
1788 passed in 120.67s; zero skipped, zero failed

uv.exe run --python 3.13 ruff check src tests
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files
```

The final import/signature/schema smoke passed with 103 unique root exports,
exact `RecoveryAPI.resolve`, `ReconciliationService.resolve`, and
`StateStore.list_reconciliation_requests` contracts, the unchanged
`RecoveryAPI.recover_workflow` contract, and SQLite `_SCHEMA_VERSION == 3`.
The scope check contains only the seven review-fix implementation/test files
plus this report, with no dependency, lockfile, docs, roadmap, progress-ledger,
migration, or schema-version change. `git diff --check` passed; its only output
was Windows LF-to-CRLF informational warnings.

No implementation or verification concerns remain. The worktree is preserved;
no merge, push, Phase 5B, M02-T003, or M02-T004 action was taken.

## Final narrow-review closure addendum

The final narrow review returned C0/I1/M0 because the preceding exact-slice
uniqueness fix over-rejected canonical first-interruption histories. With no
prior recovery control event, the recovery interval includes markers from
fully completed earlier turns; requiring one marker across that entire
interval incorrectly made later-turn resolution replay conflict.

### Canonical later-turn positive RED

The new public positive matrix is crossed with Memory/SQLite and Model/Tool.
Each case completes turn 0 through a real model call and unsafe tool execution,
then interrupts a canonical turn-1 model or tool attempt during the same
initial execution boundary. After canonical resolution, exact replay must be
zero-mutation and callback-free, and one later explicit recovery must invoke
only the target Provider or Tool callback once.

```text
uv.exe run --python 3.13 pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_later_turn_canonical_resolution_replays_and_recovers_once
4 failed in 5.03s; Model/Tool on Memory/SQLite all conflicted at exact replay
```

### Turn-filtered slice GREEN

Within the existing recovery/interrupt boundary, candidate
`step.started`/`tool.call.proposed` markers are now first filtered to those
whose logical turn—defined by the number of completed steps before the
marker—equals the resolved operation's durable turn. The unchanged exactly-one
rule and exact attempt authenticator then apply. Canonical prior-turn markers
are ignored, duplicate markers at the target turn remain rejected, and
legitimate repeated same-turn retry cycles remain separated by their control
boundaries.

The combined positive, duplicate-negative, and repeated-cycle gate passed:

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_later_turn_canonical_resolution_replays_and_recovers_once \
  tests/integration/runtime/test_reconciliation_resolution.py::test_duplicate_attempt_start_fails_closed_before_external_work \
  tests/integration/runtime/test_reconciliation_resolution.py::test_resolved_model_attempt_is_excluded_from_next_same_turn_attempt \
  tests/integration/runtime/test_reconciliation_resolution.py::test_resolved_tool_attempt_is_excluded_from_next_same_turn_attempt
10 passed in 4.09s
```

### Final narrow-review verification

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
164 passed in 10.07s

uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
542 passed in 86.08s

uv.exe run --python 3.13 pytest -q
1797 passed in 120.21s; zero skipped, zero failed

uv.exe run --python 3.13 ruff check src tests
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files
```

The import/signature/schema smoke remains at 103 unique root exports and
SQLite schema version 3. The narrow-review scope contains only
`src/agent_sdk/runtime/recovery.py`, its integration test file, and this
report. There is no dependency, lockfile, docs, roadmap, progress-ledger,
migration, or schema-version change. `git diff --check` passed with only
Windows LF-to-CRLF informational warnings.

No implementation or verification concerns remain. The worktree is preserved;
no merge, push, Phase 5B, M02-T003, or M02-T004 action was taken.

## Independent re-review closure addendum

The Phase 5A re-review returned C0/I1/M0. The remaining important finding was
an exact-slice ambiguity in resolved-history normalization: inside the current
recovery/interrupt interval, `_effective_resolved_evidence` collected every
operation-kind start marker and selected the last one. A duplicate marker
immediately before `run.interrupted` could therefore be selected and removed
while the real attempt slice remained.

### Duplicate model start RED

The public Memory/SQLite test inserts a second canonically sequenced
`step.started` immediately before the paired interrupt, while preserving the
canonical resolved row, requested/resolved events, operation, checkpoint, and
Run. Public recovery has a successful Provider callback installed and must
instead admit only bounded `recovery_state_invalid`; exact replay must then
conflict without mutation.

```text
uv.exe run --python 3.13 pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_duplicate_model_attempt_start_fails_closed_before_external_work
2 failed in 3.95s; Memory and SQLite both reached Provider completion
```

### Duplicate tool proposal RED

The matching unsafe-tool case inserts a second canonical
`tool.call.proposed`. Recovery already admitted only bounded reconciliation
with zero Provider, Tool/MCP, recovery-audit, and permission work, but the
subsequent exact replay accepted the old resolution.

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_duplicate_attempt_start_fails_closed_before_external_work \
  -k tool
2 failed, 2 deselected in 3.46s; both exact replays returned successfully
```

### Canonical-slice uniqueness GREEN

Within the already scoped prior-interrupt/`run.recovery.started` to paired
interrupt boundary, resolved model and tool operations now require exactly one
operation-kind marker (`step.started` or `tool.call.proposed`) and bind the
removed slice to that sole marker. The implementation no longer chooses the
last candidate. It does not reject starts from completed turns or earlier
same-turn attempts: each legitimate retry cycle has its own durable recovery
and interrupt boundary.

A new repeated same-turn unsafe-tool resolution/retry test was added alongside
the existing model test. It was characterization-green before the production
change:

```text
uv.exe run --python 3.13 pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_resolved_tool_attempt_is_excluded_from_next_same_turn_attempt
1 passed in 2.92s
```

The combined corruption and legal-cycle gate passed after the one-relation
production fix:

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_duplicate_attempt_start_fails_closed_before_external_work \
  tests/integration/runtime/test_reconciliation_resolution.py::test_resolved_model_attempt_is_excluded_from_next_same_turn_attempt \
  tests/integration/runtime/test_reconciliation_resolution.py::test_resolved_tool_attempt_is_excluded_from_next_same_turn_attempt
6 passed in 3.27s
```

### Re-review verification

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
160 passed in 9.01s

uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
538 passed in 84.64s

uv.exe run --python 3.13 pytest -q
1793 passed in 119.44s; zero skipped, zero failed

uv.exe run --python 3.13 ruff check src tests
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files
```

The import/signature/schema smoke remains at 103 unique root exports and
SQLite schema version 3. The re-review scope contains only
`src/agent_sdk/runtime/recovery.py`, its integration test file, and this
report. There is no dependency, lockfile, docs, roadmap, progress-ledger,
migration, or schema-version change. `git diff --check` passed with only
Windows LF-to-CRLF informational warnings.

No implementation or verification concerns remain. The worktree is preserved;
no merge, push, Phase 5B, M02-T003, or M02-T004 action was taken.
