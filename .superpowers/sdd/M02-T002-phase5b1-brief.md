# M02-T002 Phase 5B1 Implementation Brief

Source of truth: `docs/plans/tasks/M02-T002-leases-reconciliation.md`,
`.superpowers/sdd/M02-T002-phase5-plan.md`, and the approved Phase 5A
implementation/report. This is the first operational sub-slice of Phase 5B. It
implements confirmed Model outcomes only. Tool outcome projection and Workflow
projection remain Phase 5B2; `TERMINATE` remains M02-T004.

## Required public behavior

Keep the existing public signature and context-free error boundary:

```python
await sdk.recovery.resolve(
    request_id,
    ReconciliationAction.CONFIRM_COMPLETED,
    actor={...},
    evidence={"provider_result": ...},
)
```

For an operation-linked Model reconciliation request, evidence must contain
exactly one key, `provider_result`. Strictly reconstruct
`ProviderRecoveryResult` from its value. Only `completed` and `failed`
dispositions are admitted. The existing strict, bounded, detached,
extra-forbid model is the schema; no coercion, extra field, non-finite JSON,
unbounded text/finish reason/arguments, multiple Tool calls, or non-public error
code is accepted. Invalid evidence returns the constant nonretryable
`INVALID_STATE` decision error with zero mutation. Actor remains nonempty and
bounded by `ReconciliationResolution`.

Do not call LiteLLM, Provider recovery adapters, Tools, MCP, permissions, hooks,
or any application callback during resolution.

## Admission and certified relation

Retain every Phase 5A admission guarantee. In addition:

- A normal unknown Model outcome must be the unique pending request bound to
  the exact certified `STARTED` `ModelCallOperation`, `MODEL_IN_FLIGHT`
  checkpoint, same turn/fingerprint/provider/capability relation, exact
  request/audit suffix, owned `WAITING_RECONCILIATION` Run, and active Session.
- Live execution descriptor/capability drift must fail closed before mutation.
- The completed-model terminalization gap is admitted only for the existing
  certified reason `model_call_completed_terminalization_unknown`: the exact
  durable `COMPLETED` Model operation, `READY_FOR_MODEL` checkpoint, no Tool
  call, matching assistant message/output/usage and exact normal event suffix.
  Its evidence must normalize exactly to the already durable operation outcome;
  it is terminalization only, never an outcome rewrite.
- Extend resolved-history validation so the new action/event/projection is
  consumed exactly once and cannot weaken the Phase 5A attempt slicing or the
  closed lifecycle/provider grammar.

## Canonical Model projection

Normalize a completed Provider result to the same operation outcome used by
normal execution:

```text
{
  "finish_reason": result.finish_reason,
  "text": result.text,
  "tool_calls": [] or [
    {"index", "call_id", "name", "arguments_json"}
  ],
  "usage": result.usage
}
```

Use the repository's frozen JSON representation at durable boundaries. Append
the assistant message in the same shape as `_RunEmitter.complete_model`, append
Model text to `checkpoint.output_parts`, and add operation usage to cumulative
checkpoint usage with the same token semantics as normal execution.

For `completed` with one Tool call, atomically:

- resolve the request and append the exact `reconciliation.resolved` audit;
- transition the Model operation to `COMPLETED` with the canonical outcome;
- project the checkpoint to `READY_FOR_TOOL`, clear `operation_id`, append the
  assistant message/text/usage;
- project the Run from `WAITING_RECONCILIATION` to `INTERRUPTED`, retaining
  Session ownership;
- append the normal `model.usage.reported` audit when normal execution would,
  followed by `model.call.completed`; maintain contiguous Run sequences.

For `completed` without a Tool call, the same atomic batch additionally appends
the normal `step.completed` and `run.completed` lifecycle, projects a valid
terminal `COMPLETED` Run with output/usage/prior Tool results, terminalizes the
checkpoint, and detaches the Run from the Session using the existing exact
Session transition/event. A closing Session must close exactly as normal; a
deleted/missing/non-owning Session fails before mutation.

For `failed`, atomically:

- resolve the request and append the exact decision audit;
- transition the Model operation to `FAILED` with the same normalized public
  error outcome as normal Model failure;
- append `model.call.failed`, `step.failed`, and `run.failed` with the existing
  public error payload shape;
- terminalize the checkpoint and project a valid terminal `FAILED` Run using
  the public error code/retryability and a constant public message;
- detach/close the Session through the existing exact transition/event.

The completed-model terminalization gap performs only the terminal Run,
checkpoint, lifecycle, Session-detach, and reconciliation writes; the durable
operation outcome and already-recorded Model completion/message/usage remain
byte-for-byte unchanged and are not emitted twice.

Every decision above must be one `RunProgressBatch` on Memory and SQLite, with
exact request, Run, Session, checkpoint, operation, event, and current-lease
preconditions. Extend the narrowly scoped old-generation Store exception only
for these exact legal `CONFIRM_COMPLETED` shapes. Ordinary transitions remain
generation-exact. Post-commit ambiguity may converge only after verifying the
exact resolved request and the exact paired durable projection.

## Mandatory TDD and adversarial coverage

Write failing tests first and record RED/GREEN evidence in the report. Cover at
least:

- Memory and SQLite: completed text, completed Tool call, Provider failure, and
  completed-model terminalization gap;
- exact public replay, different replay conflict, two-SDK convergence, lease
  race, CAS race, and post-commit ambiguity;
- malformed/extra/coerced/unbounded evidence, unsupported dispositions,
  non-finite/non-object Tool arguments, wrong operation/kind/turn/phase,
  duplicate/orphan/moved audit markers, corrupt operation/checkpoint/history,
  capability drift, Session closing/delete/nonownership, and SDK close;
- proof that zero Provider/Tool/MCP/permission/application callback occurs;
- terminal Run/Session/checkpoint/operation/request/event consistency and safe
  subsequent explicit recovery for the Tool-call branch;
- existing Phase 5A regression matrices remain green.

Prefer focused additions to
`tests/integration/runtime/test_reconciliation_resolution.py` and
`tests/integration/storage/test_run_progress_reconciliation.py`; add narrower
unit/contract tests where the invariant belongs. Do not fabricate production
states when a production scanner/recovery path can create them.

## Gates and handoff

Run focused tests, all reconciliation/storage recovery tests, Ruff, mypy,
imports/signatures/schema/diff/scope checks, then the full Python 3.13 suite.
Create `.superpowers/sdd/M02-T002-phase5b1-report.md` containing changed files,
RED/GREEN evidence, exact gate output, known residual Phase 5B2 scope, and the
implementation commit hash. Commit all Phase 5B1 work. Do not implement Tool
confirmed outcomes, Workflow projection, Phase 5C fault/E2E, T003, or T004.
