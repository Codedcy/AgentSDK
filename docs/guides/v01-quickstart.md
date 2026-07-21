# Agent SDK v0.1 quickstart

## 1. Install and create the first SQLite Run

Install on Python 3.12 or 3.13 and expose the provider credentials expected by
your LiteLLM model (for example `OPENAI_API_KEY`).

```powershell
python -m pip install agent-sdk
```

```python
from pathlib import Path
from agent_sdk import AgentSDK, AgentSDKConfig, AgentSpec

workspace = Path(".").resolve()
sdk = AgentSDK(AgentSDKConfig(database_path=Path(".agent-sdk/state.db")))
session = await sdk.sessions.create(workspaces=(workspace,))
agent = sdk.agents.define(AgentSpec(name="main", model="openai/gpt-4o-mini"))
result = await (await sdk.runs.start(session.session_id, agent, "Inspect README.md")).result()
print(result.output_text)
await sdk.close()
```

With no `system_prompt`, the packaged general profile is the default. Add a
custom application layer with
`AgentSpec(..., system_prompt="Follow the release review policy.")`.
Context is prepared before every model call. Configure its budget with
`ContextRuntimeConfig`; the runtime records applied L0-L4 views without deleting
source events.

## 2. Register Tools and choose authorization

```python
from agent_sdk import PermissionDecision, ToolContext, ToolSpec
from agent_sdk.permissions import PermissionRule

async def catalog(context: ToolContext, *, key: str) -> dict[str, str]:
    return {"run_id": context.run_id, "value": key}

sdk.tools.register(
    ToolSpec(
        name="catalog",
        description="Read the application catalog",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
        effects=("application.read",),
    ),
    catalog,
)
```

Pass rules through `AgentSDKConfig(permission_default="ask",
permission_rules=(PermissionRule(outcome="allow", tool="read",
path_prefix=workspace), PermissionRule(outcome="ask", tool="bash"),
PermissionRule(outcome="deny", tool="write")))`. Built-in `read`/`write` paths
must remain inside a Session workspace; built-in `bash` must match both workspace
and any `command_prefix` rule.

For an `ask` result, the application owns the decision:

```python
request = await sdk.permissions.next_request(run_handle.run_id)
await sdk.permissions.resolve(request.request_id, PermissionDecision.allow_once())
# Or: PermissionDecision.deny("application denied")
```

## 3. Connect MCP and activate a Skill

```python
from agent_sdk import MCPManager, MCPServerConfig, StdioMCPTransport

manager = MCPManager(sdk.tools)
await manager.connect(MCPServerConfig(
    name="company",
    transport=StdioMCPTransport(command="python", args=("mcp_server.py",), cwd=workspace),
))
```

MCP Tools are namespaced (for example `mcp.company.lookup`) and pass through the
same authorization pipeline. Close the manager after its Runs settle.

Set `AgentSDKConfig(skill_roots=(Path("skills"),))`, place strict metadata and
instructions in `skills/review/SKILL.md`, and declare
`AgentSpec(..., skills=("review",))`. `sdk.skills.activate("review")` returns the
activated Skill. Runtime Context and Skill layers are recorded in the Prompt
Manifest; applications may also build one explicitly with `PromptComposer`.

## 4. Validate, confirm, and start a generated Workflow

The model's YAML is untrusted candidate text. A condition and bounded loop look
like this:

```yaml
api_version: agent-sdk/v1
kind: Workflow
name: review
inputs: {enabled: true}
steps:
  - id: choose
    kind: condition
    when: {path: inputs.enabled, op: eq, value: true}
    then_steps:
      - {id: selected, kind: agent, agent_revision: worker:1, input: selected}
    else_steps:
      - {id: skipped, kind: agent, agent_revision: worker:1, input: skipped}
  - id: improve
    kind: loop
    until: {path: outputs.check.done, op: exists}
    max_iterations: 3
    body:
      - {id: check, kind: agent, agent_revision: worker:1, input: check}
```

```python
candidate = sdk.workflows.compile(generated_yaml)  # validation; no execution
if await application_confirms(candidate):
    workflow = await (await sdk.workflows.start(session.session_id, candidate)).result()
```

Never call `sdk.workflows.start` merely because `sdk.workflows.compile` succeeded;
explicit application approval is the start boundary.

## 5. Child control, Trace, analysis, and recovery

Agents receive the authorized `spawn_agent`, `send_message`, `list_children`, and
`wait_child` Tools when included by capability policy. The same operations are
available through `sdk.children`; a parent should inspect progress, exchange
messages, wait, and consume the returned Child result in a later model Context.

```python
from agent_sdk import EventFilter

async for event in sdk.trace.subscribe(
    filters=EventFilter(session_id=session.session_id), cursor=0
):
    display(event)

timeline = await sdk.trace.timeline(run_id)
attribution = await sdk.trace.attribution(run_id)
```

Use `sdk.evaluations.evaluate`, `sdk.analytics.success_rate`, and
`sdk.analytics.tool_failure_rate` for v0.1 analysis. Attribution is evidence-linked,
not causal inference.

After a process restart, call `sdk.recovery.recover_run`. An unknown in-flight
operation stays interrupted. Inspect `sdk.recovery.pending_requests(run_id)` and
use `sdk.recovery.resolve(request_id, action, actor=..., evidence=...)` for a safe
retry (including explicit duplicate-side-effect acknowledgement) or an evidenced
outcome. To abort in v0.1, keep the request pending and close the SDK without
replay; `ReconciliationAction.TERMINATE` is reserved but not implemented. See the
recovery guide before using this path.

Finally, close/delete history and then the SDK:

```python
await sdk.sessions.close(session.session_id)
await sdk.sessions.delete(session.session_id)
await sdk.close()
```

Session deletion removes SDK history; workspace files remain application-owned.
