# M02-T002 Phase 3D2 Implementation Report

## Outcome

IMPLEMENTATION COMPLETE; PENDING INDEPENDENT REVIEW. Phase 3D2 adds an
application-owned Tool retry certification boundary. `ToolRetryPolicy.NEVER`
remains the conservative default and is omitted from canonical ToolSpec JSON,
so the pre-3D2 JSON shape and capability hash remain unchanged. Only exact
`idempotent` or `safe_retry` capabilities stamped before the original handler
call may re-fence and retry the same durable Tool operation after an
interrupted `TOOL_IN_FLIGHT` checkpoint.

Default, legacy, missing, changed, malformed, or internally inconsistent Tool
evidence performs no permission, handler, MCP, or LiteLLM work and creates one
bounded reconciliation request. Workflow recovery, reconciliation resolution,
provider behavior, SQLite schema, and migrations are unchanged.

## Implemented contract

- Added and root-exported strict `ToolRetryPolicy` values `never`,
  `idempotent`, and `safe_retry`, plus `ToolSpec.retry_policy`.
- Added a canonical ToolSpec serializer that omits only the default `never`
  value. The established default hash
  `2a6f67bbdf395f62fe0d6ecd1770dc6a3f3fe79e16efc8cfc61783578d78fb14`
  remains exact; non-default policies participate in Tool capability,
  execution descriptor, idempotency, and Tool request fingerprints.
- Live Tool start preserves the exact existing unsafe metadata for `never` and
  stamps only `{safe_retry=true,retry_class=<policy>}` for certified Tools,
  before handler work.
- Recovery reconstructs exactly one current Tool call from the final assistant
  message and validates the current Run/Session descriptor, ordered Tool
  capability, retry metadata, Tool identity, strict JSON/schema arguments,
  request fingerprint, current and historical Model operations, accumulated
  usage/output, Tool operations, and event relationship before external work.
- A fresh lease atomically appends a bounded
  `tool.recovery.retry.started` event and re-fences the same STARTED Tool
  operation against the exact in-flight checkpoint. No new operation or
  duplicate `tool.call.started` event is created.
- Recovery uses the normal ToolExecutor. Permission is re-evaluated; ask uses
  the normal bridge; denial creates the normal denied ToolResult without
  invoking the handler. Recovery permission events and denial text are bounded
  and omit arguments and application decision evidence.
- The lease is asserted immediately before handler work. The same Tool result,
  operation terminal state, Tool message/event, and READY_FOR_MODEL checkpoint
  are committed atomically. Only the following normal model turn uses LiteLLM.
- Handler exceptions, non-JSON results, timeout, and cancellation retain normal
  ToolResult/cancellation semantics. Repeated certified attempts accept only a
  strict sequence of bounded prior retry audit/lifecycle events.
- The public recovery task boundary reconstructs cause/context-free SDK errors
  after deleting the service and RecoveryPlan references, so retained task
  tracebacks do not keep checkpoint arguments, registered handler closures, or
  arbitrary internal failures.

## TDD RED-to-GREEN evidence

Production changes followed observable failing tests:

1. Public imports failed because no retry policy existed. The minimal enum,
   ToolSpec field, canonical serializer, and exports made the policy/hash tests
   green while retaining the exact default JSON/hash.
2. Certified live operations still stored the unsafe metadata. Live
   pre-handler observations failed until Tool start derived the exact metadata
   from the registered ToolSpec.
3. Exact certified interrupted Tools entered reconciliation. The first recovery
   execution tests failed with `recovery required` until exact admission,
   same-operation re-fencing, ToolExecutor reuse, and following-turn resume
   were added.
4. Changed capabilities originally raised `recovery capabilities unavailable`
   without a durable request. TOOL_IN_FLIGHT capability mismatch now enters one
   bounded `recovery_state_invalid` reconciliation path.
5. Cancellation after a retry audit made the certified operation permanently
   inadmissible because only the original Tool-start/interrupted suffix was
   accepted. A strict retry-cycle event validator now admits bounded prior
   interrupted attempts, including audit-only lease loss, and rejects unknown
   trailing events.
6. Recovery ask events exposed Tool arguments and decision reason. The recovery
   permission transition and denial result now store only bounded identities,
   allow/deny status, and the stable `permission denied` text.
7. Unsafe reconciliation and audit-time lease loss retained RecoveryPlan Tool
   arguments in task exception tracebacks. A detached public recovery task
   error boundary and early plan deletion made both retained-task regressions
   green.
8. Corrupted checkpoint usage was initially accepted. Admission now validates
   sequential Model operations, exact cumulative usage/output, and complete
   Model started/completed relationships.

No tests were weakened or skipped. Fake barriers and Store fault injection were
used for concurrency, cancellation, CAS, precommit, ambiguous commit, and lease
loss; no arbitrary test sleep was added.

## Final-code gates

All commands used
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` with
Python 3.13.

- Phase 3D2 policy/recovery plus complete live progress: `82 passed in 5.16s`.
  This is policy 8, Tool recovery/fault/e2e 34, and live progress 40.
- Phase 3D1 provider recovery plus Store re-fence: `183 passed in 6.99s`.
- Phase 3C2 recovery API: `89 passed in 66.94s`.
- Phase 3C1 scanner/admission: `115 passed in 7.02s`.
- Phase 3A Run-progress transaction: `123 passed in 6.26s`.
- Phase 2 recovery records/SQLite validation: `139 passed in 6.94s`.
- Phase 1 + M02-T001 regressions: `188 passed in 14.00s`.
- Session/Run/Tool/MCP/permission/Workflow/child compatibility:
  `237 passed in 10.04s`.
- Full Python 3.13 pytest on the final tree:
  `1382 passed in 105.08s`; zero skipped.
- Ruff: `All checks passed!`.
- Mypy: `Success: no issues found in 74 source files`.
- Public import/default canonical smoke: passed.
- `git diff --check`: exit 0; only Windows line-ending information.
- Forbidden scope is empty and SQLite `_SCHEMA_VERSION` remains exactly 3.

## Coverage and fault matrix

Focused tests cover both certification policies, exact Memory and SQLite
close/reopen success, conservative SQLite default recovery, seven changed or
missing capability variants, six corrupted evidence variants, allow/ask
allow/ask deny/cancel, normalized handler exception/non-JSON result/timeout,
handler and SDK-close cancellation, repeated cancellation, 20 same-SDK callers,
two SDK instances, audit and Tool-outcome precommit/ambiguous replay, Run CAS,
audit-time lease loss and takeover, same-operation retry after interrupted
permission/outcome commits, bounded recovery events, and task traceback secret
retention.

## Scope and handoff

Production changes are limited to:

- `src/agent_sdk/tools/models.py`, Tool and root exports;
- `src/agent_sdk/tools/executor.py` for recovery-only denial sanitization;
- `src/agent_sdk/runtime/engine.py` for live stamping and reuse of the exact
  Tool operation; and
- `src/agent_sdk/runtime/recovery.py` for exact admission, coordination,
  reconciliation, lifecycle, and public error cleanup.

Tests are limited to the new Tool policy/recovery suites and live Tool stamping.
This report and the progress ledger are the only documentation changes. There
are no changes to storage, migrations, provider gateway/recovery, Workflow
production/recovery, roadmap, milestones, or task index.

Residual trust boundary: certification is supplied by the application. The SDK
enforces exact identity and evidence matching, but cannot prove that an
application-labeled Tool is actually idempotent or otherwise safe to retry.

This report records implementation and gate evidence only. It does not
self-approve Phase 3D2. Fresh independent Spec and Quality review at C0/I0 is
required before the Phase 3 release gate.
