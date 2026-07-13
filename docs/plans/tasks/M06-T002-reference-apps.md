# M06-T002 Reference CLI and Monitoring App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove SDK usability with public-API-only CLI and FastAPI/SSE reference applications that keep presentation decisions outside the SDK.

**Architecture:** The CLI demonstrates sessions, coding workflow, permission/input/workflow approvals, resume/cancel, trace queries, and analytics. The web app translates query/subscription APIs into JSON/SSE and a minimal monitoring page; it contains no runtime internals.

**Tech Stack:** Typer, Rich, FastAPI, Server-Sent Events, pytest, Playwright optional extra.

## Global Constraints

- Examples import only documented `agent_sdk` public symbols.
- The SDK emits data and callbacks; applications decide when/how to display it.
- Reference apps use SQLite by default and expose memory mode explicitly.
- Test fixtures use fake LiteLLM/MCP services and require no paid provider credentials.

---

### Task 1: Build the public-API-only CLI

**Files:**
- Modify: `examples/reference_cli/__init__.py`
- Modify: `examples/reference_cli/main.py`
- Create: `examples/reference_cli/bridges.py`
- Create: `examples/reference_cli/rendering.py`
- Create: `tests/e2e/test_reference_cli.py`

- [ ] **Step 1: Write failing CLI scenario test**

```python
def test_cli_can_approve_resume_and_show_trace(cli_runner, fake_environment) -> None:
    started = cli_runner.invoke(["run", "--workspace", str(fake_environment.workspace), "change greeting"])
    assert "Permission required" in started.stdout
    resumed = cli_runner.invoke(["resume", fake_environment.run_id, "--approve"])
    assert resumed.exit_code == 0
    trace = cli_runner.invoke(["trace", fake_environment.run_id, "--json"])
    assert json.loads(trace.stdout)["tool_calls"]
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/e2e/test_reference_cli.py -v`

Expected: reference CLI is missing.

- [ ] **Step 3: Implement commands and application bridges**

```python
app = typer.Typer()

@app.command()
def run(prompt: str, workspace: Path, session: str | None = None) -> None:
    sdk = build_sdk(permission_bridge=RichPermissionBridge(), input_bridge=RichInputBridge())
    result = sdk.run_sync(coding_agent(workspace), prompt, session_id=session)
    render_result(result)

@app.command()
def trace(run_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    snapshot = build_sdk().observability.get_run_snapshot_sync(run_id)
    render_snapshot(snapshot, json_output=json_output)
```

Add session list/close/delete, run/resume/cancel, workflow proposal approve/reject, child tree, event tail, usage, failure report, tool report, and insights commands.

- [ ] **Step 4: Add public-import guard and verify**

Run: `uv run pytest tests/e2e/test_reference_cli.py -v`

Expected: interactive and JSON modes, restart/recovery, approvals, queries, analytics, and public-import guard pass.

```python
def test_reference_cli_has_no_private_imports() -> None:
    for module in parse_imports(Path("examples/reference_cli")):
        assert not module.startswith("agent_sdk._")
        assert module not in PRIVATE_IMPLEMENTATION_MODULES
```

- [ ] **Step 5: Commit**

```powershell
git add examples/reference_cli tests/e2e/test_reference_cli.py
git commit -m "feat: add reference agent sdk cli"
```

---

### Task 2: Build the monitoring and analytics web example

**Files:**
- Create: `examples/monitor_server/app.py`
- Create: `examples/monitor_server/schemas.py`
- Create: `examples/monitor_server/static/index.html`
- Create: `examples/monitor_server/static/app.js`
- Create: `tests/e2e/test_monitor_api.py`

- [ ] **Step 1: Write failing API/SSE test**

```python
@pytest.mark.asyncio
async def test_monitor_resumes_sse_from_cursor(monitor_client, populated_sdk) -> None:
    first = await monitor_client.next_event("/api/events?after=0")
    second = await monitor_client.next_event(f"/api/events?after={first['cursor']}")
    assert second["cursor"] > first["cursor"]

@pytest.mark.asyncio
async def test_monitor_returns_tool_analysis(monitor_client, populated_sdk) -> None:
    response = await monitor_client.get("/api/analytics/tools")
    assert response.json()[0].keys() >= {"reliability", "efficiency", "usefulness"}
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/e2e/test_monitor_api.py -v`

Expected: monitor example is missing.

- [ ] **Step 3: Implement public query/SSE adapters**

```python
@app.get("/api/runs/{run_id}")
async def run_snapshot(run_id: str) -> RunSnapshotDTO:
    return RunSnapshotDTO.from_public(await sdk.observability.get_run_snapshot(run_id))

@app.get("/api/events")
async def events(after: int = 0) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        async for event in sdk.observability.subscribe(after_cursor=after):
            yield f"id: {event.cursor}\ndata: {event.model_dump_json()}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")
```

Expose Run tree/timeline/workflow/children/usage, permission and input responses, control commands, evaluations, failures, tool metrics, cross-run reports, and insights. Keep the page minimal and application-owned.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/e2e/test_monitor_api.py -v`

Expected: snapshots, SSE resume, controls, approvals, analytics, redaction, and no-private-import assertions pass.

```powershell
git add examples/monitor_server tests/e2e/test_monitor_api.py
git commit -m "feat: add reference monitoring app"
```
