# Quickstart General Agent Example Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a small interactive example that builds a durable, permission-aware, observable general Agent with the public Agent SDK API.

**Architecture:** `examples/quickstart_agent.py` owns CLI parsing, SDK/Session setup, one permission-aware Run loop, compact Trace summaries, and the interactive conversation loop. Tests inject a deterministic LiteLLM-shaped provider through `AgentSDK.for_test`, while the real CLI constructs the normal SQLite-backed `AgentSDK`. Both README languages point to this example as the first general-Agent entry point.

**Tech Stack:** Python 3.12/3.13, asyncio, argparse, Agent SDK public APIs, LiteLLM, SQLite, pytest, pytest-asyncio, Ruff, mypy.

## Global Constraints

- Keep the example focused on Agent Loop, durable Session history, built-in `read`/`write`/`bash`, application permission decisions, and compact observability.
- Do not add MCP, Skills, Workflows, Child Agents, evaluations, aggregate analytics, custom Tools, or a terminal/web UI.
- Use `.agent-sdk/quickstart.db` as the default SQLite path and `openai/gpt-4o-mini` as the documented default model.
- Keep provider credentials in environment variables; never place credentials in `AgentSpec.model_params`.
- Use only package-root exports and documented public modules such as `agent_sdk.permissions`.
- Do not delete the Session or workspace on `exit`, end-of-input, or `Ctrl+C`.
- Tests must use a deterministic provider and must not open network sockets.

## File Structure

- Create `examples/quickstart_agent.py`: the complete runnable example and its small reusable functions.
- Create `tests/integration/examples/test_quickstart_agent.py`: parser, Session, Agent, permission, Trace summary, and conversation tests.
- Modify `tests/docs/test_public_readme.py`: bilingual quickstart command contract.
- Modify `README.md`: English quickstart entry point and usage.
- Modify `README.zh-CN.md`: equivalent Simplified Chinese entry point and usage.

---

### Task 1: CLI Configuration, Session Selection, and Agent Definition

**Files:**
- Create: `examples/quickstart_agent.py`
- Create: `tests/integration/examples/test_quickstart_agent.py`

**Interfaces:**
- Consumes: `AgentSDK`, `AgentSDKConfig`, `AgentSDKError`, `AgentSpec`, `ErrorCode`, `SessionSnapshot`, and `PermissionRule`.
- Produces: `build_parser() -> argparse.ArgumentParser`, `create_sdk(args: argparse.Namespace) -> AgentSDK`, `select_session(sdk: AgentSDK, workspace: Path, session_id: str | None) -> Awaitable[SessionSnapshot]`, and `define_agent(sdk: AgentSDK, model: str) -> AgentSpec`.

- [ ] **Step 1: Write failing parser, Session, Agent, and import-boundary tests**

Create `tests/integration/examples/test_quickstart_agent.py` with these initial tests and helpers:

```python
from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import AgentSDK
from agent_sdk.storage.memory import InMemoryStore
from examples.quickstart_agent import (
    build_parser,
    define_agent,
    select_session,
)


async def _unexpected_provider(**_: Any) -> AsyncIterator[dict[str, object]]:
    raise AssertionError("provider must not be called")


def test_quickstart_uses_only_public_agent_sdk_imports() -> None:
    path = Path(__file__).parents[3] / "examples" / "quickstart_agent.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("agent_sdk")
    }
    assert modules <= {"agent_sdk", "agent_sdk.permissions"}


def test_parser_supplies_documented_defaults() -> None:
    args = build_parser().parse_args([])

    assert args.model == "openai/gpt-4o-mini"
    assert args.database == Path(".agent-sdk/quickstart.db")
    assert args.workspace == Path(".")
    assert args.session_id is None


@pytest.mark.asyncio
async def test_select_session_creates_then_reopens_same_session(
    tmp_path: Path,
) -> None:
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unexpected_provider,
    )
    try:
        created = await select_session(sdk, tmp_path, None)
        reopened = await select_session(sdk, tmp_path, created.session_id)

        assert reopened.session_id == created.session_id
        assert reopened.workspaces == (str(tmp_path.resolve()),)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_general_agent_exposes_only_workspace_tools(tmp_path: Path) -> None:
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=_unexpected_provider,
    )
    try:
        agent = define_agent(sdk, "fake/general")

        assert agent.name == "quickstart"
        assert agent.model == "fake/general"
        assert agent.system_prompt is None
        assert agent.tool_allowlist == ("read", "write", "bash")
    finally:
        await sdk.close()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m pytest tests/integration/examples/test_quickstart_agent.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'examples.quickstart_agent'`.

