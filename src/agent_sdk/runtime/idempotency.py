from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.storage.idempotency import (
    IdempotencyConflictError,
    IdempotencyError,
    IdempotencyReplayMissError,
    IdempotencyValidationError,
)


def _idempotency_public_error(error: IdempotencyError) -> AgentSDKError:
    if isinstance(error, IdempotencyReplayMissError):
        return AgentSDKError(
            ErrorCode.CONFLICT,
            "idempotency replay changed concurrently",
            retryable=True,
        )
    if isinstance(error, IdempotencyConflictError):
        return AgentSDKError(
            ErrorCode.CONFLICT,
            "idempotency key conflicts with another request",
            retryable=False,
        )
    if isinstance(error, IdempotencyValidationError):
        return AgentSDKError(
            ErrorCode.INVALID_STATE,
            "idempotency key is invalid",
            retryable=False,
        )
    return AgentSDKError(
        ErrorCode.INTERNAL,
        "stored command result is invalid",
        retryable=False,
    )
