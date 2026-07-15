# M02-T002 Phase 3D2 Implementation Report

## Outcome

COMPLETE AND INDEPENDENTLY APPROVED. The eighth-review fix passed fresh
independent re-review with Spec C0/I0/M0 and Quality C0/I0/M0. Phase 3D2 adds an
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

The third independent review explicitly confirmed every prior Critical,
Important, and Minor finding closed, then found Spec/Quality C0/I1/M0: the
safe no-operation replay validated critical event payload/order but did not
fail closed on the complete EventEnvelope sequence, and a resolved permission
action other than `allow` was treated as denial. The final Important finding
now has exact Memory/SQLite reviewer REDs, a forty-four-case malformed
permission/envelope matrix, and final-code fixes. Valid provider recovery
behavior is unchanged.

The fourth independent review again confirmed every prior finding closed and
reported Spec/Quality C0/I1/M0. A second `run.created` with a unique id,
continuous sequence, and changed ownership payload could be inserted before
the Tool interrupt or anywhere in the Provider history because envelope
admission authenticated only index zero. That allowed the duplicate lifecycle
record to be ignored while certified external work proceeded. The final
Important finding now has exact Tool/Provider x Memory/SQLite RED coverage at
both sides of the interrupt, an expanded duplicate/unknown lifecycle grammar
matrix, and final-code closed-world admission. Valid repeated recovery audits,
permission cancellation histories, and Provider recovery behavior remain
accepted.

The fifth independent review again confirmed every prior finding closed and
reported Spec/Quality C0/I1/M0. Closed event types, exact payloads, and global
counts were still insufficient because a known, fully valid recovery audit
could be placed in an impossible position and remain unconsumed. A Provider
query audit between `model.call.started` and the first `run.interrupted` still
called the adapter; a Tool retry audit between `tool.call.started` and the
first interrupt still called permission, the handler/MCP transport, and the
following model. The final Important finding now has exact Memory/SQLite REDs,
an expanded position/terminal/wrong-operation matrix, and a shared per-event
lifecycle state machine. Every certified event is consumed exactly once by a
valid transition; valid cancellation, audit-only retry, query-to-resend, and
repeated recovery cycles remain accepted.

The sixth independent review again confirmed every earlier finding closed and
reported Spec/Quality C0/I2/M0. First, lifecycle replay crossed a historical
recovery transition against the operation's final projection, so a legal
Provider-to-Tool or Tool-to-Provider sequence was rejected once the earlier
operation had completed. Tool certification also selected the Run's first
interrupt rather than the current Tool cycle. Second, Provider certification
did not strictly reconstruct a historical normal PermissionRequest with the
same canonical request/decision/result relation already required by Tool
history. Both findings now have public Memory/SQLite RED-to-GREEN coverage.
Lifecycle state is reconstructed from the ordered event prefix, while only the
final checkpoint crosses the current unresolved operation. Recovery-only
controls are separated from the logical business history, current Tool retry
starts at the interrupt after its own `tool.call.started`, and one shared strict
PermissionRequest/Decision parser is used for Provider and Tool admission.
Legal historical ask-allow and ask-deny plus both cross-kind recovery directions
remain executable.

The seventh independent review confirmed all previous findings closed and
reported the sole remaining Spec/Quality C0/I1/M0 issue. Provider history used
the shared lifecycle FSM to validate a canonical historical
`tool.call.completed` identity, but an ask-allow success result was not crossed
against the corresponding terminal ToolCallOperation outcome or the ordered
checkpoint ToolResult/message. Replacing only the event with another canonical
success therefore still reached the Provider adapter. The fix adds one shared
authoritative historical ToolResult reconstruction to the lifecycle FSM used
by Provider and Tool certification. Every completion now maps to exactly one
turn/call, ordered checkpoint result and Tool message. A real Tool operation
must also have the exact terminal status, normalized outcome, capability,
recovery metadata, and request fingerprint; operation-free history remains
limited to the previously proven missing/invalid/denied normalized paths.

