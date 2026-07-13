# Subagents

## 1. 模型

子 Agent 是标准 `Run`，通过 `parent_run_id` 和可选 `workflow_node_id` 加入父执行树。它不使用特殊的轻量执行器，因此复用同一 Agent Loop、存储、权限、Trace、压缩和 Evaluation。

默认隔离 Context。父 Agent 必须提交显式 `TaskEnvelope`，不能让 Child 自动读取父 Prompt 全文。

## 2. 创建入口

- 应用通过公共 SDK 启动 Child Run。
- Workflow `agent` 节点启动 Child Run。
- Agent 在系统允许时调用内部 `spawn_agent` 能力提出子任务。

Agent 驱动的 spawn 仍受并发、深度、预算和权限限制；若超限返回结构化拒绝。

## 3. TaskEnvelope

```python
TaskEnvelope(
    objective="...",
    success_criteria=[...],
    instructions=[...],
    context_refs=[...],
    input_data={...},
    allowed_tools=[...],
    allowed_skills=[...],
    workspace_scopes=[...],
    budgets=ChildBudgets(...),
    expected_output_schema={...},
)
```

必填 objective 和 success criteria。Context refs 指向 Session events、Artifacts、文件或父 Capsule；创建时解析并生成 Child Context View snapshot。

## 4. Context 隔离

Child 默认只接收：

- TaskEnvelope。
- SDK Core/Profile Prompt。
- 明确允许的 Tool/Skill Catalog。
- 指定 context refs 的内容或摘要。
- 自己产生的消息、工具结果和 Capsule。

它不继承父 Agent 的隐式思考、完整消息历史、无关工具结果或所有 activated skills。父可配置增加继承项，但每项进入 Prompt Manifest。

## 5. 权限继承

Child 的有效权限：

```text
parent effective policy
INTERSECT task envelope limits
INTERSECT child AgentSpec limits
```

- Child 只能收窄现有 allow，不能扩大。
- 父 Session 的持久用户规则仍需满足任务包限制。
- Child 遇到 ask 时通过同一 Permission Broker 请求用户决定。
- 用户针对 Child 选择 run scope 时只作用于该 Child Run；session/persistent scope 仍受原始资源规则约束。

## 6. Scheduler

限制层级：

- SDK 全局并发。
- Session 并发。
- Parent 最大同时 Child 数。
- 最大递归深度和累计 Child 数。
- token、成本、时间、Step 和 ToolCall 预算。

Scheduler 使用公平队列，持久记录 queued reason。父等待 Child 时进入 `waiting_children`，但不占用执行 lease 的活跃计算资源。

禁止 Parent/Child/Workflow 形成等待环。创建依赖边时检查环；运行时 watchdog 检测长时间无状态变化并产生 stalled 诊断，不擅自判定失败。

## 7. 进展

进展以可验证事件为主：

- queued/running/waiting/terminal 状态。
- 当前 Step、ToolCall、Workflow node。
- 最近事件时间和等待原因。
- 已用/剩余预算。
- 完成的显式阶段或 success criteria。

模型自报百分比可作为 `reported_progress`，必须与事件推导进度分开，不能覆盖真实状态。

## 8. Parent/Child 交互

父或应用可：

- `send_message(child_id, message)`：追加显式消息。
- `get_child` / `list_children`：查询状态。
- `await_child`：等待 terminal 或 timeout。
- `cancel_child`：取消指定 Child。
- `detach_child`：仅在 AgentSpec 允许时，使其不随父取消。

消息产生事件并进入 Child Context Planner；不直接修改正在执行的 ModelCall。

Detach 只解除父 Run 的取消传播，不改变 Session 所有权。Session 进入 closing 时 detached Child 可以完成但不能再派生新 Run；Session delete 必须等待或强制取消所有 detached Child。

## 9. ChildResult

终态输出：

- status。
- structured output 或最终文本。
- concise summary。
- artifacts。
- evaluations 和 success criteria 状态。
- usage/cost/latency。
- failures、unknown outcomes、unresolved requests。

父 Context 默认只注入 summary、结构化输出和 Artifact refs；需要时可检索 Child 原始事件。

## 10. 取消和失败传播

- 父取消默认取消非 detached Child。
- 父失败按 AgentSpec 策略取消或等待 Child；默认取消。
- 单个 Child 失败不自动使父失败，父/Workflow 的 join/failure policy 决定。
- Child 中 outcome_unknown 会传播为结果中的 unresolved 状态，不能被 summary 隐藏。

## 11. 事件

- `subagent.spawn.requested|accepted|rejected`
- `subagent.queued|started|waiting|stalled|completed|failed|cancelled`
- `subagent.message.sent|received`
- `subagent.detached`
- `subagent.budget.updated|exhausted`

## 12. 验收标准

- Child 默认看不到未授权父历史。
- 深度、并发、累计数量和预算均能阻止失控递归。
- 父/子权限求交正确，Child 无法扩大 workspace。
- 父取消、Child 失败、detach、消息和等待环有测试。
- 应用能实时查询 Child 当前动作、等待原因和 usage。