- [ ] **Step 3: Implement the parser, SDK policy, Session selection, and Agent**

Create `examples/quickstart_agent.py` with:

```python
from __future__ import annotations

import argparse
from pathlib import Path

from agent_sdk import (
    AgentSDK,
    AgentSDKConfig,
    AgentSpec,
    SessionSnapshot,
)
from agent_sdk.permissions import PermissionRule


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a small general Agent with Agent SDK.",
    )
    parser.add_argument("--model", default="openai/gpt-4o-mini")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(".agent-sdk/quickstart.db"),
    )
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--session-id")
    return parser


def create_sdk(args: argparse.Namespace) -> AgentSDK:
    workspace = args.workspace.resolve()
    return AgentSDK(
        AgentSDKConfig(
            database_path=args.database,
            permission_default="ask",
            permission_rules=(
                PermissionRule(
                    outcome="allow",
                    tool="read",
                    path_prefix=workspace,
                ),
                PermissionRule(
                    outcome="ask",
                    tool="write",
                    path_prefix=workspace,
                ),
                PermissionRule(
                    outcome="ask",
                    tool="bash",
                    path_prefix=workspace,
                ),
            ),
        )
    )


async def select_session(
    sdk: AgentSDK,
    workspace: Path,
    session_id: str | None,
) -> SessionSnapshot:
    if session_id is not None:
        return await sdk.sessions.get(session_id)
    return await sdk.sessions.create(workspaces=(workspace.resolve(),))


def define_agent(sdk: AgentSDK, model: str) -> AgentSpec:
    return sdk.agents.define(
        AgentSpec(
            name="quickstart",
            model=model,
            tool_allowlist=("read", "write", "bash"),
        )
    )
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```powershell
python -m pytest tests/integration/examples/test_quickstart_agent.py -v
```

Expected: four tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add examples/quickstart_agent.py tests/integration/examples/test_quickstart_agent.py
git commit -m "feat: scaffold quickstart general agent"
```

---

### Task 2: Permission-Aware Turn Execution and Trace Summary

**Files:**
- Modify: `examples/quickstart_agent.py`
- Modify: `tests/integration/examples/test_quickstart_agent.py`

**Interfaces:**
- Consumes: `PermissionRequest`, `PermissionDecision`, `RunResult`, `TraceStageKind`, `sdk.runs.start`, `sdk.permissions.next_request`, `sdk.permissions.resolve`, and `sdk.trace.timeline`.
- Produces: `PermissionResolver`, immutable `RunSummary`, `execute_turn(...) -> Awaitable[RunResult]`, and `summarize_run(...) -> Awaitable[RunSummary]`.

- [ ] **Step 1: Add a deterministic Tool-call provider**

Append these helpers to `tests/integration/examples/test_quickstart_agent.py`:

```python
import json

from agent_sdk import AgentSpec, PermissionDecision, PermissionRequest
from agent_sdk.permissions import PermissionRule


def _text_stream(text: str) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [{"delta": {"content": text}, "finish_reason": "stop"}]
        }
        yield {
            "choices": [],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        }

    return chunks()


def _tool_stream(
    name: str,
    arguments: dict[str, object],
) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "quickstart-call",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    return chunks()


class WriteThenAnswerProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_: Any) -> object:
        self.calls += 1
        if self.calls == 1:
            return _tool_stream(
                "write",
                {"path": "note.txt", "content": "hello"},
            )
        return _text_stream("finished")
```

