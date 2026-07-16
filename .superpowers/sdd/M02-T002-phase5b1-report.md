# M02-T002 Phase 5B1 Implementation Report

## Status

DONE. Phase 5B1 implements strict `CONFIRM_COMPLETED` resolution for
operation-linked Model reconciliation requests on Memory and SQLite. The
implementation commit is `8d9d671` (`feat(recovery): confirm model
reconciliation outcomes`).

The work is intentionally limited to confirmed Model outcomes. Confirmed Tool
outcome projection and Workflow projection remain Phase 5B2. Phase 5C fault
injection/E2E, M02-T003, M02-T004, and `TERMINATE` are not implemented here.

## Implemented scope

- Admits evidence with exactly one `provider_result` key and reconstructs the
  existing strict `ProviderRecoveryResult`; only `completed` and `failed`
  dispositions are accepted.
- Rejects coercion, extra fields, non-finite/non-object Tool arguments,
  unbounded strings, multiple Tool calls, and non-public Provider errors with
  the constant decision error and zero mutation.
- Resolves normal unknown Model outcomes in one `RunProgressBatch`, including
  the request, audit event, operation, checkpoint, Run, Session ownership,
  lifecycle events, output, messages, and token usage.
- Projects completed text outcomes to a terminal completed Run and completed
  Tool-call outcomes to `READY_FOR_TOOL` plus an interrupted, Session-owned
  Run. Tool execution occurs only after explicit recovery.
- Projects failed Provider outcomes to the canonical failed operation,
  checkpoint, Run, lifecycle, public error, and Session detach/close state.
- Closes the certified completed-model terminalization gap without rewriting
  the durable completed operation or duplicating its assistant message, usage,
  Model-completed event, or step-completed event.
- Extends resolved-history validation and exact public replay for completed
  text, completed Tool-call, failed, and terminalization-gap decisions.
- Extends the Memory and SQLite old-generation exception only for exact legal
  confirmed-Model batches. Ordinary transitions remain generation-exact.
- Adds an exact operation precondition for terminalization-only batches and
  validates its signed-integer fields and byte-identical durable relation.
- Preserves post-commit convergence, two-SDK convergence, lease/CAS atomicity,
  Session closing behavior, closed-SDK behavior, and callback-free resolution.

No LiteLLM, Provider recovery adapter, Tool, MCP, permission, hook, Workflow,
or application callback is invoked while resolving the decision.

## Changed files

- `src/agent_sdk/runtime/provider_recovery.py`
- `src/agent_sdk/runtime/reconciliation.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/storage/base.py`
- `src/agent_sdk/storage/memory.py`
- `src/agent_sdk/storage/sqlite.py`
- `tests/integration/runtime/test_reconciliation_resolution.py`

There is no dependency, lockfile, migration, SQLite schema-version, public
export-count, roadmap, or progress-ledger change.

## RED/GREEN evidence

All commands used the explicit executable
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` and Python
3.13.

### Canonical Model projections

The Memory/SQLite matrix for completed text, completed Tool call, and Provider
failure was written first.

RED:

```text
pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_confirm_completed_model_projects_exact_durable_outcome
6 failed; every case returned "reconciliation action is not supported"
```

GREEN:

```text
pytest -q tests/integration/runtime/test_reconciliation_resolution.py::test_confirm_completed_model_projects_exact_durable_outcome
6 passed
```

### Completed-model terminalization gap

The gap matrix first failed at the certified recovery-state relation. After
adding terminalization-only admission with an exact operation precondition:

```text
RED:   2 failed with recovery state conflict
GREEN: 2 passed
```

The passing assertions prove that the operation remains byte-identical and
that Model usage/completion, assistant message, and step completion are not
emitted twice.

### Replay and subsequent explicit Tool recovery

Exact confirmed replay initially failed against the closed-history grammar.
The Tool-call branch then exposed a second RED where explicit recovery still
returned `recovery required`. Resolved-history certification was extended
through the existing lifecycle and ready-for-tool certifiers.

```text
Exact replay GREEN:                    6 passed
Tool-call explicit recovery RED:       2 failed with "recovery required"
Tool-call explicit recovery GREEN:     2 passed
```

### Strict nested usage evidence

The invalid-evidence matrix initially found that nested `TokenUsage` accepted
a string token count through Pydantic coercion.

```text
RED:   194 passed, 2 failed; coerced usage was admitted
GREEN: 40 passed in the targeted invalid-evidence and Provider-model gate
```

The existing recovery model now validates the nested usage mapping before
construction and accepts only real nonnegative integers.

## Fresh verification evidence

### Focused reconciliation and Store gate

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
214 passed in 13.59s
```

