# M02-T002 Phase 3D1 Brief — Certified Provider Recovery

Required base: approved Phase 3C2 plus its approval ledger commit `89b6b42`.
Read `.superpowers/sdd/M02-T002-phase3d-plan.md` and the source task completely
before editing.

## Public capability surface

Expose strict, frozen, extra-forbid, detached public models from a focused
runtime module and package exports:

- `ProviderRecoveryAdapter`: exact nonempty bounded `provider_identity`,
  `adapter_id`, and `version`; optional authoritative-status callable; optional
  same-operation-id-resend callable; explicit booleans must agree with callable
  presence. Registration itself is the application certification.
- `ProviderRecoveryRequest`: exact Run/Session/turn/operation identity,
  provider identity, request fingerprint, and detached reconstructed
  `ModelRequest`. It contains the original operation id for both query and
  resend. It is never persisted or exposed through trace/event payloads.
- `ProviderRecoveryDisposition`: `completed`, `failed`, `not_executed`,
  `pending`, or `unknown`.
- `ProviderRecoveryResult`: disposition plus exactly the fields legal for that
  disposition. Completed contains normalized finish reason, bounded text, zero
  or one exact Tool call, and strict `TokenUsage`. Failed contains only a stable
  SDK error code/category and retryable flag, never a raw provider exception or
  message. Other dispositions contain no outcome.
- an identity-safe registry available through `sdk.recovery` with deterministic
  list, duplicate conflict, exact get, and expected-identity unregister.

The implementation may refine names while preserving these semantics. No
provider adapter is registered by default. `AgentSDK.for_test` may expose only
the minimum hook needed for deterministic conformance tests; normal
applications register through the public recovery API.

## Persisted certification

Before every *new live* LiteLLM model operation, resolve the exact adapter for
`ModelRequest.model` and persist bounded recovery metadata containing adapter
id, version, authoritative-status flag, and same-operation-id-resend flag. No
callable, request, credential, or arbitrary adapter metadata is persisted.

If no adapter exists, persist the current conservative false capabilities. An
operation created before 3D1, with missing/false metadata, remains conservative
even if an adapter is registered after restart. Recovery requires exact equality
between persisted metadata and the current registered adapter. Adapter changes
or malformed metadata cause bounded reconciliation with zero adapter/LiteLLM
calls; they are not guessed or silently upgraded.

## Exact recovery request

For one authoritative `STARTED ModelCallOperation` linked to a
`MODEL_IN_FLIGHT` checkpoint:

1. validate current Run/Session ownership, execution descriptor/capabilities,
   exact checkpoint/operation/event relationship, no conflicting pending
   request, and exact adapter certification;
2. reconstruct `ModelRequest` only from the validated execution descriptor,
   checkpoint messages, registered Tool schemas, and Agent params;
3. recompute and require the exact stored request fingerprint before any adapter
   call; never rebuild from deltas or arbitrary event payloads;
4. acquire a fresh lease and atomically re-fence the *same* STARTED operation id
   to the new generation, retain the exact checkpoint as a precondition, and
   append a bounded `model.recovery.query.started` or
   `model.recovery.resend.started` audit event before external work.

Cross-SDK lease losers attach/follow and make zero adapter/LiteLLM calls.

## Query and resend decision table

- Exact authoritative query returning `completed`: validate the result and
  atomically complete the same operation/checkpoint with normal
  `model.call.completed`/usage semantics; then continue through the already
  approved safe checkpoint path without another provider call for that turn.
- Query returning `failed`: atomically fail the same operation and lifecycle-
  terminal Run, detach Session ownership in the same commit, and expose only a
  stable sanitized Run failure.
- Query returning `not_executed`: resend only if the exact persisted/current
  adapter also certifies same-operation-id resend; otherwise reconcile.
- Query returning `pending` or `unknown`, an invalid/malformed result, timeout,
  or adapter failure: reconcile once without resend.
- If no status callable exists but exact same-operation-id resend is certified,
  call resend directly with the original operation id.
- Resend returning `completed` or `failed` uses the same terminal paths. Any
  other result or adapter failure reconciles once.

Never call `LiteLLMGateway.stream`/`litellm.acompletion` from provider recovery.
Never infer support from LiteLLM, model names, timeouts, missing usage, or a
currently registered adapter that was not certified in the original operation.

## Atomicity, lifecycle, and observability

- query/resend audit-start, operation re-fence, exact Run/Session/event tail,
  and checkpoint precondition commit together before the adapter call;
