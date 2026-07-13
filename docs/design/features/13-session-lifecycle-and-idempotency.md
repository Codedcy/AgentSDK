# Session Lifecycle and Durable Command Idempotency

## 1. Purpose

M02-T001 turns the M01 Session container into an explicit lifecycle and makes
duplicate entity-creating runtime commands safe across coroutines and SQLite
restarts. The design preserves the M01 public path while adding the missing
`get`, `close`, and guarded `delete` behavior.

This slice owns four guarantees:

1. A Session moves through `active`, `closing`, `closed`, and `deleting` using
   validated transitions.
2. Closing atomically blocks new Runs and Workflows while work already owned by
   the Session reaches a terminal boundary.
3. Duplicate Session, Run, and Workflow creation commands with the same
   idempotency key return the first durable result and never create a second
   entity.
4. Normal deletion is resumable, removes all Session-owned SDK facts including
   idempotency records, and never removes application workspace files.

## 2. Chosen approach

Use Store-enforced atomic idempotency plus a Session-owned execution registry.

- Every duplicate-producing command constructs its events, projections, and
  result first, then submits one `CommitBatch` containing an `IdempotencyWrite`.
- Memory and SQLite Stores decide under their existing commit lock/transaction
  whether the key is new, a matching replay, or a conflicting reuse.
- `SessionSnapshot` carries the ids of nonterminal Runs and Workflows. Creating
  work attaches its id in the same commit; terminal work detaches it in the same
  commit. The final detach from a closing Session also writes `session.closed`.

This is preferred over command-level check-then-write because the latter races
between concurrent callers and after process restart. It is preferred over
scanning all Session snapshots on every transition because a scan cannot be
made an atomic precondition through the current Store contract and scales with
the entire retained history.

## 3. Scope boundaries

M02-T001 establishes the durable primitive and adopts it for Session creation,
Run start, and Workflow start. These operations can create duplicate entities
and start execution, so they are the first required adopters.

The following approved behavior remains in its owning task:

- M02-T002 adds leases, stale-execution recovery, and reconciliation. Until
  then, idempotency prevents duplicate entities; lease fencing will prevent a
  second process from advancing the same nonterminal Run. New Runs persist a
  complete immutable execution descriptor now, so T002 can reconstruct the
  LiteLLM request after the application has re-registered matching Tools and
  explicitly starts recovery. Legacy M01 in-flight Runs/Workflows without a
  descriptor are never auto-replayed. In T001, `Workflow.resume` may attach to
  an existing local task or read terminal state, but an abandoned nonterminal
  Workflow returns retryable `CONFLICT` (`recovery required`) without starting
  work; cross-process advancement begins only after T002 lease acquisition.
- M02-T003 generalizes migration planning/checksums and completes managed
  Artifact cleanup. M02-T001 still supplies the required forward migration for
  its own table and deletes every currently stored Session fact atomically.
- M02-T004 adds durable cancellation, pause/resume, `close(force=True)`, and
  `delete(force=True, confirm_data_loss=True)`. M02-T001 does not fake force
  deletion by erasing a live Run without first persisting cancellation.
- M03/M04/M05 mutation APIs adopt the same idempotency primitive as their full
  command models are hardened. Permission resolution, Context compaction,
  wait-node responses, and evaluator execution are not silently claimed to be
  exactly-once in this slice.

## 4. Public lifecycle contract

`SessionStatus` is a root-exported `StrEnum` with the values:

```python
class SessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    DELETING = "deleting"
```

`SessionSnapshot` is frozen and rejects unknown fields. In addition to the M01
identity, workspaces, and version, it contains deterministic tuples
`active_run_ids` and `active_workflow_run_ids`. Each tuple is duplicate-free.
`closed` and `deleting` snapshots cannot contain active work.

All lifecycle updates reconstruct through `model_dump(mode="json")` plus
`model_validate`; Pydantic's unchecked default `model_copy(update=...)` is not
used for Session or Run transitions. Therefore copied updates cannot bypass
status/work ownership, uniqueness, positive-version, execution-descriptor, or
terminal-result invariants.

