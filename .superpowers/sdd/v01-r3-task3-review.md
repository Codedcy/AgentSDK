# v0.1 R3 Task 3 Independent Review

Review range: `794739f..f80a956`

Verdict: **CHANGES_REQUIRED**

- Spec: **FAIL**
- Quality: **FAIL**
- Critical: **1**
- Important: **1**
- Minor: **0**

Task 3 is not approved. The focused implementation is otherwise coherent, but
the two findings below violate explicit R3 contracts and must be fixed and
independently re-reviewed before Task 4 proceeds.

## Findings

### C1 — Raw application prompt is exposed through the public Trace event

Evidence:

- `AgentSpec.system_prompt` is copied into `DurableAgentSpec`
  (`src/agent_sdk/runtime/models.py`, `src/agent_sdk/runtime/execution.py`).
- `RuntimeCommands.start_run` serializes the entire `RunSnapshot` as
  `run_data`, then publishes that object unchanged as the payload of the public
  `run.created` event (`src/agent_sdk/runtime/commands.py:529-565`).
- Therefore the new durable descriptor places the raw application system
  prompt at
  `run.created.payload.execution_descriptor.agent.system_prompt`. The same
  public payload also continues to contain full Tool capability/spec/schema
  objects rather than only their hashes.
- A fresh in-memory SDK reproduction using
  `system_prompt="SECRET_SYSTEM_PROMPT"` printed:

  ```text
  secret_in_run_created= True
  path_value= SECRET_SYSTEM_PROMPT
  ```

- `tests/integration/prompts/test_runtime_prompt.py` only searches the
  `prompt.manifest.created` payload. It does not inspect all public events, so
  it misses the actual leak.

Impact:

This directly violates the global R3 constraint that public Trace events
contain ids/hashes rather than raw prompt text, and the Task 3 requirement that
raw prompt/profile/Skill/Tool-schema material be absent from public Trace
payloads. Application system prompts commonly contain confidential operating
instructions or credentials, so this is a data-exposure boundary rather than a
cosmetic trace-shape issue.

Required direction:

- Keep recovery-required private data in durable snapshots/references, but
  publish a redacted/hash-only `run.created` projection (or a versioned
  equivalent that cannot expose prompt or Tool-schema bodies).
- Add a sentinel-based integration test that starts a real Run and scans
  **every** public event payload for application prompt text, packaged profile
  text, activated Skill instructions, and distinctive Tool-schema content.
- Preserve current recovery and SQLite compatibility while changing the public
  event shape.

### I1 — Skill preflight protects only direct Runs; Workflow/child Runs bypass it

Evidence:

- The new preflight is private to `RunAPI.start`
  (`src/agent_sdk/api.py:518-567`).
- `WorkflowExecutor` resolves an `AgentSpec`, builds its descriptor, and calls
  `RuntimeCommands.start_run` directly
  (`src/agent_sdk/workflow/executor.py:580-640`, `:830-868`).
- `SubagentService.spawn` likewise calls `RuntimeCommands.start_run` directly
  (`src/agent_sdk/subagents/service.py:53-106`).
- Neither path receives the initialized `SkillRegistry` or activates configured
  Skills before durable writes/model execution.
- A fresh one-node Workflow reproduction registered
  `skills=("missing-skill",)` and produced:

  ```text
  provider_calls= 1
  run_created_count= 1
  workflow_started_count= 1
  ```

Impact:

The same Agent specification has different validity depending on which public
execution path starts it. A missing or changed Skill is correctly rejected
before persistence for `sdk.runs.start`, but is silently ignored by Workflow
and child execution, allowing a persisted Run and a provider call. This
violates the explicit Task 3 fail-before-Run/model contract and will make Task
4 prompt behavior path-dependent.

Required direction:

- Centralize configured-Skill activation/preflight and use it for direct,
  Workflow, and child Run creation.
- Workflow start must validate every referenced Agent before its node Run can
  be persisted or its provider called; child spawn must validate before
  `start_run`.
- Add direct/Workflow/child tests proving missing and invalidated Skills yield
  normalized non-retryable `invalid_state`, zero provider calls, and no
  `run.created` event.

## Confirmed conforming behavior

- `AgentSpec` and `DurableAgentSpec` have the planned defaults, profile type,
  ordered unique nonempty Skill validation, and `ContextRuntimeConfig`.
- New prompt/Skill/context fields participate in `agent_hash` and
  `descriptor_hash`.
- Legacy descriptors missing the new fields are accepted only after their raw
  legacy agent and descriptor hashes validate, then upgraded to declared
  defaults and canonically rehashed. Invalid/noncanonical values remain
  rejected.
- Moving pure Context runtime configuration to
  `agent_sdk.context_runtime` avoids the runtime/context import cycle. The
  complete Context regression gate remained green.
- `AgentSDKConfig.skill_roots` round-trips; SDK initialization constructs and
  discovers one registry and exposes it as `sdk.skills`.
- For the direct Run path, missing Skill activation occurs before
  `RuntimeCommands.start_run`, creates no Run, and invokes no provider.
- `PromptComposer` orders general, coding, application, then Skill layers;
  preserves Skill order; rejects duplicate Skill names; uses Skill
  `content_hash` as the layer version; hashes instruction text; canonicalizes
  Tool-schema hashing; and returns frozen prompt messages without mutating
  inputs.
- `PromptManifest` has a generated `manifest_id`. Persistence atomically writes
  the full manifest snapshot and the minimal manifest event, with Session and
  same-Session Context View preconditions.
- SQLite recognizes the new snapshot kind, requires the referenced Context
  View to belong to the same Session, survives reopen, and existing
  Session-scoped deletion removes its event/snapshot data.
- Public root exports for `ContextRuntimeConfig` and
  `PromptManifestPersistence` are present.
- Task 3 does not prematurely wire Context preparation into `RunEngine`; that
  remains Task 4 scope.
- The implementation report's text-loop concern is not caused by this diff:
  the reviewed range does not change the existing built-in Tool default or the
  legacy provider request construction.

## Fresh verification

```text
pytest tests/integration/prompts
       tests/unit/runtime/test_execution_descriptors.py
       tests/unit/test_core_config.py
       tests/integration/skills/test_skill_slice.py
       tests/integration/test_sdk_sqlite_test_constructor.py
89 passed, 1 skipped in 4.81s

pytest tests/unit/context tests/integration/context
102 passed in 3.75s

ruff check src/agent_sdk tests/integration/prompts
           tests/unit/runtime/test_execution_descriptors.py
           tests/unit/test_core_config.py
All checks passed!

mypy --strict src/agent_sdk/context src/agent_sdk/context_runtime.py
              src/agent_sdk/runtime/models.py
              src/agent_sdk/runtime/execution.py src/agent_sdk/prompts
              src/agent_sdk/config.py src/agent_sdk/api.py
              src/agent_sdk/storage/sqlite.py
Success: no issues found in 22 source files

git diff --check 794739f..f80a956
clean
```

The skipped test is the existing package-build check when `uv` is unavailable.
A wider runtime run was not used as passing evidence because the controller
reported it exceeded its 120-second environment window; it had emitted no
failure before termination.
