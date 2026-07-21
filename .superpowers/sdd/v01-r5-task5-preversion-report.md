# v0.1 R5 Task 5 Pre-Version Gate Report

Date: 2026-07-22

Baseline: `c8b4110`

Verified HEAD: `b422985`

Package version: `0.1.0.dev0` (unchanged)

## Result

Task 5 Steps 1-3 pass at the pre-version stop point. Python 3.13 full tests,
whole-repository Ruff, strict package mypy, the Python 3.12 critical set, dev
sdist/wheel build, clean Python 3.12 wheel import, and installed-wheel reference
smoke all pass. No version bump, tag, or publication was performed.

## Initial Python 3.13 Evidence

The reproduced baseline gate collected 2,889 tests:

- 2,744 passed;
- 139 failed;
- 6 skipped;
- duration: 498.259 seconds;
- JUnit: `.superpowers/sdd/v01-r5-task5-py313-full-repro.xml`.

Raw failures were retained and classified before fixes. The final recovery
cluster passed 664 tests, and the exact 41 non-recovery nodeids remaining from
the original 139 failures passed before the final full run:

- recovery JUnit: `.superpowers/sdd/v01-r5-task5-recovery-cluster-green.xml`;
- non-recovery JUnit: `.superpowers/sdd/v01-r5-task5-nonrecovery-41-green.xml`;
- non-recovery result: 41 passed in 39.56 seconds.

## Blockers and Dispositions

1. `af8fe07 fix: preserve durable recovery evidence`
   - Root cause: v2 events were normalized for v1 validation and the normalized
     copies were incorrectly reused for exact durable precondition comparison.
   - Fix: keep raw durable events/cursors separate from the validation view.
   - Added regression:
     `test_recovery_precondition_keeps_raw_v2_events_separate_from_validation_view`.

2. `e01fee5 fix: resume recovered providers from base request`
   - Root cause: the prepared provider request, including default prompt layers,
     was passed into descriptor validation where the durable base request was
     required.
   - Fix: validate/recover from the base request while the adapter still receives
     the prepared provider request.

3. `f6a436f fix: preserve legacy recovery stage events`
   - Root cause: uncorrelated legacy recovery emitted incomplete v2 stage events.
   - Fix: emit legal v1 fallback stage events when no reliable step correlation
     exists; correlated paths retain v2.

4. `9d8b582 test: expect default prompt during recovery`
   - Updated stale recovery assertions to require the exact packaged general
     system prompt followed by the original checkpoint/tool messages.

5. `c8574cf fix: keep failed run events recoverable`
   - Root cause: model-only evidence fields polluted strict `step.failed` and
     `run.failed` payloads and prevented terminal certification.
   - Fix: retain evidence on the model event and keep step/run terminal schemas
     minimal and valid.

6. `b9dfb13 test: preserve recovery corruption coverage`
   - Replaced invalid `model_copy` fingerprint injection with a legal prepared
     request mutation, recomputed fingerprint, strict model validation, and real
     memory/SQLite persistence. Recovery still rejects the resulting mismatch
     against historical evidence. No production invariant was weakened.

7. `fcbef9c test: assert default prompt message boundaries`
   - Default prompt tests now compare the exact packaged `general/system.md`,
     then verify the original user/assistant/tool order and content.
   - The short reference history explicitly verifies the correct L3-to-L2
     lossless fallback when all sources are protected/recent and no summary is
     needed.

8. `abeee4f test: preserve lifecycle idempotency coverage` and
   `3d9b314 test: distinguish mailbox bootstrap idempotency`
   - The lifecycle scenario now seeds enough public history to exercise a real
     L3 capsule.
   - Idempotency assertions distinguish application keys from the two durable
     mailbox bootstrap keys per run; the retained deleting session contained
     exactly the expected records and final deletion removed all of them.

9. `3528af1 fix: validate run idempotency before storage`
   - Root cause: `RunAPI.start` loaded the session/tool/workspace state before the
     command layer validated the idempotency key.
   - Fix: a shared sanitizer-safe validation function now runs at the public API
     boundary and is reused by the command layer. Invalid keys are rejected
     before any Store access.

10. `6fd4d14 test: align detached replay capabilities`
    - The manual durable descriptor now matches the public API's empty workspace
      scope and disabled builtin-tool catalog. This preserves the intended
      detached recovery assertion instead of correctly triggering a capability
      fingerprint conflict.

11. `c5a5a31 test: allow durable tool limit processing`
    - The ninth tool-call test's call phase takes about 2.17 seconds through nine
      context/prompt/durable-event rounds. Its timeout changed from 1 to 5 seconds.
      All semantic assertions remain: 9 model calls, 8 handler calls, 8 tool-start
      events, and terminal `step.failed`/`run.failed` with `tool step limit exceeded`.

