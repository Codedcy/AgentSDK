# R1 Task 3 Implementation Report

## Scope

Implemented only R1 Task 3: ordinary registered `read`, `write`, and `bash`
built-ins, SDK initialization wiring, integration coverage, and the v0.1
acceptance extension. No R1 checkpoint or later release slice was started.

## TDD Evidence

Initial RED:

```text
ERROR tests/integration/tools/test_builtin_tools.py
ModuleNotFoundError: No module named 'agent_sdk.tools.builtins.registration'
```

The first implementation exposed a package-cycle collection error. The root
cause was an eager package-level registration import re-entering
`runtime.models` through `tools.models`. Registration is now lazy from
`agent_sdk.tools` and directly imported only at SDK initialization.

An additional output-envelope regression was written and observed RED:

```text
FAILED test_default_builtin_limit_still_fits_the_durable_tool_result
assert ToolResultStatus.FAILED is ToolResultStatus.SUCCEEDED
```

The handlers now retain at most a JSON-safe durable preview inside the existing
16 KiB `ToolResult` envelope, while honoring any lower configured output limit.

Final focused GREEN:

```text
.............................................                            [100%]
45 passed in 6.18s
```

The count above is the Task 3 integration/acceptance set after adding the
durable-envelope regression.

## Implemented Behavior

- `read`
  - loads Session workspace roots from the Store;
  - resolves through the shared containment helper;
  - reads only a bounded binary prefix (`limit + 1`) and decodes with UTF-8
    replacement;
  - returns only a workspace-relative display path.
- `write`
  - resolves through the shared containment helper;
  - writes a uniquely owned same-directory temporary file, flushes and fsyncs
    it, then atomically replaces the target;
  - respects `overwrite=False` and removes only its own temporary file on
    failure.
- `bash`
  - accepts only argv and uses `asyncio.create_subprocess_exec`;
  - rejects empty argv/NUL arguments and never invokes a shell;
  - resolves cwd inside the Session workspace;
  - continuously drains stdout and stderr through one shared byte budget;
  - kills and waits for timed-out/cancelled processes;
  - returns exit code, bounded stdout/stderr, and `truncated`.
- All three are normal `ToolSpec` registrations and therefore reuse
  `ToolExecutor`, `PolicyEngine`, `PermissionBroker`, events, and durable Tool
  results.
- Production and test SDK construction enable built-ins by default. Only test
  factories with exact registry/descriptor assertions opt out.
- Application-registered tools continue to share the same registry; MCP code
  was not changed.

## Acceptance Coverage

The v0.1 scripted model now performs, in order:

1. an allowed workspace write;
2. an ask-gated workspace read resolved through the public Permission API;
3. an allowed argv-based bash call;
4. an outside-workspace write that is denied under global allow.

The test asserts public permission/tool timeline events, Tool results, created
workspace content, durable reopen behavior, Session deletion, and that the
outside fixture remains untouched.

## Files

Production:

- `src/agent_sdk/tools/builtins/files.py`
- `src/agent_sdk/tools/builtins/bash.py`
- `src/agent_sdk/tools/builtins/registration.py`
- `src/agent_sdk/tools/__init__.py`
- `src/agent_sdk/config.py`
- `src/agent_sdk/api.py`

Tests:

- `tests/integration/tools/test_builtin_tools.py`
- `tests/integration/tools/test_permissioned_tool_slice.py`
- `tests/integration/workflow/test_workflow_session_ownership.py`
- `tests/e2e/test_v01_release.py`
- `tests/e2e/test_session_lifecycle_idempotency.py`
- `tests/fixtures/v01_runtime.py`

## Verification

Required Task 3 set (before the final added envelope test):

```text
44 passed in 5.87s
```

Exact descriptor regression tests:

```text
2 passed in 3.36s
```

Static checks:

```text
All checks passed!
Success: no issues found in 13 source files
```

## Known Boundary

This is workspace containment and command policy, not an OS sandbox. A process
allowed by policy can use its own capabilities after launch. That is the
explicit R1/v0.1 boundary; no broader sandbox was added.

## Review Fix

The Task 3 review found one canonical permission-resource mismatch, one
no-clobber race, and one empty-workspace normalization gap. This follow-up stays
inside Task 3 and does not start the R1 checkpoint.

### Review RED

The focused regression set first produced eight expected failures:

```text
8 failed, 15 passed in 5.08s
```

- relative `read`, relative `write`, relative `bash.cwd`, and omitted
  `bash.cwd` all incorrectly succeeded under an absolute workspace deny rule;
- a path-specific ask rule never produced a request;
- a path-specific allow rule incorrectly denied;
- the controlled concurrent creator was overwritten by `overwrite=False`;
- empty-workspace bash returned `failed` instead of `denied`.

The existing empty/NUL argv, child cancellation, and post-temp cleanup behavior
was also locked with focused regressions.

### Review Implementation

- `RegisteredTool` now has an optional private asynchronous
  `permission_arguments` resolver. It is not part of `ToolSpec`, model schemas,
  or durable execution descriptors.
- `ToolExecutor` invokes that resolver after schema validation but before the
  existing `PermissionBroker`. The resulting canonical arguments are used only
  for `PermissionRequest`; handler invocation retains the original validated
  arguments.
- Built-in file resolvers canonicalize relative `path` against the Session
  workspace. The bash resolver canonicalizes explicit cwd and injects the
  canonical first workspace root when cwd is omitted.
- Containment failure during permission normalization completes as a bounded,
  non-leaking `DENIED` result before authorization or handler-start events.
- An application Tool without a resolver retains its original request and
  handler arguments; MCP/descriptor behavior remains unchanged.
- `overwrite=False` now installs the closed, fsynced, same-directory owned
  temporary through atomic hard-link creation. A destination that appears
  concurrently causes `FileExistsError` and is preserved. `overwrite=True`
  continues to use atomic replace.
- Empty-workspace bash reaches the shared containment denial path.

### Review GREEN

Task 3, application permission, and v0.1 acceptance set:

```text
58 passed in 6.12s
```

MCP, execution descriptor, provider recovery, exact descriptor, and related
access-denial recovery set:

```text
74 passed in 5.77s
```

Static checks:

```text
All checks passed!
Success: no issues found in 17 source files
```
