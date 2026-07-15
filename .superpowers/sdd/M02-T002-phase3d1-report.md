# M02-T002 Phase 3D1 Implementation Report

## Outcome

PASS. Phase 3D1 adds application-certified provider recovery for an exact
durable Model operation. Normal Model execution still routes only through
LiteLLM. Recovery calls an application-registered adapter only when the
operation's pre-call adapter stamp, current registry entry, reconstructed
request fingerprint, execution descriptor, checkpoint, and operation identity
all match exactly. Every other case keeps the Phase 3C2 unknown-outcome path and
admits one bounded reconciliation request.

The pre-change baseline was `1272 passed in 107.24s` on Python 3.13.14 at
`9cc8e5eb5ec77b42e351904b5f346006eb6bb4e8` with a clean worktree.

## Implemented contract

- Added strict, frozen, extra-forbidden public contracts:
  `ProviderRecoveryAdapter`, `ProviderRecoveryDisposition`,
  `ProviderRecoveryRequest`, and `ProviderRecoveryResult`.
- Added one SDK-local registry exposed through
  `sdk.recovery.register_adapter`, `unregister_adapter`, `get_adapter`, and
  `list_adapters`. There is no default or built-in provider implementation.
- Live Model start resolves the registry before the existing durable
  `model.call.started` boundary. A matching adapter stamps exact adapter id,
  version, and certified query/resend flags; no adapter preserves the
  conservative two-false legacy metadata.
- Recovery reconstructs a detached `ModelRequest` from the durable execution
  descriptor and checkpoint, verifies the same request fingerprint, and passes
  the original durable operation id to query or resend.
- A fresh coordinator lease atomically re-fences the same STARTED operation and
  writes a bounded `model.recovery.query.started` or
  `model.recovery.resend.started` audit event. The Store now permits this
  STARTED-to-STARTED transition only inside `RunProgressBatch` with the exact
  linked in-flight checkpoint precondition; the standalone transition API
  remains started-to-terminal only.
- Certified completed text and one-Tool-call outcomes reuse the normal RunEngine
  completion path. Certified failure atomically terminalizes operation,
  checkpoint, Run, and Session ownership with a stable public error.
- Pending, unknown, not-executed without resend certification, invalid values,
  adapter failure, and timeout create one reconciliation request and never call
  LiteLLM for the recovered turn.
- Adapter invocation has an injectable real `asyncio.timeout`, shielded task
  settlement, lease heartbeat, SDK-close cancellation, and repeated-cancel-safe
  lease release. Adapter exceptions and arbitrary result values are reduced to
  bounded categories before durable or public observation.

## TDD and fault evidence

The implementation was developed through failing tests before production
changes. Material RED-to-GREEN findings included:

1. Public imports and registry/model validation initially failed because no
   provider recovery surface existed; strict detached contracts and sorted
   expected-identity registration semantics made the new unit suite green.
2. Live operations initially lacked certified metadata; registry resolution was
   moved before the durable Model-start commit while the no-adapter shape stayed
   conservative.
3. Recovery execution initially had no certified branch; exact descriptor,
   metadata, fingerprint, checkpoint, operation, and registry matching were
   added before any adapter call.
4. Real SQLite recovery exposed a redundant `lease_generation` column that was
   not updated with the operation JSON. The same transaction now updates both,
   so close/reopen completed and failed outcomes remain authoritative.
5. SDK close initially waited for the adapter timeout. A lifecycle close waiter
   now cancels and settles the coordinator and adapter while leaving the
   re-fenced STARTED operation recoverable.
6. A lease-loss fault initially allowed the adapter call after the audit commit.
   A fresh lease assertion at that boundary now prevents the external call.
7. Direct or checkpoint-free STARTED-to-STARTED transitions initially succeeded.
   Memory and SQLite now require an exact composite in-flight checkpoint
   precondition and reject direct re-fencing without partial mutation.

Focused coverage includes registry detachment and imports; live stamping;
Memory and real SQLite close/reopen; completed text, Tool, and failed outcomes;
unknown/malformed/mismatched evidence; real injected timeout; 20 same-SDK plus
two-SDK query/resend convergence; checkpoint, operation, and event CAS faults;
precommit and ambiguous commit; lease loss and takeover; Session delete; caller
cancellation; SDK close during query and resend; exact crash retry; and
secret/traceback/task-retention checks.

## Fresh final-code gates

All commands used
`C:\Users\10176\AppData\Roaming\Python\Python314\Scripts\uv.exe` with
Python 3.13.

- Phase 3D1 focused/fault/e2e plus Store re-fence:
  `182 passed in 7.65s`.
- Phase 3C2 focused: `89 passed in 67.05s`.
- Phase 3C1 scanner/admission: `115 passed in 6.79s`.
- Phase 3B live progress: `38 passed in 3.43s`.
- Phase 3A Run-progress transaction: `123 passed in 6.83s`.
- Phase 2 recovery models/records/SQLite validation:
  `139 passed in 7.59s`.
- Phase 1 + M02-T001 regressions: `188 passed in 13.61s`.
- Session/Run/Tool/MCP/permission/Workflow/child compatibility:
  `225 passed in 9.23s`, plus Run/Session ownership
  `39 passed in 4.08s`.
- Full Python 3.13 pytest: `1337 passed in 103.77s`; zero skipped.
- Ruff: `All checks passed!`.
- Mypy: `Success: no issues found in 74 source files`.
- Public import smoke: all four public provider recovery types import from
  `agent_sdk` and appear in `agent_sdk.__all__`.
- `git diff --check`: exit 0; only Windows LF-to-CRLF informational warnings.

## Scope audit

Production changes are limited to the brief's permitted surface:

- `src/agent_sdk/runtime/provider_recovery.py`
- `src/agent_sdk/runtime/recovery.py`
- `src/agent_sdk/runtime/engine.py`
- `src/agent_sdk/api.py`
- `src/agent_sdk/__init__.py`
- `src/agent_sdk/storage/memory.py`
- `src/agent_sdk/storage/sqlite.py`

Tests are limited to three provider recovery files plus the existing
Run-progress Store contract. This report and the SDD progress ledger are the
only documentation changes.

The explicit forbidden-scope diff is empty for migrations, the LiteLLM gateway,
Workflow production, roadmap, milestones, and task index. SQLite schema version
remains exactly 3. No built-in provider adapter, arbitrary provider evidence,
reconciliation resolution action, Tool retry production change, or Workflow
recovery behavior was added.

## Handoff and residual risks

- Certification is application-owned. A registered adapter's correctness and
  provider-side idempotency remain outside the SDK trust boundary.
- Registration after a crash cannot retroactively certify an unstamped call;
  legacy or mismatched metadata deliberately reconciles.
- An unresolved or invalid certified response deliberately stops at the existing
  reconciliation boundary. Resolution actions, Tool retry certification, and
  Workflow recovery remain Phase 3D2 or later work.
- No tests were weakened or skipped. A fresh independent reviewer must approve
  Spec and Quality with C0/I0 before Phase 3D2 begins.
