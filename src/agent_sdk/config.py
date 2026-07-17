from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agent_sdk.permissions.rules import PermissionRule


class CaptureLevel(StrEnum):
    METADATA = "metadata"
    PREVIEW = "preview"
    FULL = "full"


class AgentSDKConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    database_path: Path
    capture_level: CaptureLevel = CaptureLevel.PREVIEW
    permission_default: Literal["allow", "deny", "ask"] = "ask"
    permission_rules: tuple[PermissionRule, ...] = ()