- [ ] **Step 2: Write failing allow-once and Trace-summary tests**

Append:

```python
from examples.quickstart_agent import execute_turn, summarize_run


@pytest.mark.asyncio
async def test_execute_turn_resolves_asked_write_and_summarizes_trace(
    tmp_path: Path,
) -> None:
    provider = WriteThenAnswerProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
        permission_rules=(
            PermissionRule(
                outcome="ask",
                tool="write",
                path_prefix=tmp_path.resolve(),
            ),
        ),
    )
    requests: list[PermissionRequest] = []

    async def allow(request: PermissionRequest) -> PermissionDecision:
        requests.append(request)
        return PermissionDecision.allow_once()

    try:
        session = await sdk.sessions.create(workspaces=(tmp_path,))
        agent = sdk.agents.define(
            AgentSpec(
                name="quickstart",
                model="fake/general",
                tool_allowlist=("read", "write", "bash"),
            )
        )

        result = await execute_turn(
            sdk,
            session.session_id,
            agent,
            "write a note",
            resolve_permission=allow,
        )
        summary = await summarize_run(sdk, result)

        assert result.output_text == "finished"
        assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello"
        assert [request.tool_name for request in requests] == ["write"]
        assert summary.run_id == result.run_id
        assert summary.total_tokens == 3
        assert summary.tools == ("write",)
    finally:
        await sdk.close()
```

- [ ] **Step 3: Run the new test and verify RED**

Run:

```powershell
python -m pytest tests/integration/examples/test_quickstart_agent.py::test_execute_turn_resolves_asked_write_and_summarizes_trace -v
```

Expected: import fails because `execute_turn` and `summarize_run` do not exist.

- [ ] **Step 4: Implement permission-aware execution and Trace summary**

Add these imports and definitions to `examples/quickstart_agent.py`:

```python
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from agent_sdk import (
    PermissionDecision,
    PermissionRequest,
    RunResult,
    TraceStageKind,
)


PermissionResolver = Callable[
    [PermissionRequest],
    Awaitable[PermissionDecision],
]


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    total_tokens: int | None
    tools: tuple[str, ...]


async def _settle_permission_waiter(
    waiter: asyncio.Task[PermissionRequest],
) -> PermissionRequest | None:
    if waiter.done():
        if waiter.cancelled():
            return None
        return waiter.result()
    waiter.cancel()
    with suppress(asyncio.CancelledError):
        await waiter
    return None


async def execute_turn(
    sdk: AgentSDK,
    session_id: str,
    agent: AgentSpec,
    user_input: str,
    *,
    resolve_permission: PermissionResolver,
) -> RunResult:
    handle = await sdk.runs.start(session_id, agent, user_input)
    result_waiter = asyncio.create_task(handle.result())
    permission_waiter: asyncio.Task[PermissionRequest] | None = None
    pending_request: PermissionRequest | None = None
    try:
        while not result_waiter.done():
            permission_waiter = asyncio.create_task(
                sdk.permissions.next_request(handle.run_id)
            )
            done, _ = await asyncio.wait(
                {result_waiter, permission_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if result_waiter in done:
                pending_request = await _settle_permission_waiter(
                    permission_waiter
                )
                permission_waiter = None
                if pending_request is not None:
                    await sdk.permissions.resolve(
                        pending_request.request_id,
                        PermissionDecision.deny("Run already terminated"),
                    )
                    pending_request = None
                break
            pending_request = await permission_waiter
            permission_waiter = None
            decision = await resolve_permission(pending_request)
            await sdk.permissions.resolve(
                pending_request.request_id,
                decision,
            )
            pending_request = None
        return await result_waiter
    finally:
        if permission_waiter is not None:
            with suppress(BaseException):
                recovered = await _settle_permission_waiter(permission_waiter)
                if pending_request is None:
                    pending_request = recovered
        if pending_request is not None:
            with suppress(BaseException):
                await sdk.permissions.resolve(
                    pending_request.request_id,
                    PermissionDecision.deny("quickstart stopped"),
                )


async def summarize_run(
    sdk: AgentSDK,
    result: RunResult,
) -> RunSummary:
    timeline = await sdk.trace.timeline(result.run_id)
    traced_call_ids = {
        stage.entity_id
        for stage in timeline.stages
        if stage.kind is TraceStageKind.TOOL
    }
    tools = tuple(
        dict.fromkeys(
            item.tool_name
            for item in result.tool_results
            if item.call_id in traced_call_ids
        )
    )
    return RunSummary(
        run_id=result.run_id,
        total_tokens=result.usage.total_tokens,
        tools=tools,
    )
```

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run:

