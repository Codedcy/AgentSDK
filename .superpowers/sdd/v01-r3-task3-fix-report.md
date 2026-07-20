# v0.1 R3 Task 3 Review Fix Report

## Status

PASS. Independent review findings `C1 / I1` from `1a7af37` are fixed.

## Scope

The fix changes only the Task 3 prompt/runtime creation boundary, the shared
Skills preflight, the existing recovery and execution-tree consumers of
`run.created`, Task 3 integration tests, and this report. Task 4 middleware and
unrelated Agent Loop behavior were not added.

## Root-cause verification

### C1: private execution descriptors in public events

`RuntimeCommands.start_run` used the complete private `RunSnapshot` for the
public `run.created` event as well as for the private snapshot and idempotency
result. The event therefore exposed application system prompts, model
parameters, and raw tool schemas.

### I1: Skills preflight covered only direct runs

The Skills activation check lived in `RunAPI.start`. Workflow nodes and
subagents call the shared `RuntimeCommands.start_run` entry directly, so an
unavailable configured Skill reached durable run creation and the provider on
those paths.

## TDD evidence

Three regression tests were added before production edits:

- `test_public_run_events_never_expose_prompt_or_tool_sentinels`
- `test_workflow_missing_skill_fails_before_node_run_or_provider_call`
- `test_subagent_missing_skill_fails_before_child_run_or_provider_call`

Initial focused result:

```text
3 failed
```

The failures showed the exact review symptoms: private descriptor text in
`run.created`, Workflow execution reaching the provider, and no shared
subagent preflight entry.

After the fixes:

```text
3 passed
```

## Fix

- New `run.created` writes use schema version 2 and a dedicated
  `RunCreatedEventPayload`.
- The public payload keeps ordinary creation identity and user input needed by
  Context and trace consumers, plus descriptor/agent/tool and private-envelope
  hashes. It does not contain an execution descriptor, system prompt, Skill or
  packaged-profile instructions, raw model parameters, or raw tool schemas.
- The complete descriptor remains in the private Run snapshot and idempotency
  result, preserving recovery and replay evidence.
- Recovery authenticates schema-v2 creation events against the private Run
  snapshot and hashes. Schema-v1 full-snapshot creation events remain readable.
- Execution-tree assembly accepts both schema-v1 and schema-v2 creation events,
  loads authoritative private snapshots, and verifies their creation identity.
- `SkillRegistry.validate_agent` is injected into the single
  `RuntimeCommands.start_run` creation boundary used by direct runs, Workflow
  nodes, and subagents.
- Low-level `RuntimeCommands` remains backward compatible: callers that do not
  inject a preflight callback retain the prior no-op behavior.
- A failed Skills preflight raises the constant public `INVALID_STATE` error
  before any Run event, snapshot, provider call, or child task is created. A
  Workflow container may already exist, but no node Run is created.

## Verification

- Task 3 prompt tests: `6 passed`.
- Combined prompt/subagent/workflow/observability/context/provider-recovery
  gate: `235 passed`.
- Workflow recovery, child workflow, and subprocess recovery gate:
  `22 passed`.
- Release vertical slices: `3 passed`.
- Ruff over all source plus the changed Task 3 test: passed.
- Strict mypy over all 92 source files: passed.

