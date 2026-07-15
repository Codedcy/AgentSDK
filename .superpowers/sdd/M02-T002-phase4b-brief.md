# M02-T002 Phase 4B Implementer Brief

## Objective

Implement the cross-SDK concurrency and fault-hardening slice from
`M02-T002-phase4-plan.md`. Phase 4A is independently approved at `e3d2965`.
Preserve every Phase 4A public and persistence contract. This brief covers only
M02-T002 Step 4 / Phase 4B; it does not complete M02-T002 by itself.

## Scope exclusions

- No Workflow-wide lease, scheduler epoch, durable queue, new Workflow state,
  schema migration, reconciliation resolution action, parallel/branching
  scheduler, or M04 behavior.
- Do not expand the public API beyond the approved Phase 4A entry point.
- Do not begin Phase 5, M02-T003, or M02-T004.

## Required two-SDK concurrency matrix

Use two independently constructed SDKs over the same Store and independently
register the exact capabilities. Cover Memory and SQLite using separate SQLite
connections where applicable.

1. Pending node with no selected Run: one selected Run and no loser-generated
   Run.
2. RUNNING node with selected Run missing: recreate the exact selected id,
   exactly one Run, and exactly one `run.created` event.
3. Selected Run in CREATED: exactly one Run-lease owner; the other SDK follows.
4. Live/interrupted selected Run: follow a valid owner lease; expired or
   unreconciled ownership returns bounded `recovery required` without recording
   a synthetic Workflow failure.
5. Terminal Run with an unprojected node: CAS losers reload and converge.
6. Terminal node with an unprojected Workflow: CAS losers reload, converge, and
   detach correctly.
7. Retain the approved normal-live versus explicit-recovery convergence cases.

## External-side-effect matrix

- Provider-only node: exactly one logical Provider execution.
- Provider -> Tool -> Provider: Tool handler exactly once and logical Model
  calls exactly once per required turn.
- MCP-backed Tool: counting fake transport/handler executes exactly once.
- Permission ASK: one durable request, one decision, and one Tool execution;
  losing coordinators do not call the permission bridge or handler.
- Exercise clean owner completion, caller cancellation, SDK close, valid-lease
  following, and bounded handoff/recovery-required outcomes.

## Ambiguous-commit fault injection

Inject an exception after each durable commit boundary below, reopen/recover,
and prove the durable state is reused without duplicating externally visible
work:

- `workflow.node.started` / node selection.
- `run.created` plus Session attachment.
- Run terminal projection plus Session detachment.
- `workflow.node.completed` or `workflow.node.failed`.
- Workflow terminal projection plus Session detachment.

## Negative, lifecycle, and sanitization cases

- Changed capability descriptors fail before mutation or external work.
- A substituted selected Run or forged child/parent relation performs zero
  external work on Memory and SQLite.
- Session close while pending prevents new Run creation. If the selected Run is
  already terminal, its exact projection may finish safely.
- Session deletion racing recovery never resurrects Session, Run, Workflow,
  event, permission, idempotency, or lease state.
- `AgentSDK.close()` settles Workflow recovery tasks and permits no post-close
  external calls.
- Same-SDK fan-in of at least 20 callers remains one coordinator.
- Public messages, causes, contexts, tracebacks, and retained frame locals do
  not expose secrets from Store, Provider, Tool, MCP, or permission failures.
- Construction/import scans continue to prove that SDK construction performs no
  Workflow execution.

## TDD and gates

Write a failing test before every production change. Keep tests deterministic;
do not use timing luck as the correctness condition. Run at minimum:

- Phase 4A and Phase 4B Workflow suites.
- Existing Workflow recovery/ownership suites.
- Run RecoveryAPI, live execution, Provider, Tool, permission, MCP, Session,
  ownership, idempotency, and construction/import neighbors.
- Full Python 3.13 suite with zero skips.
- Ruff, mypy, `git diff --check`, public import/signature smoke, scope audit, and
  schema-version audit.

Write `.superpowers/sdd/M02-T002-phase4b-report.md`, commit the implementation,
and stop for independent Spec and Quality review. Do not start Phase 5.
