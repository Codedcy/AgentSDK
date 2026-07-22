# M02-T002 Phase 5C Fault/E2E and Release Report

Date: 2026-07-17 (Asia/Shanghai)

Source brief: `.superpowers/sdd/M02-T002-phase5c-brief.md`

Base approved Phase 5B commit: `813c58d`

## Outcome

Phase 5C now proves the conservative recovery contract across real process
death and certifies the distribution on both supported Python minor versions.
Every process-death case uses SQLite, a child process terminated by
`os._exit(86)`, an external append-only effect log, a bounded 15 second child
timeout, a fresh SDK/Store reopen, and the production recovery scanner with a
controlled time beyond the durable lease expiry.

No public signature, Store protocol, migration, SQLite schema, dependency,
lockfile, task ledger, or progress ledger changed. `TERMINATE` remains rejected
and no M02-T003 or M02-T004 behavior was implemented.

## Commits

- `742ae1a` - `docs: brief Phase 5C fault recovery gates`
- `68d2d2e` - `test(faults): cover subprocess recovery boundaries`
- `7c50d23` - `fix(recovery): resume committed tool outcomes after hard exit`
- `40711bd` - `test(workflow): reach interruption through recovery scanner`
- `bccca14` - `test(faults): execute MCP hard-exit recovery through manager`

The implementation changes only `src/agent_sdk/runtime/recovery.py`. The other
Phase 5C changes are the approved brief, fault/E2E tests, the replacement of a
fabricated Workflow interruption fixture, and this report.

## TDD evidence

The first real child-process matrix produced this RED on Python 3.13:

```text
tests/faults/test_subprocess_recovery.py
2 passed, 1 failed

FAILED test_safe_tool_commit_hard_exit_resumes_without_repeating_tool
checkpoint.phase == READY_FOR_MODEL
AgentSDKError: recovery required
```

The unknown Provider and unknown Tool/Workflow scenarios were already GREEN,
showing that the existing conservative unknown-outcome path worked across a
real process death. The new safe-outcome test exposed a distinct production
gap rather than a harness error. A non-subprocess regression then reproduced
the same failure by cancelling immediately after the durable Tool batch
returned and before the following progress marker.

Root cause: `complete_tool` atomically commits the Tool outcome and
`READY_FOR_MODEL` checkpoint, while `step.completed` is a later independent
event. After a hard exit at that exact safe boundary, `run.interrupted` followed
a strictly certified `tool_completed` state. The recovery lifecycle validator
accepted only `ready_for_step`, so it incorrectly sent this already-safe state
to reconciliation.

The minimal fix allows `tool_completed` only at the already strict
`READY_FOR_MODEL` safe-checkpoint relation. The surrounding validator still
requires exact Run/Session identity, complete operation outcomes, exact
messages, Tool results, usage, fingerprints, lifecycle positions, and no
STARTED operation. Unknown Provider, Tool, and MCP outcomes remain closed by
default.

GREEN evidence after the fix:

```text
tests/faults/test_subprocess_recovery.py
6 passed in 17.66s

tests/integration/runtime/test_recovery_api.py
tests/integration/runtime/test_tool_recovery_execution.py
248 passed in 76.01s

tests/faults/test_subprocess_recovery.py
tests/integration/mcp/test_mcp_tool_slice.py
36 passed in 18.54s
```

The safe hard-exit Workflow case also reached a completed terminal Run and
completed Workflow projection, thereby exercising the strict terminal evidence
certifier rather than only a standalone Run result.

## Fault and E2E matrix

