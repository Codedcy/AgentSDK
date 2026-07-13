# Agent SDK 设计规格入口

状态：已完成对话评审，等待书面规格复核。

本规格定义一个 Python、asyncio-first、headless 的通用 Agent Runtime。模型调用只使用 LiteLLM；默认存储为 SQLite，同时提供内存和自定义存储协议。SDK 包含 Agent Loop、基础 Coding Tools、权限策略、MCP Client、Agent Skills、上下文压缩、Workflow、子 Agent、可观测性、评估和跨 Run 分析。

## 阅读顺序

1. [概要设计](../../design/00-high-level-design.md)
2. [Agent Runtime 与 Session](../../design/features/01-agent-runtime-and-session.md)
3. [LiteLLM 与 Prompt Profiles](../../design/features/02-litellm-and-prompts.md)
4. [Tools 与权限](../../design/features/03-tools-and-permissions.md)
5. [MCP Client 与 Skills](../../design/features/04-mcp-client-and-skills.md)
6. [上下文与多级压缩](../../design/features/05-context-compaction.md)
7. [Workflow](../../design/features/06-workflow.md)
8. [Subagents](../../design/features/07-subagents.md)
9. [Observability](../../design/features/08-observability.md)
10. [Evaluation 与 Analytics](../../design/features/09-evaluation-and-analytics.md)
11. [恢复与错误处理](../../design/features/10-resilience-and-errors.md)
12. [测试、验证套件与参考实现](../../design/features/11-testing-and-conformance.md)

## 已确认的关键决策

- Python 实现；内部完全异步，提供同步便捷入口。
- 模型层只使用 LiteLLM，不提供公共 ModelProvider 扩展协议。
- SDK 是无界面的基础运行时；应用决定何时以及如何展示权限、Trace 和分析结果。
- 默认 SQLite 保证可恢复；`InMemoryStore` 需要显式选择。
- 完整历史与 Session 绑定；关闭保留，删除时级联清理 SDK 持久化数据。
- Runtime 采用模块化单体结构，并以不可变事件日志和可查询状态投影作为恢复、审计和观测基础。
- 工具统一注册；内置 Coding Profile，同时允许用户注册函数或类工具。
- 权限策略支持命令、路径、多个 workspace 根、`allow/deny/ask` 和四级用户放行范围。
- MCP 首版只实现 Client，支持 stdio 和 Streamable HTTP。
- Skills 对齐开放的 `SKILL.md` 格式并渐进加载。
- Workflow 使用 YAML/JSON DSL 与 Python Builder，统一编译为持久化 IR；对话生成的 Workflow 默认需验证和确认。
- 子 Agent 默认隔离上下文，只接收显式任务包；共享 Session 证据树和受限 workspace。
- 压缩只改变模型 Context View，绝不覆盖 Session 原始历史；支持自动多级、多策略压缩和原文重注入。
- Trace 不仅记录阶段状态，还支持可插拔评估、失败分类、工具有效性、跨 Run 成功率、结果归因和改进建议。
- 首次交付采用覆盖所有能力的端到端垂直切片，随后按特性加固。
- 计划阶段必须生成总路线图、多篇里程碑计划和逐任务文档，避免长任务丢失。

## 外部规范基线

- [LiteLLM 官方文档](https://docs.litellm.ai/)
- [MCP 2025-11-25 Server primitives](https://modelcontextprotocol.io/specification/2025-11-25/server/index)
- [MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [Agent Skills specification](https://agentskills.io/specification)

协议实现必须通过版本协商，不把尚未发布的 MCP 草案行为硬编码成稳定语义。

