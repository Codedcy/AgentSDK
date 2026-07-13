# Integrated Vertical Slice and Reference CLI

## 1. Purpose

M01 ends with one deterministic scenario that proves the implemented slices compose through public APIs. The scenario is an acceptance harness, not a second runtime: durable state remains in the SDK Store, the CLI owns presentation and user interaction, and no HTTP service or dashboard is introduced.

The slice must exercise one real path through SQLite, LiteLLM, an application Tool, an MCP Tool, a Skill, context compaction, a generated Workflow with a Child Run, observability, evaluation, analytics, restart, and Session deletion.

## 2. Chosen approach

Use a composed public-API scenario with a quiescent restart boundary:

1. An application creates an SDK backed by SQLite and configures its own Tool, Skill roots, and MCP connection.
2. A scripted LiteLLM seam drives deterministic tests; the reference CLI uses the normal LiteLLM configuration supplied by the application.
3. The main Run calls an application Tool and an MCP Tool. The application resolves `ask` permission requests through `sdk.permissions`.
4. The model's final text is a bounded Workflow YAML document. The application displays it and explicitly approves or rejects it before calling `sdk.workflows.start`.
5. The application forces one L3 context compaction, composes a prompt manifest with the activated Skill, runs the Workflow/Child path, evaluates the main Run, and reads analytics.
6. Once all tasks and external connections are quiescent, the SDK closes. A new SDK instance opens the same SQLite file and verifies the durable facts through public query façades.
7. Deleting the Session removes all SDK-managed Session events, snapshots, Context, Workflow, Evaluation, and analytics contribution while leaving application workspace files untouched.

This is preferred over a monolithic hidden scenario runner because each component remains independently replaceable and the example demonstrates the intended application/SDK boundary.

## 3. Explicit M01 boundaries

- Restart proves durable reopen and read recovery after work is quiescent. Resuming an in-flight Run that was waiting on an in-process permission bridge belongs to M02 leases, reconciliation, and control APIs.
- Workflow generation is application-mediated in M01: model text is validated by the existing compiler and executed only after application approval. First-class proposal records, risk analysis, and resumable dynamic approval belong to M04.
- The CLI is a minimal best-practice consumer. Rich reference applications, monitor servers, packaging polish, and fault matrices belong to M06.
- M01 uses an application-registered write-like Tool. Production built-in `write`/`bash` tools and workspace policy hardening belong to M03.
- Analytics remains the explicit verdict and terminal Tool-status slice from M01-T009; it does not claim causal attribution, Tool usefulness, or improvement advice.

## 4. Component boundaries

### 4.1 `AgentSDK.for_test` SQLite path

`AgentSDK.for_test` accepts exactly one of an injected `StateStore` or `database_path`. A database path creates the existing lazy SQLite adapter and makes the SDK responsible for closing it. Existing injected-Store callers remain source compatible and retain application-owned Store lifecycle.

The test-only constructor still requires an injected `acompletion` callable. It does not add another model-provider abstraction; all model behavior continues through LiteLLM.

### 4.2 Context façade

`sdk.context` is a thin lifecycle-aware façade over the existing `ContextPlanner` and `ContextRetrieval`. It exposes:

- `build(session_id, *, model, model_window, output_reserve=0, tool_schema_tokens=0, safety_reserve=0, policy=None, force_level=None, protected_event_ids=()) -> ContextView`
- `get_capsule(capsule_id, *, session_id) -> ContextCapsule`
- `read_sources(capsule_id, *, session_id) -> tuple[ObservedEvent, ...]`

The caller supplies the already-budgeted Tool-schema token count; M01 does not introduce a second tokenizer policy in the façade. The façade owns no second Context state and delegates all persistence and deletion semantics to the Store-backed components.

### 4.3 Application-owned capabilities

`MCPManager`, `SkillRegistry`, and `PromptComposer` remain composable public components:

- `MCPManager(sdk.tools)` registers namespaced remote tools into the same Tool registry used by Runs. The application closes the manager before closing the SDK.
- `SkillRegistry(roots)` discovers and activates filesystem Skills. Skill metadata is inert until activation, and the application decides which instructions/resources enter a prompt.
- `PromptComposer` combines the coding profile, application-selected Skill text, the Context View identity, and current Tool schemas into a deterministic manifest.

