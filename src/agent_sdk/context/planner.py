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
    SourceMessage,
)
from agent_sdk.context.rendering import render_level
from agent_sdk.context.retrieval import ContextRetrieval
from agent_sdk.context.strategies import StrategyResult
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
from agent_sdk.tools.models import thaw_json

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
        recent_messages: int = 2,
        tool_preview_bytes: int = 4_096,
        _token_counter: TokenCounter = default_token_counter,
    ) -> None:
        if (
            isinstance(recent_messages, bool)
            or not isinstance(recent_messages, int)
            or recent_messages < 0
        ):
            raise ValueError("recent_messages must be a non-negative integer")
        if (
            isinstance(tool_preview_bytes, bool)
            or not isinstance(tool_preview_bytes, int)
            or tool_preview_bytes < 0
        ):
            raise ValueError("tool_preview_bytes must be a non-negative integer")
        self._store = store
        self._model = model
        self._model_window = model_window
        self._output_reserve = output_reserve
        self._tool_schema_tokens = tool_schema_tokens
        self._safety_reserve = safety_reserve
        self._policy = policy or CompactionPolicy()
        self._recent_messages = recent_messages
        self._tool_preview_bytes = tool_preview_bytes
        self._token_counter = _token_counter
        self._compactor = ContextCompactor(models, model=model)
        self._retrieval = ContextRetrieval(store)

    async def build(
        self,
        session_id: str,
        *,
        force_level: CompactionLevel | str | None = None,
        protected_event_ids: Iterable[str] = (),
        allow_lossy: bool = True,
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
        requested = self._requested_level(force_level, recommended)
        if not isinstance(allow_lossy, bool):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "allow_lossy must be a boolean",
                retryable=False,
            )
        if not allow_lossy and requested in {
            CompactionLevel.L3,
            CompactionLevel.L4,
        }:
            requested = CompactionLevel.L2
        if requested in {CompactionLevel.L3, CompactionLevel.L4} and not source:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "context sources are empty",
                retryable=False,
            )

        sources = self._source_messages(source, protected)
        if requested in {
            CompactionLevel.L0,
            CompactionLevel.L1,
            CompactionLevel.L2,
        }:
            rendered = self._render(requested, sources)
            return await self._persist_deterministic(
                session_id=session_id,
                rendered=rendered,
                budget=budget,
                recommended=recommended,
                applied=requested,
            )

        retained = set(protected)
        if self._recent_messages:
            retained.update(
                item.event_id for item in source[-self._recent_messages :]
            )
        if requested is CompactionLevel.L3:
            result = await self._compactor.summarize(source, retained)
            prior_refs: tuple[str, ...] = ()
        else:
            records = await self._retrieval.list_capsule_records(
                session_id=session_id
            )
            capsule_ids = tuple(record[0] for record in records)
            capsules = tuple(record[1] for record in records)
            result = await self._compactor.rebase(
                capsules,
                source,
                retained,
                capsule_ids=capsule_ids,
            )
            prior_refs = capsule_ids
        if result.capsule is None:
            fallback = self._render(CompactionLevel.L2, sources)
            return await self._persist_fallback(
                session_id=session_id,
                rendered=fallback,
                usage=result.usage,
                budget=budget,
                recommended=recommended,
                requested=requested,
            )
        estimated_tokens = self._estimate_compacted_tokens(
            source,
            retained,
            result.capsule,
        )
        if estimated_tokens > budget.available_input_tokens:
            fallback = self._render(CompactionLevel.L2, sources)
            return await self._persist_fallback(
                session_id=session_id,
                rendered=fallback,
                usage=result.usage,
                budget=budget,
                recommended=recommended,
                requested=requested,
            )
        return await self._persist_compacted(
            session_id=session_id,
            source=source,
            retained=retained,
            prior_refs=prior_refs,
            capsule=result.capsule,
            usage=result.usage,
            budget=budget,
            recommended=recommended,
            applied=requested,
            estimated_tokens=estimated_tokens,
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
        projected = self._estimate_messages(messages)
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
    def _requested_level(
        force_level: CompactionLevel | str | None,
        recommended: CompactionLevel,
    ) -> CompactionLevel:
        if force_level is None:
            return recommended
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

    @staticmethod
    def _source_messages(
        source: tuple[ContextItem, ...],
        protected: set[str],
    ) -> tuple[SourceMessage, ...]:
        return tuple(
            SourceMessage(
                ref=item.event_id,
                role=item.role,
                message={"role": item.role, "content": item.content},
                event_type=item.event_type,
                protected=item.event_id in protected,
            )
            for item in source
        )

    def _render(
        self,
        level: CompactionLevel,
        source: tuple[SourceMessage, ...],
    ) -> StrategyResult:
        return render_level(
            level,
            source,
            recent_messages=self._recent_messages,
            tool_preview_bytes=self._tool_preview_bytes,
        )

    async def _persist_deterministic(
        self,
        *,
        session_id: str,
        rendered: StrategyResult,
        budget: ContextBudget,
        recommended: CompactionLevel,
        applied: CompactionLevel,
    ) -> ContextView:
        view = self._rendered_view(
            session_id=session_id,
            rendered=rendered,
            budget=budget,
            recommended=recommended,
            applied=applied,
            fallback_from=None,
        )
        await self._persist_view(view, usage=None)
        return view

    async def _persist_compacted(
        self,
        *,
        session_id: str,
        source: tuple[ContextItem, ...],
        retained: set[str],
        prior_refs: tuple[str, ...],
        capsule: ContextCapsule,
        usage: UsageReported,
        budget: ContextBudget,
        recommended: CompactionLevel,
        applied: CompactionLevel,
        estimated_tokens: int,
    ) -> ContextView:
        view_id = new_id("view")
        capsule_id = new_id("cap")
        message_refs = tuple(
            item.event_id for item in source if item.event_id in retained
        )
        current_refs = tuple(item.event_id for item in source)
        source_refs = tuple(dict.fromkeys((*prior_refs, *current_refs)))
        transformed = tuple(
            f"{applied.value.lower()}:{ref}"
            for ref in source_refs
            if ref not in message_refs
        )
        view = ContextView(
            view_id=view_id,
            session_id=session_id,
            message_refs=message_refs,
            capsule_id=capsule_id,
            estimated_tokens=estimated_tokens,
            recommended_level=recommended,
            applied_level=applied,
            budget=budget,
            source_refs=source_refs,
            transformations=transformed,
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
                    "estimated_tokens": view.estimated_tokens,
                    "message_refs": list(view.message_refs),
                    "source_refs": list(view.source_refs),
                    "transformations": list(view.transformations),
                    "usage": usage.to_payload(),
                },
            ),
            self._view_event(view, sequence=2, usage=usage),
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
                preconditions=(SnapshotPrecondition("session", session_id),),
            )
        )
        return view

    async def _persist_fallback(
        self,
        *,
        session_id: str,
        rendered: StrategyResult,
        usage: UsageReported,
        budget: ContextBudget,
        recommended: CompactionLevel,
        requested: CompactionLevel,
    ) -> ContextView:
        view = self._rendered_view(
            session_id=session_id,
            rendered=rendered,
            budget=budget,
            recommended=recommended,
            applied=CompactionLevel.L2,
            fallback_from=requested,
        )
        events = (
            self._event(
                view,
                sequence=1,
                event_type="context.compaction.failed",
                payload={
                    "view_id": view.view_id,
                    "requested_level": requested.value,
                    "applied_level": CompactionLevel.L2.value,
                    "code": "context_compaction_failed",
                    "budget": budget.model_dump(mode="json"),
                    "estimated_tokens": view.estimated_tokens,
                    "message_refs": list(view.message_refs),
                    "source_refs": list(view.source_refs),
                    "transformations": list(view.transformations),
                    "usage": usage.to_payload(),
                },
            ),
            self._view_event(view, sequence=2, usage=usage),
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
                preconditions=(SnapshotPrecondition("session", session_id),),
            )
        )
        return view

    def _rendered_view(
        self,
        *,
        session_id: str,
        rendered: StrategyResult,
        budget: ContextBudget,
        recommended: CompactionLevel,
        applied: CompactionLevel,
        fallback_from: CompactionLevel | None,
    ) -> ContextView:
        messages = []
        for item in rendered.items:
            message = thaw_json(item.message)
            assert isinstance(message, dict)
            messages.append(message)
        return ContextView(
            view_id=new_id("view"),
            session_id=session_id,
            message_refs=tuple(item.ref for item in rendered.items),
            capsule_id=None,
            estimated_tokens=self._estimate_messages(messages),
            recommended_level=recommended,
            applied_level=applied,
            budget=budget,
            source_refs=rendered.source_refs,
            transformations=rendered.transformations,
            fallback_from=fallback_from,
        )

    async def _persist_view(
        self,
        view: ContextView,
        *,
        usage: UsageReported | None,
    ) -> None:
        await self._commit(
            CommitBatch(
                events=(self._view_event(view, sequence=1, usage=usage),),
                snapshots=(
                    SnapshotWrite(
                        "context_view",
                        view.view_id,
                        view.session_id,
                        1,
                        view.model_dump(mode="json"),
                    ),
                ),
                preconditions=(
                    SnapshotPrecondition("session", view.session_id),
                ),
            )
        )

    def _estimate_compacted_tokens(
        self,
        source: tuple[ContextItem, ...],
        retained: set[str],
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
            if item.event_id in retained
        )
        return self._estimate_messages(messages)

    def _estimate_messages(self, messages: list[dict[str, Any]]) -> int:
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
        except Exception:
            failure = AgentSDKError(
                ErrorCode.INTERNAL,
                "context persistence failed",
                retryable=False,
            )
        if failure is not None:
            raise failure

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
    def _view_event(
        cls,
        view: ContextView,
        *,
        sequence: int,
        usage: UsageReported | None,
    ) -> EventEnvelope:
        return cls._event(
            view,
            sequence=sequence,
            event_type="context.view.created",
            payload={
                "view_id": view.view_id,
                "capsule_id": view.capsule_id,
                "recommended_level": view.recommended_level.value,
                "applied_level": view.applied_level.value,
                "fallback_from": (
                    view.fallback_from.value
                    if view.fallback_from is not None
                    else None
                ),
                "estimated_tokens": view.estimated_tokens,
                "budget": (
                    view.budget.model_dump(mode="json")
                    if view.budget is not None
                    else None
                ),
                "message_refs": list(view.message_refs),
                "source_refs": list(view.source_refs),
                "transformations": list(view.transformations),
                "compaction_usage": (
                    usage.to_payload() if usage is not None else None
                ),
            },
        )
