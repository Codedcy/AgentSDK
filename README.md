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
