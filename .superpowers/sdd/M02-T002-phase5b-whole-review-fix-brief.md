# M02-T002 Whole Phase 5B Review-Fix Brief

Source of truth: the approved Phase 5 operational plan and Phase 5B briefs,
plus the fresh whole-Phase-5B read-only review at HEAD `b5b5fc8`. This repair
slice closes four Important functional findings. It must not enter Phase 5C,
T003, or T004.

## Finding 1 - cumulative terminal Model decisions

Public production histories such as:

```text
CNE / RETRY / confirmed Tool
-> later Model CONFIRM_COMPLETED(text or failed)
-> exact replay and Workflow terminal projection
```

must remain valid on Memory and SQLite. The current terminal helper decides
whether to accumulate effective evidence from the current result's ToolCall
shape, leaving earlier reconciliation markers unnormalized for no-Tool terminal
results.

Repair the terminal path so it:

1. authenticates and normalizes every resolved attempt before the current
   terminal decision in requested-event order;
2. authenticates the current terminal decision separately against its exact
   operation/evidence/projection;
3. sends one normalized history through the full terminal lifecycle/provider/
   Tool FSM;
4. keeps every old and current decision exactly replayable and Workflow-
   certifiable without external callbacks.

Cover CNE, RETRY, confirmed Tool, and prior Model ToolCall histories followed by
terminal Model text and failure; include partial streams, Memory/SQLite,
Workflow success/failure projection, exact replay of each decision, and orphan/
duplicate/moved/corrupt negative histories.

## Finding 2 - atomic Workflow terminal evidence

Workflow node terminal CAS currently binds Run/Session/parent/Workflow/node
snapshots but not the complete Run recovery evidence certified immediately
before the commit. A changed checkpoint, external operation, reconciliation
record, or Run event can therefore be projected after certification.

Add a narrow internal Store precondition for exact Run recovery evidence. The
certification result supplied to Workflow must carry a detached immutable token
containing the exact:

- Run checkpoint (including absence when appropriate);
- complete ordered external-operation set for the Run;
- complete ordered reconciliation-record set for the Run;
- complete ordered Run-event identity/content/cursor set used by certification.

Memory and SQLite must compare this token inside the same transaction as the
Workflow/node commit. Exact comparison includes set cardinality: missing,
changed, added, duplicated, or reordered evidence fails the batch. The token is
internal, adds no public API or schema migration, performs no callback, and is
not serialized into snapshots/events.

The terminal certifier returns the certified Run plus this token. After the
certification await, Workflow still reloads and validates Run/Session/parent/
descriptor relations; the node transition atomically applies existing exact
snapshot preconditions and the new evidence precondition. Ordinary live
Workflow transitions retain their existing Session/Workflow/node rules.

Cover Memory/SQLite barrier mutations after certification for checkpoint,
operation, reconciliation record, and every event dimension (changed, missing,
extra, moved). All must yield zero node/Workflow/session mutation and no
projection event. Retain legal two-SDK winner/follower, ambiguity, Session
closing/delete, terminal success/failure, parent, and supported cascade tests.

## Finding 3 - exact READY_FOR_MODEL relation

Confirmed Tool immediate replay and subsequent recovery must not trust a
post-decision `READY_FOR_MODEL` checkpoint by aggregate counts. Add one complete
`READY_FOR_MODEL` relation certifier, symmetric with the approved
`READY_FOR_TOOL` relation and shared by:

- confirmed-Tool exact replay;
- safe checkpoint/recovery admission;
- cumulative effective-history validation.

Reconstruct every turn from descriptor/history and cross exact messages,
assistant/Tool messages, output parts, cumulative usage, ordered ToolResults,
Model/Tool fingerprints/outcomes, permission/policy/capability evidence,
checkpoint phase/turn/version, and lifecycle ordering. The confirmed ToolResult
must exactly equal its operation outcome and Tool message content. Provider work
is forbidden until this relation passes.

Cover Memory/SQLite mutations of every checkpoint field, messages and Tool
message content/name/call id, output, usage, ToolResults, related operations and
events. Exact replay and explicit recovery both fail closed with zero Provider,
Tool, MCP, or permission calls. Legal multi-turn, partial-stream, all ToolResult
statuses, later resolution, and terminal paths remain accepted.

## Finding 4 - total strict Tool evidence validation

`_strict_tool_result` must be total for every public Python object: return a
detached `ToolResult` or `None`; never leak recursion, mapping-access, iterator,
serialization, or model-validation exceptions. All invalid evidence maps to the
existing constant nonretryable `INVALID_STATE / reconciliation decision is
invalid` with zero mutation.

Replace unbounded recursive raw-JSON checking with an iterative validator using
explicit depth and node/byte budgets, cycle detection, exact JSON container and
scalar types, finite numbers, string keys, and guarded Mapping/list access.
Do not coerce tuples, custom mappings, numeric strings, enums, or arbitrary
objects into JSON evidence. Preserve legal bounded nested JSON and deep source
detachment.

Cover very deep lists/objects, cyclic containers, custom Mapping methods that
raise, huge node counts, non-finite values, and legal values at each boundary.
All invalid cases have the same public error and no durable change on both
backends.

## TDD, gates, and report

For every finding, record production-path RED before changing implementation,
then focused GREEN and anti-regression negatives. Prefer shared existing
certifiers; do not add parallel lifecycle state machines.

Run:

- all new exact finding matrices;
- reconciliation/Store/Provider/Tool/RecoveryAPI focused gates;
- all Workflow recovery/admission/ownership/projection gates;
- Session/lease/Store/observability neighbors;
- full Python 3.13 with zero skips/failures;
- Ruff, mypy, imports, public signatures, root exports, SQLite schema, diff, and
  scope checks.

Update the relevant Phase 5B reports or create
`.superpowers/sdd/M02-T002-phase5b-whole-review-fix-report.md` with RED/GREEN,
changed files, exact gates, commit hashes, and residual Phase 5C scope. Commit
all work. A new independent whole-Phase-5B review must reach Spec C0/I0 and
Quality C0/I0 before Phase 5C.
