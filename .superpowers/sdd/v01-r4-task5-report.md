# v0.1 R4 Task 5 Checkpoint Report

Date: 2026-07-21
Baseline HEAD: `af27bf3`

## Scope

This checkpoint records R4 only. No production code was changed. The checkpoint
follow-up updated the release-ledger contract test so it recognizes R4 as
completed with the explicitly retained recovery debt and resumes at R5 Task 1.
R4 delivered durable Child Tool/workspace capability intersection and catalog
selection; direct parent/Child mailbox delivery with atomic Context consumption;
bounded Child coordination and the public `sdk.children` facade; and the four
ordinary Tool-pipeline controls: `spawn_agent`, `send_message`, `wait_child`,
and `list_children`.

Tasks 1-4 were independently approved, with final review outcomes of Critical 0
/ Important 0 / Minor 0, Spec approved, and Quality approved. Their commits are
`23c2c39`, `5466757`, `fd6d728`, `f320642`, `ae6aa89`, `1326fb8`, `b90c8f7`,
`3e71c9a`, and `c1e570b`.

## Fresh checkpoint evidence

Commands used `.venv`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and explicit
`pytest_asyncio.plugin`.

| Check | Result |
|---|---|
| Raw R4 aggregate: unit/integration subagents, integration context, v0.1 E2E | **Not PASS:** 198 passed, 1 failed in 14.05s |
| Exact-node-deselected R4 clean gate | 198 passed, 1 deselected in 13.33s |
| Ruff planned scope | All checks passed |
| Strict mypy planned scope | Success: 31 source files |
| Strict mypy all `src/agent_sdk` | Success: 97 source files |
| Release-ledger contract | 3 passed in 0.04s; Ruff clean |

The raw gate's sole failure is
`tests/integration/context/test_context_recovery.py::test_authoritative_recovery_receives_exact_stored_prepared_request`,
which raises `AgentSDKError: recovery required`. This is known pre-R4 recovery
debt, proven at R4 Tasks 2 and 4 with the same failure shape. It was neither
modified nor repaired for this checkpoint. Thus the deselected clean gate is a
bounded R4 verification result, not a claim that the raw checkpoint is green.

## Resume

R4 recovery command:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\subagents tests\integration\subagents tests\integration\context tests\e2e\test_v01_release.py -q --deselect=tests/integration/context/test_context_recovery.py::test_authoritative_recovery_receives_exact_stored_prepared_request
```

R5 begins with the expected RED command:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\observability\test_stage_projection.py -q
```

`tests/unit/observability/test_stage_projection.py` does not yet exist because
R5 Task 1 creates it. Its absence is expected RED work, not an existing checkpoint
failure.
