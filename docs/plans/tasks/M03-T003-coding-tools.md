# M03-T003 Built-in Coding Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship safe, observable `read`, `list`, `glob`, `grep`, `write`, `edit`, and `bash` tools using the same public registration path as application tools.

**Architecture:** Each built-in is a small handler that accepts `ToolContext`; common filesystem and process services centralize workspace containment, permission requests, atomic updates, cancellation, and bounded output.

**Tech Stack:** pathlib, asyncio subprocesses, tempfile, ripgrep when available, pytest-asyncio.

## Global Constraints

- All filesystem paths pass through workspace and permission normalization.
- Writes are atomic; edits require expected content or hash to prevent stale replacement.
- Bash uses argument-list execution by default; shell text is explicit and higher risk.
- stdout, stderr, exit code, timeout, truncation, environment policy, and artifacts are recorded.

---

### Task 1: Implement read-only coding tools

**Files:**
- Create: `src/agent_sdk/tools/builtins/files.py`
- Create: `src/agent_sdk/tools/builtins/search.py`
- Modify: `src/agent_sdk/tools/builtins/__init__.py`
- Create: `tests/integration/tools/test_read_tools.py`

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_read_respects_line_and_byte_limits(coding_tools, workspace) -> None:
    workspace.write_text("large.txt", "x\n" * 10_000)
    result = await coding_tools.call("read", {"path": "large.txt", "start_line": 10, "line_count": 20})
    assert result.metadata["truncated"] is True
    assert result.metadata["start_line"] == 10

@pytest.mark.asyncio
async def test_glob_order_is_stable(coding_tools) -> None:
    first = await coding_tools.call("glob", {"pattern": "**/*.py"})
    second = await coding_tools.call("glob", {"pattern": "**/*.py"})
    assert first.content == second.content
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/tools/test_read_tools.py -v`

Expected: complete read/list/glob/grep tools are missing.

- [ ] **Step 3: Implement handlers through shared services**

```python
async def read_tool(ctx: ToolContext, path: str, start_line: int = 1, line_count: int = 200) -> ToolValue:
    target = await ctx.workspace.authorize_path(path, effect="read")
    data = await asyncio.to_thread(target.read_bytes)
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    selected = "".join(lines[start_line - 1:start_line - 1 + line_count])
    return ToolValue.text(selected, metadata={"truncated": len(selected.encode()) < len(data), "start_line": start_line})
```

Use deterministic lexical ordering, explicit hidden-file options, result limits, binary detection, and `rg --json` when installed with a pure-Python fallback.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/integration/tools/test_read_tools.py -v`

Expected: tools are deterministic, bounded, permission-aware, and portable.

```powershell
git add src/agent_sdk/tools/builtins tests/integration/tools/test_read_tools.py
git commit -m "feat: add read-only coding tools"
```

---

### Task 2: Implement atomic write and edit

**Files:**
- Modify: `src/agent_sdk/tools/builtins/files.py`
- Create: `tests/integration/tools/test_write_edit_tools.py`

- [ ] **Step 1: Write failing stale-edit and atomicity tests**

```python
@pytest.mark.asyncio
async def test_edit_rejects_stale_hash(coding_tools, workspace) -> None:
    workspace.write_text("a.py", "old")
    result = await coding_tools.call("edit", {"path": "a.py", "expected_sha256": "bad", "content": "new"})
    assert result.status == "failed"
    assert workspace.read_text("a.py") == "old"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/tools/test_write_edit_tools.py -v`

Expected: conflict-safe edit is missing.

- [ ] **Step 3: Implement atomic replacement**

```python
def atomic_replace(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.replace(temporary, path)
```

Check expected hash/content after permission approval and immediately before replacement; return previous/new hashes and byte counts.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/integration/tools/test_write_edit_tools.py -v`

Expected: parent creation, overwrite policy, stale conflict, atomic replacement, and events pass.

```powershell
git add src/agent_sdk/tools/builtins/files.py tests/integration/tools/test_write_edit_tools.py
git commit -m "feat: add atomic write and edit tools"
```

---

### Task 3: Implement cancellable bash

**Files:**
- Create: `src/agent_sdk/tools/builtins/bash.py`
- Create: `tests/integration/tools/test_bash_tool.py`

- [ ] **Step 1: Write failing process tests**

```python
@pytest.mark.asyncio
async def test_bash_captures_exit_streams_and_timeout(coding_tools) -> None:
    result = await coding_tools.call("bash", {"argv": [sys.executable, "-c", "import sys; print('o'); print('e', file=sys.stderr); sys.exit(3)"]})
    assert (result.metadata["exit_code"], result.stdout.strip(), result.stderr.strip()) == (3, "o", "e")

@pytest.mark.asyncio
async def test_bash_cancel_terminates_process_tree(coding_tools, process_probe) -> None:
    call = await coding_tools.start("bash", process_probe.long_running_args())
    await call.cancel()
    assert await process_probe.process_tree_stopped()
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/tools/test_bash_tool.py -v`

Expected: complete bash capture/cancellation behavior is missing.

- [ ] **Step 3: Implement subprocess lifecycle**

```python
process = await asyncio.create_subprocess_exec(
    *argv,
    cwd=authorized_cwd,
    env=filtered_environment,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    **platform_process_group_options(),
)
try:
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
except (TimeoutError, asyncio.CancelledError):
    await terminate_process_tree(process, grace_seconds=kill_grace_seconds)
    raise
```

Filter inherited environment through an allowlist plus explicit values, redact secrets, and artifact-offload truncated streams.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/integration/tools/test_bash_tool.py -v`

Expected: Windows/POSIX fixtures pass for exit, streams, cwd, environment, timeout, cancellation, and output bounds.

```powershell
git add src/agent_sdk/tools/builtins/bash.py tests/integration/tools/test_bash_tool.py
git commit -m "feat: add cancellable bash tool"
```
