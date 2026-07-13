# LiteLLM 与 Prompt Profiles

## 1. 范围

模型调用只使用 LiteLLM Python SDK。Agent Loop 不支持公共 Provider 插件，也不维护 OpenAI、Anthropic 或其他供应商的独立客户端。

SDK 仍保留内部 `LiteLLMGateway`，作用仅是：

- 集中配置和 Secret 注入。
- 把 LiteLLM 流式响应归一为 SDK 事件。
- 统一 usage、cost、latency 和异常记录。
- 隔离 LiteLLM 版本变化，便于契约测试。
- 在测试中替换 `litellm.acompletion` 调用点。

内部封装不是用户可注册的模型抽象。

## 2. LiteLLM 配置

```python
LiteLLMConfig(
    model="provider/model",
    api_key=SecretRef(...),
    api_base=None,
    timeout_seconds=120,
    max_retries=2,
    router=None,
    fallback_models=[],
    default_params={},
)
```

- Secret 通过 `SecretResolver` 在调用前解析，不进入 AgentSpec 明文快照。
- 用户可传 LiteLLM 支持的模型参数；SDK 对影响恢复和统计的参数做规范化并生成 fingerprint。
- Router、fallback 和 provider retry 使用 LiteLLM 能力；Runtime 不在外层重复同一 ModelCall 的无界重试。
- Runtime 的 Step retry 与 LiteLLM provider attempt 分开计数。

## 3. 流式归一

Gateway 输出内部事件：

- `TextDelta`
- `ReasoningDelta` 或供应商扩展元数据
- `ToolCallStarted/ArgumentsDelta/Completed`
- `UsageReported`
- `ModelCompleted`
- `ModelFailed`

未知供应商字段保存在受控的 `provider_metadata`，不参与核心状态机。工具参数在完整接收后再做 JSON/Schema 校验。

## 4. Usage 与成本

- 优先使用供应商/LiteLLM 返回的实际 usage。
- 调用前 token 仅用于预算估算，并标记 `estimated`。
- 实际 token 可按 prompt、completion、cached、reasoning 等已知维度保存。
- LiteLLM 能提供成本时保存金额、币种和价格表/版本信息。
- 无法确定成本时保存 `unknown`，不得用零冒充免费。
- Usage 聚合到 ModelCall、Step、Run、Workflow、Child tree 和 Session。

## 5. 异常映射

LiteLLM 异常映射为稳定 `ModelError`：

- authentication
- authorization
- invalid_request
- context_window_exceeded
- rate_limited
- timeout
- provider_unavailable
- content_filtered
- response_malformed
- cancelled
- unknown

错误保留 LiteLLM exception type、provider、model、retry-after、attempt 和脱敏 cause。是否可重试由错误类别、LiteLLM 配置和剩余预算共同决定。

## 6. 系统提示词分层

最终提示词按固定顺序组合：

1. `sdk_core`：通用 Agent 行为、工具协议和安全不变量。
2. `profile`：General 或 Coding Profile。
3. `application`：应用自定义片段。
4. `capabilities`：当前 Tools、MCP、Skills 的可用性说明。
5. `runtime`：Workflow、Child、等待、预算和当前状态。
6. `context`：Context Capsule、近期消息和当前用户输入。

每个片段包含 id、版本、来源、优先级、内容 hash 和启用条件。应用可：

- 追加片段。
- 替换某一层。
- 禁用可选层。
- 明确 `replace_all=True` 完全替换 SDK 默认提示词。

完全替换时，SDK 仍通过模型 Tool Schema 传递工具定义；权限不会因 Prompt 缺少安全说明而失效。

## 7. Prompt Manifest

每个 ModelCall 保存 Prompt Manifest：

- 片段顺序、版本和 hash。
- Context View id 和 source ranges。
- Tool Schema fingerprints。
- Model、参数和预算估算。
- 应用标记为 Secret 的内容只保存 hash。

默认不重复持久化已经位于 Ledger/Artifact 的大内容，而是引用其 id。

## 8. Profiles

### General Profile

提供任务理解、证据优先、工具使用、错误透明、权限等待、完成标准和结果汇报的默认行为。

### Coding Profile

在 General 基础上增加：

- 先理解 workspace、约束和现有变更。
- 使用 read/search/edit/bash 等工具的推荐顺序。
- 文件修改后执行比例适当的验证。
- 不覆盖用户既有改动。
- 复杂任务采用 Explore → Plan → Approve → Implement → Verify → Report 模板。

Profile 是可版本化 Prompt 资源，不是 Runtime 硬编码分支。

## 9. 压缩模型和评估模型

`compaction_model`、`evaluation_model` 和 `analysis_model` 都是 LiteLLM model 配置：

- 未配置时回退到当前 Agent model。
- 可独立设置预算、超时和 retry。
- 它们的调用进入相同 usage、cost 和 trace 体系，但标记用途。

## 10. 验收标准

- 使用不同 LiteLLM provider/model 时，Runtime 看到相同内部事件序列语义。
- Prompt Manifest 可复现每次调用的组成和版本。
- Secret 不出现在事件、错误或 Prompt Manifest 明文中。
- provider timeout、rate limit、流式中断和 malformed tool arguments 有稳定错误分类。
- 未返回成本的模型不会被统计为零成本。

