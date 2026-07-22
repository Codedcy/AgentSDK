import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
QUICKSTART = ROOT / "docs" / "guides" / "v01-quickstart.md"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


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
