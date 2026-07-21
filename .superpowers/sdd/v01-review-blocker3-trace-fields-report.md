# v0.1 whole-review Blocker 3 — complete normalized Trace fields

## Outcome

Completed the exact R5 normalized Trace shape without adding analytics, exporters,
UI, version changes, tags, or publication behavior.

- `TraceStage` now requires authenticated non-empty `session_id` and `run_id`.
- `TraceTimeline.root_kind` distinguishes Run and Workflow roots.
- Agent-owned stages use their owning Run id. Workflow and Workflow-node control
  stages use the Workflow Run id as the documented stable v0.1 convention.
- Model, Tool, Context, permission, Workflow, Child, message, evaluation, and
  recovery references come only from a small event-type whitelist. References are
  bounded to 256 UTF-8 bytes, limited to 64 per direction, and stably deduplicated.
- Model and aggregated Run `cost_usd` equals the finite non-negative value already
  represented by stage `usage`; absent cost remains `None`.
- `error_code` and `retryable` are projected only from sanitized structured failure
  facts or fixed SDK status codes. Raw messages, Tool content/value/error, arguments,
  credentials, stack traces, and provider responses are never copied.
- Historical oversized evidence event ids remain omitted from public evidence and
  output refs. Oversized values inside an allowed public reference field are now
  rejected fail-closed, as required by Blocker 3.

## Root cause

The R5 projector already preserved stage status, time, tree, usage, evidence ids,
and high-water semantics, but its public Pydantic models never exposed the remaining
specified fields. Consequently the projector discarded owning Session identity,
reference facts, top-level cost, and sanitized failure facts, while `TraceService`
did not identify the root kind. `run_id` also remained optional because Workflow,
message, evaluation, Child, and Context ownership conventions had not been encoded.

## Strict RED

The first focused run established the missing public shape:

```text
3 failed in 3.23s
```

Failures were the missing `TraceStage.session_id` and missing
`TraceTimeline.root_kind` for both Run and Workflow timelines.

The second focused run established the remaining reference/reopen contract:

```text
3 failed in 3.30s
```

Failures were missing `input_refs`, an oversized Context reference not failing
closed, and the absent root kind after SQLite reopen. Each failure was observed
before production implementation.

## GREEN and regression evidence

The exact six RED nodes passed after the minimal implementation:

```text
6 passed in 3.30s
```

Fresh pre-commit repetition of the same six nodes: `6 passed in 3.50s`.

Complete observability and deterministic attribution:

```text
111 passed in 5.01s
```

Installed/public v0.1 release scenario, reference example, and release docs:

```text
6 passed in 76.63s
```

Affected provider, Tool, Context, Workflow, SQLite, reconciliation, and public
recovery compatibility suites, including historical v1/v2 paths:

```text
670 passed in 157.05s
```

Final static and diff gate:

```text
Ruff: All checks passed
mypy --strict: Success, 107 source files
git diff --check: no errors (Windows line-ending notices only)
```

## Public contract coverage

Tests cover Run-root and Workflow-root identity, Model/Tool/Context input and output
refs, stable deduplication, Model and aggregated Run cost, structured and fixed
failure codes, optional retryability, absence of raw failure/output content, fixed
high-water behavior, public JSON serialization, malformed reference fail-closed,
SQLite reopen equality, and existing v1/v2 recovery behavior. The public v0.1
acceptance test now asserts `root_kind`, Session identity, and non-empty owning
identity through installed SDK interfaces.
