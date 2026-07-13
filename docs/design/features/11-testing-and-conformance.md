# 测试、验证套件与参考实现

## 1. 测试目标

测试不仅验证单模块，还验证组合不变量：恢复不会重复副作用、权限不能被 MCP/Skill/Child 绕过、压缩不破坏未决状态、Trace 可重放、分析可下钻证据。

## 2. 测试层级

### Unit

- Pydantic Schema 和序列化。
- Run/Node 状态 transition。
- Policy precedence、路径规范化和命令风险。
- Prompt layer 顺序和 Manifest hash。
- token budget 和策略选择。
- Workflow compiler 和表达式 AST。
- failure taxonomy、usage 聚合和 analytics queries。

### Property/Fuzz

- 随机合法/非法状态 transition。
- 路径、symlink、Windows/Posix 边界和 shell 表达式。
- Workflow graph、循环、并发和 Schema 组合。
- 事件顺序、重复交付和 upcaster。
- Context Capsule source refs 和 pin 保留。

### Contract

- Custom StateStore。
- Tool handler。
- Exporter。
- Evaluator。
- Permission/Input Bridge。
- ArtifactStore。

### Integration

- SQLite transaction、lease、migration 和 delete。
- LiteLLM streaming/error/usage seam。
- MCP stdio 与 Streamable HTTP reference server。
- `SKILL.md` 官方 validator 和资源加载。
- bash 子进程取消、timeout 和大输出。

### E2E/Recovery

- 从 Session 创建到 Run 完成/分析的全链路。
- 在事务和外部 I/O 各边界杀死进程。
- 动态 Workflow 审批、Child 并发、权限等待和恢复。
- 强制压缩和原文重注入。
- Exporter/Evaluator 背压和失败。

## 3. 测试替身

`agent_sdk.testing` 提供：

- `ScriptedLiteLLM`：在内部调用 seam 返回固定 stream、tool calls、usage 和错误；不成为公共 Provider。
- `FakeClock` 和 deterministic id source。
- `ScenarioPermissionBridge` / `ScenarioInputBridge`。
- `FakeMCPServer`，覆盖两种 Transport。
- `TestTool` 和可配置 side effect/idempotency。
- `FaultInjectingStore/Exporter/ArtifactStore`。
- `EventRecorder` 和 golden trace matcher。
- `ScenarioRunner`。

测试替身通过公共行为契约，不要求应用访问 Runtime 私有状态。

## 4. Scenario 格式

```yaml
name: resumable-workflow
given:
  session: {...}
  agent: {...}
  model_script: [...]
when:
  - start_run: {...}
  - expect_permission: {...}
  - resolve_permission: {...}
  - inject_crash: after_tool_started
  - restart_sdk: {}
then:
  - expect_run_state: waiting_reconciliation
  - expect_event_sequence: [...]
  - expect_no_duplicate_side_effect: true
```

Scenario Runner 输出人类可读报告、JUnit 和结构化 JSON。

## 5. 一致性验证套件

### Tool Contract

- 输入/输出 Schema。
- timeout/cancel。
- 错误规范化。
- 大输出/Artifact。
- 幂等声明与行为测试 hook。
- Trace 完整性。

### Store Contract

- 事务原子性。
- expected version 和 idempotency。
- cursor/sequence。
- lease generation。
- migration 和 Session delete。
- Session closing、force close/delete、detached Child 和 analytics contribution 清理。
- 并发和故障恢复。

### Exporter Contract

- 至少一次、去重和 cursor。
- batch、retry、backpressure。
- redaction 不变量。
- 失败隔离。

### Evaluator Contract

- 结构化结果。
- evidence refs。
- version/prompt hash。
- timeout/error 隔离。
- 同步质量门与异步模式。

套件输出兼容矩阵；通过是官方“最佳实践兼容”标志的前提。

## 6. 综合垂直切片

首个里程碑必须演示：

1. 默认 SQLite Session 和 Coding Agent。
2. LiteLLM 流式模型调用。
3. 一个内置 Tool、一个用户 Tool、一个 MCP Tool。
4. 一个 `SKILL.md` 的 discover/activate/resource。
5. 对话生成 Workflow，验证并经用户批准。
6. Workflow 启动至少两个 Child Run。
7. write/bash 触发路径/命令权限和四级决定之一。
8. 小窗口触发多级压缩，来源可重注入。
9. 中途重启，安全恢复或进入 reconciliation。
10. 实时查询状态树、工具、节点、token、成本和等待。
11. Evaluator 判定结果，Analytics 聚合成功/失败和 Tool effectiveness。
12. Session close 后可查，delete 后清理 SDK 数据。

任何能力若只能在孤立单测工作而不能进入该链路，不算垂直切片完成。

## 7. 参考实现

### `examples/reference_cli`

- 创建/恢复 Session。
- 流式展示事件。
- 处理 permission/input/workflow approval。
- pause/resume/cancel。
- 查询 execution tree、usage 和结果。

### `examples/monitor_server`

最小 FastAPI/SSE 示例：

- Run/Workflow/Child 查询端点。
- 事件 cursor 订阅。
- 权限和输入答复端点。
- 跨 Run success/failure/tool analytics 查询。

它只使用公共 SDK，不成为核心依赖，也不规定 UI。

### `examples/coding_workflow`

展示 Explore → Plan → Approve → Implement → Verify → Report，以及用户 DSL、Python Builder、对话生成三个入口。

## 8. CI 质量门

- formatter、lint、type check。
- unit/property/contract/integration。
- supported Python/OS/SQLite matrix。
- migration fixtures。
- MCP transport integration。
- deterministic replay/golden trace。
- package build、install 和 minimal examples。
- 文档链接和代码片段测试。

涉及状态机、Policy、Store、Event Schema 或 migration 的改动必须运行对应扩展测试集。

## 9. 任务文档与证据

实施阶段每项任务文档必须包含：

- 关联设计章节。
- 前置依赖。
- 修改范围和明确非范围。
- 先写的失败测试。
- 实现步骤。
- 验收命令和期望结果。
- 风险、迁移和回滚。
- 完成后实际证据与提交 id。

里程碑索引从任务文档汇总状态，不能仅在对话里维护进度。

## 10. 验收标准

- 一致性套件能发现一个故意违反幂等/事务/事件契约的实现。
- 参考 CLI 和 Monitor 仅依赖公共 API。
- 综合垂直切片在重启前后得到一致最终状态且无重复副作用。
- golden trace 覆盖权限、压缩、Workflow、Child、usage、Evaluation 和 Analytics。
- CI 可以从空环境安装并运行最小示例。
