# Bilingual README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a complete Simplified Chinese README and reciprocal language switching while keeping the English README canonical and both languages factually synchronized.

**Architecture:** Keep `README.md` as the English source of truth and add `README.zh-CN.md` as a manually maintained full translation. Repository tests compare selectors, executable code blocks, commands, key capability boundaries, and documentation links so language drift fails the focused documentation gate.

**Tech Stack:** Markdown, Python 3.12, pytest, Ruff, mypy, Git

## Global Constraints

- `README.md` remains the default English GitHub entry point.
- The Simplified Chinese file is named exactly `README.zh-CN.md`.
- The English selector is exactly `English | [简体中文](README.zh-CN.md)`.
- The Chinese selector is exactly `[English](README.md) | 简体中文`.
- `README.zh-CN.md` is a full translation with the same section order as `README.md`.
- PowerShell commands, Python examples, API symbols, paths, versions, model identifiers, error codes, L0-L4 labels, and documentation targets remain unchanged.
- Do not add badges, a translation generator, new languages, runtime changes, or translations under `docs/`.
- Preserve the v0.1 boundary: Python `>=3.12,<3.14`, source installation, single-instance/single-process recovery, and no exactly-once external-effect guarantee.

---

### Task 1: Add reciprocal language entry points

**Files:**
- Modify: `README.md:1-4`
- Create: `README.zh-CN.md`
- Test: `tests/docs/test_public_readme.py`

**Interfaces:**
- Consumes: Existing `README.md` title and repository-relative Markdown links.
- Produces: `ZH_README: Path` test constant and reciprocal selector lines used by Task 2.

- [ ] **Step 1: Write the failing language-selector test**

Add the Chinese path beside the existing constants and add the test below:

```python
ZH_README = ROOT / "README.zh-CN.md"


def test_readmes_offer_reciprocal_language_switching() -> None:
    english = README.read_text(encoding="utf-8")
    chinese = ZH_README.read_text(encoding="utf-8")

    assert english.splitlines()[2] == "English | [简体中文](README.zh-CN.md)"
    assert chinese.splitlines()[2] == "[English](README.md) | 简体中文"
    assert ZH_README.exists()
```

- [ ] **Step 2: Run the selector test and confirm RED**

Run:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTHONPATH='src'
C:\Users\10176\AppData\Local\Temp\agent-sdk-python312-gate\source-venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/docs/test_public_readme.py::test_readmes_offer_reciprocal_language_switching -q
```

Expected: FAIL because `README.zh-CN.md` does not exist.

- [ ] **Step 3: Add the selectors and Chinese entry file**

Insert this line immediately below the English `# Agent SDK` heading and its
blank line:

```markdown
English | [简体中文](README.zh-CN.md)
```

Create the Chinese entry with this exact initial content:

```markdown
# Agent SDK

[English](README.md) | 简体中文

Agent SDK `0.1.0` 是一个异步 Python 基础 SDK，用于构建可恢复、可观测的 Agent。
```

- [ ] **Step 4: Run the selector test and confirm GREEN**

Run the command from Step 2.

Expected: `1 passed`.

- [ ] **Step 5: Commit the language entry points**

```powershell
git add README.md README.zh-CN.md tests/docs/test_public_readme.py
git commit -m "docs: add chinese readme entry"
```

### Task 2: Complete and lock the Simplified Chinese translation

**Files:**
- Modify: `README.zh-CN.md`
- Modify: `tests/docs/test_public_readme.py`
- Test: `tests/docs/test_public_readme.py`

**Interfaces:**
- Consumes: `README`, `ZH_README`, `_readme()`, and the reciprocal selectors from Task 1.
- Produces: A full Chinese public landing page whose executable blocks and factual anchors match `README.md`.

- [ ] **Step 1: Write failing translation-completeness tests**

Add this helper and these tests:

```python
def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"```{re.escape(language)}\n(.*?)```", text, flags=re.DOTALL)


def test_chinese_readme_covers_the_public_v01_contract() -> None:
    chinese = ZH_README.read_text(encoding="utf-8")

    for text in (
        "## 为什么选择 Agent SDK",
        "## 从源码安装",
        "## 五分钟确定性 smoke 运行",
        "## 运行真实的 LiteLLM Agent",
        "## v0.1 能力矩阵",
        "## Tool 与权限决策",
        "## 生成式 Workflow 准入",
        "## 观测与恢复",
        "## v0.1 边界",
        "## 文档",
        "## 开发与验证",
        "Python 3.12 和 3.13",
        "单个进程中的单个 SDK 实例",
        "exactly-once",
        "应用确认",
        "跨 Run",
        "v0.1 发布检查点",
        "git clone https://github.com/Codedcy/AgentSDK.git",
        "--smoke --database .agent-sdk/state.db --workspace .",
    ):
        assert text in chinese

    for target in (
        "docs/guides/v01-quickstart.md",
        "docs/guides/v01-recovery.md",
        "docs/guides/v01-tracing-and-analysis.md",
        "docs/design/00-high-level-design.md",
        "docs/plans/releases/v0.1.md",
    ):
        assert f"]({target})" in chinese


def test_bilingual_readmes_share_executable_examples() -> None:
    english = README.read_text(encoding="utf-8")
    chinese = ZH_README.read_text(encoding="utf-8")

    assert _fenced_blocks(chinese, "powershell") == _fenced_blocks(
        english, "powershell"
    )
    assert _fenced_blocks(chinese, "python") == _fenced_blocks(english, "python")


def test_chinese_readme_python_examples_are_valid_modules() -> None:
    chinese = ZH_README.read_text(encoding="utf-8")
    blocks = _fenced_blocks(chinese, "python")

    assert blocks
    for index, block in enumerate(blocks, start=1):
        compile(block, f"README.zh-CN.md python block {index}", "exec")


def test_chinese_readme_avoids_unsupported_distribution_claims() -> None:
    chinese = ZH_README.read_text(encoding="utf-8").lower()

    assert "python -m pip install agent-sdk" not in chinese
    assert "pypi.org" not in chinese
    assert "shields.io" not in chinese
```

