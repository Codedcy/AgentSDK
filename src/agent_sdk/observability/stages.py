from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Literal

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.runtime.models import TokenUsage

from .models import (
    ObservedEvent,
    PUBLIC_REF_MAX_BYTES,
    PUBLIC_REF_MAX_ITEMS,
    TraceStage,
    TraceStageKind,
    TraceStageStatus,
    is_public_evidence_id,
)


@dataclass(frozen=True)
class StageEventRule:
    kind: TraceStageKind
    transition: Literal["start", "terminal", "point"]
    id_fields: tuple[str, ...]
    status: TraceStageStatus
    schema_versions: frozenset[int] = frozenset({1})


RULES: Mapping[str, StageEventRule] = {
    "run.started": StageEventRule(TraceStageKind.RUN, "start", ("run_id",), TraceStageStatus.RUNNING),
    "run.completed": StageEventRule(TraceStageKind.RUN, "terminal", ("run_id",), TraceStageStatus.COMPLETED),
    "run.failed": StageEventRule(TraceStageKind.RUN, "terminal", ("run_id",), TraceStageStatus.FAILED),
    "run.interrupted": StageEventRule(TraceStageKind.RUN, "terminal", ("run_id",), TraceStageStatus.INTERRUPTED),
    "step.started": StageEventRule(TraceStageKind.STEP, "start", ("step_id",), TraceStageStatus.RUNNING),
    "step.completed": StageEventRule(TraceStageKind.STEP, "terminal", ("step_id",), TraceStageStatus.COMPLETED),
    "step.failed": StageEventRule(TraceStageKind.STEP, "terminal", ("step_id",), TraceStageStatus.FAILED),
    "step.timed_out": StageEventRule(TraceStageKind.STEP, "terminal", ("step_id",), TraceStageStatus.TIMED_OUT),
    "model.call.started": StageEventRule(TraceStageKind.MODEL, "start", ("operation_id",), TraceStageStatus.RUNNING),
    "model.call.completed": StageEventRule(TraceStageKind.MODEL, "terminal", ("operation_id",), TraceStageStatus.COMPLETED),
    "model.call.failed": StageEventRule(TraceStageKind.MODEL, "terminal", ("operation_id",), TraceStageStatus.FAILED),
    "model.call.timed_out": StageEventRule(TraceStageKind.MODEL, "terminal", ("operation_id",), TraceStageStatus.TIMED_OUT),
    "tool.call.started": StageEventRule(TraceStageKind.TOOL, "start", ("call_id",), TraceStageStatus.RUNNING),
    "tool.call.completed": StageEventRule(TraceStageKind.TOOL, "terminal", ("call_id",), TraceStageStatus.COMPLETED),
    "tool.call.failed": StageEventRule(TraceStageKind.TOOL, "terminal", ("call_id",), TraceStageStatus.FAILED),
    "tool.call.denied": StageEventRule(TraceStageKind.TOOL, "terminal", ("call_id",), TraceStageStatus.DENIED),
    "tool.call.timed_out": StageEventRule(TraceStageKind.TOOL, "terminal", ("call_id",), TraceStageStatus.TIMED_OUT),
    "permission.requested": StageEventRule(TraceStageKind.PERMISSION, "start", ("request_id",), TraceStageStatus.WAITING),
    "permission.resolved": StageEventRule(TraceStageKind.PERMISSION, "terminal", ("request_id",), TraceStageStatus.COMPLETED),
    "permission.denied": StageEventRule(TraceStageKind.PERMISSION, "terminal", ("request_id",), TraceStageStatus.DENIED),
    "permission.timed_out": StageEventRule(TraceStageKind.PERMISSION, "terminal", ("request_id",), TraceStageStatus.TIMED_OUT),
    "context.view.created": StageEventRule(TraceStageKind.CONTEXT, "point", ("view_id",), TraceStageStatus.COMPLETED),
    "workflow.started": StageEventRule(TraceStageKind.WORKFLOW, "start", ("workflow_run_id",), TraceStageStatus.RUNNING),
    "workflow.completed": StageEventRule(TraceStageKind.WORKFLOW, "terminal", ("workflow_run_id",), TraceStageStatus.COMPLETED),
    "workflow.failed": StageEventRule(TraceStageKind.WORKFLOW, "terminal", ("workflow_run_id",), TraceStageStatus.FAILED),
    "workflow.node.started": StageEventRule(TraceStageKind.WORKFLOW_NODE, "start", ("workflow_run_id", "node_id"), TraceStageStatus.RUNNING),
    "workflow.node.completed": StageEventRule(TraceStageKind.WORKFLOW_NODE, "terminal", ("workflow_run_id", "node_id"), TraceStageStatus.COMPLETED),
    "workflow.node.failed": StageEventRule(TraceStageKind.WORKFLOW_NODE, "terminal", ("workflow_run_id", "node_id"), TraceStageStatus.FAILED),
    "workflow.node.timed_out": StageEventRule(TraceStageKind.WORKFLOW_NODE, "terminal", ("workflow_run_id", "node_id"), TraceStageStatus.TIMED_OUT),
    "child.created": StageEventRule(TraceStageKind.CHILD, "start", ("child_run_id",), TraceStageStatus.WAITING),
    "child.completed": StageEventRule(TraceStageKind.CHILD, "terminal", ("child_run_id",), TraceStageStatus.COMPLETED),
    "child.failed": StageEventRule(TraceStageKind.CHILD, "terminal", ("child_run_id",), TraceStageStatus.FAILED),
    "child.timed_out": StageEventRule(TraceStageKind.CHILD, "terminal", ("child_run_id",), TraceStageStatus.TIMED_OUT),
    "child.interrupted": StageEventRule(TraceStageKind.CHILD, "terminal", ("child_run_id",), TraceStageStatus.INTERRUPTED),
    "agent.message.sent": StageEventRule(TraceStageKind.MESSAGE, "point", ("message_id",), TraceStageStatus.COMPLETED),
    "evaluation.completed": StageEventRule(TraceStageKind.EVALUATION, "point", ("evaluation_id",), TraceStageStatus.COMPLETED),
    "run.recovery.started": StageEventRule(TraceStageKind.RECOVERY, "point", ("run_id", "sequence"), TraceStageStatus.RUNNING),
    "model.recovery.query.started": StageEventRule(TraceStageKind.RECOVERY, "point", ("operation_id",), TraceStageStatus.RUNNING),
    "model.recovery.resend.started": StageEventRule(TraceStageKind.RECOVERY, "point", ("operation_id",), TraceStageStatus.RUNNING),
    "tool.recovery.retry.started": StageEventRule(TraceStageKind.RECOVERY, "point", ("operation_id",), TraceStageStatus.RUNNING),
    "reconciliation.requested": StageEventRule(TraceStageKind.RECOVERY, "start", ("request_id",), TraceStageStatus.WAITING),
    "reconciliation.resolved": StageEventRule(TraceStageKind.RECOVERY, "terminal", ("request_id",), TraceStageStatus.COMPLETED),
}

