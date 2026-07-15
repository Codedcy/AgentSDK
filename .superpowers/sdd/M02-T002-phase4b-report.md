# M02-T002 Phase 4B Implementation Report

## Outcome

Phase 4B adds deterministic cross-SDK concurrency, ambiguous-commit, lifecycle,
negative-admission, and sanitization coverage without changing production code,
the public API, Workflow states, or schema. The Phase 4A implementation already
satisfied every exercised Phase 4B contract. This report records implementation
evidence only; it does not claim independent approval and does not begin Phase 5.

## Added hardening coverage

The Phase 4A admission file grew from 51 to 86 tests. The 35 new tests cover:

- two SDKs racing a pending node and a selected CREATED Run, with deterministic
  node-commit and Run-lease barriers;
- Memory plus SQLite (same database, two independently opened connections) CAS
  convergence at terminal Run/unprojected node and terminal node/unprojected
  Workflow boundaries;
- Provider -> Tool -> Provider, real `MCPManager`-registered MCP Tool, and ASK
  permission flows across two SDKs and both Stores, proving one logical external
  side effect, one durable permission request, and one decision;
- post-commit cancellation after `workflow.node.started`, `run.created` plus
  Session attachment, Run terminal plus Session detachment,
  `workflow.node.completed`/`workflow.node.failed`, and Workflow completed/failed
  plus Session detachment. Memory covers every boundary; Run and Workflow
  terminal boundaries additionally close and reopen SQLite;
- selected-Run substitution and forged child-parent projection on Memory and
  SQLite with zero recovery mutation or external work;
- closing Session behavior for a pending Workflow and exact projection of an
  already-terminal selected Run on Memory and SQLite;
- `AgentSDK.close()` settling an in-flight multi-turn Workflow recovery, clearing
  the Workflow/Run recovery registries, rejecting later recovery admission, and
  permitting no later Provider or Tool calls;
- Store, Provider, Tool, MCP, and permission secret handling. Public Store,
  Provider, and permission errors have no secret in message, cause, context,
  formatted traceback, or SDK frame locals. Tool and MCP failures follow their
  established normalized failed-ToolResult contract and leave no secret in the
  public result, Run snapshot, or durable events.

Existing Phase 4A tests retained and re-proved same-SDK 20-call fan-in,
capability mismatch zero mutation, complete selected-Run/child-parent relation
matrices, and normal-live versus explicit-recovery convergence. Existing
Workflow and RecoveryAPI neighbors retained deletion-race, construction/startup
no-execution, provider/store sanitization, Session ownership, and cleanup
coverage.

## TDD evidence

Every new case was added before considering a production change.

- The initial two-SDK pending/CREATED slice was GREEN: 2 passed.
- Run/node and node/Workflow projection matrices were GREEN: 4 passed.
- Provider/Tool/MCP/permission side-effect matrices were GREEN: 6 passed.
- The first ambiguous-commit run had 7 passes and 2 assertion failures. Evidence
  showed the exact durable `run.completed` plus `session.run.detached` batch had
  committed. The failed assumption was that RunHandle must propagate its Store
  child-task cancellation; the approved contract instead reloads the durable
  terminal Run and lets the Workflow coordinator converge. The test was corrected
  to require that authoritative convergence followed by a real reopen. The final
  matrix is 9 passed; production was unchanged.
- The first sanitization run had 3 passes and 2 assertion failures. The failed
  assumption was that Tool and MCP handler failures terminate the Workflow with
  an exception. Their established contract normalizes those failures to failed
  ToolResults and continues the next Model turn. Tests were corrected to inspect
  public/durable results. The final matrix is 5 passed; production was unchanged.

No RED exposed a production defect, so no production edit was authorized or
needed for Phase 4B.

## Fresh verification

- Complete Phase 4A + Phase 4B Workflow admission file: **86 passed**.
- Workflow recovery/admission/Session ownership combined: **146 passed**.
- Ambiguous durable-commit matrix: **9 passed**.
- Two-SDK concurrency and external-side-effect matrix: **12 passed**.
- Memory/SQLite selected-Run and forged-parent negatives: **4 passed**.
- Memory/SQLite closing-Session matrix: **4 passed**.
- Store/Provider/Tool/MCP/permission sanitization matrix: **5 passed**.
- Construction/startup no-Workflow-execution selection: **3 passed**.
- Workflow delete/provider/store neighbor selection: **4 passed**.
- Final Python 3.13 full suite: **1666 passed**, zero failed, zero skipped, in
  126.69 seconds.
- Ruff: all `src` and `tests` checks passed.
- mypy: 75 source files passed.
- `git diff --check`: passed.
- Public import/signature smoke: 99 unique root exports; exact
  `(self, workflow_run_id: str) -> WorkflowHandle` contract retained.
- Scope/schema: Phase 4B changes only
  `tests/integration/workflow/test_workflow_recovery_admission.py` plus this
  ignored implementation report; no production, migration, design, roadmap,
  milestone, task-index, dependency, or lockfile diff; SQLite remains schema v3.

