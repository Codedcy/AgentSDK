# v0.1 R3 Task 4 Independent Review

## Verdict

- Spec: FAIL
- Quality: FAIL
- Critical: 0
- Important: 2
- Minor: 0
- Approval: BLOCKED until both Important findings are fixed and independently
  re-reviewed.

Reviewed range: `224219b..2ea0464`.

## Important findings

### I1 — Stored prepared requests do not reject malformed message and Tool shapes

The new `_ModelRequestPayload` validates only that `messages` and `tools` are
sequences of mappings. It does not validate a provider-message shape or a Tool
schema shape. Consequently all of the following deserialize successfully:

```python
{"model": "m", "messages": [{}], "tools": [], "params": {}, "purpose": "agent_loop"}
{"model": "m", "messages": [{"role": "bogus", "content": "x"}], "tools": [], "params": {}, "purpose": "agent_loop"}
{"model": "m", "messages": [{"role": "tool", "content": "x"}], "tools": [{}], "params": {}, "purpose": "agent_loop"}
```

Evidence:

- `src/agent_sdk/runtime/reconciliation.py:133-162` declares arbitrary
  `Mapping[str, Any]` entries without a shape validator.
- `src/agent_sdk/runtime/reconciliation.py:186-211` reconstructs a
  `ModelRequest` after only that shallow validation.
- `tests/unit/runtime/test_reconciliation_models.py:137-181` covers an extra
  top-level field, a non-sequence container, and NaN, but not malformed
  message/Tool entries.

The durable SQLite path does reject duplicate JSON keys and non-canonical
records via strict decoding/canonical comparison; the unsupported direct
`BaseModel.model_validate_json` behavior is therefore not a separate finding.
Non-string keys, non-finite numbers, top-level extras, alias mutation, and exact
canonical fingerprinting are otherwise covered.

Impact: a malformed but canonical stored prepared request can pass operation
validation and enter provider recovery/reconciliation. This violates Task 4's
strict malformed-payload fail-closed requirement at the exact-recovery trust
boundary.

Required fix:

- add a closed validator for every persisted message and Tool entry while
  preserving the provider protocol shapes the runtime actually emits;
- add negative tests for missing/invalid roles, invalid Tool protocol fields,
  malformed Tool schema entries, and nested non-string/non-finite values;
- retain legacy operation loading when all three new fields are absent.

### I2 — Context View and Prompt Manifest references are not authenticated during recovery

`ModelCallOperation` requires the three new fields to be all present or all
absent, but it validates only that both ids are non-empty. The request
fingerprint covers the exact prepared model request, not either reference.
Recovery uses the stored request and validates model, Tools, params, purpose,
and fingerprint, but never loads the referenced snapshots to prove:

- the Context View exists and belongs to the operation Session;
- the Prompt Manifest exists and belongs to the operation Session;
- the Manifest's `context_view_id` equals the operation's `context_view_id`;
- the public `model.call.started` provenance references the same authenticated
  pair.

Evidence:

- `src/agent_sdk/runtime/reconciliation.py:285-343` performs all-or-none,
  non-empty, model, and request-fingerprint checks only.
- `src/agent_sdk/runtime/recovery.py:4392-4441` reconstructs and authenticates
  the request but never resolves either reference.
- There are no Context View / Prompt Manifest lookups in
  `src/agent_sdk/runtime/recovery.py`.
- `tests/integration/context/test_context_recovery.py` proves stable ids and an
  exact stored request, but has no missing, cross-Session, swapped-Manifest, or
  mismatched-View corruption case.

Impact: exact provider execution can be recovered, but its public provenance
can be reassigned to unrelated durable context/prompt evidence while all
current operation and event checks still pass. That makes the trace attribution
untrustworthy, contrary to Task 4's id-binding requirement.

Required fix:

- authenticate the referenced View and Manifest, their Session ownership, and
  the Manifest-to-View link before any prepared-request recovery,
  reconciliation, resend, or terminal certification;
- fail closed without a provider or Tool call on missing/cross-owner/mismatched
  references;
- add both memory and SQLite corruption tests;
- add the planned crash-after-completed-model recovery test and assert that the
  subsequent new model call creates exactly one new View and Manifest while
  the completed call creates neither duplicate.

## Requirements evidence

### Runtime middleware and protocol

- `ContextMiddleware.prepare` plans from the durable checkpoint, composes
  prompt layers, persists the Manifest, and returns detached messages.
- `RunEngine._execute_owned` invokes it only in the new-model branch immediately
  before `start_model`; recovered completed model results bypass it.
- The two-call Tool integration test proves two distinct Views, View-before-
  model ordering, Tool-result consumption by the second request, and a clean
  user/assistant/Tool checkpoint.
- Normal completed-call progression proves a new View on the following call,
  but the plan's corresponding crash/recovery branch still lacks the explicit
  assertion required under I2.

### Context levels and evidence

- The runtime planner applies automatic L0-L4 selection, `allow_lossy` capping,
  L3/L4 fallback to deterministic L2, over-budget events, and protected/current
  retention through the Task 1/2 implementation.
- Source extraction is Session-filtered and excludes current-Run event copies
  before appending checkpoint messages, preventing the reviewed same-Session
  duplicate path.
- Capsule retrieval remains Session-scoped and recursive.

### Prompt and public-event safety

- The Prompt Manifest is persisted before `model.call.started`.
- Public `prompt.manifest.created` and `model.call.started` payloads contain
  ids/hashes/model metadata only; no raw system prompt, prepared messages,
  model params, or Tool schemas were added.
- Prepared request snapshots are frozen and detached; canonical fingerprints
  include model, messages, Tools, params, and purpose.
- Task 3 legacy run-created compatibility tests remain green.

### Recovery compatibility

- Prepared operations use the exact stored request; legacy operations without
  the three fields retain descriptor/checkpoint reconstruction.
- Provider authoritative recovery receives the exact request.
- Closed-world model/Tool history validators use the new public started payload
  shape and prepared-first request reconstruction.
- In-flight recovery does not create a second View, capsule, Manifest, or model
  operation.
- The known prepare-before-start orphan window remains the documented v0.1
  limitation and does not weaken exact recovery after `model.call.started`.

### Acceptance scope

- The E2E scenario drives recommendations L0, L1, L2, L3, L3, L4 and applied
  levels L0, L1, L2, L2, L3, L4.
- It proves invalid L3 fallback, valid L3/L4 capsules, recursive evidence back
  to the first Run, and final general/application/Skill Manifest layers.
- No Task 5 release-ledger implementation is present in the reviewed range.

## Fresh verification

```text
Task 4 focused + reconciliation + release E2E:
54 passed

Context and prompt suites:
136 passed, 1 skipped

Provider, Tool recovery, and text loop:
168 passed

Workflow/subagent integration smoke:
21 passed

Ruff:
All checks passed

Strict mypy:
Success: no issues found in 22 source files

git diff --check:
clean
```

The sampled `tests/integration/runtime/test_recovery_api.py` still has the
pre-existing built-in-Tool capability mismatch described in the Task 4 report
(`5 passed` before the first three failures under `--maxfail=3`). The Task 4
diff does not change the capability gate that raises those failures, so it is
not counted as a Task 4 finding. It remains a project-level release-suite debt
that Task 5 must not silently present as a fully green repository.