_V1_V2_EVENT_TYPES = frozenset(
    {
        "step.started",
        "step.completed",
        "step.failed",
        "model.call.started",
        "model.call.completed",
        "model.call.failed",
        "tool.call.started",
        "tool.call.completed",
        "permission.requested",
        "permission.resolved",
    }
)
RULES = {
    event_type: (
        replace(rule, schema_versions=frozenset({1, 2}))
        if event_type in _V1_V2_EVENT_TYPES
        else rule
    )
    for event_type, rule in RULES.items()
}


@dataclass(frozen=True)
class StageReferenceRule:
    input_fields: tuple[str, ...] = ()
    input_sequence_fields: tuple[str, ...] = ()
    output_fields: tuple[str, ...] = ()
    output_event: bool = False


REFERENCE_RULES: Mapping[str, StageReferenceRule] = {
    "run.completed": StageReferenceRule(output_event=True),
    "run.failed": StageReferenceRule(output_event=True),
    "run.interrupted": StageReferenceRule(output_event=True),
    "model.call.started": StageReferenceRule(
        input_fields=("context_view_id", "prompt_manifest_id"),
    ),
    "model.call.completed": StageReferenceRule(
        input_fields=("context_view_id", "prompt_manifest_id"),
        output_event=True,
    ),
    "model.call.failed": StageReferenceRule(
        input_fields=("context_view_id", "prompt_manifest_id"),
        output_event=True,
    ),
    "model.call.timed_out": StageReferenceRule(
        input_fields=("context_view_id", "prompt_manifest_id"),
        output_event=True,
    ),
    "tool.call.started": StageReferenceRule(input_fields=("step_id",)),
    "tool.call.completed": StageReferenceRule(
        input_fields=("step_id",),
        output_event=True,
    ),
    "tool.call.failed": StageReferenceRule(
        input_fields=("step_id",),
        output_event=True,
    ),
    "tool.call.denied": StageReferenceRule(
        input_fields=("step_id",),
        output_event=True,
    ),
    "tool.call.timed_out": StageReferenceRule(
        input_fields=("step_id",),
        output_event=True,
    ),
    "permission.requested": StageReferenceRule(
        input_fields=("call_id",),
        output_fields=("request_id",),
    ),
    "permission.resolved": StageReferenceRule(
        input_fields=("call_id", "request_id"),
        output_event=True,
    ),
    "permission.denied": StageReferenceRule(
        input_fields=("call_id", "request_id"),
        output_event=True,
    ),
    "permission.timed_out": StageReferenceRule(
        input_fields=("call_id", "request_id"),
        output_event=True,
    ),
    "context.view.created": StageReferenceRule(
        input_sequence_fields=(
            "source_refs",
            "message_refs",
            "consumed_message_ids",
        ),
        output_fields=("view_id", "capsule_id"),
    ),
    "workflow.completed": StageReferenceRule(output_event=True),
    "workflow.failed": StageReferenceRule(output_event=True),
    "workflow.node.started": StageReferenceRule(input_fields=("instruction_id",)),
    "workflow.node.completed": StageReferenceRule(
        input_fields=("instruction_id",),
        output_fields=("run_id",),
        output_event=True,
    ),
    "workflow.node.failed": StageReferenceRule(
        input_fields=("instruction_id",),
        output_fields=("run_id",),
        output_event=True,
    ),
    "workflow.node.timed_out": StageReferenceRule(
        input_fields=("instruction_id",),
        output_fields=("run_id",),
        output_event=True,
    ),
    "child.created": StageReferenceRule(
        input_fields=("parent_run_id",),
        output_fields=("child_run_id",),
    ),
    "child.completed": StageReferenceRule(output_event=True),
    "child.failed": StageReferenceRule(output_event=True),
    "child.timed_out": StageReferenceRule(output_event=True),
    "child.interrupted": StageReferenceRule(output_event=True),
    "agent.message.sent": StageReferenceRule(
        input_fields=("sender_run_id", "recipient_run_id"),
        output_fields=("message_id",),
    ),
    "evaluation.completed": StageReferenceRule(
        input_fields=("subject_run_id",),
        output_fields=("evaluation_id",),
    ),
    "run.recovery.started": StageReferenceRule(input_fields=("run_id",)),
    "model.recovery.query.started": StageReferenceRule(input_fields=("operation_id",)),
    "model.recovery.resend.started": StageReferenceRule(input_fields=("operation_id",)),
    "tool.recovery.retry.started": StageReferenceRule(input_fields=("operation_id",)),
    "reconciliation.requested": StageReferenceRule(output_fields=("request_id",)),
    "reconciliation.resolved": StageReferenceRule(
        input_fields=("request_id",),
        output_event=True,
    ),
}


