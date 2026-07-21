# v0.1 R5 Task 2 Re-review

## Verdict

- Critical: 0
- Important: 2
- Minor: 1
- Spec compliance: not approved
- Task quality: needs fixes

## Remaining findings

1. `src/agent_sdk/observability/trace.py:228,348`: Run-bound Workflow
   selection includes sibling node events, and binding checks node id without
   verifying the node's run id. Keep Workflow-wide failure/control facts but
   include node events only when `payload.run_id` belongs to the selected Run
   tree; authenticate node id plus run id. Add a public two-node regression.
2. `src/agent_sdk/observability/stages.py:635` and
   `src/agent_sdk/observability/attribution.py:615`: filtering an overlong
   terminal event id from public stage evidence makes `_terminal_event` fall
   back to the start event, silently changing consumed/unused semantics and hint
   evidence. Resolve terminal facts by internal cursor/event indexes; filter
   only public evidence output. Add a completion-id-too-long regression with a
   later Context reference.

## Minor finding

- Precompute running external stages per Run instead of scanning every stage for
  each interruption.
