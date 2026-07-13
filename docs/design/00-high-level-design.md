# Agent SDK 概要设计

## 1. 目标

构建一个可嵌入 Python 应用的 headless Agent SDK。SDK 提供 Agent 运行所需的基础能力、稳定状态模型、恢复机制和可观测数据，但不决定 UI、用户交互时机或产品形态。

核心目标：

- 运行可暂停、恢复、取消和查询的 Agent Loop。
- 通过 LiteLLM 使用不同模型供应商。
- 提供基础 Coding Tools，并允许应用注册自定义工具。
- 连接外部 MCP Server，加载开放格式的 Agent Skills。
- 在不删除原始历史的前提下，自动执行多种、多级上下文压缩。
- 支持用户编写、Python 构建和对话生成的 Workflow。
- 支持隔离上下文、可调度和可监控的子 Agent。
- 实时查询工具调用、工作流、子 Agent、token、成本和等待原因。
- 跨 Run 分析成功率、失败原因、失败阶段、工具有效性、结果归因和改进方向。
- 提供最佳实践参考实现和扩展一致性验证套件。

## 2. 非目标

- 首版不提供完整产品 UI 或强制部署的控制服务。
- 首版不实现 MCP Server。
- 不实现 LiteLLM 之外的模型适配层。
- 不承诺外部系统上的 exactly-once 副作用或分布式事务补偿。
- 不允许动态 Workflow 执行任意 Python 表达式或代码。
- 不把 LLM 判断包装成确定的因果结论。

## 3. 架构风格

采用“模块化单体 + 事件日志 + 状态投影”：

```text
Application / CLI / Optional Monitor
                  |
           Public SDK Facade
                  |
   +--------------+---------------+
   | Runtime Kernel               |
   | Agent Loop / Run State       |
   +--------------+---------------+
                  |
   +--------------+---------------+
   | Capabilities | Context       |
   | Tools/Policy | Prompt        |
   | MCP/Skills   | Compaction    |
   +--------------+---------------+
                  |
   +--------------+---------------+
   | Orchestration                |
   | Workflow / Subagents         |
   +--------------+---------------+
                  |
   +--------------+---------------+
   | Events / Projections / Eval  |
   | Analytics / Exporters        |
   +--------------+---------------+
                  |
       SQLiteStore / InMemoryStore
```

模块只能依赖下层稳定协议。Public Facade 不暴露 LiteLLM、SQLite 或 MCP SDK 的内部类型。模块化单体降低嵌入成本；事件日志和投影保留 Actor/Durable Runtime 的恢复与观测优点，但不引入分布式 Actor 系统。

## 4. 核心领域模型

```text
AgentSpec -> Session -> Run -> Step -> ModelCall / ToolCall
                         |       |
                         |       +-> Evaluation
                         +-> WorkflowRun -> NodeRun
                         +-> Child Run
```

- `AgentSpec`：不可变、可版本化的 Agent 配置快照。
- `Session`：对话历史、持久数据和删除语义的边界。
- `Run`：针对一次输入或目标的执行实例。
- `Step`：Agent Loop 的一次模型推理与可选工具执行迭代。
- `WorkflowRun`：Workflow IR 的一次持久执行。
- `Child Run`：带 `parent_run_id` 的隔离 Agent Run。
- `Artifact`：大文本、文件快照或二进制结果的托管引用。
- `Evaluation`：针对 Run、Stage、Step、ToolCall 或 Artifact 的评估结果。

## 5. 全局不变量

1. 在保留期内，已提交事件不可修改；显式 Session 删除会移除该 Session 的整条事件链和派生数据。
2. 核心状态事件和状态投影在同一存储事务提交。
3. 每个 Run 同时只能被一个有效执行租约推进。
4. 所有副作用先完成 Schema 校验和权限决策。
5. 子 Agent 权限是父权限与任务包限制的交集，不能自动扩大。
6. 压缩只生成 Context View，不修改 Session Ledger。
7. 关闭 Session 保留全部数据；删除 Session 清理 SDK 托管数据和派生分析，但不回滚 workspace 文件，也不撤销用户明确保存为全局配置的永久权限规则。
8. 动态 Workflow 必须通过静态验证和风险预检；默认经用户确认后才执行。
9. 未知的非幂等工具结果不得静默重试。
10. Exporter、异步 Evaluator 和分析器失败不得破坏已完成的主执行。
11. 所有聚合指标必须可下钻到事件或评估证据。
12. 统计相关和 Judge 推断必须带方法、版本、证据和置信度，不能标为确定因果。

