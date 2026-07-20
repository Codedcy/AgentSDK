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
        resolved: list[StoredEvent] = []
        seen_events: set[str] = set()
        active_capsules: set[str] = set()

        async def resolve(ref: str) -> None:
            event = by_id.get(ref)
            if event is not None:
                if ref not in seen_events:
                    resolved.append(event)
                    seen_events.add(ref)
                return
            if ref in active_capsules:
                raise AgentSDKError(
                    ErrorCode.INTERNAL,
                    "stored context capsule cycle detected",
                    retryable=False,
                )
            active_capsules.add(ref)
            try:
                nested = await self.get_capsule(ref, session_id=session_id)
                for nested_ref in nested.source_event_ids:
                    await resolve(nested_ref)
            except AgentSDKError as error:
                if error.code is ErrorCode.NOT_FOUND:
                    raise AgentSDKError(
                        ErrorCode.NOT_FOUND,
                        "context source not found",
                        retryable=False,
                    ) from error
                raise
            finally:
                active_capsules.remove(ref)

        capsule = await self.get_capsule(capsule_id, session_id=session_id)
        for source_ref in capsule.source_event_ids:
            await resolve(source_ref)
        return tuple(resolved)

    async def list_capsule_records(
        self,
        *,
        session_id: str,
    ) -> tuple[tuple[str, ContextCapsule], ...]:
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
        capsule_ids: list[str] = []
        seen: set[str] = set()
        for stored in events:
            if stored.event.type != "context.compaction.completed":
                continue
            capsule_id = stored.event.payload.get("capsule_id")
            if (
                not isinstance(capsule_id, str)
                or not capsule_id
                or capsule_id in seen
            ):
                continue
            capsule_ids.append(capsule_id)
            seen.add(capsule_id)
        records: list[tuple[str, ContextCapsule]] = []
        for capsule_id in capsule_ids:
            records.append(
                (
                    capsule_id,
                    await self.get_capsule(
                        capsule_id,
                        session_id=session_id,
                    ),
                )
            )
        return tuple(records)
