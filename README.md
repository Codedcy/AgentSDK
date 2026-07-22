# Agent SDK

English | [简体中文](README.zh-CN.md)

Agent SDK `0.1.0` is an async Python foundation for building recoverable and
observable agents. It combines a LiteLLM-backed Agent Loop with Tools, MCP,
Skills, validated Workflows, Child agents, automatic L0-L4 Context compaction,
and SQLite-backed execution history.

The current release supports Python 3.12 and 3.13. It is a usable single-process
SDK baseline: applications own the user interface, approval experience, provider
credentials, and decisions about which Trace information to expose.

## Why Agent SDK

- Recoverable by default: SQLite is the standard store; an in-memory store is
  available for transient and test composition.
- One Tool pipeline: built-ins, application Tools, MCP Tools, and Child-control
  Tools share validation, authorization, execution, and Trace events.
- Structured orchestration: Workflows support conditions, bounded loops, and
  agent nodes; Child agents support durable two-way messages and result handoff.
- Bounded Context: the runtime automatically selects and records L0-L4 views
  without deleting source events.
- Evidence first: live events, historical timelines, token usage, evaluation,
  deterministic attribution, success rate, and Tool-failure metrics are public
  SDK capabilities.

## Install from source

Clone the repository and install it with Python 3.12 or 3.13:

```powershell
git clone https://github.com/Codedcy/AgentSDK.git
Set-Location AgentSDK
python -m pip install .
```

The repository is the supported distribution path for v0.1; no package-index
release is assumed by this README.

## Five-minute deterministic smoke run

```powershell
python examples/v01_reference.py --smoke --database .agent-sdk/state.db --workspace .
```

`--smoke` performs no provider or network calls. It emits one JSON line derived
from public SDK results and verifies:

- automatic L0-L4 Context selection;
- a Workflow condition and two bounded-loop iterations;
- Agent-driven Child spawn, two-way messaging, wait, and result consumption;
- live and historical Trace, evaluation, and evidence-linked attribution;
- reopening completed SQLite work without replay;
- Session deletion while application-owned workspace files remain intact.

The reference uses `permission_default="allow"` only to remain non-interactive.
Applications should configure explicit allow, ask, and deny rules before
admitting untrusted work.

## Run a real LiteLLM-backed Agent

Set the credential expected by your chosen LiteLLM provider in the application
environment. For example, with an OpenAI model:

```powershell
$env:OPENAI_API_KEY="your-key"
```

Then run the SDK from an async application:

```python
import asyncio
from pathlib import Path

from agent_sdk import AgentSDK, AgentSDKConfig, AgentSpec


async def main() -> None:
    workspace = Path(".").resolve()
    sdk = AgentSDK(
        AgentSDKConfig(database_path=Path(".agent-sdk/state.db"))
    )
    try:
        session = await sdk.sessions.create(workspaces=(workspace,))
        agent = sdk.agents.define(
            AgentSpec(
                name="assistant",
                model="openai/gpt-4o-mini",
                tool_allowlist=(),
            )
        )
        handle = await sdk.runs.start(
            session.session_id,
            agent,
            "Give me a two-sentence overview of this project.",
        )
        result = await handle.result()
        print(result.output_text)
    finally:
        await sdk.close()


asyncio.run(main())
```

`workspaces=(workspace,)` is intentional: it is a one-element Python tuple, and
the trailing comma creates the tuple. `SessionAPI.create` accepts any iterable of
`str` or `Path`, so `workspaces=[workspace]` is equivalent.

When `AgentSpec.system_prompt` is omitted, the packaged general system prompt is
used. Set it to add an application-specific prompt. Activated Skills are composed
as separate prompt layers and recorded in the Prompt Manifest. Raw credential
fields are rejected in durable `AgentSpec.model_params`; keep secrets in the
application environment or provider credential system.

## v0.1 capability matrix