@dataclass(frozen=True)
class _MutableStage:
    kind: TraceStageKind
    key: tuple[str, ...]
    status: TraceStageStatus
    run_id: str
    session_id: str
    started_at: datetime | None
    ended_at: datetime | None
    first_cursor: int
    last_cursor: int
    evidence_event_ids: tuple[str, ...]
    evidence_cursors: tuple[int, ...]
    input_refs: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()
    usage: TokenUsage | None = None
    error_code: str | None = None
    retryable: bool | None = None
    parent_hint: tuple[TraceStageKind, tuple[str, ...]] | None = None
    terminal: bool = False


class _ProjectionFailure(Exception):
    pass


class _ProjectionResult(Enum):
    FAILED = "failed"


def project_stages(events: tuple[ObservedEvent, ...]) -> tuple[TraceStage, ...]:
    result = _try_project_stages(events)
    if result is _ProjectionResult.FAILED:
        raise AgentSDKError(
            ErrorCode.INTERNAL,
            "failed to project trace stages",
            retryable=False,
        ) from None
    return result


def _try_project_stages(
    events: tuple[ObservedEvent, ...],
) -> tuple[TraceStage, ...] | _ProjectionResult:
    try:
        return _project_stages(events)
    except Exception:
        return _ProjectionResult.FAILED


