# M02-T001 Session Lifecycle and Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the complete normal Session lifecycle and Store-atomic idempotency for Session, Run, and Workflow creation without duplicating entities or local execution.

**Architecture:** Memory and SQLite Stores arbitrate `(scope, key)` inside the same commit that appends events and projections. A frozen Session projection owns the ids of nonterminal Runs and Workflows; start and terminal commits attach/detach those ids so closing can atomically reject new work and automatically settle to closed.

**Tech Stack:** Python 3.12-3.13, asyncio, Pydantic v2, aiosqlite WAL, SHA-256 canonical JSON fingerprints, pytest-asyncio.

## Global Constraints

- Follow `docs/design/features/13-session-lifecycle-and-idempotency.md` exactly.
- LiteLLM remains the only model integration; this task performs no provider I/O inside a Store transaction.
- Idempotency arbitration, events, snapshots, and the first result are committed atomically.
- A matching replay allocates no event cursor and launches no second local Run/Workflow task.
- Reusing one `(scope, key)` with a different request fingerprint fails closed with `CONFLICT` and reveals no stored request/result data.
- Closing rejects new Run, Workflow, and Child creation but permits already-owned work to reach a terminal state.
- Deleted is not a persisted Session state; successful delete makes lookup return `NOT_FOUND` and removes Session-owned idempotency records.
- Workspace files and future global persistent Policy rules are outside Session deletion.
- Force close/delete is implemented only with the durable cancellation machinery in M02-T004; M02-T001 implements the complete non-force lifecycle and does not erase live work.
- New behavior uses RED-GREEN-REFACTOR. Every task has a separate implementation commit and independent spec/code-quality review.

---

### Task 1: Add Store-atomic idempotency and SQLite migration 2

**Files:**
- Create: `src/agent_sdk/storage/idempotency.py`
- Modify: `src/agent_sdk/storage/base.py`
- Modify: `src/agent_sdk/storage/memory.py`
- Modify: `src/agent_sdk/storage/sqlite.py`
- Modify: `src/agent_sdk/api.py`
- Create: `src/agent_sdk/storage/migrations/0002_idempotency.sql`
- Create: `tests/contract/test_idempotency_store_contract.py`
- Modify: `tests/integration/storage/test_sqlite_spine.py`

**Interfaces:**
- Consumes: `CommitBatch`, Memory/SQLite commit locks, canonical JSON, lazy SQLite adapter.
- Produces: `IdempotencyWrite`, immutable `IdempotencyRecord`, `IdempotencyConflictError`, `fingerprint_command`, `CommitResult.applied`, `CommitResult.idempotency`, and `StateStore.get_idempotency`.

- [ ] **Step 1: Write failing Store contract tests**

Parameterize Memory and SQLite factories and add real assertions for the same
batch on both Stores:

```python
@pytest.mark.parametrize("store_factory", STORE_FACTORIES)
async def test_matching_idempotency_replay_returns_first_result_without_writes(
    store_factory: StoreFactory,
) -> None:
    async with store_factory() as store:
        write = IdempotencyWrite(
            scope="session.create",
            key="request-1",
            request_fingerprint=fingerprint_command(
                "session.create", {"workspaces": ["workspace"]}
            ),
            session_id="ses_first",
            result={"session_id": "ses_first"},
        )
        first = await store.commit(_session_batch("ses_first", write))
        replay = await store.commit(_session_batch("ses_second", write))

        assert first.applied is True
        assert replay.applied is False
        assert replay.idempotency == first.idempotency
        assert await store.get_snapshot("session", "ses_second") is None
        assert await store.latest_cursor() == first.last_cursor

@pytest.mark.parametrize("store_factory", STORE_FACTORIES)
async def test_mismatched_idempotency_reuse_is_atomic(store_factory: StoreFactory) -> None:
    async with store_factory() as store:
        await store.commit(_session_batch("ses_first", _write("fingerprint-a")))
        before = await store.latest_cursor()
        with pytest.raises(IdempotencyConflictError):
            await store.commit(_session_batch("ses_second", _write("fingerprint-b")))
        assert await store.latest_cursor() == before
        assert await store.get_snapshot("session", "ses_second") is None
```

Also cover: empty/over-256 keys, invalid fingerprints, non-JSON/non-finite
results, returned-result defensive copying, concurrent matching commits, key
scope independence, deletion cleanup, and `CancelledError` propagation.

- [ ] **Step 2: Run RED and confirm the missing contract**

Run:

