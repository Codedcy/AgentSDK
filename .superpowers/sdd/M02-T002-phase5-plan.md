# M02-T002 Phase 5 Operational Plan

Source of truth: `docs/plans/tasks/M02-T002-leases-reconciliation.md` and the
Phase 5 partition in `M02-T002-phase-plan.md`. This plan resolves the remaining
action ambiguities conservatively and divides Phase 5 into independently
reviewed slices. It does not add T004 cancellation/force-delete behavior.

## Public resolution contract

Add the application entry:

```python
await sdk.recovery.resolve(
    request_id,
    action,
    actor={...},
    evidence={...},
) -> ReconciliationRequest
```

The exact method signature is:

```python
async def resolve(
    self,
    request_id: str,
    action: ReconciliationAction,
    *,
    actor: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> ReconciliationRequest
```

Export `ReconciliationAction`, `ReconciliationRequest`,
`ReconciliationResolution`, and `ReconciliationService` from the package root.
The service and API detach/bound actor/evidence, return context-free errors,
and perform no Provider, Tool, MCP, or permission callback. The application
explicitly invokes `recover_run` or `recover_workflow` after a decision that
leaves work recoverable.

An exact replay of the same request/action/actor/evidence returns the durable
resolved request. A different replay is a bounded nonretryable conflict. Two
SDKs resolving concurrently converge through a fresh Run lease and exact
request/Run/Session/checkpoint/operation/event preconditions.

## Conservative action matrix

### `CONFIRM_NOT_EXECUTED`

Only a unique pending request linked to the exact STARTED operation and matching
MODEL_IN_FLIGHT or TOOL_IN_FLIGHT checkpoint is eligible. Evidence must be
exactly `{"disposition": "not_executed"}`. Atomically:

- resolve the request and append its exact audit event;
- refence and terminalize the old operation as a reconciliation outcome;
- rewind the checkpoint to READY_FOR_MODEL or READY_FOR_TOOL at the same logical
  turn;
- move WAITING_RECONCILIATION to INTERRUPTED while retaining Session ownership.

The next explicit recovery creates a new operation attempt. Resolution itself
does not call the external system.

### `RETRY`

The same transition is allowed only with exact evidence
`{"acknowledge_duplicate_side_effect_risk": true}`. This is the user's explicit
acceptance that the unknown old call may already have executed. It never becomes
an inferred or automatic retry.

### `CONFIRM_COMPLETED`

Only an exact operation-linked request with certified history/checkpoint is
eligible. Evidence is kind-specific and extra-forbid:

- Model: `{"provider_result": ...}` where the value strictly reconstructs a
  `ProviderRecoveryResult` with disposition `completed` or `failed`. Completed
  requires exact bounded text/finish reason/usage and at most one exact Tool
  call. Failed requires exact public error code and retryability.
- Tool: `{"tool_result": ...}` where the value strictly reconstructs a bounded
  `ToolResult` whose call id and Tool name match the certified pending call.
- The existing completed-model terminalization gap accepts only evidence exactly
  equal to its already durable normalized operation outcome.

One atomic resolution batch writes the decision audit, operation outcome,
checkpoint, Run projection, and Session detach when terminal. Model completion
with a Tool call yields READY_FOR_TOOL/INTERRUPTED; Tool completion yields
READY_FOR_MODEL/INTERRUPTED; Model completion without a Tool call completes and
detaches the Run; a failed Provider result fails and detaches the Run. No
external callback occurs inside `resolve`.

### Rejected actions/states

- `TERMINATE` is rejected with constant INVALID_STATE and zero mutation. Durable
  cancellation/termination and forced detach remain M02-T004.
- Requests without an operation id, legacy/missing checkpoints, non-unique or
  corrupt requests, unavailable Session ownership, wrong phase/turn/kind/
  fingerprint, malformed evidence, or non-current capabilities are rejected
  before mutation.
- Resolution never accepts a public result by trusting arbitrary text; it must
  reconstruct the existing strict Provider/Tool result models and certified
  execution relation.

## Certified history and storage rules

- Add `reconciliation.requested` and `reconciliation.resolved` to the closed Run
  recovery grammar. Consume each request/resolution exactly once and cross its
  id, operation, action, actor, evidence, Session/Run ownership, event id,
  sequence, and timestamp against the durable request record.
- A resolved old attempt is bound by request id to one operation. It is excluded
  from the logical current-attempt count, so a new operation at the same turn is
  legal without weakening validation of ordinary histories.
- Permit a STARTED operation from an old lease generation to be terminalized by
  the current resolution lease only in the same `RunProgressBatch` as the exact
  pending-to-resolved request transition and legal action/checkpoint/Run
  projection. Ordinary operation transitions remain generation-exact.
- The entire decision is atomic on Memory and SQLite. Post-commit ambiguity
  reloads only an exact resolved request and exact paired state; otherwise it
  returns a constant conflict. Session close/delete and SDK close cannot leave a
  half-resolved request or execute external work.

## Phase 5A - Admission and explicit safe retry decisions

- Add the service/API/models exports and strict resolution admission.
- Implement `CONFIRM_NOT_EXECUTED` and explicit `RETRY` only.
- Extend Store atomic validation and certified event grammar for resolved retry
  attempts.
- For this slice, `CONFIRM_COMPLETED` and `TERMINATE` return the planned constant
  unsupported error with zero mutation.
- Cover Memory/SQLite, two SDKs, exact replay/conflict, lease/CAS and
  post-commit ambiguity, Session/SDK lifecycle, secret sanitization, Model and
  Tool attempts, and subsequent explicit recovery.
- Commit and obtain independent Spec C0/I0 and Quality C0/I0 before Phase 5B.

## Phase 5B - Confirmed external outcomes

- Implement the strict Model/Tool `CONFIRM_COMPLETED` evidence schemas and
  atomic safe/terminal projections above.
- Cover Model text completion, Model ToolCall completion, Provider failure,
  Tool success/failure/non-JSON bounded result, completed-model terminalization
  gap, Workflow node projection, Memory/SQLite, two SDKs, ambiguity, corruption,
  capability drift, Session close/delete, SDK close, and zero callbacks during
  resolution.
- Commit and obtain independent Spec C0/I0 and Quality C0/I0 before Phase 5C.

## Phase 5C - Fault/E2E and release gate

- Replace the remaining fabricated single-SDK INTERRUPTED test with a reachable
  production scanner path.
- Add subprocess fault tests for hard process exit after Provider acceptance,
  after Tool side effect, and after a safe Tool outcome commit. Reopen SQLite,
  advance the scanner through a controlled clock, and prove unknown outcomes do
  not replay while safe checkpoints do resume.
- Add E2E coverage for explicit reconciliation decisions, recovered Workflow
  projection, Session closing/delete ownership, and no duplicate Provider/Tool/
  MCP side effects by default.
- Run all M02-T001 regressions, focused fault/E2E matrices, full Python 3.12 and
  3.13 with zero skips, Ruff, mypy, diff/import/scope/schema, sdist/wheel build,
  clean-environment wheel import on both Python versions, and
  `python -m examples.reference_cli.main --help` with no Store/model execution.
- Write the final M02-T002 report and obtain a fresh whole-task C0/I0 review.
  Only after approval update task/progress ledgers and move M02-T003 to
  in-progress; do not implement T003/T004 in this slice.

## Phase 5 release condition

Phase 5 and M02-T002 are complete only after all three slices are committed,
every independent review has no Critical/Important finding, the final dual
Python/package gates pass, and the task ledger records the approved evidence.
