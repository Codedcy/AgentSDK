# v0.1 R2 Task 2 Implementation Report

## Scope

Implemented only R2 Task 2, “Compile Nested Definitions into v2
Instructions”.

- Added immutable condition/loop definition models and the discriminated
  `WorkflowStep` union.
- Added exactly-one-of validation for nested `steps` versus legacy
  `nodes`/`edges`.
- Added schema-v2 `WorkflowInstruction` and `WorkflowIR` program models.
- Added a deterministic label-and-patch compiler for Agent, condition, bounded
  loop, jump, and complete instructions.
- Promoted valid legacy sequential definitions to schema-v2 sequential
  programs.
- Added the schema-v2 durable instruction/IR descriptor branch.
- Preserved schema-v1 graph validation, canonical JSON, definition hashes, and
  descriptor serialization without adding empty v2 fields.

No workflow control state, reducer, persistence transition, executor behavior,
acceptance scenario, or later R2 task was implemented.

## TDD Evidence

### RED

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/workflow/test_control_compiler.py tests/unit/workflow/test_workflow_compiler.py -q
```

Result before implementation:

```text
FFFF..FF.F............................                                   [100%]
7 failed, 31 passed in 2.91s
```

The failures showed the intended missing capabilities: nested `steps` and
`inputs` were rejected, legacy compilation still returned schema 1, and the
control-depth and loop-iteration compiler settings did not exist.

### GREEN

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/workflow/test_control_compiler.py tests/unit/workflow/test_workflow_compiler.py tests/unit/runtime/test_execution_descriptors.py -q
```

Result:

```text
.........................................................                [100%]
57 passed in 3.17s
```

The exact sample program is:

| pc | id | op | target(s) |
|---:|---|---|---|
| 0 | `choose` | `branch` | true 1, false 3 |
| 1 | `draft` | `agent` | agent `draft` |
| 2 | `choose:then:join` | `jump` | 4 |
| 3 | `reject` | `agent` | agent `reject` |
| 4 | `improve` | `loop_check` | true 7, false 5 |
| 5 | `review` | `agent` | agent `review` |
| 6 | `improve:body:back` | `jump` | 4 |
| 7 | `finish` | `agent` | agent `finish` |
| 8 | `complete` | `complete` | — |

The Agent table is exactly `draft`, `reject`, `review`, `finish`, once each.
Equivalent YAML, JSON text, and Python models produce identical canonical bytes
and hashes.

## Compatibility and Validation Evidence

Schema branching is explicit in both public and durable IR models:

- schema 1 hashes only `schema_version`, `name`, `nodes`, and `edges`, using the
  existing canonical sequential-graph validation;
- schema 2 hashes `schema_version`, `name`, frozen `inputs`, `nodes`, and
  `instructions`, with empty `edges`;
- empty schema-v2-only fields are excluded from schema-v1 serialization, so
  persisted descriptor content is not reinterpreted;
- the schema-v1 canonical byte/hash regression test passes unchanged.

Additional compiler coverage includes globally unique nested step IDs,
non-empty required bodies, control-depth overflow, loop-limit overflow,
arbitrary YAML tags, unknown expression operators, safe YAML parsing, frozen
inputs, stable targets, and legacy chain promotion.

Existing Workflow integration regression:

```text
253 passed in 42.03s
```

from:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/workflow -q
```

Static verification:

```text
Success: no issues found in 3 source files
All checks passed!
```

from:

```powershell
.\.venv\Scripts\python.exe -m mypy --strict src/agent_sdk/workflow/models.py src/agent_sdk/workflow/compiler.py src/agent_sdk/runtime/execution.py
.\.venv\Scripts\python.exe -m ruff check src/agent_sdk/workflow/models.py src/agent_sdk/workflow/compiler.py src/agent_sdk/runtime/execution.py tests/unit/workflow/test_control_compiler.py tests/unit/workflow/test_workflow_compiler.py tests/unit/runtime/test_execution_descriptors.py
```

`git diff --check` is clean. Self-review found no out-of-scope production
changes.