```powershell
uv run --python 3.13 pytest tests/contract/test_idempotency_store_contract.py tests/integration/storage/test_sqlite_spine.py -q
```

Expected: collection/import failures for the new idempotency types and missing
Store behavior. Existing SQLite tests remain independently runnable.

- [ ] **Step 3: Implement immutable values and deterministic fingerprints**

Create `storage/idempotency.py` with strict public values. Freeze nested result
objects and serialize them back to detached JSON:

```python
class IdempotencyRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    key: str
    request_fingerprint: str
    session_id: str
    result: Mapping[str, Any]

    @field_validator("scope", "key")
    @classmethod
    def _bounded_text(cls, value: str) -> str:
        if not value or len(value) > 256:
            raise ValueError("idempotency text must contain 1..256 characters")
        return value

    @field_validator("request_fingerprint")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("request fingerprint must be lowercase SHA-256")
        return value

class IdempotencyWrite(NamedTuple):
    scope: str
    key: str
    request_fingerprint: str
    session_id: str
    result: dict[str, Any]

def fingerprint_command(command: str, arguments: Mapping[str, Any]) -> str:
    encoded = canonical_snapshot_data(
        {"command": command, "arguments": dict(arguments)}
    )
    return sha256(encoded.encode("utf-8")).hexdigest()
```

Do not accept bytes, arbitrary Python objects, non-string mapping keys, NaN, or
Infinity. Validation happens before any Store state is mutated.

- [ ] **Step 4: Extend the Store commit contract**

Add an optional field without changing existing call sites:

```python
class CommitBatch(NamedTuple):
    events: tuple[EventEnvelope, ...]
    snapshots: tuple[SnapshotWrite, ...] = ()
    preconditions: tuple[SnapshotPrecondition, ...] = ()
    event_preconditions: tuple[EventPrecondition, ...] = ()
    idempotency: IdempotencyWrite | None = None

class CommitResult(NamedTuple):
    last_cursor: int
    applied: bool = True
    idempotency: IdempotencyRecord | None = None

class StateStore(Protocol):
    async def get_idempotency(
        self, scope: str, key: str
    ) -> IdempotencyRecord | None: ...
```

Inside `InMemoryStore.commit`, validate the incoming write, acquire `_lock`,
look up `(scope, key)` before every normal precondition, and either replay,
conflict, or stage all existing copies plus the new record. Publish the copies
only after every event/snapshot validation succeeds. `delete_session` removes
records whose `session_id` matches.

- [ ] **Step 5: Add and apply migration 2 atomically**

Create exactly:

```sql
CREATE TABLE idempotency_records(
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    session_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(scope, key)
);
CREATE INDEX idempotency_records_session
    ON idempotency_records(session_id);
```

Set `_SCHEMA_VERSION = 2`. For a new database, apply `0001_initial.sql`, record
version 1, then apply `0002_idempotency.sql` and record version 2. For an exact
version-1 database, apply only migration 2. Accept only final versions `(1, 2)`;
continue rejecting gaps, duplicates, unknown future versions, malformed tables,
and unexpected index shapes. Migration and its version insert share one SQLite
transaction.

- [ ] **Step 6: Implement SQLite arbitration and lazy forwarding**

Under `BEGIN IMMEDIATE`, execute the idempotency lookup before event/snapshot
preconditions:

```python
existing = await self._read_idempotency(write.scope, write.key)
if existing is not None:
    if existing.request_fingerprint != write.request_fingerprint:
        raise IdempotencyConflictError("idempotency key was reused")
    await self._connection.rollback()
    return CommitResult(await self._last_cursor(), False, existing)

await self._check_event_preconditions(batch)
await self._check_snapshot_preconditions(batch)
# existing event/snapshot writes
record = record_from_write(write)
await self._insert_idempotency(record)
await self._connection.commit()
return CommitResult(await self._last_cursor(), True, record)
```

Use the existing cancellation-safe rollback path. Implement
`SQLiteStore.get_idempotency` under `_lock`, forward it from `_LazySQLiteStore`,
and delete idempotency rows in the same `delete_session` transaction as events
and snapshots.

- [ ] **Step 7: Verify Task 1 and regress storage**

Run:

```powershell
uv run --python 3.13 pytest tests/contract/test_idempotency_store_contract.py tests/contract/test_memory_store_contract.py tests/integration/storage/test_sqlite_spine.py -q
uv run --python 3.13 ruff check src/agent_sdk/storage src/agent_sdk/api.py tests/contract tests/integration/storage
uv run --python 3.13 mypy src/agent_sdk
```