The public Session facade becomes:

```python
await sdk.sessions.create(workspaces=[...], idempotency_key=None)
await sdk.sessions.get(session_id)
await sdk.sessions.close(session_id, idempotency_key=None)
await sdk.sessions.delete(session_id)
```

`close` is nonblocking with respect to current work. It returns:

- `closed` immediately when an active Session owns no nonterminal work;
- `closing` when work remains;
- the current snapshot when already `closing` or `closed`;
- `INVALID_STATE` when deletion has started.

New Run and Workflow creation requires an atomically observed `active` Session.
Existing Run and Workflow state may continue committing while the Session is
`closing`. When the last owned execution reaches a terminal state, that same
commit writes the `closed` Session projection and event.

Normal `delete` accepts only `closed` or already `deleting` Sessions. A deletion
attempt against `active` or `closing` raises `SessionBusyError` with code
`CONFLICT`. The first attempt persists `session.deleting`; cleanup then removes
the Session event chain, all Session-owned projections and idempotency records.
If cleanup fails after the transition, a later call observes `deleting` and
resumes cleanup. Once cleanup succeeds, lookup returns `NOT_FOUND`.

Deletion is the retention boundary and has explicit precedence over ordinary
replay: while a `deleting` snapshot exists, only `delete` may resume cleanup.
`get`, `close` (including a matching old close key), and all execution-creating
methods return `INVALID_STATE`. Read-only trace subscribers may observe facts
already captured before cleanup wins; they cannot create new Session state.

## 5. Pure state machine

The state machine validates transitions without Store access:

```text
active  -> closing | deleting
closing -> closed  | deleting
closed  -> deleting
deleting -> (none)
```

Command preconditions intentionally narrow this general graph:

- normal close uses `active -> closing` or `active -> closed`;
- automatic terminal settlement uses `closing -> closed`;
- normal deletion uses `closed -> deleting`;
- force transitions from active/closing are reserved for M02-T004.

Same-state public calls are handled as idempotent command behavior rather than
as state-machine transitions.

## 6. Atomic idempotency contract

### 6.1 Record shape

`IdempotencyWrite`, `IdempotencyReplay`, and immutable `IdempotencyRecord`
contain the stable scope/key/fingerprint fields. A Write additionally contains
the Session owner and first result; a Replay is an atomic read-only assertion
that a matching record must already exist.

The common record/write fields are:

- `scope`: stable operation and ownership scope;
- `key`: application supplied, nonempty, at most 256 Unicode code points;
- `request_fingerprint`: SHA-256 of canonical JSON containing the command name
  and every behavior-affecting argument;
- `session_id`: Session deletion owner;
- `result`: detached JSON object representing the first command result.

Scopes are explicit:

```text
session.create
session/<session_id>/run.start
session/<session_id>/workflow.start
session/<session_id>/close
```

The create record is assigned to the newly generated Session id, so deleting
that Session also permits a future create with the former key.

### 6.2 Commit behavior

`CommitBatch.idempotency` is an optional `IdempotencyWrite | IdempotencyReplay`
and may carry exact
`replay_preconditions` that are checked only on the replay path. A Store commit
performs this sequence
inside its existing lock or `BEGIN IMMEDIATE` transaction:

1. Read `(scope, key)`.
2. If present, validate replay preconditions before comparing/returning the
   record. A mismatch raises `SnapshotPreconditionError` without mutation.
3. If absent for `IdempotencyReplay`, raise an internal typed replay-miss error
   without validating/writing normal state; the command reloads and decides
   again. If absent for `IdempotencyWrite`, validate normal preconditions,
   append events, update snapshots, and insert the record before commit.
4. If present with the same request fingerprint, skip every event/snapshot
   write and return the detached first record with `applied=False`.
5. If present with a different fingerprint, raise
   `IdempotencyConflictError`; no state changes.

