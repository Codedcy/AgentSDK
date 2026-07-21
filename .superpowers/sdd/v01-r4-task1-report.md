# v0.1 R4 Task 1 — Select and Persist Effective Run Capabilities

## RED

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/runtime/test_capability_intersection.py -q
```

Observed expected collection failure: `intersect_names` and
`intersect_workspaces` did not exist.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/integration/runtime/test_run_tool_catalog.py -q
```

Observed expected failures: `AgentSpec` rejected the missing
`tool_allowlist` and `workspace_allowlist` fields. The workspace widening
case also failed before the intersection was corrected.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/runtime/test_session_workspace_roots.py -q
```

Observed expected failure: a missing `run_id` fell back to Session roots.

## GREEN / verification

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/runtime/test_capability_intersection.py tests/integration/runtime/test_run_tool_catalog.py tests/unit/runtime/test_execution_descriptors.py tests/unit/runtime/test_session_workspace_roots.py tests/integration/runtime/test_builtin_tool_recovery.py tests/integration/runtime/test_live_run_progress.py tests/integration/subagents/test_child_run_slice.py -q
```

Result: `96 passed in 5.18s`.

```powershell
.\.venv\Scripts\python.exe -m mypy --strict src/agent_sdk/tools/registry.py src/agent_sdk/runtime/models.py src/agent_sdk/runtime/execution.py
```

Result: `Success: no issues found in 3 source files`.

```powershell
.\.venv\Scripts\python.exe -m ruff check [changed sources and Task 1 tests]
git diff --check
```

Result: Ruff passed; diff check passed.

Final Task 1 gate (focused runtime plus builtin workspace regressions):

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/runtime/test_capability_intersection.py tests/integration/runtime/test_run_tool_catalog.py tests/unit/runtime/test_execution_descriptors.py tests/unit/runtime/test_session_workspace_roots.py tests/integration/runtime/test_builtin_tool_recovery.py tests/integration/runtime/test_live_run_progress.py tests/integration/subagents/test_child_run_slice.py tests/integration/tools/test_builtin_tools.py tests/unit/tools/test_workspace_paths.py -q
```

Result: `146 passed, 3 skipped in 6.18s`.

The additional builtin/workspace regression command was:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/integration/tools/test_builtin_tools.py tests/unit/tools/test_workspace_paths.py tests/integration/tools/test_permissioned_tool_slice.py -q
```

Result: `80 passed, 3 skipped, 3 failed`.

The Task 1-relevant parts are green: `test_builtin_tools.py` and
`test_workspace_paths.py` passed, and the builtin recovery test above passed.
The exact unrelated failures are:

- `test_tool_waits_for_permission_then_runs_second_model_step`: expected the
  second provider request to begin with the user message; actual request begins
  with ContextMiddleware's generated `system` prompt.
- `test_two_sequential_model_tool_calls_complete_in_order`: expected roles
  `user, assistant, tool, assistant, tool`; actual roles include the same
  leading `system` prompt.
- `test_ninth_sequential_tool_call_fails_before_handler`: expected the durable
  failure inside one second; `asyncio.wait_for(..., timeout=1)` expired.

Task 1 does not change `context/`, prompt composition, or the tool-step state
machine. Its only test-harness adjustment persists a same-Session legacy Run
for builtin path handlers, matching the new fail-closed production rule; it
does not affect these application-tool/context assertions. These three failures
are therefore reported as pre-existing/out-of-scope, not as a green suite.

## Files changed

- `src/agent_sdk/runtime/models.py`
- `src/agent_sdk/runtime/execution.py`
- `src/agent_sdk/runtime/engine.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/api.py`
- `src/agent_sdk/subagents/models.py`
- `src/agent_sdk/tools/registry.py`
- `src/agent_sdk/tools/executor.py`
- `src/agent_sdk/tools/builtins/files.py`
- `src/agent_sdk/tools/builtins/bash.py`
- `src/agent_sdk/tools/builtins/workspace.py`
- `tests/unit/runtime/test_capability_intersection.py`
- `tests/integration/runtime/test_run_tool_catalog.py`
- `tests/unit/runtime/test_session_workspace_roots.py`
- `tests/integration/runtime/test_builtin_tool_recovery.py`
- `tests/integration/tools/test_builtin_tools.py`

