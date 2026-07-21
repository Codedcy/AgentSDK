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
