# Quickstart General Agent Example Design

## Goal

Add a small, runnable example that shows an application developer how to build
a useful general-purpose Agent with the public Agent SDK API. The example must
be easier to understand than the v0.1 reference flow and reference CLI while
still demonstrating the minimum production-shaped loop: durable state,
multi-turn conversation, workspace tools, permission decisions, and basic
observability.

## User Experience

The primary entry point is:

```powershell
python examples/quickstart_agent.py --model openai/gpt-4o-mini
```

The command starts an interactive prompt in the selected workspace. Each user
message starts one Run in the same Session, so later turns can use the durable
conversation history. Entering `exit` or pressing `Ctrl+C` closes the SDK
cleanly without deleting the Session.

At startup, the example creates or loads a Session and prints its identifier
once. Passing that identifier through `--session-id` reopens the same
conversation after a process restart. The example uses
`.agent-sdk/quickstart.db` by default and accepts explicit `--database` and
`--workspace` values. Each completed turn prints the final answer, Run ID,
provider token usage when available, and invoked Tool names. Resuming requires
the same database and the exact original resolved workspace; a workspace
mismatch is rejected before a Run starts.

## Agent and Tool Policy

The example defines one general-purpose Agent using the packaged general system
prompt. Its Tool allowlist contains the built-in `read`, `write`, and `bash`
tools.

The SDK configuration uses explicit workspace-scoped permission rules:

- `read` is allowed within the configured workspace;
- `write` requires an application decision and remains constrained to that
  workspace;
- `bash` requires an application decision, and its workspace rule binds the
  subprocess working directory used for policy matching.

An approved `bash` process is not sandboxed. It can use absolute paths outside
the workspace and inherits the application environment, including provider
credentials. Its stdout and stderr are sent to the model and stored in Session
history. The example must show this warning before approval.

When the SDK emits a permission request, the example displays the Tool name and
a bounded, redacted representation of supported Tool arguments. Control and
Unicode format characters are converted to visible escapes before bounding.
Write content is represented only by its UTF-8 byte count, and unknown Tool
arguments are omitted. The user can allow that single invocation or deny it.
The example does not implement permanent approvals, broad unrestricted mode,
policy editing, or a process sandbox.

## Runtime Flow

The example has four small responsibilities:

1. Parse configuration and either create or load a Session.
2. Define the Agent and start one Run for each input line.
3. Await Run completion and permission requests concurrently.
4. Print the final answer and a compact Run summary.

The compact summary is derived from public Trace APIs and includes the Run ID,
total token usage when reported by the provider, and Tool names invoked during
the Run. Raw events, full prompts, and Tool payloads are not printed by default.

Provider credentials remain in the application environment and are never
placed deliberately in `AgentSpec.model_params`. Approved `bash` commands
inherit that environment, however, and their stdout and stderr become Tool
output sent to the model and persisted in Session history. A command that
prints a credential can therefore persist it. LiteLLM remains the only model
gateway.

## Files

- Add `examples/quickstart_agent.py`.
- Add focused tests under `tests/integration/examples/`.
- Update `README.md` and `README.zh-CN.md` so this example is the recommended
  first Agent quickstart.

The existing `examples/v01_reference.py` remains the deterministic release
smoke flow. The existing `examples/reference_cli` remains the broader
public-API reference application.

## Test Strategy

Tests use `AgentSDK.for_test` with a deterministic LiteLLM-shaped completion
provider and do not open network sockets. They verify:

- a new Session is created in the configured workspace;
- multiple inputs run in the same Session;
- an existing Session can be selected explicitly only when its durable
  workspace exactly matches the resolved requested workspace;
- the Agent exposes only `read`, `write`, and `bash`;
- an `ask` permission can be allowed once or denied by the application;
- cancellation keeps denying later permissions until the real Run terminates;
- end-of-input at a permission prompt drains the Run and shuts down normally;
- permission summaries are bounded and redact write content and common secrets;
- two sequential SQLite-backed SDK instances preserve the Session workspace and
  prior conversation;
- the compact summary reports the Run ID, token usage, and invoked Tools;
- the CLI parser supplies the documented defaults;
- the example imports Agent SDK APIs only from the package root and documented
  public modules such as `agent_sdk.permissions`.

README contract tests verify that both languages contain equivalent installation
and invocation commands.

## Error Handling

Configuration and SDK errors are rendered as short user-facing messages and
produce a non-zero process exit. End-of-input, `exit`, and `Ctrl+C` are normal
shutdown paths. Exceptional or cancelled turns keep consuming and denying
permission requests until their real Run terminates, then preserve the caller's
original error or cancellation. A denied Tool call returns through the Agent
loop so the model can explain or choose another action. Closing the process
never deletes the Session database or application workspace files.

## Non-goals

- MCP, Skills, Workflows, Child Agents, evaluations, or aggregate analytics;
- a terminal UI, web UI, streaming token renderer, or external trace backend;
- custom Tool registration;
- automatic provider credential discovery beyond LiteLLM conventions;
- permanent permission decisions or unrestricted execution;
- a process or filesystem sandbox for approved commands;
- replacing either existing comprehensive reference example.
