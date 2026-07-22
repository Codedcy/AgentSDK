# M02-T002 Phase 3 Release Report

## Outcome

COMPLETE AND INDEPENDENTLY APPROVED. The whole-Phase-3 release fix passed fresh
independent re-review with Spec C0/I0/M0 and Quality C0/I0/M0. Phase 3 now
provides durable Run progress transactions, live execution persistence,
conservative recovery planning, fenced coordination, application-certified
Provider recovery, and application-certified Tool retry. Every Phase 3 slice
has its own independent C0/I0 approval, and the final tree passed the dual-
Python release gates below.

The reviewed release candidate was
`32be17c98e3689b0e44bd129914eb791e55ec4d9`, with release-gate evidence recorded
at `c33e8915d8a406de9ba2b66e21e26cd3fcbd55a0`, against Phase 3 base `69e0ec5`.
The whole-phase review returned Not Approved with Spec/Quality C0/I1/M1 and no
other Critical, Important, or Minor findings. The Important finding was a final
Provider registry TOCTOU after the audit/refence commit and lease assertion;
query and query-to-resend could invoke a stale adapter registration. The Minor
finding was aggregate-only READY_FOR_TOOL admission, which did not
authoritatively bind every historical Model turn's request, outcome, events,
messages, output, and usage. Both now have exact public Memory/SQLite REDs and
final-code fixes. Workflow recovery and reconciliation resolution remain Phase
4/later work.

## Delivered phases

- Phase 3A: atomic Run progress transactions, event sequencing, checkpoint and
  durable-operation invariants, Memory/SQLite parity, and fenced CAS behavior.
- Phase 3B: live Run execution writes Model/Tool progress, operations,
  checkpoints, results, usage, and bounded events atomically.
- Phase 3C1: conservative scanner/admission reconstructs exact Run ownership,
  descriptor, checkpoint, operation, and event evidence.
- Phase 3C2: public recovery coordination, leases, heartbeat, cancellation,
  Session lifecycle, concurrent callers, and bounded reconciliation.
- Phase 3D1: application-certified Provider status/query and same-operation-id
  recovery adapters without adding a second normal model gateway; ordinary
  model calls remain LiteLLM-only.
- Phase 3D2: strict `ToolRetryPolicy`, default-hash-compatible Tool contracts,
  certified same-operation Tool retry, permission re-evaluation, exact ordered
  lifecycle replay, authoritative Tool-result reconstruction, registry race
  fences, bounded observability, and fail-closed reconciliation.

## Final behavior and trust boundaries

- Default, legacy, unstamped, changed, missing, malformed, or inconsistent
  evidence performs no certified Provider/Tool external work and creates one
  bounded durable reconciliation request.
- Provider and Tool external work is admitted twice: during planning and again
  under the coordinator lease immediately before execution. Provider query and
  resend each perform their own final lease assertion, then synchronously
  re-resolve the registry with no await before callback entry. The current
  registration must be the exact planned object and retain exact recorded
  adapter id, version, authoritative-status, and same-operation resend
  certification; change is resolved by the lease owner through one atomic
  `recovery_state_invalid` request while followers converge on durable state.
- Full Run event envelopes and the closed ordered lifecycle grammar authenticate
  creation, model, tool, permission, interrupt, recovery, and terminal history.
  Every certified event is consumed in one reachable state and crossed against
  descriptor, operation, checkpoint, message, result, and event evidence.
- Historical permissions are evaluated through the production `PolicyEngine`
  using the recorded execution descriptor; recovery never calls the application
  permission bridge while authenticating history.
- Tool retry uses the same durable operation id, re-evaluates current permission,
  revalidates the exact registered Tool after the last lease await, and preserves
  normal ToolExecutor result semantics.
- Safe READY_FOR_TOOL resume reconstructs every Model turn from the recorded
  descriptor and ordered operations: exact ModelRequest fingerprint, terminal
  outcome and pending call, assistant/Tool messages, per-turn event order and
  payload, usage, joined output, and historical Tool result/operation relation.
  The shared lifecycle consumer validates the final interrupted
  model-completed state. This safe resume does not require Tool retry
  certification because the pending Tool has not started.
- Application certification remains a trust boundary: the SDK authenticates the
  recorded certification and evidence but cannot prove a business side effect
  is truly idempotent or safe.
- Durable Model outcomes and delta events authenticate exact joined output. The
  original provider stream chunk partition is not persisted and is not invented
  during recovery.

## Whole-review release-fix RED-to-GREEN evidence

