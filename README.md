# Agent SDK

This repository contains the recoverable, observable Python Agent SDK described in
`docs/`. The M01 reference CLI is a public-API consumer and validation example; it is
not a production control server.

```powershell
uv run --python 3.13 python -m examples.reference_cli.main `
  "Write result.txt and return an approved two-node Workflow YAML" `
  --database .agent-sdk/state.db `
  --workspace . `
  --model openai/gpt-4o-mini
```

The CLI asks before permissioned Tool calls and before executing generated Workflow
YAML. It reaches a quiescent boundary before closing SQLite. In-flight Run recovery,
cancellation, and reconciliation are M02 capabilities; the M01 E2E acceptance harness
verifies quiescent reopen and Session deletion separately.

Applications can make Session creation retry-safe and explicitly complete the normal
lifecycle after all owned Runs and Workflows reach terminal state:

```python
from pathlib import Path

from agent_sdk import AgentSDK, AgentSDKConfig


sdk = AgentSDK(AgentSDKConfig(database_path=Path(".agent-sdk/state.db")))
session = await sdk.sessions.create(
    workspaces=[Path(".")],
    idempotency_key="create-coding-session",
)

# Start and await application Runs or Workflows here.

await sdk.sessions.close(session.session_id)
await sdk.sessions.delete(session.session_id)
await sdk.close()
```

Normal deletion removes SDK-owned durable state and idempotency records for the
Session. Configured workspace files remain application-owned and are preserved.
