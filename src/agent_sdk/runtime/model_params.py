from __future__ import annotations

from types import MappingProxyType
from typing import Any

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


def validate_model_params_for_durability(value: Any) -> None:
    """Reject raw provider credentials before model parameters become durable."""
    pending = [(value, 0)]
    seen_containers: set[int] = set()
    item_count = 0
    while pending:
        item, depth = pending.pop()
        item_type = type(item)
        if item_type is dict or item_type is MappingProxyType:
            if depth > _MAX_DEPTH:
                _reject(_LIMIT_ERROR)
            identity = id(item)
            if identity in seen_containers:
                _reject(_LIMIT_ERROR)
            seen_containers.add(identity)
            item_count += len(item)
            if item_count > _MAX_ITEMS:
                _reject(_LIMIT_ERROR)
            for key, nested in item.items():
                if type(key) is not str:
                    _reject(_SHAPE_ERROR)
                if _normalize_key(key) in _CREDENTIAL_KEYS:
                    _reject(_CREDENTIAL_ERROR)
                pending.append((nested, depth + 1))
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


def _reject(message: str) -> None:
    raise AgentSDKError(ErrorCode.INVALID_STATE, message, retryable=False)
