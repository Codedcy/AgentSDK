from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from agent_sdk import AgentSDK, AgentSDKError, AgentSpec, ErrorCode
from agent_sdk._frozen import FrozenMapping
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.execution import (
    DurableAgentSpec,
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
)
from agent_sdk.runtime.model_params import validate_model_params_for_durability
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


_SECRET = "SECRET-SENTINEL-7F6C9D2E"
_SAFE_ERROR = "model params must not contain credential-bearing keys"
_LIMIT_ERROR = "model params exceed validation limits"
_SHAPE_ERROR = "model params must contain only built-in JSON-like values"
_PROXY_SECRET = "PROXY-MAPPING-THREW-SECRET-1D55F8"


@pytest.mark.parametrize(
    "model_params",
    [
        {"api_key": _SECRET},
        {"metadata": [{"API-Key": _SECRET}]},
    ],
)
def test_agent_spec_rejects_direct_and_nested_api_keys_without_leaking(
    model_params: dict[str, object],
) -> None:
    with pytest.raises(AgentSDKError) as captured:
        AgentSpec(name="secret-test", model="fake/model", model_params=model_params)

    error = captured.value
    assert error.code is ErrorCode.INVALID_STATE
    assert error.message == _SAFE_ERROR
    assert _SECRET not in str(error)
    assert _SECRET not in repr(error)
    assert _SECRET not in str(error.to_dict())


@pytest.mark.parametrize(
    "credential_key",
    [
        "API_KEY",
        "api-secret",
        "api-token",
        "access_token",
        "AUTH-TOKEN",
        "bearer_token",
        "client-secret",
        "application_secret",
        "secret-access-key",
        "aws_secret_access_key",
        "AZURE-AD-TOKEN",
        "Authorization",
        "AUTH_ORIZATION",
        "author-ization",
        "credentials",
        "service-account",
        "private_key",
        "password",
    ],
)
def test_agent_spec_rejects_documented_credential_keys(
    credential_key: str,
) -> None:
    with pytest.raises(AgentSDKError) as captured:
        AgentSpec(
            name="credential-key-test",
            model="fake/model",
            model_params={"provider": [{credential_key: _SECRET}]},
        )

    assert captured.value.message == _SAFE_ERROR
    assert _SECRET not in repr(captured.value)


def test_agent_spec_preserves_noncredential_token_parameters() -> None:
    params = {
        "max_tokens": 512,
        "token_budget": 1024,
        "response_token_count": 21,
        "metadata": {"token_label": "ordinary-value"},
    }

    spec = AgentSpec(name="safe-token-test", model="fake/model", model_params=params)

    assert spec.model_dump(mode="json")["model_params"] == params


@pytest.mark.parametrize("dimension", ["depth", "items"])
def test_agent_spec_bounds_recursive_model_param_validation(dimension: str) -> None:
    if dimension == "depth":
        params: dict[str, object] = {}
        for _ in range(65):
            params = {"safe": params}
    else:
        params = {f"safe_{index}": index for index in range(10_001)}

    with pytest.raises(AgentSDKError) as captured:
        AgentSpec(name="bounded-test", model="fake/model", model_params=params)

    assert captured.value.message == _LIMIT_ERROR


def test_agent_spec_rejects_cyclic_model_params_at_the_bounded_validator() -> None:
    cycle: list[object] = []
    cycle.append(cycle)

    with pytest.raises(AgentSDKError) as captured:
        AgentSpec(
            name="cycle-test",
            model="fake/model",
            model_params={"cycle": cycle},
        )

    assert captured.value.message == _LIMIT_ERROR


class _TrapMapping(Mapping[str, object]):
    def __init__(self) -> None:
        self.executed = False

    def __getitem__(self, key: str) -> object:
        self.executed = True
        raise AssertionError(f"custom mapping executed for key {key}")

    def __iter__(self) -> Iterator[str]:
        self.executed = True
        raise AssertionError("custom mapping iterator executed")

    def __len__(self) -> int:
        self.executed = True
        raise AssertionError("custom mapping length executed")


class _ProxyTrapMapping(Mapping[str, object]):
    def __init__(self) -> None:
        self.executed: list[str] = []

    def __getitem__(self, key: str) -> object:
        self.executed.append("getitem")
        raise RuntimeError(f"{_PROXY_SECRET}:{key}")

    def __iter__(self) -> Iterator[str]:
        self.executed.append("iter")
        raise RuntimeError(_PROXY_SECRET)

    def __len__(self) -> int:
        self.executed.append("len")
        raise RuntimeError(_PROXY_SECRET)


def _proxy_trap() -> tuple[Mapping[str, object], _ProxyTrapMapping]:
    trap = _ProxyTrapMapping()
    return MappingProxyType(trap), trap


