# v0.1 whole-review Blocker 4 implementation report

Baseline: `d837362`

## Scope delivered

- Replaced the reference program's forced Context calls with six ordinary public
  Runs and result-derived first occurrences of automatically applied L0-L4.
- Replaced the single-node Workflow with a compiled candidate containing a
  selected condition and a bounded loop that executes twice before completion.
- Added an Agent-driven Child control sequence through the ordinary public Tools:
  spawn, parent-to-Child message, list, Child-to-parent message, wait, and a later
  parent model call that consumes the terminal Child result.
- Starts a live Session subscription before work, then reads the normalized
  historical parent timeline after evaluation.
- Derives evaluation, deterministic attribution, message consumption, and all
  extended one-line JSON fields from public results or durable public events.
- Closes and reopens SQLite at completed boundaries using a provider that fails on
  any replay; it also compares model/Tool start counts before and after reopen.
- Closes/deletes the Session and proves an application-owned workspace marker
  survives.
- Keeps normal mode on `AgentSDK(AgentSDKConfig(...))` and LiteLLM environment
  credentials. Deterministic smoke injects a LiteLLM-shaped provider and opens no
  network connection.
- Updated README, quickstart, changelog, and release ledger. No runtime feature,
  package version, tag, or publish state changed.

## TDD and debugging evidence

Initial focused RED:

```text
2 failed in 4.04s
KeyError: 'condition_selection'
AssertionError: 'force_level' is contained here
```

The first combined run then hung. Stage-bounded direct execution established that
the public reference flow itself completed in 6.2 seconds. The hang came from the
network test replacing the entire `socket.socket` class, which interfered with the
event loop/dependency cleanup. The test now blocks only `connect`/`connect_ex`.

Direct output also exposed two evidence-join errors: a `context.view.created`
event uses the View id as its event `run_id`, not the Agent Run id. Both automatic
levels and parent mailbox consumption now join through
`model.call.started.context_view_id`. A fresh direct smoke then completed in 5.5
seconds with L0-L4, two messages, and Child-result consumption derived as true.

## Fresh gates

```text
$ pytest tests/e2e/test_v01_reference_example.py -q
...                                                                      [100%]
3 passed in 10.34s

$ pytest tests/e2e/test_v01_reference_example.py tests/docs/test_v01_release_ledger.py -q
.......                                                                  [100%]
7 passed in 10.08s

$ pytest tests/e2e/test_v01_release.py -q
.                                                                        [100%]
1 passed in 72.66s (0:01:12)

$ ruff check src examples/v01_reference.py tests/e2e/test_v01_reference_example.py tests/e2e/test_v01_release.py tests/docs/test_v01_release_ledger.py
All checks passed!

$ mypy --strict src/agent_sdk
Success: no issues found in 103 source files

$ git diff --check
clean
```

The development wheel built successfully as
`agent_sdk-0.1.0.dev0-py3-none-any.whl`. A newly created Python 3.12 virtual
environment installed that wheel and its dependencies. With `PYTHONPATH` cleared,
the same source reference imported `agent_sdk` from that environment's
`site-packages` and emitted exactly one JSON line:

```json
{"attribution_method":"deterministic_event_evidence_v1","child_result_consumed":true,"child_status":"completed","condition_selection":"then","context_levels":["L0","L1","L2","L3","L4"],"evaluation_verdict":"pass","live_subscription_observed":true,"loop_iterations":2,"message_count":2,"run_status":"completed","safe_reopen_no_replay":true,"session_deleted":true,"trace_stage_count":32,"workflow_status":"completed","workspace_preserved":true}
```

The clean environment and its database/workspace were deleted after the gate.
