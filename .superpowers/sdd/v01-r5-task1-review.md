# v0.1 R5 Task 1 Independent Review

## Verdict

- Spec compliance: not approved
- Task quality: needs fixes
- Critical: 0
- Important: 6
- Minor: 2

## Important findings

1. `src/agent_sdk/observability/trace.py:145-158`: the stable-read check
   rereads only discovered snapshots and does not scan the event tail after the
   fixed high-water. A concurrently created Child can be omitted while the
   parent snapshot remains unchanged. Reuse the QueryService tree/tail stability
   strategy and test the race.
2. `src/agent_sdk/observability/stages.py:116-140`: known stage events do not
   enforce an allowed schema-version set. Unknown versions can create public
   facts. Reject unknown versions with a sanitized internal error and test it.
3. `src/agent_sdk/runtime/engine.py:951-965,2136-2149` and
   `src/agent_sdk/runtime/event_contracts.py:14-26,174-195`: recovery-time
   permission events use hashed request/tool strings, while automatic v2
   tagging makes the normalizer require a full request mapping/request_id/call_id.
   Define and validate a precise recovery permission contract or keep this event
   explicitly v1; cover a real recovery permission flow.
4. `src/agent_sdk/runtime/engine.py:583-586,644-657` and
   `src/agent_sdk/observability/stages.py:146-163,397-401`: real
   `tool.call.started` lacks `step_id`, so the Tool stage remains parented to the
   Run even when completion has the Step. Emit/validate a consistent Step ref and
   add a real Tool timeline integration test.
5. `src/agent_sdk/observability/stages.py:55-59` and
   `src/agent_sdk/subagents/service.py:192-204`: real Child lifecycle does not
   emit the `child.*` events that the projector expects, so actual public Child
   execution has no CHILD stage. Add bounded lifecycle events or a strictly
   verified deterministic Child projection, plus public-path integration coverage.
6. `src/agent_sdk/models/litellm_gateway.py:119-130`: provider cost conversion
   does not catch `OverflowError`; invalid huge cost can fail an otherwise valid
   model call. All invalid cost values must become `None` and must not block a
   valid fallback cost. Test huge integers, NaN, infinity, negative and bool.

## Minor findings

1. `model.usage.reported` contributes usage but not evidence IDs/cursors, so
   ordering can ignore the true first evidence. Include it in bounded evidence
   and cursor maintenance.
2. Integration/contract coverage is too synthetic. Extend it for the stable-read
   race, real Tool/Child paths, recovery permission, unknown versions and invalid
   cost.
