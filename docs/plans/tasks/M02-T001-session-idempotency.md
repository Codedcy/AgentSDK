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
- T001 never advances a detached nonterminal Run/Workflow. Its handle or
  `Workflow.resume` returns retryable `CONFLICT: recovery required`; T002 owns
  capability-checked lease recovery.
- Run and Workflow start fingerprints include immutable execution descriptors,
  so revision/IR reuse cannot hide different models, model params, or Tool
  schemas.
- Idempotent close/Run/Workflow replay carries an exact Session replay
  precondition. A retained `deleting` Session is rejected before replay, and a
  replay-vs-delete race linearizes under the Store lock.
- Reusing one `(scope, key)` with a different request fingerprint fails closed with `CONFLICT` and reveals no stored request/result data.
- Closing rejects new Run, Workflow, and Child creation but permits already-owned work to reach a terminal state.
- Deleted is not a persisted Session state; successful delete makes lookup return `NOT_FOUND` and removes Session-owned idempotency records.
- Workspace files and future global persistent Policy rules are outside Session deletion.
- Force close/delete is implemented only with the durable cancellation machinery in M02-T004; M02-T001 implements the complete non-force lifecycle and does not erase live work.
- v1-to-v2 schema upgrade requires a quiescent database with no older SDK writer; M02-T001 does not claim a cross-version writer fence absent from M01.
- New behavior uses RED-GREEN-REFACTOR. Every task has a separate implementation commit and independent spec/code-quality review.

---

### Task 1: Add Store-atomic idempotency and SQLite migration 2

**Files:**
- Create: `src/agent_sdk/storage/idempotency.py`
- Create: `src/agent_sdk/runtime/execution.py`
- Modify: `src/agent_sdk/storage/base.py`
- Modify: `src/agent_sdk/storage/memory.py`
- Modify: `src/agent_sdk/storage/sqlite.py`
- Modify: `src/agent_sdk/api.py`
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/workflow/models.py`
- Modify: `src/agent_sdk/tools/models.py`
- Create: `src/agent_sdk/storage/migrations/0002_idempotency.sql`
- Create: `tests/contract/test_idempotency_store_contract.py`
- Create: `tests/unit/runtime/test_execution_descriptors.py`
- Modify: `tests/integration/storage/test_sqlite_spine.py`

**Interfaces:**
- Consumes: `CommitBatch`, Memory/SQLite commit locks, canonical JSON, lazy SQLite adapter.
- Produces: `IdempotencyWrite`, read-only `IdempotencyReplay`, immutable
  `IdempotencyRecord`, `IdempotencyValidationError`,
  `IdempotencyConflictError`, `IdempotencyCorruptionError`, internal
  `IdempotencyReplayMissError`, `fingerprint_command`, replay-only exact
  snapshot preconditions, `CommitResult.applied`, `CommitResult.idempotency`,
  `ToolCapabilityDescriptor`, `ExecutionPolicyDescriptor`, Run/Workflow
  execution descriptor base models, strict `SessionStatus`/`SessionSnapshot`,
  legacy/current Run/Workflow snapshot invariants, and `StateStore.get_idempotency`.

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
scope independence, deletion cleanup, `CancelledError` propagation, and a
matching replay whose exact replay precondition changed (no cursor or state
mutation). Also prove a read hint followed by record removal and an atomic
`IdempotencyReplay` produces a typed replay miss and no events/snapshots/new
record. Add a
real version-1 database fixture containing active and terminal Run/Workflow
snapshots and assert upgrade backfills only the nonterminal ids in deterministic
order without changing the Session version. Unknown/corrupt status data must
abort upgrade and leave version 2 unapplied. Add orphan Run/Workflow/other
Session-owned snapshots, cross-owner row/JSON identities, entity-id mismatch,
version mismatch, and an event whose Session owner is absent; every case must
fail closed. Explicitly cover every current M01 snapshot kind: `session`,
`run`, `workflow`, `workflow_node`, `context_capsule`, `context_view`, and
`evaluation`, plus an unknown kind.
After migration, load every Run through public `runs.get` and every Workflow
through `workflows.get`/`WorkflowState.load`; strict models must accept the
legacy compatibility fields. Add unit tests for full Tool capability and Policy
hashes, including equal input schema with changed Tool version/source/effects/
timeout or changed `permission_default`.

- [ ] **Step 2: Run RED and confirm the missing contract**

Run:

```powershell
uv run --python 3.13 pytest tests/contract/test_idempotency_store_contract.py tests/unit/runtime/test_execution_descriptors.py tests/integration/storage/test_sqlite_spine.py -q
```

Expected: collection/import failures for the new idempotency/descriptor types
and missing Store behavior. Existing SQLite tests remain independently
runnable.

- [ ] **Step 3: Implement immutable values and deterministic fingerprints**

Create `storage/idempotency.py` with strict public values. Freeze nested result
objects and serialize them back to detached JSON:

```python
class IdempotencyError(ValueError): ...
class IdempotencyValidationError(IdempotencyError): ...
class IdempotencyConflictError(IdempotencyError): ...
class IdempotencyCorruptionError(IdempotencyError): ...

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

