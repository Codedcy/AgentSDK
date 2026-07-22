# M02-T002 Phase 3D Operational Plan

Source of truth: `docs/plans/tasks/M02-T002-leases-reconciliation.md`,
`.superpowers/sdd/M02-T002-phase3-plan.md`, and the approved Phase 3C2 range
ending at `e7abe7c`.

## Outcome

Phase 3D closes the two explicitly certified recovery boundaries that Phase 3C
kept conservative:

1. an application-registered provider adapter may authoritatively query an
   unresolved Model operation or resend the *same* provider operation id when
   the persisted and current certifications match exactly; and
2. an unresolved Tool operation may retry only when its full persisted Tool
   capability explicitly recorded an idempotent or safe-retry policy.

LiteLLM remains the only normal model gateway. Provider recovery adapters are
recovery capabilities supplied by the application; they are not an alternate
model router and the SDK ships no inferred/built-in certification. Without an
exact adapter or Tool retry certification, recovery remains one durable
reconciliation request with zero model/Tool/MCP work.

## Slice 3D1 — Provider recovery adapters

Add strict public adapter/request/result models, an identity-safe registry,
live-operation capability stamping, authoritative status query, certified
same-operation-id resend, exact outcome normalization, generation-fenced
operation/checkpoint advancement, and terminal failure handling. Query/resend
attempts are durably observable but never persist request messages, params,
credentials, adapter evidence, or raw provider failures.

The adapter registered before the original call and the adapter registered
after restart must match exact provider identity, adapter id, version, and
capability flags. Old operations with false/missing metadata remain
reconciliation-only. A normal LiteLLM call is never used for recovery.

## Slice 3D2 — Certified Tool retry and Phase 3 release

Add a strict Tool retry policy to `ToolSpec` and therefore to the full Tool
capability hash. Stamp it on new Tool operations. Reconstruct exactly one
pending Tool call from the checkpoint, verify its operation fingerprint and
current capability, re-evaluate permission, and reuse the same durable Tool
operation under a fresh lease only for `idempotent` or `safe_retry`. Default,
legacy, mismatched, unsafe, or unknown Tool outcomes enter reconciliation.

After 3D2, run the entire Phase 3 focused/fault matrix, Python 3.12 and 3.13 full
gates, static/scope/schema audits, write the Phase 3 report, and obtain a fresh
independent C0/I0 Phase 3 review before Workflow recovery begins in Phase 4.

## Common non-negotiable invariants

- capability/descriptor/operation/checkpoint/event evidence is validated before
  any external adapter, provider, Tool, MCP, permission, or Workflow call;
- every external attempt has one stable existing operation id, a fresh lease,
  an audit-start commit before the call, heartbeat/fencing, and an atomic
  detached terminal outcome commit afterward;
- query is repeatable read-only work; resend is allowed only through an exact
  persisted same-operation-id certification; Tool retry is allowed only through
  the exact persisted Tool retry policy;
- cross-SDK losers never invoke adapters, LiteLLM, Tools, MCP, permission, or
  Workflow work and follow the same durable outcome;
- cancellation, adapter/handler failure, lease loss, commit ambiguity, Session
  delete, and SDK close settle tasks/leases and never create a second operation
  or duplicate reconciliation record;
- public errors and durable audit payloads are bounded, stable, cause/context
  free, and secret free;
- SQLite schema remains v3; no migration or Workflow production change is
  permitted in Phase 3D.

## Handoff sequence

Each slice uses strict TDD, a separate implementation commit/report, fresh
final-code gates, and independent Spec/Quality review with C0/I0. Do not begin
3D2 before 3D1 approval and do not begin Phase 4 before the whole Phase 3 review.
