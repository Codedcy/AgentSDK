# Agent SDK

[English](README.md) | 简体中文

Agent SDK `0.1.0` 是一个异步 Python 基础 SDK，用于构建可恢复、可观测的 Agent。它结合了由 LiteLLM 支持的 Agent 循环、工具（Tool）、MCP、Skill、经过验证的工作流（Workflow）、子 Agent（Child Agent）、自动 L0-L4 上下文（Context）压缩，以及由 SQLite 支持的执行历史。

当前版本支持 Python 3.12 和 3.13。它提供了可用的单进程 SDK 基线：应用负责用户界面、审批体验、提供方凭据，以及决定公开哪些 Trace 信息。

## 为什么选择 Agent SDK

- 默认可恢复：SQLite 是标准存储；内存存储可用于瞬态和测试组合。
- 统一的 Tool 管道：内置工具、应用 Tool、MCP Tool 和 Child 控制 Tool 共享验证、授权、执行和 Trace 事件。
- 结构化编排：Workflow 支持条件、有界循环和 Agent 节点；Child Agent 支持持久化双向消息和结果移交。
- 有界 Context：运行时自动选择并记录 L0-L4 视图，而不删除源事件。
- 证据优先：实时事件、历史时间线、token 用量、评估、确定性归因、成功率和 Tool 失败指标都是公开的 SDK 能力。

## 从源码安装

克隆仓库，并使用 Python 3.12 或 3.13 安装：

```powershell
git clone https://github.com/Codedcy/AgentSDK.git
Set-Location AgentSDK
python -m pip install .
```

仓库是 v0.1 支持的分发路径；本 README 不假定已发布到包索引。

## 五分钟确定性 smoke 运行

```powershell
python examples/v01_reference.py --smoke --database .agent-sdk/state.db --workspace .
```

`--smoke` 不会发起提供方或网络调用。它从公开 SDK 结果派生出一行 JSON，并验证：

- 自动 L0-L4 Context 选择；
- 一个 Workflow 条件和两次有界循环迭代；
- Agent 驱动的 Child 生成、双向消息、等待和结果消费；
- 实时及历史 Trace、评估和与证据关联的归因；
- 在不重放的情况下重新打开已完成的 SQLite 工作；
- 删除 Session 时应用所有的 workspace 文件仍保持完整。

该参考实现仅为保持非交互性而使用 `permission_default="allow"`。应用在准入不受信任的工作前，应配置明确的 allow、ask 和 deny 规则。

## 运行真实的 LiteLLM Agent

在应用环境中设置所选 LiteLLM 提供方要求的凭据。例如，使用 OpenAI 模型时：

```powershell
$env:OPENAI_API_KEY="your-key"
```

然后从异步应用中运行 SDK：

```python
import asyncio
from pathlib import Path

from agent_sdk import AgentSDK, AgentSDKConfig, AgentSpec


async def main() -> None:
    workspace = Path(".").resolve()
    sdk = AgentSDK(
        AgentSDKConfig(database_path=Path(".agent-sdk/state.db"))
    )
    try:
        session = await sdk.sessions.create(workspaces=(workspace,))
        agent = sdk.agents.define(
            AgentSpec(
                name="assistant",
                model="openai/gpt-4o-mini",
                tool_allowlist=(),
            )
        )
        handle = await sdk.runs.start(
            session.session_id,
            agent,
            "Give me a two-sentence overview of this project.",
        )
        result = await handle.result()
        print(result.output_text)
    finally:
        await sdk.close()


asyncio.run(main())
```

`workspaces=(workspace,)` 是有意如此：它是一个单元素 Python 元组，末尾逗号用于创建该元组。`SessionAPI.create` 接受任意由 `str` 或 `Path` 组成的可迭代对象，因此 `workspaces=[workspace]` 与其等价。

省略 `AgentSpec.system_prompt` 时，将使用打包的通用系统提示词。设置它可添加应用特定的提示词。已激活的 Skill 会作为独立提示层组合，并记录在 Prompt Manifest 中。持久化 `AgentSpec.model_params` 拒绝原始凭据字段；请将秘密保留在应用环境或提供方凭据系统中。

## v0.1 能力矩阵