| Area | Shipped in v0.1 | Important boundary |
| --- | --- | --- |
| Agent Loop | Async streaming model calls, Tool calls, bounded turns, cancellation, token accounting | LiteLLM is the only model gateway |
| Storage and recovery | SQLite by default, in-memory option, event/snapshot history, safe-boundary reopen, explicit reconciliation | Documented recovery uses one SDK instance in one process |
| Tools and permissions | Built-in `read`, `write`, and argv-based `bash`; custom Tool registration; allow/ask/deny rules by Tool, path, and command prefix | The application owns interactive permission decisions |
| MCP | Stdio and streamable HTTP servers register namespaced Tools in the normal Tool registry | MCP Tools use the same application authorization policy |
| Skills and prompts | Configurable Skill roots, strict `SKILL.md` activation, packaged and custom system prompts, Prompt Manifest | Applications choose which Skills an Agent may activate |
| Workflow | Validated YAML/object definitions, explicit start, conditions, bounded loops, agent nodes, durable state | Generated text is only a candidate until compile, application confirmation, and start |
| Child agents | Tool-driven spawn/send/list/wait, direct API access, bounded depth/count, durable mailbox, parent result consumption | Child capabilities remain constrained by application policy |
| Context | Automatic L0-L4 planning and compaction before model calls, source-event references, configurable budgets | Compaction is bounded summarization, not deletion of source history |
| Trace and analysis | Live subscriptions, historical timelines, execution trees, per-Run attribution, evaluation, success rate, Tool failures and failure rate | Attribution is deterministic evidence analysis, not causal proof |
| Extensibility | Application Tools, permission rules, Skills, prompts, Workflows, provider recovery adapters, Trace consumers | The SDK supplies primitives; the host application supplies product policy and presentation |

## Tools and permission decisions

Application Tools join the same registry as built-ins and MCP Tools:

```python
from agent_sdk import AgentSDK, ToolContext, ToolSpec


def register_lookup(sdk: AgentSDK) -> None:
    async def lookup(
        context: ToolContext,
        *,
        key: str,
    ) -> dict[str, str]:
        return {"run_id": context.run_id, "value": key.upper()}

    sdk.tools.register(
        ToolSpec(
            name="lookup",
            description="Look up an application value",
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
            effects=("application.read",),
        ),
        lookup,
    )
```

Configure workspace and command policy when creating the SDK:

```python
from pathlib import Path

from agent_sdk import AgentSDK, AgentSDKConfig, PermissionDecision
from agent_sdk.permissions import PermissionRule


def create_sdk(workspace: Path) -> AgentSDK:
    return AgentSDK(
        AgentSDKConfig(
            database_path=Path(".agent-sdk/state.db"),
            permission_default="ask",
            permission_rules=(
                PermissionRule(
                    outcome="allow",
                    tool="read",
                    path_prefix=workspace,
                ),
                PermissionRule(
                    outcome="allow",
                    tool="bash",
                    path_prefix=workspace,
                    command_prefix=("git", "status"),
                ),
                PermissionRule(outcome="deny", tool="write"),
            ),
        )
    )


async def resolve_next_request(sdk: AgentSDK, run_id: str) -> None:
    request = await sdk.permissions.next_request(run_id)
    await sdk.permissions.resolve(
        request.request_id,
        PermissionDecision.allow_once(),
    )
```

Built-in `read`, `write`, and `bash` enforce both the Session workspace roots and
the configured policy. The SDK suspends an `ask` decision; the host application
decides when and how to show it, then calls `allow_once()` or
`PermissionDecision.deny(...)`.

Connect an MCP server to the same Tool registry and authorization path:

```python
from pathlib import Path

from agent_sdk import AgentSDK, MCPManager, MCPServerConfig, StdioMCPTransport


async def connect_company_mcp(
    sdk: AgentSDK,
    workspace: Path,
) -> MCPManager:
    manager = MCPManager(sdk.tools)
    await manager.connect(
        MCPServerConfig(
            name="company",
            transport=StdioMCPTransport(
                command="python",
                args=("mcp_server.py",),
                cwd=workspace,
            ),
        )
    )
    return manager
```

The caller owns the returned manager and must run `await manager.close()` after
its Runs settle.

## Generated Workflow admission

Model-generated YAML is untrusted candidate text. Compile and validate it first,
obtain explicit application confirmation, and only then start it:

