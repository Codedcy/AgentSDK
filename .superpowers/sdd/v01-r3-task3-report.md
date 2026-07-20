# v0.1 R3 Task 3 Implementation Report

Status: DONE_WITH_CONCERNS

## Scope

Implemented only the Task 3 prompt/Skill/descriptor and persistence seam. The
RunEngine still sends its legacy checkpoint messages; ContextMiddleware and
per-model-call prompt preparation remain Task 4.

## TDD evidence

Initial RED:

```text
$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\integration\prompts\test_runtime_prompt.py tests\unit\runtime\test_execution_descriptors.py tests\integration\prompts\test_prompt_slice.py -q
ERROR tests/integration/prompts/test_runtime_prompt.py
  PromptManifestPersistence is absent
ERROR tests/unit/runtime/test_execution_descriptors.py
  ContextRuntimeConfig is absent
2 errors during collection
```

The first implementation run exposed two additional real boundaries:

- importing Context configuration through the eager `agent_sdk.context`
  package caused a runtime/context circular import;
- a Session-owned manifest event at sequence 1 collided with
  `session.created`.

The fixes place pure Context runtime configuration in
`agent_sdk.context_runtime` (re-exported through the public Context APIs) and
give each manifest event its own `manifest_id` event aggregate.

SQLite RED:

```text
test_prompt_manifest_survives_sqlite_reopen
ValueError: current snapshot kind is invalid
```

The current SQLite projection validator now recognizes a version-1
`prompt_manifest`, validates its identity, and requires its Context View to
belong to the same Session.

## Implemented behavior

- Added `ContextRuntimeConfig` with the planned defaults and bounds.
- Added `prompt_profile`, `system_prompt`, ordered unique nonempty `skills`,
  and `context` to `AgentSpec` and `DurableAgentSpec`.
- New fields participate in `agent_hash` and `descriptor_hash`.
- Canonically hashed legacy descriptors missing the Task 3 fields are
  validated first, upgraded to declared defaults, and rehashed in memory.
- Added `AgentSDKConfig.skill_roots`; SDK initialization creates and discovers
  one `SkillRegistry`, exposed as `sdk.skills`.
- Direct Run start activates every configured Skill before creating a Run or
  invoking the provider. Any Skill activation failure is normalized to
  non-retryable `invalid_state`.
- `PromptComposer` preserves Agent Skill order, rejects duplicate names, and
  adds immutable `skill:<name>` layers whose version is the Skill content hash
  and whose SHA-256 covers the activated instructions.
- Added `PromptManifest.manifest_id`.
- Added `PromptManifestPersistence`: atomically writes snapshot kind
  `prompt_manifest` and a `prompt.manifest.created` event containing only
  manifest/context/model/tool/layer identifiers and hashes.
- Public package exports include `ContextRuntimeConfig` and
  `PromptManifestPersistence`.

## Final verification

```text
$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\integration\prompts tests\unit\runtime\test_execution_descriptors.py tests\unit\test_core_config.py tests\integration\skills\test_skill_slice.py tests\integration\test_sdk_sqlite_test_constructor.py -q
89 passed, 1 skipped in 4.16s

$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\context tests\integration\context -q
102 passed in 3.62s

$ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk tests\integration\prompts tests\unit\runtime\test_execution_descriptors.py tests\unit\test_core_config.py
All checks passed!

$ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk\context src\agent_sdk\context_runtime.py src\agent_sdk\runtime\models.py src\agent_sdk\runtime\execution.py src\agent_sdk\prompts src\agent_sdk\config.py src\agent_sdk\api.py src\agent_sdk\storage\sqlite.py
Success: no issues found in 22 source files

$ git diff --check
clean (line-ending notices only)
```

The skipped test is the existing package-build test when `uv` is unavailable.

The Task 3 integration tests explicitly prove:

- ordered general/coding/application/Skill layers followed by Context
  messages at the compose/persist seam;
- full manifest snapshot provenance and SQLite reopen;
- public manifest events contain no profile, application, or Skill
  instruction text;
- missing configured Skill yields `invalid_state`, creates no Run, and makes
  zero provider calls;
- discovery runs exactly once during SDK initialization.

## Concern

An extra, non-required run of
`tests/integration/runtime/test_text_agent_loop.py` produced 1 failure and 32
passes: its first legacy assertion expects `tools=[]`, while the current SDK
default (already present at Task 3 base) enables the R1 built-in tools. The
failure is unrelated to this Task 3 diff and was not changed.

The actual RunEngine provider request is intentionally not rewired here.
Task 4 owns ContextMiddleware, exact request persistence, per-call preparation,
and recovery reuse.
