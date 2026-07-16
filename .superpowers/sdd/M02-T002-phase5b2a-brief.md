# M02-T002 Phase 5B2A Implementation Brief

Source of truth: `docs/plans/tasks/M02-T002-leases-reconciliation.md`,
`.superpowers/sdd/M02-T002-phase5-plan.md`, the approved Phase 5A and Phase 5B1
reports, and their closed recovery grammar. This operational sub-slice
implements confirmed Tool outcomes only. Workflow projection is Phase 5B2B;
Phase 5C, T003, and T004 remain out of scope.

## Required public behavior

Keep the existing public contract and context-free error boundary:

```python
await sdk.recovery.resolve(
    request_id,
    ReconciliationAction.CONFIRM_COMPLETED,
    actor={...},
    evidence={"tool_result": ...},
)
```

For an operation-linked Tool reconciliation request, evidence must contain
exactly `tool_result`. Strictly and detachably reconstruct one bounded
`ToolResult`. Reject key/value coercion, extra fields, non-string identities or
content/error, invalid status, non-finite/non-JSON value, oversized content or
error, and any result whose `call_id` or `tool_name` differs from the exact
certified pending call. The durable `ToolResult` model remains the outcome
shape, but public evidence admission must preflight raw types so Pydantic
coercion cannot change operator evidence.

All `ToolResultStatus` values are legal when their complete strict model is
valid. A prior handler result that was not JSON-compatible is represented only
by the exact bounded normalized failure `ToolResult` produced by the existing
Tool executor semantics; raw arbitrary Python objects are never accepted as
evidence.

Invalid evidence returns the constant nonretryable `INVALID_STATE` decision
error with zero mutation. `TERMINATE` remains unsupported. Resolution must not
call a Tool handler, MCP transport, permission bridge, LiteLLM, Provider
recovery adapter, hook, or other application callback.

## Admission and exact Tool relation

Retain every approved Phase 5A/5B1 admission, replay, sanitization, lease, and
closed-world guarantee. A Tool confirmed outcome is admitted only when:

- the unique pending request is linked to the exact certified `STARTED`
  `ToolCallOperation`, `TOOL_IN_FLIGHT` checkpoint, same turn, Run, Session,
  request fingerprint, capability hash, retry metadata, and latest exact
  `reconciliation.requested` suffix;
- the pending `ToolCallCompleted` is reconstructed from the exact preceding
  certified Model outcome/checkpoint transcript and its call id/name match the
  submitted `ToolResult`;
- the durable execution descriptor, current Tool registration/spec/capability,
  policy descriptor, Session ownership, Run/checkpoint/operation/event grammar,
  and every prior resolved attempt remain exact;
- no orphan/non-unique reconciliation record, operation, audit marker, moved or
  duplicate lifecycle marker, partial projection, or capability drift exists.

## Canonical atomic Tool projection

Project the result exactly as normal `_RunEmitter.complete_tool` followed by the
normal step completion boundary, without invoking the Tool:

- transition the `ToolCallOperation` to `COMPLETED` only for
  `ToolResultStatus.SUCCEEDED`; use `FAILED` for every other result status;
- set operation outcome to the exact detached `ToolResult.model_dump(json)`;
- append the exact Tool message (`role`, `tool_call_id`, `name`, `content`) to
  checkpoint messages;
- append the exact ToolResult to checkpoint `tool_results`;
- increment checkpoint turn and version, project `READY_FOR_MODEL`, and clear
  `operation_id`;
- project Run `WAITING_RECONCILIATION -> INTERRUPTED`, incrementing its version
  while retaining Session ownership;
- append the exact `reconciliation.resolved` audit, then
  `tool.call.completed` with the complete result payload, then
  `step.completed` with `{}`, preserving contiguous Run sequence and exact
  timestamps/cursors.

