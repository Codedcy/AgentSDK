# M02-T002 Leases, Interruption, and Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent concurrent Run advancement and safely recover abandoned Runs
and the M01 sequential Workflow slice around external side effects.

**Architecture:** Generation-based Run leases fence writers. LiteLLM model calls
and Tool calls both persist operation-id `started`/outcome boundaries before and
after the external request. A durable `RunCheckpoint` stores the exact detached
messages, accumulated output/usage/Tool results, turn, and next phase at every
safe boundary. Stale leases mark Runs interrupted; an unresolved
model call or unsafe Tool call creates a durable reconciliation request instead
of being sent again. Recovery never starts external work in `AgentSDK`
construction: after the application re-registers AgentSpecs, Tools, MCP, and
provider recovery capabilities, an explicit recovery command verifies the
M02-T001 Run or Workflow execution descriptor before Run-lease/CAS admission.

**Tech Stack:** SQLite timestamps/generations, asyncio heartbeat, fake clock, pytest subprocess fixtures.

## Global Constraints

- Only the current lease generation may commit Run progress.
- A lease owner is a unique per-Run coordinator token, not a reusable SDK or
  process id. `acquire` rejects every unexpired lease, including one carrying
  the same token; heartbeat uses `renew`, never `acquire`. A per-SDK
  `run_id -> recovery Task` registry merges concurrent local recovery calls.
- A non-idempotent started call without a terminal outcome never auto-retries.
- A `model.call.started` operation without a durable outcome never auto-retries
  unless the provider adapter explicitly certifies same-operation-id
  idempotency or supports an authoritative status/result query. LiteLLM routing
  alone is not treated as that certification.
- Recovery advances only from a validated `RunCheckpoint` or from a pristine
  T001 `CREATED`/pre-model Run whose initial descriptor proves no external
  operation started. A pre-T002 RUNNING/WAITING Run without a reconstructable
  checkpoint enters reconciliation; event text/deltas are not guessed into a
  prompt.
- Reconciliation decisions are immutable audit events.
- `interrupted`, `waiting_reconciliation`, `paused`, and all waiting/nonterminal states remain in `SessionSnapshot.active_run_ids`; only completed, failed, or durably cancelled Runs detach.
- Legacy M01 Runs/Workflows with `execution_compatibility="legacy_unknown"`
  never auto-resume; they require an explicit terminate/reconciliation decision.
- Session close remains `closing` while interrupted or reconciliation-owned work exists, and normal delete remains busy.
- Concurrent explicit `RecoveryAPI.recover_workflow` calls must win the exact
  Workflow snapshot precondition before creating/dispatching a node, and a
  node's Run must acquire its M02 Run lease before LiteLLM, Tool, or MCP side
  effects. A losing recovery command reloads/reattaches without external work.
  M04-T002 adds a Workflow-level scheduler lease for durable multi-node
  ownership; it does not weaken the M02 per-Run side-effect fence.

---

### Task 1: Add leases and reconciliation

**Files:**
- Create: `src/agent_sdk/runtime/leases.py`
- Create: `src/agent_sdk/runtime/reconciliation.py`
- Create: `src/agent_sdk/runtime/recovery.py`
- Modify: `src/agent_sdk/runtime/engine.py`
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/runtime/session_lifecycle.py`
- Modify: `src/agent_sdk/workflow/executor.py`
- Modify: `src/agent_sdk/workflow/state.py`
- Modify: `src/agent_sdk/workflow/handles.py`
- Modify: `src/agent_sdk/api.py`
- Modify: `src/agent_sdk/storage/base.py`
- Modify: `src/agent_sdk/storage/sqlite.py`
- Create: `src/agent_sdk/storage/migrations/0003_leases.sql`
- Create: `tests/integration/runtime/test_leases.py`
- Create: `tests/integration/runtime/test_recovery_admission.py`
- Create: `tests/faults/test_model_call_unknown_outcome.py`
- Create: `tests/integration/workflow/test_workflow_recovery_admission.py`
- Create: `tests/integration/storage/test_sqlite_v3_migration.py`
- Create: `tests/e2e/test_unknown_tool_outcome.py`

**Interfaces:**
- Produces: `Lease`, `LeaseManager.acquire/renew/release`, durable
  `ModelCallOperation`, `RunCheckpoint`,
  `RecoveryAPI.recover_run/recover_workflow`, `ReconciliationRequest`,
  `ReconciliationService.resolve`, and
  `RunStatus.INTERRUPTED/WAITING_RECONCILIATION`.
- Consumes: StateStore, RunEngine, M02-T001 Run/Workflow execution descriptors,
  Workflow exact state preconditions, Tool registry/schema hashes, Tool
  idempotency metadata, and Session ownership coordinator.

- [x] **Step 1: Write lease fencing and unknown-outcome tests**

```python
@pytest.mark.asyncio
async def test_stale_generation_cannot_commit(lease_manager) -> None:
    first = await lease_manager.acquire("run_1", "worker_a")
    await lease_manager.expire_for_test(first)
    second = await lease_manager.acquire("run_1", "worker_b")
    assert second.generation > first.generation
    with pytest.raises(LeaseLostError):
        await lease_manager.assert_current(first)

