from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_sdk import ToolRetryPolicy
from agent_sdk.models.litellm_gateway import ToolCallCompleted
from agent_sdk.runtime.engine import _tool_request_fingerprint
from agent_sdk.runtime.execution import ToolCapabilityDescriptor
from agent_sdk.tools import ToolRegistry, ToolSpec


def _spec(**updates: object) -> ToolSpec:
    data: dict[str, object] = {
        "name": "bash",
        "description": "Run",
        "input_schema": {"type": "object"},
        "version": "1",
        "source": "application",
        "effects": (),
        "timeout_seconds": None,
    }
    data.update(updates)
    return ToolSpec.model_validate(data)


def test_default_retry_policy_preserves_pre_3d2_canonical_json_and_hash() -> None:
    spec = _spec()

    assert spec.retry_policy is ToolRetryPolicy.NEVER
    assert spec.model_dump(mode="json") == {
        "name": "bash",
        "description": "Run",
        "input_schema": {"type": "object"},
        "version": "1",
        "source": "application",
        "effects": [],
        "timeout_seconds": None,
    }
    capability = ToolCapabilityDescriptor.from_spec(spec)
    assert capability.capability_hash == (
        "2a6f67bbdf395f62fe0d6ecd1770dc6a3f3fe79e16efc8cfc61783578d78fb14"
    )
    assert "retry_policy" not in capability.model_dump(mode="json")["spec"]


@pytest.mark.parametrize("value", ["unknown", 1, True, None])
def test_retry_policy_is_strict(value: object) -> None:
    with pytest.raises(ValidationError):
        _spec(retry_policy=value)


@pytest.mark.parametrize(
    "policy",
    [ToolRetryPolicy.IDEMPOTENT, ToolRetryPolicy.SAFE_RETRY],
)
def test_certified_retry_policy_changes_capability_and_request_fingerprints(
    policy: ToolRetryPolicy,
) -> None:
    base = ToolCapabilityDescriptor.from_spec(_spec())
    certified = ToolCapabilityDescriptor.from_spec(_spec(retry_policy=policy))
    call = ToolCallCompleted(
        index=0,
        call_id="call_1",
        name="bash",
        arguments_json='{"cmd":"pwd"}',
    )

    assert certified.spec.model_dump(mode="json")["retry_policy"] == policy.value
    assert certified.capability_hash != base.capability_hash
    assert _tool_request_fingerprint(call, certified, {"cmd": "pwd"}) != (
        _tool_request_fingerprint(call, base, {"cmd": "pwd"})
    )


def test_retry_policy_is_not_exposed_to_the_model_tool_schema() -> None:
    async def handler(**_: object) -> None:
        return None

    registry = ToolRegistry()
    registry.register(_spec(retry_policy=ToolRetryPolicy.IDEMPOTENT), handler)

    schemas = registry.schemas()
    assert json.dumps(schemas, sort_keys=True) == json.dumps(
        (
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run",
                    "parameters": {"type": "object"},
                },
            },
        ),
        sort_keys=True,
    )
