from pathlib import Path
from tomllib import loads

import agent_sdk


def test_public_version_matches_package_metadata() -> None:
    project = loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]

    assert agent_sdk.__version__ == "0.1.0"
    assert project["version"] == agent_sdk.__version__
    assert "__version__" in agent_sdk.__all__