## 6. 主要组件

| 组件 | 职责 | 直接依赖 |
|---|---|---|
| Public SDK Facade | 配置、Agent/Session/Run/Workflow API | Runtime、查询接口 |
| Runtime Kernel | Run 状态机、Agent Loop、取消和等待 | LiteLLM Gateway、Store、Events |
| LiteLLM Gateway | `acompletion`、流式归一、usage/cost/error | LiteLLM |
| Tool Registry | 内置、用户、MCP 工具统一描述 | Pydantic Schema |
| Policy Engine | workspace、命令、路径和用户审批 | Store、Events |
| MCP Client Manager | 连接、协商、发现、健康状态 | 官方 Python MCP SDK |
| Skill Registry | `SKILL.md` 发现、激活、资源解析 | Filesystem、Tools |
| Context Planner | token 预算、选择、压缩和重注入 | LiteLLM Gateway、Ledger |
| Prompt Composer | 分层系统提示词和 Prompt Manifest | Profiles、Capabilities |
| Workflow Engine | DSL/Builder 编译、验证和持久节点执行 | Runtime、Tools、Evaluators |
| Subagent Scheduler | TaskEnvelope、并发、预算和父子关系 | Runtime、Policy |
| Event Store/Bus | 事务事件、cursor 和提交后订阅 | StateStore |
| Projectors | 当前状态、执行树、usage 和统计读模型 | Events、StateStore |
| Evaluation Engine | 可插拔 Evaluator 和质量门 | Events、LiteLLM Gateway |
| Analytics Engine | 跨 Run 聚合、归因和洞察 | Projections、Evaluations |
| Testing Kit | Scenario、故障注入和契约验证 | 公共 SDK API |

## 7. 一次 Agent Step 的数据流

1. 应用以 idempotency key 提交启动或恢复 Command。
2. Runtime 获取 Run 执行租约，读取一致性快照。
3. Context Planner 计算预算；必要时生成并验证新的 Context View。
4. Prompt Composer 组合系统、Profile、应用、能力和运行时片段。
5. LiteLLM Gateway 进行流式模型调用并持续写入事件。
6. 若模型输出最终答复，保存消息并触发 Evaluator。
7. 若模型提出工具调用，Tool Registry 验证 Schema，Policy Engine 判定 `allow/deny/ask`。
8. `ask` 使 Run 进入等待，应用提交用户决定后恢复。
9. Tool Executor 执行、取消或超时，并保存 Outcome、Artifact 和评估信号。
10. 投影器更新 Run、执行树、usage 和工具统计；提交后订阅器收到事件。
11. Runtime 决定完成、进入下一 Step、等待、失败或取消。

## 8. 存储与一致性

默认 `SQLiteStore` 使用 WAL、显式事务和 Schema Migration，保存：

- Sessions、AgentSpec snapshots、Runs、Steps 和 Workflow nodes。
- 不可变事件、状态投影、订阅 cursors 和执行 leases。
- Prompt manifests、Context capsules、Evaluations 和 Analytics dimensions。
- Artifact 元数据；大 Payload 可保存到可配置 ArtifactStore。

`InMemoryStore` 保持相同协议，供测试和临时任务使用。自定义 Store 必须通过 SDK 契约套件。SQLite 不被包装成通用分布式锁；若未来支持多进程 Worker，需要单独设计兼容租约语义的数据库实现。

