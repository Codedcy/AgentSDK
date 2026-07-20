# v0.1 R3 Task 4 Report

## Status

PASS. Every new model call now prepares and persists its Context View and Prompt
Manifest before provider execution. In-flight model recovery uses the exact
stored prepared request, while legacy operations retain the historical
reconstruction path.

## Scope

- Added the runtime Context middleware and checkpoint-aware Context planning.
- Wired the middleware once at the new-model-call boundary.
- Added strict canonical prepared-request persistence and fingerprints.
- Updated every certified recovery/history validator to understand both prepared
  and legacy model operations.
- Added integration and release acceptance coverage.
- Did not modify the R3 release ledger or progress files; those remain Task 5.

## TDD evidence

The initial Task 4 integration tests failed as expected:

- no Context View existed before provider execution;
- `ModelCallOperation` had no prepared-request identity.

After the implementation, the focused middleware/recovery gate produced:

```text
4 passed
```

Additional recovery work found remaining legacy-only event/request comparisons.
The full Tool recovery gate initially produced `130 passed, 3 failed`. Two
failures were prepared-request compatibility gaps and were fixed by routing all
certified history checks through the shared request/payload helpers. The third
test deliberately corrupted a prepared request fingerprint. The strict persisted
model now rejects that corruption earlier as `recovery state conflict`; the test
was migrated while preserving fail-closed, zero-provider-call, zero-tool-call,
and no-secret-leak assertions. Final result:

```text
133 passed
```

## Implementation

### Context before each new model call

`ContextMiddleware.prepare`:

1. plans from the durable Session event stream and ordinary Run checkpoint;
2. automatically recommends/applies L0-L4 through the configured policy;
3. activates pinned Skills;
4. composes the default, application, and Skill system layers;
5. persists the Prompt Manifest;
6. returns the exact provider message sequence.

The Run checkpoint continues to contain only ordinary user, assistant, and Tool
ledger messages. Recovered completed or in-flight model operations do not invoke
the middleware again.

### Durable exact request

Prepared model operations persist:

- `context_view_id`;
- `prompt_manifest_id`;
- the canonical model request;
- the SHA-256 fingerprint of that exact canonical request.

The public `model.call.started` event contains only the model, durable reference
ids, and fingerprint. It does not contain system prompts, Skill instructions,
context text, Tool schemas, or model parameters.

Canonical request parsing is strict and fail-closed for extra fields, malformed
sequences, non-finite JSON numbers, incomplete references, and fingerprint
mismatches. Legacy operation JSON without the new optional fields still loads.

### Recovery

Provider recovery, reconciliation, terminal certification, safe checkpoint
certification, Tool history certification, and historical replay validation all
use the exact stored request when present. The descriptor/checkpoint
reconstruction path remains available only for legacy operations.

An authoritative provider recovery adapter receives the exact stored request.
A crash after `model.call.started` creates no duplicate Context View, capsule,
Prompt Manifest, or model operation.

## L0-L4 acceptance

The release acceptance scenario runs six real SDK Runs in one Session with a
small model window and deterministic token estimates. It proves:

- automatic recommendations `L0, L1, L2, L3, L3, L4`;
- applied levels `L0, L1, L2, L2, L3, L4`;
- an invalid first L3 response persists an L3-to-L2 fallback and the Run still
  completes;
- later valid L3 and L4 compactions persist capsules;
- the first Run's original source event remains queryable through the final L4
  capsule;
- the final Prompt Manifest contains exactly
  `profile:general`, `application`, and `skill:demo`.

## Verification

```text
R3 core:
186 passed, 1 skipped

Provider, Tool, text-loop, and recovery:
294 passed

Task 3 public-event, Skill-preflight, and legacy SQLite compatibility:
48 passed

Workflow and subagent smoke:
38 passed

Release E2E:
3 passed

Ruff:
All checks passed

Strict mypy:
Success: no issues found in 93 source files

git diff --check:
clean
```

The skipped test is the existing optional tokenizer-backend case.

## Non-Task-4 failure classification

`tests/integration/runtime/test_recovery_api.py` was sampled separately and its
legacy seed helpers produced 115 failures because their stored execution
descriptors declare no Tools while `AgentSDK.for_test` enables built-ins by
default. The same default and recovery capability check predate Task 4. This
suite is not a Task 4 gate, and the capability validation was intentionally not
weakened. Dedicated provider and Tool recovery suites are fully green as
recorded above.
