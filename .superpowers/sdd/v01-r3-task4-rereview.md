# v0.1 R3 Task 4 Fix Re-review

## Verdict

- Reviewed range: `ca52ffb..3a4b65f`
- Spec: PASS
- Quality: PASS
- Critical: 0
- Important: 0
- Minor: 0
- Approval: APPROVED

Both Important findings in `v01-r3-task4-review.md` are closed. No new
Critical, Important, or Minor finding was identified in the fix range.

## I1 closure — strict prepared-request protocol

Status: CLOSED.

Independent code inspection confirmed:

- every durable request requires at least one message;
- roles are closed to `system`, `user`, `assistant`, and `tool`;
- role-specific allowed and required fields are enforced;
- ordinary assistant text, nullable assistant content paired with Tool calls,
  exact assistant function Tool-call envelopes, and Tool-result correlation ids
  match the shapes emitted by the runtime;
- Tool definitions require the exact function envelope, a non-empty name, and
  mapping parameters;
- recursive canonical JSON validation still rejects non-string keys,
  non-finite values, unsupported values, top-level extras, and alias mutation;
- legacy operations remain valid when all three prepared-request fields are
  absent.

The positive test covers the four runtime roles, optional names, assistant Tool
calls, Tool results, and a registered provider Tool schema. The negative matrix
contains independent malformed variants rather than asserting implementation
internals. Fresh focused execution passed all 86 Task 4 model/recovery tests.

The durable SQLite loader's strict decoder and canonical-record comparison
continue to reject duplicate JSON keys. Direct Pydantic parsing is not the
supported persistence boundary and does not weaken that path.

## I2 closure — authenticated Context and Prompt references

Status: CLOSED.

Independent code inspection confirmed:

- prepared model operations authenticate their Run and Session ownership;
- the Context View and Prompt Manifest snapshots must both exist and validate;
- snapshot ids must equal the operation references;
- the Context View must belong to the recovered Session;
- the Prompt Manifest must link to that exact Context View and model;
- an atomic no-write commit applies exact snapshot-data and Session-owner
  preconditions, detecting replacement between read and authentication;
- `start_model` adds Session-owned Context View and Prompt Manifest snapshot
  preconditions to the same progress commit that records the operation and
  public `model.call.started`;
- legacy model operations without prepared fields do not require these
  snapshots.

Authentication is centralized at the recovery planning boundary, the validated
request boundary, and pending-reconciliation loading. Thus provider recovery,
resend, reconciliation resolution/replay, terminal certification, safe
checkpoint certification, and completed model history all receive evidence
that has passed the same authentication. Importantly, authentication precedes
the Tool-in-flight compatibility fallback, so corrupt completed model evidence
cannot be converted into a reconciliation request.

The Memory/SQLite corruption matrix exercises missing snapshots, owner
mismatch, internal id mismatch, and Manifest-to-View mismatch. It verifies
recovery conflict with zero provider and Tool calls. These are real StateStore
backends and service entry points, not mocked authentication results.

The completed-model recovery test uses a real completed model followed by a
cancelled safe-retry Tool. On reopen it proves:

- the old model operation and references are preserved and read;
- corrupt old evidence fails before the Tool or provider;
- valid old evidence permits exactly one recovered Tool execution;
- the subsequent new model call creates a different View and Manifest;
- durable View/Manifest event counts increase from one pair to exactly two.

This closes both the old-reference recovery gap and the missing
crash-after-completed-call acceptance branch.

## Regression and safety assessment

- Public `model.call.started` and `prompt.manifest.created` shapes were not
  expanded with raw messages, prompts, model parameters, or Tool schemas.
- Existing closed-world event checks still require the public started-event
  references and fingerprint to match the authenticated operation.
- The Task 3 prompt, Skill preflight, and legacy run-created v1/v2 suite remains
  green.
- Workflow and subagent integration smoke remains green.
- The fix does not add Task 5 release-ledger behavior or broaden R3 scope.
- The previously documented `test_recovery_api.py` built-in-Tool capability
  mismatch is unchanged and remains project-level release-suite debt, not a
  Task 4 fix regression.

## Fresh independent verification

```text
Task 4 focused model/recovery tests:
86 passed

R3 Context, Prompt, reconciliation, and release E2E:
221 passed, 1 skipped

Provider, Tool, text-loop, and recovery regressions:
294 passed

Task 3 compatibility plus Workflow/subagent smoke:
86 passed

Ruff:
All checks passed

Strict mypy:
Success: no issues found in 93 source files

git diff --check:
clean

worktree before review artifact:
clean
```

The single skip is the existing optional tokenizer-backend test.

