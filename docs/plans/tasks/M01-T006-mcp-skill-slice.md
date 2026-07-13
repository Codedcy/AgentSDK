# M01-T006 MCP and Skill Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect one MCP 2025-11-25 server, route one namespaced remote Tool through the normal local Tool/Policy loop, and progressively discover/activate one official-format `SKILL.md` without granting permissions.

**Architecture:** MCPManager owns official Python SDK ClientSession lifetimes behind stdio or Streamable HTTP configs, requires the 2025-11-25 negotiated protocol, paginates `tools/list`, and converts remote schemas/calls/results to detached local Tool contracts. SkillRegistry strictly validates Agent Skills frontmatter and records a full-file digest during discovery while exposing only catalog metadata; activation verifies that pinned digest before returning instructions, and resource resolution always re-checks real-path containment.

**Tech Stack:** official MCP Python SDK, PyYAML, pathlib, Pydantic.

## Global Constraints

- MCP Tool name is `mcp.<server>.<tool>` and still passes Policy.
- Skill scripts/resources cannot escape the Skill root.
- `allowed-tools` never modifies Policy.
- The release baseline is exactly MCP `2025-11-25`; stdio and Streamable HTTP are the only public transports, and legacy HTTP+SSE is not accepted.
- MCP response/SDK/transport objects never cross the Manager/Tool handler boundary; remote `isError` and transport exceptions become sanitized local Tool failures.
- `connect` is failure-atomic for registry entries and connection lifetime; `close` is idempotent and unregisters remote tools before the ClientSession/transport becomes unusable.
- M01 uses a private injected session connector for deterministic tests; it is not a public alternative MCP/provider protocol.
- Skill discovery returns name/description/location/digest and optional official metadata only. Instructions are `None` until explicit activation; referenced files load only on explicit, containment-checked access.

---

### Task 1: Add the minimal MCP and Skill adapters

**Files:**
- Create: `src/agent_sdk/mcp/config.py`
- Create: `src/agent_sdk/mcp/normalize.py`
- Create: `src/agent_sdk/mcp/manager.py`
- Create: `src/agent_sdk/skills/models.py`
- Create: `src/agent_sdk/skills/loader.py`
- Create: `src/agent_sdk/skills/registry.py`
- Modify: `src/agent_sdk/tools/registry.py`
- Modify: `src/agent_sdk/__init__.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/integration/mcp/test_mcp_tool_slice.py`
- Create: `tests/integration/skills/test_skill_slice.py`
- Create: `tests/fixtures/skills/demo/SKILL.md`

**Interfaces:**
- Produces: discriminated `StdioMCPTransport` / `StreamableHTTPMCPTransport`, `MCPServerConfig`, `MCPManager.connect/register_tools/close`; strict `SkillMetadata`, `ActivatedSkill.resolve_member/read_text`, `SkillRegistry.discover/activate`.
- Consumes: `ToolRegistry`, `ToolSpec`, `PolicyEngine`.

- [ ] **Step 1: Write MCP normalization and Skill disclosure tests**

```python
@pytest.mark.asyncio
async def test_mcp_tool_is_namespaced_and_registered(fake_mcp_session, registry) -> None:
    manager = MCPManager._for_test(registry, fake_mcp_session.connector)
    await manager.connect(MCPServerConfig(name="demo", transport=StdioMCPTransport(command="ignored")))
    assert registry.get("mcp.demo.echo").spec.source == "mcp:demo"

def test_skill_loads_metadata_before_body(skill_root: Path) -> None:
    skills = SkillRegistry([skill_root])
    catalog = skills.discover()
    assert catalog[0].name == "demo"
    assert catalog[0].instructions is None
    assert "Follow this demo" in skills.activate("demo").instructions
```

Also add focused tests proving: the fake initialize result must negotiate `2025-11-25`; all paginated tools register in deterministic namespace order; duplicate server/tool/invalid remote schema rolls back connection and registry entries; MCP result content/structuredContent is detached JSON and `isError` is sanitized; close unregisters only its MCP tools and is idempotent. Run a scripted normal `sdk.runs.start` against `mcp.demo.echo`: local default `ask` must publish a PermissionRequest before `session.call_tool`, proving remote annotations/registration cannot bypass Policy.

