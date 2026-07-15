# M02-T002 Phase 3D2 Implementation Report

## Outcome

SECOND REVIEW FIX COMPLETE; PENDING FRESH INDEPENDENT RE-REVIEW. Phase 3D2 adds an
application-owned Tool retry certification boundary. `ToolRetryPolicy.NEVER`
remains the conservative default and is omitted from canonical ToolSpec JSON,
so the pre-3D2 JSON shape and capability hash remain unchanged. Only exact
`idempotent` or `safe_retry` capabilities stamped before the original handler
call may re-fence and retry the same durable Tool operation after an
interrupted `TOOL_IN_FLIGHT` checkpoint.

The initial independent review found Spec/Quality C1/I1/M1: checkpoint content
was not fully reconstructed from durable operations/events, Tool registry
identity could change between certification and permission/handler work, and
recovery observability exposed unbounded call/Tool identities. Those findings
now have strict RED regressions and final-code fixes and were confirmed closed
by the second independent review. That review found Spec/Quality C0/I2/M0:
the final handler preflight had a registry TOCTOU after its lease await, and
valid historical handler-before safe rejections had no Tool operation and were
rejected by the authoritative replay. Both Important findings now have strict
reviewer reproductions and final-code fixes. Default, legacy, missing,
changed, malformed, or internally inconsistent Tool
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
- Recovery starts from the execution descriptor and reconstructs every turn
  from the exact Model and Tool operations/outcomes: Model request
  fingerprints, assistant messages, Tool capability/schema/fingerprints,
  historical Tool results/messages, accumulated usage, joined output, and
  critical event counts/payloads/order. The reconstructed messages and ordered
  Tool results must exactly equal the checkpoint before external work.
- A historical turn may have no Tool operation only for an exactly reconstructed
  handler-before `tool not found`, invalid-arguments, or permission-denied
  result. Its normalized Tool result is derived from the durable completion
  event, crossed against the assistant call, descriptor/schema or missing
  capability, permission evidence, Tool message, and checkpoint. All other
  missing-operation shapes remain reconciliation-only.
- A fresh lease atomically appends a bounded
  `tool.recovery.retry.started` event and re-fences the same STARTED Tool
  operation against the exact in-flight checkpoint. No new operation or
  duplicate `tool.call.started` event is created.
- The exact `RegisteredTool` object certified before the audit is rechecked
  after the audit, before permission, after permission, on every early
  completion path, immediately before the handler, and before outcome commit.
  A missing or replaced registration becomes one generation-fenced durable
  `recovery_state_invalid` reconciliation request.
- Every recovery preflight performs a second synchronous registry/spec/
  capability/metadata check after the lease await. The final handler preflight
  then verifies the Tool fingerprint and invokes the already certified handler
  without another await window.
- Recovery uses the normal ToolExecutor. Permission is re-evaluated; ask uses
  the normal bridge; denial creates the normal denied ToolResult without
  invoking the handler. Recovery permission events and denial text are bounded
  and omit arguments and application decision evidence.
- The lease is asserted immediately before handler work. The same Tool result,
  operation terminal state, Tool message/event, and READY_FOR_MODEL checkpoint
  are committed atomically. Only the following normal model turn uses LiteLLM.
- Recovery audit, permission, and authorization events represent operation,
  call, Tool, and permission-request identities only as stable SHA-256 objects;
  their size is independent of application-controlled identity length.
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
9. Review RED showed forged checkpoint system messages and Tool results reached
   the handler on both Memory and SQLite. Descriptor-to-checkpoint replay now
   rejects those plus multi-turn message/ToolResult/fingerprint/outcome/event
   insert, delete, reorder, and modification cases before any external work.
10. Review RED showed unregister-after-plan and audit-time registry changes
    either had no durable reconciliation or could execute a replacement
    handler. Exact registration-object preflight plus owned conflict
    coordination makes missing/schema/version/source/effects/timeout/handler
    changes and ask-deny races one durable reconciliation with zero handler or
    model calls.
11. Review RED showed 4 KiB Tool/call identities containing a secret copied
    into audit, permission, and authorization events. Public recovery identity
    payloads now contain only exact SHA-256 digests and remain bounded.