def _project_stages(events: tuple[ObservedEvent, ...]) -> tuple[TraceStage, ...]:
    ordered = _correlate_legacy_events(sorted(events, key=lambda item: item.cursor))
    if any(left.cursor >= right.cursor for left, right in zip(ordered, ordered[1:], strict=False)):
        raise _ProjectionFailure
    stages: dict[tuple[TraceStageKind, tuple[str, ...]], _MutableStage] = {}
    pending_model_usage: dict[str, tuple[TokenUsage, ObservedEvent]] = {}
    run_parents = _run_parents(ordered)
    context_parents = _context_parents(ordered)
    projection_events = _with_derived_child_events(ordered, run_parents)

    for observed in projection_events:
        event = observed.event
        rule = RULES.get(event.type)
        allowed_versions = (
            frozenset({1, 2})
            if event.type == "model.usage.reported"
            else None if rule is None else rule.schema_versions
        )
        if allowed_versions is not None and event.schema_version not in allowed_versions:
            raise _ProjectionFailure
        if event.type == "model.usage.reported":
            operation_id = _identifier(observed, "operation_id")
            usage = _usage(event.payload)
            if usage is None or operation_id in pending_model_usage:
                raise _ProjectionFailure
            pending_model_usage[operation_id] = (usage, observed)
            stage_key = (TraceStageKind.MODEL, (operation_id,))
            if stage_key in stages:
                usage_stage = stages[stage_key]
                if (
                    usage_stage.terminal
                    or usage_stage.session_id != _bounded_identifier(event.session_id)
                    or usage_stage.run_id != _event_run_id(observed, context_parents)
                ):
                    raise _ProjectionFailure
                evidence_ids, evidence_cursors = _evidence(observed)
                stages[stage_key] = replace(
                    usage_stage,
                    usage=usage,
                    first_cursor=min(usage_stage.first_cursor, observed.cursor),
                    last_cursor=max(usage_stage.last_cursor, observed.cursor),
                    evidence_event_ids=(*usage_stage.evidence_event_ids, *evidence_ids),
                    evidence_cursors=(*usage_stage.evidence_cursors, *evidence_cursors),
                )
            continue
        if rule is None:
            continue
        key = tuple(_identifier(observed, field) for field in rule.id_fields)
        lookup = (rule.kind, key)
        current = stages.get(lookup)
        status = _effective_status(rule.status, event.payload)
        evidence_ids, evidence_cursors = _evidence(observed)
        run_id = _event_run_id(observed, context_parents)
        session_id = _bounded_identifier(event.session_id)
        input_refs, output_refs = _public_refs(observed)
        error_code, retryable = _failure_fact(rule.kind, status, event.payload)
        if rule.transition == "start":
            if current is not None:
                raise _ProjectionFailure
            pending_usage = (
                pending_model_usage.get(key[0])
                if rule.kind is TraceStageKind.MODEL
                else None
            )
            usage = None if pending_usage is None else pending_usage[0]
            pending_observed = None if pending_usage is None else pending_usage[1]
            first_cursor = (
                observed.cursor
                if pending_observed is None
                else min(pending_observed.cursor, observed.cursor)
            )
            start_evidence_event_ids = evidence_ids
            start_evidence_cursors = evidence_cursors
            if pending_observed is not None and pending_observed.cursor < observed.cursor:
                pending_evidence_ids, pending_evidence_cursors = _evidence(
                    pending_observed
                )
                start_evidence_event_ids = (
                    *pending_evidence_ids,
                    *evidence_ids,
                )
                start_evidence_cursors = (
                    *pending_evidence_cursors,
                    *evidence_cursors,
                )
            stages[lookup] = _MutableStage(
                kind=rule.kind,
                key=key,
                status=status,
                run_id=run_id,
                session_id=session_id,
                started_at=event.occurred_at,
                ended_at=None,
                first_cursor=first_cursor,
                last_cursor=observed.cursor,
                evidence_event_ids=start_evidence_event_ids,
                evidence_cursors=start_evidence_cursors,
                input_refs=input_refs,
                output_refs=output_refs,
                usage=usage,
                error_code=error_code,
                retryable=retryable,
                parent_hint=_parent_hint(rule.kind, observed, run_parents, context_parents),
            )
            continue
        if rule.transition == "point":
            if current is not None:
                raise _ProjectionFailure
            stages[lookup] = _MutableStage(
                kind=rule.kind,
                key=key,
                status=status,
                run_id=run_id,
                session_id=session_id,
                started_at=event.occurred_at,
                ended_at=event.occurred_at,
                first_cursor=observed.cursor,
                last_cursor=observed.cursor,
                evidence_event_ids=evidence_ids,
                evidence_cursors=evidence_cursors,
                input_refs=input_refs,
                output_refs=output_refs,
                error_code=error_code,
                retryable=retryable,
                parent_hint=_parent_hint(rule.kind, observed, run_parents, context_parents),
                terminal=True,
            )
            continue
        terminal_usage = _usage(event.payload)
        if current is None:
            pending_usage = (
                pending_model_usage.get(key[0])
                if rule.kind is TraceStageKind.MODEL
                else None
            )
            pending_observed = None if pending_usage is None else pending_usage[1]
            first_cursor = (
                observed.cursor
                if pending_observed is None
                else min(pending_observed.cursor, observed.cursor)
            )
            terminal_evidence_event_ids = evidence_ids
            terminal_evidence_cursors = evidence_cursors
            if pending_observed is not None and pending_observed.cursor < observed.cursor:
                pending_evidence_ids, pending_evidence_cursors = _evidence(
                    pending_observed
                )
                terminal_evidence_event_ids = (
                    *pending_evidence_ids,
                    *evidence_ids,
                )
                terminal_evidence_cursors = (
                    *pending_evidence_cursors,
                    *evidence_cursors,
                )
            stages[lookup] = _MutableStage(
                kind=rule.kind,
                key=key,
                status=status,
                run_id=run_id,
                session_id=session_id,
                started_at=None,
                ended_at=event.occurred_at,
                first_cursor=first_cursor,
                last_cursor=observed.cursor,
                evidence_event_ids=terminal_evidence_event_ids,
                evidence_cursors=terminal_evidence_cursors,
                input_refs=input_refs,
                output_refs=output_refs,
                usage=(
                    terminal_usage
                    or (None if pending_usage is None else pending_usage[0])
                ),
                error_code=error_code,
                retryable=retryable,
                parent_hint=_parent_hint(rule.kind, observed, run_parents, context_parents),
                terminal=True,
            )
            continue
        if current.run_id != run_id or current.session_id != session_id:
            raise _ProjectionFailure
        resumed_terminal = (
            rule.kind in {TraceStageKind.RUN, TraceStageKind.CHILD}
            and current.status is TraceStageStatus.INTERRUPTED
            and status in {TraceStageStatus.COMPLETED, TraceStageStatus.FAILED}
        )
        if current.terminal and not resumed_terminal:
            raise _ProjectionFailure
        terminal_parent = _parent_hint(
            rule.kind,
            observed,
            run_parents,
            context_parents,
        )
        if (
            rule.kind is TraceStageKind.TOOL
            and current.parent_hint is not None
            and current.parent_hint[0] is TraceStageKind.STEP
            and isinstance(observed.event.payload.get("step_id"), str)
            and terminal_parent != current.parent_hint
        ):
            raise _ProjectionFailure
        stages[lookup] = replace(
            current,
            status=status,
            ended_at=event.occurred_at,
            last_cursor=observed.cursor,
            evidence_event_ids=(*current.evidence_event_ids, *evidence_ids),
            evidence_cursors=(*current.evidence_cursors, *evidence_cursors),
            input_refs=_merge_refs(current.input_refs, input_refs),
            output_refs=_merge_refs(current.output_refs, output_refs),
            usage=(
                terminal_usage
                or current.usage
                or (
                    pending_model_usage[key[0]][0]
                    if key[0] in pending_model_usage
                    else None
                )
            ),
            error_code=error_code,
            retryable=retryable,
            terminal=True,
        )

    ids = {lookup: _stage_id(*lookup) for lookup in stages}
    fallback_run_usage = _aggregate_model_usage(stages.values())
    projected: list[TraceStage] = []
    for lookup, stage in sorted(stages.items(), key=lambda item: item[1].first_cursor):
        parent_id = ids.get(stage.parent_hint) if stage.parent_hint is not None else None
        usage = stage.usage
        if (
            usage is None
            and stage.kind is TraceStageKind.RUN
            and stage.status
            in {TraceStageStatus.FAILED, TraceStageStatus.INTERRUPTED}
        ):
            usage = fallback_run_usage.get(stage.run_id)
        duration_ms: int | None = None
        if stage.started_at is not None and stage.ended_at is not None:
            seconds = (stage.ended_at - stage.started_at).total_seconds()
            duration_ms = max(0, round(seconds * 1000))
        projected.append(
            TraceStage(
                stage_id=ids[lookup],
                kind=stage.kind,
                status=stage.status,
                entity_id=stage.key[-1],
                run_id=stage.run_id,
                session_id=stage.session_id,
                parent_stage_id=parent_id,
                started_at=stage.started_at,
                ended_at=stage.ended_at,
                duration_ms=duration_ms,
                first_cursor=stage.first_cursor,
                last_cursor=stage.last_cursor,
                input_refs=stage.input_refs,
                output_refs=stage.output_refs,
                usage=usage,
                cost_usd=(None if usage is None else usage.cost_usd),
                error_code=stage.error_code,
                retryable=stage.retryable,
                evidence_event_ids=stage.evidence_event_ids,
                evidence_cursors=stage.evidence_cursors,
            )
        )
    return tuple(projected)


