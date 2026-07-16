# M02-T002 Phase 5B2A Implementation Report

## Status

DONE. Phase 5B2A implements strict `CONFIRM_COMPLETED` resolution for
operation-linked Tool reconciliation requests on Memory and SQLite. The
implementation commit is `2178fab` (`feat(recovery): confirm tool
reconciliation outcomes`).

This slice is intentionally limited to confirmed Tool outcomes. Workflow
snapshot projection remains Phase 5B2B. Phase 5C fault-injection/E2E,
M02-T003, M02-T004, and `TERMINATE` remain untouched.

## Implemented scope

- Accepts evidence only as the exact object `{"tool_result": ...}` and
  reconstructs a strict detached `ToolResult` without Pydantic coercion.
- Enforces exact Tool-result fields and raw types, valid statuses, 16 KiB
  content/value bounds, a 512-byte error bound, finite JSON values, string
  object keys, and a JSON round trip. Tuples, non-finite numbers, arbitrary
  objects, unknown fields, wrong call IDs, and wrong Tool names fail closed.
- Reuses the Phase 5A closed lifecycle, reconciliation-operation, capability,
  and Tool-call certifier chain before admitting an operator decision.
- Projects one exact atomic batch: resolved audit record, terminal Tool
  operation, `READY_FOR_MODEL` checkpoint at the next turn, interrupted Run,
  then `reconciliation.resolved`, `tool.call.completed`, and `step.completed`.
- Maps only `succeeded` to a completed Tool operation; denied, timed out,
  invalid arguments, and normalized execution failures remain canonical
  failed Tool outcomes while still becoming authoritative durable results.
- Extends Memory and SQLite old-generation admission only for the exact legal
  confirmed-Tool batch. Ordinary transition and generation rules remain
  unchanged.
- Authenticates exact public replay both immediately and after legal later
  history: normal terminal completion, a later Tool turn, or a unique later
  pending Model reconciliation.
- Preserves stable replay after post-commit ambiguity and across two SDK
  instances, without re-running the Tool or invoking Provider, MCP,
  permission, hook, or application callbacks.
- Preserves Session closing/deletion behavior, explicit recovery, SDK close,
  cancellation atomicity, capability-drift rejection, and zero mutation on
  every invalid/conflicting request.
- Rejects missing, duplicated, moved, or malformed Tool lifecycle and atomic
  resolution evidence on both Stores.

The certification-only effective history removes reconciliation control
markers, retains the authoritative Tool completion and step completion, and
places the original interruption at its certified historical position. The
durable event history is never rewritten.

## Changed files

- `src/agent_sdk/runtime/reconciliation.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/storage/memory.py`
- `src/agent_sdk/storage/sqlite.py`
- `tests/integration/runtime/test_reconciliation_resolution.py`
- `.superpowers/sdd/M02-T002-phase5b2a-report.md`

There is no dependency, lockfile, migration, SQLite schema-version, public
export-count, Workflow snapshot, roadmap, or progress-ledger change.

## RED/GREEN evidence

All commands used
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` with
Python 3.13.

### Canonical Tool projection

The production-path Memory/SQLite matrix for succeeded and normalized
non-JSON failure results was written first.

```text
RED:  4 failed in 5.94s; public resolve returned the unsupported-action error
GREEN: the initial matrix passed; the expanded 8-shape/2-Store matrix passed
       16 cases
```

The expanded matrix covers scalar, object, list, and null success values plus
normalized non-JSON failure, denied, timed-out, and invalid-arguments results.
Every case proves exact recovery, no repeated Tool call, later terminal
completion, and stable replay.

### Strict evidence and zero mutation

The raw evidence matrix covers extra/missing fields, coercible strings,
tuples, non-string keys, non-finite values, arbitrary objects, oversized
payloads, mismatched call identity, and mismatched Tool identity.

```text
RED:  wrong call/name evidence reached durable-state conflict instead of the
      constant non-retryable invalid-state decision
GREEN: 2 Store cases passed the complete invalid-evidence matrix with zero
       durable mutation
```

Boundary and detachment tests additionally prove exact 16 KiB admission,
deep detachment from caller-owned values, and conflict on changed replay.

### Atomicity, replay, and closed history

Dedicated Memory/SQLite matrices pass for post-commit ambiguity, two-SDK
convergence, cancellation, SDK close, Session closing/deletion, later pending
Model reconciliation, later Tool turns, capability drift, and Store-level
malformed old-generation batches.

The missing/duplicate/moved Tool lifecycle matrix passes 10 cases. It
authenticates the complete historical Tool lifecycle rather than only the
confirmed suffix.

## Fresh verification evidence

### Focused reconciliation and Store gate

```text
pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
334 passed in 22.02s
```

### Reconciliation, Provider, Tool-recovery, and RecoveryAPI gate

```text
pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
712 passed in 96.47s
```

### Store/lease/Session and Workflow neighbors

```text
pytest -q \
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
543 passed in 18.59s
```

### Full Python 3.13 suite

```text
pytest -q
1967 passed in 131.02s; zero skipped, zero failed
```

### Static, compatibility, diff, and scope gates

```text
ruff check .
All checks passed!

mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The fresh compatibility smoke passed with 103 unique `agent_sdk.__all__`
exports, exact unchanged `RecoveryAPI.resolve` and
`ReconciliationService.resolve` signatures, and SQLite schema version 3. The
pre-report scope check contained exactly the five implementation/test files
listed above.

## Risks and handoff

No known Phase 5B2A implementation or verification risk remains. The
confirmed-Tool exception is narrow and must stay bound to the exact resolution
record, exact Tool operation, exact lifecycle suffix, and the existing closed
historical certifier chain.

Phase 5B2B must separately design and test Workflow snapshot projection. This
implementation must not be treated as admitting Workflow evidence or changing
Workflow recovery semantics. The branch and worktree are preserved for
independent review. No merge or push was performed.

## Independent review closure addendum

The independent Phase 5B2A review returned C0/I1/M0. The Important finding
was reproduced on Memory and SQLite before production code changed, then
closed in commit `4f22203` (`fix(recovery): certify multiple tool
resolutions`). This addendum supersedes the pre-review risk statement above.

### I1: Tool exact replay assumed a single resolved request

`_is_confirmed_tool_replay_closed_world` required every resolved record to be
the original Tool request. Its pending branch also required exactly two total
records. This rejected legal histories in both temporal directions:

- confirmed Tool outcome, explicit recovery, later unknown Model outcome,
  `CONFIRM_NOT_EXECUTED`, then exact replay of the original Tool decision;
- a resolved Model attempt followed by a retried Model call, a later unknown
  Tool outcome, Tool `CONFIRM_COMPLETED`, then immediate exact Tool replay.

The Memory/SQLite production-path tests were written first. Both paths use
real cancellation, startup scanning, reconciliation admission, resolution,
and explicit recovery. Exact replay is also required to perform zero writes
and invoke neither Provider nor Tool callbacks.

```text
RED:
test_confirmed_tool_replay_survives_later_resolved_model_reconciliation
test_confirmed_tool_replay_accepts_a_prior_resolved_model_attempt
4 failed in 5.81s; all four failed only at original Tool replay with
"recovery state conflict"
```

The first attempted removal of the two cardinality checks intentionally did
not mask the deeper relation: the same four positive cases remained RED while
the four corruption cases stayed GREEN. Diagnostics showed that
`_effective_resolved_evidence` sorted records chronologically, but the next
attempt was still certified against raw operations/events rather than the
accumulated prior normalization. In particular, a confirmed Tool's historical
interruption is deferred behind its authoritative Tool and step completion;
that deferred interruption was absent from certification of a later resolved
attempt.

The fix keeps one closed grammar instead of adding another state machine:

- `_has_closed_reconciliation_markers` still proves one exact requested marker
  per record, one exact resolved marker per resolved record, no resolved marker
  for pending records, and unique request IDs.
- `_effective_resolved_evidence` remains the chronological certifier for every
  resolved attempt. It now carries accumulated removed operations/events and
  deferred confirmed-Tool interruptions into certification of the next
  attempt.
- Confirmed Tool replay delegates exact decision authentication to that closed
  effective-history path instead of authenticating the same decision once
  against unnormalized raw history and again against the normalized history.
- Immediate interrupted-state projection uses the certified effective
  operations and the normalized final `run.interrupted` marker. Pending and
  terminal paths continue through their existing operation/provider history
  certifiers.

The negative matrix injects either an orphan resolved reconciliation record or
a duplicate historical `reconciliation.resolved` event after constructing a
real two-resolution history. Both Stores reject every corruption as a constant
conflict with zero mutation, preventing the cardinality relaxation from
opening the history grammar.

```text
GREEN:
two temporal directions plus orphan/duplicate matrix
8 passed in 4.21s
```

## Review-fix verification

All commands used the explicit executable
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` and Python
3.13.

```text
pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
342 passed in 22.30s

pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
720 passed in 97.94s

Store/lease/Session and Workflow-neighbor gate:
543 passed in 19.00s

pytest -q
1975 passed in 132.64s; zero skipped, zero failed

ruff check .
All checks passed!

mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The fresh compatibility smoke passed with 103 unique root exports, exact
unchanged `RecoveryAPI.resolve` and `ReconciliationService.resolve`
signatures, and SQLite schema version 3. The implementation commit touched
only `src/agent_sdk/runtime/recovery.py` and
`tests/integration/runtime/test_reconciliation_resolution.py`; this report is
the only documentation change.

No known Phase 5B2A implementation or verification concern remains after the
review fix. Workflow snapshot projection remains Phase 5B2B. Phase 5C,
M02-T003, M02-T004, `TERMINATE`, roadmap, progress ledger, schema, public API,
dependencies, and lockfiles remain untouched. No merge or push was performed.

## Second independent review closure addendum

The second independent Phase 5B2A review returned C0/I1/M0. The remaining
Important finding was reproduced on Memory and SQLite before production code
changed, then closed in commit `a05ac6b` (`fix(recovery): replay later safe
tool states`). This addendum supersedes the preceding post-review risk
statement.

### I1: later resolved READY_FOR_TOOL state was tied to the original Tool turn

After an original Tool `CONFIRM_COMPLETED`, explicit recovery can run the next
Model turn and produce a new Tool call. If that later Tool outcome is unknown
and the operator chooses `CONFIRM_NOT_EXECUTED`, the canonical current state is
an interrupted `READY_FOR_TOOL` checkpoint at the later turn. Exact replay of
the original confirmed Tool decision incorrectly conflicted.

The outer closed-world branch still required the original decision's immediate
projection: `READY_FOR_MODEL`, `checkpoint.turn == original_tool.turn + 1`,
equal completed Model/Tool turn sets, and a final normalized interruption. It
therefore rejected a history that `_has_closed_reconciliation_markers` and
`_effective_resolved_evidence` had already authenticated as two chronological
resolved attempts with a legal current checkpoint.

The Memory/SQLite production-path test was written first. It performs the
original Tool confirmation, explicit recovery, a real following Model Tool
call, cancellation during that new Tool execution, startup scan, a new Tool
reconciliation, `CONFIRM_NOT_EXECUTED`, and exact replay of the original
decision. Provider and Tool callbacks are forbidden during replay, and the
complete resolution domain must remain byte-identical.

```text
RED:
test_confirmed_tool_replay_accepts_later_resolved_ready_for_tool_state
2 failed in 4.01s; Memory and SQLite both returned
"recovery state conflict" only at original Tool replay
```

The fix preserves the original immediate `READY_FOR_MODEL` fast path. A
fallback is available only when another resolved reconciliation record exists.
That fallback reuses the same effective-history safe-checkpoint certification
already used by recovery planning:

- `_is_safe_checkpoint` now delegates its already-normalized state check to
  `_is_certified_safe_checkpoint`;
- confirmed Tool replay calls that helper only after exact closed marker and
  chronological resolved-attempt certification;
- `READY_FOR_TOOL` continues through `_is_exact_ready_tool_relation`, which
  authenticates checkpoint messages/output/usage/results, exact operation
  turns and fingerprints, lifecycle event order and payloads, and the full FSM
  relation;
- the engine's resume-checkpoint validator is also applied before replay is
  accepted.

The original immediate branch is unchanged semantically, and histories without
a second resolved record cannot enter the fallback.

### Fail-closed current-state matrix

The negative matrix constructs the same real two-resolution history, then
corrupts exactly one current-state component: the checkpoint output, the later
Model completion event, or the later Model operation fingerprint. It crosses
both Stores. All six replay attempts remain constant conflicts with zero
mutation and no callbacks.

```text
Initial negative characterization: 6 passed in 3.77s
GREEN positive plus negative matrix: 8 passed in 4.52s
Two-review compatibility matrix:    32 passed in 6.34s
```

## Second-review verification

All commands used the explicit executable
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` and Python
3.13.

```text
pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
350 passed in 22.48s

pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
728 passed in 98.23s

Store/lease/Session and Workflow-neighbor gate:
543 passed in 19.06s

pytest -q
1983 passed in 131.07s; zero skipped, zero failed

ruff check .
All checks passed!

mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The fresh compatibility smoke passed with 103 unique root exports, exact
unchanged `RecoveryAPI.resolve` and `ReconciliationService.resolve`
signatures, and SQLite schema version 3. The implementation commit touched
only `src/agent_sdk/runtime/recovery.py` and
`tests/integration/runtime/test_reconciliation_resolution.py`; this report is
the only documentation change.

No known Phase 5B2A implementation or verification concern remains after the
second review fix. Workflow snapshot projection remains Phase 5B2B. Phase 5C,
M02-T003, M02-T004, `TERMINATE`, roadmap, progress ledger, schema, public API,
dependencies, and lockfiles remain untouched. No merge or push was performed.