class IdempotencyReplay(NamedTuple):
    scope: str
    key: str
    request_fingerprint: str

def fingerprint_command(command: str, arguments: Mapping[str, Any]) -> str:
    encoded = canonical_snapshot_data(
        {"command": command, "arguments": dict(arguments)}
    )
    return sha256(encoded.encode("utf-8")).hexdigest()
```

Do not accept bytes, arbitrary Python objects, non-string mapping keys, NaN, or
Infinity. Convert input/model validation to `IdempotencyValidationError` before
any Store state is mutated. A stored row that cannot validate is
`IdempotencyCorruptionError`. Matching/mismatching records never include stored
request/result values in exception messages.

Create `runtime/execution.py` in this task, before the migration transform, with
frozen/extra-forbid `ToolCapabilityDescriptor`, `ExecutionPolicyDescriptor`,
`ExecutionDescriptor`, `WorkflowAgentDescriptor`, and
`WorkflowExecutionDescriptor` base models. A Tool capability stores the entire
canonical `ToolSpec` plus a hash; make `ToolSpec.version`/`source` nonempty and
document `version` as the application-maintained handler/capability version.
The Policy descriptor stores canonical execution-affecting config and hash
(`permission_default` in M02). Hashes use detached canonical JSON and validators
recompute them. Handler callables and credentials are forbidden from serialized
descriptors.

In the same Task 1 commit, introduce `SessionStatus` and make `SessionSnapshot`
frozen/extra-forbid with positive version, unique deterministic active Run/
Workflow tuples, and the closed/deleting-no-work invariant. Existing creation
defaults to active/empty. Add to `RunSnapshot` the compatibility flag,
optional `ExecutionDescriptor`, and ordered `tool_results`; add to
`WorkflowRunSnapshot` its compatibility flag and optional
`WorkflowExecutionDescriptor`. Defaults keep existing M01-created entities
`legacy_unknown`/`None`. Validators require legacy→no descriptor and
current→valid descriptor, and preserve all terminal/nonterminal invariants.
Tasks 3 and 4 later change new public Run/Workflow creation to build `current`
descriptors; they do not introduce these model fields. This makes the Task 1
migration and its independent commit readable through strict public models.

- [ ] **Step 4: Extend the Store commit contract**

Add an optional field without changing existing call sites:

```python
class CommitBatch(NamedTuple):
    events: tuple[EventEnvelope, ...]
    snapshots: tuple[SnapshotWrite, ...] = ()
    preconditions: tuple[SnapshotPrecondition, ...] = ()
    event_preconditions: tuple[EventPrecondition, ...] = ()
    idempotency: IdempotencyWrite | IdempotencyReplay | None = None
    replay_preconditions: tuple[SnapshotPrecondition, ...] = ()

class CommitResult(NamedTuple):
    last_cursor: int
    applied: bool = True
    idempotency: IdempotencyRecord | None = None

class StateStore(Protocol):
    async def get_idempotency(
        self, scope: str, key: str
    ) -> IdempotencyRecord | None: ...