```powershell
python -m pytest tests/integration/examples/test_quickstart_agent.py -v
```

Expected: all quickstart tests pass.

- [ ] **Step 6: Add and verify the denial path**

Append this test:

```python
@pytest.mark.asyncio
async def test_execute_turn_can_deny_write(tmp_path: Path) -> None:
    provider = WriteThenAnswerProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
    )

    async def deny(_: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.deny("not this time")

    try:
        session = await sdk.sessions.create(workspaces=(tmp_path,))
        agent = define_agent(sdk, "fake/general")
        result = await execute_turn(
            sdk,
            session.session_id,
            agent,
            "write a note",
            resolve_permission=deny,
        )

        assert result.output_text == "finished"
        assert not (tmp_path / "note.txt").exists()
        assert result.tool_results[0].status.value == "denied"
    finally:
        await sdk.close()
```

Run:

```powershell
python -m pytest tests/integration/examples/test_quickstart_agent.py::test_execute_turn_can_deny_write -v
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```powershell
git add examples/quickstart_agent.py tests/integration/examples/test_quickstart_agent.py
git commit -m "feat: run permission-aware quickstart turns"
```

---

### Task 3: Interactive Multi-Turn Application

**Files:**
- Modify: `examples/quickstart_agent.py`
- Modify: `tests/integration/examples/test_quickstart_agent.py`

**Interfaces:**
- Consumes: `execute_turn`, `summarize_run`, `create_sdk`, `select_session`, and `define_agent`.
- Produces: `LineReader`, `LineWriter`, `prompt_for_permission`, `run_chat`, `async_main`, and `main`.

- [ ] **Step 1: Write a failing multi-turn conversation test**

Append:

```python
from examples.quickstart_agent import run_chat


class ConversationProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[tuple[dict[str, Any], ...]] = []

    async def __call__(self, **params: Any) -> object:
        self.calls += 1
        self.requests.append(tuple(dict(item) for item in params["messages"]))
        return _text_stream(f"answer-{self.calls}")


@pytest.mark.asyncio
async def test_run_chat_keeps_multiple_turns_in_one_session(
    tmp_path: Path,
) -> None:
    provider = ConversationProvider()
    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=provider,
        permission_default="ask",
    )
    inputs = iter(("first question", "second question", "exit"))
    output: list[str] = []

    async def read_line(_: str) -> str:
        return next(inputs)

    async def deny(_: PermissionRequest) -> PermissionDecision:
        return PermissionDecision.deny("not needed")

    try:
        session = await sdk.sessions.create(workspaces=(tmp_path,))
        agent = define_agent(sdk, "fake/general")
        await run_chat(
            sdk,
            session.session_id,
            agent,
            read_line=read_line,
            write_line=output.append,
            resolve_permission=deny,
        )

        assert provider.calls == 2
        assert any("answer-1" in str(message) for message in provider.requests[1])
        assert any("answer-1" in line for line in output)
        assert any("answer-2" in line for line in output)
        assert sum(line.startswith("Run ") for line in output) == 2
    finally:
        await sdk.close()
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m pytest tests/integration/examples/test_quickstart_agent.py::test_run_chat_keeps_multiple_turns_in_one_session -v
```

Expected: import fails because `run_chat` does not exist.

- [ ] **Step 3: Implement interactive input, permission prompts, output, and shutdown**

Add these imports and definitions to `examples/quickstart_agent.py`:

```python
import json
import sys
from collections.abc import Sequence

