# M02-T002 Phase 4A Implementer Brief

Implement Phase 4A from `M02-T002-phase4-plan.md` with strict TDD. The source of
truth is `docs/plans/tasks/M02-T002-leases-reconciliation.md`, especially the
Global Constraints and Step 4 Workflow paragraphs.

## Start point and ownership

- Start from commit `9a486a6a0b3dac84691a444ff9da724204dcc50e`.
- Work only in `D:\code\AgentSDK\.worktrees\agent-sdk-implementation`.
- You own production/tests needed for Phase 4A, plus this slice report.
- Preserve unrelated user work and all Phase 1-3 behavior.
- Use `apply_patch` for edits and explicit uv at
  `C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe`.

## Mandatory architecture

1. `sdk.recovery.recover_workflow(id) -> WorkflowHandle` is the only new public
   entry. `WorkflowAPI.resume` remains read-only.
2. Inject the Workflow executor into RecoveryAPI and let it reuse
   `WorkflowExecutor._active`/`_start_lock`; do not add a second Workflow task
   registry. Factor the Run recovery admission/task path so Workflow recovery
   reuses the existing per-Run `_tasks` registry without recursively entering
   SDK lifecycle admission.
3. Load terminal Workflow state before capability checks. For a nonterminal
   current descriptor, reconstruct and require exact Workflow descriptor
   equality from live AgentSpecs, complete ToolSpecs, and effective Policy.
   Legacy returns bounded `CONFLICT / recovery required / retryable=True`.
4. Extend `RuntimeCommands.start_run` explicit-id idempotency safely:
   - fingerprint and validate explicit `run_id` only when supplied;
   - do not include the freshly generated id when `run_id` is omitted, because
     that would break existing normal idempotent replay;
   - add focused regression tests for same/different explicit ids and existing
     generated-id replay.
5. Use one deterministic, bounded idempotency key per selected Workflow node
   (including Workflow/node identity). After command replay, reload and validate
   the authoritative current Run rather than trusting the original CREATED
   idempotency payload as its current status.
6. Create both parent and child selected Runs without launching external work.
   Child values must exactly match the prior node Run, rendered TaskEnvelope,
   Workflow/node identity, Agent revision, and a per-node descriptor whose
   message is the rendered envelope. Then call Run recovery; do not call
   `SubagentService.spawn`, because it starts RunEngine immediately.
7. Exact relation validation includes `execution_compatibility="current"` and
   exact per-node `ExecutionDescriptor`, not only ids/input. Any mismatch fails
   before Run recovery.
8. Recovery loop semantics:
   - pending node: `WorkflowState.start_node` exact CAS;
   - selected missing/CREATED/interrupted/waiting/etc.: use existing Run recovery
     service/handle and lease semantics;
   - Run COMPLETED: exact-CAS node completion;
   - Run FAILED: project the durable sanitized Run failure, then fail Workflow;
   - CAS conflict: reload/converge, never persist a synthetic failure;
   - waiting reconciliation/shutdown/no-owner diagnostic: leave Workflow
     nonterminal and propagate context-free `recovery required`;
   - completed/failed Workflow: detached handle.
9. A normal active Workflow and same-SDK recovery share one task. If a normal
   coordinator loses the selected Run lease to another SDK, it must safely
   follow/reload durable Run state rather than fail the node. Keep follower
   waiting bounded by durable lease ownership and shutdown semantics.
10. Public errors and task tracebacks must not retain raw Store/Provider/Tool
    exception context or secret-bearing snapshots/results. Preserve the Phase 3
    cancellation and cleanup standards.

## TDD sequence

Write failing tests first in a new
`tests/integration/workflow/test_workflow_recovery_admission.py` and focused
Run-id tests in `tests/integration/runtime/test_run_session_ownership.py`.
Record the exact RED command/failures before production edits.

Minimum Phase 4A matrix:

- explicit Run-id same-key exact replay; different explicit id conflict;
  omitted-id replay remains stable;
- terminal success/failure detached without live capabilities;
- nonterminal legacy, missing Agent, changed Agent model params, changed Tool
  effect/timeout/source/version/retry metadata, changed Policy, and substituted
  persisted descriptor: no write/provider/tool/MCP;
- pending root selection/create/execute/complete;
- selected missing Run repair; selected CREATED Run; selected COMPLETED and
  FAILED Run projections; selected WAITING_RECONCILIATION diagnostic;
- child creation/recovery with exact parent/envelope/descriptor;
- foreign session/Workflow/node/Agent/input/parent/envelope/descriptor selected
  Run rejection;
- same-SDK active task attachment, caller cancellation shielding, success/error/
  cancellation cleanup.

Use both Memory and SQLite wherever Store/CAS/idempotency parity matters.

## Focused verification before handoff

At minimum run:

1. New Workflow recovery admission file plus explicit-id Run tests.
2. Existing `tests/integration/workflow/test_workflow_recovery.py` and
   `test_workflow_session_ownership.py`.
3. Existing `tests/integration/runtime/test_recovery_api.py` and
   `test_run_session_ownership.py`.
4. Phase 3 Provider/Tool recovery focused suites.
5. Ruff and mypy.
6. Full Python 3.13 pytest with zero unexpected skips.
7. Diff/scope/schema/public-import checks.

Create `.superpowers/sdd/M02-T002-phase4a-report.md` with RED/GREEN evidence,
changed files, concurrency/cancellation reasoning, exclusions, and exact gate
counts. Commit with a concise message and return the commit hash. Do not claim
Phase 4 complete and do not begin Phase 4B.