```

Inside `InMemoryStore.commit`, validate the incoming request, acquire `_lock`,
look up `(scope, key)` before every normal precondition, and either replay,
conflict, or stage all existing copies plus the new record. When the record
exists, validate `replay_preconditions` before comparing its fingerprint or
returning it. When no record exists, `IdempotencyReplay` raises
`IdempotencyReplayMissError` without any write, while `IdempotencyWrite`
validates normal preconditions and may insert.
`replay_preconditions` without `idempotency` is invalid. Publish the copies only
after every event/snapshot validation succeeds. `delete_session` removes
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

Set `_SCHEMA_VERSION = 2`. Immediately after connecting, configure a finite
SQLite `busy_timeout`, then establish `PRAGMA journal_mode=WAL` with bounded
`SQLITE_BUSY`/`SQLITE_LOCKED` retry. Execute `BEGIN IMMEDIATE` with the same
monotonic-deadline/event-loop-yield retry *before* reading `sqlite_master` or
`schema_migrations`, then
discover and validate the schema/version under that writer lock. For an empty
database, apply `0001_initial.sql`, record version 1, then apply migration 2.
For an exact version-1 database, validate the exact current M01 tables,
indexes, `AUTOINCREMENT`, unique event id, aggregate index SQL, projection
identities, and sole version row `(1,)`, then apply only migration 2. If a
concurrent opener has already produced an exact version-2 database, validate it
and finish without reapplying DDL or inserting another version row. Accept only
the final version sequence `(1, 2)`; continue rejecting gaps, duplicates,
unknown future versions, malformed tables, and unexpected index shapes.

Do not call `aiosqlite.Connection.executescript` anywhere in this open/migration
transaction because SQLite's script API changes transaction boundaries. Split
every trusted packaged SQL resource, including version 1 for an empty database,
with `sqlite3.complete_statement`, reject non-whitespace trailing text, and
execute each complete statement through `connection.execute`:

```python
await _begin_immediate_with_busy_retry(connection, deadline=open_deadline)
try:
    state = await _discover_schema_state(connection)
    if state is SchemaState.EMPTY:
        await _apply_version_one(connection)
        state = SchemaState.V1
    if state is SchemaState.V1:
        await _validate_schema(connection, expected_version=1)
        for statement in complete_sql_statements(migration_sql):
            await connection.execute(statement)
        await _validate_and_backfill_v1_projections(connection)
        await connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (2, now.isoformat()),
        )
    await _validate_schema(connection, expected_version=2)
    commit_task = asyncio.create_task(connection.commit())
    await _await_cleanup(commit_task)
except BaseException:
    await _rollback_connection_cancellation_safely(connection)
    raise
```

`_MIGRATION_2_TRANSFORM_ID = "session-ownership-v1-to-v2"` is stable input to
the checksum bootstrap in M02-T003. Table/index DDL, all backfill writes, the
version insert, and final v2 validation share this one transaction.

Before recording version 2 for an existing database, backfill the representation
that Task 1's strict Session/Run/Workflow models validate. Under the same
transaction:

```python
for session_row in await _snapshot_rows("session"):
    session = _strict_json_object(session_row.data_json)
    if set(session) != {"session_id", "status", "workspaces", "version"}:
        raise ValueError("incompatible version-1 session projection")
    if session["status"] != "active":
        raise ValueError("incompatible version-1 session status")
    session["active_run_ids"] = sorted(
        row.entity_id
        for row in await _owned_snapshot_rows(session["session_id"], "run")
        if _v1_run_is_nonterminal(row.data_json)
    )
    session["active_workflow_run_ids"] = sorted(
        row.entity_id
        for row in await _owned_snapshot_rows(session["session_id"], "workflow")
        if _v1_workflow_is_nonterminal(row.data_json)
    )
    await _replace_snapshot_json(session_row, session)