def _forged_frozen_trap() -> tuple[FrozenMapping, _ProxyTrapMapping]:
    trap = _ProxyTrapMapping()
    forged = object.__new__(FrozenMapping)
    object.__setattr__(forged, "_FrozenMapping__values", trap)
    return forged, trap


def _assert_sanitized_proxy_rejection(
    captured: pytest.ExceptionInfo[AgentSDKError],
    trap: _ProxyTrapMapping,
) -> None:
    assert captured.value.message == _SHAPE_ERROR
    assert _PROXY_SECRET not in str(captured.value)
    assert _PROXY_SECRET not in repr(captured.value)
    assert trap.executed == []


def test_agent_spec_rejects_custom_mapping_without_executing_it() -> None:
    custom = _TrapMapping()

    with pytest.raises(AgentSDKError) as captured:
        AgentSpec(name="custom-test", model="fake/model", model_params=custom)

    assert captured.value.message == _SHAPE_ERROR
    assert custom.executed is False


@pytest.mark.parametrize("boundary", ["validator", "agent", "durable"])
def test_untrusted_mapping_proxy_is_rejected_without_executing_custom_mapping(
    boundary: str,
) -> None:
    proxy, trap = _proxy_trap()

    with pytest.raises(AgentSDKError) as captured:
        if boundary == "validator":
            validate_model_params_for_durability(proxy)
        elif boundary == "agent":
            AgentSpec(name="proxy-test", model="fake/model", model_params=proxy)
        else:
            DurableAgentSpec.model_validate(
                {
                    "name": "proxy-durable-test",
                    "model": "fake/model",
                    "model_params": proxy,
                }
            )

    _assert_sanitized_proxy_rejection(captured, trap)


@pytest.mark.parametrize(
    "boundary",
    ["validator", "agent", "durable", "registry", "descriptor"],
)
def test_forged_frozen_mapping_is_rejected_before_backing_mapping_execution(
    boundary: str,
) -> None:
    forged, trap = _forged_frozen_trap()
    bypassed = AgentSpec.model_construct(
        name="forged-frozen-test",
        model="fake/model",
        model_params=forged,
    )

    with pytest.raises(AgentSDKError) as captured:
        if boundary == "validator":
            validate_model_params_for_durability(forged)
        elif boundary == "agent":
            AgentSpec(name="forged-test", model="fake/model", model_params=forged)
        elif boundary == "durable":
            DurableAgentSpec.model_validate(
                {
                    "name": "forged-durable-test",
                    "model": "fake/model",
                    "model_params": forged,
                }
            )
        elif boundary == "registry":
            AgentRegistry().define(bypassed)
        else:
            ExecutionDescriptor.create(
                agent=bypassed,
                messages=({"role": "user", "content": "hello"},),
                tools=(),
                policy=ExecutionPolicyDescriptor.create(permission_default="deny"),
            )

    _assert_sanitized_proxy_rejection(captured, trap)


def test_durable_agent_spec_rejects_secret_without_leaking() -> None:
    with pytest.raises(AgentSDKError) as captured:
        DurableAgentSpec.model_validate(
            {
                "name": "durable-secret-test",
                "model": "fake/model",
                "model_params": {"provider": {"api_key": _SECRET}},
            }
        )

    assert captured.value.message == _SAFE_ERROR
    assert _SECRET not in str(captured.value)
    assert _SECRET not in repr(captured.value)


@pytest.mark.parametrize("boundary", ["registry", "descriptor"])
def test_bypassed_custom_mapping_is_rejected_before_agent_serialization(
    boundary: str,
) -> None:
    custom = _TrapMapping()
    bypassed = AgentSpec.model_construct(
        name="bypassed-custom-test",
        model="fake/model",
        model_params=custom,
    )

    with pytest.raises(AgentSDKError) as captured:
        if boundary == "registry":
            AgentRegistry().define(bypassed)
        else:
            ExecutionDescriptor.create(
                agent=bypassed,
                messages=({"role": "user", "content": "hello"},),
                tools=(),
                policy=ExecutionPolicyDescriptor.create(permission_default="deny"),
            )

    assert captured.value.message == _SHAPE_ERROR
    assert custom.executed is False


def test_safe_litellm_params_remain_durable_and_recoverable() -> None:
    params = {
        "temperature": 0.25,
        "max_tokens": 256,
        "response_format": {"type": "json_object"},
        "metadata": [{"token_label": "ordinary-value"}],
    }
    descriptor = ExecutionDescriptor.create(
        agent=AgentSpec(
            name="safe-roundtrip",
            model="fake/model",
            model_params=params,
        ),
        messages=({"role": "user", "content": "hello"},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="deny"),
    )

    recovered = ExecutionDescriptor.model_validate(descriptor.model_dump(mode="json"))

    assert recovered.agent.model_dump(mode="json")["model_params"] == params
    validate_model_params_for_durability(recovered.agent.model_params)


