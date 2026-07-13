# 上下文与多级压缩

## 1. 双层模型

### Session Ledger

保存完整消息、工具输出、事件、Artifact、摘要和评估，是审计与重建依据。关闭 Session 不删除 Ledger；删除 Session 清理其持久数据。

### Context View

一次 ModelCall 实际使用的上下文投影。它包含选中的原文、引用、结构化 Capsule、近期消息和 Prompt Manifest。不同 Run/Child 可以对同一 Session Ledger 拥有不同 View。

压缩只能新增 View/Capsule/索引，不能修改原始 Ledger。

## 2. 预算模型

Context Planner 计算：

```text
available_input = model_window
                - reserved_output
                - tool_schema_tokens
                - safety_margin
```

输入预算在系统片段、Capability Catalog、Workflow state、历史和当前输入之间分配。实际模型上下文限制通过 LiteLLM/模型配置获得；未知时要求应用显式设置保守值。

默认阈值是相对比例并可配置：

- soft：优先 L1，无模型摘要调用。
- target：允许 L2，恢复目标余量。
- high：执行 L3 结构化摘要。
- critical：执行 L4 Rebase；仍失败则在 ModelCall 前返回预算错误。

具体默认百分比在实施计划通过基准测试确定，不成为长期 API 常量；配置语义固定为 `soft/target/high/critical`。

## 3. 压缩级别

### L0 Full

保留所有选中原文，仅做 token 计算、排序和 Prompt Manifest。

### L1 Reference

近似无损：

- 大 Tool/MCP 输出移到 Artifact，只留有界预览和引用。
- 重复文件内容、错误栈和提示片段去重。
- 已存在 Ledger 的内容通过引用避免重复持久化。

### L2 Selective

按确定性规则裁剪低价值内容：

- 保护近期窗口、用户 pin、未决状态和当前 Workflow。
- 移除已完成且无后续引用的进度 delta。
- 用结构化 Tool Outcome 替代冗长 stdout 预览。
- 低重要度内容保留 source range 和检索键。

### L3 Summary

使用 LiteLLM 生成结构化 `ContextCapsule`，对封闭历史片段做 rolling summary。摘要不能覆盖仍在运行的 Step、节点或请求。

### L4 Rebase

将片段摘要聚合为主题/Session 层级摘要，创建持久 checkpoint，并以系统提示词、活动目标、约束、当前状态、近期窗口和检索索引构成新基线。

## 4. 策略组合

Planner 可组合：

- `ArtifactReference`
- `Deduplicate`
- `RecencyWindow`
- `ImportancePruner`
- `RollingSummary`
- `HierarchicalSummary`
- `WorkflowCapsule`
- 应用注册的 `CompactionStrategy`

策略接口接收只读候选片段和预算，返回选取/替换建议及证据；它不能直接删除 Ledger。

## 5. ContextCapsule Schema

至少包含：

- objective
- user_constraints
- decisions
- verified_facts
- open_questions
- completed_actions
- current_plan / next_actions
- tool_outcomes
- artifact_refs
- failures_and_recovery
- workflow_state
- child_status
- source_event_ranges
- capsule_version / model / prompt_hash

字段为空时保存空集合而不是省略语义关键字段。

## 6. 固定和保护内容

以下内容默认不可被摘要静默替换：

- 当前系统提示词，由 Composer 每次重建。
- 当前用户输入和受保护的近期窗口。
- 用户显式 pin 的约束、事实和决定。
- 未解决的权限、输入、审批和 reconciliation 请求。
- 当前 Workflow 节点、Child 依赖和剩余预算。
- Artifact id、失败证据和摘要 source ranges。

应用可增加 pin，但不能取消 Runtime 的安全保护项。

## 7. 摘要验证

提交 Capsule 前执行：

- Pydantic Schema 验证。
- source event id/range 存在性。
- pin、未决项和活动状态完整性。
- 数字、路径、标识符和决策等关键事实一致性检查。
- 可选 LiteLLM Judge 事实一致性评估。

验证失败时先重试一次更明确的结构化修复；仍失败则回退到更保守级别。失败、重试和回退全部进入 Trace。

## 8. 检索与重注入

Context Retrieval 提供：

- 按事件范围读取原文。
- 按关键词/元数据搜索消息和 Tool Outcome。
- 读取 Artifact 或其有界片段。
- 由 Capsule source refs 反查来源。

Agent 可通过受控历史检索能力请求原文；应用也可在下一次 Run 前注入检索结果。检索本身受 Session 和资源权限约束。

## 9. 自动策略和手动控制

- SDK 默认自动选择级别和策略。
- 应用可覆盖阈值、禁用某策略、指定 compaction model 或设置最大摘要成本。
- 应用可 `compact_now`，但仍必须通过验证。
- 应用可选择只允许 L0-L2，从而禁止有损摘要；预算不足时 Run 明确等待或失败。

## 10. Trace 与分析

每次 Context build/compaction 记录：

- model window、各类预算和估算误差。
- 输入/输出 token、压缩率、耗时和成本。
- 运行的策略、候选/保留/替换范围。
- Capsule、source refs、验证和回退。
- 后续 Run 成功率与压缩 revision 的关联维度。

## 11. 验收标准

- 所有压缩级别均不修改原始 Ledger。
- 强制小窗口场景能从 L1 逐步升级并留下完整事件。
- pin、活动节点、未决请求和 source refs 不丢失。
- 摘要验证失败可靠回退，不把坏 Capsule 注入模型。
- 原文能通过 Capsule 引用和搜索重新注入。
- Child Context 不意外复制父 Session 全量历史。