| Boundary / decision | Durable and external proof | Result |
| --- | --- | --- |
| Provider accepted, before outcome | Provider writes `provider_accepted`, child exits, durable Model operation remains STARTED | Scanner interrupts; one pending Model request; default recovery does not invoke Provider; `CONFIRM_NOT_EXECUTED` itself has zero callbacks; explicit recovery creates one new Provider attempt |
| Application Tool side effect, before outcome | Tool writes `application:side_effect`, child exits, durable Tool operation remains STARTED | Scanner interrupts; one pending Tool request; default recovery does not invoke Tool; `CONFIRM_COMPLETED` itself has zero callbacks; explicit Workflow recovery projects the confirmed result and completes without repeating Tool |
| MCP side effect, before outcome | A real `MCPManager._for_test` discovers and normalizes `mcp.fault.external_lookup`; the fake MCP session writes its marker inside `session.call_tool` and exits | Reopened SDK reconnects the same normalized capability; default recovery makes zero MCP calls; resolution makes zero MCP calls; explicit `RETRY` after risk acknowledgement makes exactly one reopened `session.call_tool` call |
| Safe Tool outcome batch committed | Store delegate exits only after the `tool.call.completed` `commit_run_progress` call returns | Reopened checkpoint is `READY_FOR_MODEL`; scanner interrupts; explicit recovery resumes without Tool/MCP repetition and completes exactly once |
| Safe Tool outcome inside Workflow | Same post-commit hard exit while a selected Workflow node owns the Run | `recover_workflow` resumes the Run, certifies its terminal evidence, completes the node and Workflow, and detaches ownership without repeating Tool |

The Provider case closes its Session before resolution and proves the Session
remains `closing` and ordinary delete remains busy while the interrupted/
reconciling Run is owned. After explicit recovery naturally terminalizes and
detaches the Run, ordinary delete succeeds. The full suite also retains the
Memory/SQLite resolution, Workflow projection, closing/delete, two-SDK, and
zero-Provider/Tool/MCP/permission-callback matrices from Phases 5A and 5B.

The prior single-SDK Workflow test no longer writes an `INTERRUPTED` snapshot.
It starts a real blocked Provider call, abandons it, invokes the public recovery
scanner, observes `INTERRUPTED`, and then proves the selected Workflow remains
active while the production recovery path admits reconciliation without a
second Provider call.

## Full supported-Python gates

All final full-suite commands had zero skips and zero failures.

Python 3.12 used an official signed CPython runtime because Windows application
control rejected the unsigned DLLs in the pre-existing uv standalone 3.12.13
runtime. The official Python 3.12.10 installer SHA256 was verified as
`67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb`,
installed into an external temporary directory with no launcher, PATH,
shortcuts, docs, tests, or file associations, and synchronized from `uv.lock`
into an external gate environment with the required workspace `uv` executable.
Pytest plugin autoload was disabled only for this gate because the repository
does not use Hypothesis and Windows blocked Hypothesis's newly installed native
plugin; `pytest_asyncio.plugin` was loaded explicitly.

```text
CPython 3.12.10
python -m pytest -p pytest_asyncio.plugin -q -ra
2153 passed in 172.58s

CPython 3.13.14
uv.exe run --frozen --python 3.13 python -m pytest -q -ra
2153 passed in 165.60s
```

Before the successful 3.12 gate, two environment-only attempts were rejected by
Windows application control: the isolated pytest executable was blocked before
collection, then the uv standalone runtime's `_sqlite3` DLL was blocked during
collection. A first official-runtime run reached 100% but was not counted
because its Hypothesis summary plugin was blocked and the CLI build test skipped
without an explicit `UV` environment value. None of those attempts changed the
repository; the final command above supplied `UV`, produced no skip, and exited
zero.

## Static, import, scope, and schema gates

```text
uv.exe run --frozen --python 3.13 ruff check src tests examples
All checks passed!

uv.exe run --frozen --python 3.13 mypy src
Success: no issues found in 75 source files

git diff --check 813c58d..HEAD
exit 0
```

Fresh import and compatibility smoke imported all 53 `agent_sdk` modules,
resolved all 103 unique package-root exports, confirmed identical exact
`RecoveryAPI.resolve` and `ReconciliationService.resolve` signatures, and
confirmed SQLite schema version 3.

Migration hashes remained:

- `0001_initial.sql`: `bbba32d3480b1a2ce4d9e0443bcd118dbaad0f9e639622040922ba5fa2d796b3`
- `0002_idempotency.sql`: `ab0169f70c28946a0564cc57a8bce97b9f5164819930cad71b96aaba8d0bc02c`
- `0003_leases.sql`: `63eaef03dcd1c10aabb6ce654374b8ae4d4bcc40477742a992ab2e26f933b7ee`

The exact scope audit from `813c58d` found no diff in `pyproject.toml`,
`uv.lock`, storage, migrations, task ledgers, or progress ledgers.

## Distribution and clean-install gates

The required workspace `uv` executable built both artifacts into an external
temporary directory:

