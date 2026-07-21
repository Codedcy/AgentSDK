# v0.1 Whole Review Blocker 1: Durable Secret Rejection Report

Baseline: `19e3982`

## Outcome

Raw credential-bearing keys in `AgentSpec.model_params` are rejected before
registry serialization, execution-descriptor creation, Session/Store access,
provider dispatch, Tool execution, or any durable Run side effect. The error is
the stable, sanitized non-retryable `invalid_state` message:

`model params must not contain credential-bearing keys`

No secret resolver, secret store, or `SecretRef` feature was added.

## Contract

- Validation is recursive over exact built-in dictionaries, lists, tuples, and
  the SDK's frozen mapping proxies.
- Key matching is exact after case folding and removing underscores/hyphens.
- The v0.1 deny set is: `api_key`, `api_secret`, `api_token`, `access_token`,
  `auth_token`, `bearer_token`, `client_secret`, `application_secret`,
  `secret_access_key`, `aws_secret_access_key`, `azure_ad_token`, `credentials`,
  `service_account`, `private_key`, and `password`.
- Substring matching is deliberately not used; `max_tokens`, token budgets, and
  response token-count fields remain valid.
- Traversal is iterative and bounded to depth 64 and 10,000 aggregate container
  entries. Cycles fail closed. Unsupported custom mappings/objects are rejected
  without invoking their mapping protocol.
- The same validator protects `AgentSpec`, `DurableAgentSpec`, `AgentRegistry`,
  `ExecutionDescriptor.create`, and the first line of public `RunAPI.start`.

## TDD evidence

1. Initial RED: 3 failures. Direct/nested `api_key` did not raise; public start
   reached Store/session loading and returned `failed to load session`.
2. Initial GREEN: 3 passed.
3. Credential-key RED: 13 failures for the additional normalized deny names;
   GREEN: 18 passed including non-credential token parameters.
4. Bounds/custom-object RED: depth and item limits did not reject, and Pydantic
   executed the custom Mapping iterator; GREEN: 21 passed.
5. Durable-boundary RED: `DurableAgentSpec` accepted a secret and registry /
   descriptor serialization executed a bypassed custom Mapping; GREEN: 25 passed.
6. `api_secret` RED: one failure; GREEN after adding the exact normalized key.
7. Cycle RED: recursive freezing raised `RecursionError`; GREEN after fail-closed
   cycle detection.
8. Final focused GREEN: 28 passed, including SQLite raw-byte and durable-record
   sentinel checks.

The two pre-existing privacy tests that used credential-shaped keys as opaque
test markers now use non-credential marker keys; their original traceback and
public-event redaction assertions are unchanged.

## Fresh verification

- Affected persistence/recovery command: `454 passed in 109.59s`.
  It covered the focused tests, execution descriptors, Prompt persistence,
  atomic live progress, Context recovery, public recovery API, Workflow Session
  ownership/recovery admission, and provider recovery.
- Ruff: `All checks passed!`
- Strict mypy: `Success: no issues found in 102 source files`
- `git diff --check`: exit 0 (only existing Windows line-ending notices).

No version bump, tag, publish, or external side effect was performed.
