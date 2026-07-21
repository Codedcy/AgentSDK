# v0.1 R4 Task 3 — Generalize Durable Child Coordination

## Outcome

Task 3 is implemented without registering the Task 4 control Tools. Ordinary
Runs and Workflow-owned Runs now create Children through one durable
`ChildCoordinator`; SDK-created Workflow/API Children share the same
process-local concurrency gate.

The public facade is available as `sdk.children` with lifecycle-admitted
`spawn`, `send_message`, `wait`, and `list` operations. `send_message` reuses
the Task 2 `MailboxService` and retains its same-Session/direct-relation rules.

## TDD evidence

The first ordinary-parent test failed because `SubagentService.spawn` required
`workflow_run_id` and `workflow_node_id`. After making the ownership pair
optional (while rejecting half-bound identities), ordinary spawn and the old
Workflow-owned slice passed together.

The capability test first failed because the service had no per-Run Tool
catalog input. The implementation now computes the non-expanding intersection
in this order:

1. Session Tool/workspace capability;
2. durable parent effective capability (legacy `None` inherits Session);
3. `TaskEnvelope` capability;
4. Child `AgentSpec` capability.

A legacy-intermediate-parent regression then exposed a second RED: a restricted
root could be expanded when its direct Child had no persisted descriptor. Spawn
now walks the authenticated ancestor chain and intersects every persisted
effective descriptor, while legacy `None` descriptors continue to mean
inheritance rather than expansion.

Explicit empty tuples remain empty. Child input is only the canonical rendered
`TaskEnvelope`, including its explicit evidence refs; parent conversation and
output are not copied.

Limit tests were observed RED before their checks were added: depth and
per-Session cases created the forbidden Child, and the concurrency case let the
second Child complete while the first was blocked. GREEN behavior rejects
depth/per-parent/per-Session overflow before `run.created`; the semaphore wraps
the whole `RunEngine.execute` call, so excess durable Children remain
`RunStatus.CREATED` and list as `queued` until a slot releases.

The shared-gate test found a second real RED after Coordinator injection:
Workflow recovery-created Children still bypassed the Coordinator and listed as
`running`. The missing/new-child recovery branch now uses the injected
Coordinator; existing durable Children retain the existing RecoveryAPI path.
The regression now observes Workflow Child `queued` behind an API Child and
execution only after release.

Public wait RED/GREEN covered bounded pending, completed, and failed outcomes.
`asyncio.wait` observes the local/recovery task without cancelling it. Ordinary
Child failure is returned as `ChildWaitResult(status="failed", error=RunFailure)`
rather than raised. Invalid identity/ownership/storage remains an
`AgentSDKError`.

## Durable relation, counting, and progress audit

`ChildCoordinator` authenticates keyed `RunSnapshot` data against its exact
same-Session `run.created` evidence with `run_created_event_matches`, including
the existing schema-v1/v2 compatibility path and current schema v3. Durable
per-parent and per-Session counts include terminal Children and use only those
authenticated same-Session Runs. Deleting a closed Session removes its
snapshots/events, and a new Session does not inherit the old count.

Depth walks the durable parent chain, enforces same-Session ownership, and
rejects missing/cyclic/corrupt relations. Unknown Agent revisions retain the
public NOT_FOUND error; a damaged registry entry is normalized to a non-leaking
INTERNAL error. Both cases are rejected before Run creation.

`ChildProgress.created_at` is the authenticated `run.created.occurred_at`.
`updated_at` is the latest durable event timestamp for that Child, never a
process-local reconstruction. SQLite reopen therefore returns the same
relationship, terminal result, status, depth, and timestamps. Status mapping is
`created→queued`, `running→running`, both waiting statuses→`waiting`, and direct
terminal/interrupted mappings.

If Run persistence succeeds but process task construction fails, spawn returns
the durable CREATED Child. It remains observable/queued and can follow the
existing recovery path on a later wait or reopen.

## Public RunFailure compatibility

`RunFailure` was moved to the cycle-free `runtime/failures.py` module solely so
`ChildWaitResult` can use the exact public type without importing
`runtime.models` back through `subagents.models`. `runtime.models` explicitly
re-exports the same class, so both the historical
`from agent_sdk.runtime.models import RunFailure` and top-level
`from agent_sdk import RunFailure` imports remain identity-compatible. A focused
regression and direct import smoke verify this.

## Verification

Baseline before Task 3 changes:

```text
unit/subagents + integration/subagents + integration/workflow:
322 passed in 57.19s
```

Task 3 focused gate:

```text
tests/integration/subagents/test_child_coordinator.py
tests/integration/subagents/test_child_run_slice.py
28 passed in 5.85s
```

Subagent and Workflow regression gate:

```text
tests/unit/subagents tests/integration/subagents tests/integration/workflow
339 passed in 73.56s
```

Task 1/2 capability/mailbox/context smoke:

```text
79 passed in 4.01s
```

Static and diff gates:

```text
mypy --strict src: Success, 96 source files
ruff check src tests/integration/subagents tests/integration/workflow: passed
git diff --check: passed
```

The final post-report verification is recorded in the task handoff. No baseline
failure required an out-of-scope production change.

## Scope exclusions preserved

- No Task 4 Agent control Tool is registered.
- No detach/cancel propagation, budgets, fairness, distributed scheduling, or
  cross-Session/arbitrary Run messaging was added.
- SDK close retains the existing lifecycle contract: tracked Child execution is
  awaited, not implicitly cancelled; bounded public waits do not cancel it.
