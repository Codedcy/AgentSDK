# v0.1 R5 Task 2 Second Re-review

## Verdict

- Critical: 0
- Important: 1
- Minor: 0
- Spec compliance: not approved
- Task quality: needs fixes

## Remaining finding

`src/agent_sdk/observability/attribution.py:123,624`: `_terminal_event`
unconditionally resolves `stage.last_cursor`. For a Tool stage still in RUNNING
or WAITING, that event is the start rather than a result, so an interrupted
unknown Tool outcome is incorrectly exposed as `unused`. A non-terminal Tool has
no result and must be `supporting`; resolve a terminal event only for terminal
stage statuses or verify the transition kind explicitly. Add an interrupted
in-flight Tool contributor regression.