- `agent_sdk-0.1.0.dev0-py3-none-any.whl`, 208,374 bytes,
  SHA256 `9156b2faa93c7a3970a3a52371c97edbfeaeea82b33ce4c891aa6291ceb5f2ef`
- `agent_sdk-0.1.0.dev0.tar.gz`, 3,488,453 bytes,
  SHA256 `fc4cdb17d595eb7f478b72c8845203f88cb467e9ea693b40c72ebe9e988607f6`

The wheel was installed into fresh external CPython 3.12.10 and 3.13.14
environments. Both clean installs validated:

- distribution version `0.1.0.dev0` and import origin under that clean
  environment's `site-packages`;
- all 53 modules and all 103 unique root exports;
- public reconciliation action/models/service and exact resolve signatures;
- SQLite schema version 3.

Both clean environments ran:

```text
python -m examples.reference_cli.main --help
exit 0
```

An additional `TrapSDK` invocation on each installed environment made SDK
construction fail loudly if reached. `--help` exited zero before the trap, which
proves the help path neither opens a Store nor reaches model execution.

## Cleanup and handoff

The external build directory, official temporary Python runtime and installer,
locked Python 3.12 gate environment, and both clean wheel environments were
removed after verification. The linked worktree had no staged, unstaged, or
untracked files before this report was added.

Phase 5C implementation and release evidence are ready for independent Spec and
Quality review, followed by the required fresh whole-M02-T002 review. Task and
progress ledgers intentionally remain unchanged until those approvals.

## Independent review finding closure

The first independent Phase 5C review returned no Critical finding and two
test-evidence findings: I1 and M1. Commit `78f6df6` closes both without changing
production code, public API, schema, dependencies, or ledgers.

### I1 - distinguish the Tool side effect from the Store commit

The original safe Tool subprocess cases wrote
`safe_tool_outcome_committed` only after `commit_run_progress` returned. That
proved the atomic Store boundary but did not independently prove that the Tool
handler's externally visible effect happened before the SDK committed its
outcome.

Tests-first RED added the required
`application:safe_side_effect` expectation to both the standalone and Workflow
safe Tool cases. Both failed with only the commit and recovery-model markers
present. The child Tool handler now fsyncs the distinct append-only side-effect
marker before returning. The Store delegate continues to fsync the separate
`safe_tool_outcome_committed` marker after the durable outcome/checkpoint batch
returns and then performs `os._exit(86)`.

Both reopened tests now assert the complete cross-process order:

```text
application:safe_side_effect
safe_tool_outcome_committed
safe_*_model
```

They independently assert that `application:safe_side_effect` occurs exactly
once and that the reopened Tool handler call count remains zero. Thus the proof
now distinguishes the real Tool effect, the SDK outcome commit, and the later
recovered Model call.

### M1 - derive scanner time from the actual durable lease

The original helper used `datetime.now() + 1 hour`. Although it advanced the
scanner beyond the current 30 second lease in practice, it was not bound to the
actual durable lease used by each child scenario.

Tests-first RED compared the configured clock to the child Run's real
`lease.expires_at + 1 microsecond`; the old helper failed that exact equality.
The replacement async helper opens a fresh `SQLiteStore` connection, loads the
target Run lease by `run_id`, closes the connection, settles the SDK startup
scan through the public recovery API, and sets the scanner test clock to the
captured expiry plus the deterministic one-microsecond delta. Provider,
application Tool, MCP, safe standalone Tool, and safe Workflow Tool subprocess
cases all use this helper. No test reads a Store through `RecoveryAPI` private
state.

### Fresh post-review gates

```text
three exact RED-to-GREEN cases
3 passed in 11.58s

complete tests/faults/test_subprocess_recovery.py
6 passed in 17.73s

RecoveryAPI + Tool recovery regressions
248 passed in 73.50s

scanner-reached Workflow + safe Workflow targets
2 passed in 5.84s

fault matrix + MCP neighboring suite
36 passed in 17.79s

ruff check src tests examples
All checks passed!

mypy src
Success: no issues found in 75 source files

git diff --check 72b9baf..HEAD
exit 0
```

The post-review scope from `72b9baf` contains only
`tests/faults/test_subprocess_recovery.py` and this report. The task/progress
ledgers and M02-T003/M02-T004 remain untouched pending fresh independent review.