from agent_sdk import AgentSDKError, ErrorCode


LineReader = Callable[[str], Awaitable[str]]
LineWriter = Callable[[str], None]


async def _console_read(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


def _console_write(text: str) -> None:
    print(text, flush=True)


async def prompt_for_permission(
    request: PermissionRequest,
) -> PermissionDecision:
    arguments = json.dumps(
        request.model_dump(mode="json")["arguments"],
        ensure_ascii=False,
    )
    answer = await _console_read(
        f"Allow {request.tool_name} once with {arguments}? [y/N] "
    )
    if answer.strip().lower() == "y":
        return PermissionDecision.allow_once()
    return PermissionDecision.deny("user denied")


async def run_chat(
    sdk: AgentSDK,
    session_id: str,
    agent: AgentSpec,
    *,
    read_line: LineReader = _console_read,
    write_line: LineWriter = _console_write,
    resolve_permission: PermissionResolver = prompt_for_permission,
) -> None:
    while True:
        try:
            user_input = (await read_line("You> ")).strip()
        except EOFError:
            return
        if user_input.lower() in {"exit", "quit"}:
            return
        if not user_input:
            continue
        result = await execute_turn(
            sdk,
            session_id,
            agent,
            user_input,
            resolve_permission=resolve_permission,
        )
        summary = await summarize_run(sdk, result)
        write_line(f"Agent> {result.output_text}")
        tool_text = ", ".join(summary.tools) if summary.tools else "none"
        token_text = (
            str(summary.total_tokens)
            if summary.total_tokens is not None
            else "unknown"
        )
        write_line(
            f"Run {summary.run_id} | tokens={token_text} | tools={tool_text}"
        )


async def async_main(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise AgentSDKError(
            ErrorCode.INVALID_STATE,
            "workspace must be an existing directory",
            retryable=False,
        )
    sdk = create_sdk(args)
    try:
        session = await select_session(sdk, workspace, args.session_id)
        agent = define_agent(sdk, args.model)
        _console_write(f"Session: {session.session_id}")
        _console_write("Type exit to stop. The Session will be kept.")
        await run_chat(sdk, session.session_id, agent)
        return 0
    finally:
        await sdk.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        _console_write("\nStopped. The Session was kept.")
        return 130
    except AgentSDKError as error:
        _console_write(f"Agent SDK error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run the quickstart suite and verify GREEN**

Run:

```powershell
python -m pytest tests/integration/examples/test_quickstart_agent.py -v
```

Expected: all tests pass, including the two-turn history assertion.

- [ ] **Step 5: Verify the real CLI help path**

Run:

```powershell
python examples/quickstart_agent.py --help
```

Expected: exit code 0 and options for `--model`, `--database`, `--workspace`, and `--session-id`.

- [ ] **Step 6: Commit Task 3**

```powershell
git add examples/quickstart_agent.py tests/integration/examples/test_quickstart_agent.py
git commit -m "feat: add interactive quickstart agent"
```

---

### Task 4: Bilingual README Entry Point

**Files:**
- Modify: `tests/docs/test_public_readme.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`

**Interfaces:**
- Consumes: the installed-source workflow and `examples/quickstart_agent.py`.
- Produces: equivalent English and Chinese quickstart commands and recovery guidance.

- [ ] **Step 1: Write the failing bilingual documentation contract**

Add:

```python
def test_bilingual_readmes_recommend_the_general_agent_quickstart() -> None:
    english = README.read_text(encoding="utf-8")
    chinese = ZH_README.read_text(encoding="utf-8")
    command = (
        "python examples/quickstart_agent.py "
        "--model openai/gpt-4o-mini"
    )

    assert "## Build a General Agent" in english
    assert "examples/quickstart_agent.py" in chinese
    assert command in english
    assert command in chinese
    assert "--session-id" in english
    assert "--session-id" in chinese
```

- [ ] **Step 2: Run the documentation test and verify RED**

Run:

```powershell
python -m pytest tests/docs/test_public_readme.py::test_bilingual_readmes_recommend_the_general_agent_quickstart -v
```

Expected: FAIL because neither README contains the new section.

- [ ] **Step 3: Add the English quickstart section**

Insert after source installation in `README.md`:

````markdown
## Build a General Agent

Set the credentials required by your LiteLLM provider, then start the
interactive example:

```powershell
python examples/quickstart_agent.py --model openai/gpt-4o-mini
```

The example creates a SQLite-backed Session, uses the built-in `read`, `write`,
and `bash` Tools inside the selected workspace, and asks before write or command
execution. It prints the Session ID, final answer, token usage, and invoked
Tools after each turn.

To continue the same conversation after restarting the process, pass the
printed identifier:

```powershell
python examples/quickstart_agent.py --model openai/gpt-4o-mini --session-id SESSION_ID
```

Use `--workspace` and `--database` to select different paths. Enter `exit` to
close the application without deleting its Session.
````

- [ ] **Step 4: Add the equivalent Chinese section**

Insert at the same position in `README.zh-CN.md`, keeping both PowerShell blocks
byte-for-byte identical to the English version:

````markdown
## 搭建一个通用 Agent

设置 LiteLLM 提供方所需的凭据，然后启动交互式示例：

```powershell
python examples/quickstart_agent.py --model openai/gpt-4o-mini
```

该示例会创建由 SQLite 持久化的 Session，在选定的 workspace 内使用内置
`read`、`write` 和 `bash` Tool，并在写入文件或执行命令前询问用户。每轮
对话结束后，它会显示 Session ID、最终回复、token 用量和调用过的 Tool。

进程重启后，可以通过打印出的标识继续同一段对话：

```powershell
python examples/quickstart_agent.py --model openai/gpt-4o-mini --session-id SESSION_ID
```

使用 `--workspace` 和 `--database` 可以选择其他路径。输入 `exit` 会关闭
应用，但不会删除 Session。
````

- [ ] **Step 5: Run documentation contracts and verify GREEN**

Run:

```powershell
python -m pytest tests/docs/test_public_readme.py -v
```

Expected: all README tests pass, including identical bilingual command blocks.

- [ ] **Step 6: Commit Task 4**

```powershell
git add README.md README.zh-CN.md tests/docs/test_public_readme.py
git commit -m "docs: add general agent quickstart"
```

---

### Task 5: Completion Verification

**Files:**
- Verify: `examples/quickstart_agent.py`
- Verify: `tests/integration/examples/test_quickstart_agent.py`
- Verify: `tests/docs/test_public_readme.py`
- Verify: `README.md`
- Verify: `README.zh-CN.md`

**Interfaces:**
- Consumes: all deliverables from Tasks 1-4.
- Produces: evidence that the example is tested, formatted, typed, documented, and runnable.

- [ ] **Step 1: Run focused behavioral and documentation tests**

```powershell
python -m pytest tests/integration/examples/test_quickstart_agent.py tests/docs/test_public_readme.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run neighboring example regression tests**

```powershell
python -m pytest tests/integration/examples/test_reference_cli.py tests/e2e/test_v01_reference_example.py -q
```

Expected: all existing reference-example tests pass.

- [ ] **Step 3: Run static verification**

```powershell
python -m ruff check examples tests/integration/examples tests/docs/test_public_readme.py
python -m mypy src/agent_sdk
git diff --check
```

Expected: all commands exit 0 with no diagnostics.

- [ ] **Step 4: Run the complete supported test suite**

```powershell
python -m pytest -q
```

Expected: the complete suite passes with zero failures.

- [ ] **Step 5: Inspect the final repository state**

```powershell
git status --short
git log -5 --oneline
```

Expected: no unintended files, generated databases, workspace notes, or
uncommitted changes remain.