The request resolution, operation, checkpoint, Run, and events are one
`RunProgressBatch` on Memory and SQLite with exact request, current lease,
Session, Run, checkpoint, operation, request-event, and event-envelope
preconditions. The Session is not detached because the Run remains explicitly
recoverable. Extend the old-generation Store exception only for this exact
legal Tool `CONFIRM_COMPLETED` batch. Ordinary operation transitions and all
Phase 5A/5B1 exceptions remain unchanged.

After the decision, only an explicit `recover_run` may continue. It must start
at `READY_FOR_MODEL`, must never re-invoke the confirmed Tool call, and may call
the Provider for the next Model turn. Tool failure/denied/timeout/invalid-result
statuses are durable Tool results supplied to the next Model turn; they do not
implicitly terminate the Run.

## Stable replay and closed history

An exact replay must return the durable resolution after verifying the complete
paired projection. A different replay conflicts. Exact replay must remain valid
after explicit recovery adds later Model/Tool turns, after the Run terminates,
or after a later independent reconciliation request is created. The original
confirmed Tool decision slice must be consumed exactly once and normalized at
its original turn; all later history must pass the existing full lifecycle,
Provider, Tool, policy, permission, operation, checkpoint, Run, Session, and
reconciliation closed-world certifiers.

Post-commit ambiguity and two-SDK races converge only after exact paired-state
verification. Session close/delete and SDK close cannot leave a half-resolved
decision or cause external work. A closing Session retains the active Run until
subsequent explicit recovery reaches a terminal state; deletion/busy ownership
rules remain unchanged.

## Mandatory TDD and adversarial coverage

Write production-path failing tests first and record exact RED/GREEN evidence.
Cover at least:

- Memory and SQLite: succeeded scalar/object/list/null results; normalized Tool
  failure including the non-JSON-result failure; denied, timed-out, and invalid
  arguments; exact content/value/error preservation;
- raw evidence strictness: coercions, extras, wrong call/tool, invalid/fake
  status, non-finite/non-JSON values, size boundaries, mutable-source detachment,
  and secret-safe constant public errors;
- exact replay, changed replay conflict, two SDKs, lease/CAS races,
  post-commit ambiguity, cancellation, SDK close, Session closing/delete, and
  Memory/SQLite atomic parity;
- zero callbacks during `resolve`, including Tool, MCP, permissions, Provider,
  LiteLLM, and hooks;
- explicit recovery starts the next Model turn exactly once and never repeats
  the confirmed Tool side effect, for both success and failure results;
- replay after later completion, later Tool turn, and later reconciliation;
- corruption matrices for request/operation/checkpoint/call/fingerprint/
  capability/policy/Tool registration, missing/duplicate/moved Tool lifecycle
  markers, orphan records/operations, partial streams, and prior resolved
  attempts;
- exact Store rejection of malformed old-generation Tool confirmation batches
  with zero partial mutation on both backends;
- all Phase 5A and Phase 5B1 regression matrices remain green.

Prefer focused additions to
`tests/integration/runtime/test_reconciliation_resolution.py` and
`tests/integration/storage/test_run_progress_reconciliation.py`, with narrower
model/contract tests where the invariant belongs. Use production scanner and
recovery paths rather than fabricated states whenever reachable.

## Gates and handoff

Run focused tests, all reconciliation/Provider/Tool-recovery/RecoveryAPI files,
the Phase 5A/5B1 and lease/Session neighbors, Ruff, mypy,
imports/signatures/schema/diff/scope, and the full Python 3.13 suite. Create
`.superpowers/sdd/M02-T002-phase5b2a-report.md` with changed files, RED/GREEN,
exact gates, residual Phase 5B2B scope, and commit hashes. Commit all work and
obtain independent Spec C0/I0 and Quality C0/I0 before Phase 5B2B.

Do not directly mutate Workflow snapshots in this sub-slice. Do not implement
Workflow projection, Phase 5C fault E2E, T003, or T004.