12. `b422985 fix: mark package as typed`
    - Exact `mypy --strict` initially failed because the package lacked a
      `py.typed` marker, while explicit-path strict mypy was otherwise clean.
    - Added `src/agent_sdk/py.typed`; exact strict package mypy now passes.

## Python 3.13 Final Gate

- Full pytest: 2,884 passed, 6 skipped, 0 failed, 0 errors in 495.16 seconds.
- Collected: 2,890 tests (the one-test increase is the raw-v2 separation
  regression listed above).
- JUnit: `.superpowers/sdd/v01-r5-task5-py313-full-green.xml`.
- Ruff: `python -m ruff check .` -> `All checks passed!`.
- Strict mypy: `python -m mypy --strict` -> 105 source files, no issues.
- Build: `python -m build` successfully built the dev sdist and wheel.

### Expected skips

The six skips are environment/platform-specific and are not release acceptance
nodes:

1. `tests/integration/prompts/test_prompt_slice.py::test_wheel_and_sdist_include_both_markdown_profiles`
   - Reason: `uv` executable unavailable.
   - Covered by the required real `python -m build`, archive inspection, clean
     install, and installed-wheel smoke below.
2. `tests/unit/runtime/test_session_workspace_roots.py::test_run_workspace_scope_rejects_a_symlink_redirected_outside`
   - Reason: Windows denied symlink creation without the required privilege.
3. `tests/unit/runtime/test_session_workspace_roots.py::test_final_workspace_resolution_rechecks_scope_after_redirect`
   - Reason: Windows denied symlink creation without the required privilege.
4. `tests/unit/tools/test_workspace_paths.py::test_resolve_workspace_path_rejects_existing_symlink_escape`
   - Reason: Windows denied symlink creation without the required privilege.
5. `tests/unit/tools/test_workspace_paths.py::test_posix_trailing_dot_and_space_names_remain_valid[trailing.]`
   - Reason: POSIX filename semantics only.
6. `tests/unit/tools/test_workspace_paths.py::test_posix_trailing_dot_and_space_names_remain_valid[trailing ]`
   - Reason: POSIX filename semantics only.

## Dev Build Evidence

Artifacts:

- `dist/agent_sdk-0.1.0.dev0-py3-none-any.whl`
  - SHA256: `1330DFBAD0E70D31548DE05ECF48514F81910E36EB4770302C6699C94042401F`
- `dist/agent_sdk-0.1.0.dev0.tar.gz`
  - SHA256: `F66A3BE83F3689B30E58BD50621B1BE15658467BFD52C1B6968B88216DC5DD58`

Wheel metadata is `agent-sdk 0.1.0.dev0`, `Requires-Python: >=3.12,<3.14`,
with the declared LiteLLM, Pydantic, SQLite, MCP, YAML, and JSON Schema runtime
dependencies. Both archives contain:

- `agent_sdk/py.typed`;
- `agent_sdk/prompts/profiles/general/system.md`;
- `agent_sdk/prompts/profiles/coding/system.md`.

## Python 3.12 Critical Gate

The exact Task 5 Step 2 set ran under official CPython 3.12.10:

- `tests/e2e/test_v01_release.py`;
- `tests/e2e/test_v01_reference_example.py`;
- `tests/integration/context/test_context_recovery.py`;
- `tests/integration/workflow/test_control_recovery.py`;
- `tests/integration/subagents/test_child_coordinator.py`.

Result: 78 passed in 60.10 seconds, 0 skipped.

JUnit: `.superpowers/sdd/v01-r5-task5-py312-critical.xml`.

The pre-existing signed Python installation had been cleaned from its registered
temporary target and the uv-managed runtime was blocked from loading compiled
extensions by Windows Application Control. Running the official python.org
installer's repair mode restored the signed CPython 3.12.10 runtime. The gate
then ran in a dedicated source venv with `PYTHONPATH=src`.

## Clean Installed-Wheel Evidence

A second fresh CPython 3.12.10 venv installed only the dev wheel and resolved
runtime dependencies. `PYTHONPATH` was cleared for all installed-wheel checks.

- Imported path:
  `C:\Users\10176\AppData\Local\Temp\agent-sdk-python312-gate\wheel-venv\Lib\site-packages\agent_sdk\__init__.py`
- `importlib.metadata.version("agent-sdk")`: `0.1.0.dev0`
- Python: `3.12.10`
- Source tree on `sys.path`: false
- Installed `py.typed` and both packaged prompt profiles: present

Installed-wheel reference smoke output:

```json
{"attribution_method":"deterministic_event_evidence_v1","child_status":"completed","context_levels":["L0","L1","L2","L3","L4"],"evaluation_verdict":"pass","run_status":"completed","trace_stage_count":1,"workflow_status":"completed"}
```

## Stop Point

Steps 1-3 are complete. The mandatory whole-v0.1 review and any resulting
written-contract blockers must be handled before changing package metadata to
`0.1.0`. The version remains `0.1.0.dev0`; no tag or publication was created.
