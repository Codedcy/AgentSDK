# M02-T002 Whole Phase 5B Review-Fix Report

Date: 2026-07-17

Baseline: `b5b5fc8`

Implementation head: `3d9f412`
Source of truth: `M02-T002-phase5b-whole-review-fix-brief.md`

## Outcome

The four Important findings from the fresh whole-Phase-5B review were reproduced
through public production paths, repaired with test-first changes, and verified
on both Memory and SQLite. The final Python 3.13 suite is green with 2,147 tests,
zero failures, and zero skips. Public SDK exports and signatures are unchanged,
SQLite remains schema version 3, and no Phase 5C, M02-T003, or M02-T004 work was
entered.

Implementation commits:

- `8dfa921` — `fix(recovery): bound strict Tool evidence validation`
- `7b168ac` — `fix(recovery): certify exact ready model histories`
- `cd1397c` — `fix(recovery): normalize cumulative terminal decisions`
- `3d9f412` — `fix(workflow): bind terminal recovery evidence atomically`

The planning commit is `879af4d`.

## Finding 4 — total strict Tool evidence validation

### RED

Public `RecoveryAPI.resolve` inputs demonstrated that the recursive raw-JSON
check was not total. Very deep/cyclic values and hostile Mapping operations could
raise outside the strict evidence boundary and surface the wrong public error.
The new tests first reproduced the failure with no durable mutation on Memory
and SQLite.

### GREEN

`_strict_tool_result` now:

- contains every mapping access, iteration, serialization, and model-validation
  exception;
- validates exact JSON scalars and exact `dict`/`list` containers without
  coercing tuples, enums, custom mappings, numeric strings, or arbitrary
  objects;
- uses an iterative traversal with explicit depth, node, and byte budgets;
- detects cycles and rejects non-finite numbers and non-string object keys;
- returns a deeply detached `ToolResult` or `None`, never an input-dependent
  exception.

All invalid evidence still maps to the constant nonretryable
`INVALID_STATE / reconciliation decision is invalid` and performs no write.
Legal nested JSON at the configured boundaries remains accepted.

Focused result: 10 passed. The production and test changes are limited to
`runtime/recovery.py` and `test_reconciliation_resolution.py`.

## Finding 3 — exact `READY_FOR_MODEL` relation

### RED

Mutation matrices showed that confirmed-Tool replay and explicit recovery could
admit post-decision `READY_FOR_MODEL` state using aggregate turn/result counts.
Checkpoint fields, messages, Tool messages, output, usage, ToolResults, related
operations, and events could drift without being related back to the exact
history before Provider admission.

### GREEN

One shared `_is_exact_ready_model_relation` now reconstructs the complete history
and is used by:

- confirmed-Tool exact replay;
- safe checkpoint and recovery admission;
- cumulative effective-history validation.

The relation crosses exact Model/Tool operations and fingerprints, outcomes,
messages, assistant ToolCalls, Tool messages, output parts, cumulative usage,
ordered ToolResults, permission/policy/capability evidence, checkpoint
phase/turn/version, and lifecycle ordering. The confirmed ToolResult must equal
both the Tool operation outcome and Tool message content. Rejection occurs before
Provider, Tool, MCP, or permission callbacks.

Coverage added 52 checkpoint/operation mutation combinations and 20 event
mutation combinations across exact replay and explicit recovery on Memory and
SQLite. Legal multi-turn, partial-stream, all ToolResult statuses, later
resolution, and terminal histories remain accepted.

Focused results included 346 reconciliation tests, 117 RecoveryAPI tests, and
237 Provider/Tool recovery tests.

## Finding 1 — cumulative terminal Model decisions

### RED

The first matrix reproduced eight failures for prior Model
`CONFIRM_NOT_EXECUTED`/`RETRY` followed by Model terminal text/failure on Memory
and SQLite. A further four failures reproduced confirmed Tool followed by later
Model terminal text/failure. The terminal decision could commit, but one or both
decisions then failed exact replay and the Run could not be safely projected to
Workflow.

### GREEN

Terminal validation now:

1. authenticates every resolved attempt in `reconciliation.requested` order;
2. normalizes all prior resolved evidence into one effective history;
3. separately authenticates the current exact terminal operation, evidence, and
   event projection;
4. sends one normalized history through the complete lifecycle, Provider, and
   Tool state machines.

The repair also preserves the runtime output-parts convention by excluding an
empty ToolCall Model text from the exact persisted output-parts tuple, while
retaining reconciliation authority records needed for denied and
invalid-argument ToolResults.

Coverage crosses CNE, RETRY, confirmed Tool, prior confirmed Model ToolCall,
terminal text/failure, partial streams, exact replay of every decision,
Memory/SQLite, and Workflow live/success/failure projection. Existing orphan,
duplicate, moved, and corrupt-history negatives remain fail-closed.

Focused results:

- new cumulative public matrix: 12 passed;
- cumulative plus all confirmed ToolResult statuses: 28 passed;
- reconciliation resolution file: 358 passed;
- Workflow reconciliation/projection file at this stage: 48 passed.

