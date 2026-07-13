# M03-T004 MCP Client and Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete MCP 2025-11-25 stdio/Streamable HTTP integration and official `SKILL.md`-compatible progressive capability loading.

**Architecture:** MCP connections are supervised state machines whose remote capabilities become versioned registry entries. Skills are immutable revisions discovered from configured roots; metadata is loaded first and bodies/resources/scripts only on activation through normal policy gates.

**Tech Stack:** official MCP Python SDK, httpx, asyncio, YAML front matter, pytest-asyncio.

## Global Constraints

- Release baseline is MCP 2025-11-25; legacy HTTP+SSE is out of scope.
- Reconnect never silently replays a mutating call with unknown outcome.
- Remote catalog changes update fingerprints and emit events.
- Skill paths and scripts cannot escape the selected revision root.

---

### Task 1: Harden MCP connection and catalog lifecycle

**Files:**
- Modify: `src/agent_sdk/mcp/client.py`
- Modify: `src/agent_sdk/mcp/connection.py`
- Modify: `src/agent_sdk/mcp/catalog.py`
- Modify: `src/agent_sdk/mcp/transports.py`
- Create: `tests/integration/mcp/test_connection_lifecycle.py`
- Create: `tests/integration/mcp/test_remote_capabilities.py`

- [ ] **Step 1: Write failing lifecycle tests**

```python
@pytest.mark.asyncio
async def test_catalog_revision_changes_after_server_notification(mcp_fixture, registry) -> None:
    connection = await mcp_fixture.connect(registry)
    before = registry.fingerprint()
    await mcp_fixture.publish_tools_changed()
    await connection.wait_for_catalog_revision(2)
    assert registry.fingerprint() != before

@pytest.mark.asyncio
async def test_disconnect_marks_inflight_mutation_unknown(mcp_fixture) -> None:
    result = await mcp_fixture.disconnect_during_mutating_call()
    assert result.status == "unknown_outcome"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/mcp/test_connection_lifecycle.py tests/integration/mcp/test_remote_capabilities.py -v`

Expected: supervised connection/catalog behaviors are incomplete.

- [ ] **Step 3: Implement explicit state machine**

```python
class ConnectionState(StrEnum):
    CREATED = "created"
    CONNECTING = "connecting"
    READY = "ready"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"

async def call_tool(self, request: MCPToolRequest) -> MCPToolResult:
    call_id = await self._ledger.record_started(request)
    try:
        result = await self._session.call_tool(request.name, request.arguments)
    except TransportClosed as error:
        return await self._ledger.record_unknown(call_id, error) if request.mutating else await self._retry_read(call_id, request)
    return await self._ledger.record_result(call_id, result)
```

Negotiate protocol/capabilities, expose tools/resources/prompts, handle pagination and list-change notifications, supervise reconnect/backoff, and health-check both transports.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/integration/mcp/test_connection_lifecycle.py tests/integration/mcp/test_remote_capabilities.py -v`

Expected: stdio and Streamable HTTP fixtures pass for initialization, calls, changes, reconnect, close, and unknown outcomes.

```powershell
git add src/agent_sdk/mcp tests/integration/mcp
git commit -m "feat: harden mcp client lifecycle"
```

---

### Task 2: Complete Skill discovery, activation, and trust controls

**Files:**
- Modify: `src/agent_sdk/skills/models.py`
- Modify: `src/agent_sdk/skills/loader.py`
- Modify: `src/agent_sdk/skills/registry.py`
- Modify: `src/agent_sdk/skills/runtime.py`
- Create: `tests/fixtures/skills/`
- Create: `tests/unit/skills/test_loader.py`
- Create: `tests/integration/skills/test_activation.py`

- [ ] **Step 1: Write failing strict/lenient and containment tests**

```python
def test_strict_loader_rejects_invalid_front_matter(skill_root) -> None:
    with pytest.raises(SkillValidationError):
        SkillLoader(mode="strict").load(skill_root / "invalid")

@pytest.mark.asyncio
async def test_skill_script_uses_normal_permission_path(skill_runtime, permission_probe) -> None:
    await skill_runtime.activate("formatter")
    await skill_runtime.run_script("formatter", "scripts/run.py")
    assert permission_probe.last_request.kind == "command"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/skills/test_loader.py tests/integration/skills/test_activation.py -v`

Expected: revision, trust, and progressive loading behavior are incomplete.

- [ ] **Step 3: Implement immutable revisions and conflict policy**

```python
@dataclass(frozen=True, slots=True)
class SkillRevision:
    name: str
    description: str
    root: Path
    digest: str
    trusted: bool
    metadata: Mapping[str, Any]

def resolve_member(revision: SkillRevision, relative_path: str) -> Path:
    candidate = (revision.root / relative_path).resolve(strict=True)
    candidate.relative_to(revision.root.resolve(strict=True))
    return candidate
```

Discover roots in configured precedence order, reject duplicate names in strict mode, expose diagnostics in lenient mode, and pin the activated digest into Run provenance.

- [ ] **Step 4: Implement progressive disclosure**

Initial discovery returns name/description/digest only. Activation loads the entire `SKILL.md`; resources are resolved on demand and scripts execute only through the registered coding tools and permission broker.

```python
async def activate(self, name: str) -> ActivatedSkill:
    revision = self._registry.resolve(name)
    instructions = resolve_member(revision, "SKILL.md").read_text(encoding="utf-8")
    activated = ActivatedSkill(revision=revision, instructions=parse_skill_body(instructions))
    await self._events.append(skill_activated(activated))
    return activated

async def run_script(self, skill: ActivatedSkill, relative_path: str) -> ToolResult:
    script = resolve_member(skill.revision, relative_path)
    return await self._tools.execute_named("bash", {"argv": [sys.executable, str(script)]})
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/unit/skills/test_loader.py tests/integration/skills/test_activation.py -v`

Expected: official fixture compatibility, conflicts, revisions, path containment, resources, trust, and script permission tests pass.

```powershell
git add src/agent_sdk/skills tests/fixtures/skills tests/unit/skills tests/integration/skills
git commit -m "feat: complete skill loading and activation"
```
