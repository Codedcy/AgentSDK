# Workflow

## 1. 目标

Workflow 为确定性编排层，用于把 Agent、Tool、审批、输入、并发、评估和子 Workflow 组合成可恢复状态机。它不替代 Agent Loop；`agent` 节点内部仍运行标准 Run。

## 2. 三种入口

- YAML/JSON DSL：适合保存、版本控制和对话生成。
- Python Builder：适合应用代码中的复用和类型提示。
- Conversation Generated：LiteLLM 以结构化输出生成 DSL。

三种入口都编译到不可变 Canonical Workflow IR；Executor 只接收 IR，不直接解释任意 YAML 或 Python 对象。

## 3. WorkflowDefinition

```yaml
api_version: agent-sdk/v1
kind: Workflow
metadata:
  name: coding-change
inputs: {}
outputs: {}
budgets: {}
nodes: []
edges: []
failure_policy: {}
```

Definition snapshot 保存 source、IR、Schema version、content hash、创建 actor 和审批信息。

## 4. 首版节点

- `agent`：启动 Agent Run，可引用 AgentSpec revision。
- `tool`：调用 Tool Registry 中的工具。
- `condition`：使用受限表达式读取结构化输入。
- `parallel`：启动多个分支并按 `all/any/quorum` join。
- `foreach`：对有界集合执行映射，必须有并发和项目上限。
- `approval`：发出 WorkflowApprovalRequested。
- `input`：发出 InputRequested，并按 Schema 校验答复。
- `evaluate`：运行 Evaluator，可作为质量门。
- `subworkflow`：调用固定 revision 的 Workflow。

首版不允许动态 DSL 携带 Python callback。应用逻辑通过注册 Tool、Evaluator 或预定义 Builder 组件接入。

## 5. 节点状态

```text
pending -> ready -> running <-> waiting
pending/ready -> skipped
running/waiting -> completed | failed | cancelled
```

每次 transition 检查 expected state/version，并在事务中写事件和投影。节点输出不可原地改写；重试 Attempt 产生新记录并引用同一 NodeRun。

## 6. 编译期验证

- DSL Schema 和 api_version。
- 唯一节点 id、边引用和入口/终点。
- 不可达节点、死分支和无消费输出诊断。
- 输入/输出 JSON Schema 兼容。
- Tool、AgentSpec、Workflow 和 Evaluator 引用存在。
- 循环必须通过显式 loop 节点表示，并声明最大迭代。
- foreach 项目数、并发、递归深度、Child 数和预算上限。
- condition 使用受限表达式 AST，禁止属性反射、I/O 和函数注入。
- 权限预检和风险评分。

验证结果包含 error/warning/info；error 阻止 IR 生成。

## 7. 对话生成和审批

流程固定为：

1. 模型输出符合 Workflow DSL Schema 的候选。
2. Compiler 运行静态验证和风险预检。
3. SDK 发出 `workflow.proposed`，包含摘要、节点、权限、预算、风险和验证诊断。
4. Run 进入 `waiting_approval`。
5. 应用收集用户批准、拒绝或修改意见。
6. 批准后冻结 Definition snapshot，再开始执行。

应用可定义低风险自动批准规则，例如：工具集合、路径范围、最大预算、无网络/写入。自动批准本身进入审计事件。

## 8. 执行与调度

- Scheduler 从投影计算 ready nodes。
- 并行节点受 Workflow 和 SDK 全局并发限制。
- 节点开始前再次检查预算、取消和权限；预检不等于最终授权。
- 每个节点有 timeout、retry/backoff、failure edge。
- 输出按 Schema 验证后写 Artifact/Projection。
- Restart 后依据节点状态恢复；已完成节点不重跑。

## 9. 失败语义

节点可配置：

- `fail_workflow`
- `follow_failure_edge`
- `continue_with_error`
- `retry`

首版不提供隐式 Saga。若要补偿，Definition 必须有显式 failure edge 调用补偿 Tool；补偿失败是可见的二次失败。

## 10. Coding Workflow Profile

SDK 提供可编辑模板：

```text
Explore -> Plan -> Approval -> Implement -> Verify -> Report
```

- Explore 可并行只读 Agent/Tool。
- Plan 输出结构化计划和风险。
- Approval 可被应用策略禁用或自动批准。
- Implement 可启动 Child Run。
- Verify 运行测试和 Evaluator。
- Report 汇总结果、证据、未完成项和 usage。

模板只是普通版本化 Workflow，不在 Runtime 写特殊分支。

## 11. 事件和查询

- `workflow.proposed|approved|rejected|started|completed|failed|cancelled`
- `workflow.node.ready|started|waiting|completed|failed|retried|skipped|cancelled`
- `workflow.branch.started|joined`

查询返回 Definition revision、当前节点、所有 NodeRun、依赖、等待原因、输出引用、预算和错误。

## 12. 验收标准

- YAML/JSON 和 Python Builder 对等生成相同 IR/hash。
- 非法引用、无界循环、任意代码和超预算在执行前拒绝。
- 动态 Workflow 未经批准不能执行，自动批准有审计证据。
- 进程在任意节点边界退出后可恢复，不重复已完成节点。
- 并发、retry、failure edge、approval/input 和 quality gate 有端到端测试。

