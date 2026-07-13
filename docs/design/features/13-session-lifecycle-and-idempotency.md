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
  second process from advancing the same nonterminal Run.
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

`IdempotencyWrite` and immutable `IdempotencyRecord` contain:

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

`CommitBatch.idempotency` is optional. A Store commit performs this sequence
inside its existing lock or `BEGIN IMMEDIATE` transaction:

1. Read `(scope, key)`.
2. If absent, validate normal preconditions, append events, update snapshots,
   and insert the idempotency record before commit.
3. If present with the same request fingerprint, skip every event/snapshot
   write and return the detached first record with `applied=False`.
4. If present with a different fingerprint, raise
   `IdempotencyConflictError`; no state changes.

The duplicate lookup happens before snapshot/event preconditions. Therefore a
retry returns the first result even if that entity has subsequently advanced.
Session deletion is the exception by design: it removes the record, after which
the old command is no longer replayable because the Session retention boundary
has been explicitly destroyed.

`StateStore.get_idempotency(scope, key)` is available for command recovery,
replay fast paths, and contract tests. An absent read never authorizes a write:
the later commit must still carry the same `IdempotencyWrite` and arbitrate it
atomically. This permits a replay of Run/Workflow start after the Session has
already closed without re-admitting new work.

### 6.3 Command outcomes

Internal entity-creating commands return `CommandOutcome[T]` containing
`value: T` and `replayed: bool`. Public methods continue returning the existing
snapshot/handle shapes. Only `applied=True` may launch a new Run/Workflow task.
A replay reuses an already registered local task or returns a detached durable
handle; it never starts abandoned `created` work. M02-T002 recovery and lease
fencing decide how another process may advance that work.

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

On an exact-precondition race, the command reloads and retries a bounded number
of times. If close wins, start returns `INVALID_STATE`; if start wins, close sees
the new active Run and returns `closing`.

### 7.2 Run terminal transition

The terminal Run event and snapshot commit also removes the Run id from the
Session snapshot. If the Session is closing and no active Run or Workflow ids
remain, the commit emits `session.closed`; otherwise it emits
`session.run.detached`. A precondition race reloads only the Session projection
and retries the unchanged terminal Run transition. No terminal event is ever
written twice because a failed transaction rolls back both aggregates.

The terminal `RunSnapshot` persists the ordered `tool_results` needed to
reconstruct the same durable `RunResult` for an idempotent replay after restart.
Nonterminal snapshots require an empty tuple; failed snapshots preserve any
Tool results completed before the failure.

### 7.3 Workflow start and terminal transition

Workflow start follows the same pattern with `session.workflow.attached`. A
matching duplicate key returns the original Workflow id and resumes/attaches to
the existing execution handle instead of creating another Workflow snapshot.

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

Opening a version-1 database applies migration 2 exactly once and records
version 2. A new database applies versions 1 and 2. Unsupported, missing,
duplicate, or malformed version rows remain fail-closed. The generalized
checksum/dry-run migration engine belongs to M02-T003.

Version-1 Session JSON does not contain the new ownership tuples. During the
same v1-to-v2 migration transaction, SQLite scans Session-owned Run and Workflow
snapshots, validates their known status schemas, and backfills each Session with
deterministically ordered nonterminal ids. The representation upgrade keeps the
existing Session version because it adds derived fields rather than a domain
transition. A corrupt/unknown snapshot aborts the migration instead of risking a
false `closed` decision. New databases need no backfill because no projections
exist before the current runtime writes them.

Memory and SQLite Stores have identical replay, conflict, deletion, defensive
copy, and cursor behavior. Idempotent replay does not allocate a new global
event cursor.

## 9. Error and cancellation behavior

- Key reuse with different request data maps to public `CONFLICT` without
  exposing stored input/result values.
- Starting work on closing/closed/deleting maps to `INVALID_STATE`; a missing
  Session maps to `NOT_FOUND`.
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
- same key survives SQLite reopen and returns the first result;
- a version-1 database with nonterminal Run/Workflow snapshots upgrades with
  exact active ownership and cannot be closed prematurely;
- different keys continue to create different entities;
- close/start and close/terminal races resolve without orphaned work;
- a Run or Workflow terminal transition automatically closes the final closing
  Session;
- deletion resumes from `deleting`, removes every current Session-owned fact
  and idempotency record, preserves workspace files, and returns not found;
- the M01 vertical E2E and full regression suite remain green.
