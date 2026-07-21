# v0.1 R3 Final Review Fix Report

## Scope

This change closes every finding in the independent R3 final review at
`5a8a160`: I1, I2, M1, and M2. It does not start R4.

## I1 - Safe first-use L4

Root cause: `ContextCompactor.rebase()` accepted an empty prior-capsule set, so
both automatic and forced first-use L4 could persist an incomplete L4 capsule
whose recursive lineage omitted older sources.

The minimum safe invariant is now enforced at the compactor boundary: L4 with
no validated prior capsule returns a no-usage compaction failure without a
LiteLLM call. The existing planner path persists the deterministic L2 result
with `fallback_from=L4`. L4 with a real prior capsule is unchanged and must cite
that capsule before it can persist successfully.

TDD evidence:

- RED: automatic and forced first-use L4 both persisted `applied_level=L4`;
  `2 failed`.
- GREEN: automatic/forced first-use fallback plus existing-prior rebase;
  `3 passed`.

The SQLite recursive-retrieval scenario now creates an L3 capsule before L4
and proves the final capsule recursively resolves the complete original source
order.

## I2 - Complete prepared-attribution authentication

Root cause: recovery authenticated snapshot identity, ownership, links, and
read/commit stability, but did not prove the snapshot attribution was the
projection originally recorded by immutable creation events or derivable from
the exact prepared request.

Prepared recovery now fails closed before provider, Tool, reconciliation,
resend, or terminal-certification side effects unless all of the following
hold:

- exactly one same-Session `context.view.created` and
  `prompt.manifest.created` creation event exists for the referenced ids;
- both event payloads are strict closed projections of the complete snapshots;
- Context View level, fallback, capsule, token estimate, budget, message/source
  refs, transformations, consumed message ids, and compaction usage are valid;
- Prompt Manifest id/view/model/tool hash, aggregate hash, and ordered
  layer id/version/hash tuples exactly match its creation event;
- the exact prepared request begins with the ordered system layers, every
  system-layer content hash and the aggregate prompt hash match, and canonical
  request Tool schemas reproduce `tools_sha256`;
- exact snapshot and creation-event identity preconditions still hold at the
  no-write authentication commit.

`prompt.manifest.created` now includes layer versions and
`context.view.created` includes consumed message ids, so the creation payloads
are complete rather than silently accepting older incomplete prepared
evidence. Legacy operations with no prepared references retain their existing
compatibility path; prepared operations without complete evidence fail closed.

TDD evidence:

- RED: the original 14 missing/owner/id/link cases passed, while all 14 new
  valid-but-altered cases failed to raise (`7 attribution mutations x Memory /
  SQLite`).
- GREEN: the expanded matrix covers 17 corruption classes on both backends;
  `34 passed`, with zero provider and Tool calls on every rejection.
- Covered mutations include Manifest tool/aggregate/layer hashes, layer
  version/order, and View level/refs/transformations/consumed ids/budget.

## M1 and M2

- Removed the extra EOF blank lines from both R3 Task 4 review artifacts.
- Corrected the historical R3 plan handoff: R4 Task 1 starts at
  `tests/unit/runtime/test_capability_intersection.py` with an expected RED;
  mailbox work is R4 Task 2.
- Docs-contract TDD: the new handoff assertion failed before the plan fix and
  the complete docs suite now passes (`3 passed`).

## Fresh verification

- Context and Prompt suites: `175 passed, 1 skipped in 11.58s`.
- R3 representative combination (Context, Prompt, reconciliation models,
  release E2E, docs): `246 passed, 1 skipped in 12.47s`.
- Provider, Tool, built-in Tool, and text-loop recovery representative suite:
  `276 passed in 39.82s`.
- The single skip is the existing optional tokenizer-backend case.
- Ruff over all changed source/tests: clean.
- Strict mypy over all 93 source files: clean.
- Whole-R3 `git diff --check 2774a94..HEAD`: clean after M1.

The known non-R3 `tests/integration/runtime/test_recovery_api.py` fixture /
built-in-capability mismatch remains release-candidate debt and was not
weakened or broadened by this fix.