The duplicate lookup happens before snapshot/event preconditions. Therefore a
retry returns the first result even if that entity has subsequently advanced.
Session deletion is the exception by design: it removes the record, after which
the old command is no longer replayable because the Session retention boundary
has been explicitly destroyed. A retained `deleting` Session is also an
exception: close, Run start, and Workflow start load the current Session and
reject deletion before attempting replay. They also include that exact current
Session as a replay precondition. Therefore replay and deletion linearize under
the Store lock: replay may return if it wins while the Session is not deleting;
if the deleting transition wins, an old key returns `INVALID_STATE` and cannot
start or attach work.

`StateStore.get_idempotency(scope, key)` is available for command recovery,
replay fast paths, and contract tests. An absent read never authorizes a write:
the later commit must still carry an `IdempotencyWrite` and arbitrate it
atomically. Conversely, a present read is only a hint: the authoritative path
uses `IdempotencyReplay`, so a record removed before commit produces a replay
miss and re-evaluation rather than silently inserting a new result. This
permits a replay of Run/Workflow start after the Session has already closed
without re-admitting new work, while retained `deleting` still wins as
described above.

### 6.3 Command outcomes

Internal entity-creating commands return `CommandOutcome[T]` containing
`value: T` and `replayed: bool`. Public methods continue returning the existing
snapshot/handle shapes. Only `applied=True` may launch a new Run/Workflow task.
A replay reuses an already registered local task or returns a detached durable
handle; it never starts abandoned `created` work. A detached handle exposes
that it is not attached to an executor, and `result()` on a nonterminal detached
Run/Workflow returns retryable `CONFLICT` (`recovery required`) immediately
rather than polling forever. Terminal detached handles reconstruct their result
from durable state. M02-T002 recovery and lease fencing decide how another
process may advance nonterminal work.

Within one live SDK, caller cancellation cannot create an avoidable handoff gap.
Run/Workflow start executes command commit and task registration in an
independent coordinator awaited through `asyncio.shield`; if the caller is
cancelled, the coordinator finishes registration first and the original
`CancelledError` is then re-raised. A process crash between durable commit and
registration remains a real crash boundary and is recovered only by T002.

Without an idempotency key, behavior remains source-compatible and every call
creates a new entity.

## 7. Session-owned work and concurrency

### 7.1 Run start

Run start loads and validates the Session snapshot, computes a new Session
version containing the Run id, and commits these facts atomically:

- `session.run.attached` on the Session aggregate;
- `run.created` on the Run aggregate;
- the updated Session and new Run snapshots;
- the optional idempotency record;
- an exact Session version/data precondition.

The Run snapshot also persists an immutable `ExecutionDescriptor` containing
the canonical full AgentSpec identity/content hash, LiteLLM model and model
parameters, initial messages/input, and the exact Tool schemas plus their hash.
The start-command fingerprint covers that descriptor and all parent/workflow
relationships, so two AgentSpecs that reuse a revision string cannot match.
Handlers and credentials remain application-owned; T002 recovery begins only
after the application re-registers capabilities whose schemas match the
descriptor.

On an exact-precondition race, the command reloads and retries a bounded number
of times. If close wins, start returns `INVALID_STATE`; if start wins, close sees
the new active Run and returns `closing`.

### 7.2 Run terminal transition

The lifecycle-final Run event and snapshot commit also removes the Run id from the
Session snapshot. If the Session is closing and no active Run or Workflow ids
remain, the commit emits `session.closed`; otherwise it emits
`session.run.detached`. A precondition race reloads only the Session projection
and retries the unchanged terminal Run transition. No terminal event is ever
written twice because a failed transaction rolls back both aggregates.

The terminal `RunSnapshot` persists the ordered `tool_results` needed to
reconstruct the same durable `RunResult` for an idempotent replay after restart.
Nonterminal snapshots require an empty tuple; failed snapshots preserve any
Tool results completed before the failure.

Lifecycle-final statuses are `completed`, `failed`, and—once M02-T004 adds
durable cancellation—`cancelled`. `interrupted`, `waiting_reconciliation`,
`paused`, permission/input/approval waits, queued, created, and running continue
to own the Session. They cannot allow normal close/delete to discard recoverable
or unresolved work.

