from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agent_sdk import (
    AgentSpec,
    ProviderRecoveryAdapter,
    ProviderRecoveryDisposition,
    ProviderRecoveryRequest,
    ProviderRecoveryResult,
    TokenUsage,
)
from agent_sdk.models.litellm_gateway import LiteLLMGateway, ModelRequest
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.execution import ExecutionDescriptor, ExecutionPolicyDescriptor
from agent_sdk.runtime.provider_recovery import ProviderRecoveryRegistry
from agent_sdk.storage.memory import InMemoryStore


async def _completed(
    request: ProviderRecoveryRequest,
) -> ProviderRecoveryResult:
    del request
    return ProviderRecoveryResult(
        disposition=ProviderRecoveryDisposition.COMPLETED,
        text="recovered",
        usage=TokenUsage(),
    )


async def _make_live_run(
    registry: ProviderRecoveryRegistry,
) -> list[dict[str, object]]:
    store = InMemoryStore()
    commands = RuntimeCommands(store)
    session = await commands.create_session(workspaces=[])
    messages = ({"role": "user", "content": "hello"},)
    spec = AgentSpec(name="agent", model="provider/model")
    descriptor = ExecutionDescriptor.create(
        agent=spec,
        messages=messages,
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    )
    run = await commands.start_run(
        session.session_id,
        agent_revision="agent:1",
        user_input="hello",
        execution_descriptor=descriptor,
    )
    observed: list[dict[str, object]] = []

    async def provider(**_: object) -> AsyncIterator[dict[str, object]]:
        operations = await store.list_unresolved_external_operations(run.run_id)
        assert len(operations) == 1
        observed.append(dict(operations[0].recovery_metadata))

        async def chunks() -> AsyncIterator[dict[str, object]]:
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        return chunks()

    await RunEngine(
        store,
        LiteLLMGateway._for_test(provider),
        policy=PolicyEngine("allow"),
        provider_recovery=registry,
    ).execute(
        run.run_id,
        ModelRequest(model="provider/model", messages=messages),
    )
    return observed


@pytest.mark.asyncio
async def test_live_model_operation_stamps_exact_registered_certification() -> None:
    registry = ProviderRecoveryRegistry()
    registry.register(
        ProviderRecoveryAdapter(
            provider_identity="provider/model",
            adapter_id="application.adapter",
            version="2026-07-15",
            authoritative_status=True,
            same_operation_id_resend=True,
            query_status=_completed,
            resend=_completed,
        )
    )

    observed = await _make_live_run(registry)

    assert observed == [
        {
            "adapter_id": "application.adapter",
            "adapter_version": "2026-07-15",
            "authoritative_status": True,
            "same_operation_id_resend": True,
        }
    ]


@pytest.mark.asyncio
async def test_live_model_operation_without_adapter_stays_conservative() -> None:
    observed = await _make_live_run(ProviderRecoveryRegistry())

    assert observed == [
        {
            "authoritative_status": False,
            "same_operation_id_resend": False,
        }
    ]
