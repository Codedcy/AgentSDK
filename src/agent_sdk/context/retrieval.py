from __future__ import annotations

from typing import Any, cast

from pydantic import ValidationError

from agent_sdk.context.models import ContextCapsule
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.storage.base import StateStore, StoredEvent


class ContextRetrieval:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def get_capsule(
        self,
        capsule_id: str,
        *,
        session_id: str,
    ) -> ContextCapsule:
        try:
            record = await self._store.get_snapshot("context_capsule", capsule_id)
        except Exception as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "context retrieval failed",
                retryable=False,
            ) from error
        if record is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "context capsule not found",
                retryable=False,
            )
        try:
            if set(record) != {"session_id", "capsule"}:
                raise ValueError("unexpected capsule record fields")
            owner = record["session_id"]
            capsule_data = record["capsule"]
            if not isinstance(owner, str) or not isinstance(capsule_data, dict):
                raise ValueError("invalid capsule record")
            if owner != session_id:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "context capsule not found",
                    retryable=False,
                )
            return ContextCapsule.model_validate(cast(dict[str, Any], capsule_data))
        except AgentSDKError:
            raise
        except (TypeError, ValueError, ValidationError) as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "stored context capsule is invalid",
                retryable=False,
            ) from error

    async def read_sources(
        self,
        capsule_id: str,
        *,
        session_id: str,
    ) -> tuple[StoredEvent, ...]:
        capsule = await self.get_capsule(capsule_id, session_id=session_id)
        try:
            events = await self._store.read_events(
                after_cursor=0,
                session_id=session_id,
            )
        except Exception as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "context retrieval failed",
                retryable=False,
            ) from error
        by_id = {stored.event.event_id: stored for stored in events}
        try:
            return tuple(by_id[event_id] for event_id in capsule.source_event_ids)
        except KeyError as error:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "context source not found",
                retryable=False,
            ) from error
