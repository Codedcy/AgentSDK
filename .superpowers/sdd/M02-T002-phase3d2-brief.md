# M02-T002 Phase 3D2 Brief — Certified Tool Retry and Phase 3 Release

Required base: approved Phase 3D1 ending at `28f2a6f`. Read
`.superpowers/sdd/M02-T002-phase3d-plan.md`, the Phase 3 plan, progress ledger,
and the source task completely before editing.

## Outcome

An application may explicitly certify a Tool as `idempotent` or `safe_retry`.
Only a live Tool operation stamped with that exact full Tool capability may be
retried after an interrupted `TOOL_IN_FLIGHT` checkpoint. The same durable Tool
operation id is re-fenced under a fresh lease; permission is re-evaluated and
the normal registered handler is called at most once per concurrent recovery
winner. Default, legacy, unstamped, unsafe, missing, changed, malformed, or
unknown Tool evidence performs zero Tool/MCP work and enters one durable
reconciliation request.

This slice also closes the Phase 3 release gate. Workflow recovery and
reconciliation resolution actions remain Phase 4/later work.

## Public Tool retry policy

Add and root-export a strict `ToolRetryPolicy` enum with values:

- `never` — default; no automatic retry;
- `idempotent` — the application certifies repeated execution with the same
  Tool call is idempotent; and
- `safe_retry` — the application explicitly certifies recovery retry as safe.

Add `retry_policy: ToolRetryPolicy = never` to `ToolSpec`. Preserve exact
backward compatibility for default ToolSpec JSON and capability hashes: the
default `never` value must be omitted from canonical serialization so current
descriptors created before 3D2 still validate byte-for-byte/hash-for-hash. A
non-default policy must be included in canonical JSON and therefore in
`ToolCapabilityDescriptor.capability_hash`, Run/Workflow execution descriptors,
idempotency fingerprints, and recovery capability admission.

The policy is application-owned certification. The SDK infers nothing from Tool
name, source, effects, timeout, implementation, MCP transport, prior success, or
exception type. Existing `effects`, timeout, version, source, and schema remain
part of the exact capability match.

## Live Tool operation stamping

Before the original handler call, persist the Tool operation and checkpoint as
today, but stamp bounded recovery metadata derived only from the registered
ToolSpec:

- default `never` retains the already shipped conservative metadata shape
  (`safe_retry=false`, unsafe retry class) for backward compatibility;
- `idempotent` and `safe_retry` set `safe_retry=true` and the exact retry class.

No handler, arguments, result, credential, or arbitrary metadata is stored in
`recovery_metadata`. Arguments remain only in the already durable assistant
Tool call/checkpoint and request fingerprint.

## Exact recovery admission

For one authoritative `STARTED ToolCallOperation` linked to an interrupted
`TOOL_IN_FLIGHT` checkpoint, before any permission/Tool/MCP work:

1. validate the current Run/Session ownership, execution descriptor, Agent,
   ordered full Tool capabilities, policy, checkpoint, operation, pending
   requests, and complete Run event history;
2. reconstruct exactly one `ToolCallCompleted` from the final assistant message;
   require the checkpoint operation id, operation turn, Tool identity/capability
   hash, call id/name/arguments, and Tool-started event relationship to match;
3. parse arguments with strict JSON semantics, validate the current Tool schema,
   recompute the exact Tool request fingerprint, and require equality;
4. require persisted retry metadata to be canonical and exactly equal to the
   current non-default Tool retry policy; and
5. reject future/duplicate/missing/terminal/conflicting Tool operations or event
   relationships conservatively.

`never`, legacy metadata, capability/policy mismatch, malformed args/schema,
missing registration, or any evidence disagreement creates one bounded
`tool_call_unknown_outcome`/`recovery_state_invalid` reconciliation request with
zero permission/handler/MCP/LiteLLM calls.

## Retry execution

For exact certified evidence:

- acquire a fresh lease; cross-SDK losers follow durable state and call no
  permission/handler/MCP/LiteLLM work;
- reload and revalidate exact Run/Session/checkpoint/operation/event evidence;
- atomically append bounded `tool.recovery.retry.started`, re-fence the same
  STARTED operation id to the current generation, and retain the exact
  `TOOL_IN_FLIGHT` checkpoint as an adjacent CAS precondition before external
  work;
- start heartbeat and re-evaluate the current exact permission policy. `ask`
  uses the normal application permission bridge; `deny` calls no handler but
  records the normal denied ToolResult and advances safely;
- immediately before the handler/MCP call, assert the current lease again;
- call the existing ToolExecutor/registered handler with the same call id and
  reconstructed arguments, without creating a new Tool operation or duplicate
  `tool.call.started` event;
- atomically transition the same operation to COMPLETED/FAILED, append the
  normal Tool result/message/event, advance checkpoint to READY_FOR_MODEL, and
  preserve ordered Tool results/turn/usage/output; then continue through the
  approved safe Run recovery path;
- handler exceptions, invalid results, and timeout use the existing normalized
  ToolResult behavior. Cancellation/lease loss before terminal commit leaves the
  same certified STARTED operation recoverable; a later retry is permitted only
  because the application certification is still exact.

