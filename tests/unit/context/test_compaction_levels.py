from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from agent_sdk.context.compactor import ContextCompactor
from agent_sdk.context.models import ContextCapsule, ContextItem
from agent_sdk.models.litellm_gateway import (
    ModelRequest,
    StructuredCompletion,
    UsageReported,
)


def _item(
    ref: str,
    content: str,
    *,
    cursor: int,
    role: str = "user",
) -> ContextItem:
    return ContextItem(
        event_id=ref,
        cursor=cursor,
        event_type="context.message.appended",
        role=role,
        content=content,
    )


def _capsule(*refs: str, objective: str = "ship") -> ContextCapsule:
    return ContextCapsule(
        objective=objective,
        constraints=("preserve evidence",),
        decisions=(),
        facts=(),
        next_actions=("verify",),
        artifact_refs=(),
        source_event_ids=refs,
    )


class _StructuredGateway:
    def __init__(self, responses: Sequence[ContextCapsule]) -> None:
        self._responses = iter(responses)
        self.requests: list[ModelRequest] = []

    async def complete_structured(
        self,
        request: ModelRequest,
        schema: type[ContextCapsule],
    ) -> StructuredCompletion[ContextCapsule]:
        assert schema is ContextCapsule
        self.requests.append(request)
        return StructuredCompletion(
            parsed=next(self._responses),
            usage=UsageReported(11, 4, 15),
        )


@pytest.mark.asyncio
async def test_l3_summarize_sends_only_closed_older_slice() -> None:
    sources = (
        _item("evt_old_user", "old question", cursor=1),
        _item("evt_old_answer", "old answer", cursor=2, role="assistant"),
        _item("evt_recent", "recent question", cursor=3),
        _item("evt_protected", "must remain exact", cursor=4),
    )
    gateway = _StructuredGateway(
        [_capsule("evt_old_user", "evt_old_answer")]
    )
    compactor = ContextCompactor(gateway, model="fake/compact")  # type: ignore[arg-type]

    result = await compactor.summarize(
        sources,
        {"evt_recent", "evt_protected"},
    )

    assert result.capsule == _capsule("evt_old_user", "evt_old_answer")
    assert result.usage == UsageReported(11, 4, 15)
    request = gateway.requests[0]
    assert request.purpose == "context_compaction"
    document = json.loads(request.messages[-1]["content"])
    assert [item["event_id"] for item in document["sources"]] == [
        "evt_old_user",
        "evt_old_answer",
    ]
    assert document["retained_event_ids"] == ["evt_recent", "evt_protected"]


@pytest.mark.asyncio
async def test_l3_rejects_citation_of_retained_message() -> None:
    sources = (
        _item("evt_old", "old question", cursor=1),
        _item("evt_recent", "recent question", cursor=2),
    )
    gateway = _StructuredGateway([_capsule("evt_old", "evt_recent")])
    compactor = ContextCompactor(gateway, model="fake/compact")  # type: ignore[arg-type]

    result = await compactor.summarize(sources, {"evt_recent"})

    assert result.capsule is None
    assert result.usage == UsageReported(11, 4, 15)
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_l4_rebase_supplies_prior_capsules_and_active_bounded_sources() -> None:
    prior = (
        _capsule("evt_prior_a", objective="prior A"),
        _capsule("evt_prior_b", objective="prior B"),
    )
    source = (
        _item("evt_old", "x" * 400_000, cursor=1),
        _item("evt_active", "active constraint", cursor=2),
        _item("evt_recent", "recent question", cursor=3),
    )
    gateway = _StructuredGateway(
        [
            _capsule(
                "evt_prior_a",
                "evt_prior_b",
                "evt_active",
                "evt_recent",
                objective="rebased",
            )
        ]
    )
    compactor = ContextCompactor(gateway, model="fake/compact")  # type: ignore[arg-type]

    result = await compactor.rebase(
        prior,
        source,
        {"evt_active", "evt_recent"},
    )

    assert result.capsule is not None
    assert {"evt_prior_a", "evt_prior_b"} <= set(
        result.capsule.source_event_ids
    )
    request = gateway.requests[0]
    assert request.purpose == "context_compaction"
    encoded = request.messages[-1]["content"].encode("utf-8")
    assert len(encoded) <= 256 * 1024
    document: dict[str, Any] = json.loads(request.messages[-1]["content"])
    assert [capsule["objective"] for capsule in document["capsules"]] == [
        "prior A",
        "prior B",
    ]
    assert [item["event_id"] for item in document["sources"]] == [
        "evt_active",
        "evt_recent",
    ]