- [ ] **Step 2: Run the new tests and confirm RED**

Run:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTHONPATH='src'
C:\Users\10176\AppData\Local\Temp\agent-sdk-python312-gate\source-venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/docs/test_public_readme.py -q
```

Expected: FAIL on missing Chinese headings, boundaries, documentation links, and
executable blocks.

- [ ] **Step 3: Replace the Chinese entry with the complete translation**

Translate every prose paragraph, heading, table heading, table description, and
list item in `README.md` in the same order. Use these exact Chinese headings:

```markdown
## 为什么选择 Agent SDK
## 从源码安装
## 五分钟确定性 smoke 运行
## 运行真实的 LiteLLM Agent
## v0.1 能力矩阵
## Tool 与权限决策
## 生成式 Workflow 准入
## 观测与恢复
## v0.1 边界
## 文档
## 开发与验证
```

Use this terminology consistently in prose:

```text
Agent Loop -> Agent 循环
Tool -> 工具（Tool） on first use, then Tool
Workflow -> 工作流（Workflow） on first use, then Workflow
Child agent -> 子 Agent（Child Agent） on first use, then Child Agent
Trace -> 追踪（Trace） on first use, then Trace
Context -> 上下文（Context） on first use, then Context
Run and Session -> retain the public API terms
```

Copy all fenced `powershell` and `python` blocks from `README.md` without edits.
Retain `LiteLLM`, `MCP`, `SQLite`, `Skill`, `Prompt Manifest`, API symbols,
`application_resolution_aborted`, L0-L4, version numbers, paths, and model names.

The Chinese boundary text must include these unambiguous statements:

```markdown
- 支持 Python 3.12 和 3.13；Python 3.14 不在本版本支持范围内。
- 文档所述的恢复模型限定为单个进程中的单个 SDK 实例；v0.1 不包含多 worker 协同恢复。
- 外部副作用不提供 exactly-once 保证。未知尝试必须由应用显式作出协调决策。
- 生成的 Workflow 不会在生成后自动执行；它必须经过 compile、验证、应用确认和显式启动。
- 跨 Run 的多维 Trace 聚合分析、无用结果评分、高级调度、导出器和额外可靠性加固属于 v0.1 之后的工作。
```

End the development section with historical, not current, evidence:

```markdown
v0.1 发布检查点记录为 2,956 passed、6 个预期的平台跳过。发布台账记录了该可复现的历史环境和 v0.1 之后的剩余工作；请为当前代码树重新运行上述检查。
```

- [ ] **Step 4: Run the focused documentation suite and confirm GREEN**

Run the command from Step 2.

Expected: all tests in `tests/docs/test_public_readme.py` pass.

- [ ] **Step 5: Run all documentation, static, and type gates**

Run:

```powershell
$py='C:\Users\10176\AppData\Local\Temp\agent-sdk-python312-gate\source-venv\Scripts\python.exe'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTHONPATH='src'
& $py -m pytest -p pytest_asyncio.plugin tests/docs -q
& $py -m ruff check .
& $py -m mypy
git diff --check
```

Expected: documentation tests pass, Ruff reports `All checks passed!`, mypy
reports no issues in 107 source files, and `git diff --check` exits zero.

- [ ] **Step 6: Run the deterministic smoke reference in a verified temp path**

Run:

```powershell
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$target = [IO.Path]::GetFullPath(
    (Join-Path $tempBase ('agent-sdk-bilingual-' + [guid]::NewGuid().ToString('N')))
)
if (-not $target.StartsWith($tempBase)) { throw 'unsafe temp target' }
[IO.Directory]::CreateDirectory((Join-Path $target 'workspace')) | Out-Null
try {
    $env:PYTHONPATH='src'
    & $py examples/v01_reference.py --smoke `
        --database (Join-Path $target 'state.db') `
        --workspace (Join-Path $target 'workspace')
    if ($LASTEXITCODE -ne 0) { throw 'smoke failed' }
} finally {
    if ([IO.Directory]::Exists($target)) {
        [IO.Directory]::Delete($target, $true)
    }
}
```

Expected: one JSON line with `run_status`, `workflow_status`, and `child_status`
equal to `completed`; `context_levels` equal to `L0` through `L4`; and all
recovery/observability booleans true.

- [ ] **Step 7: Review and commit the complete translation**

Review:

```powershell
git diff -- README.md README.zh-CN.md tests/docs/test_public_readme.py
git status --short
```

Confirm the Chinese file is complete, its code blocks match English, both
selectors work, and no files outside the planned scope changed. Then commit:

```powershell
git add README.md README.zh-CN.md tests/docs/test_public_readme.py
git commit -m "docs: add complete chinese readme"
```
