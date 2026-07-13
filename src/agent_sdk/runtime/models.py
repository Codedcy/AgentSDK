from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SessionSnapshot(BaseModel):
    session_id: str
    status: Literal["active"] = "active"
    workspaces: tuple[str, ...]
    version: int = 1


class RunSnapshot(BaseModel):
    run_id: str
    session_id: str
    agent_revision: str
    status: RunStatus
    user_input: str
    version: int = 1
