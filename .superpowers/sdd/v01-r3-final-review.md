# v0.1 R3 Independent Final Review

## Verdict

- Reviewed range: `aa2d410..72dd259`
- Spec: **FAIL**
- Quality: **FAIL**
- Critical: **0**
- Important: **2**
- Minor: **2**
- Summary: **C0 / I2 / M2**
- Approval: **NOT APPROVED**

R3 has broad positive coverage and its previously reported Task 1-5 findings
are closed on their focused paths. The whole-increment review nevertheless
found two cross-feature recovery/context gaps that are not covered by those
task reviews. R3 cannot be approved until both Important findings are fixed
and independently re-reviewed.

## Strengths

- L0-L2 rendering is deterministic, bounded, detached, and preserves current
  Tool protocol messages while retaining source references.
- L3 summary and L4 rebase use LiteLLM with
  `purpose="context_compaction"`; invalid/schema/input/output-budget failures
  use the deterministic L2 fallback without failing the main Run.
- `ContextMiddleware` is called at the new-model boundary, so each new model
  call receives a durable Context View and Prompt Manifest. Recovered
  completed/in-flight operations do not create duplicate preparation evidence.
- Default, coding, application, and ordered Skill prompt layers are composed
  and represented in a redacted public manifest. Direct, Workflow, and child
  Run creation share the centralized Skill preflight.
- Prepared provider requests are stored canonically with an exact fingerprint;
  malformed request shapes fail closed and legacy operations retain their
  authenticated compatibility path.
- The active release ledger correctly closes R3 and hands R4 to Task 1's
  capability-intersection RED boundary. It does not claim the whole repository
  is green.

## Critical findings

None.

## Important findings

### I1 - First-use L4 succeeds without a prior capsule and drops older history from the capsule lineage

- Files: `src/agent_sdk/context/planner.py:174-210`,
  `src/agent_sdk/context/compactor.py:64-112`
- Requirement: R3 defines L4 as a rebase of validated prior capsules plus
  active/recent/protected state. A new L4 capsule must cite prior capsule
  references so recursive evidence still reaches the older source history.
- Problem: when the policy first recommends L4 and the Session has no capsule,
  `list_capsule_records()` returns an empty tuple. `rebase()` accepts that
  state, sends only retained recent/protected sources, and requires only those
  retained refs. The planner then persists `applied_level=L4` even though older
  unprotected sources were neither summarized nor represented by a prior
  capsule.
- Fresh minimal reproduction on the real planner:

  ```text
  applied= L4
  prior_capsule_ids= []
  rebase_sources= ['evt_recent_answer', 'evt_latest_user']
  view_source_refs= ('evt_old_user', 'evt_old_answer', 'evt_old_tool',
                     'evt_recent_answer', 'evt_latest_user')
  capsule_refs= ('evt_recent_answer', 'evt_latest_user')
  retrievable_refs= ('evt_recent_answer', 'evt_latest_user')
  ```

  The View claims all five source refs, but recursive capsule retrieval can
  recover only the two retained refs. The three older refs have no compressed
  representation in the L4 lineage.
- Impact: automatic L4 can silently erase usable historical context on its
  first activation while recording a successful L4 result and misleading
  provenance. This violates both the distinct-L4 contract and traceable source
  preservation.
- Required correction: treat absence of a valid prior capsule as an invalid L4
  precondition and use the established deterministic L2 fallback (or another
  explicitly specified safe transition). Add automatic and forced first-use L4
  tests with older unprotected history, and assert that successful L4 is
  possible only with a cited prior capsule whose recursive evidence includes
  the older sources.

### I2 - Recovery authenticates prepared reference identity, but not the integrity of View/Manifest attribution contents

- Files: `src/agent_sdk/runtime/recovery.py:4627-4681`,
  `src/agent_sdk/prompts/models.py:43-56`
- Requirement: prepared recovery evidence and public trace attribution must be
  fail-closed under Memory/SQLite corruption. The Prompt Manifest's layer/tool
  hashes and the Context View's level/source/transform evidence are the durable
  attribution for the exact prepared request.
- Problem: `_authenticate_prepared_operation()` validates snapshot shape,
  identity, Session ownership, Manifest-to-View link, and model, then uses exact
  snapshot-data preconditions to prevent replacement during the check. It does
  not authenticate the View/Manifest attribution fields against their creation
  events or the exact prepared request. The precondition proves only that the
  value did not change between read and commit; it does not prove that the
  value was the originally created evidence.