The eighth independent review confirmed every previous finding closed and
reported the sole remaining Spec/Quality C0/I1/M0 issue. A canonical normal
`permission.requested`/`permission.resolved` pair was reconstructed exactly,
but its existence was not crossed against the permission decision reachable
from the recorded execution policy. A Run recorded with direct deny could
therefore contain a forged ask/deny pair and still reach the Provider adapter.
The fix deterministically evaluates the recorded policy through the production
`PolicyEngine` while replaying each historical call. Initial ask requires the
exact request and optional allow/deny resolution; initial allow permits no
request/resolution and requires authorization/execution; initial deny permits
no request/resolution/authorization and only the exact normalized denied
no-operation result. The same lifecycle consumer protects Provider and Tool
certification, and replay performs no broker callbacks.

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
- Before either certified provider or Tool work, recovery validates the complete
  authoritative Run event envelope at both planning and coordination: globally
  unique event ids, strictly increasing positive target cursors, exact Run and
  Session ownership, schema version and timezone-aware timestamps, contiguous
  SDK Run sequence `1..N`, and an exact reconstructed `run.created` payload
  including agent ownership and execution descriptor. Global cursors are not
  required to be contiguous because unrelated events and deletion leave valid
  holes.
- Certified recovery treats the SDK Run lifecycle grammar as closed-world.
  There is exactly one `run.created` at index zero and one exact `run.started`
  at index one; unknown application events cannot be smuggled into the
  authoritative Run history because the public SDK exposes no custom Run-event
  registration or emission surface. Interrupt and recovery-start counts and
  payloads must form a complete lifecycle, Provider Model/Tool/step counts must
  match durable operations and the checkpoint, and every recovery audit must
  reference a compatible durable operation with its exact action/capability
  metadata. Repeated query/resend or Tool retry audits remain valid when they
  are complete audit attempts rather than being rejected by a naive global
  duplicate count.
- Tool and Provider certification additionally share an ordered lifecycle
  consumer. It follows ready-for-step, Model in-flight/completed, Tool
  proposed/permission/authorized/in-flight/completed, interrupted, and
  recovery states. A recovery audit is legal only while interrupted and must
  identify the exact operation, turn, call, and Tool represented by the event
  prefix; `run.recovery.started` must consume the corresponding audit before
  resuming that phase. Historical transitions are not compared with the
  operation's later final projection; after complete replay, the final state
  alone must match the checkpoint kind/turn/operation. Provider audits cannot
  appear before the first
  interrupt, after recovery has started, or inside Tool permission states;
  Model delta/usage tokens cannot appear after Tool execution starts. Each
  transition validates its bounded payload shape and crosses current
  operation/checkpoint/descriptor identity before advancing.
- Current Tool certification anchors its retry suffix at the first interrupt
  after that Tool's own exact `tool.call.started`, rather than at the Run's
  first interrupt. Recovery-only audits, lifecycle, hashed authorization, and
  hashed permission controls are consumed by the lifecycle FSM and excluded
  from normal business-turn counts. This permits Provider-to-Tool and
  Tool-to-Provider recovery without allowing a historical audit to certify the
  current operation.
- Historical permission evidence is reconstructed through strict
  `PermissionRequest` and `PermissionDecision` validation with canonical exact
  round trips and forbidden extras. Requested/resolved requests must match;
  only broker-valid resolution actions `allow` or `deny` are admitted; request
  id, Run, Session, Tool, arguments, and effects are crossed against the
  descriptor and call, decision scope remains within its strict model, and the
  denial reason is crossed against the normalized Tool result.
- Historical permission lifecycle reachability is derived by passing the
  strictly reconstructed request and the recorded execution-policy descriptor
  through the production `PolicyEngine`. An initial `ask` decision requires
  the canonical request and, when the call terminates, its exact allow/deny
  resolution; `allow` cannot contain requested/resolved events and must proceed
  through authorization/execution; `deny` cannot contain requested/resolved or
  authorization events and may produce only the exact normalized denied
  no-operation completion. This evaluation is local and deterministic, so
  replay never calls an application permission bridge. The currently persisted
  policy descriptor exposes `permission_default` only; no nonexistent
  rule/workspace interpreter was added in parallel with the production policy
  implementation.