### Reconciliation, Provider, Tool-recovery, and RecoveryAPI gate

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
592 passed in 90.83s
```

### Phase 2 Store/lease/Session and Phase 4 Workflow neighbors

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
543 passed in 18.46s
```

### Full Python 3.13 suite

```text
uv.exe run --python 3.13 pytest -q
1847 passed in 121.03s; zero skipped, zero failed
```

### Static, diff, import, signature, schema, and scope gates

```text
uv.exe run --python 3.13 ruff check src tests
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The import/signature/schema smoke passed with 103 unique `agent_sdk.__all__`
exports, exact unchanged `RecoveryAPI.resolve` and
`ReconciliationService.resolve` signatures, and SQLite schema version 3. The
pre-report scope check contained exactly the seven files listed above.

## Risks and handoff

No known Phase 5B1 implementation or verification risk remains. The large
recovery projection is guarded by exact lifecycle, capability, operation,
checkpoint, Run, Session, event-envelope, request, current-lease, and Store
preconditions on both backends, plus replay and corruption matrices.

Phase 5B2 must separately design and test confirmed Tool outcomes and Workflow
projection; this implementation must not be treated as admitting either. The
branch and worktree are preserved for independent review. No merge or push was
performed.

## Independent review closure addendum

The independent Phase 5B1 review returned C0/I3/M0. All three Important
findings were reproduced through public production paths before production
changes, fixed without entering Phase 5B2, and verified on Memory and SQLite.
The review-fix implementation commit is `3aeaddb` (`fix(recovery): certify
confirmed model replay history`). This addendum supersedes the pre-review risk
statement above.

### I1: confirmed Tool-call replay consumed all later history

`_is_exact_confirmed_model_replay` previously treated every event after
`reconciliation.resolved` as the immediate decision suffix. A valid explicit
Tool recovery followed by normal Model completion therefore made exact replay
conflict permanently. The same assumption placed the original
`run.interrupted` at the end of normalized history, preventing a later crashed
Model attempt from being certified.

The existing public Tool-recovery test was extended first across Memory and
SQLite. The reported-usage cases completed the Tool and following Model, then
failed only at exact replay:

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_confirmed_model_tool_call_resumes_only_on_explicit_recovery
4 failed in 4.10s
reported Memory/SQLite: exact replay raised "recovery state conflict"
empty-usage Memory/SQLite: explicit recovery raised "recovery required"
```

A second production-path test resolves the original Tool call, executes it,
blocks and cancels the following Model call, runs the startup scanner, creates
the next Model reconciliation request, and replays the original decision.
Before the fix, the Memory case reached the new pending request but the old
exact replay still conflicted.

The confirmed decision certifier now authenticates only the fixed atomic slice
`reconciliation.resolved`, `model.usage.reported`, and
`model.call.completed`, including exact payloads, contiguous global cursors,
and identical timestamps. Normalization removes the requested marker and
moves the original interruption immediately after that projection and before
all subsequent recovery events. Immediate state still passes the existing
ready-for-Tool certifier; later interrupted or closed histories pass the
existing envelope and current-operation certifiers without requiring the
current checkpoint to remain `READY_FOR_TOOL`.

GREEN:

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_confirmed_model_tool_call_resumes_only_on_explicit_recovery
4 passed in 3.45s

uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_confirmed_tool_call_history_allows_a_later_model_reconciliation
2 passed in 3.07s
```

### I2: terminal replay omitted exact lifecycle-batch evidence

Terminal replay loaded only `run_id == run.run_id` events, so it could not see
the terminal `session.run.detached` or `session.closed` event whose `run_id` is
`None`. Completed replay also checked the terminal event types without checking
the exact `step.completed` and `run.completed` payloads.

The new public corruption matrix first creates a real confirmed terminal
projection, then performs one bounded durable-history corruption and invokes
exact replay. It crosses Memory/SQLite with tampered step payload, tampered Run
payload, missing Session detach, duplicate Session detach, moved Session
detach, and tampered Session payload. The existing exact failed-event payload
check was retained as a characterization-positive case.

RED:

```text
12 failed, 2 passed
The 12 missing checks returned exact replay instead of raising.
The 2 failed-run payload cases were already rejected correctly.

After correcting the SQLite duplicate fixture to retain aggregate sequence
uniqueness, the duplicate-only RED was:
2 failed; Memory and SQLite both returned exact replay.
```

Recovery evidence now retains the relevant Session lifecycle event and its
global cursor. Terminal replay requires exactly one event, the exact detach or
close type and payload, exact Session version sequence, valid envelope fields,
adjacency immediately after the final Run event, and the same timestamp as the
entire decision batch. Completed, failed, and terminalization-gap branches
also authenticate the exact Run lifecycle payloads. A closing Session exact
replay is covered separately so `session.closed` remains legal.

GREEN:

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_confirm_completed_terminal_replay_authenticates_entire_batch
14 passed in 3.80s

uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_confirm_completed_closes_a_closing_session_atomically
2 passed in 3.22s
```

### I3: recovered empty usage was inconsistent with ready-for-Tool grammar

`ProviderRecoveryResult(usage={})` is a valid strict result. Confirmed Model
projection always records `model.usage.reported`, including when all three
token fields are `None`, but the ready-for-Tool certifier previously allowed a
usage event only when at least one token field was non-null.

The empty-usage Memory/SQLite cases in the first RED above failed at explicit
recovery with `recovery required`. The certifier now requires exactly one
matching usage event for every recovered Model outcome, while retaining the
old conditional rule for ordinary non-recovered Model history. Both cases now
execute the Tool, complete the following Model, and replay the original
decision in the four-case GREEN gate above.

## Review-fix verification

All commands used the explicit executable
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe`.

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
232 passed in 12.93s

uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
610 passed in 90.03s

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
543 passed in 18.04s

uv.exe run --python 3.13 pytest -q
1865 passed in 124.61s; zero skipped, zero failed

uv.exe run --python 3.13 ruff check src tests
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The fresh import/signature/schema smoke passed with 103 unique root exports,
the exact unchanged `RecoveryAPI.resolve` and `ReconciliationService.resolve`
contracts, and SQLite schema version 3. Review-fix scope is exactly
`src/agent_sdk/runtime/recovery.py`,
`tests/integration/runtime/test_reconciliation_resolution.py`, and this
report. There is no dependency, lockfile, migration, schema-version, public
export, roadmap, or progress-ledger change.

No known Phase 5B1 implementation or verification concern remains after the
review fixes. Confirmed Tool outcomes and Workflow projection remain Phase
5B2; Phase 5C, M02-T003, M02-T004, and `TERMINATE` remain untouched. No merge
or push was performed.

## Second independent review closure addendum

The second independent Phase 5B1 review returned C0/I2/M0. Both Important
findings were reproduced on Memory and SQLite before changing production code,
then closed in commit `85856f4` (`fix(recovery): close confirmed replay
history`). This addendum supersedes the preceding post-review risk statement.

### I1: terminal replay coupled historical Session evidence to current state

Terminal replay required the historical `session.run.detached` or
`session.closed` event sequence and status to equal the current Session version
and status. After a confirmed terminal outcome, a later legal Run on the same
active Session increments the Session version; exact replay of the earlier
decision then conflicted even though its atomic history remained intact.

The new production-path matrix confirms a completed or failed Model outcome,
starts and completes a later Run on the same Session, then replays the original
decision on both backends. The existing terminalization-gap test now performs
the same later Session evolution before replay.

RED and GREEN:

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_confirmed_terminal_replay_accepts_later_session_run_evolution \
  tests/integration/runtime/test_reconciliation_resolution.py::test_confirm_completed_terminalization_gap_preserves_model_outcome
6 failed in 4.47s

same command after fix:
6 passed in 3.54s
```

