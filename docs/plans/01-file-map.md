# Agent SDK File Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix source-file ownership and stable cross-module contracts before implementation begins.

**Architecture:** Files are grouped by responsibility and communicate through Pydantic models and Protocols. Runtime orchestration depends on contracts, while adapters such as SQLite, LiteLLM, MCP, exporters, and examples remain at package edges.

**Tech Stack:** Python 3.12+, src-layout package, Pydantic v2 models, typing Protocols, asyncio.

## Global Constraints

- A source file has one clear responsibility and should stay below roughly 400 lines; split before mixing unrelated concerns.
- Public imports are re-exported only from `agent_sdk/__init__.py` and explicit subpackage `__init__.py` files.
- Third-party response/exception types never cross internal adapter boundaries.
- Contracts use immutable/frozen Pydantic models unless mutation is intrinsic to a local builder.
- Event payloads, public errors, Workflow IR, and persisted records carry schema versions.

---

## Root and Package Layout

```text
pyproject.toml
README.md
src/agent_sdk/
  __init__.py
  config.py
  errors.py
  ids.py
  api.py
  workspace.py
  secrets.py
  events/{models.py,bus.py,upcast.py}
  storage/{base.py,memory.py,sqlite.py,migrations.py,artifacts.py,migrations/0001_initial.sql}
  runtime/{models.py,commands.py,state_machine.py,leases.py,reconciliation.py,engine.py,handles.py}
  models/litellm_gateway.py
  prompts/{models.py,composer.py,profiles/general/system.md,profiles/coding/system.md}
  tools/{models.py,registry.py,executor.py,builtins/files.py,builtins/search.py,builtins/bash.py}
  permissions/{models.py,policy.py,paths.py,commands.py,store.py,broker.py}
  mcp/{config.py,manager.py,normalize.py,connection.py,transports.py,catalog.py}
  skills/{models.py,loader.py,registry.py,runtime.py}
  context/{models.py,budget.py,planner.py,strategies.py,compactor.py,retrieval.py}
  workflow/{models.py,dsl.py,builder.py,compiler.py,expressions.py,events.py,state.py,executor.py,scheduler.py,generation.py,risk.py,service.py,templates/coding.yaml}
  subagents/{models.py,limits.py,messages.py,handles.py,scheduler.py,service.py}
  observability/{queries.py,projections.py,subscriptions.py,redaction.py,exporters/{base.py,jsonl.py,python.py,otel.py,worker.py}}
  evaluation/{models.py,registry.py,engine.py,builtins.py,failures.py,classifier.py}
  analytics/{models.py,queries.py,comparison.py,attribution.py,tool_effectiveness.py,rules.py,insights.py}
  profiles/coding.py
  testing/{scenario.py,fakes.py,faults.py,contracts.py}
examples/{reference_cli,monitor_server,coding_workflow}
tests/{unit,integration,contract,property,faults,compat,package,docs,release,e2e,fixtures}
benchmarks/
scripts/
```

## Stable Contract Names

| Contract | Canonical location |
|---|---|
| `AgentSDKConfig` | `agent_sdk.config` |
| `AgentSDKError`, `ErrorCode` | `agent_sdk.errors` |
| `EventEnvelope`, `EventPayload`, `StoredEvent` | `agent_sdk.events.models` |
| `EventStore`, `StateStore`, `Transaction` | `agent_sdk.storage.base` |
| `AgentSpec`, `SessionSnapshot`, `RunSnapshot`, `RunStatus` | `agent_sdk.runtime.models` |
| `AgentSDK`, `RunHandle` | `agent_sdk.api`, `agent_sdk.runtime.handles` |
| `LiteLLMGateway`, `ModelRequest`, `ModelEvent` | `agent_sdk.models.litellm_gateway` |
| `ToolSpec`, `ToolContext`, `ToolResult` | `agent_sdk.tools.models` |
| `PermissionRequest`, `PermissionDecision`, `PolicyEngine` | `agent_sdk.permissions` |
| `ContextView`, `ContextCapsule`, `CompactionPolicy` | `agent_sdk.context.models` |
| `WorkflowDefinition`, `WorkflowIR`, `NodeRun` | `agent_sdk.workflow.models` |
| `TaskEnvelope`, `ChildResult` | `agent_sdk.subagents.models` |
| `EvaluationResult`, `Evaluator` | `agent_sdk.evaluation.models` |
| `AnalyticsQuery`, `AnalyticsResult`, `ImprovementInsight` | `agent_sdk.analytics.models` |

## Dependency Direction

```text
api -> runtime/workflow/subagents/queries
runtime -> events/storage/models/tools/context/evaluation
workflow -> runtime/tools/evaluation/storage
subagents -> runtime/permissions/context
analytics -> observability/evaluation/storage
adapters -> contracts
contracts -X-> adapters
```

## Review Rule

Any task that introduces a cross-package import not represented above must update this plan and explain why the dependency does not create a cycle.