```

Strictly parse and transform every M01 Run by adding
`execution_compatibility="legacy_unknown"`, `execution_descriptor=None`, and
`tool_results=[]`; transform every M01 Workflow by adding
`execution_compatibility="legacy_unknown"` and `execution_descriptor=None`.
Do not change their row or aggregate versions. New T001 entities use `current`
descriptors and never pass through this legacy transform.

Validate entity/session ownership inside every decoded projection. Known M01
nonterminal Run statuses are `created`, `running`, and `waiting_permission`;
known terminal statuses are `completed` and `failed`. Known Workflow status is
`running`, `completed`, or `failed`. Any other/malformed status aborts the whole
migration. Enumerate every snapshot globally, require its row `session_id` to
resolve to a valid Session, reject unknown kinds, and apply these validators:

- Session, Run, and Workflow row ids/session ids/versions equal their strict
  JSON identities and legal status-derived versions;
- every `workflow_node` parses as `WorkflowNodeSnapshot`, matches row
  entity/session/version, resolves its owner Workflow, and equals that
  Workflow's nested node with the same id;
- every `context_capsule` is exactly `{session_id, capsule}`, has row version 1,
  parses its nested `ContextCapsule`, and is referenced only by same-Session
  Context views;
- every `context_view` parses as `ContextView`, has `view_id == entity_id`,
  matching Session and row version 1, and any `capsule_id` resolves to a
  same-Session Context capsule;
- every `evaluation` parses as `EvaluationResult`, has
  `evaluation_id == entity_id`, matching Session,
  `record_version == row.version`, and a same-Session subject Run;
- every event owner Session exists. Event/projection facts are checked in both
  directions: `session.created`, `run.created`, and `workflow.started` require
  the same-owner aggregate snapshot; every Run/Workflow snapshot requires its
  start event; Run/Workflow terminal event type must agree with terminal
  snapshot status and nonterminal snapshots must not have a terminal event;
  Workflow-node events are reduced in sequence as a legal
  pending→running→completed/failed prefix, every historical payload must keep
  the same owner/node/selected-Run identity, and only the reducer's final
  status/version must equal the nested and standalone current snapshots;
  Context view/capsule and Evaluation creation
  events must resolve to their same-owner snapshots. Missing aggregate facts,
  duplicate/inconsistent terminal facts, or cross-owner event payloads abort.

Keep all aggregate versions unchanged: this is schema representation backfill,
not a domain event. Add row/JSON cross-owner tests for each kind and missing
Context/Evaluation reference tests. Add fixtures with
`run.created`/`workflow.started` but missing projections, snapshots without
start events, terminal-event/status mismatches, and Context/Evaluation creation
events pointing at missing or cross-owner projections; every fixture must leave
exact v1.

Fault-inject after the table DDL, index DDL, each projection update, version
insert, final validation, and during commit. For failures before commit, reopen
and assert exact v1: no idempotency table/index, byte-identical snapshot JSON,
and versions `(1,)`. For cancellation racing commit, settle the independent
coordinator and then reopen: accept only exact v1 or complete schema-validated
v2 with fully backfilled projections and `(1, 2)`; partial combinations fail the
test. Add a two-connection concurrent-open test starting from v1: both opens
synchronize at WAL/open and migration boundaries, both succeed, migration is
applied exactly once, and both observe validated v2. Inject transient busy
errors into `PRAGMA journal_mode=WAL` and `BEGIN IMMEDIATE`; assert retry and
eventual success. Inject busy through the deadline; assert a stable retryable
open conflict and an unchanged valid database rather than a hang. Add
a documented test showing that an intentionally retained old writer is
unsupported and must be quiesced before upgrade.

- [ ] **Step 6: Implement SQLite arbitration and lazy forwarding**

Under `BEGIN IMMEDIATE`, execute the idempotency lookup before event/snapshot
preconditions:

```python
existing = await self._read_idempotency(write.scope, write.key)
if existing is not None:
    await self._check_snapshot_preconditions(batch.replay_preconditions)
    if existing.request_fingerprint != write.request_fingerprint:
        raise IdempotencyConflictError("idempotency key was reused")
    await self._connection.rollback()
    return CommitResult(await self._last_cursor(), False, existing)

if isinstance(write, IdempotencyReplay):
    raise IdempotencyReplayMissError("idempotency replay record no longer exists")

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
and snapshots. Test replay/delete ordering on two SQLite connections: if replay
linearizes first it may return the pre-delete durable value; if `deleting`
linearizes first, the exact replay precondition fails and the public command
reloads to `INVALID_STATE`. No ordering launches a second task or resurrects
facts.

- [ ] **Step 7: Verify Task 1 and regress storage**

Run:

```powershell
uv run --python 3.13 pytest tests/contract/test_idempotency_store_contract.py tests/contract/test_memory_store_contract.py tests/unit/runtime/test_execution_descriptors.py tests/integration/storage/test_sqlite_spine.py -q
uv run --python 3.13 ruff check src/agent_sdk/storage src/agent_sdk/runtime/execution.py src/agent_sdk/runtime/models.py src/agent_sdk/workflow/models.py src/agent_sdk/tools/models.py src/agent_sdk/api.py tests/contract tests/unit/runtime/test_execution_descriptors.py tests/integration/storage
uv run --python 3.13 mypy src/agent_sdk
```

Expected: all selected tests pass; Ruff and mypy report no errors.

- [ ] **Step 8: Commit Task 1**

```powershell
git add src/agent_sdk/storage src/agent_sdk/runtime/execution.py src/agent_sdk/runtime/models.py src/agent_sdk/workflow/models.py src/agent_sdk/tools/models.py src/agent_sdk/api.py tests/contract tests/unit/runtime/test_execution_descriptors.py tests/integration/storage/test_sqlite_spine.py
git commit -m "feat: add atomic command idempotency"
```

---

### Task 2: Add the pure Session state machine and normal lifecycle commands