### 7.3 Workflow start and terminal transition

Workflow start follows the same pattern with `session.workflow.attached`. A
matching duplicate key returns the original Workflow id and reattaches to an
existing local task when present; otherwise it returns a detached durable handle
instead of creating or executing another Workflow.

`WorkflowRunSnapshot` also persists a `WorkflowExecutionDescriptor`. It covers
the canonical Workflow definition hash, the full canonical `AgentSpec` and
content hash for every referenced agent revision, and the exact Tool schemas
plus schema hash used to construct its node Runs. Workflow start fingerprints
this descriptor with the Session id and Workflow IR. Thus a key cannot replay
across SDK instances that bind the same IR/revision strings to different
models, model parameters, or Tool schemas. Migrated M01 Workflows are marked
`legacy_unknown` and require explicit T002 resolution rather than resume.

Until T002 exists, `Workflow.resume` is deliberately limited: terminal state is
readable and an existing local task may be reattached, but a nonterminal
Workflow without that local task returns retryable `CONFLICT` (`recovery
required`) and performs no state transition, LiteLLM request, Tool call, MCP
call, Run creation, or Child creation.

Workflow completion/failure emits `session.workflow.detached` or
`session.closed` in the same transaction as the Workflow terminal event and
projection. If a closing Session prevents an existing Workflow from creating a
later Run/Child, the Workflow records its normal failure, detaches, and allows
the Session to close; it never remains permanently registered as active work.

## 8. SQLite migration and Store parity

`0002_idempotency.sql` creates:

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

Every SQLite open first configures a finite `busy_timeout` and establishes WAL
with bounded retry for `SQLITE_BUSY`/`SQLITE_LOCKED`, then begins
`BEGIN IMMEDIATE` with the same bounded retry before discovering schema/version
state. Retry uses monotonic deadlines plus event-loop yielding; exhaustion is a
stable retryable Store-open conflict, never an unbounded hang. The opener then
re-reads and validates schema state while holding the writer lock. An
empty database installs versions 1 and 2; an exact v1 database validates v1 and
applies migration 2; an already-valid v2 database is only validated and opened.
This retry-then-lock-then-discover order makes two concurrent opens converge instead of
both planning migration 2 from the same stale v1 observation. Migration 2
executes each statement without `executescript`, performs the ownership
backfill, inserts version 2, validates the complete v2 schema inside the same
transaction, and commits. Exceptions before commit use the cancellation-safe
rollback path. If cancellation races the SQLite commit, the SDK settles the
independent transaction coordinator before closing; reopening must observe
either the byte-equivalent valid v1 state or the complete validated v2 state,
never a partial migration. Unsupported, missing, duplicate, malformed, or
future version rows remain fail-closed. The generalized checksum/dry-run
migration engine belongs to M02-T003.

Version-1 Session JSON does not contain the new ownership tuples, Run execution
compatibility/Tool-result fields, or Workflow execution compatibility. During the
same v1-to-v2 migration transaction, SQLite globally enumerates Session, Run,
Workflow, other Session-owned snapshot rows, and events. It validates row kind/entity
id/session id/version against strict JSON identities, requires every owner
Session to exist, rejects orphans/cross-owner facts, validates known statuses,
and backfills each Session with deterministically ordered nonterminal Run and
Workflow ids. Legacy Runs receive `execution_compatibility="legacy_unknown"`,
`execution_descriptor=None`, and an empty ordered Tool-result tuple. Legacy
Workflows receive `execution_compatibility="legacy_unknown"` and
`execution_descriptor=None`. Context capsule/view and Evaluation rows are
validated with kind-specific models, row/JSON identities, owner/reference
checks, and versions; Workflow-node rows must match their owning Workflow's
nested node. Unknown snapshot kinds fail closed. The representation upgrade
keeps existing aggregate versions because it adds derived/compatibility fields
rather than domain transitions. A corrupt or unknown snapshot aborts migration
instead of risking a false `closed` decision. Validation is bidirectional:
Session/Run/Workflow start events require same-owner snapshots, aggregate
snapshots require their start facts, terminal Run/Workflow events must agree
with snapshot status, and Workflow-node/Context/Evaluation creation events must
resolve to matching same-owner projections. Historical Workflow-node events
are validated as a legal ordered transition prefix; their reduced final state,
not every historical status, must equal the current nested/standalone node
snapshot. Missing or contradictory facts abort before backfill.
Migration 2 exposes a stable transform identity that M02-T003 includes when it
bootstraps trusted checksums for versions 1-3. New databases need no backfill
because no projections exist before the current runtime writes them.