```python
from collections.abc import Awaitable, Callable

from agent_sdk import AgentSDK, WorkflowIR, WorkflowResult


async def run_confirmed_workflow(
    sdk: AgentSDK,
    session_id: str,
    generated_yaml: str,
    application_confirms: Callable[[WorkflowIR], Awaitable[bool]],
) -> WorkflowResult:
    candidate = sdk.workflows.compile(generated_yaml)  # validates; no execution
    if not await application_confirms(candidate):
        raise RuntimeError("workflow was not approved")

    handle = await sdk.workflows.start(session_id, candidate)
    return await handle.result()
```

Conditions select a branch from persisted inputs/outputs. Loops require a finite
`max_iterations`, so a generated Workflow cannot request an unbounded loop.

## Observe and recover work

While work is active, use `sdk.trace.subscribe(...)` to consume normalized live
events. Afterwards, use `sdk.trace.timeline(run_id)` and
`sdk.trace.attribution(run_id)` to inspect stages, Tool calls, Child progress,
Workflow state, token usage, evidence, failure stage, contributors, and suggested
improvements. `sdk.evaluations` and `sdk.analytics` expose deterministic
evaluation, success-rate, Tool-failure, and Tool-failure-rate queries.

Parent Agents can receive the ordinary authorized `spawn_agent`, `send_message`,
`list_children`, and `wait_child` Tools. The same operations are available
directly through `sdk.children`; mailbox messages, progress, and terminal Child
results are persisted and included in the parent's later Context.

After restart, completed safe-boundary work is read rather than repeated. Unknown
in-flight work remains interrupted until the application inspects
`sdk.recovery.pending_requests(run_id)` and resolves it. A terminal decision can
abort without provider or Tool replay:

```python
from agent_sdk import AgentSDK, ReconciliationAction


async def terminate_unknown_attempt(
    sdk: AgentSDK,
    request_id: str,
) -> None:
    await sdk.recovery.resolve(
        request_id,
        ReconciliationAction.TERMINATE,
        actor={"type": "operator", "id": "user-123"},
        evidence={"reason": "application chose not to retry"},
    )
```

Termination performs no replay and does not claim whether the interrupted
external attempt executed. It atomically fails the Run with error code
`application_resolution_aborted`. Deleting a Session removes SDK-owned
persisted history, events, and snapshots; it does not delete application-owned
workspace files.

## v0.1 boundaries

- Python 3.12 and 3.13 are supported; Python 3.14 is outside this release.
- Install from this repository's source. A package-index release is not assumed.
- The documented recovery model is one SDK instance in one process; coordinated
  multi-worker recovery is not part of v0.1.
- External effects have no exactly-once guarantee. Unknown attempts require an
  explicit application reconciliation decision.
- Generated Workflows are never automatically executed after generation; they
  require compile, validation, application confirmation, and explicit start.
- Aggregate cross-run multidimensional Trace analysis, useless-result scoring,
  advanced scheduling, exporters, and additional reliability hardening are
  post-v0.1 work.

## Documentation

- [v0.1 quickstart](docs/guides/v01-quickstart.md)
- [Recovery guide](docs/guides/v01-recovery.md)
- [Tracing and analysis guide](docs/guides/v01-tracing-and-analysis.md)
- [High-level design](docs/design/00-high-level-design.md)
- [v0.1 release ledger](docs/plans/releases/v0.1.md)

## Development and verification

Create an isolated Python 3.12 or 3.13 environment, install the project in
editable mode, and install the development dependencies declared in
`pyproject.toml`:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pip install "pytest>=8,<9" "pytest-asyncio>=0.25,<1" "hypothesis>=6,<7" "ruff>=0.9,<1" "mypy>=1.14,<2" "types-jsonschema>=4.23,<5" "types-PyYAML>=6,<7"
```

Run the release checks:

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy
```

The v0.1 release checkpoint recorded 2,956 passed with 6 expected platform
skips. The release ledger records that reproducible historical environment and
the remaining post-v0.1 work; rerun the checks above for the current tree.