@pytest.mark.asyncio
async def test_unknown_non_idempotent_call_waits_for_reconciliation(crash_fixture) -> None:
    run_id = await crash_fixture.crash_after_tool_started(idempotency="non_idempotent")
    recovered = await crash_fixture.reopen(run_id)
    assert recovered.status == "waiting_reconciliation"
    assert crash_fixture.side_effect_count == 1

@pytest.mark.asyncio
async def test_interrupted_run_keeps_closing_session_busy(crash_fixture) -> None:
    session_id, run_id = await crash_fixture.crash_with_stale_lease()
    await crash_fixture.reopen_and_mark_interrupted(run_id)
    assert (await crash_fixture.sdk.sessions.close(session_id)).status == "closing"
    with pytest.raises(SessionBusyError):
        await crash_fixture.sdk.sessions.delete(session_id)

@pytest.mark.asyncio
async def test_recovery_requires_registered_descriptor_match(crash_fixture) -> None:
    run_id = await crash_fixture.crash_before_model_call()
    reopened = await crash_fixture.reopen_without_capabilities()
    with pytest.raises(AgentSDKError, match="capabilities"):
        await reopened.recovery.recover_run(run_id)
    reopened.register_matching_capabilities()
    await reopened.recovery.recover_run(run_id)

@pytest.mark.asyncio
async def test_two_sdk_workflow_recovery_executes_provider_once(workflow_crash) -> None:
    workflow_id = await workflow_crash.abandon_at_created_run()
    first, second = await workflow_crash.reopen_two_with_matching_capabilities()
    results = await asyncio.gather(
        first.recovery.recover_workflow(workflow_id),
        second.recovery.recover_workflow(workflow_id),
        return_exceptions=True,
    )
    assert workflow_crash.provider_calls == 1
    assert one_success_or_safe_reattach(results)

@pytest.mark.asyncio
async def test_same_sdk_concurrent_recovery_attaches_once(model_crash) -> None:
    run_id = await model_crash.abandon_before_model_call()
    sdk = await model_crash.reopen_with_matching_capabilities()
    results = await asyncio.gather(*(sdk.recovery.recover_run(run_id) for _ in range(20)))
    assert len({result.run_id for result in results}) == 1
    assert model_crash.provider_calls == 1

@pytest.mark.asyncio
async def test_crash_after_provider_accept_never_resends_by_default(model_crash) -> None:
    run_id, operation_id = await model_crash.after_provider_accept_before_outcome()
    recovered = await model_crash.reopen_and_recover(run_id)
    assert recovered.status == "waiting_reconciliation"
    assert recovered.reconciliation.operation_id == operation_id
    assert recovered.reconciliation.operation_kind == "model_call"
    assert model_crash.provider_calls == 1

@pytest.mark.asyncio
async def test_checkpoint_recovery_uses_exact_messages_and_results(checkpoint_crash) -> None:
    run_id, expected = await checkpoint_crash.after_safe_tool_outcome()
    result = await checkpoint_crash.reopen_and_recover(run_id)
    assert checkpoint_crash.recovered_request_messages == expected.messages
    assert result.tool_results == expected.tool_results

@pytest.mark.asyncio
async def test_pre_t002_running_without_checkpoint_requires_resolution(legacy_crash) -> None:
    run_id = await legacy_crash.t001_running_after_model_activity()
    recovered = await legacy_crash.upgrade_and_recover(run_id)
    assert recovered.status == "waiting_reconciliation"
    assert legacy_crash.provider_calls_after_upgrade == 0

@pytest.mark.asyncio
async def test_real_v2_database_upgrades_atomically_to_v3(version_two_database) -> None:
    first, second = await open_concurrently(version_two_database, count=2)
    assert await migration_versions(first) == (1, 2, 3)
    assert await migration_versions(second) == (1, 2, 3)
```

- [x] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/runtime/test_leases.py tests/integration/runtime/test_recovery_admission.py tests/integration/workflow/test_workflow_recovery_admission.py tests/integration/storage/test_sqlite_v3_migration.py tests/faults/test_model_call_unknown_outcome.py tests/e2e/test_unknown_tool_outcome.py -v`

Expected: Lease/Reconciliation types missing.