- completed outcome, operation transition, checkpoint transition, normalized
  events, and all adjacent CAS checks commit together under the current lease;
- failed outcome, operation transition, terminal checkpoint, Run failure,
  Session detach, and events commit together;
- crash/cancel after audit-start leaves the same STARTED operation recoverable;
  a later exact adapter may repeat query or certified same-id resend safely;
- ambiguous commits use exact replay, while precommit/CAS/lease failure performs
  no partial mutation and no unbounded follow loop;
- every event includes only adapter id/version, operation id, action, and bounded
  disposition/error category. Do not emit request fingerprint if it would add no
  user value, and never emit messages, params, Tool schemas, raw outcome,
  evidence, or exception text;
- adapter exceptions and invalid values are deleted before raising public
  constant errors; traceback/cause/context and background tasks retain no
  request, credential, adapter result, or arbitrary secret.

## Permitted production scope

- a focused provider-recovery runtime module and exports;
- `runtime/reconciliation.py` only for strict public result/value models if not
  kept in the focused module;
- `models/litellm_gateway.py` only to share exact request fingerprint or detached
  request validation; normal LiteLLM behavior stays unchanged;
- `runtime/engine.py` for live certification stamping and applying a certified
  recovered outcome under an existing lease;
- `runtime/recovery.py`, `api.py`, and package exports for planning, registry,
  execution, lifecycle, and public registration;
- existing storage interfaces/Memory/SQLite only if an atomic existing-record
  re-fence/transition invariant needs strengthening; schema and migrations are
  forbidden;
- focused unit/integration/fault/e2e tests and the 3D1 report.

Forbidden: built-in provider-specific implementation, non-LiteLLM normal model
gateway, arbitrary provider evidence persistence, reconciliation resolution
actions, Tool retry-policy production changes (3D2), Workflow production,
schema/migrations, roadmap/milestone/task-index edits.

## Required TDD evidence

1. registry validation, duplicate/list/get/expected-unregister, detached models,
   public imports, and default no-adapter behavior;
2. live operation stamps exact adapter id/version/flags before LiteLLM, while no
   adapter stamps conservative false metadata;
3. adapter registered only after crash, metadata mismatch, malformed metadata,
   request fingerprint mismatch, descriptor mismatch, and unknown provider make
   zero adapter/LiteLLM calls and one reconciliation;
4. authoritative completed text and one-Tool-call outcomes resume exactly on
   Memory and real SQLite close/reopen, preserve usage/output/message/event
   sequence, and never call LiteLLM for the recovered turn;
5. authoritative failed outcome terminalizes operation/checkpoint/Run and
   Session detach atomically on both stores;
6. `not_executed` + exact resend and direct certified resend preserve the same
   operation id and call the adapter once across 20 same-SDK callers and two SDK
   instances;
7. pending/unknown/query failure/resend failure/invalid result/timeout always
   create one bounded reconciliation request and never fall back to LiteLLM;
8. precommit, ambiguous commit, checkpoint/operation/event CAS race, lease loss
   and takeover, Session delete, caller cancel, double cancel, SDK close, and
   adapter cancellation leave all-or-none durable state and no task/lease leak;
9. crash after query/resend audit-start can be retried only through the same
   exact certification and operation id; provider side-effect count remains one;
10. secret-bearing request params, adapter closures, invalid result values, and
    exceptions do not appear in events, public messages, causes/contexts, SDK
    traceback locals, reports, or retained tasks;
11. existing Phase 3C2 default unknown-outcome behavior, live RunAPI, Tool/MCP,
    permission, Workflow, Session ownership, and child-agent tests remain green.

Use barriers/fake clocks, not wall-clock sleeps. Do not weaken or skip tests.

## Gates and handoff

Write `.superpowers/sdd/M02-T002-phase3d1-report.md`, then run fresh:

1. all new 3D1 focused/fault/e2e tests;
2. Phase 3C2, 3C1, 3B, 3A, Phase 2, Phase 1+T001 groups;
3. Session/Run/Tool/MCP/permission/Workflow/child compatibility groups;
4. full Python 3.13 pytest;
5. `ruff check src tests`, `mypy src/agent_sdk`, `git diff --check`;
6. explicit schema/migration/built-in-provider/Tool-retry/Workflow/roadmap scope
   audit and public import smoke.

Commit only after final-code gates. Return SHA, exact counts, skip reasons, scope
audit, report, and risks. A fresh independent reviewer must approve Spec and
Quality with C0/I0 before 3D2 starts.
