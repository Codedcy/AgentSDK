# v0.1 R3 Task 4 Review Fix Report

## Scope

This change fixes both Important findings from
`v01-r3-task4-review.md` (review commit `4d0bb5b`) without changing release or
progress documents:

- I1: strictly validate every durable prepared provider message and Tool schema.
- I2: authenticate durable Context View and Prompt Manifest references before
  recovery trusts or executes a prepared model request.

It also closes the prepare-to-start race by requiring both prepared snapshots
to exist and belong to the Run Session in the same commit that records
`model.call.started`.

## I1 - Closed prepared-request protocol

### RED

The new parameterized negative test exercised 17 malformed request cases:

- missing, empty, or invalid message roles;
- role-specific missing fields;
- invalid Tool-result messages;
- empty or malformed assistant Tool calls;
- unknown Tool-call fields;
- empty/malformed provider Tool schemas;
- non-JSON nested values.

Before the fix, the focused run produced:

```text
16 failed, 2 passed, 47 deselected
```

The already-existing recursive JSON freeze rejected one nested case, and the
positive runtime-shape case passed. The other 16 malformed protocol shapes
were accepted.

### GREEN

`_ModelRequestPayload` now applies closed, role-specific validators:

- roles are limited to `system`, `user`, `assistant`, and `tool`;
- every role has an exact allowed field set;
- Tool-result messages require a non-empty `tool_call_id`;
- assistant Tool calls have the exact provider function-call shape;
- provider Tool entries have the exact `{"type": "function", "function": ...}`
  envelope and require a non-empty name plus mapping parameters;
- the existing recursive freeze still rejects non-string keys, non-finite
  numbers, and other non-JSON values.

The positive test covers every message shape emitted by the runtime, optional
message names, assistant function calls, Tool results, and the provider Tool
schema. The focused result after the fix was:

```text
18 passed, 47 deselected
```

Legacy operations remain loadable when `prepared_request`,
`context_view_id`, and `prompt_manifest_id` are all absent.

## I2 - Authenticated Context View and Prompt Manifest references

### RED

The corruption matrix covers both Memory and SQLite backends for:

- missing Context View;
- missing Prompt Manifest;
- cross-Session Context View ownership;
- cross-Session Prompt Manifest ownership;
- mismatched Context View identity;
- mismatched Prompt Manifest identity;
- Manifest linked to a different Context View.

Before reference authentication, all 14 cases failed because recovery did not
raise:

```text
14 failed
```

A separate completed-model crash test proved that recovery did not read the
old completed model's Context View or Prompt Manifest.

The completed-model/Tool-in-flight corruption variant then exposed a second
boundary: the compatibility path converted invalid old references into a
reconciliation plan instead of failing closed:

```text
1 failed, 1 passed
```

### GREEN

For every `ModelCallOperation` with a prepared request, recovery now:

1. verifies the operation belongs to the recovered Run and Session;
2. loads the referenced Context View and Prompt Manifest;
3. validates their durable model shapes;
4. verifies View identity and Session identity;
5. verifies Manifest identity, its View link, and its provider model identity;
6. executes a no-write atomic commit with exact snapshot-data and Session-owner
   preconditions.

The exact-data preconditions detect a replacement between the snapshot reads
and the atomic authentication check. Existing closed-world model-event
certification also requires the public `model.call.started` payload to carry
the same authenticated pair. Authentication occurs:

- at the recovery planning boundary, before compatibility fallback;
- when reconstructing a prepared request;
- when validating pending reconciliation requests.

Provider and Tool side-effect counters remain zero for every rejected
corruption. The Memory/SQLite matrix plus completed-model positive recovery
passed:

```text
15 passed
```

After moving authentication ahead of the Tool-in-flight compatibility path,
the focused positive/corruption pair passed:

```text
2 passed
```

### Completed-model crash/recovery evidence

The positive recovery test completes the first model call, crashes while its
safe-retry Tool is in flight, scans the interrupted Run, and recovers it. It
asserts:

- the completed model operation id and its two reference ids remain unchanged;
- recovery reads and authenticates the old View and Manifest;
- the recovered Tool executes exactly once;
- the following new model call executes exactly once;
- the following call receives a different View and Manifest;
- durable event counts move from exactly one View/Manifest pair to exactly two.

Thus recovery creates no duplicate evidence for the completed call and exactly
one fresh pair for the subsequent call.

## Prepare-to-start race

### RED

A middleware test deletes the just-created Context View immediately after the
Prompt Manifest commit. Before the fix, model start proceeded:

```text
Failed: DID NOT RAISE
```

### GREEN

`start_model` now includes Session-owned `SnapshotPrecondition`s for the
prepared Context View and Prompt Manifest in the same progress commit that
records the model operation and public start event. The provider is not called,
and neither `model.call.started` nor a model operation is persisted when either
snapshot is missing. The combined focused suite passed:

```text
85 passed
```

## Fresh verification

All commands were run from
`D:\code\AgentSDK\.worktrees\agent-sdk-implementation`.

### Task 4, Context, Prompt, and release E2E

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin `
  tests\unit\context `
  tests\unit\runtime\test_reconciliation_models.py `
  tests\integration\context `
  tests\integration\prompts `
  tests\e2e\test_v01_release.py -q
```

```text
221 passed, 1 skipped in 23.19s
```

### Provider, Tool, text-loop, and recovery regression

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin `
  tests\unit\runtime\test_provider_recovery.py `
  tests\integration\runtime\test_text_agent_loop.py `
  tests\integration\runtime\test_provider_recovery_execution.py `
  tests\integration\runtime\test_tool_recovery_execution.py -q
```

```text
294 passed in 73.92s
```

### Task 3 security and legacy compatibility

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin `
  tests\integration\prompts\test_runtime_prompt.py `
  tests\unit\runtime\test_execution_descriptors.py -q
```

```text
48 passed in 14.08s
```

### Workflow and subagent smoke

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin `
  tests\integration\subagents\test_child_run_slice.py `
  tests\integration\workflow\test_control_child_parent.py `
  tests\integration\workflow\test_workflow_child_slice.py `
  tests\integration\workflow\test_workflow_recovery.py -q
```

```text
38 passed in 15.64s
```

### Static quality

```powershell
.\.venv\Scripts\python.exe -m ruff check src `
  tests\unit\runtime\test_reconciliation_models.py `
  tests\integration\context\test_context_recovery.py `
  tests\integration\context\test_runtime_middleware.py
```

```text
All checks passed!
```

```powershell
.\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk
```

```text
Success: no issues found in 93 source files
```

```powershell
git diff --check
```

```text
clean
```

## Remaining project-level concern

The independent review documented a pre-existing built-in-Tool capability
mismatch in `tests/integration/runtime/test_recovery_api.py`. This Task 4 fix
does not modify that capability gate and does not present the repository-wide
suite as fully green. It remains release-suite debt outside this review fix.