Expected: all selected tests pass; Ruff and mypy report no errors.

- [ ] **Step 8: Commit Task 1**

```powershell
git add src/agent_sdk/storage src/agent_sdk/api.py tests/contract tests/integration/storage/test_sqlite_spine.py
git commit -m "feat: add atomic command idempotency"
```

---

### Task 2: Add the pure Session state machine and normal lifecycle commands

**Files:**
- Create: `src/agent_sdk/runtime/state_machine.py`
- Create: `src/agent_sdk/runtime/session_lifecycle.py`
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/runtime/commands.py`
- Modify: `src/agent_sdk/errors.py`
- Modify: `src/agent_sdk/api.py`
- Modify: `src/agent_sdk/__init__.py`
- Create: `tests/unit/runtime/test_session_state_machine.py`
- Create: `tests/integration/runtime/test_session_lifecycle.py`

**Interfaces:**
- Consumes: Task 1 idempotent commit result, existing snapshot preconditions, SDK lifecycle admission.
- Produces: `SessionStatus`, `SessionStateMachine.transition`, frozen `SessionSnapshot`, `SessionBusyError`, `RuntimeCommands.get_session/close_session/delete_session`, and public `sessions.get/close/delete`.

- [ ] **Step 1: Write state-machine and empty-Session lifecycle tests**

```python
@pytest.mark.parametrize(
    ("source", "target"),
    [
        (SessionStatus.ACTIVE, SessionStatus.CLOSING),
        (SessionStatus.ACTIVE, SessionStatus.DELETING),
        (SessionStatus.CLOSING, SessionStatus.CLOSED),
        (SessionStatus.CLOSING, SessionStatus.DELETING),
        (SessionStatus.CLOSED, SessionStatus.DELETING),
    ],
)
def test_allowed_session_transition(source: SessionStatus, target: SessionStatus) -> None:
    assert SessionStateMachine.transition(source, target) is target

def test_closed_session_cannot_own_work() -> None:
    with pytest.raises(ValidationError):
        SessionSnapshot(
            session_id="ses_1",
            status="closed",
            workspaces=(),
            active_run_ids=("run_1",),
        )

async def test_empty_session_closes_immediately_and_delete_removes_it(sdk) -> None:
    session = await sdk.sessions.create(workspaces=[])
    closed = await sdk.sessions.close(session.session_id)
    assert closed.status is SessionStatus.CLOSED
    await sdk.sessions.delete(session.session_id)
    with pytest.raises(AgentSDKError) as raised:
        await sdk.sessions.get(session.session_id)
    assert raised.value.code is ErrorCode.NOT_FOUND
```

Also assert duplicate active/closing/closed/deleting ids are rejected, versions
are positive, `close` is same-state idempotent, busy delete is `SessionBusyError`,
and deletion retries from a deliberately retained `deleting` snapshot.

- [ ] **Step 2: Run RED**

```powershell
uv run --python 3.13 pytest tests/unit/runtime/test_session_state_machine.py tests/integration/runtime/test_session_lifecycle.py -q
```

Expected: imports and public lifecycle methods are missing.

- [ ] **Step 3: Implement the pure model and state machine**

Use an explicit table and stable error:

```python
_ALLOWED: Mapping[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.ACTIVE: frozenset({SessionStatus.CLOSING, SessionStatus.DELETING}),
    SessionStatus.CLOSING: frozenset({SessionStatus.CLOSED, SessionStatus.DELETING}),
    SessionStatus.CLOSED: frozenset({SessionStatus.DELETING}),
    SessionStatus.DELETING: frozenset(),
}

class SessionStateMachine:
    @staticmethod
    def transition(source: SessionStatus, target: SessionStatus) -> SessionStatus:
        if target not in _ALLOWED[source]:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "session transition is invalid",
                retryable=False,
            )
        return target
```

Make `SessionSnapshot` frozen/extra-forbid with empty tuple defaults for
`active_run_ids` and `active_workflow_run_ids`; validate uniqueness and forbid
active work for `closed`/`deleting`.

- [ ] **Step 4: Implement safe Session loading and transition commits**

In `session_lifecycle.py`, isolate defensive loading and event/snapshot creation:

```python
async def load_session(store: StateStore, session_id: str) -> SessionSnapshot:
    data = await store.get_snapshot("session", session_id)
    if data is None:
        raise AgentSDKError(ErrorCode.NOT_FOUND, "session not found", retryable=False)
    try:
        return SessionSnapshot.model_validate(data)
    except Exception:
        raise AgentSDKError(
            ErrorCode.INTERNAL, "failed to load session", retryable=False
        ) from None