- Fresh minimal reproduction changed only the in-memory Prompt Manifest's
  `tools_sha256` to another syntactically valid 64-hex value. Recovery accepted
  the changed manifest and continued to the ordinary unknown-outcome path:

  ```text
  recovery_error= conflict recovery required
  ```

  It did not raise the expected `recovery state conflict`. The same class of
  undetected change applies to valid-but-altered layer hashes and Context View
  attribution fields that preserve ids/ownership/linkage.
- Impact: the exact provider request remains recoverable, but the durable/public
  explanation of which tools, prompt layers, compaction level, and sources
  produced it can be reassigned without fail-closed detection. This makes trace
  analysis and result attribution untrustworthy after persisted-state
  corruption.
- Required correction: authenticate the complete Context View and Prompt
  Manifest projections against their immutable creation events and bind all
  derivable Manifest fields to the stored prepared request (at minimum Tool
  schema hash and prompt-layer hashes/order). Apply the check before recovery,
  reconciliation, resend, or terminal certification. Add Memory and SQLite
  corruption tests for valid-but-altered hashes, levels, refs, and
  transformations, with zero provider/Tool calls on rejection.

## Minor findings

### M1 - Whole-R3 diff-check is not clean

Fresh `git diff --check aa2d410..72dd259` reports:

```text
.superpowers/sdd/v01-r3-task4-rereview.md:131: new blank line at EOF.
.superpowers/sdd/v01-r3-task4-review.md:187: new blank line at EOF.
```

These are documentation-only whitespace defects and do not affect runtime
behavior, but they contradict the recorded clean whole-increment quality gate.

### M2 - The historical R3 Task 5 plan still contains the superseded mailbox-first handoff

`docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md:630-639`
still says the R3 checkpoint should resume with
`tests/unit/subagents/test_mailbox.py`. The corrected release ledger, progress
record, Task 5 report, and R4 plan correctly resume at
`tests/unit/runtime/test_capability_intersection.py` and identify mailbox as R4
Task 2. Mark or amend the stale plan step so the implementation plan no longer
contradicts the durable handoff.

## Requirement assessment

1. Automatic L0-L3 selection, deterministic L0-L2, distinct L3, bounded
   compaction, persisted Views/capsules/events, and L2 failure fallback:
   **PASS**.
2. L4 with an existing prior capsule and recursive same-Session evidence:
   **PASS**; first-use L4 with no prior capsule: **FAIL (I1)**.
3. Context preparation before every new model call, ordinary checkpoint
   preservation, and exact in-flight request reuse without duplicate
   preparation: **PASS**.
4. Default/configurable application prompts, ordered Skill layers, shared Skill
   preflight, and redacted public prompt/run events: **PASS**.
5. Strict canonical prepared requests and authenticated reference
   identity/ownership/linkage: **PASS**; complete attribution-integrity
   authentication: **FAIL (I2)**.
6. Memory/SQLite reopen behavior and tested missing/cross-owner/id/link
   corruption: **PASS**; valid-but-altered attribution corruption:
   **FAIL (I2)**.
7. R3 completion ledger and R4 Task 1 active handoff: **PASS**, with the stale
   historical plan text noted as M2.

## Fresh verification evidence

All commands were run independently from
`D:\code\AgentSDK\.worktrees\agent-sdk-implementation` at `72dd259`.

```text
R3 Context, Prompt, reconciliation, release E2E, and docs:
223 passed, 1 skipped in 13.51s

Provider/Tool/text recovery, execution descriptors, Workflow/subagent smoke:
358 passed in 46.41s

Combined representative tests:
581 passed, 1 skipped

Ruff over src and tests:
All checks passed!

mypy --strict src/agent_sdk:
Success: no issues found in 93 source files

git diff --check aa2d410..72dd259:
FAILED with the two M1 EOF warnings
```

The single skip is the existing optional tokenizer-backend case. Passing tests
establish a strong regression baseline but do not cover the two independently
reproduced Important boundaries.

## Known non-R3 debt

`tests/integration/runtime/test_recovery_api.py` retains the previously
documented fixture/capability mismatch: legacy seeded descriptors declare no
Tools while `AgentSDK.for_test` enables built-ins by default. R3 does not change
that default or weaken the capability check, and this review does not present
the full repository as green. The debt remains for the release-candidate gate.

## Decision

**Approved: No.** R3 requires one narrow context fix for first-use L4 and one
consolidated recovery-integrity fix for View/Manifest attribution, followed by
fresh focused and whole-R3 re-review. R4 must not start before R3 reaches
Critical 0 / Important 0.