| 领域 | v0.1 已交付 | 重要边界 |
| --- | --- | --- |
| Agent 循环 | 异步流式模型调用、Tool 调用、有界轮次、取消、token 计量 | LiteLLM 是唯一的模型网关 |
| 存储和恢复 | 默认 SQLite、内存选项、事件/快照历史、安全边界重新打开、显式协调 | 文档所述的恢复使用单个进程中的单个 SDK 实例 |
| Tool 与权限 | 内置 `read`、`write` 和基于 argv 的 `bash`；自定义 Tool 注册；按 Tool、路径和命令前缀配置 allow/ask/deny 规则 | 应用负责交互式权限决策 |
| MCP | Stdio 和可流式 HTTP 服务器在普通 Tool 注册表中注册带命名空间的 Tool | MCP Tool 使用相同的应用授权策略 |
| Skill 与提示词 | 可配置 Skill 根目录、严格 `SKILL.md` 激活、打包和自定义系统提示词、Prompt Manifest | 应用选择 Agent 可以激活哪些 Skill |
| Workflow | 经过验证的 YAML/对象定义、显式启动、条件、有界循环、Agent 节点、持久化状态 | 生成文本在 compile、应用确认和启动前只是候选内容 |
| Child Agent | Tool 驱动的生成/发送/列出/等待、直接 API 访问、有界深度/数量、持久化邮箱、父级结果消费 | Child 能力仍受应用策略约束 |
| Context | 模型调用前自动进行 L0-L4 规划和压缩、源事件引用、可配置预算 | 压缩是有界摘要，不会删除源历史 |
| Trace 与分析 | 实时订阅、历史时间线、执行树、每 Run 归因、评估、成功率、Tool 失败及失败率 | 归因是确定性证据分析，而非因果证明 |
| 可扩展性 | 应用 Tool、权限规则、Skill、提示词、Workflow、提供方恢复适配器、Trace 消费者 | SDK 提供原语；宿主应用提供产品策略和呈现 |

## Tool 与权限决策

应用 Tool 与内置工具和 MCP Tool 加入同一注册表：

```python
from agent_sdk import AgentSDK, ToolContext, ToolSpec


def register_lookup(sdk: AgentSDK) -> None:
    async def lookup(
        context: ToolContext,
        *,
        key: str,
    ) -> dict[str, str]:
        return {"run_id": context.run_id, "value": key.upper()}

    sdk.tools.register(
        ToolSpec(
            name="lookup",
            description="Look up an application value",
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
            effects=("application.read",),
        ),
        lookup,
    )
```

创建 SDK 时配置 workspace 和命令策略：

```python
from pathlib import Path

from agent_sdk import AgentSDK, AgentSDKConfig, PermissionDecision
from agent_sdk.permissions import PermissionRule


def create_sdk(workspace: Path) -> AgentSDK:
    return AgentSDK(
        AgentSDKConfig(
            database_path=Path(".agent-sdk/state.db"),
            permission_default="ask",
            permission_rules=(
                PermissionRule(
                    outcome="allow",
                    tool="read",
                    path_prefix=workspace,
                ),
                PermissionRule(
                    outcome="allow",
                    tool="bash",
                    path_prefix=workspace,
                    command_prefix=("git", "status"),
                ),
                PermissionRule(outcome="deny", tool="write"),
            ),
        )
    )


async def resolve_next_request(sdk: AgentSDK, run_id: str) -> None:
    request = await sdk.permissions.next_request(run_id)
    await sdk.permissions.resolve(
        request.request_id,
        PermissionDecision.allow_once(),
    )
```

内置 `read`、`write` 和 `bash` 会同时强制执行 Session 的 workspace 根目录约束和已配置策略。SDK 会暂停 `ask` 决策；宿主应用决定何时以及如何展示该请求，然后调用 `allow_once()` 或 `PermissionDecision.deny(...)`。

将 MCP 服务器接入相同的 Tool 注册表和授权路径：

```python
from pathlib import Path

from agent_sdk import AgentSDK, MCPManager, MCPServerConfig, StdioMCPTransport


async def connect_company_mcp(
    sdk: AgentSDK,
    workspace: Path,
) -> MCPManager:
    manager = MCPManager(sdk.tools)
    await manager.connect(
        MCPServerConfig(
            name="company",
            transport=StdioMCPTransport(
                command="python",
                args=("mcp_server.py",),
                cwd=workspace,
            ),
        )
    )
    return manager
```

调用方拥有返回的 manager，并必须在其 Run 安定后运行 `await manager.close()`。