def session_transition_batch(
    previous: SessionSnapshot,
    updated: SessionSnapshot,
    event_type: str,
    *,
    idempotency: IdempotencyWrite | None = None,
) -> CommitBatch:
    return CommitBatch(
        events=(EventEnvelope.new(
            type=event_type,
            session_id=previous.session_id,
            run_id=None,
            sequence=updated.version,
            payload=updated.model_dump(mode="json"),
        ),),
        snapshots=(session_write(updated),),
        preconditions=(exact_session_precondition(previous),),
        idempotency=idempotency,
    )
```

Exact preconditions include version, owner Session id, and canonical snapshot
data. Retry at most eight exact-precondition races, reloading each time.

- [ ] **Step 5: Implement create/get/close/delete commands**

`create_session` accepts `idempotency_key: str | None`. The fingerprint contains
the exact ordered normalized workspace strings. A matching replay validates the
stored result as `SessionSnapshot`; malformed replay data becomes sanitized
`INTERNAL`.

`close_session` applies this decision table:

```python
if current.status is SessionStatus.DELETING:
    raise AgentSDKError(ErrorCode.INVALID_STATE, "session is deleting", retryable=False)
if current.status in {SessionStatus.CLOSING, SessionStatus.CLOSED}:
    return await self._record_session_result(current, idempotency_key)
target = (
    SessionStatus.CLOSED
    if not current.active_run_ids and not current.active_workflow_run_ids
    else SessionStatus.CLOSING
)
event_type = "session.closed" if target is SessionStatus.CLOSED else "session.closing"
```

When a key is supplied for a closing/closed no-op, use an idempotency-only
`CommitBatch` with the exact Session precondition so its first returned snapshot
is durable. A matching replay is resolved before the now-current snapshot is
interpreted.

Define the helper in `RuntimeCommands` rather than leaving a second command
path implicit:

```python
def session_result_idempotency(
    snapshot: SessionSnapshot, key: str
) -> IdempotencyWrite:
    return IdempotencyWrite(
        scope=f"session/{snapshot.session_id}/close",
        key=key,
        request_fingerprint=fingerprint_command(
            "session.close", {"session_id": snapshot.session_id}
        ),
        session_id=snapshot.session_id,
        result=snapshot.model_dump(mode="json"),
    )

def validate_session_result(result: Mapping[str, Any]) -> SessionSnapshot:
    try:
        return SessionSnapshot.model_validate(dict(result))
    except Exception:
        raise AgentSDKError(
            ErrorCode.INTERNAL, "session command result is invalid", retryable=False
        ) from None

async def _record_session_result(
    self,
    current: SessionSnapshot,
    key: str | None,
) -> SessionSnapshot:
    if key is None:
        return current
    write = session_result_idempotency(current, key)
    result = await self._store.commit(CommitBatch(
        events=(),
        preconditions=(exact_session_precondition(current),),
        idempotency=write,
    ))
    record = result.idempotency
    if record is None:
        raise AgentSDKError(ErrorCode.INTERNAL, "session command result is missing", retryable=False)
    return validate_session_result(record.result)
```

`delete_session` raises `SessionBusyError` for active/closing; transitions a
closed Session to deleting, then invokes Store deletion. If already deleting,
skip the transition and retry Store deletion. Do not catch `CancelledError`.

- [ ] **Step 6: Expose lifecycle-aware public methods**

Add optional keys without breaking M01 callers:

```python
async def create(
    self,
    *,
    workspaces: Iterable[str | Path],
    idempotency_key: str | None = None,
) -> SessionSnapshot: ...

async def get(self, session_id: str) -> SessionSnapshot: ...

async def close(
    self, session_id: str, *, idempotency_key: str | None = None
) -> SessionSnapshot: ...

