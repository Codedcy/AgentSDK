# M01-T006 MCP and Skill Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize one MCP Tool into ToolRegistry and progressively load one valid `SKILL.md`.

**Architecture:** MCPManager owns client sessions and converts server tool schemas to local ToolSpecs. SkillRegistry discovers only metadata, then loads full instructions/resources on explicit activation.

**Tech Stack:** official MCP Python SDK, PyYAML, pathlib, Pydantic.

## Global Constraints

- MCP Tool name is `mcp.<server>.<tool>` and still passes Policy.
- Skill scripts/resources cannot escape the Skill root.
- `allowed-tools` never modifies Policy.

---

### Task 1: Add the minimal MCP and Skill adapters

**Files:**
- Create: `src/agent_sdk/mcp/config.py`
- Create: `src/agent_sdk/mcp/normalize.py`
- Create: `src/agent_sdk/mcp/manager.py`
- Create: `src/agent_sdk/skills/models.py`
- Create: `src/agent_sdk/skills/loader.py`
- Create: `src/agent_sdk/skills/registry.py`
- Create: `tests/integration/mcp/test_mcp_tool_slice.py`
- Create: `tests/integration/skills/test_skill_slice.py`
- Create: `tests/fixtures/skills/demo/SKILL.md`

**Interfaces:**
- Produces: `MCPServerConfig`, `MCPManager.connect/register_tools/close`, `SkillMetadata`, `ActivatedSkill`, `SkillRegistry.discover/activate`.
- Consumes: `ToolRegistry`, `ToolSpec`, `PolicyEngine`.

- [ ] **Step 1: Write MCP normalization and Skill disclosure tests**

```python
@pytest.mark.asyncio
async def test_mcp_tool_is_namespaced_and_registered(fake_mcp_server, registry) -> None:
    manager = MCPManager(registry)
    await manager.connect(MCPServerConfig(name="demo", transport=fake_mcp_server.transport))
    assert registry.get("mcp.demo.echo").spec.source == "mcp:demo"

def test_skill_loads_metadata_before_body(skill_root: Path) -> None:
    skills = SkillRegistry([skill_root])
    catalog = skills.discover()
    assert catalog[0].name == "demo"
    assert catalog[0].instructions is None
    assert "Follow this demo" in skills.activate("demo").instructions
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/mcp/test_mcp_tool_slice.py tests/integration/skills/test_skill_slice.py -v`

Expected: missing MCP/Skill modules.

- [ ] **Step 3: Implement MCP config and normalizer**

Use a discriminated config for `stdio` and `streamable_http`. Normalize `tools/list` entries to ToolSpec with a dynamically created Pydantic input model or JSON-Schema validator; handler delegates to `ClientSession.call_tool` and returns content plus structuredContent/Artifact candidates.

```python
def normalize_tool(server: str, remote: MCPTool, session: ClientSession) -> RegisteredTool:
    async def invoke(arguments: dict[str, Any], _: ToolContext) -> Any:
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

- [ ] **Step 4: Implement strict Skill parsing**

```python
class SkillMetadata(BaseModel):
    name: str; description: str; location: Path; content_hash: str
    instructions: None = None
class ActivatedSkill(BaseModel):
    metadata: SkillMetadata; instructions: str; root: Path
```

Require frontmatter name/description, directory-name match, lowercase/hyphen format, and references resolved under real Skill root.

- [ ] **Step 5: Verify both adapters and Policy route**

Run: `uv run pytest tests/integration/mcp tests/integration/skills -v`

Expected: MCP Tool is registered/namespaced; Skill body loads only on activate; MCP invocation produces a normal ToolCall permission event.

- [ ] **Step 6: Commit**

```powershell
git add src/agent_sdk/mcp src/agent_sdk/skills tests/integration/mcp tests/integration/skills tests/fixtures/skills
git commit -m "feat: add mcp and skill slice"
```
