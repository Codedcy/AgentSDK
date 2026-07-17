# R1 Task 2 Implementation Report

## Scope

Implemented only **R1 Task 2: Workspace Containment**:

- one shared workspace path resolver;
- private access-denied and handler-timeout execution markers;
- normalized `ToolExecutor` status mapping for those markers.

This task does not add `read`, `write`, or `bash` handlers, tool registration,
SDK default enablement, an OS sandbox, or any R1 Task 3/4 behavior.

## TDD Evidence

Initial RED command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\tools\test_workspace_paths.py -q
```

Initial RED result:

```text
ModuleNotFoundError: No module named 'agent_sdk.tools.builtins'
1 error in 3.19s
```

Initial GREEN result:

```text
18 passed, 1 skipped in 2.84s
```

Self-review found an additional dangling-junction escape case. Regression RED:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\tools\test_workspace_paths.py::test_resolve_workspace_path_rejects_dangling_junction_escape -q
```

```text
FAILED test_resolve_workspace_path_rejects_dangling_junction_escape
Failed: DID NOT RAISE ToolAccessDenied
1 failed in 2.89s
```

After the minimal link-aware existing-parent fix:

```text
1 passed in 2.85s
19 passed, 1 skipped in 3.16s
```

## Implementation

- Resolves configured roots strictly before evaluating candidates.
- Accepts contained relative and absolute paths and searches multiple roots.
- Uses canonical path containment rather than string-prefix matching.
- Resolves the nearest existing parent for write targets with nonexistent
  suffixes.
- Rejects parent traversal, path-boundary escapes, Windows alternate data
  streams, NUL/empty/dot inputs, and unavailable read targets.
- Rejects existing and dangling symlink/junction escapes. The resolver treats
  link objects as existing even when their targets are missing, so it never
  appends an unresolved link as an ordinary write-path component.
- Maps `ToolAccessDenied` to `denied` and `ToolExecutionTimedOut` to
  `timed_out`, with fixed bounded error messages that do not expose exception
  types or external filesystem paths.

## Changed Files

- `src/agent_sdk/tools/builtins/__init__.py`
- `src/agent_sdk/tools/builtins/workspace.py`
- `src/agent_sdk/tools/errors.py`
- `src/agent_sdk/tools/executor.py`
- `tests/unit/tools/test_workspace_paths.py`

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\tools\test_workspace_paths.py tests\unit\test_tool_retry_policy.py tests\integration\tools\test_permissioned_tool_slice.py -q
```

```text
60 passed, 1 skipped in 4.62s
```

```text
mypy --strict on all changed production modules and the focused test:
Success: no issues found in 4 source files

ruff check on all changed production and test files:
All checks passed!

git diff --check:
Exit code 0 (line-ending notice only)
```

## Concerns

- No blocking concerns.
- The ordinary directory-symlink test was skipped because this Windows
  environment refused symlink creation, as permitted by the task. Windows
  existing-junction and dangling-junction cases both ran and passed.
- This is path/cwd containment only; it intentionally does not claim hardened
  race-free OS sandboxing.
