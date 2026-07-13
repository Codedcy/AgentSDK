# Evaluation 与 Analytics

## 1. 目标

把“运行完成”与“任务成功”分开。SDK 必须允许应用定义成功标准，并结合测试、规则、用户反馈和 LiteLLM Judge 保存多个评估结果；跨 Run 分析必须能解释统计来自哪些事件、版本和 Evaluator。

## 2. Evaluator 协议

```python
class Evaluator(Protocol):
    id: str
    version: str
    scope: EvaluationScope
    async def evaluate(self, subject, context) -> EvaluationResult: ...
```

作用域：

- Run
- Stage
- Step
- ToolCall
- Artifact
- Workflow/Node
- ChildResult

`EvaluationResult`：

- evaluator id/version/prompt hash。
- metric values。
- labels。
- pass/fail/unknown verdict。
- reason。
- evidence event/artifact refs。
- confidence 和 method。
- created_at、dataset/case id。

一个 subject 可有多个结果；不合并为单个模糊布尔值。

## 3. 内置 Evaluators

- `StructuredAssertionEvaluator`：Schema、字段、范围和自定义谓词。
- `ExitCodeEvaluator`：命令/测试退出码。
- `TestReportEvaluator`：解析常见测试结果的统一摘要。
- `UserFeedbackEvaluator`：应用提交评分、标签和原因。
- `RepetitionEvaluator`：重复工具调用和无进展循环。
- `ToolUsefulnessEvaluator`：relevance/novelty/utilization/contribution/waste。
- `LiteLLMJudgeEvaluator`：使用独立 LiteLLM 配置的结构化 Judge。

应用验收器优先级由 Workflow/AgentSpec 配置；Judge 不能覆盖确定性测试失败。

## 4. 同步质量门与异步评估

- 同步 Evaluator 在 Run/Node 完成前执行，可决定 pass/failure edge。
- 异步 Evaluator 在主结果提交后执行，用于离线质量、归因和分析。
- 异步失败只记录 evaluation failure，不改写主 Run 状态。
- 用户反馈可在任意时间追加，并触发分析投影更新。

## 5. 失败分类

`FailureRecord` 保存：

- category：model、tool、mcp、permission、input、workflow、subagent、context、storage、budget、timeout、cancel、internal。
- stage：发生的 Run/Workflow 阶段。
- reason_code：稳定机器码。
- retryability。
- root/cascading/unknown 标记。
- cause event refs 和原始脱敏异常。
- affected result/evaluation。

Root cause 不仅靠时间先后判断：确定性传播边来自执行依赖；无法确定时标 `candidate_root` 并带置信度。

## 6. Tool Effectiveness

### Reliability

- total calls
- success/failure/timeout/cancel/permission-denied rate
- malformed/empty result rate
- retry count 和 retry-success rate
- outcome_unknown rate

### Efficiency

- latency distribution
- token/output bytes
- model-visible preview 比例
- cost per successful/useful call
- repeated-call rate
- discarded output/waste

### Usefulness

- relevance：结果是否满足当前 Step 的信息需求。
- novelty：是否提供已有 Context 中不存在的信息。
- utilization：是否被后续 Prompt、Tool、Artifact 或决策消费。
- contribution：是否有证据支持最终成功标准。

确定性利用信号包括：后续 Context 引用、Artifact 读取、结构化字段消费、最终结果引用、紧接着重复调用、压缩时无保留。Judge 可补充语义判断，但结果保存 method/confidence。

## 7. 版本维度

每个 Run 固化：

- SDK 版本。
- AgentSpec revision。
- Prompt Manifest hash。
- model 和非 Secret 参数 fingerprint。
- Tool version/schema/source。
- MCP server/tool schema hash。
- Skill content hash。
- Workflow Definition hash。
- Context/compaction policy revision。
- Permission policy revision。
- Evaluator/dataset version。

这些维度支持跨 Run 对比；缺失维度显示 unknown，不归到错误版本。

## 8. Analytics 查询

```python
await sdk.analytics.success_rate(filters=..., group_by=[...])
await sdk.analytics.failures(filters=..., group_by=["stage", "reason_code"])
await sdk.analytics.tool_effectiveness(tool=..., window=...)
await sdk.analytics.compare(baseline=..., candidate=..., metrics=[...])
await sdk.analytics.run_attribution(run_id)
await sdk.analytics.insights(filters=...)
```

结果带：样本数、时间窗口、filters、group keys、缺失率、统计方法、as_of_cursor 和可下钻 evidence query。

样本过小时显示 insufficient_data，不生成强结论。

## 9. 归因层级

1. Provenance：确定的执行和数据依赖，例如哪个 Tool 输出进入哪个 Node。
2. Evaluation attribution：Evaluator 基于证据判断贡献或失败影响。
3. Statistical comparison：版本、队列或 A/B 数据集的相关/差异证据。
4. Judge inference：LLM 解释性推断。

API 返回 `method`，并禁止把第 2-4 层统一命名为 causal。真正因果结论需要应用提供合理实验设计；SDK 只保存分配信息和统计证据。

## 10. Insight Analyzer

Insight 结构：

- finding
- affected scope/versions
- evidence queries/refs
- severity
- confidence
- recommended action
- expected metric impact
- validation experiment

内置规则先覆盖高失败率工具、特定阶段失败簇、重复调用、成本回归、压缩后质量下降和版本回归。可选 LiteLLM Analyzer 解释聚类和生成建议。

默认只生成建议，不能自动修改 Prompt、Tool、Skill、Policy 或 Workflow。应用若要自动优化，必须另建带审批和实验的 Workflow。

## 11. 存储和刷新

- Evaluation 是不可变追加记录；撤销通过 superseding result 表达。
- 小规模 SQLite 默认按事件增量更新基础聚合，并支持按需 SQL 查询。
- 每个聚合保留可按 Session 删除的 contribution；Session 删除时移除 contribution 并重算/递减聚合，保证删除数据不继续影响成功率或工具指标。
- 大规模外部仓库通过 Exporter 消费同一事件和 Evaluation。
- 异步投影暴露 cursor，应用可等待达到目标 cursor 后再比较。

## 12. 验收标准

- 应用能用确定性验收器定义 Run 成功，不依赖 Judge。
- 同一 Run 可并存测试、用户反馈和 Judge 结果。
- 能按工具/版本/阶段计算失败率并下钻具体调用。
- Tool usefulness 指标区分确定性信号与模型判断。
- compare/insight 对样本量和 missing data 给出明确警告。
- 每条归因和建议都能追溯证据、方法、版本和置信度。