class _ZeroAccessStore:
    def __init__(self) -> None:
        self._delegate = InMemoryStore()
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    def __getattr__(self, name: str) -> Any:
        if self._armed:
            raise AssertionError(f"Store accessed after secret rejection boundary: {name}")
        return getattr(self._delegate, name)


@pytest.mark.asyncio
async def test_public_start_rejects_bypassed_secret_before_store_access() -> None:
    store = _ZeroAccessStore()

    async def must_not_call_provider(**_: Any) -> object:
        raise AssertionError("provider called for rejected model params")

    sdk = AgentSDK.for_test(
        store=store,  # type: ignore[arg-type]
        acompletion=must_not_call_provider,
        enable_builtin_tools=False,
    )
    assert sdk._startup_scan_task is not None
    await sdk._startup_scan_task
    store.arm()
    bypassed = AgentSpec.model_construct(
        name="secret-test",
        model="fake/model",
        model_params={"api_key": _SECRET},
    )

    try:
        with pytest.raises(AgentSDKError) as captured:
            await sdk.runs.start("missing-session", bypassed, "do not persist this")
        assert captured.value.message == _SAFE_ERROR
        assert _SECRET not in str(captured.value)
        assert _SECRET not in repr(captured.value)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_public_start_rejects_untrusted_mapping_proxy_without_side_effects() -> None:
    store = _ZeroAccessStore()

    async def must_not_call_provider(**_: Any) -> object:
        raise AssertionError("provider called for rejected model params")

    sdk = AgentSDK.for_test(
        store=store,  # type: ignore[arg-type]
        acompletion=must_not_call_provider,
        enable_builtin_tools=False,
    )
    assert sdk._startup_scan_task is not None
    await sdk._startup_scan_task
    store.arm()
    proxy, trap = _proxy_trap()
    bypassed = AgentSpec.model_construct(
        name="proxy-public-test",
        model="fake/model",
        model_params=proxy,
    )

    try:
        with pytest.raises(AgentSDKError) as captured:
            await sdk.runs.start("missing-session", bypassed, "do not persist this")
        _assert_sanitized_proxy_rejection(captured, trap)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_public_start_rejects_forged_frozen_mapping_before_store_access() -> None:
    store = _ZeroAccessStore()

    async def must_not_call_provider(**_: Any) -> object:
        raise AssertionError("provider called for rejected model params")

    sdk = AgentSDK.for_test(
        store=store,  # type: ignore[arg-type]
        acompletion=must_not_call_provider,
        enable_builtin_tools=False,
    )
    assert sdk._startup_scan_task is not None
    await sdk._startup_scan_task
    store.arm()
    forged, trap = _forged_frozen_trap()
    bypassed = AgentSpec.model_construct(
        name="forged-public-test",
        model="fake/model",
        model_params=forged,
    )

    try:
        with pytest.raises(AgentSDKError) as captured:
            await sdk.runs.start("missing-session", bypassed, "do not persist this")
        _assert_sanitized_proxy_rejection(captured, trap)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_rejected_secret_never_reaches_sqlite_or_durable_records(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rejected-secret.sqlite3"

    async def must_not_call_provider(**_: Any) -> object:
        raise AssertionError("provider called for rejected model params")

    sdk = AgentSDK.for_test(
        database_path=database,
        acompletion=must_not_call_provider,
        enable_builtin_tools=False,
    )
    session = await sdk.sessions.create(workspaces=[])
    before = await sdk.queries.query_events(after_cursor=0, limit=100)
    bypassed = AgentSpec.model_construct(
        name="sqlite-secret-test",
        model="fake/model",
        model_params={"provider": [{"API-Key": _SECRET}]},
    )

    try:
        with pytest.raises(AgentSDKError) as captured:
            await sdk.runs.start(
                session.session_id,
                bypassed,
                "do not persist this",
                idempotency_key="secret-attempt",
            )
        assert captured.value.message == _SAFE_ERROR
        after = await sdk.queries.query_events(after_cursor=0, limit=100)
        assert after.as_of_cursor == before.as_of_cursor
        assert after.events == before.events
    finally:
        await sdk.close()

    store = await SQLiteStore.open(database)
    try:
        assert (
            await store.get_idempotency(
                f"session/{session.session_id}/run.start",
                "secret-attempt",
            )
            is None
        )
        events = await store.read_events(after_cursor=0)
        serialized_events = json.dumps(
            [item.event.model_dump(mode="json") for item in events],
            sort_keys=True,
        )
        assert _SECRET not in serialized_events
    finally:
        await store.close()

    for sqlite_file in tmp_path.glob(f"{database.name}*"):
        assert _SECRET.encode() not in sqlite_file.read_bytes()
