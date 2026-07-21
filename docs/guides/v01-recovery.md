# Agent SDK v0.1 recovery

v0.1 recovery is conservative. Durable events, snapshots, execution descriptors,
and checkpoints let a reopened SDK continue only from a certified boundary.
Completed model/Tool/Workflow work is read from SQLite and is not repeated merely
because the application reopened the database.

## Scope and guarantees

- Recovery is supported by one SDK instance in one process. v0.1 does not provide
  multi-process worker coordination, distributed leases, or a recovery service.
- There is no exactly-once guarantee for external effects. A process can stop after
  an external provider or Tool performed an effect but before the SDK durably
  recorded the outcome.
- The SDK therefore never silently guesses an unknown in-flight outcome. It marks
  the Run `interrupted` and creates a durable reconciliation request.
- Application workspace contents are outside SDK transactionality and are not
  deleted with Session history.

## Safe-boundary reopen

Open the same database with the same Agent, Tool, Skill, policy, and Workflow
descriptors. Register those capabilities before recovery. Then scan or recover:

```python
sdk = AgentSDK(AgentSDKConfig(database_path=database))
sdk.agents.define(agent_spec)
sdk.tools.register(tool_spec, handler)
await sdk.recovery.scan()
handle = await sdk.recovery.recover_run(run_id)
```

If the checkpoint is safe, `await handle.result()` resumes from that boundary. A
completed Run returns its durable result without calling the provider or Tool.
Descriptor mismatches fail closed; restore the exact capability set rather than
editing SQLite.

## Unknown in-flight operation

`await handle.result()` reports recovery required until the application resolves
every pending request:

```python
from agent_sdk import ReconciliationAction

request = (await sdk.recovery.pending_requests(run_id))[0]
await sdk.recovery.resolve(
    request.request_id,
    ReconciliationAction.RETRY,
    actor={"type": "operator", "id": "user-123"},
    evidence={"acknowledge_duplicate_side_effect_risk": True},
)
result = await (await sdk.recovery.recover_run(run_id)).result()
```

Choose an action from evidence, not convenience:

- `CONFIRM_COMPLETED` records a provider/Tool outcome only with the required
  trustworthy completion evidence.
- `CONFIRM_NOT_EXECUTED` permits one explicit retry when evidence establishes the
  operation did not execute.
- `RETRY` is the risk-accepting path. For external effects, acknowledge that a
  duplicate may occur; prefer idempotency keys and effect-specific verification.
- `TERMINATE` is the terminal abort path. It requires a bounded, non-empty
  application reason, performs no provider/Tool replay, and atomically fails the
  Run with `application_resolution_aborted`:

  ```python
  await sdk.recovery.resolve(
      request.request_id,
      ReconciliationAction.TERMINATE,
      actor={"type": "operator", "id": "user-123"},
      evidence={"reason": "application chose not to retry"},
  )
  ```

  The resolution records that the application aborted recovery. It does not claim
  that the unknown external attempt completed, failed, or never executed.

Always store meaningful `actor` and bounded `evidence` fields. Re-read pending
requests after conflicts: resolution is durable and compare-and-set protected.
Repeating the exact abort returns the same durable resolution; a different reason
or action conflicts and never replays the operation. Do not run two SDK instances
against the same recoverable work.

## Shutdown and deletion

Stop admitting new work, allow owned Runs/Workflows to settle, close the Session,
then close the SDK. Delete a Session only when its Runs and Workflows are terminal.
`sdk.sessions.delete` removes SDK-owned history; it intentionally leaves configured
workspace files untouched.
