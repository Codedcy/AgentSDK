# M04-T001 Workflow DSL and Compiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compile YAML/JSON DSL and Python Builder definitions into the same validated, immutable Workflow IR.

**Architecture:** External syntax is parsed into discriminated node models, normalized into canonical IR, statically validated, and fingerprinted. Expressions use a restricted AST interpreter; arbitrary Python evaluation is forbidden.

**Tech Stack:** Pydantic v2, PyYAML, Python AST, Hypothesis, pytest.

## Global Constraints

- DSL and Builder parity is verified against canonical IR bytes.
- Workflow versions are immutable after a Run starts.
- Graph validation rejects missing edges, duplicate ids, illegal cycles, invalid joins, and incompatible ports.
- Expression evaluation has no imports, calls, attribute traversal, or mutation.

---

### Task 1: Define complete node models and Builder parity

**Files:**
- Modify: `src/agent_sdk/workflow/models.py`
- Modify: `src/agent_sdk/workflow/builder.py`
- Modify: `src/agent_sdk/workflow/dsl.py`
- Create: `tests/unit/workflow/test_dsl_builder_parity.py`

**Interfaces:**
- Produces: agent, tool, condition, parallel, foreach, loop, approval, input, evaluate, and subworkflow nodes.
- Consumes: YAML, JSON, and Python Builder definitions.

- [ ] **Step 1: Write failing parity tests**

```python
def test_yaml_and_builder_compile_to_identical_ir() -> None:
    yaml_ir = WorkflowCompiler().compile_yaml(WORKFLOW_YAML)
    builder_ir = WorkflowCompiler().compile_builder(WORKFLOW_BUILDER)
    assert yaml_ir.canonical_json() == builder_ir.canonical_json()

@pytest.mark.parametrize("kind", ["agent", "tool", "condition", "parallel", "foreach", "loop", "approval", "input", "evaluate", "subworkflow"])
def test_every_node_kind_round_trips(kind, node_fixture) -> None:
    assert parse_node(node_fixture(kind)).model_dump(mode="json")["kind"] == kind
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/workflow/test_dsl_builder_parity.py -v`

Expected: complete node union and parity are missing.

- [ ] **Step 3: Implement discriminated models and canonical serialization**

```python
class WorkflowNode(BaseModel):
    id: str
    kind: str
    retry: RetryPolicy = RetryPolicy()
    timeout_seconds: float | None = None
    on_failure: str | None = None

Node = Annotated[
    AgentNode | ToolNode | ConditionNode | ParallelNode | ForEachNode |
    LoopNode | ApprovalNode | InputNode | EvaluateNode | SubworkflowNode,
    Field(discriminator="kind"),
]

class WorkflowIR(BaseModel):
    schema_version: Literal["1"] = "1"
    name: str
    version: str
    entry: str
    nodes: tuple[Node, ...]
    edges: tuple[WorkflowEdge, ...]

    def canonical_json(self) -> bytes:
        return canonical_json(self.model_dump(mode="json", exclude_none=True))
```

- [ ] **Step 4: Implement Builder methods as model constructors**

Every Builder method returns a new Builder value or appends one typed node, then delegates final output to the same compiler normalization path used by YAML/JSON.

```python
def tool(self, node_id: str, tool: str, arguments: Mapping[str, Any]) -> "WorkflowBuilder":
    node = ToolNode(id=node_id, tool=tool, arguments=dict(arguments))
    return replace(self, nodes=self.nodes + (node,))

def edge(self, source: str, target: str, *, condition: str | None = None) -> "WorkflowBuilder":
    return replace(self, edges=self.edges + (WorkflowEdge(source=source, target=target, condition=condition),))

def build(self) -> WorkflowIR:
    return WorkflowCompiler().compile(WorkflowDocument(name=self.name, version=self.version, entry=self.entry, nodes=self.nodes))
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/unit/workflow/test_dsl_builder_parity.py -v`

Expected: all node kinds and representative full workflows have byte-identical IR.

```powershell
git add src/agent_sdk/workflow/models.py src/agent_sdk/workflow/builder.py src/agent_sdk/workflow/parser.py tests/unit/workflow/test_dsl_builder_parity.py
git commit -m "feat: define complete workflow ir"
```

---

### Task 2: Implement static validation and safe expressions

**Files:**
- Modify: `src/agent_sdk/workflow/compiler.py`
- Create: `src/agent_sdk/workflow/expressions.py`
- Create: `tests/unit/workflow/test_validation.py`
- Create: `tests/property/test_workflow_graphs.py`

- [ ] **Step 1: Write failing graph and expression tests**

```python
@pytest.mark.parametrize("fixture", ["missing_edge", "duplicate_id", "illegal_cycle", "bad_join", "unknown_port"])
def test_invalid_graph_is_rejected(load_workflow, fixture) -> None:
    with pytest.raises(WorkflowValidationError):
        WorkflowCompiler().compile(load_workflow(fixture))

@pytest.mark.parametrize("expression", ["__import__('os')", "value.__class__", "open('x')", "items.append(1)"])
def test_unsafe_expression_is_rejected(expression) -> None:
    with pytest.raises(UnsafeExpressionError):
        compile_expression(expression)
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/workflow/test_validation.py tests/property/test_workflow_graphs.py -v`

Expected: static graph and expression validation are incomplete.

- [ ] **Step 3: Implement graph validation**

```python
def validate_graph(ir: WorkflowIR) -> None:
    by_id = {node.id: node for node in ir.nodes}
    if len(by_id) != len(ir.nodes):
        raise WorkflowValidationError("duplicate node id")
    if ir.entry not in by_id:
        raise WorkflowValidationError("entry node does not exist")
    for edge in ir.edges:
        if edge.target not in by_id:
            raise WorkflowValidationError(f"missing edge target: {edge.target}")
    validate_cycles(by_id, ir.edges, allowed_only_through_bounded_loop=True)
    validate_parallel_joins(by_id)
    validate_ports(by_id)
```

- [ ] **Step 4: Implement an allowlisted expression interpreter**

Permit constants, names, dict/list indexing, boolean operations, comparisons, arithmetic, and explicitly registered pure functions only. Evaluate against immutable input/output maps with node/time limits.

```python
ALLOWED_NODES = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Subscript, ast.List, ast.Tuple, ast.Dict, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div)

def compile_expression(source: str) -> SafeExpression:
    tree = ast.parse(source, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise UnsafeExpressionError(type(node).__name__)
    return SafeExpression(tree)
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/unit/workflow/test_validation.py tests/property/test_workflow_graphs.py -v`

Expected: generated graph cases either compile to valid canonical IR or return stable diagnostic codes; unsafe syntax always fails closed.

```powershell
git add src/agent_sdk/workflow/compiler.py src/agent_sdk/workflow/expressions.py tests/unit/workflow/test_validation.py tests/property/test_workflow_graphs.py
git commit -m "feat: validate workflow graphs and expressions"
```