def _aggregate_model_usage(
    stages: Iterable[_MutableStage],
) -> dict[str, TokenUsage]:
    totals: dict[str, TokenUsage] = {}
    for stage in sorted(stages, key=lambda item: item.first_cursor):
        usage = stage.usage
        if (
            stage.kind is not TraceStageKind.MODEL
            or usage is None
            or not _has_usage_fact(usage)
        ):
            continue
        current = totals.get(stage.run_id)
        totals[stage.run_id] = usage if current is None else _add_usage(current, usage)
    return totals


def _has_usage_fact(usage: TokenUsage) -> bool:
    return any(
        value is not None
        for value in (
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.cost_usd,
        )
    )


def _add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    def add(first: int | None, second: int | None) -> int | None:
        if first is None:
            return second
        if second is None:
            return first
        return first + second

    left_cost = left.cost_usd
    right_cost = right.cost_usd
    cost = (
        right_cost
        if left_cost is None
        else left_cost
        if right_cost is None
        else left_cost + right_cost
    )
    return TokenUsage(
        prompt_tokens=add(left.prompt_tokens, right.prompt_tokens),
        completion_tokens=add(left.completion_tokens, right.completion_tokens),
        total_tokens=add(left.total_tokens, right.total_tokens),
        cost_usd=cost,
    )


def _with_derived_child_events(
    events: list[ObservedEvent],
    run_parents: Mapping[str, tuple[TraceStageKind, tuple[str, ...]]],
) -> list[ObservedEvent]:
    explicit_children = {
        child_id
        for observed in events
        if observed.event.type.startswith("child.")
        for child_id in (observed.event.payload.get("child_run_id"),)
        if isinstance(child_id, str)
    }
    terminal_types = {
        "run.completed": "child.completed",
        "run.failed": "child.failed",
        "run.interrupted": "child.interrupted",
    }
    expanded: list[ObservedEvent] = []
    for observed in events:
        expanded.append(observed)
        event = observed.event
        child_id = event.run_id
        if not isinstance(child_id, str) or child_id in explicit_children:
            continue
        parent_hint = run_parents.get(child_id)
        if parent_hint is None or parent_hint[0] is not TraceStageKind.RUN:
            continue
        parent_id = parent_hint[1][0]
        derived_type: str | None = None
        if event.type == "run.created":
            derived_type = "child.created"
        elif event.type in terminal_types:
            derived_type = terminal_types[event.type]
        if derived_type is None:
            continue
        expanded.append(
            ObservedEvent(
                cursor=observed.cursor,
                event=event.model_copy(
                    update={
                        "type": derived_type,
                        "schema_version": 1,
                        "payload": {
                            "child_run_id": child_id,
                            "parent_run_id": parent_id,
                        },
                    }
                ),
            )
        )
    return expanded


