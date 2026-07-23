import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
ZH_README = ROOT / "README.zh-CN.md"
QUICKSTART = ROOT / "docs" / "guides" / "v01-quickstart.md"
QUICKSTART_DESIGN = (
    ROOT / "docs" / "superpowers" / "specs" / "2026-07-23-quickstart-general-agent-design.md"
)
QUICKSTART_PLAN = ROOT / "docs" / "superpowers" / "plans" / "2026-07-23-quickstart-general-agent.md"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"```{re.escape(language)}\n(.*?)```", text, flags=re.DOTALL)


def _without_fenced_blocks(text: str) -> str:
    return re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.DOTALL)


def _inline_code_spans(text: str) -> list[str]:
    prose = _without_fenced_blocks(text)
    return re.findall(r"(?<!`)`([^`\n]+)`(?!`)", prose)


def _section_bullets(text: str, heading: str) -> list[str]:
    match = re.search(
        rf"^{re.escape(heading)}\n(.*?)(?=^## |\Z)",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match is not None

    bullets: list[str] = []
    for line in match.group(1).splitlines():
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif bullets and line.startswith("  "):
            bullets[-1] = f"{bullets[-1]} {line.strip()}"
    return bullets


def test_readmes_offer_reciprocal_language_switching() -> None:
    english = README.read_text(encoding="utf-8")
    chinese = ZH_README.read_text(encoding="utf-8")

    assert english.splitlines()[2] == "English | [简体中文](README.zh-CN.md)"
    assert chinese.splitlines()[2] == "[English](README.md) | 简体中文"
    assert ZH_README.exists()


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

    assert _fenced_blocks(chinese, "powershell") == _fenced_blocks(english, "powershell")
    assert _fenced_blocks(chinese, "python") == _fenced_blocks(english, "python")


def test_bilingual_readmes_recommend_the_general_agent_quickstart() -> None:
    english = README.read_text(encoding="utf-8")
    chinese = ZH_README.read_text(encoding="utf-8")
    command = "python examples/quickstart_agent.py --model openai/gpt-4o-mini"

    assert "## Build a General Agent" in english
    assert "examples/quickstart_agent.py" in chinese
    assert command in english
    assert command in chinese
    assert "--session-id" in english
    assert "--session-id" in chinese


def test_quickstart_docs_disclose_bash_boundary_and_output_timing() -> None:
    english = " ".join(README.read_text(encoding="utf-8").split())
    chinese = " ".join(ZH_README.read_text(encoding="utf-8").split())
    design = " ".join(QUICKSTART_DESIGN.read_text(encoding="utf-8").split())

    for text in (english, design):
        assert "not sandboxed" in text
        assert "absolute paths outside the workspace" in text
        assert "inherits the application environment" in text
        assert "stdout and stderr" in text
        assert "sent to the model" in text
        assert "stored in Session history" in text
        assert "before approval" in text
    for text in (chinese,):
        assert "不在沙箱中" in text
        assert "绝对路径访问 workspace 之外" in text
        assert "继承应用环境" in text
        assert "stdout 和 stderr" in text
        assert "发送给模型" in text
        assert "存储在 Session 历史中" in text
        assert "审批前" in text

    assert "Session ID is printed once at startup" in english
    assert (
        "after each turn it prints the final answer, Run ID, token usage, and invoked Tools"
    ) in english
    assert "Session ID 仅在启动时打印一次" in chinese
    assert "每轮结束后会显示最终回复、Run ID、token 用量和调用过的 Tool" in chinese
    assert "reuse the same database and the exact original resolved workspace" in english
    assert "复用同一个数据库和原始 workspace 的解析后精确路径" in chinese


def test_quickstart_implementation_plan_readme_copy_matches_final_boundary() -> None:
    plan = " ".join(QUICKSTART_PLAN.read_text(encoding="utf-8").split())

    assert "and `bash` Tools inside the selected workspace" not in plan
    assert (
        "It prints the Session ID, final answer, token usage, and invoked Tools after each turn."
        not in plan
    )
    assert "Session ID is printed once at startup" in plan
    assert "approved process is not sandboxed" in plan
    assert "stdout and stderr are sent to the model and stored in Session history" in plan
    assert "reuse the same database and the exact original resolved workspace" in plan


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


def test_chinese_readme_preserves_complete_v01_boundary_bullets() -> None:
    chinese = ZH_README.read_text(encoding="utf-8")
    bullets = _section_bullets(chinese, "## v0.1 边界")

    for expected in (
        "支持 Python 3.12 和 3.13；Python 3.14 不在本版本支持范围内。",
        "文档所述的恢复模型限定为单个进程中的单个 SDK 实例；v0.1 不包含多 worker 协同恢复。",
        "外部副作用不提供 exactly-once 保证。未知尝试必须由应用显式作出协调决策。",
        "生成的 Workflow 不会在生成后自动执行；它必须经过 compile、验证、应用确认和显式启动。",
        "跨 Run 的多维 Trace 聚合分析、无用结果评分、高级调度、导出器和额外可靠性加固属于 v0.1 之后的工作。",
    ):
        assert expected in bullets


def test_bilingual_readmes_preserve_non_fenced_technical_content() -> None:
    english = _without_fenced_blocks(README.read_text(encoding="utf-8"))
    chinese = _without_fenced_blocks(ZH_README.read_text(encoding="utf-8"))
    normalized_english = " ".join(english.split())
    normalized_chinese = " ".join(chinese.split())

    assert "```" not in english
    assert "```" not in chinese
    assert _inline_code_spans(chinese) == _inline_code_spans(english)

    for token in (
        "0.1.0",
        "L0-L4",
        "LiteLLM",
        "MCP",
        "SQLite",
        "Prompt Manifest",
        "application_resolution_aborted",
        "exactly-once",
    ):
        assert chinese.count(token) == english.count(token)

    for source, translation in (
        (
            "Built-in `read` and `write` enforce Session workspace roots and "
            "the configured policy. For `bash`, `path_prefix` constrains the "
            "subprocess working directory used for policy matching; it does "
            "not sandbox an approved process, which can use absolute paths "
            "and inherits the application environment.",
            "内置 `read` 和 `write` 会强制执行 Session 的 workspace 根目录约束和"
            "已配置策略。对于 `bash`，`path_prefix` 约束用于策略匹配的子进程工作"
            "目录；它不会隔离获批进程，进程仍可使用绝对路径并会继承应用环境。",
        ),
        (
            "Deleting a Session removes SDK-owned persisted history, events, "
            "and snapshots; it does not delete application-owned workspace files.",
            "删除 Session 会移除该 Session 中由 SDK 持久化的历史、事件和快照；"
            "不会删除任何由应用拥有的 workspace 文件。",
        ),
    ):
        assert source in normalized_english
        assert translation in normalized_chinese


def test_readme_uses_source_install_and_deterministic_smoke() -> None:
    readme = _readme()

    assert "git clone https://github.com/Codedcy/AgentSDK.git" in readme
    assert "python -m pip install ." in readme
    assert "python -m pip install agent-sdk" not in readme
    assert "--smoke --database .agent-sdk/state.db --workspace ." in readme


def test_readme_states_release_and_recovery_boundaries() -> None:
    readme = _readme()

    for text in (
        "0.1.0",
        "Python 3.12 and 3.13",
        "one SDK instance in one process",
        "exactly-once",
        "compile",
        "application confirmation",
        "cross-run",
    ):
        assert text in readme


def test_readme_links_public_v01_documentation() -> None:
    readme = _readme()

    for target in (
        "docs/guides/v01-quickstart.md",
        "docs/guides/v01-recovery.md",
        "docs/guides/v01-tracing-and-analysis.md",
        "docs/design/00-high-level-design.md",
        "docs/plans/releases/v0.1.md",
    ):
        assert f"]({target})" in readme


def test_readme_does_not_claim_unsupported_distribution_or_badges() -> None:
    readme = _readme().lower()

    assert "published on pypi" not in readme
    assert "pypi.org" not in readme
    assert "shields.io" not in readme


def test_public_getting_started_docs_use_source_install() -> None:
    for document in (README, QUICKSTART):
        text = document.read_text(encoding="utf-8")
        assert "git clone https://github.com/Codedcy/AgentSDK.git" in text
        assert "python -m pip install ." in text
        assert "python -m pip install agent-sdk" not in text


def test_readme_python_examples_are_valid_modules() -> None:
    readme = _readme()
    blocks = re.findall(r"```python\n(.*?)```", readme, flags=re.DOTALL)

    assert blocks
    for index, block in enumerate(blocks, start=1):
        compile(block, f"README.md python block {index}", "exec")


def test_readme_labels_the_full_suite_count_as_historical() -> None:
    readme = _readme()

    assert "current supported v0.1 full-suite evidence" not in readme
    assert "v0.1 release checkpoint" in readme
