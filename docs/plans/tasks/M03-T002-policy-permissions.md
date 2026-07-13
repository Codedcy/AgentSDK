# M03-T002 Policy and Permission Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce configurable workspace, path, command, and interactive permission policy with safe defaults and four grant scopes.

**Architecture:** Pure policy evaluation normalizes a `PermissionRequest` and returns `allow`, `deny`, or `ask`. `PermissionBroker` resolves `ask` through an application-provided bridge, persists scoped grants, and emits decisions without coupling policy to presentation.

**Tech Stack:** pathlib, shlex, SQLite, Hypothesis, pytest-asyncio.

## Global Constraints

- Explicit deny outranks allow; allow outranks ask; unmatched requests ask by default.
- Symlinks, junctions, case folding, UNC paths, and non-existing write targets cannot escape configured workspace roots.
- Missing UI bridge or invalid response converts `ask` to deny.
- Persistent grants contain sanitized rule data and no Session identifier.

---

### Task 1: Implement normalized path and command evaluation

**Files:**
- Modify: `src/agent_sdk/permissions/models.py`
- Modify: `src/agent_sdk/permissions/paths.py`
- Modify: `src/agent_sdk/permissions/commands.py`
- Modify: `src/agent_sdk/workspace.py`
- Create: `tests/unit/permissions/test_path_policy.py`
- Create: `tests/unit/permissions/test_command_policy.py`
- Create: `tests/property/test_permission_normalization.py`

- [ ] **Step 1: Write failing normalization and precedence tests**

```python
@given(segment=st.sampled_from(["..", ".", "folder"]))
def test_normalized_path_never_bypasses_denied_root(tmp_path, segment) -> None:
    policy = deny_subtree(tmp_path / "secret")
    request = path_request(tmp_path / "secret" / segment / "value.txt")
    assert policy.evaluate(request).action == PermissionAction.DENY

def test_command_rule_matches_executable_argv_and_cwd() -> None:
    rule = CommandRule.allow(executable="git", argv_prefix=["status"], cwd="{workspace}")
    assert rule.matches(command_request(["git", "status"], cwd="C:/repo"), workspace="C:/repo")
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/permissions tests/property/test_permission_normalization.py -v`

Expected: normalized path/command matching is incomplete.

- [ ] **Step 3: Implement containment without string-prefix checks**

```python
def resolve_candidate(root: Path, candidate: Path, *, for_write: bool) -> Path:
    absolute = candidate if candidate.is_absolute() else root / candidate
    existing = absolute
    suffix: list[str] = []
    while for_write and not existing.exists():
        suffix.append(existing.name)
        existing = existing.parent
    resolved = existing.resolve(strict=True)
    for part in reversed(suffix):
        resolved /= part
    return Path(os.path.normcase(os.path.normpath(resolved)))

def is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False
```

- [ ] **Step 4: Implement command descriptors and risk classification**

Parse list-form commands directly; when shell text is enabled, retain the original text and conservatively classify metacharacters, redirections, network/process/package operations, and destructive verbs. Match executable, argument prefix/pattern, working directory, effects, and risk—not a raw substring alone.

```python
@dataclass(frozen=True, slots=True)
class CommandDescriptor:
    executable: str
    argv: tuple[str, ...]
    cwd: Path
    shell_text: str | None
    risk: RiskLevel

def describe_command(argv: Sequence[str], cwd: Path, shell_text: str | None = None) -> CommandDescriptor:
    if not argv:
        raise ValueError("command argv cannot be empty")
    risk = classify_command(argv, shell_text=shell_text)
    return CommandDescriptor(Path(argv[0]).name.casefold(), tuple(argv[1:]), cwd, shell_text, risk)
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/unit/permissions tests/property/test_permission_normalization.py -v`

Expected: path escape cases and command precedence pass on Windows and POSIX-normalized fixtures.

```powershell
git add src/agent_sdk/permissions src/agent_sdk/workspace.py tests/unit/permissions tests/property/test_permission_normalization.py
git commit -m "feat: enforce normalized permission policy"
```

---

### Task 2: Implement interactive broker and grant scopes

**Files:**
- Modify: `src/agent_sdk/permissions/broker.py`
- Modify: `src/agent_sdk/permissions/store.py`
- Modify: `src/agent_sdk/storage/sqlite.py`
- Create: `tests/integration/permissions/test_broker.py`

- [ ] **Step 1: Write failing broker tests**

```python
@pytest.mark.asyncio
async def test_missing_bridge_fails_closed(broker_without_bridge) -> None:
    decision = await broker_without_bridge.authorize(ask_request())
    assert decision.action == PermissionAction.DENY

@pytest.mark.asyncio
async def test_session_grant_is_removed_with_session(sdk) -> None:
    await sdk.permissions.grant(ask_request(), scope="session", session_id="s1")
    await sdk.sessions.delete("s1")
    assert not await sdk.permissions.matches(ask_request(), session_id="s1")
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/permissions/test_broker.py -v`

Expected: permission scopes and fail-closed bridge behavior are missing.

- [ ] **Step 3: Implement scope resolution**

```python
class GrantScope(StrEnum):
    ONCE = "once"
    RUN = "run"
    SESSION = "session"
    PERSISTENT = "persistent"

async def authorize(self, request: PermissionRequest) -> PermissionDecision:
    cached = await self._store.match(request, run_id=request.run_id, session_id=request.session_id)
    if cached:
        return cached
    policy = self._policy.evaluate(request)
    if policy.action is not PermissionAction.ASK:
        return await self._record(request, policy)
    if self._bridge is None:
        return await self._record(request, PermissionDecision.deny("permission bridge unavailable"))
    response = await self._bridge.request_permission(PermissionPrompt.from_request(request))
    return await self._persist_and_record(request, response)
```

- [ ] **Step 4: Verify cleanup and durable global grants**

Run: `uv run pytest tests/integration/permissions/test_broker.py -v`

Expected: once is consumed once, run/session grants expire with ownership, persistent grants survive restart and Session deletion, and every decision is observable.

- [ ] **Step 5: Commit**

```powershell
git add src/agent_sdk/permissions src/agent_sdk/storage/sqlite.py tests/integration/permissions/test_broker.py
git commit -m "feat: add scoped permission broker"
```
