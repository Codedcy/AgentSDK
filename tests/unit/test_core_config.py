from pathlib import Path

import pytest

from agent_sdk.config import AgentSDKConfig, CaptureLevel
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.ids import new_id


def test_core_contracts_are_stable() -> None:
    config = AgentSDKConfig(database_path=Path("state.db"))
    assert config.capture_level is CaptureLevel.PREVIEW
    assert new_id("run").startswith("run_")
    with pytest.raises(Exception):
        AgentSDKConfig(database_path=Path("x.db"), unknown=True)
    error = AgentSDKError(ErrorCode.INVALID_STATE, "bad state", retryable=False)
    assert error.to_dict()["code"] == "invalid_state"
