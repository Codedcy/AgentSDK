# M02-T002 Phase 4A Review-Fix Brief

Fix only the three independently reproduced Important findings from the review
of `8c22fa0..90d5e8f`. Source contracts remain
`M02-T002-phase4-plan.md`, `M02-T002-phase4a-brief.md`, and the M02-T002 Step 4
Workflow recovery paragraphs. Do not begin Phase 4B and do not add a Workflow
lease/schema/state.

## Finding I1 - Capability admission TOCTOU

RED must deterministically block the Session ownership load, mutate an Agent or
Tool registry during that await, then release it. Recovery must fail with
`recovery capabilities unavailable` while preserving the exact pending
Workflow/node snapshot, event cursor, Session ownership, and zero Run/provider/
Tool/MCP work.

The fix must validate one coherent live capability view after every awaited
ownership read and again immediately before Workflow transition/selected-Run
creation dispatch. A descriptor check that precedes an await is not a final
preflight. If registry mutation cannot currently expose a generation/snapshot,
use exact immutable capability values plus synchronous final revalidation at
the no-await boundary; do not invent a durable Workflow scheduler lease.

## Finding I2 - Child durable parent is unauthenticated

RED matrix must include missing, foreign-session, wrong Workflow/node/Agent,
legacy or descriptor-substituted, non-COMPLETED, and output/usage projection-
mismatched parent Runs. Every case is zero child Run write and zero external
work.

Before creating or recovering a child selected Run, load the previous durable
parent Run and require:

- exact Run id from the completed previous node;
- same Session and Workflow, previous node id and Agent revision;
- current compatibility and exact per-parent-node ExecutionDescriptor;
- parent/root relation for that previous node;
- durable `COMPLETED` status with valid result;
- output text and usage exactly equal the previous completed node projection.

The check must be repeated at the final child create boundary after any await.

## Finding I3 - Normal-live and explicit recovery do not converge

Add deterministic two-SDK REDs for both boundaries:

1. pending node selected but Run missing; normal and explicit recovery race
   exact Run creation while Provider is blocked;
2. selected Run already exists; normal and explicit recovery race Run lease and
   terminal node/Workflow projection.

Required GREEN behavior:

- one logical selected Run and one Provider/Tool/MCP side effect;
- no synthetic node/Workflow failure from Run-create conflict, lease loss, or
  Workflow/node projection CAS loss;
- both attached handles either return the same durable success or one returns
  the stable `recovery required` diagnostic while durable state stays active;
- after winner completion, a CAS loser reloads and converges to durable terminal
  state instead of surfacing `workflow state changed concurrently`;
- normal execution must preserve existing public idempotency-record counts.

Use a shared create/ensure primitive that makes the exact selected Run
idempotent without adding a normal-execution application idempotency record.
An explicit recovery key may remain for crash repair, but normal-vs-recovery
concurrency must converge on the same authoritative Run snapshot and exact
relation. Handle ambiguous commit by reloading and accepting only the exact
selected Run; never accept a foreign/substituted Run.

## Verification and handoff

- Record all three strict REDs before the fix and exact GREEN results after.
- Run the complete Phase 4A file, explicit Run-id tests, Workflow recovery/
  ownership, Run recovery/ownership, Provider/Tool recovery neighbors, Ruff,
  mypy, diff/import/scope/schema, and full Python 3.13 with zero skips.
- Update `M02-T002-phase4a-report.md` with review findings, fixes, and fresh
  counts; create no approval claim.
- Commit the fix and return the hash for a new independent review.