def _correlate_legacy_events(events: list[ObservedEvent]) -> list[ObservedEvent]:
    active_steps: dict[str, str] = {}
    active_models: dict[str, str] = {}
    step_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    correlated: list[ObservedEvent] = []
    step_terminals = {"step.completed", "step.failed", "step.timed_out"}
    model_terminals = {
        "model.call.completed",
        "model.call.failed",
        "model.call.timed_out",
    }

    for observed in events:
        event = observed.event
        run_id = event.run_id
        if event.schema_version != 1 or event.type not in {
            "step.started",
            *step_terminals,
            "model.call.started",
            "model.usage.reported",
            *model_terminals,
        }:
            if isinstance(run_id, str) and run_id:
                identity = event.payload.get("step_id")
                if event.type == "step.started" and isinstance(identity, str):
                    active_steps[run_id] = identity
                elif event.type in step_terminals:
                    active_steps.pop(run_id, None)
                identity = event.payload.get("operation_id")
                if event.type == "model.call.started" and isinstance(identity, str):
                    active_models[run_id] = identity
                elif event.type in model_terminals:
                    active_models.pop(run_id, None)
            correlated.append(observed)
            continue
        if not isinstance(run_id, str) or not run_id:
            correlated.append(observed)
            continue
        payload = dict(event.payload)
        if event.type == "step.started":
            step_id = payload.get("step_id")
            if step_id is None:
                count = step_counts.get(run_id, 0)
                step_id = _legacy_id("step", run_id, count)
                step_counts[run_id] = count + 1
                payload["step_id"] = step_id
            if isinstance(step_id, str):
                active_steps[run_id] = step_id
        elif event.type in step_terminals:
            step_id = payload.get("step_id", active_steps.get(run_id))
            if step_id is not None:
                payload["step_id"] = step_id
            if step_id == active_steps.get(run_id):
                active_steps.pop(run_id, None)
        elif event.type == "model.call.started":
            operation_id = payload.get("operation_id")
            if operation_id is None:
                count = model_counts.get(run_id, 0)
                operation_id = _legacy_id("model", run_id, count)
                model_counts[run_id] = count + 1
                payload["operation_id"] = operation_id
            if isinstance(operation_id, str):
                active_models[run_id] = operation_id
            if "step_id" not in payload and run_id in active_steps:
                payload["step_id"] = active_steps[run_id]
        else:
            operation_id = payload.get("operation_id", active_models.get(run_id))
            if operation_id is not None:
                payload["operation_id"] = operation_id
            if "step_id" not in payload and run_id in active_steps:
                payload["step_id"] = active_steps[run_id]
            if event.type in model_terminals and operation_id == active_models.get(run_id):
                active_models.pop(run_id, None)
        correlated.append(
            ObservedEvent(
                cursor=observed.cursor,
                event=event.model_copy(update={"payload": payload}),
            )
        )
    return correlated


def _legacy_id(kind: str, run_id: str, count: int) -> str:
    encoded = f"{kind}:{run_id}:{count}".encode("utf-8")
    return f"legacy_{kind}_{sha256(encoded).hexdigest()[:24]}"


def _run_parents(
    events: list[ObservedEvent],
) -> dict[str, tuple[TraceStageKind, tuple[str, ...]]]:
    parents: dict[str, tuple[TraceStageKind, tuple[str, ...]]] = {}
    for observed in events:
        if observed.event.type != "run.created":
            continue
        run_id = _event_run_id(observed)
        if run_id is None:
            raise _ProjectionFailure
        parent = observed.event.payload.get("parent_run_id")
        workflow_id = observed.event.payload.get("workflow_run_id")
        node_id = observed.event.payload.get("workflow_node_id")
        if parent is not None:
            if not isinstance(parent, str) or not parent or run_id in parents:
                raise _ProjectionFailure
            parents[run_id] = (
                TraceStageKind.RUN,
                (_bounded_identifier(parent),),
            )
        elif isinstance(workflow_id, str) and isinstance(node_id, str):
            parents[run_id] = (
                TraceStageKind.WORKFLOW_NODE,
                (
                    _bounded_identifier(workflow_id),
                    _bounded_identifier(node_id),
                ),
            )
    return parents


def _context_parents(events: list[ObservedEvent]) -> dict[str, tuple[str, str]]:
    parents: dict[str, tuple[str, str]] = {}
    for observed in events:
        if observed.event.type != "model.call.started":
            continue
        payload = observed.event.payload
        view_id = payload.get("context_view_id")
        operation_id = payload.get("operation_id")
        run_id = observed.event.run_id
        if (
            not isinstance(view_id, str)
            or not view_id
            or not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(run_id, str)
            or not run_id
        ):
            continue
        parent = (_bounded_identifier(run_id), _bounded_identifier(operation_id))
        bounded_view = _bounded_identifier(view_id)
        if bounded_view in parents and parents[bounded_view] != parent:
            raise _ProjectionFailure
        parents[bounded_view] = parent
    return parents


