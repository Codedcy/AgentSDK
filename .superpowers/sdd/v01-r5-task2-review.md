# v0.1 R5 Task 2 Independent Review

## Verdict

- Critical: 0
- Important: 4
- Minor: 1
- Spec compliance: not approved
- Task quality: needs fixes

## Important findings

1. `src/agent_sdk/observability/trace.py:143`: Run-root attribution ignores
   `root_run.workflow_run_id`, so real Workflow failure/control evidence is not
   selected and `workflow_loop_limit` is unreachable through the public API.
   Authenticate/load the bound Workflow and add a real bounded-loop integration.
2. `src/agent_sdk/observability/attribution.py:493`: Child message consumption
   does not enforce recipient=parent or message cursor before the referencing
   parent Context. Index message direction/cursor and require strict later use.
3. `src/agent_sdk/observability/attribution.py:392` and
   `src/agent_sdk/observability/models.py:147`: non-stage Manifest evidence IDs
   can reach public output without a common element bound. Validate all selected
   event/evidence IDs and add element-level bounds to public models.
4. `src/agent_sdk/observability/attribution.py:460`: only the root Run's final
   successful Model is terminal. Each completed Child Run's final successful
   Model must also be terminal; Child contributor separately describes whether
   the parent consumed the Child result/message.

## Minor finding

- The projector repeatedly scans Models/events/Contexts for each contributor.
  Precompute per-Run final Models, message direction/cursor, and Context consumer
  indexes while fixing the Important findings to avoid O(M^2 + C*(E+V)).