async def delete(self, session_id: str) -> None: ...
```

All four methods use `_SDKLifecycle.admit`. Root-export `SessionStatus`,
`SessionSnapshot`, and `SessionBusyError`.

- [ ] **Step 7: Verify Task 2 and runtime compatibility**

```powershell
uv run --python 3.13 pytest tests/unit/runtime/test_session_state_machine.py tests/integration/runtime/test_session_lifecycle.py tests/integration/storage/test_sqlite_spine.py tests/integration/runtime/test_text_agent_loop.py -q
uv run --python 3.13 ruff check src/agent_sdk/runtime src/agent_sdk/api.py src/agent_sdk/errors.py tests/unit/runtime tests/integration/runtime
uv run --python 3.13 mypy src/agent_sdk
```

Expected: all selected tests pass and static checks are clean.

- [ ] **Step 8: Commit Task 2**

```powershell
git add src/agent_sdk/runtime src/agent_sdk/api.py src/agent_sdk/errors.py src/agent_sdk/__init__.py tests/unit/runtime tests/integration/runtime/test_session_lifecycle.py
git commit -m "feat: add complete normal session lifecycle"
```

---

### Task 3: Make Run start idempotent and settle closing Sessions atomically

**Files:**
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/runtime/session_lifecycle.py`
- Modify: `src/agent_sdk/runtime/commands.py`
- Modify: `src/agent_sdk/runtime/engine.py`
- Modify: `src/agent_sdk/runtime/handles.py`
- Modify: `src/agent_sdk/api.py`
- Create: `tests/integration/runtime/test_run_session_ownership.py`
- Modify: `tests/integration/runtime/test_text_agent_loop.py`
- Modify: `tests/integration/tools/test_permissioned_tool_slice.py`

**Interfaces:**
- Consumes: Tasks 1-2, `RunSnapshot`, `_RunEmitter`, `RunHandle`, SDK task tracking.
- Produces: internal `CommandOutcome[T]`, optional `RunAPI.start(..., idempotency_key=...)`, atomic Run attach/detach, and durable replay handles.

- [ ] **Step 1: Write failing ownership, race, and replay tests**

```python
async def test_close_rejects_new_run_and_last_run_closes_session(
    sdk: AgentSDK, blocking_completion: BlockingCompletion
) -> None:
    session = await sdk.sessions.create(workspaces=[])
    handle = await sdk.runs.start(session.session_id, AGENT, "first")
    await blocking_completion.started.wait()

    closing = await sdk.sessions.close(session.session_id)
    assert closing.status is SessionStatus.CLOSING
    with pytest.raises(AgentSDKError) as raised:
        await sdk.runs.start(session.session_id, AGENT, "second")
    assert raised.value.code is ErrorCode.INVALID_STATE

    blocking_completion.finish("done")
    await handle.result()
    assert (await sdk.sessions.get(session.session_id)).status is SessionStatus.CLOSED

async def test_concurrent_duplicate_run_start_executes_once(sdk, completion) -> None:
    session = await sdk.sessions.create(workspaces=[])
    handles = await asyncio.gather(*(
        sdk.runs.start(
            session.session_id,
            AGENT,
            "same input",
            idempotency_key="run-request",
        )
        for _ in range(32)
    ))
    assert len({handle.run_id for handle in handles}) == 1
    await asyncio.gather(*(handle.result() for handle in handles))
    assert completion.call_count == 1
```

Also test different request data with the same key, no-key creation, close/start
races in both orderings, model failure detachment, terminal commit rollback,
replay after completion, replay after SQLite reopen, and a caller cancellation
after durable creation followed by key-based entity recovery. Completed and
partially failed Tool Runs must return the exact ordered durable Tool results.

- [ ] **Step 2: Run RED**

```powershell
uv run --python 3.13 pytest tests/integration/runtime/test_run_session_ownership.py tests/integration/runtime/test_text_agent_loop.py -q
```

Expected: `RunAPI.start` has no key, Session does not track Runs, and terminal
execution does not settle closing.

- [ ] **Step 3: Add command outcomes and atomic Run attachment**

Define an internal frozen generic:

```python
@dataclass(frozen=True)
class CommandOutcome(Generic[T]):
    value: T
    replayed: bool
```

`RuntimeCommands.start_run` accepts `idempotency_key`. On each bounded attempt:

1. Load a valid Session.
2. Require `status is ACTIVE`.
3. Construct one Run id and preserve it across retry attempts.
4. Add it once to `active_run_ids`, increment Session version, and create
   `session.run.attached` plus `run.created`.
5. Submit both projections/events and the exact Session precondition in one
   optional-idempotent batch.
6. Return the stored first Run snapshot and `replayed=True` when Store replayed.

The fingerprint contains the Session id, full agent revision, user input,
parent/workflow/node ids, and canonical TaskEnvelope JSON. An idempotent replay
is checked through `get_idempotency` before the current Session status is
interpreted. An absent read is only a fast-path miss: the subsequent commit
still carries the write and arbitrates atomically. Therefore a retry can recover
the first Run after the Session has moved to closing/closed.

