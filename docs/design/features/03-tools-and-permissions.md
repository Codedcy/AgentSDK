# Tools 与权限

## 1. 统一 Tool 模型

Built-in、用户和 MCP Tools 都规范化为 `ToolSpec`：

```python
ToolSpec(
    name="...",
    description="...",
    input_model=InputModel,
    output_model=OutputModel | None,
    handler=callable,
    version="...",
    source=ToolSource(...),
    effects=[EffectDescriptor(...)],
    timeout_seconds=60,
    idempotency=Idempotency.UNKNOWN,
    parallel_safe=False,
    output_limit=...,
)
```

名称在 Registry 内唯一。内置工具使用稳定名称；MCP 工具命名为 `mcp.<server>.<tool>`。用户覆盖必须在配置中点名旧、新来源，不能因加载顺序静默覆盖。

## 2. 用户工具注册

支持：

- 带类型注解的 async/sync Python 函数。
- `@tool` decorator。
- 实现调用协议的类实例。

Pydantic 模型或类型注解生成 JSON Schema。同步函数在线程执行器运行，并继承取消/timeout 限制；CPU 或不可中断任务建议由应用封装为外部进程。

`ToolContext` 提供 Session/Run/Call id、workspace 解析器、取消信号、事件发送器、Artifact writer 和只读配置。它不暴露绕过 Policy 的原始执行器。

## 3. 内置 Coding Tools

- `read`：有界读取文本或二进制元数据。
- `list`：列出目录项。
- `glob`：按 workspace 内模式查找文件。
- `grep`：文本搜索，结果有数量和字节上限。
- `write`：新建或原子替换文件。
- `edit`：带 expected hash/文本的精确编辑，检测并发变化。
- `bash`：在配置 cwd 和环境下执行命令，保存 stdout、stderr、exit code 和 timeout。

大输出写入 Artifact，模型只得到有界预览和引用。文件工具使用真实路径校验；写入先在目标目录创建临时文件再原子替换。`edit` 不命中或命中多处时失败，不猜测修改位置。

## 4. ToolCall 生命周期

```text
proposed -> schema_validated -> authorized -> running
proposed/validated -> waiting_permission -> authorized|denied
running -> completed|failed|timed_out|cancelled|outcome_unknown
```

每个状态产生事件。Tool Outcome 保存结构化结果、Artifact 引用、错误、耗时、重试 Attempt 和有效性信号。

## 5. 权限请求

规范化请求包含：

- subject：AgentSpec、Run、Child、Tool、Skill/MCP 来源。
- action：read、write、delete、execute、network、spawn 等。
- resource：真实路径、命令、host 或其他结构化资源。
- context：cwd、workspace、参数摘要、workflow node 和风险标记。

策略结果固定为 `allow`、`deny` 或 `ask`。

## 6. 规则和优先级

规则可匹配：

- Agent/Tool/Skill/MCP 来源。
- action。
- workspace root、目录或文件 glob。
- executable、规范化 argv、cwd 和 shell 风险。
- network host/port 等扩展资源。
- Run/Session/Profile 标签。

优先级：

1. 显式 deny。
2. 更具体的 allow。
3. 更具体的 ask。
4. 默认策略。

同优先级冲突时 deny 胜出。完全放行是显式 Policy mode，仍记录请求和命中规则。

## 7. Workspace 与路径安全

- Session 可配置多个 workspace root，每个 root 可有独立 read/write/delete 规则。
- 所有输入路径先相对指定 workspace 解析，再解析符号链接和真实父目录。
- 不存在的写入目标以最近存在父目录的真实路径验证。
- Windows 处理盘符、UNC、大小写和保留设备名；POSIX 处理 mount/symlink escape。
- 路径校验在执行前再次进行，降低 TOCTOU 风险；无法消除的竞态记录在风险文档。

## 8. Bash 权限

- 尽量解析 executable 与 argv；不以简单字符串前缀判断安全。
- 管道、重定向、命令替换、shell builtin 和复合表达式生成风险标记。
- 规则可放行精确 executable、参数模式和 cwd。
- 无法可靠规范化时降级为 `ask`，除非完全放行。
- 环境变量按 allowlist 注入；Secret 环境值不进入 Trace。

## 9. 用户决策范围

用户可选择：

- `once`：仅当前 PermissionRequest。
- `run`：当前 Run 内相同规范化请求。
- `session`：当前 Session 内相同请求。
- `persistent`：写入 Policy Store，供后续 Session 使用。

决定包含 decision id、用户/应用 actor、规则快照、创建时间和可选说明。持久规则可枚举、撤销和审计。

没有 Permission Bridge 时，`ask` 立即变为带原因的 deny。应用 Bridge 只负责展示和收集决定；SDK 负责暂停、持久化和恢复。

## 10. Tool Effect Metadata

工具声明 effect 与幂等性，但声明不被盲目信任：

- 内置工具由 SDK 保证。
- 用户工具由应用信任级别标记。
- MCP annotation/description 视为提示，不能自动授予权限。

Tool Evaluator 结合声明和观测结果判断失败、空结果、重复调用、结果利用率和贡献度。

## 11. 验收标准

- 路径和命令规则覆盖 allow、deny、ask、完全放行及冲突优先级。
- symlink、`..`、大小写、UNC 和不存在目标不能绕过 workspace。
- 四级用户放行范围在重启后保持正确语义。
- 没有 Bridge 时敏感工具不会挂起或默认放行。
- 自定义工具可被取消、限时、记录和评估。
- 同名工具冲突必须显式解决。

