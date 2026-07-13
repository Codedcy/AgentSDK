# Agent Runtime 与 Session

## 1. 职责

Runtime Kernel 管理 AgentSpec、Session、Run、Step、等待、取消、暂停、恢复和执行租约。它只通过 LiteLLM Gateway 调用模型，通过 Tool Executor 产生副作用，通过 Event Store 持久化事实。

## 2. 领域对象

### AgentSpec

不可变配置快照，包含：

- `agent_id`、`revision`、名称和描述。
- LiteLLM model 与非 Secret 参数。
- Prompt layers 与 Profile。
- Tool/Skill/MCP 选择规则。
- Policy、Context、预算和 Evaluator 配置引用。
- 内容 hash、创建时间和 SDK 版本。

修改 Agent 定义会生成新 revision；既有 Run 始终引用启动时的 snapshot。

### Session

Session 是历史和删除的边界，状态为：

- `active`：允许启动新 Run 和继续对话。
- `closed`：不允许新执行，历史、事件和分析仍可查询。
- `deleted`：逻辑上不可访问；事务内删除元数据并调度 Artifact 清理。

删除不会撤销工具对用户 workspace 或外部系统造成的副作用。

### Run

Run 状态：

```text
created -> queued -> running
                    <-> waiting_permission
                    <-> waiting_input
                    <-> waiting_approval
                    <-> waiting_children
                    <-> paused
running/waiting -> completed | failed | cancelled | interrupted
```

`interrupted` 表示执行租约丢失或进程异常退出；它不是业务失败。恢复器确认安全边界后可将其重新排队。

### Step

Step 是一次 Agent Loop 迭代，包含：

- 读取的状态版本和 Context View id。
- Prompt Manifest 和 ModelCall。
- 零个或多个 ToolCall。
- Step Outcome、usage、耗时和错误。

ModelCall 或 ToolCall 可以有自己的重试 Attempt，但不会伪造为新 Step。

## 3. 公共接口轮廓

```python
sdk = AgentSDK(config)
agent = sdk.agents.define(AgentSpec(...))
session = await sdk.sessions.create(workspaces=[...])
run = await sdk.runs.start(
    session_id=session.id,
    agent_id=agent.id,
    input=UserInput(...),
    idempotency_key="...",
)

snapshot = await sdk.runs.get(run.id)
async for event in run.events(cursor=None): ...

await run.resolve_permission(request_id, decision)
await run.provide_input(request_id, value)
await run.pause()
await run.resume()
await run.cancel()
```

同步 Facade 提供 `run_sync` 等便捷入口，但内部仍使用异步 Runtime。若在已有 event loop 的同线程调用同步入口，SDK 返回明确错误，不嵌套运行 loop。

## 4. Command 与幂等

所有会改变状态的公共方法转换为 Command：

- Command 带 `command_id` 和可选 `idempotency_key`。
- 同一作用域内重复 key 返回第一次的结果，不重复副作用。
- Command 处理验证 expected version，防止过期客户端覆盖新状态。
- 用户对同一权限或输入请求重复答复时，第二次返回已有决定。

## 5. Agent Loop

1. 获取 Run lease；验证 Session 为 active、预算未耗尽且未取消。
2. 从投影读取消息、Workflow/Child 状态和未决请求。
3. Context Planner 选择或创建 Context View。
4. Prompt Composer 生成 Manifest。
5. 创建 ModelCall，调用 `litellm.acompletion(..., stream=True)`。
6. 将流式文本、reasoning 元数据、tool-call delta、usage 和错误归一为事件。
7. 若收到最终内容，提交 Assistant Message，执行同步 Evaluator 和质量门。
8. 若收到工具意图，稳定排序并创建 ToolCall；执行 Schema 和 Policy 检查。
9. 处理工具结果，必要时外置 Artifact，完成 Step。
10. 根据最终结果、工具结果、预算和状态决定完成或进入下一 Step。

每轮开始和结束都有显式事件；单轮最大工具数、总 Step、时间、token 和成本可配置。

## 6. 并行 ToolCall

- 只有声明 `parallel_safe` 且资源冲突检测通过的工具才能并行。
- 写同一资源、未知副作用或要求用户决策的调用串行化。
- 并行完成顺序不影响写回模型的顺序；按原始 call index 稳定排序。
- 一个调用失败是否取消同批其他调用，由 AgentSpec 的 batch policy 决定。

## 7. Lease 与崩溃恢复

- Run lease 保存 owner、generation、acquired/renewed/expiry 时间。
- 只有持有当前 generation 的执行器能追加推进 Run 的事件。
- 启动恢复扫描 stale lease，把对应 Run 变为 interrupted。
- 已完成并持久化的 Step 不重做。
- 明确未开始的幂等调用可重试。
- 已开始但没有完成事件的非幂等工具进入 `reconciliation_required`，等待应用或用户选择“确认已完成、确认未执行、重新执行、终止”。

## 8. 取消与暂停

- 取消是持久 Command，Runtime 立即发出取消信号。
- Python 工具通过 `ToolContext.cancelled` 协作取消；子进程先优雅终止，再按配置强制结束。
- Pause 在当前安全边界生效，不中断不可安全暂停的事务。
- 父 Run 取消默认向下传播到 Workflow 节点和 Child Run；应用可配置让已分离 Child 继续。

## 9. 事件

至少包含：

- `session.created|closed|deleted`
- `run.created|queued|started|waiting|paused|resumed|completed|failed|cancelled|interrupted`
- `step.started|completed|failed`
- `model.call.started|delta|completed|failed`
- `tool.call.proposed|authorized|started|completed|failed|cancelled|timed_out`
- `input.requested|resolved`
- `reconciliation.requested|resolved`

## 10. 验收标准

- 重复启动 Command 不产生重复 Run。
- 在任意已提交 Step 边界杀死进程，重启后可恢复且不重做已完成工具。
- 未知非幂等调用不会自动重放。
- 等待权限、输入、Child 或 Workflow 审批时，快照和事件均给出准确原因。
- 同步和异步入口对相同场景产生等价事件和最终状态。