- [ ] **Step 4: Commit terminal Run and Session detachment together**

Extend `_RunEmitter` with the shared Session lifecycle coordinator. For
`COMPLETED`, `FAILED`, and every later terminal `RunStatus`, build one batch:

```python
remaining = tuple(
    run_id for run_id in session.active_run_ids if run_id != updated_run.run_id
)
close_now = (
    session.status is SessionStatus.CLOSING
    and not remaining
    and not session.active_workflow_run_ids
)
updated_session = session.model_copy(update={
    "active_run_ids": remaining,
    "status": SessionStatus.CLOSED if close_now else session.status,
    "version": session.version + 1,
})
session_event = "session.closed" if close_now else "session.run.detached"
```

The batch contains the Run terminal event/snapshot, Session event/snapshot, the
exact previous Session precondition, and exact previous Run precondition. Reuse
the same Run event id/sequence on bounded Session-race retries; failed SQLite
transactions leave neither aggregate changed. If the Session no longer owns the
Run, return a sanitized `CONFLICT` rather than silently repairing corruption.

Add `tool_results: tuple[ToolResult, ...] = ()` to `RunSnapshot`. Nonterminal
states require the tuple to be empty. Both completed and failed terminal updates
persist the results completed so far, and durable `RunResult` reconstruction
uses that tuple rather than returning an empty substitute.

- [ ] **Step 5: Prevent duplicate local execution and support durable handles**

Add a per-`RunAPI` lock and `run_id -> Task[RunResult]` registry. The critical
section covers command arbitration and initial task registration. A replay:

- reuses an existing local task;
- returns a detached `RunHandle` for every replayed nonterminal Run when no
  local task is registered, including an abandoned `created` Run;
- reconstructs `RunResult` from a completed snapshot or raises its durable
  failure from a terminal failed snapshot.

Make `RunHandle` accept `task: Task[RunResult] | None`. Detached `result()` and
`events()` poll bounded Store pages until a terminal event/snapshot; they do not
start execution. Only a fresh `outcome.replayed is False` starts a task.
M02-T002 later supplies interruption recovery and lease fencing for a genuinely
abandoned nonterminal handle.

- [ ] **Step 6: Verify Task 3 and Tool regressions**

```powershell
uv run --python 3.13 pytest tests/integration/runtime/test_run_session_ownership.py tests/integration/runtime/test_text_agent_loop.py tests/integration/tools/test_permissioned_tool_slice.py tests/integration/subagents/test_child_run_slice.py -q
uv run --python 3.13 ruff check src/agent_sdk/runtime src/agent_sdk/api.py tests/integration/runtime
uv run --python 3.13 mypy src/agent_sdk
```

Expected: all selected tests pass; same-process duplicate execution count is
exactly one.

- [ ] **Step 7: Commit Task 3**

```powershell
git add src/agent_sdk/runtime src/agent_sdk/api.py tests/integration/runtime tests/integration/tools/test_permissioned_tool_slice.py tests/integration/subagents/test_child_run_slice.py
git commit -m "feat: bind runs to session lifecycle"
```

---

### Task 4: Make Workflow start idempotent and bind Workflow terminal state

**Files:**
- Modify: `src/agent_sdk/runtime/session_lifecycle.py`
- Modify: `src/agent_sdk/workflow/state.py`
- Modify: `src/agent_sdk/workflow/executor.py`
- Modify: `src/agent_sdk/workflow/handles.py`
- Modify: `src/agent_sdk/api.py`
- Create: `tests/integration/workflow/test_workflow_session_ownership.py`
- Modify: `tests/integration/workflow/test_workflow_child_slice.py`
- Modify: `tests/integration/workflow/test_workflow_recovery.py`

**Interfaces:**
- Consumes: `CommandOutcome`, Session exact transition helper, Workflow IR hash and existing executor active-task map.
- Produces: optional `WorkflowAPI.start(..., idempotency_key=...)`, atomic Workflow attach/detach, replay attachment/resume, and closing-safe Workflow failure settlement.

- [ ] **Step 1: Write failing Workflow lifecycle tests**

