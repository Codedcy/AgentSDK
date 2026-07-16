# M02-T002 Phase 5C Fault/E2E and Release Brief

Source of truth: `docs/plans/tasks/M02-T002-leases-reconciliation.md`,
`.superpowers/sdd/M02-T002-phase5-plan.md`, and the approved Phase 5A/5B
outcomes. This slice completes only M02-T002. It must not implement M02-T003 or
the M02-T004 cancellation/force-delete contract.

## Goal

Prove that the production recovery path remains conservative across a real
process death, complete the explicit reconciliation E2E story, and certify the
package on every supported Python version. The proof must use SQLite durability
and observable cross-process side effects rather than an in-process exception
or a fabricated `INTERRUPTED` snapshot.

## Test architecture

- Add a small subprocess fault harness under `tests/`. Each child must create or
  reopen a real SQLite database, use the public SDK execution/recovery surface,
  and terminate abruptly with `os._exit` at a named boundary. Do not rely on
  `AgentSDK.close()` or task cancellation to simulate the crash.
- Prefer test-only fault collaborators (Provider/Tool handlers and Store
  delegates) over public production hooks. A private production seam is allowed
  only if the exact post-side-effect/pre-commit boundary is otherwise
  unreachable; it must be minimal, undocumented as public API, and covered by
  ordinary behavior tests.
- Record Provider, Tool, and MCP effects in a separate append-only artifact so
  the parent can count them after the child dies. The marker write must occur at
  the external acceptance/side-effect boundary, not after the SDK commits its
  durable outcome.
- Use a bounded child timeout, capture stdout/stderr for diagnosis, require the
  expected exit code/marker, and keep all paths and payloads deterministic.
- Reopen SQLite in a fresh SDK instance. Read the durable lease, set the
  production recovery scanner's controlled clock beyond its expiry, and invoke
  the public scan/recovery APIs. The test must not directly set the Run to
  `INTERRUPTED` or synthesize a reconciliation request.

## Required hard-exit scenarios

1. **Unknown Provider outcome.** The Provider records acceptance and the SDK has
   durably recorded the exact STARTED Model operation, then the child exits
   before its outcome/checkpoint commit. Scanning must admit one pending Model
   reconciliation request. Recovery must not invoke the Provider again by
   default. An explicit legal resolution followed by explicit recovery must be
   demonstrated without duplicating the original Provider acceptance.
2. **Unknown Tool side effect.** The Tool (and one MCP-backed Tool variant)
   records its side effect, then the child exits before the Tool outcome and
   safe checkpoint commit. Scanning must admit one pending Tool reconciliation
   request. Recovery must not invoke Tool/MCP again by default. Demonstrate an
   explicit legal resolution and the resulting Workflow/Run projection without
   duplicating the side effect.
3. **Committed safe Tool outcome.** The Tool outcome and READY_FOR_MODEL
   checkpoint are committed atomically, then the child exits before the next
   Model call or terminal projection. Scanning and explicit recovery must resume
   from the safe checkpoint, never call the Tool/MCP again, and finish exactly
   once.

If an exact crash point is naturally after an atomic batch rather than between
its members, assert that the whole batch is visible and classify it as safe;
never weaken production atomicity just to create a test point.

## E2E and lifecycle matrix

- Exercise explicit `CONFIRM_NOT_EXECUTED`, `RETRY`, and kind-appropriate
  `CONFIRM_COMPLETED` decisions through public APIs. `TERMINATE` remains rejected
  and belongs to M02-T004.
- Prove recovered Workflow node and Workflow terminal projections, including
  one confirmed external outcome and one safe checkpoint resume.
- Prove Session closing retains owned interrupted/reconciling work and ordinary
  delete remains blocked until ownership is naturally detached. Do not add
  force-delete behavior.
- Assert that resolution itself invokes no Provider, Tool, MCP, or permission
  callback and that default recovery never duplicates an unknown external
  effect.
- Use SQLite for every real process-death proof. Where lifecycle/projection
  behavior is not process-specific, retain the Memory/SQLite matrix.
- Audit existing integration helpers that directly construct `INTERRUPTED`
  state. Any E2E case intended to prove scanner behavior must instead enter that
  state through the production scanner. Lower-level storage validation fixtures
  may continue to seed exact snapshots when their subject is atomic validation,
  not crash recovery.

## TDD and scope rules

- Add the failing fault/E2E tests first and record the RED reason in the report.
- Make the smallest implementation change needed for the GREEN outcome, then
  run focused regressions after each scenario.
- Keep public signatures, SQLite schema version 3, migration SQL, Store protocol,
  and M02-T001 behavior unchanged unless the approved task contract strictly
  requires otherwise. Any unavoidable change must be called out for independent
  review before proceeding.
- Do not add automatic replay of unknown Model, Tool, or MCP work. Do not expose
  secrets or raw exception text in durable events, errors, or subprocess output.

## Required release gates

Run from the linked implementation worktree with the explicit workspace `uv`
executable and zero pytest skips:

- focused Phase 5C fault/E2E tests and all M02-T002 recovery/reconciliation/
  Workflow regressions;
- the full suite on Python 3.12 and Python 3.13;
- Ruff on `src`, `tests`, and supported examples, plus strict mypy on the package;
- `git diff --check`, package-root import/export validation, source-scope audit,
  and SQLite schema-version/hash checks;
- build both sdist and wheel into an external temporary directory;
- install the wheel into clean Python 3.12 and 3.13 environments, then validate
  package version, root exports, representative recovery contracts, and imports;
- run `python -m examples.reference_cli.main --help` in the clean installs and
  prove help exits successfully without opening a Store or invoking a model.

Temporary build/install artifacts must remain outside the worktree and be
removed after verification.

## Deliverables and review

- Commit the fault/E2E implementation and tests in reviewable units.
- Write `.superpowers/sdd/M02-T002-phase5c-report.md` with RED/GREEN evidence,
  exact gate commands/results, versions, counts, hashes, and final clean status.
- Obtain an independent Phase 5C Spec and Quality review with C0/I0, followed by
  a fresh independent whole-M02-T002 review with C0/I0.
- Only after both approvals, update the M02-T002 task/progress ledgers to
  complete and move M02-T003 to in progress. Do not implement M02-T003 here.
