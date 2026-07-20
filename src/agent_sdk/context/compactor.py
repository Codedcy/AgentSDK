from __future__ import annotations

import json
from collections.abc import Sequence, Set
from dataclasses import dataclass
from typing import Any

from agent_sdk.context.models import ContextCapsule, ContextItem
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.models.litellm_gateway import (
    LiteLLMGateway,
    ModelRequest,
    UsageReported,
)

_MAX_COMPACTION_PROMPT_BYTES = 256 * 1024
_MAX_COMPACTION_SOURCES = 128


@dataclass(frozen=True)
class CompactionResult:
    capsule: ContextCapsule | None
    usage: UsageReported


class ContextCompactor:
    def __init__(self, models: LiteLLMGateway, *, model: str) -> None:
        self._models = models
        self._model = model

    async def summarize(
        self,
        source: tuple[ContextItem, ...],
        protected: set[str],
    ) -> CompactionResult:
        try:
            retained = set(protected)
            summarized = tuple(
                item for item in source if item.event_id not in retained
            )
            return await self._complete(
                document={
                    "schema": "ContextCapsule",
                    "operation": "summarize",
                    "retained_event_ids": [
                        item.event_id
                        for item in source
                        if item.event_id in retained
                    ],
                    "sources": self._bounded_sources(summarized),
                },
                allowed_refs={item.event_id for item in source},
                required_refs={item.event_id for item in summarized},
                instruction=(
                    "Summarize only the supplied closed older sources into a "
                    "ContextCapsule. Do not summarize retained messages. Cite "
                    "every supplied source event id and no other id."
                ),
            )
        except AgentSDKError:
            return CompactionResult(
                capsule=None,
                usage=UsageReported(None, None, None),
            )

    async def rebase(
        self,
        capsules: tuple[ContextCapsule, ...],
        source: tuple[ContextItem, ...],
        protected: set[str],
        *,
        capsule_ids: tuple[str, ...] = (),
    ) -> CompactionResult:
        try:
            if capsule_ids and len(capsule_ids) != len(capsules):
                raise ValueError("capsule ids must correspond to capsules")
            retained_source = tuple(
                item for item in source if item.event_id in protected
            )
            prior_source_refs = {
                ref for capsule in capsules for ref in capsule.source_event_ids
            }
            prior_refs = set(capsule_ids) if capsule_ids else prior_source_refs
            capsule_documents: list[dict[str, Any]] = []
            for index, capsule in enumerate(capsules):
                value = capsule.model_dump(mode="json")
                if capsule_ids:
                    value["capsule_id"] = capsule_ids[index]
                capsule_documents.append(value)
            return await self._complete(
                document={
                    "schema": "ContextCapsule",
                    "operation": "rebase",
                    "capsule_ids": list(capsule_ids),
                    "capsules": capsule_documents,
                    "sources": self._bounded_sources(retained_source),
                },
                allowed_refs=(
                    {item.event_id for item in source}
                    | prior_source_refs
                    | set(capsule_ids)
                ),
                required_refs=prior_refs
                | {item.event_id for item in retained_source},
                instruction=(
                    "Rebase the validated prior capsules with only the supplied "
                    "active, recent, or protected sources. Cite every prior "
                    "capsule reference and retained source id, and cite no "
                    "unknown id."
                ),
            )
        except AgentSDKError:
            return CompactionResult(
                capsule=None,
                usage=UsageReported(None, None, None),
            )

    async def compact(
        self,
        source: Sequence[ContextItem],
        protected: Set[str],
    ) -> CompactionResult:
        return await self.summarize(tuple(source), set(protected))

    async def _complete(
        self,
        *,
        document: dict[str, Any],
        allowed_refs: set[str],
        required_refs: set[str],
        instruction: str,
    ) -> CompactionResult:
        try:
            completion = await self._models.complete_structured(
                ModelRequest(
                    model=self._model,
                    messages=self._messages(document, instruction),
                    purpose="context_compaction",
                ),
                ContextCapsule,
            )
            capsule = completion.parsed
            cited = set(capsule.source_event_ids)
            if not cited <= allowed_refs or not required_refs <= cited:
                return CompactionResult(
                    capsule=None,
                    usage=completion.usage,
                )
            return CompactionResult(
                capsule=capsule,
                usage=completion.usage,
            )
        except AgentSDKError:
            return CompactionResult(
                capsule=None,
                usage=UsageReported(None, None, None),
            )

    @staticmethod
    def _bounded_sources(
        source: tuple[ContextItem, ...],
    ) -> list[dict[str, Any]]:
        if len(source) > _MAX_COMPACTION_SOURCES:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "context compaction source count exceeds limit",
                retryable=False,
            )
        return [item.model_dump(mode="json") for item in source]

    @staticmethod
    def _messages(
        document: dict[str, Any],
        instruction: str,
    ) -> tuple[dict[str, object], ...]:
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
            {"role": "system", "content": instruction},
            {"role": "user", "content": text},
        )