The terminal certifier now validates the historical Session event as its own
projection: exact type, payload, sequence, cursor adjacency, timestamp, and
projected status. The current Session may be that exact projection or a legal
monotonic successor. A detached active Session may later be active, closing,
or closed; a closing projection may later be closing or closed. A historical
closed projection cannot acquire a later successor. A separate corruption case
also confirms that substituting `deleting` as the historical projected status
is rejected as a conflict.

### I2: confirmed replay admitted orphan durable records

Exact confirmed replay authenticated the expected resolution slice but did not
prove a closed world around reconciliation records and Model operations. An
orphan pending reconciliation, orphan resolved reconciliation, or extra
completed Model operation could therefore coexist with a successful replay.
For the supported Tool-call history with one later pending Model
reconciliation, replay checked only the pending envelope and not the complete
current Model lifecycle.

Two adversarial matrices first create real durable history through public SDK
paths, then inject exactly one otherwise-valid orphan record into Memory or
SQLite and require exact replay to conflict without mutation. The terminal
matrix crosses both backends with all three orphan kinds. The later-pending
matrix preserves the canonical success case and crosses the same three orphan
kinds.

RED:

```text
test_confirmed_terminal_replay_rejects_orphan_closed_world_records
6 failed in 3.98s; every orphan replay was incorrectly accepted

test_confirmed_tool_call_later_pending_history_is_closed_world
6 failed, 2 passed in 4.94s; canonical histories passed and every orphan was
incorrectly accepted
```

Confirmed replay now enforces a closed reconciliation grammar: unique request
IDs, a one-to-one exact requested marker for every record, a one-to-one exact
resolved marker for every resolved record, and no resolution marker for a
pending record. Model-operation turns must be unique and exactly cover every
turn through the current checkpoint. Terminal histories permit only the
original resolved request. The single supported later-pending shape permits
only the original resolved request plus the unique current pending request,
requires the pending marker to be the final event, validates the exact current
started Model operation, removes reconciliation markers only for normalized
certification, resequences the retained event stream, and passes the existing
full operation/lifecycle/provider FSM certifier.

GREEN:

```text
test_confirmed_terminal_replay_rejects_orphan_closed_world_records
6 passed in 3.70s

test_confirmed_tool_call_later_pending_history_is_closed_world
8 passed in 4.20s
```

## Second-review verification

All commands used the explicit executable
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe`.

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
248 passed in 15.96s

uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
626 passed in 92.31s

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
543 passed in 18.43s

uv.exe run --python 3.13 pytest -q
1883 passed in 123.40s; zero skipped, zero failed

uv.exe run --python 3.13 ruff check src tests
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The fresh import/signature/schema smoke again passed with 103 unique root
exports, the exact unchanged `RecoveryAPI.resolve` and
`ReconciliationService.resolve` contracts, and SQLite schema version 3. The
implementation commit touched only
`src/agent_sdk/runtime/recovery.py` and
`tests/integration/runtime/test_reconciliation_resolution.py`; this report is
the only documentation change.

No known Phase 5B1 implementation or verification concern remains after the
second review. Confirmed Tool outcomes and Workflow projection remain Phase
5B2; Phase 5C, M02-T003, M02-T004, and `TERMINATE` remain untouched. No merge
or push was performed.

## Third independent review closure addendum

The third independent Phase 5B1 review returned C0/I1/M0. The remaining
Important finding was reproduced on Memory and SQLite before production code
changed, then closed in commit `f1b9ba0` (`fix(recovery): certify confirmed
terminal history`). This addendum supersedes the preceding post-review risk
statement.

### I1: terminal confirmed replay did not certify its complete history

The terminal branch of `_is_confirmed_replay_closed_world` proved the closed
reconciliation grammar, unique Model-operation turns, exact confirmed decision
slice, terminal batch, and final snapshot fields, but never submitted the
complete historical event stream to the lifecycle/provider certifier. A real
confirmed text terminal Run could therefore retain a valid envelope and exact
terminal batch while an earlier `step.started`, `model.call.started`,
`model.usage.reported`, or `model.call.completed` marker was missing,
duplicated, or moved. Exact replay still succeeded.

The new adversarial matrix creates a real two-turn Run: turn 0 produces text,
usage, a Tool call, and an executed Tool result; turn 1 is interrupted in
Model flight and resolved with confirmed terminal text. It changes only one
turn-0 lifecycle marker, preserving event IDs, sequences, global cursors, the
confirmed atomic suffix, and the final Session transition. It crosses four
marker kinds, three corruptions, and both Stores. Each replay must conflict
without durable mutation. A separate positive matrix retains the complete
multi-turn Tool history and requires exact replay to succeed on both Stores.

RED:

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_confirmed_terminal_replay_authenticates_complete_lifecycle_history
24 failed in 6.94s
All 24 missing/duplicate/moved corruptions were incorrectly accepted.

uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py::test_confirmed_terminal_replay_accepts_complete_multiturn_tool_history
2 passed in 3.37s
```

