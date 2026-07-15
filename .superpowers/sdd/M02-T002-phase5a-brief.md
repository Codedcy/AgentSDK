# M02-T002 Phase 5A Implementer Brief

## Objective

Implement strict reconciliation admission plus explicit
`CONFIRM_NOT_EXECUTED` and `RETRY` transitions from
`M02-T002-phase5-plan.md`. Phase 4 is approved at `20cf09e` with approval ledger
`975ed73`. Do not implement `CONFIRM_COMPLETED`, T004 termination/cancellation,
subprocess release gates, M02-T003, or M02-T004 in this slice.

## Required API and exports

- Add exact `RecoveryAPI.resolve(request_id, action, *, actor, evidence)` and
  `ReconciliationService.resolve(...)` returning a detached
  `ReconciliationRequest`.
- Export `ReconciliationAction`, `ReconciliationRequest`,
  `ReconciliationResolution`, and `ReconciliationService` from `agent_sdk`.
- Preserve every existing public signature and behavior.
- Resolution is lifecycle-admitted and startup-scan-safe; it invokes no
  Provider, Tool, MCP, permission bridge, or Workflow coordinator.

## TDD matrix

Start with public RED tests, then production changes. Cover Model and Tool,
Memory and SQLite, and two independently constructed SDKs for:

1. Exact NOT_EXECUTED resolution to a safe checkpoint, followed by explicit
   recovery that performs exactly one new external attempt.
2. Exact RETRY acknowledgement, followed by explicit recovery; resolution does
   zero external work and the later retry is attributable to the user action.
3. Same decision replay returns the same event/request with no cursor change;
   changed action/actor/evidence conflicts with no mutation.
4. Two-SDK concurrent same decisions converge; different decisions produce one
   winner and one bounded conflict.
5. Post-commit Store ambiguity accepts only exact paired Run/checkpoint/
   operation/request/events and never emits a second decision.
6. Lease held/lost/expired, capability changes, Session close/delete, SDK close,
   cancellation, and CAS conflicts never create a partial decision or external
   call.
7. Wrong/missing/legacy request, operation, phase, turn, kind, Session ownership,
   fingerprint, event sequence/id, actor/evidence shape, or duplicate pending
   request fails before mutation.
8. `CONFIRM_COMPLETED` and `TERMINATE` are constant INVALID_STATE with zero
   mutation in Phase 5A.
9. Public messages, cause/context, formatted traceback, and retained SDK frame
   locals contain no actor/evidence/Store/capability secrets.

## Atomic state requirements

Use one current Run lease and one `RunProgressBatch` to write the resolved
request/event, terminalized old operation, rewound checkpoint, INTERRUPTED Run,
and required recovery-control events. The Session retains the Run. Store
validation may allow old-generation operation terminalization only in this
exact resolution batch. Exact replay is accepted; partial/mixed replay is not.

Extend the closed recovery grammar to authenticate requested/resolved pairs and
resolved prior attempts. A subsequent new operation at the same logical turn is
valid only when the prior operation is uniquely bound to the exact resolved
request. Do not relax ordinary event, operation, permission, ToolResult, or
Provider history validation.

## Gates and handoff

Run focused Phase 5A, Phase 2 Store records, Phase 3 Provider/Tool recovery,
RecoveryAPI/live/lease/reconciliation/Session/idempotency neighbors, Phase 4
Workflow recovery, full Python 3.13 zero-skip, Ruff, mypy, diff/import/scope/
schema/signature checks. Write `M02-T002-phase5a-report.md`, commit, and stop for
independent review. Do not begin Phase 5B.