**Files:**
- Create: `src/agent_sdk/runtime/state_machine.py`
- Create: `src/agent_sdk/runtime/session_lifecycle.py`
- Create: `src/agent_sdk/runtime/idempotency.py`
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/runtime/commands.py`
- Modify: `src/agent_sdk/errors.py`
- Modify: `src/agent_sdk/api.py`
- Modify: `src/agent_sdk/__init__.py`
- Create: `tests/unit/runtime/test_session_state_machine.py`
- Create: `tests/integration/runtime/test_session_lifecycle.py`
- Modify: `tests/integration/context/test_public_context_api.py`
- Modify: `tests/integration/evaluation/test_evaluation_slice.py`
- Modify: `tests/integration/observability/test_queries.py`
- Modify: `tests/integration/observability/test_subscriptions.py`

**Interfaces:**
- Consumes: Task 1 idempotent commit result, strict `SessionStatus`/
  `SessionSnapshot`, existing snapshot preconditions, and SDK lifecycle
  admission.
- Produces: `SessionStateMachine.transition`, `SessionBusyError`,
  `RuntimeCommands.get_session/close_session/delete_session`, and public
  `sessions.get/close/delete`.

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
and deletion retries from a deliberately retained `deleting` snapshot. A public
`get` and `close`—even with a previously matching close key—must return
`INVALID_STATE` while deletion is retained; only `delete` may resume it. Add
concurrent public `sessions.create` tests for matching requests, different
workspace requests with one key, empty/oversized keys, SQLite reopen, and a
malformed stored replay result.

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

Use Task 1's strict `SessionSnapshot` and `SessionStatus`; do not redefine or
weaken them. Implement the transition table and validated transition helpers.
Retain the Task 1 `model_copy` override/validated reconstruction path and add
state-machine tests that invalid copied versions, duplicate work ids, and
closed-with-work updates raise `ValidationError`.

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
    idempotency: IdempotencyWrite | IdempotencyReplay | None = None,
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
        replay_preconditions=(exact_session_precondition(previous),)
        if idempotency is not None else (),
    )
```

The internal loader accepts `deleting` so `delete_session` can resume. The
public `get_session` wrapper rejects it with stable `INVALID_STATE`; query
services continue to use Store facts directly and cannot create new state.

Exact preconditions include version, owner Session id, and canonical snapshot
data. Retry at most eight exact-precondition races, reloading each time.

- [ ] **Step 5: Implement create/get/close/delete commands**

`create_session` accepts `idempotency_key: str | None`. The fingerprint contains
the exact ordered normalized workspace strings. A matching replay validates the
stored result as `SessionSnapshot`; malformed replay data becomes sanitized
`INTERNAL`.

Translate idempotency failures exactly once with the shared
`runtime/idempotency.py` boundary used by Runtime and Workflow commands:

```python
def _idempotency_public_error(error: IdempotencyError) -> AgentSDKError:
    if isinstance(error, IdempotencyReplayMissError):
        return AgentSDKError(
            ErrorCode.CONFLICT, "idempotency replay changed concurrently", retryable=True
        )
    if isinstance(error, IdempotencyConflictError):
        return AgentSDKError(
            ErrorCode.CONFLICT, "idempotency key conflicts with another request", retryable=False
        )
    if isinstance(error, IdempotencyValidationError):
        return AgentSDKError(
            ErrorCode.INVALID_STATE, "idempotency key is invalid", retryable=False
        )
    return AgentSDKError(
        ErrorCode.INTERNAL, "stored command result is invalid", retryable=False
    )
```

Catch only `IdempotencyError` at this boundary, raise the mapped error `from
None`, and let `asyncio.CancelledError` propagate unchanged. Workflow Task 4
imports the same helper rather than allowing its broad Store-error mapping to
turn conflicts into `INTERNAL`.

Commands normally consume `IdempotencyReplayMissError` inside their bounded
reload loop. The public mapping above is the fail-closed exhaustion fallback;
it never becomes `INTERNAL` and never authorizes a fresh write from a stale
present-record hint.

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