The SDK does not automatically connect MCP servers, discover arbitrary filesystem roots, or print activated content.

### 4.4 Reference CLI

The example uses only imports re-exported from `agent_sdk` and Python's standard library. `argparse` handles configuration; no CLI dependency is added to the SDK package.

The CLI:

- creates a Session and starts a Run;
- consumes `sdk.events.subscribe` and renders concise JSON-line status records;
- races the Run result with `sdk.permissions.next_request`, asks the user, and resolves the request;
- treats a final YAML document as a Workflow candidate, displays it, and calls `sdk.workflows.start` only after approval;
- displays Run/Workflow state, Tool terminal facts, Child progress, token usage, Evaluation, and analytics through public records;
- performs no hidden logging, server startup, or automatic permission grant.

## 5. Deterministic E2E data flow

The acceptance test uses a scripted LiteLLM callable with model-specific turns:

- `fake/main`: application Tool call, MCP echo Tool call, then canonical two-node Workflow YAML;
- `fake/planner`: parent node text result;
- `fake/worker`: Child node verification result;
- `fake/compact`: a valid structured `ContextCapsule` citing the supplied source event ids.

The MCP fixture is a real stdio MCP server using protocol revision `2025-11-25`; it exposes one `echo` Tool. The Skill fixture contains one instruction file and one referenced resource. No network access or real model credentials are required.

The test records live Session events until `workflow.completed`, acknowledges two permission requests, and asserts that the application and MCP Tool results are both durable. It then verifies one L3 Capsule, one prompt manifest, a two-node Workflow whose second node is a Child Run, a passing exact-output evaluation, success rate `1.0`, Tool failure count `0.0`, and evidence ids that resolve to durable events.

After reopening the database, the test rechecks the main Run, timeline, execution tree, Workflow snapshot, Capsule sources, Evaluation event, and analytics without re-executing a Tool or model. After Session deletion it requires empty Session-filtered event results, NOT_FOUND for Session-owned snapshots exposed by public façades, `sample_count == 0` for recomputed success analytics, and the application-created workspace file to remain present.

## 6. Failure and lifecycle rules

- A denied permission produces the existing durable Tool/Run failure semantics; the CLI reports the SDK error and exits nonzero.
- Invalid generated YAML is shown as a rejected candidate and never creates a Workflow snapshot or event.
- MCP startup or protocol failure rolls back its Tool registrations and cannot partially start the Run scenario.
- Cancelling the event-display task is settled before closing resources. No example task or MCP owner is left running.
- The application waits for Run/Workflow work, closes its MCP manager, settles its display task, and then closes the SDK. SDK close waits for admitted Evaluation/Context work and closes its owned SQLite adapter.
- Ordinary provider, Store, MCP, Tool, and evaluator failures cross the example boundary only as `AgentSDKError`; the example never prints exception locals or raw third-party responses.

## 7. Alternatives considered

### Resume while waiting for permission

This would look more dramatic, but the M01 bridge is intentionally in-process and the runtime has no lease/reconciliation command that can safely recreate its waiter. Implementing it here would duplicate M02 and produce misleading recovery semantics.

### Subprocess-only CLI acceptance

A subprocess test would verify console packaging, but deterministic LiteLLM injection would require a second configuration protocol or a fake provider embedded in the example. M01 instead tests the reusable CLI runner in-process and reserves installed-console/subprocess matrices for M06.

### Add Typer and an SDK-owned capability container

Typer is unnecessary for a small example, while automatic MCP/Skill ownership would force configuration and lifecycle decisions not yet hardened. Standard-library CLI parsing plus application-owned capability components keeps the SDK base small and explicit.

## 8. Acceptance criteria

- The example source imports no private `agent_sdk.*` modules.
- The E2E scenario performs no network call and uses the real SQLite and stdio MCP adapters.
- Every user-visible state is obtained from a public immutable record or event.
- Reopen performs zero model and Tool calls and returns the same durable facts.
- Session deletion removes all SDK-managed Session facts and analytics contribution but not the workspace file.
- The full Python 3.13 test suite, Ruff, mypy, package resource smoke tests, and diff checks pass with no xfail acceptance path.