## Explicit exclusions

- No Workflow lease, scheduler epoch, durable queue, new Workflow state,
  parallel/branching scheduler, or M04 behavior.
- No reconciliation resolution action.
- No public API or schema expansion.
- No Phase 5, M02-T003, or M02-T004 work.

Phase 4B implementation evidence is ready for independent Phase 4 Spec and
Quality review.

## First whole-Phase-4 review

The independent review was **Not Approved**: Spec Compliance C0/I3/M0 and Task
Quality C0/I1/M0. It found an unintended public `WorkflowExecutor.recover`
entry, incomplete two-backend/two-SDK pending/missing/CREATED/live/ownership and
Session-delete coverage, incomplete Session attach/detach assertions at
ambiguous commit boundaries, and brittle mixed one-second/unbounded test
barriers. The bounded remediation is recorded in
`M02-T002-phase4-review-fix-brief.md`; Phase 5 remains blocked.

## Whole-Phase-4 review remediation

The first whole-Phase-4 review findings were remediated within the exact
review-fix boundary. This is implementation evidence for a new independent
review; it does not claim approval and does not begin Phase 5.

### Public recovery surface

- Added a public-surface regression that requires
  `RecoveryAPI.recover_workflow(self, workflow_run_id: str) -> WorkflowHandle`
  to remain the sole new public recovery entry.
- The RED failed because exported `WorkflowExecutor` still exposed public
  `recover(...)` callback injection.
- Renamed that internal entry to `_recover` and changed only the internal API
  assembly call. `WorkflowAPI.resume`, root exports, behavior, and schema were
  unchanged.

### Complete two-SDK matrix

Memory and SQLite now run the same deterministic, independently constructed SDK
matrix. SQLite always uses two independently opened connections to one database.

- Pending node: both callers converge on one exact selected Run id, one
  `workflow.node.started`, one `run.created`, one Session attachment, and one
  Provider execution.
- Selected RUNNING node with a missing Run: the exact selected id is recreated
  once, attached once, and executed once.
- Selected CREATED Run: two lease contenders produce one lease owner, one
  logical Provider execution, and the same terminal Workflow result.
- Selected live Run with a valid lease owner: the second SDK follows durable
  state without recording a synthetic node or Workflow failure.
- Expired/unreconciled Run: both callers receive the bounded retryable
  `CONFLICT / recovery required` outcome; the Run remains
  `WAITING_RECONCILIATION`, Workflow/node/Session ownership remains active, and
  no terminal projection or Provider work is recorded.
- Authoritative Session deletion: a deterministic two-SDK node-selection race
  deletes the Session before either candidate commit. Both recoveries return
  `NOT_FOUND`; Session, Workflow, nodes, candidate Runs, events, idempotency,
  leases, checkpoints, unresolved operations, and reconciliation requests stay
  absent, with clean local registries.

### Ambiguous-commit ownership proof

All nine existing post-commit fault cases now assert complete durable ownership
pairs instead of only the headline event count. They prove the exact selected
Run id, Run attach/detach, active ownership while nonterminal, terminal absence
from Session active ids, exact node projection consistency, stable Session
status, terminal checkpoint ownership, no unresolved external work, and one
Workflow detach. SQLite terminal cases close the faulting connection and read
through a newly opened connection. A second recovery proves terminal
idempotency, unchanged events and Session state, one Provider execution, and
clean Workflow/recovery registries.

### Deterministic synchronization

- Replaced one-second and direct barrier waits with one shared 10-second
  diagnostic timeout.
- Arrival, winner, owner, and release conditions use explicit events.
- Timeout diagnostics include the coordination phase, arrival counts, selected
  ids, or durable Run state as applicable.
- Every Projection, Lease, Deletion, Provider, Run-read, and plan release is
  opened in `finally`, so a failed assertion cannot strand its peer.

### Fresh verification after remediation

- Exact public-surface RED/GREEN: **1 passed** after the intentional RED.
- Complete Memory/SQLite two-SDK state matrix: **10 passed**.
- Authoritative deletion race: **2 passed**.
- Ambiguous durable-commit ownership matrix: **9 passed**.
- Complete Phase 4A + Phase 4B Workflow admission file: **97 passed**.
- Final Python 3.13 full suite: **1677 passed**, zero failed, zero skipped, in
  127.07 seconds.
- Ruff: all `src` and `tests` checks passed.
- mypy: 75 source files passed.
- `git diff --check`: passed.
- Public import/signature smoke: 99 unique root exports; exact
  `(self, workflow_run_id: str) -> WorkflowHandle` contract retained;
  `WorkflowExecutor.recover` is absent from its public class dictionary.
- Scope/schema: production edits are limited to the internal executor rename
  and its internal caller; tests are limited to the Phase 4 admission file;
  SQLite remains schema v3. No migration, design, roadmap, milestone,
  task-index, dependency, or lockfile change was made.

The remediation is ready for a fresh whole-Phase-4 independent Spec/Quality
review. Phase 5, M02-T003, and M02-T004 remain blocked pending that verdict.
