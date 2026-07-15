from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from pydantic import ValidationError

from agent_sdk import (
    AgentSDK,
    AgentSDKError,
    ErrorCode,
    ProviderRecoveryAdapter,
    ProviderRecoveryDisposition,
    ProviderRecoveryRequest,
    ProviderRecoveryResult,
    TokenUsage,
)
from agent_sdk.models.litellm_gateway import ModelRequest, ToolCallCompleted
from agent_sdk.runtime.provider_recovery import ProviderRecoveryRegistry
from agent_sdk.storage.memory import InMemoryStore


async def _completed(
    request: ProviderRecoveryRequest,
) -> ProviderRecoveryResult:
    del request
    return ProviderRecoveryResult(
        disposition=ProviderRecoveryDisposition.COMPLETED,
        finish_reason="stop",
        text="done",
        usage=TokenUsage(total_tokens=1),
    )


async def _failed(
    request: ProviderRecoveryRequest,
) -> ProviderRecoveryResult:
    del request
    return ProviderRecoveryResult(
        disposition=ProviderRecoveryDisposition.FAILED,
        error_code=ErrorCode.INTERNAL,
        retryable=False,
    )


def _adapter(
    provider_identity: str = "provider/model",
    *,
    adapter_id: str = "application.provider-recovery",
    version: str = "1",
    authoritative_status: bool = True,
    same_operation_id_resend: bool = False,
) -> ProviderRecoveryAdapter:
    return ProviderRecoveryAdapter(
        provider_identity=provider_identity,
        adapter_id=adapter_id,
        version=version,
        authoritative_status=authoritative_status,
        same_operation_id_resend=same_operation_id_resend,
        query_status=_completed if authoritative_status else None,
        resend=_completed if same_operation_id_resend else None,
    )


def _request() -> ProviderRecoveryRequest:
    return ProviderRecoveryRequest(
        session_id="session_1",
        run_id="run_1",
        turn=2,
        operation_id="op_model_1",
        provider_identity="provider/model",
        request_fingerprint="f" * 64,
        model_request=ModelRequest(
            model="provider/model",
            messages=({"role": "user", "content": "secret"},),
            tools=({"type": "function", "function": {"name": "lookup"}},),
            params={"api_key": "secret"},
            purpose="run",
        ),
    )


def test_provider_recovery_models_are_strict_frozen_and_extra_forbid() -> None:
    adapter = _adapter()
    with pytest.raises(ValidationError):
        ProviderRecoveryAdapter.model_validate(
            {**adapter.model_dump(), "authoritative_status": 1}
        )
    with pytest.raises(ValidationError):
        ProviderRecoveryResult.model_validate(
            {
                "disposition": ProviderRecoveryDisposition.UNKNOWN,
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        ProviderRecoveryRequest.model_validate(
            {**_request().model_dump(), "turn": True}
        )
    with pytest.raises(ValidationError):
        adapter.version = "2"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"provider_identity": " "}, "nonempty"),
        ({"adapter_id": "x" * 257}, "bounded"),
        ({"authoritative_status": False}, "callable"),
        (
            {
                "same_operation_id_resend": True,
                "resend": None,
            },
            "callable",
        ),
    ],
)
def test_provider_recovery_adapter_rejects_invalid_certification(
    updates: dict[str, Any],
    message: str,
) -> None:
    values = {
        "provider_identity": "provider/model",
        "adapter_id": "application.provider-recovery",
        "version": "1",
        "authoritative_status": True,
        "same_operation_id_resend": False,
        "query_status": _completed,
        "resend": None,
    }
    values.update(updates)
    with pytest.raises(ValidationError, match=message):
        ProviderRecoveryAdapter.model_validate(values)