- [x] **Step 3: Implement lease storage and heartbeat**

Lease table fields: run_id PK, owner, generation, acquired_at, renewed_at,
expires_at. The owner is a fresh coordinator token for one local Run task.
Acquire uses `BEGIN IMMEDIATE`, rejects any unexpired lease (same token
included), and increments generation only after expiry. Renewal requires exact
owner/generation and never changes generation. Engine includes generation in
progress commits. Under a per-SDK start lock, `RecoveryAPI` creates at most one
coordinator task per Run and every concurrent local caller attaches to it.

```python
async def acquire(self, run_id: str, owner: str, now: datetime) -> Lease:
    async with self._store.immediate_transaction() as transaction:
        current = await transaction.get_lease(run_id)
        if current and current.expires_at > now:
            raise LeaseHeld(run_id)
        generation = 1 if current is None else current.generation + 1
        return await transaction.put_lease(run_id, owner, generation, now + self._ttl)
```

Before every LiteLLM request, create one stable operation id and commit
`model.call.started` with request fingerprint, Run id, turn, provider/model
identity, lease generation, and recovery-capability metadata. Only after that
commit may the gateway call LiteLLM. Persist the detached normalized response,
usage, or sanitized provider failure as `model.call.completed`/`failed` under
the same operation id and lease generation before advancing the agent loop.
Both commits are generation-fenced. If a paused process returns after its lease
expired, its outcome commit is rejected and cannot overwrite reconciliation.

An unresolved started model operation is conservative `unknown_outcome` even
when the crash might have occurred before network send. Recovery may query a
provider only through an explicitly registered authoritative status adapter; it
may resend the same operation id only through an adapter that explicitly
declares provider-enforced idempotency for that request. Otherwise persist a
reconciliation request and `WAITING_RECONCILIATION`. Never infer safety merely
from model name, LiteLLM support, timeout type, or missing usage.

Add a frozen, extra-forbid `RunCheckpoint` projection with Run/Session identity,
checkpoint version, turn, phase (`ready_for_model`, `model_in_flight`,
`ready_for_tool`, `tool_in_flight`, `waiting`, `terminal`), exact detached
messages, accumulated output parts, usage, ordered Tool results, and the current
operation id when in flight. Validate phase-dependent fields and deep-freeze
all message/Tool JSON. The initial checkpoint is derived from the T001
`ExecutionDescriptor` before the first model operation. Each
`model.call.started`/`tool.call.started` commit moves the checkpoint to its
in-flight phase; its outcome commit atomically stores the normalized outcome
and advances the checkpoint to the next safe phase. The lifecycle-final Run
outcome and Session detach consume the same final checkpoint transactionally.

Migration 3 creates lease, external-operation, checkpoint, and reconciliation
tables/indexes. `external_operations` stores operation id/kind, Run/turn,
request fingerprint, provider/Tool identity, lease generation, status, detached
outcome, and recovery metadata. Unique `(run_id, turn, kind, operation_id)` plus
lease-generation preconditions prevent duplicate progress records.

Set SQLite `_SCHEMA_VERSION = 3` and extend the T001 lock-before-discovery
opener rather than merely adding a SQL file. Under the existing WAL/busy retry,
`BEGIN IMMEDIATE` first, then rediscover EMPTY/v1/v2/v3. New databases apply
1→2→3; exact v1 applies both forward transforms; exact v2 is validated with all
T001 schema/representation/event invariants before applying
`0003_leases.sql`; exact v3 is fully validated and opened without reapplying.
Execute complete statements without `executescript`, insert version 3, validate
the complete v3 tables/indexes/data constraints inside the transaction, and use
the cancellation-safe commit/rollback coordinator. Two concurrent v2 opens
must both succeed with one migration. Fault-inject after every v3 DDL/index,
version insert, validation, and commit race; reopen observes exact v2 or complete
v3, never partial. Add real v2 fixtures plus malformed/gapped/future version
rows and transient/exhausted busy tests.

- [x] **Step 4: Implement recovery scan**

At SDK open, a read/write recovery scan may mark stale leased Runs interrupted,
but it must not invoke LiteLLM, Tools, MCP, or Workflow execution. Application
setup then registers AgentSpecs/Tools/MCP and explicitly calls
`sdk.recovery.recover_run(...)` or `recover_workflow(...)`. Verify the persisted
AgentSpec content hash, model params, initial messages, full ToolSpec capability
hashes/versions, and effective Policy hash against registered capabilities
before acquiring a new lease. A changed Tool effect/timeout/source/version or
permission default fails recovery even when model-visible schemas match. Inspect unresolved model
operations before the next model turn: use an authoritative registered provider
status query when available, otherwise create a model-call reconciliation
request without invoking LiteLLM. Then inspect started ToolCalls without
outcomes. Retry only `idempotent`/`safe_retry`; otherwise create a Tool-call
reconciliation request and waiting state. A legacy Run or Workflow descriptor
always takes the explicit resolution path.