- Every historical `tool.call.completed` is canonically reconstructed once in
  the shared lifecycle consumer. Its exact ToolResult must equal the ordered
  checkpoint ToolResult and Tool message. When a ToolCallOperation exists, the
  same result must equal its terminal outcome, its completed/failed status must
  match the result, and capability identity, retry metadata, turn ownership,
  and request fingerprint must match the original call. Success content/value,
  normalized handler failure/non-JSON/timeout, and recovery-produced results
  are checked by their exact SDK normalization. Without a Tool operation, only
  an independently derived missing-Tool, invalid-arguments, or permission-deny
  result is accepted.
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
- If malformed authoritative evidence makes the normal Store sequence query
  fail closed, only the already-selected reconciliation path may derive the
  maximum positive target-Run sequence from the fixed cursor high-water and
  append `max+1`. The bounded reconciliation event, WAITING_RECONCILIATION Run
  snapshot, and single request still use the original atomic
  `commit_run_progress`; external-work paths never use this fallback.

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
14. Third-review exact RED changed a historical resolved action from `deny` to
    `ask`, and separately changed a historical `tool.call.completed` sequence
    from its contiguous value to `+1000` while preserving cursor order. Both
    Memory and SQLite incorrectly reached permission, the MCP-style handler,
    transport, and LiteLLM (`4/4` RED). Shared envelope admission and strict
    permission reconstruction made those `4/4` green. The expanded matrix
    covers forty-four Memory/SQLite request/decision extra, malformed,
    mismatch, action/scope/reason, sequence gap/backward/duplicate/out-of-order,
    cursor, event-id, Run/Session/agent ownership, and historical/current
    Tool/Model critical-event mutations; all perform zero external work and
    atomically create exactly one bounded reconciliation request.
15. Fourth-review exact RED inserted a second syntactically valid
    `run.created`, with a different agent revision, before and after the
    interrupt across Tool/Provider and Memory/SQLite. Six of eight cases
    reached certified work; Tool's existing strict post-interrupt tail already
    rejected the other two. Exact singleton/position/payload admission made all
    eight green with zero permission, handler, MCP, LiteLLM, query, or resend
    calls and exactly one reconciliation. An additional eleven-case Tool and
    Provider matrix rejects unknown Run events, duplicate start/model/tool/
    interrupt events, and malformed recovery audits. Provider lifecycle counts
    and audit-to-operation checks were added after three new RED cases proved
    that a known duplicate or malformed audit could otherwise bypass the
    simple type allow-list.
16. Fifth-review exact RED inserted a fully valid Provider query audit before
    the initial interrupt and a fully valid Tool retry audit before its initial
    interrupt across Memory and SQLite. All four reached external work. The
    shared per-event state machine made all four reconcile with zero query,
    resend, permission, handler, MCP, or LiteLLM calls. Seven additional REDs
    proved that pre-interrupt usage/permission pairs, permission tokens between
    audit and recovery, an audit after recovery start or between permission
    states, and late Model delta/usage after Tool start were also ignored by
    count/selective validation. The final eighteen-case position matrix adds
    resend, recovery-start, completed/failed/authorized tokens plus wrong and
    historical operation/turn identities. All are green with exactly one
    reconciliation, while twelve focused positive cancellation/retry/repeated
    cycle cases and both complete recovery files remain green.
17. Sixth-review public cross-kind REDs executed a legal Provider recovery
    that produced the current interrupted Tool, and the inverse legal Tool
    recovery that produced the current interrupted Model. Both directions
    reconciled on Memory and SQLite (`4/4` RED) because historical recovery was
    compared with the operation's final projection and Tool admission selected
    the first Run interrupt. Prefix-state replay, current-cycle anchoring, and
    recovery-control filtering made all `4/4` green and execute the current
    handler or Provider adapter followed by the normal next phase. A second
    Memory/SQLite matrix changed both copies of one historical normal permission
    request with a forbidden extra, malformed argument type, or mismatched Tool
    (`6/6` RED). Shared canonical PermissionRequest/Decision reconstruction made
    all `6/6` reconcile before query, LiteLLM, or Tool work, with exactly one
    request. Four ask-allow/ask-deny Memory/SQLite positive cases remain
    certified and complete through Provider recovery.
