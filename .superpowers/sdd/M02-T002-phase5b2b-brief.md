# M02-T002 Phase 5B2B Implementation Brief

Source of truth: `docs/plans/tasks/M02-T002-leases-reconciliation.md`,
`.superpowers/sdd/M02-T002-phase5-plan.md`, the approved Phase 4 Workflow
recovery design/report, and the approved Phase 5A/5B1/5B2A reports. This is the
final Phase 5B operational sub-slice. It integrates confirmed Run outcomes with
explicit Workflow recovery. It does not add evidence fields or directly mutate
Workflow state inside `RecoveryAPI.resolve`.

## Architectural boundary

`sdk.recovery.resolve(...)` remains a Run-only atomic decision. It must not
write `workflow` or `workflow_node` snapshots and must not call Workflow,
Provider, Tool, MCP, permission, or application callbacks.

Workflow projection happens only after the application explicitly calls:

```python
await sdk.recovery.recover_workflow(workflow_run_id)
```

The existing Phase 4 single-coordinator Workflow recovery path is the only
projection authority. Reuse its descriptor admission, child ownership,
parent/sequence relation, node CAS, Workflow CAS, Session lifecycle, ambiguity,
and two-SDK convergence rules. Do not create a parallel reconciliation-specific
Workflow state machine.

## Required confirmed-outcome projections

### Confirmed terminal Model text

After `CONFIRM_COMPLETED` terminalizes the running child Run:

- the Workflow node remains `RUNNING` until explicit Workflow recovery;
- Workflow recovery loads the exact terminal child Run, projects the node to
  `COMPLETED` with the exact Run output and usage, and emits the existing exact
  `workflow.node.completed` event once;
- a one-node Workflow then projects to `COMPLETED` and detaches from the
  Session; a multi-node Workflow starts the next node exactly once with the
  exact parent Run relation and task envelope;
- no Provider or Tool callback is made for the already terminal child.

### Confirmed Provider failure

After a confirmed failed Provider result terminalizes the child Run:

- explicit Workflow recovery projects the exact public failure to the running
  node and Workflow using the existing `WorkflowFailure` semantics;
- node/workflow failure events and Session detach/close occur exactly once;
- no external callback is made for the terminal child.

### Confirmed Tool result

After a Tool `CONFIRM_COMPLETED` leaves the child Run `INTERRUPTED` at
`READY_FOR_MODEL`:

- explicit Workflow recovery delegates to the approved Run recovery path;
- the confirmed Tool handler/MCP/permission side effect is never repeated;
- the next Provider turn receives the exact durable Tool message/result and is
  invoked at most once by the winning coordinator;
- terminal child success/failure is then projected to the node/Workflow through
  the normal Phase 4 path exactly once.

All legal confirmed Tool statuses, including normalized failure/denied/timeout/
invalid-arguments results, remain durable Tool results for the next Model turn;
Workflow state follows the eventual child Run result rather than treating the
Tool status itself as a Workflow failure.

## Admission, concurrency, and lifecycle rules

- The child Run must exactly own the current Workflow node: matching Session,
  Workflow id, node id, agent revision, parent Run, task envelope, execution
  descriptor, and current node `run_id`.
- Existing Workflow descriptor/capability drift fails closed before node or
  Workflow mutation and before external work.
- A pending child reconciliation makes Workflow recovery return the existing
  bounded `recovery required` failure with zero Workflow mutation; after the
  explicit decision, a new explicit recovery may continue.
- Two SDKs racing Workflow recovery converge on one Run/Workflow coordinator;
  followers observe the durable result. No duplicate Provider, Tool, MCP,
  permission, node start, node completion, or Workflow completion is allowed.
- Post-commit ambiguity at child terminal, node projection, Workflow terminal,
  or Session detach is resolved only by exact durable reload. No synthetic
  Workflow failure may be created for a legal concurrent winner.
- A closing Session closes only after both the active child Run and active
  Workflow are detached. Public delete remains busy while either is owned;
  deletion after completion removes all Run, Workflow, node, reconciliation,
  operation, checkpoint, event, and idempotency state under existing semantics.
- SDK close/cancellation cannot create a half-projected node/Workflow. A later
  SDK can recover from the durable boundary.

## Stable history and observability

After Workflow projection, exact replay of the original reconciliation decision
must remain valid and read-only. Run history, Workflow/node snapshots, Session
ownership, and all related events must remain mutually consistent. Query APIs
must show the confirmed ToolResult, terminal Run, Workflow tree, and exact node
status/output/usage/error without fabricating duplicate execution.

This slice must not change public signatures, SQLite schema version, root
exports, or the meaning of existing trace events. It may add tests and the
smallest production correction required to make the approved paths converge.

## Mandatory TDD and adversarial coverage

Use production `workflows.start`, scanner/reconciliation, public `resolve`, and
public `recover_workflow` paths. Write failing tests before production changes.
Cover Memory and SQLite for at least:

- one-node confirmed Model text -> node/Workflow completion, zero callback for
  terminal child, original decision replay after projection;
- one-node confirmed Provider failure -> exact node/Workflow failure and
  Session detach, zero external callback, replay after projection;
- one-node confirmed Tool success and normalized Tool failure -> no Tool repeat,
  one next Provider call, exact ToolResult/message, eventual node/Workflow
  completion, replay after projection;
- multi-node Workflow: confirmed first-node terminal result projects exactly,
  next child starts once with exact parent/task relation, final output/usage and
  execution tree are correct;
- child pending reconciliation before decision, explicit decision, and later
  recovery; include a later second reconciliation/resolution and stable replay
  of the first decision;
- two SDKs and both backends across terminal-child and interrupted-child races;
- post-commit ambiguity at node/Workflow/Session transitions;
- capability drift, mismatched child/node/parent/task/descriptor, corrupt
  terminal Run, corrupt Workflow/node/event/Session ownership, orphan or
  duplicate projection events, and moved lifecycle markers fail closed before
  external work or Workflow mutation;
- Session closing/delete, SDK close/cancellation, reopen from SQLite, and query/
  execution-tree observability;
- proof `resolve` itself never mutates Workflow snapshots or invokes callbacks.

Retain and rerun the Phase 4 Workflow recovery/admission/session-ownership
matrices and all Phase 5A/5B1/5B2A focused matrices.

## Gates and handoff

Run the new Workflow integration tests, all Workflow recovery/admission/
ownership tests, reconciliation/Provider/Tool/RecoveryAPI focused files,
Session/lease/Store neighbors, Ruff, mypy, imports/signatures/schema/diff/scope,
and the full Python 3.13 suite. Write
`.superpowers/sdd/M02-T002-phase5b2b-report.md` with RED/GREEN evidence, changed
files, exact gate output, commit hashes, and remaining Phase 5C scope. Commit all
work and obtain independent Spec C0/I0 and Quality C0/I0.

After Phase 5B2B approval, obtain a fresh whole-Phase-5B read-only review over
Phase 5B1 through Phase 5B2B before entering Phase 5C. Do not implement Phase
5C fault/E2E, T003, or T004 in this sub-slice.
