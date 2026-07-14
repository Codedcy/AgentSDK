from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_STATE = "invalid_state"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INTERNAL = "internal"


class AgentSDKError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable}


class SessionBusyError(AgentSDKError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CONFLICT,
            "session has active work",
            retryable=False,
        )
