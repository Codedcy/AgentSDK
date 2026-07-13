# M02-T001 Session Lifecycle and Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete active/closing/closed/deleting Session behavior and durable idempotent commands.

**Architecture:** A pure state machine validates transitions; RuntimeCommands commits transition events, snapshots, and idempotency results atomically. Deletion is a Store operation after preconditions pass.

**Tech Stack:** Pydantic, SQLite migration, pytest-asyncio.

## Global Constraints

- Closing rejects new Run/Workflow/Child creation but permits existing Run control/completion.
- Deleted is not a persisted Session state; successful delete makes lookup return not found.
- Persistent permission rules survive Session deletion without Session-linked payloads.

---

### Task 1: Implement lifecycle and idempotency

**Files:**
- Create: `src/agent_sdk/runtime/state_machine.py`
- Modify: `src/agent_sdk/runtime/models.py`
- Modify: `src/agent_sdk/runtime/commands.py`
- Modify: `src/agent_sdk/storage/base.py`
- Modify: `src/agent_sdk/storage/memory.py`
- Modify: `src/agent_sdk/storage/sqlite.py`
- Create: `src/agent_sdk/storage/migrations/0002_idempotency.sql`
- Create: `tests/integration/runtime/test_session_lifecycle.py`
- Create: `tests/contract/test_idempotency.py`

**Interfaces:**
- Produces: `SessionStatus`, `SessionStateMachine.transition`, `RuntimeCommands.close_session/delete_session`, `IdempotencyRecord`, `StateStore.get_idempotency`.
- Consumes: M01 StateStore/CommitBatch and Runtime snapshots.

- [ ] **Step 1: Write lifecycle/idempotency tests**

```python
@pytest.mark.asyncio
async def test_close_waits_for_existing_run_and_rejects_new(commands) -> None:
    session = await commands.create_session([], idempotency_key="session-1")
    run = await commands.start_run(session.session_id, "agent:1", "x")
    closing = await commands.close_session(session.session_id)
    assert closing.status == "closing"
    with pytest.raises(AgentSDKError, match="closing"):
        await commands.start_run(session.session_id, "agent:1", "new")
    await commands.mark_run_terminal(run.run_id, "completed")
    assert (await commands.get_session(session.session_id)).status == "closed"

@pytest.mark.asyncio
async def test_duplicate_key_returns_same_entity(commands) -> None:
    first = await commands.create_session([], idempotency_key="same")
    second = await commands.create_session([], idempotency_key="same")
    assert first.session_id == second.session_id
```

- [ ] **Step 2: Verify tests fail**

Run: `uv run pytest tests/integration/runtime/test_session_lifecycle.py tests/contract/test_idempotency.py -v`

Expected: SessionStatus/close/idempotency behavior missing.

- [ ] **Step 3: Add pure transition rules**

```python
class SessionStatus(StrEnum):
    ACTIVE = "active"; CLOSING = "closing"; CLOSED = "closed"; DELETING = "deleting"

ALLOWED = {
    SessionStatus.ACTIVE: {SessionStatus.CLOSING, SessionStatus.DELETING},
    SessionStatus.CLOSING: {SessionStatus.CLOSED, SessionStatus.DELETING},
    SessionStatus.CLOSED: {SessionStatus.DELETING},
    SessionStatus.DELETING: set(),
}
```

Return `INVALID_STATE` for all other transitions.

- [ ] **Step 4: Persist idempotency with command result**

Add `(scope, key)` primary key and `result_json`. `CommitBatch` accepts one optional idempotency write; each Store checks/returns an existing record before applying a duplicate command.

```python
async def execute_idempotently(self, scope: str, key: str, command: Callable[[], Awaitable[CommandResult]]) -> CommandResult:
    existing = await self._store.get_idempotency(scope, key)
    if existing is not None:
        return CommandResult.model_validate(existing)
    result = await command()
    await self._store.commit(result.batch.with_idempotency(scope, key, result.model_dump(mode="json")))
    return result
```

- [ ] **Step 5: Implement close/delete preconditions**

Close emits closing, prevents new work, and auto-closes after the last terminal Run. Delete requires closed/no pending reconciliation; force delete requires `confirm_data_loss=True`, cancels nonterminal runs, removes Session facts/contributions, and schedules artifacts.

```python
async def delete_session(self, session_id: str, *, force: bool = False, confirm_data_loss: bool = False) -> None:
    snapshot = await self.get_session(session_id)
    if force and not confirm_data_loss:
        raise AgentSDKError(ErrorCode.CONFLICT, "force deletion requires data-loss confirmation", retryable=False)
    if not force and (snapshot.status != SessionStatus.CLOSED or await self._has_pending_reconciliation(session_id)):
        raise AgentSDKError(ErrorCode.INVALID_STATE, "session is not safely deletable", retryable=False)
    await self._store.delete_session(session_id)
    await self._artifacts.schedule_session_cleanup(session_id)
```

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/integration/runtime/test_session_lifecycle.py tests/contract/test_idempotency.py -v`

Expected: lifecycle and duplicate-command tests pass for memory and SQLite stores.

- [ ] **Step 7: Commit**

```powershell
git add src/agent_sdk/runtime src/agent_sdk/storage tests/integration/runtime tests/contract
git commit -m "feat: complete session lifecycle and idempotency"
```