### Single-FSM implementation and scope boundary

The fix does not add a second lifecycle state machine.
`_is_valid_certified_lifecycle_positions` remains the single event-order FSM.
Its existing non-terminal path and default arguments are unchanged. An
optional terminal mode adds only the completed/failed terminal states and
collects per-Model delta/usage evidence while the same FSM continues to
authenticate step, Model, Tool, permission, recovery-control, and failure
ordering and payloads. Recovered Model IDs distinguish authoritative or
operator-confirmed outcomes, which require exact usage and no fabricated text
deltas.

`_is_exact_confirmed_terminal_history` constructs a certification-only view.
For a direct terminal confirmation it removes exactly the current
`run.interrupted`, requested marker, and resolved marker. For an earlier
confirmed Tool call followed by later normal completion it reuses
`_effective_resolved_evidence`, including its established rule that moves the
interruption behind the confirmed Model terminal markers and before the later
recovery. The durable history is never rewritten.

After the lifecycle FSM succeeds, the terminal provider helper performs no
state transitions. It reuses `_messages_before_turn` to authenticate every
prior Model request fingerprint and Tool-result/message relation, validates the
final Model request, and compares the reconstructed output, usage, messages,
Tool results, terminal event payload, Run, and checkpoint projection. Thus the
new helper is a projection certifier layered after the one lifecycle FSM, not a
duplicate FSM.

The optional terminal arguments are supplied only by confirmed terminal exact
replay. Phase 5A reconciliation and the supported unique later-pending path
continue to call `_is_resolution_operation_certified` and
`_is_valid_certified_provider_history` with their original defaults. Store
contracts and durable schemas are unchanged. Phase 5B2 behavior is not
admitted.

GREEN and compatibility:

```text
new positive and corruption matrices:
26 passed in 4.64s

new matrices plus confirmed Tool later-terminal compatibility, direct
completed/failed projection, and terminalization-gap compatibility:
38 passed in 6.28s
```

## Third-review verification

All commands used the explicit executable
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe`.

```text
uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py
276 passed in 18.67s

uv.exe run --python 3.13 pytest -q \
  tests/integration/runtime/test_reconciliation_resolution.py \
  tests/integration/storage/test_run_progress_reconciliation.py \
  tests/unit/runtime/test_provider_recovery.py \
  tests/integration/runtime/test_provider_recovery_live.py \
  tests/integration/runtime/test_provider_recovery_execution.py \
  tests/integration/runtime/test_tool_recovery_execution.py \
  tests/integration/runtime/test_recovery_api.py
654 passed in 92.05s

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
543 passed in 18.46s

uv.exe run --python 3.13 pytest -q
1909 passed in 126.26s; zero skipped, zero failed

uv.exe run --python 3.13 ruff check src tests
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files

git diff --check
exit 0; only Windows LF-to-CRLF informational warnings
```

The fresh import/signature/schema smoke again passed with 103 unique root
exports, exact unchanged `RecoveryAPI.resolve` and
`ReconciliationService.resolve` signatures, and SQLite schema version 3. The
implementation commit touched only
`src/agent_sdk/runtime/recovery.py` and
`tests/integration/runtime/test_reconciliation_resolution.py`; this report is
the only documentation change.

No known Phase 5B1 implementation or verification concern remains after the
third review. Confirmed Tool outcomes and Workflow projection remain Phase
5B2; Phase 5C, M02-T003, M02-T004, and `TERMINATE` remain untouched. No merge
or push was performed.
