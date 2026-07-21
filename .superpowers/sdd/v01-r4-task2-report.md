# v0.1 R4 Task 2 — Durable Direct-Relation Mailbox

## Outcome

Implemented a durable, versioned mailbox for direct parent/child Runs, with
bounded optimistic-concurrency retries, optional idempotent send replay,
SQLite reopen validation, and atomic Context View consumption cursors.

Task 2 remains self-contained. It does not add the ChildCoordinator or child
control tools planned for Tasks 3 and 4.

## Design and invariants

- `AgentMessage`, `MailboxSnapshot`, and `MailboxCursorSnapshot` are frozen,
  closed-world public models. Message timestamps must be timezone-aware and are
  normalized to UTC; content is bounded to 1..32768 characters.
- A mailbox authenticates every nested message against its Session and
  recipient, requires contiguous recipient-local sequences beginning at one,
  and rejects duplicate message ids.
- `MailboxService.send` authenticates both durable Run snapshots and permits
  only a direct parent-to-child or child-to-parent edge in the same Session.
  Missing Runs return the same safe `NOT_FOUND` error, self/sibling/cross-
  Session sends are rejected, and terminal Runs cannot send or receive.
- Each send atomically commits the advanced mailbox, an
  `agent.message.sent` event, and the optional idempotency record under exact
  Run/mailbox preconditions. Conflicts reload and retry at most eight times.
- Mailbox/cursor bootstrap uses durable idempotency records already supported
  by `StateStore`; no SQL migration was required. Exact replay preconditions
  make concurrent first-bootstrap safe even when the projection has already
  advanced beyond v1.
- A replay is returned only after the current authenticated Runs, mailbox, and
  persisted command result agree on sender, recipient, Session, content, and
  message membership.
- `ContextPlanner.prepare` loads unread messages before strategy selection,
  renders them as current protected user sources with the sender Run id in the
  content and the message id as the source ref, and stores those ids in
  `ContextView.consumed_message_ids`.
- Every Context branch (deterministic L0/L1/L2, L3/L4 success, and L3/L4
  fallback) writes the Context View and advanced mailbox cursor in one
  `CommitBatch` under exact mailbox/cursor preconditions. A conflicting send or
  consumption reloads the mailbox and rebuilds the view.
- SQLite's current projection validator recognizes mailbox/cursor snapshots,
  authenticates their row identity and Run ownership, validates nested message
  identity through the models, and requires the cursor not to exceed the
  mailbox tail. The legacy v1 validator remains deliberately closed-world.

## TDD evidence

### Initial RED

`tests/unit/subagents/test_mailbox.py` first failed collection because the
mailbox models and service did not exist. After the basic relation/order slice
was green, the tests were expanded before implementation for cross-Session,
missing/terminal Run, content bounds, concurrent send, idempotency, and SQLite
reopen behavior.

The initial Context integration assertion observed
`consumed_message_ids == ()`; this established the RED for atomic injection and
cursor advancement.

### Concurrency and corruption RED

Controlled first-mailbox and first-cursor races initially exposed bootstrap
replay preconditions that could reject an already-advanced v2 projection.
Corrupt owner/session/sequence/duplicate-id snapshots were initially accepted
by the in-memory read path, and a corrupt nested owner survived until SQLite
reopen. Sender attribution was also absent from the first rendered source.

The final implementation made all of those focused regressions green, then
covered every Context compaction success/fallback branch, a controlled cursor
conflict, concurrent first cursor bootstrap, and SQLite cursor persistence.

A public-model compatibility test also established that mailbox `version` is
not derived from message count: the documented default version 1 remains valid
for a caller-constructed snapshot containing messages.

## Verification

Plan-specified focused gate:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\subagents\test_mailbox.py tests\integration\subagents\test_mailbox_context.py tests\integration\context\test_runtime_middleware.py -q
```

Result: `36 passed in 3.53s`.

Expanded subagent regression gate:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\subagents tests\integration\subagents -q
```

Result: `45 passed in 4.65s`.

Context regression gate, excluding one proven baseline failure described
below:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\context tests\integration\context -q -k "not authoritative_recovery_receives_exact_stored_prepared_request"
```

Result: `144 passed, 1 deselected in 8.63s`.

Static and diff gates:

```powershell
.\.venv\Scripts\python.exe -m mypy --strict src
.\.venv\Scripts\python.exe -m ruff check src\agent_sdk\subagents src\agent_sdk\context src\agent_sdk\storage\sqlite.py tests\unit\subagents tests\integration\subagents
git diff --check
```

Results: strict mypy passed for 94 source files; Ruff passed; diff check passed.

## Proven pre-existing recovery failure

The full Context suite has one failing node:

```text
tests/integration/context/test_context_recovery.py::test_authoritative_recovery_receives_exact_stored_prepared_request
```

It raises public `AgentSDKError: recovery required`. The exact node was rerun
against a clean detached worktree at baseline commit
`2eb252ee1e1ff1ae69cc15cf3297077aa403e166`, with that worktree's `src` forced
to the front of `sys.path`; it failed identically.

Post-mortem comparison showed the current and baseline executions are
state/event-isomorphic: the authoritative query is called once; the event path
ends at interrupted Run v3 plus `model.recovery.query.started` sequence 6; the
model operation remains `STARTED` at lease generation 3; and no reconciliation
request exists. Task 2 adds the expected empty mailbox/cursor v1 projections,
but does not change the recovery evidence, Run/operation state, event path, or
failure point. Recovery code was therefore not changed outside Task 2 scope.

## Files changed

- `src/agent_sdk/subagents/models.py`
- `src/agent_sdk/subagents/mailbox.py`
- `src/agent_sdk/subagents/__init__.py`
- `src/agent_sdk/context/sources.py`
- `src/agent_sdk/context/planner.py`
- `src/agent_sdk/storage/sqlite.py`
- `tests/unit/subagents/test_mailbox.py`
- `tests/integration/subagents/test_mailbox_context.py`
- `.superpowers/sdd/v01-r4-task2-report.md`

## Final self-review

- Send ordering is recipient-local and monotonic under concurrency.
- Idempotent replay cannot bypass current Run relation/state authentication or
  return a command result absent from the authenticated mailbox.
- A persisted Context View cannot claim message consumption without the cursor
  advancement in the same atomic batch.
- Retry loops are bounded and return safe public errors; durable corruption is
  mapped to non-leaking internal errors or rejected during SQLite reopen.
- Existing generic snapshot storage is reused; no migration or Task 3/4 API is
  introduced.
