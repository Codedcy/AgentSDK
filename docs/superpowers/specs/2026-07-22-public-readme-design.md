# Public README Design

## Goal

Turn the repository README into the public entry point for Agent SDK v0.1. A
new Python SDK user should understand the product boundary, install it from the
repository, run the deterministic reference loop, and find the right detailed
guide without reading the implementation plans.

## Audience

The primary audience is an application developer evaluating or adopting the
SDK. Maintainer information is intentionally short and appears after user-facing
installation, examples, capabilities, and limitations.

## Truth Sources

README claims must agree with these repository sources:

- `pyproject.toml`: package name `agent-sdk`, version `0.1.0`, Python
  `>=3.12,<3.14`, and runtime dependencies.
- `examples/v01_reference.py`: supported reference arguments and the real
  deterministic `--smoke` contract.
- `docs/plans/releases/v0.1.md`: R0-R5 completion, release evidence, known
  boundaries, and the first v0.2 recommendation.
- `docs/guides/v01-quickstart.md`, `docs/guides/v01-recovery.md`, and
  `docs/guides/v01-tracing-and-analysis.md`: detailed public usage contracts.

The README must not imply that the package has been published to PyPI. The
primary installation path is clone plus `python -m pip install .`; editable
installation is shown only in the development section.

## Information Architecture

The README uses this order:

1. Product name, one-paragraph positioning, version/Python status.
2. A concise list of what makes this SDK useful.
3. Installation from the GitHub repository.
4. A five-minute deterministic SQLite smoke run with no provider/network use.
5. A minimal real LiteLLM-backed Agent Run.
6. A capability matrix for Agent Loop, persistence/recovery, Tools and
   permissions, MCP, Skills/prompts, Workflow, Child agents, Context L0-L4,
   Trace/evaluation/analytics, and extensibility.
7. Focused examples for authorization and generated Workflow admission.
8. Recovery and observability entry points.
9. Explicit v0.1 limitations and deferred v0.2 work.
10. Documentation navigation and maintainer verification commands.

The existing long-form explanations are compressed where detailed guides
already exist. README code must remain copyable and include every imported
symbol it uses.

## Public Positioning

The opening description is factual rather than aspirational:

- async Python SDK;
- default recoverable SQLite storage with an in-memory option;
- LiteLLM model gateway;
- normal Tool pipeline shared by built-ins, application Tools, MCP Tools, and
  Child-control Tools;
- validated Workflows with conditions and bounded loops;
- automatic Context L0-L4;
- live/historical Trace and deterministic evidence-based attribution.

No claim of production-grade distributed reliability, exactly-once external
effects, advanced causal analysis, or multi-worker coordination is allowed.

## Quickstart Flow

The first runnable flow is:

```powershell
git clone https://github.com/Codedcy/AgentSDK.git
Set-Location AgentSDK
python -m pip install .
python examples/v01_reference.py --smoke --database .agent-sdk/state.db --workspace .
```

It is described as deterministic and network-free. The README summarizes only
the public evidence it proves: automatic L0-L4, Workflow condition and bounded
loop, two-way Child communication/result consumption, live/historical Trace,
evaluation/attribution, safe completed-boundary reopen, Session deletion, and
workspace preservation.

The real-provider example uses `AgentSDK`, SQLite, `AgentSpec`, and a LiteLLM
model name. Credentials are supplied through the application environment; raw
credential fields in durable model parameters remain explicitly rejected.

## Capability Matrix

Each row states what v0.1 supports and its important boundary. The matrix must
distinguish shipped behavior from future work. In particular:

- Workflow supports validated definitions, explicit start, conditions, bounded
  loops, and agent nodes; generated text is only a candidate until the
  application compiles and confirms it.
- Child agents support Tool-driven spawn/send/list/wait plus direct API access,
  durable mailbox exchange, bounded limits, and parent result consumption.
- Analysis includes deterministic per-Run attribution, evaluation, success
  rate, and Tool failure metrics; cross-run multidimensional failure analysis
  and useless-result aggregation are v0.2 work.
- Recovery is single SDK instance/process in v0.1 and never claims exactly-once
  external effects.

## Safety and Permissions

The README keeps one compact application Tool example and one permission flow.
It states that built-in `read`, `write`, and `bash` enforce configured workspace
and command policies. `allow`, `ask`, and `deny` are application policy choices;
the reference smoke's permissive configuration is demo-only.

Generated Workflow execution follows compile, application confirmation, then
explicit start. This ordering is visible in the README rather than left only in
the detailed guide.

## Recovery and Observability

The README links the public calls rather than duplicating full guides:

- `sdk.trace.subscribe(...)` for live events;
- `sdk.trace.timeline(...)` and `sdk.trace.attribution(...)` for historical
  normalized evidence;
- `sdk.recovery.pending_requests(...)` and `sdk.recovery.resolve(...)` for
  interrupted work;
- terminal abort with `ReconciliationAction.TERMINATE`, which performs no
  provider or Tool replay and does not claim whether the interrupted external
  attempt executed.

Session deletion is stated to remove SDK-owned persisted history while
preserving application-owned workspace files.

## Limitations

The README has a visible `v0.1 boundaries` section containing:

- Python 3.12 and 3.13 only;
- no PyPI publication claim;
- one SDK instance in one process for the documented recovery model;
- no exactly-once guarantee for external effects;
- no automatic execution of unvalidated generated Workflows;
- aggregate cross-run Trace analysis, advanced scheduling, multi-worker
  recovery, exporters, and hardening remain post-v0.1 work.

## Documentation and Development

Navigation links include the quickstart, recovery guide, tracing/analysis
guide, high-level design, and v0.1 release ledger.

The development section uses a virtual environment and editable install with
development dependencies derived from the repository configuration. Verification
commands cover pytest, Ruff, and strict mypy. It records the current supported
full-suite evidence as `2,956 passed, 6 expected platform skips` without turning
that historical count into a permanent API guarantee.

No CI, coverage, PyPI, license, security, or compatibility badge is displayed
unless a corresponding repository or external service contract exists.

## Validation

Documentation tests must lock these high-risk README facts:

- version `0.1.0` and Python 3.12-3.13 support;
- clone/source installation rather than an unqualified PyPI install claim;
- deterministic `--smoke` command and SQLite arguments;
- links to all five required navigation targets;
- visible v0.1 boundaries including single-process recovery, no exactly-once
  effects, generated Workflow validation, and deferred aggregate Trace work;
- no unsupported badge or PyPI publication claim.

Run the focused documentation tests, Ruff on the documentation tests, and
`git diff --check`. The README change does not modify runtime code, so the
existing full v0.1 gate remains the runtime evidence unless a documentation
test exposes a contract mismatch.