## 生成式 Workflow 准入

模型生成的 YAML 是不受信任的候选文本。请先 compile 并验证它，获得明确的应用确认，然后才启动它：

```python
from collections.abc import Awaitable, Callable

from agent_sdk import AgentSDK, WorkflowIR, WorkflowResult


async def run_confirmed_workflow(
    sdk: AgentSDK,
    session_id: str,
    generated_yaml: str,
    application_confirms: Callable[[WorkflowIR], Awaitable[bool]],
) -> WorkflowResult:
    candidate = sdk.workflows.compile(generated_yaml)  # validates; no execution
    if not await application_confirms(candidate):
        raise RuntimeError("workflow was not approved")

    handle = await sdk.workflows.start(session_id, candidate)
    return await handle.result()
```

条件从持久化输入/输出中选择分支。循环需要有限的 `max_iterations`，因此生成的 Workflow 无法请求无界循环。

## 观测与恢复

工作处于活动状态时，使用 `sdk.trace.subscribe(...)` 消费标准化实时事件。之后，使用 `sdk.trace.timeline(run_id)` 和 `sdk.trace.attribution(run_id)` 检查阶段、Tool 调用、Child 进度、Workflow 状态、token 用量、证据、失败阶段、贡献者和建议改进。`sdk.evaluations` 和 `sdk.analytics` 提供确定性评估、成功率、Tool 失败和 Tool 失败率查询。

父 Agent 可以获得常规已授权的 `spawn_agent`、`send_message`、`list_children` 和 `wait_child` Tool。同样的操作也可直接通过 `sdk.children` 使用；邮箱消息、进度和最终 Child 结果会被持久化，并纳入父级后续 Context。

重启后，已完成的安全边界工作会被读取而不会重复。未知的进行中工作保持中断，直到应用检查 `sdk.recovery.pending_requests(run_id)` 并作出处理。终止决策可以在不重放提供方或 Tool 的情况下中止：

```python
from agent_sdk import AgentSDK, ReconciliationAction


async def terminate_unknown_attempt(
    sdk: AgentSDK,
    request_id: str,
) -> None:
    await sdk.recovery.resolve(
        request_id,
        ReconciliationAction.TERMINATE,
        actor={"type": "operator", "id": "user-123"},
        evidence={"reason": "application chose not to retry"},
    )
```

终止不会执行重放，也不会声明中断的外部尝试是否已执行。它以原子方式使 Run 失败，错误代码为 `application_resolution_aborted`。删除 Session 会移除该 Session 中由 SDK 持久化的历史、事件和快照；不会删除任何由应用拥有的 workspace 文件。

## v0.1 边界

- 支持 Python 3.12 和 3.13；Python 3.14 不在本版本支持范围内。
- 从此仓库源码安装；不假定已发布到包索引。
- 文档所述的恢复模型限定为单个进程中的单个 SDK 实例；v0.1 不包含多 worker 协同恢复。
- 外部副作用不提供 exactly-once 保证。未知尝试必须由应用显式作出协调决策。
- 生成的 Workflow 不会在生成后自动执行；它必须经过 compile、验证、应用确认和显式启动。
- 跨 Run 的多维 Trace 聚合分析、无用结果评分、高级调度、导出器和额外可靠性加固属于 v0.1 之后的工作。

## 文档

- [v0.1 快速入门](docs/guides/v01-quickstart.md)
- [恢复指南](docs/guides/v01-recovery.md)
- [追踪与分析指南](docs/guides/v01-tracing-and-analysis.md)
- [高层设计](docs/design/00-high-level-design.md)
- [v0.1 发布台账](docs/plans/releases/v0.1.md)

## 开发与验证

创建隔离的 Python 3.12 或 3.13 环境，以可编辑模式安装项目，并安装 `pyproject.toml` 中声明的开发依赖：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pip install "pytest>=8,<9" "pytest-asyncio>=0.25,<1" "hypothesis>=6,<7" "ruff>=0.9,<1" "mypy>=1.14,<2" "types-jsonschema>=4.23,<5" "types-PyYAML>=6,<7"
```

运行发布检查：

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy
```

v0.1 发布检查点记录为 2,956 passed、6 个预期的平台跳过。发布台账记录了该可复现的历史环境和 v0.1 之后的剩余工作；请为当前代码树重新运行上述检查。