For Skills, test every official frontmatter constraint used in this slice: name is 1-64 lowercase ASCII letters/digits/hyphens, has no leading/trailing/consecutive hyphen, and matches its parent directory; description is 1-1024; compatibility is 1-500 when present; metadata is string-to-string; experimental `allowed-tools` is a space-separated string retained only as hints. Reject missing/malformed/unknown frontmatter, duplicate names, non-UTF-8/oversized files, skill-directory symlink escape, `..`/absolute/member symlink escape, and activation after the discovered file digest changes. Discovery must not retain/expose the body.

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/mcp/test_mcp_tool_slice.py tests/integration/skills/test_skill_slice.py -v`

Expected: missing MCP/Skill modules.

- [ ] **Step 3: Implement MCP config and normalizer**

Use a frozen, extra-forbid discriminated config for `stdio` and `streamable_http`; server names are safe lowercase namespace segments, timeouts are positive, and nested args/env/headers are detached. Raise a stable SDK error for any other transport. The default connector uses only official SDK `stdio_client` or `streamable_http_client`, enters them and `ClientSession` through an `AsyncExitStack`, calls `initialize()`, and rejects any negotiated protocol other than `2025-11-25`. Pin the dependency to `mcp>=1.27,<2` (v1 stable line) and update the lock.

Follow `nextCursor` until exhausted and validate every remote schema before changing the shared ToolRegistry. Manager keeps one connection per unique server name. Normalize each tool to `ToolSpec`; handler delegates to `ClientSession.call_tool` with keyword arguments, returns a detached JSON object containing normalized content and structured content, and converts `isError` into an internal exception that the existing ToolExecutor sanitizes. Do not include raw MCP `_meta` in model-facing content.

```python
def normalize_tool(server: str, remote: MCPTool, session: ClientSession) -> RegisteredTool:
    async def invoke(_: ToolContext, **arguments: Any) -> Any:
        result = await session.call_tool(remote.name, arguments)
        return normalize_mcp_content(result.content, result.structuredContent)
    spec = ToolSpec(
        name=f"mcp.{server}.{remote.name}",
        description=remote.description or remote.name,
        input_schema=remote.inputSchema,
        source=f"mcp:{server}",
    )
    return RegisteredTool(spec=spec, handler=invoke)
```

Add an identity-safe `ToolRegistry.unregister(name, expected=...)` or equivalent so failed connect/Manager close cannot remove an application tool it does not own. Registration and rollback remain deterministic; a closed Manager cannot reconnect.

- [ ] **Step 4: Implement strict Skill parsing**

```python
class SkillMetadata(BaseModel):
    name: str; description: str; location: Path; content_hash: str
    license: str | None = None; compatibility: str | None = None
    metadata: Mapping[str, str] = {}; allowed_tools: tuple[str, ...] = ()
    instructions: None = None
class ActivatedSkill(BaseModel):
    metadata: SkillMetadata; instructions: str; root: Path
```

Parse UTF-8 `SKILL.md` with a first-line/closing `---` YAML frontmatter block and `yaml.safe_load`; reject YAML that is not a mapping, unknown fields, YAML type coercions that violate official string fields, and unsafe/oversized input. Strictly enforce the official constraints listed in Step 1. Catalog ordering is deterministic and duplicate names never silently override. Compute SHA-256 over the full bytes but retain only validated metadata/location/digest during discovery. Activation rereads and reparses, requires an unchanged digest, and returns the Markdown body without frontmatter.

Resolve any explicitly requested member by joining to the selected activated root, calling `resolve(strict=True)`, then `relative_to(real_root)`; require a regular file for `read_text`. Absolute paths, `..`, junction/symlink escape, and missing members fail stably. `allowed-tools` remains inert metadata: neither loader nor registry imports Policy/ToolRegistry or registers/grants anything.

- [ ] **Step 5: Verify both adapters and Policy route**

Run: `uv run pytest tests/integration/mcp tests/integration/skills -v && uv run pytest -q && uv run ruff check src tests && uv run mypy src`

Expected: both public MCP transports select the official connector shape; paginated tools register atomically and invoke through local Policy; MCP objects/errors stay internal; Manager cleanup is complete. Skill body loads only on activate; official metadata is strict; digest and every member path are containment-safe; `allowed-tools` grants nothing; no context/session/task remains open.

- [ ] **Step 6: Commit**

```powershell
git add src/agent_sdk/mcp src/agent_sdk/skills tests/integration/mcp tests/integration/skills tests/fixtures/skills
git commit -m "feat: add mcp and skill slice"
```