## Finding 2 — atomic Workflow terminal evidence

### RED

The node projection barrier mutated durable recovery evidence after terminal Run
certification but before `workflow.node.completed`/`failed` commit. All 32
combinations initially failed the new expectation:

- checkpoint, operation, and reconciliation mutation;
- event identity, content, missing, extra, and moved mutation;
- terminal success and failure;
- Memory and SQLite.

The original snapshot CAS did not cover this evidence, so Workflow/node state
was written from a certification that was no longer current.

### GREEN

The terminal certifier now returns the certified `RunSnapshot` together with a
detached immutable internal token containing:

- the exact Run checkpoint JSON, including the `None` representation;
- the complete ordered external-operation JSON tuple;
- the complete ordered reconciliation-record JSON tuple;
- every certified Run event as exact `(cursor, canonical envelope JSON)`.

The token is carried only through the internal Workflow projection path. It is
not public, is not stored in snapshots or events, performs no callback, and
requires no schema migration. Ordinary live Workflow transitions do not carry
it.

Memory compares the token while holding the same Store lock used for the node
commit. SQLite compares it after `BEGIN IMMEDIATE` and before any write in the
same transaction. The comparisons include order and cardinality, so changed,
missing, added, duplicated, or moved evidence conflicts atomically.

A dedicated internal precondition error prevents the Workflow executor from
silently retrying an evidence-CAS failure and re-certifying a newly changed but
otherwise valid event identity. This closes the re-certification race while
retaining the existing winner/follower handling for ordinary Workflow CAS
conflicts.

Focused results:

- atomic evidence barrier matrix: 32 passed;
- complete Workflow reconciliation/projection file: 80 passed;
- Workflow recovery admission: 103 passed;
- Workflow recovery/ownership/child neighbors: 70 passed;
- all storage integration tests: 449 passed;
- reconciliation resolution regression: 358 passed.

Legal terminal success/failure, two-SDK winner/follower, parent relation,
Session closing/delete, ambiguity, and supported cascade cases remain green.

## Changed files

The whole repair range changes only:

- `.superpowers/sdd/M02-T002-phase5b-whole-review-fix-brief.md`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/storage/base.py`
- `src/agent_sdk/storage/memory.py`
- `src/agent_sdk/storage/sqlite.py`
- `src/agent_sdk/workflow/executor.py`
- `src/agent_sdk/workflow/state.py`
- `tests/integration/runtime/test_reconciliation_resolution.py`
- `tests/integration/runtime/test_recovery_api.py`
- `tests/integration/workflow/test_workflow_reconciliation_projection.py`
- this report

There is no dependency, lockfile, migration, durable schema, roadmap, progress
ledger, Phase 5C, M02-T003, or M02-T004 change.

## Final verification

### Full Python 3.13

```text
uv.exe run --python 3.13 pytest -q
2147 passed in 147.12s; zero skipped, zero failed
```

### Static and compatibility gates

```text
uv.exe run --python 3.13 ruff check .
All checks passed!

uv.exe run --python 3.13 mypy src
Success: no issues found in 75 source files

git diff --check b5b5fc8..HEAD
exit 0
```

Fresh import/signature/schema smoke:

- 53 `agent_sdk` modules imported;
- 103 unique root `agent_sdk.__all__` exports resolved;
- exact unchanged `RecoveryAPI.resolve` and
  `ReconciliationService.resolve` signatures;
- SQLite schema version remains 3.

The final scope audit from `b5b5fc8` contains only the implementation, tests,
brief, and report listed above. `pyproject.toml`, `uv.lock`, migrations, docs,
M02-T003, and M02-T004 are unchanged.

## Residual scope and handoff

No known whole-Phase-5B repair risk remains after the final gates. Phase 5C,
M02-T003, M02-T004, additional Workflow features, and any public Store API or
durable schema expansion remain explicitly deferred. The branch is preserved;
no merge or push was performed.

The independent whole-Phase-5B re-review result is recorded in the closure
addendum below.

## Independent whole-Phase-5B re-review closure

A fresh read-only review compared `b5b5fc8..3d9f412`, read the complete repair
brief, inspected every production and test diff, and traced each of I1–I4 through
its public boundary and Store/Workflow integration.

Result:

- Spec Compliance: **C0 / I0 / M0**
- Task Quality: **C0 / I0 / M0**
- Verdict: **Approved**

The reviewer found no Critical, Important, or Minor issue. It specifically
confirmed the total strict Tool boundary, shared exact `READY_FOR_MODEL`
relation, requested-order cumulative terminal normalization, immutable complete
recovery-evidence token, Memory/SQLite atomic parity, nonretryable evidence-CAS
handling, and absence of Provider/Tool/MCP/permission callbacks before the
required validation boundaries.

The reviewer remained read-only and made no file, index, HEAD, or commit change.
Its own dynamic command could not locate `uv.exe`, so it made no independent
dynamic-pass claim; the authoritative fresh dynamic evidence is the successful
2,147-test Python 3.13 gate recorded above with the repository's explicit uv
path.