- Provider final-preflight RED: Memory/SQLite x query boundary with unregister,
  same-metadata replacement, adapter version, adapter id, or certification
  change, plus query-result-to-resend same-metadata replacement, all with two
  SDK owner/follower coordination. All 12 cases invoked a stale callback before
  the fix; all 12 now perform zero affected callback work and create exactly one
  owner-atomic reconciliation. Query-to-resend necessarily performs the already
  certified query once, but neither stale nor replacement resend runs.
- READY_FOR_TOOL multi-turn RED: thirteen corruptions across Memory/SQLite
  changed historical started/completed payload or order, moved an event to the
  wrong turn, forged the historical request fingerprint or used the next turn's
  hash, changed outcome text/call, changed or reordered checkpoint assistant and
  Tool messages, changed joined output, or redistributed per-turn usage while
  preserving the aggregate. All 26 reached Tool/MCP and LiteLLM before the fix;
  all now reconcile before Tool, MCP, permission, or LiteLLM work. Legal
  Memory/SQLite two-turn histories remain executable (2/2).

## Fresh whole-review fix gates - Python 3.13

All commands used the explicit workspace `uv` executable and disabled pytest's
cache provider where applicable.

- Exact Provider registry barrier matrix: 12 passed.
- Exact READY_FOR_TOOL multi-turn positive/negative matrix: 28 passed; complete
  READY_FOR_TOOL selection: 54 passed.
- Complete Provider, Tool, and RecoveryAPI recovery: 354 passed.
- All 17 Phase 3 changed test files: 896 passed, zero failed, zero skipped.
- Existing `tests/e2e`: 3 passed.

## Fresh Phase 3 focused gates — Python 3.13

All commands used the explicit workspace `uv` executable and disabled pytest's
cache provider.

- Public contracts and policy: 32 passed in 5.04s.
- Provider, Tool, RecoveryAPI fault/E2E/recovery: 314 passed in 80.90s.
- Scanner and reconciliation: 115 passed in 6.64s.
- Live progress: 40 passed in 3.54s.
- Store progress and recovery records: 206 passed in 10.08s.
- Compatibility files changed by Phase 3: 149 passed in 8.53s.
- All 17 test files changed by `69e0ec5..HEAD`: 856 passed, zero failed,
  zero skipped.
- Existing `tests/e2e`: 3 passed in 4.60s. Fault cases are embedded in the
  recovery, live, and Store suites; the repository has no `tests/faults` folder.

## Dual-Python full gates

- Python 3.12.13, isolated and frozen: 1577 passed in 125.48s, zero failed,
  zero skipped.
- Python 3.13.14, frozen: 1577 passed in 122.85s, zero failed,
  zero skipped.

## Static, build, import, scope, and schema gates

- Ruff passed across `src` and `tests`.
- Mypy passed across 75 source files.
- A source distribution and wheel were built in a temporary directory outside
  the worktree. Isolated wheel installs and import smoke passed on Python 3.12
  and Python 3.13.
- The installed distribution was `agent-sdk==0.1.0.dev0`; root Phase 3 imports
  passed 9/9, root `__all__` contained 99 unique available names, and Provider
  recovery contracts imported successfully.
- `ToolRetryPolicy` values were exactly `never`, `idempotent`, and `safe_retry`.
  Default Tool canonical JSON omitted `retry_policy`, and its established hash
  remained `2a6f67bbdf395f62fe0d6ecd1770dc6a3f3fe79e16efc8cfc61783578d78fb14`.
- `git diff --check 69e0ec5..HEAD` passed.
- Release-fix diffs for Workflow production, roadmap/milestone/task-index,
  storage, migration, and SQLite DDL/schema-version are empty. SQLite schema
  remains version 3.
- Temporary build artifacts were removed; final staged, unstaged, and untracked
  status was clean at the verified HEAD.

One non-code command attempt used an unsupported `uv build --frozen` option and
stopped during CLI parsing before any build or filesystem change. The supported
external-output build command then completed successfully. A redundant
`--no-project` warning in the Python 3.12 wheel smoke had no functional effect.

For the release fix, the first non-isolated Python 3.12 command stopped before
test collection because Windows denied replacing the concurrently occupied
project virtual environment; the isolated frozen command then passed all 1577
tests. The first wheel smoke incorrectly requested the internal
`RunCheckpoint` as a root export and failed as the public contract requires;
the corrected nine-root-export, complete `__all__`, package-version, and
dual-Python smoke passed. All release-fix build output was external and removed.

## Release decision

This report records verified release-fix evidence. The fresh independent
whole-Phase-3 re-review approved Spec C0/I0/M0 and Quality C0/I0/M0 after
rerunning the exact Provider and READY_FOR_TOOL matrices, all changed Phase 3
tests, E2E, static, import, scope, and schema gates. Phase 3 is complete and
Phase 4 Workflow recovery may begin.