The v1-to-v2 upgrade is quiescent: no older SDK process/connection may continue
writing the database during or after migration. M02-T001 documents and tests
this precondition rather than claiming a cross-version writer fence that the M01
schema never implemented. M02-T003 adds the generalized migration lock/open
coordination used by later release tooling.

Memory and SQLite Stores have identical replay, conflict, deletion, defensive
copy, and cursor behavior. Idempotent replay does not allocate a new global
event cursor.

## 9. Error and cancellation behavior

- Key reuse with different request data maps to public `CONFLICT` without
  exposing stored input/result values.
- Invalid key/fingerprint input maps to public `INVALID_STATE`; a malformed
  stored replay maps to sanitized `INTERNAL`. Store-level idempotency exception
  classes cross repository helpers unchanged and are translated exactly once at
  the Runtime/Workflow public command boundary.
- Starting work on closing/closed/deleting maps to `INVALID_STATE`; a missing
  Session maps to `NOT_FOUND`.
- A detached nonterminal Run/Workflow and an abandoned nonterminal
  `Workflow.resume` map to retryable `CONFLICT` with the stable sanitized
  message `recovery required`; T001 never advances them without a local task.
- Normal deletion of a busy Session raises `SessionBusyError`/`CONFLICT`.
- Store corruption or malformed replay data maps to a sanitized `INTERNAL`
  error at the public boundary.
- `asyncio.CancelledError` is never converted. Store commit/rollback retains its
  existing cancellation-safe behavior.
- Cancelling a caller after an atomic commit may leave a durable entity whose
  result was not delivered. Repeating the command with the same key retrieves
  that entity; this is a primary purpose of durable idempotency.

## 10. Verification

The task is accepted only when tests prove:

- pure transition and Session snapshot invariants;
- matching replay, mismatched reuse, defensive copy, deletion cleanup, and
  Memory/SQLite parity;
- concurrent same-key callers create one Session, Run, or Workflow;
- concurrent public Session creation with the same key and matching request
  returns one id; different input with the same key returns `CONFLICT`;
- same key survives SQLite reopen and returns the first result;
- a version-1 database with nonterminal Run/Workflow snapshots upgrades with
  exact active ownership and cannot be closed prematurely;
- migration faults after DDL, backfill writes, version insertion, and precommit
  validation roll back to an exact usable v1 database; two concurrent opens
  converge on one exact v2; orphans and identity mismatches across every M01
  snapshot kind fail closed;
- different keys continue to create different entities;
- close/start and both close/terminal orderings resolve without orphaned work;
- a Run or Workflow terminal transition automatically closes the final closing
  Session;
- same revision with different model/params conflicts, and corrupted Run or
  Workflow replay results return sanitized public errors without execution;
- same Workflow IR/key with different resolved AgentSpec model/params or Tool
  schemas conflicts without execution;
- retained `deleting` rejects old Run/Workflow/close keys, while replay-vs-delete
  races linearize under exact replay preconditions and create no post-delete
  work;
- detached nonterminal Run/Workflow `result()` and abandoned Workflow `resume`
  return bounded recovery diagnostics without LiteLLM/Tool/MCP activity;
- deletion resumes from `deleting`, removes every current Session-owned fact
  and idempotency record, preserves workspace files, and returns not found;
- the M01 vertical E2E and full regression suite remain green.
