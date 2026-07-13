# 恢复与错误处理

## 1. 稳定错误模型

所有公共异常继承 `AgentSDKError`，包含：

- `code`：稳定机器码。
- `message`：安全的人类可读摘要。
- `category` 和 `stage`。
- `retryable`。
- Session/Run/Step/Call/Node ids。
- `cause_refs`：关联事件或 Artifact。
- `details`：经过 Redactor 的结构化信息。

Python cause chain 可用于本地调试，但序列化错误不依赖具体第三方异常类。

## 2. 错误边界

- LiteLLM Gateway 映射模型异常。
- Tool Executor 映射 handler、timeout、cancel、Schema 和 unknown outcome。
- MCP Client 映射 Transport/Protocol/Server errors。
- Context Planner 映射预算、压缩和验证错误。
- Workflow/Child 映射编译、调度、节点和依赖错误。
- Store 映射事务、迁移、锁、损坏和兼容错误。
- Exporter/Evaluator/Analyzer 的错误隔离在各自后台组件。

## 3. Retry 所有权

避免多层重复重试：

- LiteLLM provider/Router attempt 由 LiteLLM 配置管理。
- Runtime 只在 ModelCall 整体失败且策略允许时创建新的 Call attempt。
- Tool retry 由 ToolSpec/Workflow node 决定，必须考虑幂等性。
- MCP connect 可以重连；具体 ToolCall 是否重试仍受副作用语义限制。
- Exporter 使用自己的 cursor retry。
- Store transaction 仅对明确 transient lock/busy 做有界退避。

所有 retry 受总时间、attempt、token 和成本预算约束。

## 4. 自动恢复矩阵

| 故障 | 默认行为 |
|---|---|
| LiteLLM rate limit/临时不可用 | 按 LiteLLM/Run 策略有界重试或 fallback |
| Model stream 中断且无副作用 | 新 attempt，旧 attempt 标失败 |
| 幂等 Tool 明确未完成 | 按 Tool/Workflow 策略重试 |
| 非幂等 Tool 结果未知 | 进入 reconciliation，禁止静默重试 |
| MCP 连接断开 | 重连并重新协商；调用结果另行判断 |
| 压缩生成/验证失败 | 修复一次，随后回退更保守策略 |
| Exporter/异步 Evaluator 失败 | 后台重试或记录失败，不改主 Run |
| Run lease 过期 | 标 interrupted，从安全边界恢复 |
| 预算耗尽 | 明确 failed 或 waiting，按上层策略 |
| Store migration/损坏 | 拒绝启动写路径，输出恢复说明 |

## 5. SQLiteStore

默认 SQLite Schema 覆盖：

- sessions、agent_specs、runs、steps、model_calls、tool_calls。
- workflows、workflow_nodes、child_relations。
- events、projection_positions、subscriber_cursors。
- messages、context_views、capsules、prompt_manifests。
- permissions、policy_rules、pending_requests。
- evaluations、failures、analytics_dimensions。
- artifacts、leases、idempotency_records。

事件表按 Session/Run/sequence 和全局 cursor 索引。大内容不塞入频繁查询表。

## 6. 事务

- Command 处理在短事务中验证版本、追加事件、更新核心投影和 idempotency record。
- 模型/工具等外部 I/O 不持有 SQLite 事务。
- started 事件先提交，外部 I/O 完成后用新事务提交 outcome。
- 因此进程可能在两者之间退出；unknown outcome/recovery 语义必须显式。
- SQLite busy 使用有界重试，超过阈值返回 StoreBusyError 并保持状态可恢复。

## 7. Migration

- 每个 Schema version 有前滚 migration。
- Migration 有唯一 id、checksum 和 applied_at。
- 重复运行必须幂等或检测已应用。
- 启动前检测数据库版本；应用可执行 `migration_plan`/dry-run。
- 高风险 migration 建议先备份；SDK 提供备份 hook，不擅自删除旧库。
- CI 从所有受支持旧版本 fixture 升级并重放关键事件。

## 8. Artifact 生命周期

- 默认 ArtifactStore 可位于 SQLite 旁的受管目录，也可由应用替换。
- Artifact 写入使用临时文件、fsync/rename 或等价原子语义。
- DB 保存 owner Session、hash、size、mime、redaction 和存储 locator。
- Session delete 在事务内将 Artifact 标记待删除；后台清理可重试。
- 清理失败产生 health/diagnostic event，不恢复已删除 Session 可见性。
- workspace 文件不是托管 Artifact，不随 Session 删除。

Session 的事件、投影、消息、Context、Evaluation、Analytics contribution 和 idempotency records 在删除事务内清除。全局永久 Policy rule 不属于 Session 数据；删除时移除其中的 Session 反向引用。后台 Artifact 清理事件只使用匿名 cleanup job id，不重新持久化 Session id。

## 9. Reconciliation

当外部副作用状态未知：

1. 创建 pending reconciliation request，记录 ToolCall、输入 hash、开始事件和外部 idempotency key。
2. Run 进入 waiting。
3. 应用可提供 Tool-specific reconciler 自动查询外部状态。
4. 仍不确定时，用户选择：视为已完成、视为未执行并重试、终止 Run。
5. 决定和证据进入审计事件。

## 10. 降级

- Analytics/Exporter 不可用：核心运行继续，查询标记投影滞后。
- MCP Server 不可用：只禁用该 Server 的能力；是否继续由 Agent/Workflow 决定。
- Skill 无效：跳过并产生诊断，不破坏无关 Skill。
- compaction model 不可用：可回退当前 Agent model；仍不可用且预算不足则明确失败。
- SQLite 只读：允许只读查询；禁止启动/恢复 Run。

## 11. 验收标准

- 每种错误有稳定 code/category/stage 和可重试性。
- Retry 不跨层无限叠加，总预算始终生效。
- 进程在 started/outcome 之间退出可进入正确恢复或 reconciliation。
- 旧库迁移、备份 hook、只读模式和损坏检测有集成测试。
- Session 删除最终清理托管 Artifact，但不删除 workspace 文件。
- 后台组件故障不篡改主 Run 结果。