def _parent_hint(
    kind: TraceStageKind,
    observed: ObservedEvent,
    run_parents: Mapping[str, tuple[TraceStageKind, tuple[str, ...]]],
    context_parents: Mapping[str, tuple[str, str]],
) -> tuple[TraceStageKind, tuple[str, ...]] | None:
    run_id = _event_run_id(observed, context_parents)
    payload = observed.event.payload
    if kind is TraceStageKind.RUN:
        return run_parents.get(run_id or "")
    if kind is TraceStageKind.CONTEXT:
        view_id = payload.get("view_id")
        context_parent = (
            context_parents.get(view_id) if isinstance(view_id, str) else None
        )
        if context_parent is not None:
            return (TraceStageKind.MODEL, (context_parent[1],))
        return None if run_id is None else (TraceStageKind.RUN, (run_id,))
    if kind in {TraceStageKind.STEP, TraceStageKind.MESSAGE, TraceStageKind.RECOVERY}:
        return None if run_id is None else (TraceStageKind.RUN, (run_id,))
    if kind in {TraceStageKind.MODEL, TraceStageKind.TOOL}:
        step_id = payload.get("step_id")
        if isinstance(step_id, str) and step_id:
            return (TraceStageKind.STEP, (_bounded_identifier(step_id),))
        return None if run_id is None else (TraceStageKind.RUN, (run_id,))
    if kind is TraceStageKind.PERMISSION:
        call_id = payload.get("call_id")
        if isinstance(call_id, str) and call_id:
            return (TraceStageKind.TOOL, (_bounded_identifier(call_id),))
        return None if run_id is None else (TraceStageKind.RUN, (run_id,))
    if kind is TraceStageKind.WORKFLOW_NODE:
        workflow_run_id = _identifier(observed, "workflow_run_id")
        return (TraceStageKind.WORKFLOW, (workflow_run_id,))
    if kind is TraceStageKind.CHILD:
        parent = payload.get("parent_run_id")
        if isinstance(parent, str) and parent:
            return (TraceStageKind.RUN, (_bounded_identifier(parent),))
    if kind is TraceStageKind.EVALUATION:
        subject = payload.get("subject_run_id")
        if isinstance(subject, str) and subject:
            return (TraceStageKind.RUN, (_bounded_identifier(subject),))
    return None


def _identifier(observed: ObservedEvent, field: str) -> str:
    payload = observed.event.payload
    value: object = payload.get(field)
    if value is None and field == "run_id":
        value = observed.event.run_id
    if value is None and field == "workflow_run_id" and observed.event.type.startswith("workflow."):
        value = observed.event.run_id
    if value is None and field == "sequence":
        value = observed.event.sequence
    if value is None and field == "request_id":
        request = payload.get("request")
        value = request.get("request_id") if isinstance(request, Mapping) else request
        if (
            value is None
            and observed.event.schema_version == 1
            and observed.event.type in {"permission.requested", "permission.resolved"}
            and _sha256_reference(payload.get("tool")) is not None
        ):
            value = _sha256_reference(request)
    if (
        value is None
        and field == "operation_id"
        and observed.event.schema_version == 1
        and observed.event.type == "tool.recovery.retry.started"
    ):
        operation = payload.get("operation")
        value = _sha256_reference(operation) or operation
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise _ProjectionFailure
    bounded = _bounded_identifier(str(value))
    if field == "workflow_run_id" and observed.event.type.startswith("workflow."):
        event_run_id = observed.event.run_id
        if not isinstance(event_run_id, str) or _bounded_identifier(event_run_id) != bounded:
            raise _ProjectionFailure
    return bounded