```python
async def test_concurrent_duplicate_workflow_start_creates_and_executes_once(
    sdk: AgentSDK, workflow: WorkflowIR, completion: ScriptedCompletion
) -> None:
    session = await sdk.sessions.create(workspaces=[])
    handles = await asyncio.gather(*(
        sdk.workflows.start(
            session.session_id,
            workflow,
            idempotency_key="workflow-request",
        )
        for _ in range(24)
    ))
    assert len({handle.workflow_run_id for handle in handles}) == 1
    await asyncio.gather(*(handle.result() for handle in handles))
    assert completion.total_calls == len(workflow.nodes)

async def test_last_workflow_terminal_closes_closing_session(sdk, blocked_workflow) -> None:
    session, handle = await blocked_workflow.start()
    assert (await sdk.sessions.close(session.session_id)).status == "closing"
    blocked_workflow.finish()
    await handle.result()
    assert (await sdk.sessions.get(session.session_id)).status == "closed"
```

Also test: close rejects new Workflow start; Workflow failure detaches; closing
between nodes rejects the next Run/Child, persists Workflow failure, and closes;
same key/different IR conflicts; replay after completion and SQLite reopen
returns the original Workflow id without duplicate nodes/children.

- [ ] **Step 2: Run RED**

```powershell
uv run --python 3.13 pytest tests/integration/workflow/test_workflow_session_ownership.py tests/integration/workflow/test_workflow_child_slice.py tests/integration/workflow/test_workflow_recovery.py -q
```

Expected: Workflow start has no key and Session work ownership is absent.

- [ ] **Step 3: Attach Workflow creation to active Session**

Change `WorkflowState.create` to return `CommandOutcome[WorkflowRunSnapshot]`
and accept an optional key. Validate the IR before generating durable state.
One commit contains:

- `session.workflow.attached` at the next Session sequence;
- `workflow.started` and every initial Workflow/node projection;
- updated Session projection with the Workflow id;
- exact Session precondition;
- optional idempotency write whose fingerprint includes Session id and canonical
  Workflow IR/definition hash, and whose result is the full first Workflow
  snapshot.

Preserve one generated Workflow id across exact-precondition retries.

- [ ] **Step 4: Detach every terminal Workflow in the terminal commit**

Update both `complete_workflow` and `fail_workflow`. Their terminal batch removes
the Workflow id from `active_workflow_run_ids`, writes
`session.workflow.detached`, or writes `session.closed` when it is the final work
owned by a closing Session. Require the exact prior Workflow and Session
snapshots and retry only a Session precondition race.

Node transitions remain permitted while the Session exists. If later Run/Child
creation returns `INVALID_STATE` because close won, the existing executor maps
that to Workflow/node failure; the failure commit must detach the Workflow.

- [ ] **Step 5: Reuse active tasks and durable Workflow state on replay**

Add `idempotency_key` to `WorkflowAPI.start` and `WorkflowExecutor.start`. Under
an executor start lock, a fresh outcome calls `_start_task`; a replay only
reuses an already registered task or returns a detached durable handle:

```python
if outcome.replayed:
    return await self._handle_replay(
        outcome.value.workflow_run_id, expected_workflow=validated
    )
return self._start_task(outcome.value.workflow_run_id)
```

An active local task is reused. A completed/failed durable Workflow gets a
handle that returns/raises from durable state without rerunning terminal nodes.
An abandoned nonterminal replay remains detached until explicit `resume` or the
M02-T002 recovery path acquires its lease. Do not convert cancellation.

- [ ] **Step 6: Verify Task 4 and Workflow/Child regressions**

```powershell
uv run --python 3.13 pytest tests/integration/workflow tests/integration/subagents -q
uv run --python 3.13 ruff check src/agent_sdk/workflow src/agent_sdk/runtime/session_lifecycle.py src/agent_sdk/api.py tests/integration/workflow
uv run --python 3.13 mypy src/agent_sdk
```

Expected: all Workflow/Child tests pass and duplicate starts execute one graph.

- [ ] **Step 7: Commit Task 4**

```powershell
git add src/agent_sdk/runtime/session_lifecycle.py src/agent_sdk/workflow src/agent_sdk/api.py tests/integration/workflow tests/integration/subagents
git commit -m "feat: bind workflows to session lifecycle"
```

---

### Task 5: Prove restart, concurrency, deletion, and M01 compatibility end to end

**Files:**
- Create: `tests/e2e/test_session_lifecycle_idempotency.py`
- Modify: `tests/e2e/test_vertical_slice.py`
- Modify: `README.md`
- Modify: `docs/plans/tasks/index.md`
- Create or update ignored: `.superpowers/sdd/M02-T001-report.md`
- Modify ignored: `.superpowers/sdd/progress.md`

