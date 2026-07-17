from pathlib import Path

import pytest

from agent_sdk.config import AgentSDKConfig, CaptureLevel
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.ids import new_id
from agent_sdk.permissions import PermissionRule


def test_core_contracts_are_stable() -> None:
    config = AgentSDKConfig(database_path=Path("state.db"))
    assert config.capture_level is CaptureLevel.PREVIEW
    assert new_id("run").startswith("run_")
    with pytest.raises(Exception):
        AgentSDKConfig(database_path=Path("x.db"), unknown=True)
    error = AgentSDKError(ErrorCode.INVALID_STATE, "bad state", retryable=False)
    assert error.to_dict()["code"] == "invalid_state"


def test_permission_rules_round_trip_through_config_json(tmp_path: Path) -> None:
    config = AgentSDKConfig(
        database_path=tmp_path / "state.db",
        permission_default="deny",
        permission_rules=(
            PermissionRule(
                outcome="allow",
                tool="bash",
                path_prefix=tmp_path / "workspace",
                command_prefix=("git", "status"),
            ),
        ),
    )

    restored = AgentSDKConfig.model_validate_json(config.model_dump_json())

    assert restored == config
    assert restored.permission_rules[0].command_prefix == ("git", "status")