Check `DELETING` before looking up or committing a close idempotency record.
Thus an old matching close key cannot bypass the destructive retention boundary.
When a key is supplied for a closing/closed no-op, use an idempotency-only
`CommitBatch` with the exact Session precondition as both its normal and replay
precondition so its first returned snapshot is durable. A matching replay is
resolved after the current Session has been checked for `deleting`, but before
other current status changes affect the first-result semantics. If delete races
the commit, the replay precondition conflict reloads the Session and deletion
wins with `INVALID_STATE`.

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
    candidate = session_result_idempotency(current, key)
    hint = await self._store.get_idempotency(candidate.scope, key)
    write = (
        IdempotencyReplay(candidate.scope, key, candidate.request_fingerprint)
        if hint is not None
        else candidate
    )
    result = await self._store.commit(CommitBatch(
        events=(),
        preconditions=(exact_session_precondition(current),),
        idempotency=write,
        replay_preconditions=(exact_session_precondition(current),),
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

Update legacy SDK/Runtime test call sites that used command-level delete on an
active empty Session: close through `RuntimeCommands.close_session` first. Raw
`StateStore.delete_session` contract tests remain unchanged because the low-level
retention primitive intentionally has no business-state policy.

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
- Modify: `src/agent_sdk/permissions/policy.py`
- Modify: `src/agent_sdk/api.py`
- Create: `tests/integration/runtime/test_run_session_ownership.py`
- Modify: `tests/integration/runtime/test_text_agent_loop.py`
- Modify: `tests/integration/tools/test_permissioned_tool_slice.py`

**Interfaces:**
- Consumes: Tasks 1-2, `RunSnapshot`, `_RunEmitter`, `RunHandle`, SDK task
  tracking, current AgentSpec, full Tool capabilities, and effective Policy.
- Produces: current `ExecutionDescriptor` builders, internal
  `CommandOutcome[T]`, optional `RunAPI.start(..., idempotency_key=...)`, atomic
  Run attach/detach, and durable replay handles.

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
at every command/registration boundary. In a live SDK, an independent shielded
coordinator must finish task registration before the original cancellation is
re-raised, and a key retry must attach to that task. A simulated process-crash
gap may remain detached for T002 recovery. Completed and partially failed Tool
Runs must return the exact ordered durable Tool results.
Two AgentSpecs sharing the same name/revision string but differing in model or
model params must conflict on one key. So must equal input schemas with changed
Tool version/source/effects/timeout, or a changed `permission_default` Policy.
Corrupted descriptor/result replay data
must return sanitized `INTERNAL` without invoking LiteLLM or a Tool. A retained
`deleting` Session must reject an old matching Run key with `INVALID_STATE`, and
a detached nonterminal Run after SQLite reopen must report retryable
`CONFLICT: recovery required` from `result()` within the test timeout with zero
provider/Tool/MCP calls.

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

Use the frozen, extra-forbid Task 1 `ExecutionDescriptor` already present on
`RunSnapshot`:

```python
class ExecutionDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str
    agent_revision: str
    agent_spec_hash: str
    model: str
    model_params: Mapping[str, Any]
    initial_messages: tuple[Mapping[str, Any], ...]
    tool_capabilities: tuple[ToolCapabilityDescriptor, ...]
    tool_capability_hash: str
    policy: ExecutionPolicyDescriptor
    policy_hash: str
```

Deep-freeze/detach JSON exactly as `AgentSpec.model_params` does. The AgentSpec
hash covers `agent.model_dump(mode="json")`; full ToolSpecs are stored in the
exact registry/request order and hashed from canonical JSON, and the current
Policy descriptor/hash covers `permission_default`. `ToolSpec.version` is the
explicit application-maintained handler capability version. `RunAPI.start`,
Workflow, and Subagent creation all build this descriptor from the exact request
and effective Policy they pass to `RunEngine`. They set new Runs to `current`;
migrated/intermediate M01 Runs remain `legacy_unknown`/`None` and are never
automatically replayed.

Expose a detached `PolicyEngine.execution_config()` containing every current
execution-affecting setting (`permission_default` in M02), pass the same Policy
instance/config to RunAPI and WorkflowExecutor, and build descriptors from it.
Do not introspect private fields or assume the default. Tests construct two SDKs
with different defaults and prove one key conflicts before execution.

Override `RunSnapshot.model_copy` with JSON reconstruction plus
`RunSnapshot.model_validate`. Tests must prove copy/update cannot create a
nonterminal Run with Tool results, a current Run without a descriptor, or an
invalid terminal result.

`RuntimeCommands.start_run` accepts `idempotency_key`. On each bounded attempt:

1. Load a valid Session.
2. Reject `DELETING` before any idempotency fast-path lookup.
3. If a key has a stored-record hint, validate it, preserve its original Run
   id, but still submit an authoritative `IdempotencyReplay` built from the
   *current request fingerprint* with the exact current Session in
   `replay_preconditions`.
4. If no record hint exists, require `status is ACTIVE`, construct one Run id,
   and preserve it across retry attempts.
5. Add a new id once to `active_run_ids`, increment Session version, and create
   `session.run.attached` plus `run.created`.
6. Submit both projections/events, the exact normal Session precondition, the
   optional idempotency Write/Replay, and the exact Session replay precondition
   in one batch.
7. Return the stored first Run snapshot and `replayed=True` when Store replayed.

The fingerprint contains the Session id, complete canonical execution
descriptor, user input, parent/workflow/node ids, and canonical TaskEnvelope
JSON. After the explicit `DELETING` rejection, an idempotent replay may be
hinted through `get_idempotency` before other current Session statuses are
interpreted. A read never returns the public result directly: the subsequent
commit still carries the Write/Replay request and exact replay precondition and arbitrates
atomically. Therefore a retry can recover the first Run after the Session has
moved to closing/closed, but cannot win after deletion has linearized. If a
delete race changes the Session after the hint, reload; `DELETING` maps to
`INVALID_STATE` and completed cleanup maps to `NOT_FOUND`.

If the hinted record disappears before the authoritative commit,
`IdempotencyReplayMissError` causes a reload. The command never converts that
stale hint into a new Run on a closing/closed Session.

- [ ] **Step 4: Commit terminal Run and Session detachment together**

Extend `_RunEmitter` with the shared Session lifecycle coordinator. Define
`RUN_LIFECYCLE_FINAL_STATUSES = {COMPLETED, FAILED}` in T001 and extend it with
`CANCELLED` in T004. `INTERRUPTED`, `WAITING_RECONCILIATION`, `PAUSED`, every
waiting state, queued, created, and running remain Session-owned. Only a
lifecycle-final transition builds the detach batch:

```python
remaining = tuple(
    run_id for run_id in session.active_run_ids if run_id != updated_run.run_id
)
close_now = (
    session.status is SessionStatus.CLOSING
    and not remaining
    and not session.active_workflow_run_ids
)
updated_session = SessionSnapshot.model_validate({
    **session.model_dump(mode="json"),
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

The bounded Session-race loop uses eight attempts with event-based yielding
(`await asyncio.sleep(0)`) between conflicts. Exhaustion maps to
`AgentSDKError(CONFLICT, "session state changed concurrently", retryable=True)`;
it never maps to not-found unless the Session projection is actually absent.

- [ ] **Step 5: Prevent duplicate local execution and support durable handles**

Add a per-`RunAPI` lock and `run_id -> Task[RunResult]` registry. The critical
section covers command arbitration and initial task registration. Execute it in
an independent coordinator task and await it through `asyncio.shield`. If the
public caller is cancelled, keep awaiting the coordinator under shield, create
and register the engine task synchronously after an applied commit, then re-raise
the first `CancelledError`. `_SDKLifecycle.admit` remains held until this handoff
settles, so SDK close cannot cancel the coordinator between commit and registry.
A replay:

- reuses an existing local task;
- returns a detached `RunHandle` for every replayed nonterminal Run when no
  local task is registered, including an abandoned `created` Run;
- reconstructs `RunResult` from a completed snapshot or raises its durable
  failure from a terminal failed snapshot.

Make `RunHandle` accept `task: Task[RunResult] | None`. Detached `result()` and
`events()` never start execution. A detached handle exposes `attached=False`.
`result()` loads once: it reconstructs terminal durable state, but for any
nonterminal state immediately raises
`AgentSDKError(CONFLICT, "recovery required", retryable=True)`. Detached
`events()` yields only the currently available bounded Store pages and returns;
callers may subscribe/query through observability APIs for later cross-process
progress. Only a fresh `outcome.replayed is False` starts a task.
M02-T002 later supplies interruption recovery and lease fencing for a genuinely
abandoned nonterminal handle.

Only an applied outcome owns task launch. Before T002, a detached legacy or
abandoned `created` Run is observable but not executed by idempotency replay.
T002 exposes explicit recovery after the application re-registers AgentSpecs,
Tools/MCP capabilities, and verifies the persisted descriptor; SDK construction
alone never silently executes external work.

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
- Consumes: `CommandOutcome`, Session exact transition helper, resolved
  AgentSpecs, full Tool capabilities, effective Policy, Workflow IR hash, and existing executor active-task
  map.
- Produces: immutable `WorkflowExecutionDescriptor`, optional
  `WorkflowAPI.start(..., idempotency_key=...)`, atomic Workflow attach/detach,
  local replay attachment/detached recovery diagnostics, and closing-safe
  Workflow failure settlement.

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
returns the original Workflow id without duplicate nodes/children. Empty or
oversized keys map to `INVALID_STATE`; corrupted replay results map to sanitized
`INTERNAL`; neither case launches a Workflow/Run/Child. Exercise both Session
precondition conflict orderings, bounded-retry exhaustion, and caller
cancellation before/after durable Workflow creation. The live-SDK coordinator
must register the single Workflow task before re-raising cancellation. Also
cover:

- same Session/key/IR with a referenced AgentSpec that keeps its revision but
  changes model/model params, with the same schema but changed Tool
  version/source/effects/timeout, or with changed effective Policy, returns
  `CONFLICT` and launches nothing;
- a retained `deleting` Session rejects an old matching Workflow key with
  `INVALID_STATE` and launches no Workflow/Run/Child;
- after SQLite reopen, detached nonterminal `result()` and explicit
  `Workflow.resume` return retryable `CONFLICT: recovery required` within the
  test timeout and make zero provider/Tool/MCP calls;
- while the original SDK still owns one active local Workflow task, a second
  SDK instance cannot resume that Workflow; only the original provider/Tool
  call occurs. Two reopened SDK instances both fail recovery-required rather
  than racing side effects.

- [ ] **Step 2: Run RED**

```powershell
uv run --python 3.13 pytest tests/integration/workflow/test_workflow_session_ownership.py tests/integration/workflow/test_workflow_child_slice.py tests/integration/workflow/test_workflow_recovery.py -q
```

Expected: Workflow start has no key and Session work ownership is absent.

- [ ] **Step 3: Attach Workflow creation to active Session**

Change `WorkflowState.create` to return `CommandOutcome[WorkflowRunSnapshot]`
and accept an optional key. Validate the IR and resolve all referenced
AgentSpecs, full Tool capabilities, and effective Policy before generating
durable state. Use the Task 1 frozen models:

```python
class WorkflowAgentDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str
    agent_revision: str
    agent_spec: Mapping[str, Any]
    agent_spec_hash: str

class WorkflowExecutionDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_definition_hash: str
    agents: tuple[WorkflowAgentDescriptor, ...]
    tool_capabilities: tuple[ToolCapabilityDescriptor, ...]
    tool_capability_hash: str
    policy: ExecutionPolicyDescriptor
    policy_hash: str
```

Deep-freeze all nested JSON. Store referenced agents once in deterministic
first-node order; each full canonical AgentSpec includes LiteLLM model and model
params and must match its hash. Full Tool capabilities use exact registry/request
order and must match their hash; effective Policy config must match its hash.
Set new Workflows to `current` with a descriptor matching their IR, referenced
revisions, Tool capabilities, and Policy. Migrated M01 Workflows remain
`legacy_unknown`/`None` as established in Task 1.

Load the current Session first and reject `DELETING` before reading a stored
idempotency hint. A hint never returns directly; the authoritative commit
uses `IdempotencyReplay` with the current request fingerprint and carries the
exact current Session as `replay_preconditions`, just like Run start. A missing
hint requires `ACTIVE`; matching replay may return after
closing/closed, while a delete race reloads and returns `INVALID_STATE` or
`NOT_FOUND` according to the deletion linearization. A replay miss reloads and
never creates a Workflow from the stale hint.

One commit contains:

- `session.workflow.attached` at the next Session sequence;
- `workflow.started` and every initial Workflow/node projection;
- updated Session projection with the Workflow id;
- exact Session precondition;
- optional idempotency write plus exact Session replay precondition; its
  fingerprint includes Session id, canonical Workflow IR/definition hash, and
  the full Workflow execution descriptor, and its result is the full first
  Workflow snapshot.

Preserve one generated Workflow id across exact-precondition retries.

`WorkflowState` must not absorb `IdempotencyError` in its existing broad Store
failure helper. Propagate those typed errors unchanged to `WorkflowExecutor`,
translate them with the Task 2 `_idempotency_public_error` boundary, and preserve
`CancelledError`. Other Store failures retain the existing sanitized `INTERNAL`
mapping.

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
an executor start lock and the same shielded coordinator pattern as Run start, a
fresh outcome calls `_start_task`; a replay only
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
An abandoned nonterminal replay returns a detached handle with
`attached=False`; its `result()` loads durable state once and raises retryable
`CONFLICT: recovery required` while nonterminal. It never polls forever.

Narrow `WorkflowExecutor.resume` in T001. It may validate and return terminal
state or attach to an existing non-done task in that executor's `_active` map.
If the Workflow is nonterminal and there is no such local task, raise
`AgentSDKError(CONFLICT, "recovery required", retryable=True)` and do not call
`_start_task`. Only the explicit M02-T002 recovery API, after capability checks
and lease/CAS admission, may advance abandoned durable Workflow state. Do not
convert cancellation.

Use the same eight-attempt/yield policy as Run/Session transitions. Exhaustion
is public retryable `CONFLICT`, while a missing owner Session remains
`NOT_FOUND`.

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
value and holes are not reused. Inject one cleanup failure after
`session.deleting`, assert public get/close reject while a second delete resumes,
then prove the same complete cleanup. The E2E also checks the persisted Run
execution descriptor contains the AgentSpec, full Tool-capability, and Policy
hashes but contains no handler object or credential.
Persist and inspect one Workflow too: its descriptor must contain the exact
referenced AgentSpec, Tool-capability, and Policy hashes, and same IR/key with a
changed capability binding must conflict.

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

Before accepting the composed test, explicitly cross-check the focused evidence
for: concurrent matching/different public Session create; invalid key and
corrupt replay mappings for Session/Run/Workflow; both close/start and
close/terminal orderings; SQLite migration rollback at every phase; orphan and
row/JSON identity rejection for every M01 snapshot kind; deleting cleanup and
old-key replay races; detached nonterminal Run/Workflow bounded recovery
diagnostics; abandoned Workflow resume refusal; capability-mismatched Workflow
replay; and exact ToolResult replay. Do not treat the low-level Store contract
as a substitute for these public command tests.

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
