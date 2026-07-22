import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
ZH_README = ROOT / "README.zh-CN.md"
QUICKSTART = ROOT / "docs" / "guides" / "v01-quickstart.md"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"```{re.escape(language)}\n(.*?)```", text, flags=re.DOTALL)


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
