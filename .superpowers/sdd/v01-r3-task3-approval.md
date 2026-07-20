# v0.1 R3 Task 3 Final Approval

Review range: `f34d2fc..9fbcd16`

Verdict: **APPROVED**

- Spec: **PASS**
- Quality: **PASS**
- Critical: **0**
- Important: **0**
- Minor: **0**

The last open legacy precondition finding is closed. The previously closed
public Trace and Skill-preflight findings remain closed, and this patch does
not enter Task 4 scope.

## Legacy precondition finding — CLOSED

`SQLiteStore._legacy_v1_run_snapshot_matches` now permits normalized semantic
equality only after authenticating complete legacy creation evidence:

- the raw stored snapshot must be canonical JSON;
- it must validate as a complete `RunSnapshot`;
- its `run_id` must match the precondition entity;
- exactly one `run.created` event may exist for that Run;
- the event Session must equal the stored Run Session;
- the event sequence must be exactly 1;
- the event schema version must be exactly 1;
- the raw event payload must be canonical JSON;
- `run_created_event_matches(..., schema_version=1)` must authenticate the
  complete historical payload, including original R2 descriptor hashes and
  normalized identity/state;
- the expected precondition data must validate as a complete `RunSnapshot` and
  equal the fully normalized stored snapshot.

The compatibility exception therefore applies only to a genuine, uniquely
owned schema-v1 creation event. Current schema-v2 Runs and all other snapshot
preconditions retain byte-exact comparison; the v2 event matcher and public
payload were not relaxed.

## Positive and negative evidence

The focused compatibility tests prove:

- a genuine R2 raw private snapshot plus its authenticated v1 creation event
  accepts the normalized snapshot precondition;
- the same R2 data survives SQLite reopen, builds an execution tree, produces
  a recovery plan, resumes provider execution, and completes;
- each of the following fails closed:
  - wrong event Session;
  - wrong event sequence;
  - wrong event schema version;
  - forged event payload;
  - noncanonical event payload JSON;
  - wrong original legacy descriptor hash;
  - multiple `run.created` events.

The tests exercise the real SQLite persistence/precondition implementation
rather than a duplicated validation helper.

## Previously closed findings remain closed

- Schema-v2 `run.created` remains an explicit minimal public payload containing
  creation identity, ordinary user input, and hashes only.
- Public events contain no raw application system prompt, Skill/profile
  instructions, model parameters, or Tool schemas.
- Full execution descriptors remain private in Run snapshots and idempotency
  results.
- Direct, Workflow-node, and subagent execution still share the injected
  `SkillRegistry.validate_agent` preflight and fail before Run persistence,
  provider execution, or child task creation when a Skill is unavailable.
- Genuine schema-v1 historical descriptors validate their original hashes
  before safe default upgrade; malformed, cross-Session, or forged evidence is
  rejected by recovery and execution-tree authentication.
- No Context middleware, prepared model-request, or other Task 4 behavior was
  introduced.

## Fresh verification

```text
pytest tests/integration/prompts/test_runtime_prompt.py
       -k "normalized_snapshot_precondition or authenticated_event_allows"
8 passed, 14 deselected in 6.04s

pytest tests/integration/prompts/test_runtime_prompt.py
       tests/unit/runtime/test_execution_descriptors.py
48 passed in 6.16s

ruff check src/agent_sdk
           tests/integration/prompts/test_runtime_prompt.py
           tests/unit/runtime/test_execution_descriptors.py
All checks passed!

mypy --strict src/agent_sdk
Success: no issues found in 92 source files

git diff --check f34d2fc..9fbcd16
clean
```

Task 3 may proceed to its transition/checkpoint gate.
