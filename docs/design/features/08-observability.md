# Observability

## 1. 原则

Domain Event 是运行事实的权威记录。状态快照、Trace 树、usage、工作流视图和分析维度均由事件及不可变配置 snapshot 投影得到。

事件写入与提交后分发分离：Runtime 的正确性只依赖事务 Event Store，不依赖外部 Exporter 是否健康。

## 2. Event Envelope

```python
EventEnvelope(
    event_id=UUID,
    schema_version=1,
    type="tool.call.completed",
    sequence=42,
    occurred_at=...,
    session_id=...,
    run_id=...,
    span_id=...,
    parent_span_id=...,
    actor=ActorRef(...),
    source=SourceRef(...),
    correlation=Correlation(...),
    payload={...},
    artifact_refs=[...],
    redaction=RedactionMetadata(...),
)
```

- `sequence` 在 Run 内严格递增，Event id 全局唯一。
- 同一事务多事件有 transaction position 保证稳定顺序。
- 时间同时保存 wall clock 和可用的 duration/monotonic 数据。
- Payload 使用按 event type 独立版本化的 Pydantic Schema。

## 3. 事件兼容性

- 只新增可选字段属于向后兼容。
- 删除、重命名或改变语义必须增加 payload/schema version。
- Projector 通过 upcaster 读取旧事件。
- 未知事件类型可被旧客户端跳过；核心 projector 对未知必需状态事件应停止并报告版本不兼容。

## 4. Span 与执行树

Span 是事件的可查询关联，不是第二套事实：

```text
Session
└─ Run span
   ├─ Step span
   │  ├─ ModelCall span
   │  └─ ToolCall spans
   ├─ Workflow span
   │  └─ Node spans
   └─ Child Run spans
```

MCP、LiteLLM 和 Exporter 可传播 W3C Trace Context；外部 trace id 保存在 correlation 字段。内部 Event id/Run id 仍为恢复依据。

## 5. 状态投影

内置 read models：

- Session summary。
- Run snapshot 和 terminal result。
- Execution tree。
- Timeline。
- Workflow/Node 状态。
- Child list/progress。
- Permission/Input/Approval inbox。
- Usage/budget。
- Tool health。
- Exporter health。

投影更新与核心事件同事务；大型分析投影可异步处理并暴露 `as_of_cursor`。

## 6. 公共查询接口

```python
await sdk.runs.get(run_id)
await sdk.runs.timeline(run_id, filters=...)
await sdk.runs.execution_tree(run_id)
await sdk.workflows.get(workflow_run_id)
await sdk.subagents.list(parent_run_id)
await sdk.usage.get(scope=...)
await sdk.events.query(filters=..., cursor=..., limit=...)

async for event in sdk.events.subscribe(filters=..., cursor=...): ...
```

查询结果带 `as_of_sequence/cursor`，让应用知道快照新鲜度。

## 7. 订阅语义

- 持久 Event Store 分配 cursor。
- subscriber 从 cursor 后读取，交付语义为至少一次。
- 应用用 event id 去重。
- 断线后从最后确认 cursor 恢复。
- 慢订阅者不阻塞 Runtime；达到缓冲上限后从持久存储追赶。

进程内低延迟订阅也在事务提交后触发，绝不发布随后回滚的事件。

## 8. Exporters

内置：

- Python callback/subscriber。
- JSONL。
- OpenTelemetry spans/metrics/logs 映射。

用户可实现 async Exporter 协议。Exporter 有独立 cursor、batch、retry/backoff 和 health 状态。永久失败进入 dead-letter/diagnostic 状态；不得让 Run 失败。

## 9. Capture 与隐私

Capture level：

- `metadata`：类型、状态、大小、hash 和指标。
- `preview`：元数据加有界脱敏预览，默认。
- `full`：保存脱敏后的完整 payload 或 Artifact。

Redactor 在事件持久化和 Export 前执行，支持 SecretRef、header、环境变量、路径和应用自定义字段。Redactor 失败时对敏感事件采取 fail-closed：只保存元数据和错误，不保存原文。

应用负责对查询接口实施身份认证和授权；SDK 在请求中保留 actor/tenant metadata 供应用过滤。

## 10. Usage

记录并聚合：

- prompt/completion/cached/reasoning token。
- estimated 与 actual 标记。
- LiteLLM cost、币种和 unknown。
- model、tool、compaction、evaluation、analysis 用途。
- 延迟、排队、等待用户、执行和恢复时长。

按 Session、Run、Step、Workflow、Node、Child、Agent revision、model 和时间窗口查询。

## 11. 健康和背压

- Store、MCP connections、Exporter、scheduler 和 lease renewer 发布 health events。
- Delta 高频事件可配置采样或合并，但 started/completed/failed、usage 和状态 transition 不可丢弃。
- 大 Payload 写 Artifact，事件只保留引用。
- 事件保留与 Session 删除遵循同一生命周期。

## 12. 验收标准

- 在 Run 任意阶段能查询准确状态、当前动作、等待原因和 execution tree。
- 订阅断开后按 cursor 恢复，无静默丢失；重复事件可去重。
- Exporter 崩溃或变慢不影响 Runtime。
- 事件 Schema 升级和旧数据库重放通过测试。
- Secret 不出现在默认 preview/full Trace。
- token/cost 聚合可下钻到具体 ModelCall。