def test_provider_recovery_request_detaches_reconstructed_model_request() -> None:
    messages = [{"role": "user", "content": ["original"]}]
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    params = {"nested": {"value": "original"}}
    request = ProviderRecoveryRequest(
        session_id="session_1",
        run_id="run_1",
        turn=0,
        operation_id="op_model_1",
        provider_identity="provider/model",
        request_fingerprint="f" * 64,
        model_request=ModelRequest(
            model="provider/model",
            messages=tuple(messages),
            tools=tuple(tools),
            params=params,
        ),
    )

    messages[0]["content"] = ["changed"]
    tools[0]["function"]["name"] = "changed"
    params["nested"]["value"] = "changed"

    assert request.model_request.messages[0]["content"] == ["original"]
    assert request.model_request.tools[0]["function"]["name"] == "lookup"
    assert request.model_request.params["nested"]["value"] == "original"
    with pytest.raises((FrozenInstanceError, TypeError)):
        request.model_request.model = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "result",
    [
        ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="stop",
            text="done",
            usage=TokenUsage(total_tokens=1),
        ),
        ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.COMPLETED,
            finish_reason="tool_calls",
            text="",
            tool_call=ToolCallCompleted(
                index=0,
                call_id="call_1",
                name="lookup",
                arguments_json='{"value":1}',
            ),
            usage=TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        ),
        ProviderRecoveryResult(
            disposition=ProviderRecoveryDisposition.FAILED,
            error_code=ErrorCode.INTERNAL,
            retryable=True,
        ),
        ProviderRecoveryResult(disposition=ProviderRecoveryDisposition.NOT_EXECUTED),
        ProviderRecoveryResult(disposition=ProviderRecoveryDisposition.PENDING),
        ProviderRecoveryResult(disposition=ProviderRecoveryDisposition.UNKNOWN),
    ],
)
def test_provider_recovery_result_accepts_only_legal_disposition_fields(
    result: ProviderRecoveryResult,
) -> None:
    assert ProviderRecoveryResult.model_validate(result) is result


@pytest.mark.parametrize(
    "values",
    [
        {
            "disposition": ProviderRecoveryDisposition.COMPLETED,
            "text": "missing usage",
        },
        {
            "disposition": ProviderRecoveryDisposition.COMPLETED,
            "text": "x" * (64 * 1024 + 1),
            "usage": TokenUsage(),
        },
        {
            "disposition": ProviderRecoveryDisposition.COMPLETED,
            "text": "done",
            "usage": TokenUsage(total_tokens=-1),
        },
        {
            "disposition": ProviderRecoveryDisposition.COMPLETED,
            "text": "done",
            "tool_call": ToolCallCompleted(1, "call_1", "lookup", "{}"),
            "usage": TokenUsage(),
        },
        {
            "disposition": ProviderRecoveryDisposition.COMPLETED,
            "text": "done",
            "tool_call": ToolCallCompleted(0, "call_1", "lookup", "not-json"),
            "usage": TokenUsage(),
        },
        {
            "disposition": ProviderRecoveryDisposition.FAILED,
            "error_code": ErrorCode.INTERNAL,
        },
        {
            "disposition": ProviderRecoveryDisposition.FAILED,
            "error_code": ErrorCode.INTERNAL,
            "retryable": False,
            "text": "forbidden",
        },
        {
            "disposition": ProviderRecoveryDisposition.UNKNOWN,
            "retryable": False,
        },
    ],
)
def test_provider_recovery_result_rejects_illegal_or_unbounded_outcomes(
    values: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ProviderRecoveryResult.model_validate(values)


def test_provider_recovery_registry_is_deterministic_and_identity_safe() -> None:
    registry = ProviderRecoveryRegistry()
    second = registry.register(_adapter("provider/z"))
    first = registry.register(_adapter("provider/a"))

    assert registry.list() == (first, second)
    assert registry.get("provider/a") is first
    with pytest.raises(AgentSDKError) as duplicate:
        registry.register(_adapter("provider/a", version="2"))
    assert duplicate.value.code is ErrorCode.CONFLICT
    with pytest.raises(AgentSDKError) as missing:
        registry.get("provider/missing")
    assert missing.value.code is ErrorCode.NOT_FOUND

    impostor = _adapter("provider/a")
    assert registry.unregister("provider/a", expected=impostor) is False
    assert registry.unregister("provider/a", expected=first) is True
    assert registry.unregister("provider/a", expected=first) is False


@pytest.mark.asyncio
async def test_recovery_api_exposes_registry_and_starts_without_adapters() -> None:
    async def unused_acompletion(**kwargs: Any) -> Any:
        raise AssertionError(f"unexpected LiteLLM call: {sorted(kwargs)}")

    sdk = AgentSDK.for_test(
        store=InMemoryStore(),
        acompletion=unused_acompletion,
    )
    try:
        assert sdk.recovery.list_adapters() == ()
        registered = sdk.recovery.register_adapter(_adapter())
        assert registered is not _adapter()
        assert sdk.recovery.list_adapters() == (registered,)
        assert sdk.recovery.get_adapter("provider/model") is registered
        assert (
            sdk.recovery.unregister_adapter(
                "provider/model",
                expected=_adapter(),
            )
            is False
        )
        assert (
            sdk.recovery.unregister_adapter(
                "provider/model",
                expected=registered,
            )
            is True
        )
        assert sdk.recovery.list_adapters() == ()
    finally:
        await sdk.close()
