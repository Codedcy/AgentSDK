from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.storage.idempotency import (
    IdempotencyConflictError,
    IdempotencyError,
    IdempotencyReplayMissError,
    IdempotencyValidationError,
    IdempotencyReplay,
    validate_replay,
)


def validate_idempotency_key(scope: str, key: str | None) -> None:
    if key is None:
        return
    public_error: AgentSDKError | None = None
    try:
        validate_replay(IdempotencyReplay(scope, key, "0" * 64))
    except IdempotencyError as error:
        public_error = _idempotency_public_error(error)
    if public_error is not None:
        key = None
        raise public_error from None


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