## Self-review

- `None` remains inherit/not-further-narrowed; explicit empty tuples persist
  and deny tools/workspaces.
- Every new primary Run stores its canonical effective Tool catalog and an
  explicit workspace scope tuple.
- RunEngine, recovery, and ToolExecutor share the descriptor-selected catalog.
  Later unrelated Tool registration is covered for both execution and recovery.
- Builtin filesystem/process paths use current descriptor scopes. When a
  `run_id` is supplied, missing, malformed, or cross-Session Runs fail closed;
  only legacy `workspace_scopes=None` falls back to Session roots.
- The descriptor's `workspace_scopes=None` sentinel is retained strictly for
  legacy durable data.

## Attention points

- The three unrelated context/prompt timing failures described above remain
  outside Task 1 scope and are not hidden by production fallbacks.
- Child/workflow envelope fields now preserve inherit-vs-empty semantics;
  Task 1 does not expand into later workflow capability work.

## Post-review hardening (C0 / I2 / M1)

Reviewer conclusion: legacy schema-v2 event hashes needed a narrowly-scoped
pre-R4 projection, and run-scoped workspace authorization needed storage-owner
authentication plus current filesystem containment checks.

### RED

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/runtime/test_execution_descriptors.py::test_schema_v2_event_authenticates_a_genuine_pre_r4_descriptor_projection tests/unit/runtime/test_session_workspace_roots.py -q
```

Observed: a genuine R3-era schema-v2 immutable `run.created` payload did not
match its upgraded descriptor; a same-data Run under a wrong storage owner was
accepted. The new outside/symlink-scope tests established the missing
containment requirement. (The symlink test is skipped only when Windows link
creation is unavailable.)

### GREEN

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/runtime/test_execution_descriptors.py tests/unit/runtime/test_session_workspace_roots.py -q
```

Result: `35 passed, 1 skipped in 3.18s`.

The schema-v2 matcher first requires the strict current projection. Only an
upgraded descriptor whose three R4 fields are all legacy `None` is eligible for
the reconstructed pre-R4 hash projection; corrupted old hashes and every new
restricted descriptor remain rejected. Schema-v1 behavior is unchanged.

`workspace_roots(run_id=...)` now authenticates the original raw Session and
Run snapshot data with exact no-op preconditions, fails closed on owner/data
conflicts, re-resolves Session roots and descriptor scopes through the existing
symlink-aware canonical path logic, and requires each explicit scope to be a
current descendant of a canonical Session root. Explicit empty remains empty;
only an authenticated legacy `None` descriptor scope inherits Session roots.

Final post-review gate:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/unit/runtime/test_capability_intersection.py tests/integration/runtime/test_run_tool_catalog.py tests/unit/runtime/test_execution_descriptors.py tests/unit/runtime/test_session_workspace_roots.py tests/integration/runtime/test_builtin_tool_recovery.py tests/integration/runtime/test_live_run_progress.py tests/integration/subagents/test_child_run_slice.py tests/integration/workflow/test_workflow_child_slice.py tests/integration/tools/test_builtin_tools.py tests/unit/tools/test_workspace_paths.py -q
.\.venv\Scripts\python.exe -m mypy --strict src/agent_sdk
.\.venv\Scripts\python.exe -m ruff check src/agent_sdk/runtime/models.py src/agent_sdk/tools/builtins/files.py tests/unit/runtime/test_execution_descriptors.py tests/unit/runtime/test_session_workspace_roots.py
git diff --check
```

Result: `159 passed, 4 skipped in 6.40s`; strict mypy (93 source files),
Ruff, and diff-check passed.