12. Second-review barrier RED swapped an exact-schema MCP-style registration
    inside the fourth lease assertion. Recovery entered reconciliation only
    after invoking the captured old handler once. Preflight now re-reads and
    compares the exact registration object, spec, capability descriptor/hash,
    and recovery metadata synchronously after every lease await. The reviewer
    barrier is green with old/new handlers and model all zero and one durable
    reconciliation request.
13. Second-review Memory/SQLite RED built valid two-turn histories where turn
    zero ended before a handler because permission was denied, arguments were
    invalid, or the Tool was missing; turn one held a certified in-flight Tool.
    All six were incorrectly reconciled because replay demanded two operations
    per turn. Replay now admits only the exact Model-only safe-rejection shape;
    all six recover the same current operation and resume the model. Four
    dedicated ToolResult/permission modification/insertion cases remain
    zero-external reconciliation paths, as do all earlier forgery regressions.

No tests were weakened or skipped. Fake barriers and Store fault injection were
used for concurrency, cancellation, CAS, precommit, ambiguous commit, and lease
loss; no arbitrary test sleep was added.

## Final-code gates

All commands used
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` with
Python 3.13.

- Phase 3D2 policy/recovery plus complete live progress:
  `120 passed in 6.93s`.
- Phase 3D1 provider recovery, Store reconciliation, and recovery API neighbor
  group: `195 passed in 69.35s`.
- Phase 3C1 scanner/admission: `115 passed in 6.88s`.
- Phase 3B live progress: `40 passed in 3.52s`.
- Phase 3A Run-progress transaction: `123 passed in 6.99s`.
- Phase 2 recovery records/SQLite validation: `139 passed in 8.56s`.
- Phase 1 + M02-T001 regressions: `188 passed in 13.39s`.
- Session/Run/Tool/MCP/permission/Workflow/child compatibility:
  `150 passed in 7.74s`, plus ownership `87 passed in 5.46s` = 237.
- Full Python 3.13 pytest on the final tree:
  `1420 passed in 107.58s`; zero skipped.
- Ruff: `All checks passed!`.
- Mypy: `Success: no issues found in 75 source files`.
- Public import/default canonical smoke: passed.
- `git diff --check`: exit 0; only Windows line-ending information.
- Forbidden scope is empty and SQLite `_SCHEMA_VERSION` remains exactly 3.

## Coverage and fault matrix

Focused tests cover both certification policies, exact Memory and SQLite
close/reopen success, conservative SQLite default recovery, seven changed or
missing capability variants, descriptor/checkpoint forgeries on both stores,
ten multi-turn historical evidence mutations, recovery permission-event
mutation, post-audit missing plus seven registration replacements, allow/ask
allow/ask deny/cancel, normalized handler exception/non-JSON result/timeout,
Memory/SQLite close-reopen historical permission-deny/invalid-arguments/
missing-Tool safe rejections, four no-operation ToolResult/permission forgery
cases, the final handler-preflight lease barrier,
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
- `src/agent_sdk/runtime/_recovery_observability.py` for stable hashed public
  recovery identities; and
- `src/agent_sdk/runtime/recovery.py` for exact admission, coordination,
  reconciliation, lifecycle, and public error cleanup.

Tests are limited to the new Tool policy/recovery suites and live Tool stamping.
This report and the progress ledger are the only documentation changes. There
are no changes to storage, migrations, provider gateway/recovery, Workflow
production/recovery, roadmap, milestones, or task index.

Residual trust boundary: certification is supplied by the application. The SDK
enforces exact identity and evidence matching, but cannot prove that an
application-labeled Tool is actually idempotent or otherwise safe to retry.
The durable Model outcome and `model.text.delta` events preserve exact joined
text but not the original provider stream's chunk partition, so recovery
authenticates exact joined output rather than inventing an unavailable chunk
boundary.

This report records implementation and gate evidence only. It does not
self-approve Phase 3D2. Fresh independent Spec and Quality review at C0/I0 is
required before the Phase 3 release gate.