def _bounded_identifier(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 256:
        raise _ProjectionFailure
    return value


def _evidence(observed: ObservedEvent) -> tuple[tuple[str, ...], tuple[int, ...]]:
    if not is_public_evidence_id(observed.event.event_id):
        return (), ()
    return (observed.event.event_id,), (observed.cursor,)


def _sha256_reference(value: object) -> str | None:
    if not isinstance(value, Mapping) or set(value) != {"sha256"}:
        return None
    digest = value.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return None
    return digest


def _event_run_id(
    observed: ObservedEvent,
    context_parents: Mapping[str, tuple[str, str]] | None = None,
) -> str:
    if observed.event.type == "context.view.created" and context_parents is not None:
        view_id = observed.event.payload.get("view_id")
        parent = context_parents.get(view_id) if isinstance(view_id, str) else None
        if parent is not None:
            return parent[0]
        raise _ProjectionFailure
    if observed.event.type.startswith("workflow."):
        return _identifier(observed, "workflow_run_id")
    if observed.event.type.startswith("child."):
        return _identifier(observed, "child_run_id")
    if observed.event.type == "agent.message.sent":
        return _identifier(observed, "sender_run_id")
    if observed.event.type == "evaluation.completed":
        return _identifier(observed, "subject_run_id")
    value = observed.event.run_id
    if value is None:
        raise _ProjectionFailure
    return _bounded_identifier(value)


def _public_refs(observed: ObservedEvent) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rule = REFERENCE_RULES.get(observed.event.type)
    if rule is None:
        return (), ()
    payload = observed.event.payload
    inputs = [_optional_public_ref(observed, field) for field in rule.input_fields]
    outputs = [_optional_public_ref(observed, field) for field in rule.output_fields]
    for field in rule.input_sequence_fields:
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, (list, tuple)) or len(value) > PUBLIC_REF_MAX_ITEMS:
            raise _ProjectionFailure
        inputs.extend(_bounded_public_ref(item) for item in value)
    if rule.output_event and is_public_evidence_id(observed.event.event_id):
        outputs.append(observed.event.event_id)
    return (
        _deduplicate_refs(item for item in inputs if item is not None),
        _deduplicate_refs(item for item in outputs if item is not None),
    )


def _optional_public_ref(observed: ObservedEvent, field: str) -> str | None:
    value: object = observed.event.payload.get(field)
    if value is None and field == "run_id":
        value = observed.event.run_id
    if value is None:
        return None
    return _bounded_public_ref(value)


def _bounded_public_ref(value: object) -> str:
    if not isinstance(value, str):
        raise _ProjectionFailure
    if not value or len(value.encode("utf-8")) > PUBLIC_REF_MAX_BYTES:
        raise _ProjectionFailure
    return value


def _deduplicate_refs(values: Iterable[str]) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        selected.append(value)
        if len(selected) > PUBLIC_REF_MAX_ITEMS:
            raise _ProjectionFailure
    return tuple(selected)


def _merge_refs(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return _deduplicate_refs((*left, *right))


def _failure_fact(
    kind: TraceStageKind,
    status: TraceStageStatus,
    payload: Mapping[str, object],
) -> tuple[str | None, bool | None]:
    if status not in {
        TraceStageStatus.FAILED,
        TraceStageStatus.DENIED,
        TraceStageStatus.TIMED_OUT,
        TraceStageStatus.INTERRUPTED,
    }:
        return None, None
    structured: Mapping[str, object] | None = None
    for field in ("error", "failure"):
        candidate = payload.get(field)
        if isinstance(candidate, Mapping):
            structured = candidate
            break
    code: str | None = None
    retryable: bool | None = None
    if structured is not None:
        raw_code = structured.get("code")
        if raw_code is not None:
            code = _sanitized_error_code(raw_code)
        raw_retryable = structured.get("retryable")
        if raw_retryable is not None:
            if not isinstance(raw_retryable, bool):
                raise _ProjectionFailure
            retryable = raw_retryable
    if code is None:
        raw_code = payload.get("code")
        if raw_code is not None:
            code = _sanitized_error_code(raw_code)
    if code is None:
        raw_status = payload.get("status")
        if raw_status == "invalid_arguments":
            code = "invalid_arguments"
        elif status is TraceStageStatus.DENIED:
            code = "permission_denied"
        elif status is TraceStageStatus.TIMED_OUT:
            code = f"{kind.value}_timed_out"
        elif status is TraceStageStatus.INTERRUPTED:
            code = f"{kind.value}_interrupted"
        else:
            code = f"{kind.value}_failed"
    return code, retryable


def _sanitized_error_code(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 128
        or any(
            not (
                character.isascii()
                and (character.isalnum() or character in "._:-")
            )
            for character in value
        )
    ):
        raise _ProjectionFailure
    return value


def _usage(payload: Mapping[str, object]) -> TokenUsage | None:
    candidate: object = payload.get("usage", payload)
    if not isinstance(candidate, Mapping):
        return None
    fields = {"prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"}
    if not fields.intersection(candidate):
        return None
    return TokenUsage.model_validate({name: candidate.get(name) for name in fields})


def _effective_status(default: TraceStageStatus, payload: Mapping[str, object]) -> TraceStageStatus:
    raw = payload.get("status")
    if raw == "denied":
        return TraceStageStatus.DENIED
    if raw == "timed_out":
        return TraceStageStatus.TIMED_OUT
    if raw in {"failed", "invalid_arguments"}:
        return TraceStageStatus.FAILED
    if raw == "interrupted":
        return TraceStageStatus.INTERRUPTED
    allowed = payload.get("allowed")
    if allowed is False:
        return TraceStageStatus.DENIED
    decision = payload.get("decision")
    if isinstance(decision, Mapping) and decision.get("action") == "deny":
        return TraceStageStatus.DENIED
    return default


def _stage_id(kind: TraceStageKind, key: tuple[str, ...]) -> str:
    encoded = json.dumps([kind.value, *key], ensure_ascii=False, separators=(",", ":"))
    return f"stg_{kind.value}_{sha256(encoded.encode('utf-8')).hexdigest()[:24]}"