18. Seventh-review public ask-allow history replaced only the canonical
    `tool.call.completed` value, status, or content while leaving its terminal
    Tool operation and checkpoint unchanged. Provider recovery still completed
    on Memory and SQLite (`6/6` RED). Shared authoritative ToolResult
    reconstruction made all `6/6` green with zero query, resend, new permission,
    handler, or LiteLLM work and exactly one reconciliation. The positive
    matrix covers Memory/SQLite normal success, ask-deny, normalized handler
    exception, non-JSON result, timeout, and Tool-recovery-produced success and
    failure histories before Provider recovery. Existing operation-free
    denied/invalid/missing histories and the complete Tool recovery suite remain
    green.
19. Eighth-review public Memory/SQLite RED inserted a canonical matching
    `permission.requested` plus deny `permission.resolved` between proposal and
    completion for a Run whose recorded policy directly denied the call. Both
    cases still reached the Provider query (`2/2` RED), even though a real
    broker direct-deny path emits no permission callbacks. Production-policy
    evaluation made both `2/2` green with zero query/resend work and exactly one
    bounded reconciliation. The expanded thirty-six-case matrix covers legal
    direct allow, legal direct deny, forged permission pairs under both direct
    decisions, legal ask allow/deny, strict request reconstruction, historical
    ToolResult authentication, and cross-kind recovery across Memory/SQLite.
    Provider and Tool share the evaluator-backed lifecycle semantics.

No tests were weakened or skipped. Fake barriers and Store fault injection were
used for concurrency, cancellation, CAS, precommit, ambiguous commit, and lease
loss; no arbitrary test sleep was added.

## Final-code gates

