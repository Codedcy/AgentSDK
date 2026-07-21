# v0.1 R3 Final Fix Independent Re-review

## Verdict

- Reviewed range: `5a8a160..4a49532`
- Spec: **PASS**
- Quality: **PASS**
- Critical: **0**
- Important: **0**
- Minor: **1**
- Summary: **C0 / I0 / M1**
- Approval: **APPROVED**

All four findings from the independent R3 final review are closed. No new
Critical or Important issue was found in the fix. R3 is approved; the retained
Minor is a nonblocking recovery-performance optimization and does not require
R3 to reopen.

## Original finding closure

### I1 - First-use L4 without a prior capsule

Status: **CLOSED**.

- `ContextCompactor.rebase()` now returns a failed, zero-usage compaction
  result before any LiteLLM call when no validated prior capsule exists.
- Both an automatic L4 recommendation and an explicit forced L4 therefore use
  the existing deterministic L2 fallback, record `fallback_from=L4`, preserve
  all source refs, and persist no capsule.
- L4 with an existing validated capsule is unchanged: it calls the model,
  requires the prior capsule reference in the new capsule, persists L4, and
  recursively resolves the complete older source sequence.
- The SQLite reopen/retrieval test now creates a genuine L3 predecessor before
  L4 and verifies recursive source order and Session deletion behavior.

Fresh focused execution covered automatic first-use, forced first-use, and
existing-prior L4 paths successfully.

### I2 - Complete prepared-attribution authentication

Status: **CLOSED**.

The recovery trust boundary now binds all three required evidence layers:

1. the exact prepared request stored and fingerprinted by the model operation;
2. the Session-owned Context View and Prompt Manifest snapshots;
3. exactly one immutable same-Session creation event for each referenced
   snapshot.

Independent inspection confirmed:

- Context View and Manifest snapshot identity, Session ownership,
  Manifest-to-View link, and provider model are validated first;
- the complete `context.view.created` projection is compared with the View,
  including capsule/levels/fallback, budget, token estimate, refs,
  transformations, consumed ids, and valid usage shape;
- the complete `prompt.manifest.created` projection is compared with the
  Manifest, including aggregate/tool hashes and ordered layer id/version/hash;
- the exact prepared request must begin with the ordered system layers; each
  layer content hash, the aggregate prompt hash, provider model, and canonical
  Tool-schema hash are recomputed and compared;
- the no-write authentication commit applies exact snapshot-data
  preconditions plus both creation-event identity preconditions. This closes
  the supported StateStore TOCTOU window because snapshots are checked by
  value and events are append-only/immutable under both Memory and SQLite
  stores;
- `EventPreconditionError` remains normalized through the existing
  fail-closed recovery conflict boundary;
- authentication runs before recovery planning can reach provider, Tool,
  reconciliation, resend, or terminal-certification side effects.

The expanded Memory/SQLite matrix covers 17 corruption classes per backend,
including valid-but-altered Manifest hashes/layer ordering and View
level/ref/transformation/consumed-id/budget fields. Every rejection asserts
zero provider calls and zero Tool calls. Positive in-flight, authoritative,
and completed-model recovery paths remain green.

Legacy operations whose three prepared-reference fields are all absent still
take the existing legacy reconstruction path. The focused legacy model test
and broad provider/Tool recovery suites remain green.

The two enriched creation payloads are still R3's unreleased schema-v1 event
shape. The recovery validator accepts only the exact current closed payload;
older incomplete prepared evidence intentionally fails closed, while legacy
operations without prepared references remain compatible. Future changes to
these public payload shapes should use an explicit schema-version transition.

### M1 - Whole-R3 diff whitespace

Status: **CLOSED**.

The extra EOF blank lines were removed from both Task 4 review artifacts.
Fresh `git diff --check 2774a94..HEAD` is clean.

### M2 - Historical R3-to-R4 handoff

Status: **CLOSED**.

The R3 plan now hands R4 to Task 1's
`tests/unit/runtime/test_capability_intersection.py` expected-RED boundary and
identifies mailbox work as R4 Task 2. The executable docs contract protects
the ordering and rejects the former mailbox-first command.

## New findings

### Critical

None.

### Important

None.

### Minor

#### M1 - Prepared-reference authentication repeatedly scans the complete Session event stream

`RunRecoveryService._authenticate_prepared_operation()` reads the complete
Session event stream to find two creation events. The caller invokes it once
per prepared model operation, and some recovery paths authenticate the same
evidence more than once. A long multi-turn Session can therefore turn this
step into repeated full scans and approach quadratic work in operation/event
count.

This does not change correctness, fail-closed behavior, or the v0.1 functional
closed loop, so it is nonblocking for R3. A later recovery optimization should
reuse the Session event window already loaded by `_load_evidence`, index
creation events once by `(type, run_id)`, and pass that immutable index through
the authentication calls.

## Fresh independent verification

All commands were run from
`D:\code\AgentSDK\.worktrees\agent-sdk-implementation` at `4a49532`.

```text
Focused I1/I2 positive, corruption, completed recovery, and legacy paths:
42 passed, 73 deselected in 8.08s

Context and Prompt suites:
175 passed, 1 skipped in 14.73s

Provider, Tool, text-loop, and recovery regressions:
294 passed in 55.60s

Reconciliation models, v0.1 release E2E, and docs:
71 passed in 4.47s

Non-overlapping representative total:
540 passed, 1 skipped

Ruff over src and tests:
All checks passed!

mypy --strict src/agent_sdk:
Success: no issues found in 93 source files

git diff --check 2774a94..HEAD:
clean

worktree before review artifacts:
clean
```

The single skip is the existing optional tokenizer-backend case.

## Known non-R3 debt

`tests/integration/runtime/test_recovery_api.py` retains the previously
documented fixture/default-built-in-capability mismatch. The R3 fix neither
weakens that capability validation nor claims a repository-wide green suite;
the debt remains for the release-candidate gate.

## Decision

**Approved: Yes.** R3 now satisfies the final-review requirements with
Critical 0 / Important 0. R4 may begin at the documented Task 1
capability-intersection RED boundary; no R4 work is included in this review.
