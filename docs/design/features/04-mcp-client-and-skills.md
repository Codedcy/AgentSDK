# MCP Client 与 Agent Skills

## 1. MCP 范围

首版只实现 MCP Client。SDK 作为 MCP Host 的一部分，为每个外部 Server 建立独立 Client 连接，发现并消费 Tools、Resources 和 Prompts。

兼容基线为 MCP 2025-11-25 稳定修订，并通过初始化/能力协商处理 Server 支持的版本和功能。标准 Transport：

- stdio：SDK 启动本地 Server 子进程。
- Streamable HTTP：连接远程 endpoint。

Legacy HTTP+SSE 不作为首版必需 Transport；若官方 Python SDK 提供低成本兼容，可作为明确标记的可选模式，不能影响标准路径。

## 2. MCP Server 配置

```python
MCPServerConfig(
    name="github",
    transport=StdioConfig(...) | StreamableHTTPConfig(...),
    enabled=True,
    startup_timeout=...,
    request_timeout=...,
    secret_refs={...},
    trust_level="untrusted",
    capability_allowlist={"tools", "resources", "prompts"},
)
```

Server name 在 SDK 实例内唯一并进入 Tool namespace。凭证由 SecretResolver 注入；配置 snapshot 只保存引用和 hash。

## 3. Connection Manager

每个连接状态：

```text
configured -> connecting -> negotiating -> ready
ready -> degraded -> reconnecting -> ready
* -> disabled | failed | closed
```

职责：

- 建立和关闭 Transport。
- 协议版本、ClientCapabilities、ServerCapabilities 协商。
- 缓存 tools/resources/prompts 列表及其 Schema hash。
- 处理列表变更通知、进度、日志和取消。
- 输出连接健康事件和诊断。
- 连接恢复后重新协商，不假定旧 session 状态仍有效。

## 4. MCP Tools

- `tools/list` 结果规范化到 ToolSpec。
- 名称为 `mcp.<server>.<tool>`。
- 调用统一经过 Schema、Policy、timeout、cancel、Trace 和 Evaluator。
- Server annotations、描述和幂等性信息视为不受信提示；权限由本地 Policy 决定。
- 结构化和非结构化 Tool Result 均保留；大内容或二进制写 Artifact。
- Transport 在调用中断时若无法确定 Server 是否执行副作用，结果为 `outcome_unknown`，不得静默重试。

## 5. MCP Resources

Resources 是应用/Context Layer 控制的数据源，不直接作为模型可任意调用的工具：

- 应用可显式读取并加入 Context。
- Skill、Workflow 或 Agent 可通过受控 resource-read 能力请求读取。
- URI、mime type、大小、audience、priority 和 source server 进入元数据。
- 读取前执行本地数据访问 Policy；敏感资源在发送给模型前需要允许。
- 二进制资源写 Artifact，Context 使用元数据或派生文本。

## 6. MCP Prompts

Prompts 属于用户/应用控制模板：

- 可列出、获取、参数化并转换为 SDK Prompt Fragment。
- 默认不由模型自主枚举或注入。
- 来源、Server、参数、内容 hash 和版本进入 Prompt Manifest。
- Prompt 内容不能覆盖 Runtime 权限或系统不变量。

## 7. MCP 事件

- `mcp.connection.configured|connecting|ready|degraded|failed|closed`
- `mcp.capabilities.negotiated`
- `mcp.catalog.changed`
- `mcp.request.started|progress|completed|failed|cancelled`
- `mcp.log.received`

stdio Server 的 stderr 作为独立日志流捕获；stdout 仅用于协议，非法输出记录协议错误。

## 8. Skill 格式

Skill 目录至少包含 `SKILL.md`，支持官方字段：

- 必填：`name`、`description`。
- 可选：`license`、`compatibility`、`metadata`、实验性 `allowed-tools`。
- 可选目录：`scripts/`、`references/`、`assets/`。

默认严格模式遵循 [Agent Skills specification](https://agentskills.io/specification)。可选 lenient 模式只放宽非安全的格式问题，并产生诊断；缺失 description 或完全无法解析时跳过。

## 9. Skill Roots 与优先级

应用显式配置 Skill roots，推荐分层：

1. SDK/Plugin bundled。
2. User-level。
3. Workspace/project-level。
4. Application-injected。

同名冲突不静默覆盖。默认报告冲突并要求配置选择来源；应用可配置固定优先级。

## 10. 渐进加载

三阶段：

1. Discovery：解析并验证目录，只把 name、description、location、source 和 hash 加入 Catalog。
2. Activation：用户显式点名或模型根据 Catalog 选择后，加载完整 `SKILL.md`。
3. Execution：根据指令按需读取 references/assets，或通过 Tool Registry 执行 scripts。

每次激活记录 Skill revision、触发方式、加载 token 和 source。Session 中 Skill 文件变化时，新激活生成新 revision；已运行 Step 仍引用旧 snapshot。

## 11. Skill 安全

- Skill 文本、脚本和资源视为外部输入，保存来源与 trust level。
- `allowed-tools` 仅是兼容性/提示信息，不能授予权限。
- scripts 不直接由 Skill Registry 执行；转换为受 Policy 约束的 bash/python ToolCall。
- 引用路径必须位于 Skill root，解析 symlink 后不能逃逸。
- 不自动下载远程资源或安装依赖；需要网络/安装时产生权限请求。
- Prompt Composer 用边界标记区分 SDK 系统指令与 Skill 内容。

## 12. 验收标准

- stdio 和 Streamable HTTP 的连接、协商、发现、调用、取消和重连通过集成测试。
- MCP Tool 无法绕过本地 Policy，未知调用结果不自动重试。
- Resource 和 Prompt 的控制主体与 MCP 规范一致。
- 大/binary MCP 结果通过 Artifact 安全传递。
- Skill discovery 只加载小型 Catalog；激活后才加载完整指令。
- 非法 Skill、同名冲突、路径逃逸和脚本权限有明确诊断。