All commands used
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` with
Python 3.13.

- Seventh-review exact plus expanded authoritative ToolResult matrix:
  `28 passed in 6.97s`.
- Complete Provider recovery file: `86 passed in 8.48s`; complete Tool recovery
  file: `131 passed in 11.92s`.
- Provider + Tool + RecoveryAPI: `306 passed in 78.45s`.
- Provider/live/scanner/Store validation and reconciliation neighbors:
  `255 passed in 14.42s`.
- Phase 3C1/3B/3A, Phase 2, and Phase 1 + M02-T001 combined fresh gate:
  `605 passed in 25.07s`.
- Full Python 3.13 pytest on the seventh-review final tree:
  `1529 passed in 116.02s`; zero skipped.
- Ruff: `All checks passed!`; mypy:
  `Success: no issues found in 75 source files`; diff/scope clean; SQLite
  `_SCHEMA_VERSION` remains exactly 3.
- Eighth-review exact plus expanded permission-reachability matrix:
  `36 passed in 9.09s`.
- Complete Provider + Tool recovery files: `225 passed in 16.41s`.
- Provider + Tool + RecoveryAPI: `314 passed in 83.72s`.
- Provider/live/scanner/Store validation and reconciliation neighbors:
  `301 passed in 17.03s`.
- Phase 3C1/3B/3A, Phase 2, and Phase 1 + M02-T001 combined fresh gate:
  `605 passed in 26.22s`.
- Full Python 3.13 pytest on the eighth-review final tree:
  `1537 passed in 117.52s`; zero skipped.
- Ruff: `All checks passed!`; mypy:
  `Success: no issues found in 75 source files`; diff/scope clean; SQLite
  `_SCHEMA_VERSION` remains exactly 3.
- Sixth-review exact cross-kind/strict-permission/legal-decision matrix:
  `14 passed in 7.53s`.
- Complete Provider recovery file: `72 passed in 7.05s`.
- Complete Tool recovery plus RecoveryAPI: `220 passed in 77.30s`.
- Fresh Provider/live/scanner/Store validation and reconciliation neighbor
  gate: `241 passed in 13.71s`.
- Full Python 3.13 pytest on the sixth-review final tree:
  `1515 passed in 115.86s`; zero skipped.
- Ruff: `All checks passed!`; mypy:
  `Success: no issues found in 75 source files`; diff-check clean.
- Phase 3D2 policy/recovery plus complete live progress:
  `164 passed in 12.18s`.
- Phase 3D1 provider recovery, Store reconciliation, and recovery API neighbor
  group: `195 passed in 69.34s`.
- Phase 3C1 scanner/admission: `115 passed in 6.79s`.
- Phase 3B live progress: `40 passed` as part of the Phase 3D2 group.
- Phase 3A Run-progress transaction: `123 passed in 6.95s`.
- Phase 2 recovery records/SQLite validation: `139 passed in 7.75s`.
- Phase 1 + M02-T001 regressions: `188 passed in 14.46s`.
- Session/Run/Tool/MCP/permission/Workflow/child compatibility:
  `150 passed in 7.91s`, plus ownership `87 passed in 5.68s` = 237.
- Full Python 3.13 pytest on the final tree:
  `1501 passed in 114.10s`; zero skipped.
- Fifth-review exact and expanded lifecycle position/terminal/wrong-operation
  matrix: `18 passed`; selected legal cancellation/retry cycles: `12 passed`.
- Exact duplicate creation matrix: `8 passed`; expanded duplicate/unknown
  lifecycle matrix with the exact cases: `19 passed`.
- Complete Tool recovery file: `131 passed in 13.64s`.
- Complete Provider recovery file: `58 passed in 6.05s`.
- Expanded Provider/Store/RecoveryAPI neighbor gate (strict superset of the
  prior 195/246-case gates): `259 passed in 70.90s`.
- Phase 3C2 recovery API: `89 passed`; Phase 3C1 scanner/admission: `115`;
  Phase 3B live progress: `40`; Phase 3A Run progress: `123`; Phase 2 recovery
  records: `139`; Phase 1 + M02-T001: `188`.
- Session/Run/Tool/MCP/permission/Workflow/child compatibility:
  `150 passed`, plus ownership `87 passed` = `237`.
- Ruff: `All checks passed!`.
- Mypy: `Success: no issues found in 75 source files`.
- Public import/default canonical smoke: passed.
- `git diff --check`: exit 0; only Windows line-ending information.
- Forbidden scope is empty and SQLite `_SCHEMA_VERSION` remains exactly 3.

## Coverage and fault matrix

Focused tests cover both certification policies, exact Memory and SQLite
close/reopen success, conservative SQLite default recovery, seven changed or
missing capability variants, descriptor/checkpoint forgeries on both stores,
both legal Provider-to-Tool and Tool-to-Provider repeated recovery directions,
historical normal ask-allow and ask-deny Provider recovery on both stores,
recorded direct-allow and direct-deny permission reachability with forged
normal permission pairs rejected on both stores,
historical ToolResult value/status/content substitutions, normalized handler
failure/non-JSON/timeout results, and Tool-recovery-produced completed/failed
operations before Provider recovery,
ten multi-turn historical evidence mutations, recovery permission-event
mutation, post-audit missing plus seven registration replacements, allow/ask
allow/ask deny/cancel, normalized handler exception/non-JSON result/timeout,
Memory/SQLite close-reopen historical permission-deny/invalid-arguments/
missing-Tool safe rejections, four no-operation ToolResult/permission forgery
cases, forty-four strict permission and complete Run-envelope corruptions,
eight exact duplicate-creation cases, eleven duplicate/unknown lifecycle
grammar cases, eighteen exact lifecycle-position/terminal/wrong-operation
cases, twelve selected valid repeated audit/cancellation/recovery cycles, the
final handler-preflight lease barrier,
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

Tests are limited to the Tool policy/recovery suites, Provider recovery
certification regressions, and live Tool stamping.
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

This report records implementation and gate evidence. The final independent
review approved Phase 3D2 at Spec C0/I0/M0 and Quality C0/I0/M0 after fresh
permission-reachability, Provider, Tool, RecoveryAPI, static, scope, and schema
verification. The Phase 3 release gate may now begin.
