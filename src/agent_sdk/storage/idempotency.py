from __future__ import annotations

import json
import math
from collections.abc import Mapping
from hashlib import sha256
from types import MappingProxyType
from typing import Any, NamedTuple, cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_serializer, field_validator


class IdempotencyError(ValueError):
    """Base class for durable command idempotency failures."""


class IdempotencyValidationError(IdempotencyError):
    """The caller supplied an invalid idempotency request."""


class IdempotencyConflictError(IdempotencyError):
    """A key was reused for behaviorally different input."""


class IdempotencyCorruptionError(IdempotencyError):
    """A durable idempotency record is malformed."""


class IdempotencyReplayMissError(IdempotencyError):
    """An atomic replay assertion no longer has a durable record."""


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("value must be JSON-compatible")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json_object(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    frozen = _freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise ValueError("JSON value must be an object")
    text = json.dumps(
        _thaw_json(frozen),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return frozen, text


class IdempotencyRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    scope: str
    key: str
    request_fingerprint: str
    session_id: str
    result: Mapping[str, Any]

    @field_validator("scope", "key")
    @classmethod
    def _bounded_text(cls, value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("idempotency text must contain 1..256 characters")
        return value

    @field_validator("session_id")
    @classmethod
    def _session_id(cls, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("idempotency session id must be nonempty")
        return value

    @field_validator("request_fingerprint")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("request fingerprint must be lowercase SHA-256")
        return value

    @field_validator("result", mode="after")
    @classmethod
    def _result(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen, _ = _canonical_json_object(value)
        return frozen

    @field_serializer("result")
    def _serialize_result(self, value: Mapping[str, Any]) -> dict[str, Any]:
        thawed = _thaw_json(value)
        assert isinstance(thawed, dict)
        return thawed


class IdempotencyWrite(NamedTuple):
    scope: str
    key: str
    request_fingerprint: str
    session_id: str
    result: dict[str, Any]


class IdempotencyReplay(NamedTuple):
    scope: str
    key: str
    request_fingerprint: str


def fingerprint_command(command: str, arguments: Mapping[str, Any]) -> str:
    try:
        if not isinstance(command, str) or not command:
            raise ValueError("command must be nonempty")
        _, encoded = _canonical_json_object(
            {"command": command, "arguments": cast(dict[str, Any], arguments)}
        )
    except (TypeError, ValueError) as error:
        raise IdempotencyValidationError("invalid idempotency command") from error
    return sha256(encoded.encode("utf-8")).hexdigest()


def record_from_write(write: IdempotencyWrite) -> IdempotencyRecord:
    try:
        return IdempotencyRecord.model_validate(write._asdict())
    except (ValidationError, TypeError, ValueError) as error:
        raise IdempotencyValidationError("invalid idempotency request") from error


def validate_replay(replay: IdempotencyReplay) -> IdempotencyReplay:
    try:
        placeholder = IdempotencyRecord(
            scope=replay.scope,
            key=replay.key,
            request_fingerprint=replay.request_fingerprint,
            session_id="validation-only",
            result={},
        )
    except (ValidationError, TypeError, ValueError) as error:
        raise IdempotencyValidationError("invalid idempotency replay") from error
    return IdempotencyReplay(
        placeholder.scope,
        placeholder.key,
        placeholder.request_fingerprint,
    )


def record_from_stored_json(
    *,
    scope: object,
    key: object,
    request_fingerprint: object,
    session_id: object,
    result_json: object,
) -> IdempotencyRecord:
    try:
        if not isinstance(result_json, str):
            raise ValueError("stored result is not text")
        result = json.loads(result_json)
        if not isinstance(result, dict):
            raise ValueError("stored result is not an object")
        return IdempotencyRecord.model_validate(
            {
                "scope": scope,
                "key": key,
                "request_fingerprint": request_fingerprint,
                "session_id": session_id,
                "result": result,
            }
        )
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise IdempotencyCorruptionError("stored idempotency record is invalid") from error


def detached_record(record: IdempotencyRecord) -> IdempotencyRecord:
    return IdempotencyRecord.model_validate(record.model_dump(mode="json"))


def canonical_result_json(record: IdempotencyRecord) -> str:
    _, encoded = _canonical_json_object(record.result)
    return encoded

