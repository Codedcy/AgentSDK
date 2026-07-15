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
