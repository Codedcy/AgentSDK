# M02-T002 Phase 4A Implementation Report

## Outcome

Phase 4A implementation and its first independent-review fixes are complete and
ready for re-review. This slice adds exact sequential Workflow recovery
admission and coordination; it does not claim Phase 4 completion and does not
begin Phase 4B cross-SDK fault hardening.

## Delivered behavior

- Added the sole new public entry
  `RecoveryAPI.recover_workflow(workflow_run_id) -> WorkflowHandle` while
  preserving read-only `WorkflowAPI.resume` behavior.
- Terminal Workflows return detached handles before capability admission.
  Legacy nonterminal Workflows return bounded
  `CONFLICT / recovery required / retryable=True` without mutation.
- Current nonterminal Workflows reconstruct and compare the exact persisted
  Workflow descriptor from the live Workflow IR, AgentSpecs, complete ToolSpecs
  (including retry/execution metadata), and effective Policy. Session existence
  and exact active-Workflow ownership are checked at admission and before every
  recovery transition/create boundary.
- Normal start and explicit recovery reuse `WorkflowExecutor._active` and
  `_start_lock`. Recovery admission is cancellation-shielded and Workflow tasks
  remain under SDK lifecycle tracking.
- Pending nodes are selected by exact Workflow CAS. Missing selected Runs are
  created with the exact persisted id; explicit recovery uses one deterministic
  per-Workflow/node idempotency key and reloads the authoritative Run after
  command replay. Normal live execution preserves its pre-existing command
  idempotency record count.
- Parent and child Runs are created without external work. Child parent id,
  rendered TaskEnvelope, input, and per-node ExecutionDescriptor are exact.
  Child admission also authenticates the exact durable parent Run, its terminal
  result, and its Workflow projection. Parent validation is part of the child
  creation commit, so a concurrent parent mutation cannot create the child.
  Every nonterminal selected Run is then routed through the existing per-Run
  recovery registry and Run lease admission.
- Exact related-Run validation covers session, Workflow, node, Agent, input,
  parent, envelope, current compatibility marker, and complete per-node
  descriptor.
- Run COMPLETED/FAILED states are projected through exact Workflow/node/session
  preconditions. Projection conflicts reload and converge. A
  `recovery required` follower/no-owner/shutdown diagnostic leaves the Workflow
  active instead of persisting a synthetic failure.
- Capability descriptors are synchronously revalidated after every awaited
  Session admission and immediately before every explicit-recovery Workflow
  mutation. Normal live execution and explicit recovery reload and converge
  when they race on selected-Run creation or Workflow projection.
- Explicit `RuntimeCommands.start_run(run_id=...)` idempotency now fingerprints
  and validates the exact selected id. Calls that omit `run_id` retain the
  established generated-id replay contract.

## TDD evidence

Initial RED command selected four contracts. It produced one pass and three
expected failures:

- explicit selected Run id substitution did not conflict because `run_id` was
  absent from the fingerprint;
- terminal and legacy Workflow recovery failed because `RecoveryAPI` had no
  `recover_workflow` method;
- omitted-id generated replay already passed and remained unchanged.

Subsequent REDs proved the missing loop-level descriptor re-admission, missing
Session ownership admission, normal Workflow bypass of the per-Run recovery
registry, and incorrect conversion of `recovery required` into durable Workflow
failure. A full-suite RED also caught an extra normal-execution idempotency
record; the fix confines deterministic node keys to explicit recovery repair.

Independent review then supplied three additional RED groups:

- a capability mutation during the awaited Session load caused Workflow state
  mutation before the mismatch was rejected;
- eleven missing, foreign, mismatched, legacy, nonterminal, or projection-
  inconsistent durable parent variants were accepted for child recovery;
- normal live execution and explicit recovery did not converge at the
  selected-Run-missing and precreated-Run projection race boundaries.

All twelve capability/parent REDs and both concurrency REDs now pass. The final
focused Phase 4A admission matrix is **51 passed**. It covers Memory/SQLite
explicit Run-id behavior, terminal success/failure, legacy/current admission,
every Agent/Tool/Policy mismatch requested by the brief, pending/missing/
CREATED/COMPLETED/FAILED/waiting Run boundaries, durable child-parent
authentication and projection exactness, all related-Run substitutions,
same-SDK attachment, cancellation shielding, registry cleanup, normal-live Run
recovery routing, race convergence, and active diagnostic preservation.

## Fresh verification

- Workflow recovery admission: **51 passed**.
- Independent-review capability/parent cases: **12 passed**; selected-Run and
  projection race cases: **2 passed**.
- Explicit Run-id idempotency contracts: **3 passed**.
- Existing Workflow recovery/session ownership plus Run RecoveryAPI/session
  ownership regression selection: **270 passed**.
- Phase 3 Provider/Tool recovery selection: **261 passed**.
- Final Python 3.13 full suite: **1631 passed**, zero failed, zero skipped, in
  122.84 seconds.
- Ruff: all `src` and `tests` checks passed.
- mypy: 75 source files passed.
- `git diff --check`: passed.
- Public import/signature smoke: `RecoveryAPI.recover_workflow` has the exact
  required signature; all 99 unique root exports remain available.
- Schema/scope: SQLite remains schema version 3; storage, migration, roadmap,
  milestone, and task-index diffs are empty.

## Changed files

- `src/agent_sdk/api.py`
- `src/agent_sdk/runtime/commands.py`
- `src/agent_sdk/workflow/executor.py`
- `tests/integration/runtime/test_run_session_ownership.py`
- `tests/integration/workflow/test_workflow_recovery_admission.py`
- `.superpowers/sdd/M02-T002-phase4a-report.md`

## Concurrency, cancellation, and sanitization reasoning

The Workflow `_start_lock` serializes local admission and `_active` provides one
same-SDK coordinator. Caller cancellation cannot cancel coordinator admission or
the shared Workflow task. Selected Run execution delegates to RecoveryAPI's
existing `_tasks` registry, whose Run recovery service owns lease acquisition,
follow/reload, reconciliation, shutdown, and context-free error behavior.
Selected-Run create ambiguity is accepted only after the authoritative durable
Run exactly matches the Workflow relation and descriptor. Child creation carries
an exact, data-bearing parent precondition. Workflow CAS losers reload only when
the durable Workflow itself changed, preserving Session-conflict semantics while
allowing normal and recovery coordinators to converge. The Workflow recovery
task catches errors outside their original exception context and exposes only
bounded SDK errors. Durable Run failures are projected from their sanitized
snapshot; raw Store/Provider/Tool state is not included in public messages.

## Explicit exclusions

- No Workflow-wide lease, scheduler epoch, durable queue, parallel/branching
  scheduler, or M04-T002 behavior.
- No reconciliation resolution actions.
- No Phase 4B two-SDK concurrency matrix or crash/fault-injection hardening.
- No Provider/Tool safety inference and no LiteLLM-only recovery certification.
- No schema or migration change.

Independent Spec and Quality C0/I0 re-review is required before Phase 4B.
