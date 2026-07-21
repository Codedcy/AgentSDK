# v0.1 Whole Review Blocker 2: Recovery Abort Report

Baseline: `4216268`

## Outcome

The public recovery API now accepts `ReconciliationAction.TERMINATE` for an
unknown in-flight external operation. The application must provide a bounded,
non-empty reason. The SDK resolves the exact pending request atomically and
terminally fails the Run with the stable, non-retryable error code
`application_resolution_aborted`, without calling the provider or Tool again.

The recorded external-operation outcome remains explicitly unknown. Abort does
not claim that the external operation completed, failed externally, or never
executed, and it does not add an exactly-once guarantee.

## Durable contract

- The resolution reason is normalized, must contain only `{"reason": str}`,
  rejects assignments to any key in the shared credential-key policy, and is
  limited to 256 UTF-8 bytes after sanitization.
- Actor metadata is canonical JSON, recursively rejects secret-bearing keys,
  rejects credential assignments in string values, and is limited to 1,024
  UTF-8 bytes. Authorization bearer values are rejected, not persisted in
  redacted form.
- The existing `commit_run_progress` transaction and CAS preconditions are
  reused for both Memory and SQLite. No new table, Store protocol, transaction,
  state machine, or persistence mechanism was introduced.
- One atomic batch resolves the reconciliation request, closes the external
  operation with `outcome_known: false`, writes a terminal checkpoint, fails the
  Run, updates Session ownership, and appends the legal event sequence:
  `reconciliation.resolved`, `step.failed`, `run.failed`, then Session detach or
  close.
- Abort intentionally emits neither `tool.call.failed` nor
  `model.call.failed`: the unknown external attempt is not reclassified as an
  observed external failure.
- Exact repetition is idempotent after Store reopen. A changed reason, actor, or
  action conflicts through the existing reconciliation contract and never
  replays the operation.
- Recovery trace projection permits the already-interrupted Run/Child stage to
  reach its final failed state and correlates the legacy terminal step event
  with its version-2 start event. No new trace stage, field, or attribution rule
  was added.

## Implementation shape and scope control

The production change is larger than a single action branch because the durable
resolution must be authenticated independently at three fail-closed boundaries:

1. runtime validation and construction of the canonical terminal projection;
2. Store-side validation of the complete atomic batch for both backends; and
3. closed-world history validation used to recognize an exact replay after
   process/Store reopen.

Those boundaries deliberately share one termination projection and one strict
batch validator. The remaining code at each boundary is specific to event
construction, persistence admission, or historical authentication and cannot be
collapsed without weakening the existing CAS/recovery model. The repair does
not introduce distributed recovery, new recovery actions, background workers,
or a broader trace redesign.

## TDD evidence

1. Initial focused RED: 14 expected failures across Memory and SQLite. Every
   terminate case failed at `reconciliation action is not supported`.
2. Core GREEN: 16 passed, 358 deselected. Coverage includes unknown Tool work,
   zero provider/Tool calls, event order, terminal Run/checkpoint/operation,
   sanitized reason and actor values, empty/oversized/secret-bearing metadata,
   reopen, exact repetition, and changed-resolution conflict.
3. Public trace RED then GREEN: abort initially appeared as internal-only
   because an interrupted version-2 stage was followed by a legacy terminal
   event. The recovery-compatible transition/correlation repair made public Run
   and Recovery stages observable; the focused projection checks pass.
4. Documentation RED then GREEN: the release ledger test first failed because
   README did not expose TERMINATE; it passes after README, quickstart, recovery
   guide, changelog, and release ledger were corrected.
5. The single v0.1 subprocess acceptance now exercises both the existing retry
   branch and a fresh-SQLite abort branch. Abort verifies terminal failure,
   exact-repeat idempotency, reopen behavior, and zero external replay.

## Secret-safety review correction

The initial abort implementation used a local, narrower credential-key list and
assignment regex. Consequently normalized shared-policy names such as
`client_secret`, `private_key`, and several hyphen/case variants could be
accepted in nested actor metadata or a reason assignment and become durable.

The correction exposes one internal `is_credential_key` predicate from the
existing model-parameter durability policy. Both model-parameter validation and
termination metadata now use that same normalized 16-key deny set, including
`authorization`; the
termination layer retains only its additional generic metadata names. Actor
metadata is checked recursively and with the existing depth/item bounds before
canonicalization. Reason and actor-string credential assignments are rejected
as a whole with the sanitized public error `reconciliation decision is
invalid`; no partial credential or authorization value is redacted and stored.

Strict public-API RED tests reproduced the defect for both Memory and SQLite:
the first normalized actor key was accepted and the next attempt observed a
conflict (`2 failed`). The GREEN test traverses all 16 shared credential names,
using mixed case, underscores, and hyphens, in both deeply nested actor keys and
direct `=` and quoted `:` reason assignments. The quoted-assignment extension
also produced a strict `2 failed` RED before the parser accepted optional key
quotes. The final test proves the sentinel is absent from error
`str`/`repr`, events, snapshots, complete Store state, and every SQLite database
sidecar byte stream.

## Authorization bearer re-review correction

The first secret-safety correction still treated `Authorization: Bearer ...` as
a redaction exception, while normalized authorization assignments using another
separator could follow the shared rejection path. Strict RED tests demonstrated
the mismatch: three normalized `authorization` variants were accepted by model
parameters, and public TERMINATE accepted an actor-string bearer assignment for
both Memory and SQLite (`5 failed, 15 passed`).

The final correction adds normalized `authorization` to the single shared
credential-key deny set and removes the termination-specific authorization key,
Bearer allow branch, and Bearer redactor. Nested actor headers and actor/reason
assignments using `:`, `=`, quoted keys, case, underscores, or hyphens are now
rejected before any durable mutation. The public error remains sanitized and
the sentinel is absent from exceptions, events, snapshots, Store state, and all
SQLite database/sidecar bytes.

## Verification

- Full reconciliation-resolution suite: `372 passed in 102.32s`.
- Public v0.1 release acceptance: `1 passed in 72.80s`.
- Model-parameter secret, observability, recovery, Workflow, Child, control,
  documentation, and public-release suites after the authorization correction:
  `768 passed in 285.32s`.
- Model-parameter secret plus terminate-focused selection:
  `59 passed, 358 deselected in 7.01s`.
- Strict authorization/shared-policy selection after its RED:
  `20 passed, 397 deselected in 3.52s`.
- Documentation release gate: `1 passed`.
- Ruff: `All checks passed!`
- Strict mypy: `Success: no issues found in 103 source files`.
- `git diff --check`: exit 0 (only Windows line-ending notices).

No version bump, tag, publish, or external side effect was performed.
