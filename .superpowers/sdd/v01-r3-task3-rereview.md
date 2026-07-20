# v0.1 R3 Task 3 Fix Re-review

Review range: `1a7af37..76c33ea`

Verdict: **CHANGES_REQUIRED**

- Spec: **FAIL**
- Quality: **FAIL**
- Critical: **0**
- Important: **1**
- Minor: **1**

The original C1 and I1 are closed on current schema-v2 runs. Task 3 is not yet
approved because the claimed schema-v1 compatibility fails for a real R2
descriptor shape.

## Original finding closure

### Original C1 — CLOSED

The fix separates public creation evidence from private recovery state:

- New `run.created` events use schema version 2 and the extra-forbid
  `RunCreatedEventPayload`.
- The public payload contains creation identity, ordinary user input, and
  hashes only. It has no execution descriptor, system prompt, Skill/profile
  instructions, model parameters, Tool specification, or Tool schema.
- The authoritative Run snapshot and idempotency result still contain the full
  descriptor required by recovery/replay.
- Schema-v2 recovery and execution-tree assembly load that authoritative
  snapshot and require the public payload to equal a freshly derived payload,
  including descriptor, agent, Tool-capability, user-input, and task-envelope
  hashes. A forged hash or cross-Session claim therefore does not authenticate.
- The new sentinel integration test starts a real Run, proves private values
  remain in the private snapshot, and scans every public event for application
  prompt, model-parameter, Skill, packaged-profile, and Tool-schema markers.

### Original I1 — CLOSED

The production SDK now injects `SkillRegistry.validate_agent` into the shared
`RuntimeCommands.start_run` boundary. Preflight runs before Session loading,
idempotency handling, event/snapshot persistence, task creation, or provider
execution. Direct, Workflow-node, and subagent paths all use that boundary.
Activation failures are normalized to non-retryable `invalid_state`.

Focused tests prove:

- direct missing Skill: no `run.created`, zero provider calls;
- Workflow missing Skill: the Workflow container may exist, but no node Run is
  persisted and the provider is not called;
- subagent missing Skill: no child Run is persisted and the provider is not
  called.

The optional no-op callback on the internal low-level `RuntimeCommands`
constructor preserves existing internal/test callers; all production
`AgentSDK` construction injects the validating boundary. This is a reasonable
backward-compatible low-level default.

## New findings

### I1 — Genuine R2 schema-v1 descriptors fail recovery and execution-tree authentication

`run_created_event_matches(..., schema_version=1)` reconstructs a current
`RunSnapshot` from the already-loaded authoritative snapshot and compares its
current serialized form byte-for-byte with the raw v1 event payload.

That is incompatible with the Task 3 legacy-descriptor upgrader:

1. R2 descriptors legitimately lack `prompt_profile`, `system_prompt`,
   `skills`, and `context`.
2. Loading the private Run snapshot validates the original legacy
   `agent_hash`/`descriptor_hash`, adds those defaults, and canonically rehashes
   the descriptor in memory.
3. The immutable v1 `run.created` event still contains the original R2
   descriptor and hashes.
4. The new helper compares that raw event to the upgraded serialization and
   returns false.

Fresh minimal reproduction:

```text
upgraded_fields= True
v1_matches= False
```

This reaches all three claimed compatibility paths:

- `RunRecoveryService._is_pristine_created`;
- `RunRecoveryService._is_valid_run_event_envelope`;
- `QueryService._assemble_tree_unchecked`.

After SQLite reopen, the private snapshot is normalized by Pydantic while the
stored event remains raw, so a valid historical Run can no longer be recovered
or placed in an execution tree. Context projection still reads its user input,
but that does not repair recovery/tree compatibility.

Required fix:

- For schema v1, validate/authenticate the raw event in its historical shape,
  including its original descriptor hashes, then compare normalized semantic
  creation state after applying the same safe legacy upgrade to both sides.
- Continue rejecting malformed fields, wrong original hashes, mismatched
  Run/Session/parent/workflow identity, and cross-Session event/snapshot pairs.
- Add SQLite-reopen recovery and execution-tree tests seeded with a genuine R2
  descriptor missing all four Task 3 fields. Include wrong-hash and
  cross-Session negative cases.
- Keep schema-v2 exact-shape comparison unchanged.

### M1 — Diff-check is not clean

Fresh `git diff --check 1a7af37..76c33ea` reports:

```text
.superpowers/sdd/v01-r3-task3-fix-report.md:87: new blank line at EOF.
```

Remove the extra blank line before the next review.

## Compatibility and scope notes

- Current schema-v2 Run creation, recovery authentication, execution-tree
  assembly, Context user-input projection, and same-Session checks are
  internally consistent.
- The v2 event shape is explicit and minimal for the stated public Trace
  boundary; private snapshot/idempotency evidence remains complete.
- The change does not wire Task 4 Context middleware or alter model-call
  preparation.
- Existing schema-v1 events using a current/full descriptor remain readable;
  the failure is specifically the real pre-Task3 descriptor shape that the fix
  report claims to support.

## Fresh verification

```text
pytest tests/integration/prompts/test_runtime_prompt.py
6 passed in 4.04s

pytest tests/integration/prompts
       tests/integration/observability/test_queries.py
       tests/unit/context tests/integration/context
       tests/integration/runtime/test_provider_recovery_execution.py
269 passed, 1 skipped in 14.88s

pytest tests/integration/workflow/test_workflow_recovery.py
       tests/integration/workflow/test_workflow_child_slice.py
       tests/faults/test_subprocess_recovery.py
28 passed in 24.31s

ruff check src/agent_sdk tests/integration/prompts/test_runtime_prompt.py
All checks passed!

mypy --strict src/agent_sdk
Success: no issues found in 92 source files

git diff --check 1a7af37..76c33ea
FAILED: extra blank line at EOF in the fix report
```

The skipped test is the existing package-build check when `uv` is unavailable.
