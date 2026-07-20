# v0.1 R3 Task 3 Final Compatibility Review

Review range: `fc72ae4..aa3fcac`

Verdict: **CHANGES_REQUIRED**

- Spec: **FAIL**
- Quality: **FAIL**
- Critical: **0**
- Important: **1**
- Minor: **0**

The genuine R2 schema-v1 recovery fix and the previous review findings are
closed on their tested paths. Task 3 is not yet approved because the SQLite
legacy snapshot-precondition exception does not verify that its qualifying v1
creation event is legal.

## Confirmed closures

### Original C1 — CLOSED

- Current `run.created` events retain the explicit, minimal schema-v2 public
  payload.
- Public events do not expose application system prompts, Skill/profile
  instructions, model parameters, or raw Tool schemas.
- Full execution descriptors remain private in Run snapshots and idempotency
  results.
- Schema-v2 recovery and execution-tree authentication still derive and
  compare the exact public payload and hashes from the authoritative private
  snapshot. This final compatibility patch does not relax the v2 branch.

### Original I1 — CLOSED

- The production SDK still injects `SkillRegistry.validate_agent` at the shared
  `RuntimeCommands.start_run` boundary.
- Direct, Workflow-node, and subagent paths fail with normalized,
  non-retryable `invalid_state` before Run persistence, provider execution, or
  child task creation when a configured Skill is unavailable.

### Previous re-review I1 — CLOSED

- `run_created_event_matches(..., schema_version=1)` now validates the complete
  raw historical `RunSnapshot`. Nested descriptor validation authenticates
  the original R2 `agent_hash` and `descriptor_hash`, applies the safe legacy
  defaults, and compares the complete normalized created state.
- A genuine R2 raw descriptor/event/private snapshot survives SQLite close and
  reopen, builds an execution tree, produces a recovery plan, resumes provider
  execution, and reaches a completed Run.
- Wrong event agent/descriptor hashes, identity changes, cross-Session event
  ownership, wrong private snapshot hashes, and noncanonical private snapshot
  JSON fail closed in the added tests.
- Multiple or non-v1 `run.created` versions do not qualify for the legacy
  snapshot-precondition fallback.

### Previous M1 — CLOSED

`git diff --check fc72ae4..aa3fcac` is clean.

## Finding

### I1 — Legacy exact-precondition fallback accepts an invalid v1 creation event

`SQLiteStore._legacy_v1_run_snapshot_matches` queries only:

```sql
SELECT schema_version FROM events
WHERE run_id = ? AND type = 'run.created'
```

It requires the resulting tuple to equal `(1,)`, then compares the complete
normalized stored and expected Run snapshots. It does **not** load or validate
the qualifying event's:

- `session_id`;
- `sequence`;
- payload shape and canonical JSON;
- original legacy descriptor hashes;
- semantic equality with the stored Run snapshot.

Consequently, the exception is limited to one schema-v1 event, but not to one
**legal** schema-v1 event as required.

Fresh minimal reproduction:

1. Create a valid current Run.
2. Convert its private snapshot to a genuine R2 raw descriptor with valid old
   hashes.
3. Convert its creation event to schema v1, but change the event Session to
   `ses_forged` and replace the entire payload with `{"forged": "payload"}`.
4. Submit a `SnapshotPrecondition` containing the complete normalized
   `RunSnapshot`.

Observed result:

```text
malformed_v1_event_precondition_accepted=True
```

Impact:

The SQLite optimistic-concurrency compatibility path accepts semantic
snapshot equality under corrupted or cross-Session historical evidence. Other
recovery and execution-tree checks reject this event, but the exact
precondition boundary itself is deliberately weakened and does not satisfy the
required fail-closed contract.

Required fix:

- Load the complete set of `run.created` rows for the target Run.
- Require exactly one event with schema version 1, sequence 1, the same Session
  as the stored/precondition Run, and canonical payload JSON.
- Validate/authenticate the raw historical payload using the schema-v1
  `run_created_event_matches` path before allowing normalized snapshot
  equality.
- Retain byte-exact matching for schema-v2 Runs and every non-Run snapshot.
- Add a focused test proving wrong event Session, sequence, payload, and
  original descriptor hashes reject the normalized legacy precondition.

## Scope

- The compatibility patch does not add Context middleware or otherwise enter
  Task 4 scope.
- No further Critical or Minor finding was identified in the requested final
  review scope.

## Fresh verification

```text
pytest tests/integration/prompts/test_runtime_prompt.py
       tests/unit/runtime/test_execution_descriptors.py
40 passed in 5.04s

pytest tests/integration/prompts
       tests/integration/observability/test_queries.py
       tests/unit/context tests/integration/context
       tests/integration/runtime/test_provider_recovery_execution.py
277 passed, 1 skipped in 16.63s

ruff check src/agent_sdk
           tests/integration/prompts/test_runtime_prompt.py
           tests/unit/runtime/test_execution_descriptors.py
All checks passed!

mypy --strict src/agent_sdk
Success: no issues found in 92 source files

git diff --check fc72ae4..aa3fcac
clean
```

The skipped test is the existing package-build check when `uv` is unavailable.
An additional exploratory broad runtime selection was not used as passing
evidence: it produced 408 passes, 1 skip, and 115 failures concentrated in
existing recovery tests whose empty seeded Tool descriptors conflict with the
SDK's pre-existing default built-in Tool set. The reviewed range does not
change that default or those seed helpers.
