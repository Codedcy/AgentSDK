from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any, Literal, cast

from pydantic import ValidationError

from agent_sdk.context.budget import TokenCounter, default_token_counter
from agent_sdk.context.compactor import ContextCompactor
from agent_sdk.context.models import (
    CompactionLevel,
    CompactionPolicy,
    ContextBudget,
    ContextCapsule,
    ContextItem,
    ContextView,
)
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.ids import new_id
from agent_sdk.models.litellm_gateway import LiteLLMGateway, UsageReported
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    SnapshotWrite,
    StateStore,
    StoredEvent,
)

_Role = Literal["system", "user", "assistant", "tool"]
_APPLICATION_ROLES = frozenset({"system", "user", "assistant", "tool"})


class ContextPlanner:
    def __init__(
        self,
        store: StateStore,
        models: LiteLLMGateway,
        *,
        model: str,
        model_window: int,
        output_reserve: int = 0,
        tool_schema_tokens: int = 0,
        safety_reserve: int = 0,
        policy: CompactionPolicy | None = None,
        _token_counter: TokenCounter = default_token_counter,
    ) -> None:
        self._store = store
        self._model = model
        self._model_window = model_window
        self._output_reserve = output_reserve
        self._tool_schema_tokens = tool_schema_tokens
        self._safety_reserve = safety_reserve
        self._policy = policy or CompactionPolicy()
        self._token_counter = _token_counter
        self._compactor = ContextCompactor(models, model=model)

    async def build(
        self,
        session_id: str,
        *,
        force_level: CompactionLevel | str | None = None,
        protected_event_ids: Iterable[str] = (),
    ) -> ContextView:
        session = await self._store.get_snapshot("session", session_id)
        if session is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "session not found",
                retryable=False,
            )
        if session.get("session_id") != session_id:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "stored session is invalid",
                retryable=False,
            )
        stored_events = await self._store.read_events(
            after_cursor=0,
            session_id=session_id,
        )
        source = self._project(stored_events)
        protected = set(protected_event_ids)
        source_ids = {item.event_id for item in source}
        if not protected <= source_ids:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "protected context source not found",
                retryable=False,
            )
        latest_user = next(
            (item for item in reversed(source) if item.role == "user"),
            None,
        )
        if latest_user is not None:
            protected.add(latest_user.event_id)

        budget = self._budget(source)
        if budget.available_input_tokens <= 0:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "context budget has no input capacity",
                retryable=False,
            )
        recommended = self._policy.recommend(budget.watermark_ratio)
        requested = self._forced_level(force_level)
        if requested in (CompactionLevel.L1, CompactionLevel.L2):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "compaction level is not implemented",
                retryable=False,
            )
        if requested in (CompactionLevel.L3, CompactionLevel.L4) and not source:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "context sources are empty",
                retryable=False,
            )

        if requested in (CompactionLevel.L3, CompactionLevel.L4):
            result = await self._compactor.compact(source, protected)
            if result.capsule is not None:
                return await self._persist_compacted(
                    session_id=session_id,
                    source=source,
                    protected=protected,
                    capsule=result.capsule,
                    usage=result.usage,
                    budget=budget,
                    recommended=recommended,
                    applied=requested,
                )
            return await self._persist_fallback(
                session_id=session_id,
                source=source,
                usage=result.usage,
                budget=budget,
                recommended=recommended,
                requested=requested,
            )
        return await self._persist_l0(
            session_id=session_id,
            source=source,
            budget=budget,
            recommended=recommended,
        )

    def _budget(self, source: tuple[ContextItem, ...]) -> ContextBudget:
        messages: list[dict[str, Any]] = [
            {"role": item.role, "content": item.content} for item in source
        ]
        try:
            baseline = ContextBudget.calculate(
                model_window=self._model_window,
                output_reserve=self._output_reserve,
                tool_schema_tokens=self._tool_schema_tokens,
                safety_reserve=self._safety_reserve,
                projected_source_tokens=0,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "context budget configuration invalid",
                retryable=False,
            ) from error
        if baseline.available_input_tokens <= 0:
            return baseline
        try:
            projected = self._token_counter(
                model=self._model,
                messages=deepcopy(messages),
            )
            if (
                isinstance(projected, bool)
                or not isinstance(projected, int)
                or projected < 0
            ):
                raise ValueError("token counter returned an invalid count")
        except Exception as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "context token estimation failed",
                retryable=False,
            ) from error
        try:
            return ContextBudget.calculate(
                model_window=self._model_window,
                output_reserve=self._output_reserve,
                tool_schema_tokens=self._tool_schema_tokens,
                safety_reserve=self._safety_reserve,
                projected_source_tokens=projected,
            )
        except Exception as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "context token estimation failed",
                retryable=False,
            ) from error

    @staticmethod
    def _forced_level(
        force_level: CompactionLevel | str | None,
    ) -> CompactionLevel:
        if force_level is None:
            return CompactionLevel.L0
        try:
            return CompactionLevel(force_level)
        except ValueError as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "unknown compaction level",
                retryable=False,
            ) from error

    @classmethod
    def _project(cls, events: list[StoredEvent]) -> tuple[ContextItem, ...]:
        projected: list[ContextItem] = []
        for stored in sorted(events, key=lambda item: item.cursor):
            role_and_content = cls._role_and_content(stored.event)
            if role_and_content is None:
                if stored.event.type in {
                    "run.created",
                    "model.text.delta",
                    "tool.call.completed",
                    "context.message.appended",
                }:
                    raise AgentSDKError(
                        ErrorCode.INVALID_STATE,
                        "context source event is invalid",
                        retryable=False,
                    )
                continue
            role, content = role_and_content
            projected.append(
                ContextItem(
                    event_id=stored.event.event_id,
                    cursor=stored.cursor,
                    event_type=stored.event.type,
                    role=role,
                    content=content,
                )
            )
        return tuple(projected)

    @staticmethod
    def _role_and_content(event: EventEnvelope) -> tuple[_Role, str] | None:
        payload: Mapping[str, object] = event.payload
        if event.type == "run.created":
            content = payload.get("user_input")
            return ("user", content) if isinstance(content, str) else None
        if event.type == "model.text.delta":
            content = payload.get("text")
            return ("assistant", content) if isinstance(content, str) else None
        if event.type == "tool.call.completed":
            content = payload.get("content")
            return ("tool", content) if isinstance(content, str) else None
        if event.type != "context.message.appended" or set(payload) != {
            "role",
            "content",
        }:
            return None
        role = payload.get("role")
        content = payload.get("content")
        if role not in _APPLICATION_ROLES or not isinstance(content, str):
            return None
        return cast(_Role, role), content

    async def _persist_compacted(
        self,
        *,
        session_id: str,
        source: tuple[ContextItem, ...],
        protected: set[str],
        capsule: ContextCapsule,
        usage: UsageReported,
        budget: ContextBudget,
        recommended: CompactionLevel,
        applied: CompactionLevel,
    ) -> ContextView:
        view_id = new_id("view")
        capsule_id = new_id("cap")
        message_refs = tuple(
            item.event_id for item in source if item.event_id in protected
        )
        view = ContextView(
            view_id=view_id,
            session_id=session_id,
            message_refs=message_refs,
            capsule_id=capsule_id,
            estimated_tokens=self._estimate_compacted_tokens(
                source,
                protected,
                capsule,
            ),
            recommended_level=recommended,
            applied_level=applied,
            budget=budget,
        )
        events = (
            self._event(
                view,
                sequence=1,
                event_type="context.compaction.completed",
                payload={
                    "view_id": view_id,
                    "capsule_id": capsule_id,
                    "level": applied.value,
                    "model": self._model,
                    "budget": budget.model_dump(mode="json"),
                    "usage": usage.to_payload(),
                },
            ),
            self._view_event(view, sequence=2),
        )
        snapshots = (
            SnapshotWrite(
                "context_capsule",
                capsule_id,
                session_id,
                1,
                {
                    "session_id": session_id,
                    "capsule": capsule.model_dump(mode="json"),
                },
            ),
            SnapshotWrite(
                "context_view",
                view_id,
                session_id,
                1,
                view.model_dump(mode="json"),
            ),
        )
        await self._commit(
            CommitBatch(
                events=events,
                snapshots=snapshots,
                preconditions=(
                    SnapshotPrecondition("session", session_id),
                ),
            )
        )
        return view

    async def _persist_fallback(
        self,
        *,
        session_id: str,
        source: tuple[ContextItem, ...],
        usage: UsageReported,
        budget: ContextBudget,
        recommended: CompactionLevel,
        requested: CompactionLevel,
    ) -> ContextView:
        view = self._raw_view(session_id, source, budget, recommended)
        events = (
            self._event(
                view,
                sequence=1,
                event_type="context.compaction.failed",
                payload={
                    "view_id": view.view_id,
                    "requested_level": requested.value,
                    "code": "context_compaction_failed",
                    "budget": budget.model_dump(mode="json"),
                    "usage": usage.to_payload(),
                },
            ),
            self._view_event(view, sequence=2),
        )
        await self._commit(
            CommitBatch(
                events=events,
                snapshots=(
                    SnapshotWrite(
                        "context_view",
                        view.view_id,
                        session_id,
                        1,
                        view.model_dump(mode="json"),
                    ),
                ),
                preconditions=(
                    SnapshotPrecondition("session", session_id),
                ),
            )
        )
        return view

    async def _persist_l0(
        self,
        *,
        session_id: str,
        source: tuple[ContextItem, ...],
        budget: ContextBudget,
        recommended: CompactionLevel,
    ) -> ContextView:
        view = self._raw_view(session_id, source, budget, recommended)
        await self._commit(
            CommitBatch(
                events=(self._view_event(view, sequence=1),),
                snapshots=(
                    SnapshotWrite(
                        "context_view",
                        view.view_id,
                        session_id,
                        1,
                        view.model_dump(mode="json"),
                    ),
                ),
                preconditions=(
                    SnapshotPrecondition("session", session_id),
                ),
            )
        )
        return view

    def _estimate_compacted_tokens(
        self,
        source: tuple[ContextItem, ...],
        protected: set[str],
        capsule: ContextCapsule,
    ) -> int:
        messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": json.dumps(
                    capsule.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        ]
        messages.extend(
            {"role": item.role, "content": item.content}
            for item in source
            if item.event_id in protected
        )
        try:
            count = self._token_counter(
                model=self._model,
                messages=deepcopy(messages),
            )
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("token counter returned an invalid count")
        except Exception as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "context token estimation failed",
                retryable=False,
            ) from error
        return count

    async def _commit(self, batch: CommitBatch) -> None:
        failure: AgentSDKError | None = None
        try:
            await self._store.commit(batch)
        except SnapshotPreconditionError:
            failure = AgentSDKError(
                ErrorCode.NOT_FOUND,
                "context session no longer exists",
                retryable=False,
            )
        except Exception as error:
            del error
            failure = AgentSDKError(
                ErrorCode.INTERNAL,
                "context persistence failed",
                retryable=False,
            )
        if failure is not None:
            raise failure

    @staticmethod
    def _raw_view(
        session_id: str,
        source: tuple[ContextItem, ...],
        budget: ContextBudget,
        recommended: CompactionLevel,
    ) -> ContextView:
        return ContextView(
            view_id=new_id("view"),
            session_id=session_id,
            message_refs=tuple(item.event_id for item in source),
            capsule_id=None,
            estimated_tokens=budget.projected_source_tokens,
            recommended_level=recommended,
            applied_level=CompactionLevel.L0,
            budget=budget,
        )

    @staticmethod
    def _event(
        view: ContextView,
        *,
        sequence: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> EventEnvelope:
        return EventEnvelope.new(
            type=event_type,
            session_id=view.session_id,
            run_id=view.view_id,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def _view_event(cls, view: ContextView, *, sequence: int) -> EventEnvelope:
        return cls._event(
            view,
            sequence=sequence,
            event_type="context.view.created",
            payload={
                "view_id": view.view_id,
                "capsule_id": view.capsule_id,
                "recommended_level": view.recommended_level.value,
                "applied_level": view.applied_level.value,
                "estimated_tokens": view.estimated_tokens,
            },
        )