**Interfaces:**
- Consumes: Tasks 1-4 through package-root public APIs and real SQLite.
- Produces: M02-T001 acceptance evidence and a clean handoff to M02-T002.

- [ ] **Step 1: Write the integrated failing acceptance test before any fixes**

The E2E uses `AgentSDK.for_test(database_path=...)` and package-root imports only.
It must perform this exact sequence:

```python
first = AgentSDK.for_test(database_path=db, acompletion=script)
session = await first.sessions.create(
    workspaces=[workspace], idempotency_key="create-session"
)
run_handles = await asyncio.gather(*(
    first.runs.start(
        session.session_id, agent, "main", idempotency_key="start-main"
    )
    for _ in range(16)
))
assert len({handle.run_id for handle in run_handles}) == 1
assert (await first.sessions.close(session.session_id)).status == "closing"
await asyncio.gather(*(handle.result() for handle in run_handles))
assert (await first.sessions.get(session.session_id)).status == "closed"
await first.close()

reopened = AgentSDK.for_test(database_path=db, acompletion=must_not_call)
same = await reopened.sessions.create(
    workspaces=[workspace], idempotency_key="create-session"
)
assert same.session_id == session.session_id
replayed = await reopened.runs.start(
    session.session_id, agent, "main", idempotency_key="start-main"
)
assert replayed.run_id == run_handles[0].run_id
assert (await replayed.result()).output_text == "done"
await reopened.sessions.delete(session.session_id)
assert workspace_file.read_text(encoding="utf-8") == "application-owned"
```

Then assert Session/Run/Workflow snapshots, events, Context/Evaluation/Analytics
contributions inherited from the M01 scenario, and all Session-owned
idempotency records are gone. A new `session.create` with the old key creates a
new Session after deletion. Global cursor remains greater than its pre-delete
value and holes are not reused.

- [ ] **Step 2: Run RED or confirm the composed behavior is already GREEN**

```powershell
uv run --python 3.13 pytest tests/e2e/test_session_lifecycle_idempotency.py -q
```

Expected before integration corrections: at least one missing cross-component
behavior fails. If the test is already green because Tasks 1-4 fully compose,
record that fact and do not invent production changes.

- [ ] **Step 3: Correct only failures exposed by the integrated test**

For each failure, first narrow it to a focused regression test in the owning
Task 1-4 test module, observe RED, make the smallest production correction, and
rerun both focused and E2E tests. Do not add force deletion, leases, Artifact
storage, or generalized migration APIs in this task.

- [ ] **Step 4: Preserve and extend the M01 vertical slice**

Keep M01 application behavior unchanged. Update the E2E only where the public
Session contract now requires explicit close before normal delete. It must still
prove real stdio MCP, Skill, L3 Context, Workflow/Child, Evaluation, Analytics,
quiescent reopen, deletion cleanup, and workspace preservation.

- [ ] **Step 5: Run the complete M02-T001 gate**

```powershell
uv run --python 3.13 pytest tests/contract/test_idempotency_store_contract.py tests/integration/runtime/test_session_lifecycle.py tests/integration/runtime/test_run_session_ownership.py tests/integration/workflow/test_workflow_session_ownership.py tests/e2e/test_session_lifecycle_idempotency.py tests/e2e/test_vertical_slice.py -q
uv run --python 3.13 pytest -q
uv run --python 3.13 ruff check .
uv run --python 3.13 mypy src/agent_sdk
uv build
python -m examples.reference_cli.main --help
git diff --check
```

Expected: focused and full suites pass on Python 3.13; Ruff/mypy/build/CLI help
and diff checks succeed. Before M02 milestone completion, the same release gate
will additionally run on Python 3.12.

- [ ] **Step 6: Update documentation and durable evidence**

Update the README Session example to show `idempotency_key`, `close`, and normal
`delete`. After independent review reports Critical 0 and Important 0:

- mark M02-T001 `done` in `docs/plans/tasks/index.md`;
- mark M02-T002 `in_progress`;
- append all design/implementation/fix commit ids and verification evidence;
- write the ignored `.superpowers/sdd/M02-T001-report.md` with RED/GREEN commands,
  boundary decisions, final results, and residual M02-T002/T003/T004 scope;
- update `.superpowers/sdd/progress.md` with the reviewed commit range.

- [ ] **Step 7: Commit acceptance and milestone ledger separately**

```powershell
git add tests/e2e README.md
git commit -m "test: prove session lifecycle idempotency"
git add docs/plans/tasks/index.md
git commit -m "chore: complete M02-T001 session lifecycle"
```