Session 删除会清除该 Session 的事件、投影、消息、Capsule、Evaluation、Analytics contribution、idempotency record 和托管 Artifact。全局 cursor 允许出现因删除形成的空洞；按 cursor 读取必须跳过空洞。已通过 Exporter 发送到 SDK 之外的数据不在本地删除事务范围内，SDK 只向当前订阅者发送非持久 deletion notification，供应用执行外部清理。

## 9. 配置模型

配置分为 SDK、AgentSpec、Session、Run 和 Workflow 五级，越靠近执行的显式值优先。安全上限只能收紧，除非应用以受信任配置明确扩大。

关键配置包括：

- LiteLLM model、参数、Router/retry/fallback 和凭证解析。
- SQLite 路径、ArtifactStore、保留与删除策略。
- Tools、MCP servers、Skill roots 和 Coding Profile。
- Workspace roots、Policy rules、审批 Bridge 和完全放行模式。
- Context token 水位、压缩策略、compaction model 和固定内容。
- Workflow/Child concurrency、深度、Step、token、成本和时间预算。
- Trace capture level、Redactor、Exporters、Evaluators 和 Analytics 维度。

配置、Prompt、工具、Skill、Workflow 和 Evaluator 均生成版本指纹，用于恢复和跨 Run 对比。Secret 只记录引用或脱敏 hash。

## 10. 可观测与分析

SDK 的可观测面由四层构成：

1. Domain Events：记录已发生事实。
2. Projections：提供当前状态和快速查询。
3. Evaluations：判断结果质量并保存证据。
4. Analytics：跨 Run 聚合、对比和生成洞察。

应用可随时查询 Run 状态、当前阶段、等待原因、Workflow 节点、子 Agent、工具调用、token、成本、延迟和错误。跨 Run 可按 Agent、Prompt、模型、工具、Skill、Workflow、压缩策略和数据集版本计算成功率、失败阶段、工具失败率与工具有效性。

## 11. 安全边界

- 默认文件操作限制在配置 workspace；真实路径和符号链接均校验。
- 权限规则支持 `deny/allow/ask`，匹配优先级固定为 deny 优先。
- 没有应用审批 Bridge 时，`ask` 默认拒绝。
- Skill 和 MCP 内容视为外部输入，不能授予权限或修改 Policy。
- 动态 Workflow 使用受限 DSL，不允许 `eval` 或任意 Python。
- Secret 在事件持久化和导出前由 Redactor 处理。
- 完全放行必须显式开启并生成审计事件。

## 12. 交付与文档策略

第一里程碑是端到端垂直切片：每项核心能力完成最小闭环，并通过一次可恢复的综合场景。之后分别加固 Runtime、Capabilities、Context/Orchestration、Analytics 和发布质量。

书面设计通过评审后，实施计划必须拆为：

- 全局路线图。
- 多篇里程碑计划，每篇说明范围、依赖、验收、风险和回滚。
- 多篇逐任务文档，每篇只包含一个可独立测试和提交的工作单元。
- 任务索引记录状态、阻塞关系、对应设计章节、测试证据和提交。

这样即使对话或单次实现中断，也能从任务文档和已提交证据继续，而不是依赖聊天上下文。

## 13. 兼容基线

- Python：首版在实施计划中选定一个仍受支持的最小版本，并在 CI 覆盖连续版本区间。
- LiteLLM：锁定兼容版本区间，通过行为契约测试后升级。
- MCP：以 2025-11-25 稳定修订为兼容基线，使用版本协商而非固定假设。
- Agent Skills：严格验证官方必填字段；兼容性宽松模式只在应用显式开启时使用。
- SQLite：每次 Schema 变更包含迁移和旧数据库升级测试。

Python 首版支持范围固定为 `>=3.12,<3.14`，CI 覆盖 3.12 和 3.13。该上限来自 LiteLLM 当前官方包的 `Python <3.14` 约束；待 LiteLLM 官方发布可安装的 Python 3.14 版本并通过 Windows/Linux 兼容性矩阵后再扩大范围。第三方依赖的精确 patch 版本由锁文件管理；兼容版本区间和升级测试写入实施计划。
