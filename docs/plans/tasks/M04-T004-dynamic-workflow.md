# M04-T004 Dynamic Workflow and Coding Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate, validate, review, freeze, and execute workflows during a conversation, and ship a Claude Code-like coding workflow profile.

**Architecture:** LiteLLM structured output proposes DSL only. The normal compiler validates it, a risk analyzer produces a preflight report, and the application resolves approval before an immutable workflow snapshot is attached to the Run.

**Tech Stack:** LiteLLM, Pydantic v2, workflow compiler/executor, pytest-asyncio.

## Global Constraints

- Generated content never bypasses compiler, policy, budgets, or permission checks.
- Generated workflows require approval by default; low-risk auto-approval is explicit configuration.
- The approved canonical IR and all fingerprints are frozen before execution.
- The coding profile is a normal public workflow template, not privileged runtime code.

---

### Task 1: Implement generated workflow proposal and preflight

**Files:**
- Create: `src/agent_sdk/workflow/generation.py`
- Create: `src/agent_sdk/workflow/risk.py`
- Modify: `src/agent_sdk/workflow/service.py`
- Create: `tests/integration/workflow/test_generation.py`

- [ ] **Step 1: Write failing generation safety tests**

```python
@pytest.mark.asyncio
async def test_generated_workflow_is_compiled_before_approval(generator, invalid_model) -> None:
    proposal = await generator.propose("change the database")
    assert proposal.status == "invalid"
    assert proposal.diagnostics
    assert generator.approval_bridge.call_count == 0

@pytest.mark.asyncio
async def test_high_risk_proposal_requires_approval(generator, mutating_model) -> None:
    proposal = await generator.propose("delete generated files")
    assert proposal.risk.maximum >= RiskLevel.HIGH
    assert proposal.status == "awaiting_approval"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/workflow/test_generation.py -v`

Expected: generation, diagnostics, and preflight are missing.

- [ ] **Step 3: Implement structured proposal pipeline**

```python
async def propose(self, objective: str, context: GenerationContext) -> WorkflowProposal:
    draft = await self._models.complete_structured(
        messages=self._prompt(objective, context),
        schema=WorkflowDocument,
    )
    try:
        ir = self._compiler.compile(draft)
    except WorkflowValidationError as error:
        return WorkflowProposal.invalid(draft=draft, diagnostics=error.diagnostics)
    preflight = self._risk.analyze(ir, context.capabilities)
    return WorkflowProposal.awaiting_decision(ir=ir, preflight=preflight)
```

Preflight lists tools, effects, path/command patterns, MCP servers, skills, child agents, waits, budgets, unavailable capabilities, and static permission outcomes.

- [ ] **Step 4: Implement approval/rejection/auto-approval and frozen snapshots**

Approval accepts the exact proposal fingerprint. Any edit recompiles and creates a new proposal. Auto-approval requires configured maximum risk, no unresolved capability, and no static deny/ask outside the configured envelope.

```python
async def approve(self, proposal_id: str, fingerprint: str) -> FrozenWorkflow:
    proposal = await self._proposals.get(proposal_id)
    if proposal.fingerprint != fingerprint:
        raise WorkflowConflict("proposal changed before approval")
    frozen = FrozenWorkflow(ir=proposal.ir, fingerprint=fingerprint, approved_at=self._clock.now())
    return await self._workflows.store_frozen(frozen)

def can_auto_approve(proposal: WorkflowProposal, policy: AutoApprovalPolicy) -> bool:
    return proposal.risk.maximum <= policy.maximum_risk and not proposal.preflight.unresolved and not proposal.preflight.requires_decision
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/integration/workflow/test_generation.py -v`

Expected: invalid, rejected, edited, approved, auto-approved, tampered, and restart cases pass with full provenance.

```powershell
git add src/agent_sdk/workflow/generation.py src/agent_sdk/workflow/risk.py src/agent_sdk/workflow/service.py tests/integration/workflow/test_generation.py
git commit -m "feat: add safe dynamic workflow generation"
```

---

### Task 2: Ship the coding workflow profile and quality gates

**Files:**
- Create: `src/agent_sdk/workflow/templates/coding.yaml`
- Create: `src/agent_sdk/profiles/coding.py`
- Create: `examples/coding_workflow/main.py`
- Create: `tests/e2e/test_coding_workflow.py`

- [ ] **Step 1: Write failing end-to-end profile test**

```python
@pytest.mark.asyncio
async def test_coding_profile_follows_approved_phases(coding_example, repo_fixture) -> None:
    result = await coding_example.run("add a greeting function", workspace=repo_fixture.path)
    assert result.workflow.completed_nodes == ["explore", "plan", "approve", "implement", "verify", "report"]
    assert repo_fixture.tests_pass()
    assert result.outputs["report"]["changed_files"]
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/e2e/test_coding_workflow.py -v`

Expected: template/profile are missing.

- [ ] **Step 3: Implement the public template**

```yaml
schema_version: "1"
name: coding
version: "1"
entry: explore
nodes:
  - {id: explore, kind: agent, profile: coding}
  - {id: plan, kind: agent, output_schema: coding_plan}
  - {id: approve, kind: approval}
  - {id: implement, kind: agent, profile: coding}
  - {id: verify, kind: evaluate, evaluator: command}
  - {id: repair_loop, kind: loop, max_iterations: 3}
  - {id: report, kind: agent, output_schema: coding_report}
edges:
  - {source: explore, target: plan}
  - {source: plan, target: approve}
  - {source: approve, target: implement}
  - {source: implement, target: verify}
  - {source: verify, target: report, condition: "evaluation.passed"}
  - {source: verify, target: repair_loop, condition: "not evaluation.passed"}
  - {source: repair_loop, target: implement}
```

Bound implement→verify repair loops, require explicit approval before mutations by default, and make verification commands application-configurable.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/e2e/test_coding_workflow.py -v`

Expected: phase order, approval, edit, retry gate, verification, report, events, and usage assertions pass using public APIs only.

```powershell
git add src/agent_sdk/workflow/templates src/agent_sdk/profiles examples/coding_workflow/main.py tests/e2e/test_coding_workflow.py
git commit -m "feat: ship coding workflow profile"
```