Load and validate the checkpoint before recovery. `ready_for_model` and
`ready_for_tool` may advance under a newly acquired lease; any in-flight phase
must first resolve its matching external operation. A T001 Run with no
checkpoint may synthesize only the pristine initial checkpoint when its event
history proves no model/Tool operation began. Any other pre-T002
RUNNING/WAITING history becomes a durable `legacy_checkpoint_missing`
reconciliation request and never reconstructs prompts from partial text-delta
events.

```python
async def reconcile_stale_runs(self) -> None:
    for run in await self._store.list_stale_nonterminal_runs(self._clock.now()):
        await self._commands.mark_interrupted(run.id)
        model_call = await self._store.unresolved_model_call(run.id)
        if model_call is not None:
            outcome = await self._provider_recovery.query_if_supported(model_call)
            if outcome is not None:
                await self._commands.record_model_outcome(model_call, outcome)
            elif self._provider_recovery.certifies_idempotent_resend(model_call):
                await self._commands.queue_same_model_operation(model_call)
            else:
                await self._commands.request_reconciliation(model_call)
            continue
        for call in await self._store.unresolved_tool_calls(run.id):
            if call.idempotent or call.safe_retry:
                await self._commands.queue_tool_retry(call.id)
            else:
                await self._commands.request_reconciliation(call.id)
```

Neither `mark_interrupted` nor `request_reconciliation` detaches the Run from
its Session. Resolution actions that keep work recoverable also retain
ownership. Only `CONFIRM_COMPLETED`, a durable failed outcome, or the T004
durable cancel/terminate path performs the lifecycle-final detach in the same
commit as the Run outcome.

`RecoveryAPI.recover_workflow` first verifies the persisted
`WorkflowExecutionDescriptor` against current AgentSpecs/full Tool
capabilities/effective Policy. It then claims
the next M01 sequential Workflow transition with an exact Workflow snapshot
precondition. If the node already selected a Run, recover that same Run; if a
pending node has no Run, only the CAS winner creates/selects one. RunEngine then
acquires the Run lease before persisting/advancing `run.started`; a lease loser
may attach to the durable Run but cannot call LiteLLM, Tools, or MCP. Test two
SDK instances at pending-node, created-Run, and terminal-Run projection
boundaries and assert one provider/Tool side effect. Workflow-wide scheduler
ownership is completed in M04-T002.

- [x] **Step 5: Implement resolution actions**

```python
class ReconciliationAction(StrEnum):
    CONFIRM_COMPLETED = "confirm_completed"
    CONFIRM_NOT_EXECUTED = "confirm_not_executed"
    RETRY = "retry"
    TERMINATE = "terminate"
```

Require evidence/actor metadata; RETRY remains forbidden unless user explicitly selects it.

- [x] **Step 6: Verify crash boundary**

Run: `uv run pytest tests/integration/runtime/test_leases.py tests/integration/runtime/test_recovery_admission.py tests/integration/workflow/test_workflow_recovery_admission.py tests/integration/storage/test_sqlite_v3_migration.py tests/faults/test_model_call_unknown_outcome.py tests/e2e/test_unknown_tool_outcome.py -v`

Expected: fencing works; side effect count stays one until explicit resolution;
interrupted/reconciliation work keeps Session ownership; mismatched or missing
capabilities never execute. Crash after durable model-start, provider accept,
response receipt, and lease expiry during an in-flight request never causes a
second provider call by default; only certified status/idempotency adapters may
resolve automatically.

- [x] **Step 7: Commit**

```powershell
git add src/agent_sdk/runtime src/agent_sdk/workflow src/agent_sdk/storage tests/integration/runtime tests/integration/workflow/test_workflow_recovery_admission.py tests/integration/storage/test_sqlite_v3_migration.py tests/faults/test_model_call_unknown_outcome.py tests/e2e/test_unknown_tool_outcome.py
git commit -m "feat: add run leases and reconciliation"
```

## Completion evidence

Completed on 2026-07-17 at `5da9e79`. The final independent whole-task
re-review approved Spec C0/I0/M0 and Quality C0/I0/M2. Python 3.13 passed all
2,159 tests with zero failures/skips; the Phase 5C release gate also passed the
full supported Python 3.12 suite, external sdist/wheel builds, clean installs,
and side-effect-free reference CLI help. See
`.superpowers/sdd/M02-T002-final-report.md` for the consolidated evidence and
retained nonblocking maintenance notes.
