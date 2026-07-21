from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, NoReturn

from agent_sdk._frozen import FrozenMapping
from agent_sdk.errors import AgentSDKError, ErrorCode


# Exact normalized names only. Do not use substring matching: ordinary parameters
# such as ``max_tokens`` and response token counts must remain valid.
_CREDENTIAL_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "apisecret",
        "apitoken",
        "applicationsecret",
        "authtoken",
        "awssecretaccesskey",
        "azureadtoken",
        "bearertoken",
        "clientsecret",
        "credentials",
        "password",
        "privatekey",
        "secretaccesskey",
        "serviceaccount",
    }
)
_CREDENTIAL_ERROR = "model params must not contain credential-bearing keys"
_LIMIT_ERROR = "model params exceed validation limits"
_SHAPE_ERROR = "model params must contain only built-in JSON-like values"
_MAX_DEPTH = 64
_MAX_ITEMS = 10_000


def freeze_model_params(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Freeze already validated model parameters into an SDK-owned container."""
    frozen = _freeze_validated_value(value)
    assert isinstance(frozen, Mapping)
    return frozen


def validate_model_params_for_durability(value: Any) -> None:
    """Reject raw provider credentials before model parameters become durable."""
    pending = [(value, 0)]
    seen_containers: set[int] = set()
    item_count = 0
    while pending:
        item, depth = pending.pop()
        item_type = type(item)
        if item_type is dict or item_type is FrozenMapping:
            if depth > _MAX_DEPTH:
                _reject(_LIMIT_ERROR)
            identity = id(item)
            if identity in seen_containers:
                _reject(_LIMIT_ERROR)
            seen_containers.add(identity)
            if item_type is dict:
                item_count += len(item)
                if item_count > _MAX_ITEMS:
                    _reject(_LIMIT_ERROR)
                items = item.items()
            else:
                values = FrozenMapping._builtin_values(item)
                if values is None:
                    _reject(_SHAPE_ERROR)
                item_count += dict.__len__(values)
                if item_count > _MAX_ITEMS:
                    _reject(_LIMIT_ERROR)
                items = dict.items(values)
            for key, nested in items:
                if type(key) is not str:
                    _reject(_SHAPE_ERROR)
                if _normalize_key(key) in _CREDENTIAL_KEYS:
                    _reject(_CREDENTIAL_ERROR)
                pending.append((nested, depth + 1))
        elif item_type is MappingProxyType:
            _reject(_SHAPE_ERROR)
        elif item_type is list or item_type is tuple:
            if depth > _MAX_DEPTH:
                _reject(_LIMIT_ERROR)
            identity = id(item)
            if identity in seen_containers:
                _reject(_LIMIT_ERROR)
            seen_containers.add(identity)
            item_count += len(item)
            if item_count > _MAX_ITEMS:
                _reject(_LIMIT_ERROR)
            pending.extend((nested, depth + 1) for nested in item)
        elif item is None or item_type in {bool, int, float, str}:
            continue
        else:
            _reject(_SHAPE_ERROR)


def _normalize_key(key: str) -> str:
    return str.casefold(key).replace("_", "").replace("-", "")


def _reject(message: str) -> NoReturn:
    raise AgentSDKError(ErrorCode.INVALID_STATE, message, retryable=False)


def _freeze_validated_value(value: Any) -> Any:
    value_type = type(value)
    if value_type is FrozenMapping:
        return value
    if value_type is dict:
        return FrozenMapping(
            {key: _freeze_validated_value(item) for key, item in value.items()}
        )
    if value_type is list or value_type is tuple:
        return tuple(_freeze_validated_value(item) for item in value)
    if value is None or value_type in {bool, int, float, str}:
        return value
    _reject(_SHAPE_ERROR)
