# Agent SDK v0.1 R4 Task 4 Report

## Scope

Implemented Task 4 only from
`docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r4-child-mailbox.md` on baseline
`aaacb5b`.

The change registers the exact Agent control Tool names `spawn_agent`,
`send_message`, `wait_child`, and `list_children`, routes them through the normal
per-Run Tool catalog, permission broker, Tool executor, recovery metadata, and
durable event pipeline, and extends the v0.1 acceptance scenario through a
deterministic parent/Child exchange. No R4 Task 5 checkpoint or release-ledger
files were modified.

## TDD evidence

The first focused RED executed `spawn_agent` through a descriptor-selected
`ToolCatalog` and real `ToolExecutor`; it failed with
`ToolResultStatus.FAILED: tool not found`. The full pre-implementation matrix then
reported `15 failed`, all caused by the four missing registrations. It covered the
four Tool pipelines, closed payloads, model-supplied identity rejection, unknown
Agent, Child limit rejection, invalid direct relation, `allow`/`deny`/`ask`,
pending/completed/failed waits, per-Run capability removal, ancestor
non-expansion, and exact-name initialization collision.

After registration the matrix passed `15 passed`. A later configuration-contract
RED proved that `enable_builtin_tools=False` still exposed the new Tools. Because
all four specifications have `source="builtin"`, registration was placed under
the existing SDK builtin switch while preserving default registration. The
expanded focused matrix then passed `16 passed`.

## Implementation

- Added closed JSON Schemas and exact effects:
  - `spawn_agent`: `agent.spawn`
  - `send_message`: `agent.message`
  - `wait_child`: `agent.inspect`
  - `list_children`: `agent.inspect`
- Every handler receives `ToolContext` as the executor-supplied first argument.
  Parent/sender identity comes only from `context.run_id`; no Tool schema accepts
  a caller, sender, or parent identity.
- `spawn_agent` validates the nested payload with frozen, extra-forbidden
  `TaskEnvelope.model_validate` and delegates to the shared `ChildCoordinator`.
- `send_message` delegates to the shared durable `MailboxService`.
- `wait_child` always supplies
  `expected_parent_run_id=context.run_id`; `list_children` lists only
  `context.run_id`.
- Handler results are detached JSON dictionaries/lists. No Run snapshot, Pydantic
  model, or internal mutable object crosses the Tool result boundary.
- SDK initialization now constructs the Coordinator, constructs one shared
  Mailbox service, atomically checks exact-name collisions, registers the control
  Tools into the same registry already referenced by `RunEngine`, and only then
  constructs Workflow/public APIs. A collision fails initialization with the
  conflicting exact name instead of partially or silently overwriting the
  registry.
- Default registration remains subject to Task 1 descriptor-selected catalogs.
  Explicit parent capability removal produces `tool not found`, and Child
  capability intersection cannot restore a Tool removed by an ancestor.

## Deterministic v0.1 acceptance scenario

The new fake LiteLLM scenario uses `asyncio.Event` handshakes rather than timing
sleeps. The parent model:

1. calls `spawn_agent`;
2. calls `send_message`;
3. calls `list_children`;
4. calls `wait_child` and observes `pending` while the Child is gated;
5. receives the Child-to-parent mailbox message through a newly prepared Context
   View and calls `wait_child` to terminal;
6. returns a terminal answer containing the Child output.

The Child consumes the parent message through its own automatic Context View,
calls `send_message` back through its descriptor-selected Tool pipeline, and then
completes. Assertions cover direct durable ownership, Tool/workspace intersection,
both mailbox message ids in the corresponding Context View `message_refs` and
`consumed_message_ids`, public Child progress, public execution tree, parent and
Child Tool event counts, model/Context correlations, and terminal output use.

## Verification

- Task 4 focused child Tools plus new v0.1 scenario: `17 passed`.
- Task 1-3 capability/mailbox/Context/Coordinator smoke: `121 passed`.
- Workflow integration regression: `274 passed`.
- All unit/integration subagents, integration Context, and v0.1 E2E except the
  known baseline node below: `198 passed, 1 deselected`.
- Strict typing: `Success: no issues found in 97 source files`.
- Ruff over `src` and `tests`: `All checks passed!`.
- `git diff --check`: clean.

### Known baseline debt

`tests/integration/context/test_context_recovery.py::test_authoritative_recovery_receives_exact_stored_prepared_request`
continues to fail with `AgentSDKError: recovery required`. The same exact node was
already established as failing on the Task 4 baseline. With
`enable_builtin_tools=False`, the current descriptor and prepared request again
contain an empty Tool catalog, yet the status and recovery event shape remain the
same. Task 4 does not modify the Context/recovery implementation; the node was
therefore recorded and explicitly deselected only for the otherwise-clean broad
gate.
