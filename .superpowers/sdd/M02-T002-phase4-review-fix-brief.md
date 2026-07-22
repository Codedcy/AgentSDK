# M02-T002 Phase 4 Review-Fix Brief

## Review verdict

The whole-Phase-4 independent review of `9a486a6..92d389f` was **Not
Approved**: Spec Compliance C0/I3/M0 and Task Quality C0/I1/M0. This brief is
the complete fix boundary. Do not start Phase 5, M02-T003, or M02-T004.

## I1 - Preserve the sole new public recovery API

`WorkflowExecutor` is publicly exported, so its non-underscored `recover(...)`
method is an unintended second public entry and exposes callback injection.

- Add a RED public-surface regression proving `WorkflowExecutor.recover` is not
  public while `RecoveryAPI.recover_workflow(workflow_run_id)` retains the exact
  approved signature.
- Rename the executor entry to `_recover` (or an equivalent private name) and
  update only the internal `RecoveryAPI` assembly call.
- Do not change `WorkflowAPI.resume`, root exports, behavior, or add another
  public method.

## I2 - Complete the two-SDK Workflow matrix

Use two independently constructed SDKs. For SQLite, use the same database with
two independently opened `SQLiteStore` connections. Use deterministic
commit/lease coordination, never timing luck.

Cover both Memory and SQLite for:

1. pending node / no selected Run: one selected id, one node-start event, one
   Run, and no loser-generated Run;
2. selected RUNNING node / missing Run: recreate the exact selected id, one
   `run.created`, one Session attachment, and one Provider execution;
3. selected CREATED Run: one Run-lease owner, one logical Provider execution,
   and both Workflow callers safely converge;
4. selected live/interrupted Run with a valid lease owner: the Workflow-level
   loser follows authoritative durable state and never records synthetic node
   or Workflow failure;
5. expired/unreconciled ownership: return the bounded retryable `recovery
   required` outcome, leave Workflow/node/Session ownership active, and record
   no synthetic terminal projection or external work.

Add a deterministic Session deletion/recovery race. It must prove that after
the supported lifecycle decision, neither SDK creates unexpected Session, Run,
Workflow, event, permission, idempotency, lease, checkpoint, operation, or
reconciliation state. In the current M02-T002 contract, ACTIVE/CLOSING Sessions
with owned work reject public deletion as busy. Prove that rejection races
safely with recovery and that recovery remains correct. Do not directly invoke
`StateStore.delete_session` on an ACTIVE/CLOSING Session and do not add the
T004 force-delete path. A delete-wins/force-delete matrix remains deferred to
T004 unless separately approved.

## Second-review correction - use only reachable interruption state

The first fix implementation's expired/unreconciled test directly rewrote a
CREATED Run to an INTERRUPTED snapshot, which is not a legal lifecycle proof.
Replace it completely:

- Start from a real RUNNING or WAITING Run with a persisted checkpoint and real
  lease.
- Advance a controlled clock past the lease expiry.
- Run the production `RecoveryScanner` path so it atomically records the real
  `run.interrupted` transition and new lease generation/ownership evidence.
- Then race Workflow recovery from two SDKs (and two independently opened
  SQLite connections) over Memory and SQLite.
- Assert bounded retryable `recovery required`, zero new Provider/Tool/MCP work,
  no synthetic node/Workflow failure, retained Workflow/Session ownership, and
  the exact interruption/reconciliation event/checkpoint/lease evidence.
- The test may use existing public/test construction hooks and controlled clock;
  it may not `model_copy` a Run into a state production cannot create.

## I3 - Prove paired ambiguous-commit ownership boundaries

For every post-commit fault and subsequent reopen/recovery, assert the complete
durable pair and final ownership, not only the headline event count:

- node selection: exact selected id and one `workflow.node.started`;
- Run creation: one `run.created`, one `session.run.attached`, and exact
  `active_run_ids` while nonterminal;
- Run terminal: one terminal Run event, one `session.run.detached`, terminal Run
  absent from `active_run_ids`, and no duplicate external effect;
- node completed/failed: one exact node projection and consistent Workflow
  snapshot;
- Workflow completed/failed: one terminal Workflow event, one
  `session.workflow.detached`, terminal Workflow absent from
  `active_workflow_run_ids`, and stable Session status.

SQLite reopen cases must close the faulting connection and read through a
newly opened connection. Repeat recovery once more to prove idempotent terminal
attachment/detachment and clean local registries.

## Quality I1 - Deterministic bounded synchronization

- Remove hard-coded one-second owner/arrival windows.
- No barrier wait may be unbounded.
- Use explicit arrival/owner/release events for semantic coordination and one
  shared, sufficiently wide diagnostic timeout at the outer test boundary.
- On timeout, release peers in `finally` and include arrival counts plus durable
  state in the assertion/diagnostic so a regression fails instead of hanging.
- Keep the suite practical; factor the new backend and barrier machinery rather
  than duplicating large bodies.

## Required verification

- Exact new public-surface RED/GREEN.
- Complete two-backend/two-SDK matrix above.
- Complete ambiguous-commit ownership matrix.
- Phase 4A + 4B admission file and Workflow recovery/ownership neighbors.
- Run RecoveryAPI/live/lease/reconciliation, Session deletion/lifecycle,
  Provider/Tool/MCP/permission, construction/idempotency neighbors.
- Full Python 3.13 with zero skips, Ruff, mypy, diff/import/signature/scope/schema
  checks.
- Update `M02-T002-phase4b-report.md`, commit, and stop for a fresh whole-Phase-4
  independent Spec/Quality review. Do not claim approval.
