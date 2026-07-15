# M02-T002 Phase 4 Operational Plan

Source of truth: `docs/plans/tasks/M02-T002-leases-reconciliation.md`.
This file partitions only the approved Phase 4 sequential Workflow recovery
scope. It does not add Workflow-wide scheduler ownership, reconciliation
resolution actions, or M04 behavior.

## Required public contract

- Add only `RecoveryAPI.recover_workflow(workflow_run_id: str) -> WorkflowHandle`.
- Keep `WorkflowAPI.resume` read-only: it may attach to a same-SDK active task or
  return terminal durable state, but it never starts recovery or external work.
- Construction/startup scan still performs no Workflow, Provider, Tool, or MCP
  execution.
- A terminal Workflow returns a detached handle before capability admission.
- A nonterminal legacy Workflow never auto-resumes and returns the bounded
  `recovery required` diagnostic without mutation or external work.

## Phase 4A - Exact admission and single-coordinator recovery

1. Make explicit-`run_id` Run creation durably idempotent. When `start_run`
   receives an explicit Run id, include it in the idempotency fingerprint and
   require the replay result to contain that exact id. Preserve the existing
   generated-id replay behavior when the caller omits `run_id`.
2. Add exact nonterminal Workflow admission by reconstructing the persisted
   `WorkflowExecutionDescriptor` from the current Workflow IR, registered
   AgentSpecs, full Tool capabilities (including execution/retry metadata), and
   effective Policy. Missing, changed, malformed, or legacy capabilities fail
   before Workflow/Run writes or external work.
3. Reuse `WorkflowExecutor._active` and `_start_lock` for normal start and
   recovery. Do not create an independent Workflow task registry. A same-SDK
   active normal/recovery coordinator is reattached; terminal coordinators are
   released. Recovery admission is cancellation-safe and all tasks remain under
   SDK lifecycle tracking.
4. Drive only the M01 strict sequential state machine:
   - exact-CAS a pending node to `RUNNING` with one selected Run id;
   - idempotently ensure that exact selected Run exists using a deterministic
     per-Workflow-node key;
   - for a child Run, persist the exact parent, rendered TaskEnvelope, and
     per-node current ExecutionDescriptor without launching it during creation;
   - validate an existing Run's complete Workflow/session/node/agent/input/
     parent/envelope/descriptor relation;
   - route every nonterminal selected Run through the existing Run recovery
     coordinator so RunEngine must win the Run lease before Provider/Tool/MCP;
   - project terminal Run success/failure and Workflow terminal state using the
     existing exact Workflow/node/session preconditions.
5. CAS losers reload and converge. A projection conflict never becomes a
   durable Workflow failure. A selected missing Run after a prior crash may be
   recreated only with the exact persisted id and deterministic idempotency
   identity.
6. Preserve nonterminal recovery diagnostics: a selected Run waiting for
   reconciliation, an expired follower without a new owner, or SDK shutdown
   leaves the Workflow active and returns `recovery required`; it is not
   converted to Workflow failure.

Phase 4A RED/GREEN coverage:

- explicit Run-id replay exactness and conflict behavior;
- terminal Workflow detached recovery without registered capabilities;
- legacy and Agent/Tool/Policy/descriptor mismatch admission with zero writes
  and zero external work;
- single-SDK pending-node, selected-CREATED-Run, terminal-Run projection, child
  Run, failed Run, and waiting-reconciliation boundaries;
- exact related-Run substitution/corruption rejection;
- cancellation-safe coordinator registration and registry cleanup.

Commit and obtain an independent Spec C0/I0 and Quality C0/I0 review before
Phase 4B.

## Phase 4B - Cross-SDK concurrency and fault hardening

1. Exercise two independently constructed SDK instances over one durable Store
   at pending-node, selected-CREATED-Run, Run-terminal/node-unprojected, and
   node-terminal/Workflow-unprojected boundaries. CAS and idempotency losers
   reload or safely reattach; they never create a second logical Run.
2. Prove exactly one Provider call and exactly one Tool/MCP side effect across
   two SDK recovery coordinators. Cover a normal live Workflow racing explicit
   recovery so a Run lease loser follows durable state instead of failing the
   Workflow.
3. Fault-inject cancellation/store ambiguity after node selection, Run
   creation, Run terminal commit, node terminal commit, and Workflow terminal
   commit. Reopen and recover to one legal projection without replaying a
   completed external effect.
4. Cover lifecycle close, session close/delete races, same-SDK 20-call
   deduplication, terminal cleanup, Store/capability/provider secret
   sanitization, and context-free public tracebacks.
5. Run focused Workflow/Run-recovery regressions, all prior Phase 3 recovery
   suites, full Python 3.13, Ruff, mypy, diff/scope/schema/import checks, and an
   independent Phase 4 review. Python 3.12 and packaging remain the Phase 5
   release gate unless a compatibility issue requires an earlier run.

## Explicit exclusions

- No Workflow lease table, scheduler lease, ownership epoch, durable queue, or
  parallel/branching scheduler; those remain M04-T002.
- No `ReconciliationAction` implementation or user decision resolution; that
  remains outside the approved Phase 4 partition.
- No inferred Provider/Tool retry safety and no LiteLLM-only recovery
  certification.
- No change to schema version 3 unless implementation evidence proves an
  unavoidable source-contract requirement and the plan is revised first.

## Phase 4 release condition

Phase 4 is complete only after both slices are committed, reports record fresh
test evidence, and an independent reviewer reports Spec C0/I0 and Quality
C0/I0. Only then may Phase 5 begin.
