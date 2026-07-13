from __future__ import annotations

import json
from collections.abc import Sequence, Set
from dataclasses import dataclass

from agent_sdk.context.models import ContextCapsule, ContextItem
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.models.litellm_gateway import (
    LiteLLMGateway,
    ModelRequest,
    UsageReported,
)

_MAX_COMPACTION_PROMPT_BYTES = 256 * 1024


@dataclass(frozen=True)
class _CompactionResult:
    capsule: ContextCapsule | None
    usage: UsageReported


class ContextCompactor:
    def __init__(self, models: LiteLLMGateway, *, model: str) -> None:
        self._models = models
        self._model = model

    async def compact(
        self,
        source: Sequence[ContextItem],
        protected: Set[str],
    ) -> _CompactionResult:
        try:
            completion = await self._models.complete_structured(
                ModelRequest(
                    model=self._model,
                    messages=self._messages(source, protected),
                    purpose="compaction",
                ),
                ContextCapsule,
            )
            capsule = completion.parsed
            source_ids = {item.event_id for item in source}
            cited_ids = set(capsule.source_event_ids)
            if not cited_ids <= source_ids or not set(protected) <= cited_ids:
                return _CompactionResult(
                    capsule=None,
                    usage=completion.usage,
                )
            return _CompactionResult(capsule=capsule, usage=completion.usage)
        except AgentSDKError:
            return _CompactionResult(
                capsule=None,
                usage=UsageReported(None, None, None),
            )

    @staticmethod
    def _messages(
        source: Sequence[ContextItem],
        protected: Set[str],
    ) -> tuple[dict[str, object], ...]:
        document = {
            "schema": "ContextCapsule",
            "protected_event_ids": [
                item.event_id for item in source if item.event_id in protected
            ],
            "sources": [item.model_dump(mode="json") for item in source],
        }
        text = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(text.encode("utf-8")) > _MAX_COMPACTION_PROMPT_BYTES:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "context compaction input exceeds size limit",
                retryable=False,
            )
        return (
            {
                "role": "system",
                "content": (
                    "Create a ContextCapsule that cites only supplied event ids. "
                    "Include every protected source conveyed by the caller."
                ),
            },
            {"role": "user", "content": text},
        )
