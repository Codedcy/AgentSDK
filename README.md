# Agent SDK

Agent SDK is an async Python library for recoverable, observable Agent Runs,
Workflows, Tools, Skills, Child agents, and SQLite-backed history. It supports
Python 3.12 and 3.13.

## Install and run with SQLite

```powershell
python -m pip install agent-sdk
python examples/v01_reference.py --smoke --database .agent-sdk/state.db --workspace .
```

For a real provider, set the LiteLLM credentials in the application environment,
omit `--smoke`, and select a LiteLLM model name with `--model`.

```python
from pathlib import Path
from agent_sdk import AgentSDK, AgentSDKConfig, AgentSpec

sdk = AgentSDK(AgentSDKConfig(database_path=Path(".agent-sdk/state.db")))
session = await sdk.sessions.create(workspaces=(Path(".").resolve(),))
agent = sdk.agents.define(AgentSpec(name="assistant", model="openai/gpt-4o-mini"))
result = await (await sdk.runs.start(session.session_id, agent, "Summarize README.md")).result()
print(result.output_text)
await sdk.close()
```

`AgentSpec.system_prompt=None` uses the packaged default profile. Set
`system_prompt="Follow the application's review policy."` for an application
layer; activated Skills are composed as separate prompt layers and recorded in
the Prompt Manifest.

## Tools and permissions

Register an application Tool before starting a Run:

```python
from agent_sdk import ToolContext, ToolSpec

async def lookup(context: ToolContext, *, key: str) -> dict[str, str]:
    return {"run_id": context.run_id, "value": key.upper()}

sdk.tools.register(
    ToolSpec(
        name="lookup",
        description="Look up an application value",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
        effects=("application.read",),
    ),
    lookup,
)
```

Configure `PermissionRule` entries for `allow`, `ask`, or `deny`. Built-in
`read`, `write`, and `bash` also enforce configured workspace roots and command
permissions. For `ask`, receive `sdk.permissions.next_request(run_id)` and call
`sdk.permissions.resolve(request.request_id, PermissionDecision.allow_once())`
or `PermissionDecision.deny("application denied")`.

An MCP server registers namespaced Tools in the same registry and therefore uses
the same authorization path:

```python
from agent_sdk import MCPManager, MCPServerConfig, StdioMCPTransport

manager = MCPManager(sdk.tools)
await manager.connect(MCPServerConfig(
    name="company",
    transport=StdioMCPTransport(command="python", args=("server.py",)),
))
# ... run Agents, then: await manager.close()
```

Point `AgentSDKConfig.skill_roots` at directories containing `SKILL.md`, then set
`AgentSpec(skills=("review",))`. Use `sdk.skills.activate("review")` to inspect
the activated Skill and `PromptComposer.compose(...)` when the application needs
the resulting Prompt Manifest directly.

## Workflow, Child agents, Trace, and recovery

Treat generated YAML as a candidate: validate it with
`candidate = sdk.workflows.compile(text)`, obtain application confirmation, and
only then call `sdk.workflows.start(session.session_id, candidate)`. v0.1 supports
conditions, bounded loops, and agent nodes. Parent Agents can use the ordinary
authorized control Tools `spawn_agent`, `send_message`, `list_children`, and
`wait_child`; the direct `sdk.children` facade is also available.

Subscribe while work is active with `sdk.trace.subscribe(...)`. Afterwards use
`await sdk.trace.timeline(run_id)` and `await sdk.trace.attribution(run_id)`;
evaluation and basic success/Tool-failure rates are exposed by `sdk.evaluations`
and `sdk.analytics`.

On reopen, completed safe-boundary work is read rather than repeated. Unknown
in-flight work remains interrupted until the application inspects
`sdk.recovery.pending_requests(run_id)` and calls `sdk.recovery.resolve(...)` to
retry with duplicate-effect acknowledgement or confirm an evidenced outcome. To
abort in v0.1, leave the request pending and close the SDK; `TERMINATE` resolution
is not implemented.
See the [quickstart](docs/guides/v01-quickstart.md),
[recovery guide](docs/guides/v01-recovery.md), and
[tracing and analysis guide](docs/guides/v01-tracing-and-analysis.md).

Normal Session deletion removes SDK-owned events and snapshots, but never deletes
application-owned workspace files.