Recovery never bypasses permission, never calls LiteLLM for the unresolved Tool
turn, and never runs Workflow production. A following model turn may use the
normal LiteLLM gateway after the recovered Tool result is durable.

## Atomicity, lifecycle, and observability

- audit-start/refence requires exact Run, Session, event tail, operation, and
  checkpoint preconditions in one RunProgress transaction;
- terminal Tool outcome and checkpoint advancement are one fenced transaction;
- same-SDK callers share one coordinator task; two SDKs have one lease winner;
- permission wait/cancel, handler cancel/double-cancel, timeout, lease expiry,
  takeover, Session delete, ambiguous commit, SDK close, and lazy SQLite open
  failure settle tasks/leases with all-or-none durable state;
- audit and reconciliation payloads contain only bounded run/operation/call/tool
  identities, retry class, disposition, and status. Never persist or emit raw
  arguments, handler results, workspace data, permission evidence, exceptions,
  credentials, or closures;
- public errors and retained tasks are constant/cause-context-free and do not
  retain Tool arguments, handler closures/results, permission decisions, or
  arbitrary secrets in traceback locals.

## Permitted production scope

- `tools/models.py`, Tool exports, root exports for the retry enum/field and
  backward-compatible canonical serialization;
- `runtime/execution.py` only as needed for hash/canonical compatibility tests;
- `runtime/engine.py` for live stamping and retrying the existing Tool operation;
- `runtime/recovery.py` for exact admission, planning, execution, and bounded
  reconciliation;
- `api.py` only for wiring already existing registries/lifecycle hooks;
- existing storage Memory/SQLite only if the generic composite re-fence guard
  needs a Tool-specific invariant; schema/migrations are forbidden;
- focused unit/integration/fault/e2e tests and Phase 3D2/Phase 3 reports.

Forbidden: automatic retry for default/legacy Tools, built-in Tool/MCP
certification, new provider behavior, reconciliation resolution actions,
Workflow production/recovery, schema/migrations, roadmap/milestone/task-index
edits.

## Required TDD evidence

1. enum/ToolSpec strict validation, root exports, default canonical JSON/hash
   unchanged, non-default policy changes capability/descriptor/idempotency
   fingerprints, model-visible Tool schema unchanged;
2. live default/idempotent/safe-retry Tool operations stamp exact metadata before
   handler work; default existing tests remain byte-compatible;
3. default/legacy/missing/malformed metadata, changed retry policy/effects/
   timeout/source/version/schema, missing Tool, invalid args/fingerprint, wrong
   operation/checkpoint/event relation all make zero permission/handler/MCP/
   LiteLLM calls and one reconciliation on Memory and SQLite reopen;
4. exact idempotent and safe-retry interrupted Tool operations retry the same
   operation id once, persist exact ToolResult/message/event/checkpoint, then use
   LiteLLM only for the following model turn;
5. permission allow/deny/ask-allow/ask-deny/cancel are observable and use the
   normal bridge; deny paths call no handler and still complete the same durable
   operation safely;
6. Tool handler success, normalized exception, invalid JSON-compatible result,
   timeout, and cancellation preserve normal ToolResult semantics without a new
   operation;
7. 20 same-SDK callers and two SDK instances cause one concurrent retry; cross-
   SDK loser observes the same result; lease takeover after winner loss remains
   bounded and certification-safe;
8. audit precommit/ambiguous, operation/checkpoint/event CAS, heartbeat/lease
   loss, Session delete, permission/handler barriers, double cancel, and SDK
   close have no partial refence/outcome, duplicate request/event, or task leak;
9. secret arguments, permission data, handler closure/result/exception and
   corrupted durable records never appear in public errors, events, reports,
   causes/contexts, SDK traceback locals, or retained tasks;
10. provider 3D1, all 3C recovery, live RunAPI, Tool/MCP, permission, Workflow,
    Session ownership, and child-agent suites remain green.

Use fake clocks/barriers, not wall-clock sleeps. Do not weaken or skip tests.

## Slice gates

Write `.superpowers/sdd/M02-T002-phase3d2-report.md`, then run fresh on the final
code tree:

1. all new 3D2 focused/fault/e2e tests;
2. Phase 3D1, 3C2, 3C1, 3B, 3A, Phase 2, Phase 1+T001 groups;
3. Session/Run/Tool/MCP/permission/Workflow/child compatibility groups;
4. full Python 3.13 pytest;
5. Ruff, mypy, diff, public import, forbidden-scope and schema-v3 audits.

Commit and obtain a fresh independent Spec/Quality C0/I0 review before the Phase
3 release gate.

## Phase 3 release gate

After 3D2 approval, write `.superpowers/sdd/M02-T002-phase3-report.md` and run:

- all Phase 3 focused/fault/e2e suites;
- full pytest on Python 3.12 and Python 3.13;
- Ruff, mypy, package build/import smoke, diff/scope/schema audits; and
- a fresh independent whole-Phase-3 review over `69e0ec5..HEAD` at C0/I0.

Only then mark Phase 3 complete and begin Phase 4 Workflow recovery.
