# v0.1 R5 Task 1 Re-review

## Verdict

- Critical: 0
- Important: 1
- Minor: 1
- Spec compliance: not yet approved
- Task quality: needs fixes

## Remaining findings

1. `src/agent_sdk/runtime/engine.py:749-764` and
   `src/agent_sdk/observability/stages.py:564-585`: a legitimate recovery-time
   permission event remains exact schema v1 with hashed `request` and `tool`
   mappings. The projector cannot derive a stable permission entity id from the
   `sha256` reference, so a real recovery permission timeline fails with a
   sanitized INTERNAL error. Extract the same bounded deterministic hash id for
   requested/resolved without rewriting v1 payloads, and cover a real recovery
   followed by `sdk.trace.timeline`.
2. `src/agent_sdk/observability/stages.py:246-268`: when
   `model.usage.reported` precedes a terminal Model event and the start event is
   absent, pending usage is attached as a value but its cursor/event id is not
   merged into first/last cursor and evidence. The terminal-only stage must use
   the usage event as its first evidence.
