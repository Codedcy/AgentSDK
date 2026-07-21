# v0.1 Whole Review Blocker 2: Recovery Abort Report

Baseline: `4216268`

## Outcome

The public recovery API now accepts `ReconciliationAction.TERMINATE` for an
unknown in-flight external operation. The application must provide a bounded,
non-empty reason. The SDK resolves the exact pending request atomically and
terminally fails the Run with the stable, non-retryable error code
`application_resolution_aborted`, without calling the provider or Tool again.

The recorded external-operation outcome remains explicitly unknown. Abort does
not claim that the external operation completed, failed externally, or never
executed, and it does not add an exactly-once guarantee.

## Durable contract

- The resolution reason is normalized, must contain only `{"reason": str}`,
  and is limited to 256 UTF-8 bytes after sanitization.
- Actor metadata is canonical JSON, recursively rejects secret-bearing keys,
  redacts recognizable bearer/credential assignments in string values, and is
  limited to 1,024 UTF-8 bytes.
- The existing `commit_run_progress` transaction and CAS preconditions are
  reused for both Memory and SQLite. No new table, Store protocol, transaction,
  state machine, or persistence mechanism was introduced.
- One atomic batch resolves the reconciliation request, closes the external
  operation with `outcome_known: false`, writes a terminal checkpoint, fails the
  Run, updates Session ownership, and appends the legal event sequence:
  `reconciliation.resolved`, `step.failed`, `run.failed`, then Session detach or
  close.
- Abort intentionally emits neither `tool.call.failed` nor
  `model.call.failed`: the unknown external attempt is not reclassified as an
  observed external failure.
- Exact repetition is idempotent after Store reopen. A changed reason, actor, or
  action conflicts through the existing reconciliation contract and never
  replays the operation.
- Recovery trace projection permits the already-interrupted Run/Child stage to
  reach its final failed state and correlates the legacy terminal step event
  with its version-2 start event. No new trace stage, field, or attribution rule
  was added.

## Implementation shape and scope control

The production change is larger than a single action branch because the durable
resolution must be authenticated independently at three fail-closed boundaries:

1. runtime validation and construction of the canonical terminal projection;
2. Store-side validation of the complete atomic batch for both backends; and
3. closed-world history validation used to recognize an exact replay after
   process/Store reopen.

Those boundaries deliberately share one termination projection and one strict
batch validator. The remaining code at each boundary is specific to event
construction, persistence admission, or historical authentication and cannot be
collapsed without weakening the existing CAS/recovery model. The repair does
not introduce distributed recovery, new recovery actions, background workers,
or a broader trace redesign.

## TDD evidence

1. Initial focused RED: 14 expected failures across Memory and SQLite. Every
   terminate case failed at `reconciliation action is not supported`.
2. Core GREEN: 16 passed, 358 deselected. Coverage includes unknown Tool work,
   zero provider/Tool calls, event order, terminal Run/checkpoint/operation,
   sanitized reason and actor values, empty/oversized/secret-bearing metadata,
   reopen, exact repetition, and changed-resolution conflict.
3. Public trace RED then GREEN: abort initially appeared as internal-only
   because an interrupted version-2 stage was followed by a legacy terminal
   event. The recovery-compatible transition/correlation repair made public Run
   and Recovery stages observable; the focused projection checks pass.
4. Documentation RED then GREEN: the release ledger test first failed because
   README did not expose TERMINATE; it passes after README, quickstart, recovery
   guide, changelog, and release ledger were corrected.
5. The single v0.1 subprocess acceptance now exercises both the existing retry
   branch and a fresh-SQLite abort branch. Abort verifies terminal failure,
   exact-repeat idempotency, reopen behavior, and zero external replay.

## Verification

- Full reconciliation-resolution suite: `372 passed in 102.32s`.
- Public v0.1 release acceptance: `1 passed in 72.80s`.
- Selected observability, recovery, Workflow, Child, control, documentation, and
  public-release suites: `725 passed in 283.29s`.
- Final focused metadata/reconciliation selection: `16 passed, 358 deselected`.
- Documentation release gate: `1 passed`.
- Ruff: `All checks passed!`
- Strict mypy: `Success: no issues found in 103 source files`.
- `git diff --check`: exit 0 (only Windows line-ending notices).

No version bump, tag, publish, or external side effect was performed.
