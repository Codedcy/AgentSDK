from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, cast

from agent_sdk.runtime.models import RunStatus

from .models import (
    AttributionContributor,
    AttributionSummary,
    FailureAttribution,
    ImprovementHint,
    ImprovementHintCode,
    ObservedEvent,
    TraceStage,
    TraceStageKind,
    TraceStageStatus,
    TraceTimeline,
    is_public_evidence_id,
)


_FAILING_STATUSES = frozenset(
    {
        TraceStageStatus.FAILED,
        TraceStageStatus.DENIED,
        TraceStageStatus.TIMED_OUT,
        TraceStageStatus.INTERRUPTED,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        TraceStageStatus.COMPLETED,
        TraceStageStatus.FAILED,
        TraceStageStatus.DENIED,
        TraceStageStatus.TIMED_OUT,
        TraceStageStatus.INTERRUPTED,
    }
)
_TOOL_FAILURE_STATUSES = frozenset({"denied", "failed", "invalid_arguments", "timed_out"})
_HINT_SUMMARIES: Mapping[ImprovementHintCode, str] = {
    "repeated_tool_failure": "The same Tool failed more than once.",
    "unused_tool_output": "A successful Tool output was not used by a later Context View.",
    "context_fallback": "Context compaction fell back to a lower level.",
    "workflow_loop_limit": "A Workflow reached its configured loop limit.",
    "child_failure": "A Child Run failed during the execution tree.",
    "permission_denied": "A Tool permission request was denied.",
    "interrupted_external_work": "External work was interrupted before its outcome was known.",
}
_HINT_ORDER: tuple[ImprovementHintCode, ...] = (
    "repeated_tool_failure",
    "unused_tool_output",
    "context_fallback",
    "workflow_loop_limit",
    "child_failure",
    "permission_denied",
    "interrupted_external_work",
)


@dataclass(frozen=True)
class _ContextFact:
    view_id: str
    cursor: int
    owner_run_id: str | None
    refs: frozenset[str]
    evidence_id: str


@dataclass(frozen=True)
class _ContributorFact:
    first_cursor: int
    contributor: AttributionContributor


@dataclass(frozen=True)
class _AttributionIndexes:
    context_cursor_by_consumer_ref: Mapping[tuple[str, str], int]
    messages_by_route: Mapping[tuple[str, str], tuple[tuple[str, int], ...]]
    last_model_stage_id_by_run: Mapping[str, str]
    completed_run_ids: frozenset[str]


def project_attribution(
    *,
    root_run_id: str,
    terminal_status: RunStatus,
    timeline: TraceTimeline,
    events: tuple[ObservedEvent, ...],
) -> AttributionSummary:
    ordered = tuple(sorted(events, key=lambda item: item.cursor))
    cursor_by_id = {item.event.event_id: item.cursor for item in ordered}
    event_by_id = {item.event.event_id: item for item in ordered}
    event_by_cursor = {item.cursor: item for item in ordered}
    contexts = _context_facts(ordered)
    indexes = _attribution_indexes(ordered, contexts, timeline.stages)
    manifest_evidence = _manifest_evidence_by_view(ordered)
    child_parents = _child_parents(ordered)
    failure_stage = _failure_stage(root_run_id, terminal_status, timeline.stages)
    failure = (
        None
        if failure_stage is None
        else _failure_attribution(failure_stage, event_by_cursor, cursor_by_id)
    )

    contributors: list[_ContributorFact] = []
    tool_terminals: list[tuple[TraceStage, ObservedEvent]] = []
    unused_successful_tools: list[str] = []
    evaluations: list[tuple[int, str]] = []

    for stage in timeline.stages:
        evidence_ids = _sorted_evidence(stage.evidence_event_ids, cursor_by_id)
        if stage.kind is TraceStageKind.MODEL:
            model_disposition = _model_disposition(
                stage,
                indexes=indexes,
            )
            contributors.append(
                _ContributorFact(
                    stage.first_cursor,
                    AttributionContributor(
                        kind="model",
                        entity_id=stage.entity_id,
                        status=stage.status.value,
                        disposition=model_disposition,
                        evidence_ids=evidence_ids,
                    ),
                )
            )
            continue
        if stage.kind is TraceStageKind.TOOL:
            terminal = _terminal_event(stage, event_by_cursor)
            if terminal is None:
                tool_disposition: Literal[
                    "consumed", "unused", "terminal", "supporting"
                ] = (
                    "supporting"
                )
            else:
                tool_terminals.append((stage, terminal))
                tool_disposition = (
                    "consumed"
                    if _tool_is_consumed(stage, terminal, indexes=indexes)
                    else "unused"
                )
                raw_status = terminal.event.payload.get("status")
                if (
                    tool_disposition == "unused"
                    and stage.status is TraceStageStatus.COMPLETED
                    and raw_status in {None, "completed", "succeeded"}
                ):
                    unused_successful_tools.append(terminal.event.event_id)
            contributors.append(
                _ContributorFact(
                    stage.first_cursor,
                    AttributionContributor(
                        kind="tool",
                        entity_id=stage.entity_id,
                        status=stage.status.value,
                        disposition=tool_disposition,
                        evidence_ids=evidence_ids,
                    ),
                )
            )
            continue
        if stage.kind is TraceStageKind.CONTEXT:
            context_evidence = _sorted_evidence(
                (*evidence_ids, *manifest_evidence.get(stage.entity_id, ())),
                cursor_by_id,
            )
            contributors.append(
                _ContributorFact(
                    stage.first_cursor,
                    AttributionContributor(
                        kind="context",
                        entity_id=stage.entity_id,
                        status=stage.status.value,
                        disposition=(
                            "terminal"
                            if failure_stage is not None
                            and stage.stage_id == failure_stage.stage_id
                            else "supporting"
                        ),
                        evidence_ids=context_evidence,
                    ),
                )
            )
            continue
        if stage.kind in {TraceStageKind.WORKFLOW, TraceStageKind.WORKFLOW_NODE}:
            contributors.append(
                _ContributorFact(
                    stage.first_cursor,
                    AttributionContributor(
                        kind="workflow",
                        entity_id=stage.entity_id,
                        status=stage.status.value,
                        disposition=(
                            "terminal"
                            if failure_stage is not None
                            and stage.stage_id == failure_stage.stage_id
                            else "supporting"
                        ),
                        evidence_ids=evidence_ids,
                    ),
                )
            )
            continue
        if stage.kind is TraceStageKind.CHILD:
            contributors.append(
                _ContributorFact(
                    stage.first_cursor,
                    AttributionContributor(
                        kind="child",
                        entity_id=stage.entity_id,
                        status=stage.status.value,
                        disposition=(
                            "consumed"
                            if _child_is_consumed(
                                stage,
                                child_parents=child_parents,
                                indexes=indexes,
                            )
                            else "unused"
                        ),
                        evidence_ids=evidence_ids,
                    ),
                )
            )
            continue
        if stage.kind is TraceStageKind.EVALUATION:
            evaluation = _terminal_event(stage, event_by_cursor)
            verdict = "unknown"
            referenced: tuple[str, ...] = ()
            if evaluation is not None:
                raw_verdict = evaluation.event.payload.get("verdict")
                if raw_verdict in {"pass", "fail", "unknown"}:
                    verdict = cast(str, raw_verdict)
                referenced = _strings(
                    evaluation.event.payload.get("evidence_event_ids")
                )
            combined_evidence = _sorted_evidence(
                (*evidence_ids, *referenced),
                cursor_by_id,
            )
            contributors.append(
                _ContributorFact(
                    stage.first_cursor,
                    AttributionContributor(
                        kind="evaluation",
                        entity_id=stage.entity_id,
                        status=verdict,
                        disposition="supporting",
                        evidence_ids=combined_evidence,
                    ),
                )
            )
            evaluations.append((stage.first_cursor, stage.entity_id))

    hint_evidence: dict[ImprovementHintCode, set[str]] = {}
    repeated_tool_evidence = _repeated_tool_failure_evidence(tool_terminals)
    if repeated_tool_evidence:
        hint_evidence["repeated_tool_failure"] = repeated_tool_evidence
    if unused_successful_tools:
        hint_evidence["unused_tool_output"] = set(unused_successful_tools)
    fallback_evidence = {
        context.evidence_id
        for context in contexts
        if _context_fallback(event_by_id.get(context.evidence_id))
    }
    if fallback_evidence:
        hint_evidence["context_fallback"] = fallback_evidence
    loop_evidence = {
        item.event.event_id
        for item in ordered
        if _failure_code(item.event.payload) == "workflow_loop_limit"
    }
    if loop_evidence:
        hint_evidence["workflow_loop_limit"] = loop_evidence
    child_failure_evidence = {
        evidence_id
        for stage in timeline.stages
        if stage.kind is TraceStageKind.CHILD and stage.status in _FAILING_STATUSES
        for evidence_id in stage.evidence_event_ids
    }
    if child_failure_evidence:
        hint_evidence["child_failure"] = child_failure_evidence
    permission_evidence = {
        evidence_id
        for stage in timeline.stages
        if stage.kind is TraceStageKind.PERMISSION
        and stage.status is TraceStageStatus.DENIED
        for evidence_id in stage.evidence_event_ids
    }
    if permission_evidence:
        hint_evidence["permission_denied"] = permission_evidence
    interrupted_evidence = _interrupted_external_evidence(timeline.stages, ordered)
    if interrupted_evidence:
        hint_evidence["interrupted_external_work"] = interrupted_evidence

    hints = tuple(
        ImprovementHint(
            code=code,
            summary=_HINT_SUMMARIES[code],
            evidence_ids=_sorted_evidence(hint_evidence[code], cursor_by_id),
        )
        for code in _HINT_ORDER
        if code in hint_evidence
    )
    ordered_contributors = tuple(
        item.contributor
        for item in sorted(
            contributors,
            key=lambda item: (item.first_cursor, item.contributor.entity_id),
        )
    )
    return AttributionSummary(
        root_run_id=root_run_id,
        terminal_status=terminal_status,
        failure=failure,
        contributors=ordered_contributors,
        evaluation_ids=tuple(
            entity_id for _, entity_id in sorted(evaluations, key=lambda item: item[0])
        ),
        hints=hints,
        as_of_cursor=timeline.as_of_cursor,
    )


def _context_facts(events: tuple[ObservedEvent, ...]) -> tuple[_ContextFact, ...]:
    owners: dict[str, str] = {}
    for item in events:
        if item.event.type != "model.call.started" or item.event.run_id is None:
            continue
        view_id = item.event.payload.get("context_view_id")
        if isinstance(view_id, str) and view_id:
            owners[view_id] = item.event.run_id
    result: list[_ContextFact] = []
    for item in events:
        if item.event.type != "context.view.created":
            continue
        view_id = item.event.payload.get("view_id")
        if not isinstance(view_id, str) or not view_id:
            continue
        refs = frozenset(
            (
                *_strings(item.event.payload.get("source_refs")),
                *_strings(item.event.payload.get("message_refs")),
                *_strings(item.event.payload.get("consumed_message_ids")),
            )
        )
        result.append(
            _ContextFact(
                view_id=view_id,
                cursor=item.cursor,
                owner_run_id=owners.get(view_id),
                refs=refs,
                evidence_id=item.event.event_id,
            )
        )
    return tuple(result)


def _attribution_indexes(
    events: tuple[ObservedEvent, ...],
    contexts: tuple[_ContextFact, ...],
    stages: tuple[TraceStage, ...],
) -> _AttributionIndexes:
    context_cursors: dict[tuple[str, str], int] = {}
    for context in contexts:
        if context.owner_run_id is None:
            continue
        for ref in context.refs:
            key = (context.owner_run_id, ref)
            context_cursors[key] = max(context.cursor, context_cursors.get(key, 0))

    messages: dict[tuple[str, str], list[tuple[str, int]]] = {}
    completed_run_ids: set[str] = set()
    for item in events:
        event = item.event
        if event.type == "run.completed" and event.run_id is not None:
            completed_run_ids.add(event.run_id)
        if event.type != "agent.message.sent":
            continue
        message_id = event.payload.get("message_id")
        sender_run_id = event.payload.get("sender_run_id")
        recipient_run_id = event.payload.get("recipient_run_id")
        if not all(
            isinstance(value, str) and value
            for value in (message_id, sender_run_id, recipient_run_id)
        ):
            continue
        route = (cast(str, sender_run_id), cast(str, recipient_run_id))
        messages.setdefault(route, []).append((cast(str, message_id), item.cursor))

    last_models: dict[str, TraceStage] = {}
    for stage in stages:
        if stage.kind is not TraceStageKind.MODEL or stage.run_id is None:
            continue
        current = last_models.get(stage.run_id)
        if current is None or (stage.first_cursor, stage.last_cursor) > (
            current.first_cursor,
            current.last_cursor,
        ):
            last_models[stage.run_id] = stage
    return _AttributionIndexes(
        context_cursor_by_consumer_ref=context_cursors,
        messages_by_route={route: tuple(items) for route, items in messages.items()},
        last_model_stage_id_by_run={
            run_id: stage.stage_id for run_id, stage in last_models.items()
        },
        completed_run_ids=frozenset(completed_run_ids),
    )


def _child_parents(events: tuple[ObservedEvent, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in events:
        if item.event.type == "run.created" and item.event.run_id is not None:
            parent_id = item.event.payload.get("parent_run_id")
            if isinstance(parent_id, str) and parent_id:
                result[item.event.run_id] = parent_id
        elif item.event.type == "child.created":
            child_id = item.event.payload.get("child_run_id")
            parent_id = item.event.payload.get("parent_run_id")
            if isinstance(child_id, str) and isinstance(parent_id, str):
                result[child_id] = parent_id
    return result


def _manifest_evidence_by_view(
    events: tuple[ObservedEvent, ...],
) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for item in events:
        if item.event.type != "prompt.manifest.created":
            continue
        view_id = item.event.payload.get("context_view_id")
        manifest_id = item.event.payload.get("manifest_id")
        if (
            isinstance(view_id, str)
            and view_id
            and isinstance(manifest_id, str)
            and manifest_id == item.event.run_id
        ):
            result.setdefault(view_id, []).append(item.event.event_id)
    return {view_id: tuple(evidence) for view_id, evidence in result.items()}


def _failure_stage(
    root_run_id: str,
    terminal_status: RunStatus,
    stages: tuple[TraceStage, ...],
) -> TraceStage | None:
    if terminal_status is RunStatus.COMPLETED:
        return None
    candidates = tuple(stage for stage in stages if stage.status in _FAILING_STATUSES)
    if candidates:
        return min(candidates, key=lambda stage: (stage.last_cursor, stage.first_cursor))
    root = tuple(
        stage
        for stage in stages
        if stage.kind is TraceStageKind.RUN and stage.entity_id == root_run_id
    )
    return None if not root else max(root, key=lambda stage: stage.last_cursor)


def _failure_attribution(
    stage: TraceStage,
    event_by_cursor: Mapping[int, ObservedEvent],
    cursor_by_id: Mapping[str, int],
) -> FailureAttribution:
    terminal = _terminal_event(stage, event_by_cursor)
    code = None if terminal is None else _failure_code(terminal.event.payload)
    retryable = False
    if terminal is not None:
        failure = terminal.event.payload.get("error")
        if not isinstance(failure, Mapping):
            failure = terminal.event.payload.get("failure")
        if isinstance(failure, Mapping) and isinstance(failure.get("retryable"), bool):
            retryable = cast(bool, failure["retryable"])
    return FailureAttribution(
        stage_id=stage.stage_id,
        stage_kind=stage.kind,
        code=code or _default_failure_code(stage),
        retryable=retryable,
        evidence_ids=_sorted_evidence(stage.evidence_event_ids, cursor_by_id),
    )


def _default_failure_code(stage: TraceStage) -> str:
    if stage.status is TraceStageStatus.DENIED:
        return "permission_denied"
    if stage.status is TraceStageStatus.TIMED_OUT:
        return f"{stage.kind.value}_timed_out"
    if stage.status is TraceStageStatus.INTERRUPTED:
        return f"{stage.kind.value}_interrupted"
    return f"{stage.kind.value}_failed"


def _failure_code(payload: Mapping[str, object]) -> str | None:
    for key in ("error", "failure"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            code = value.get("code")
            if isinstance(code, str) and code:
                return code
    code = payload.get("code")
    return code if isinstance(code, str) and code else None


def _model_disposition(
    stage: TraceStage,
    *,
    indexes: _AttributionIndexes,
) -> Literal["consumed", "unused", "terminal", "supporting"]:
    if stage.status is not TraceStageStatus.COMPLETED:
        return "supporting"
    if stage.run_id is None:
        return "supporting"
    if indexes.last_model_stage_id_by_run.get(stage.run_id) != stage.stage_id:
        return "consumed"
    if stage.run_id in indexes.completed_run_ids:
        return "terminal"
    return "supporting"


def _tool_is_consumed(
    stage: TraceStage,
    terminal: ObservedEvent,
    *,
    indexes: _AttributionIndexes,
) -> bool:
    if stage.run_id is None:
        return False
    result_refs = {terminal.event.event_id}
    for key in ("result_id", "result_ref"):
        value = terminal.event.payload.get(key)
        if isinstance(value, str) and value:
            result_refs.add(value)
    return any(
        indexes.context_cursor_by_consumer_ref.get((stage.run_id, ref), 0)
        > terminal.cursor
        for ref in result_refs
    )


def _child_is_consumed(
    stage: TraceStage,
    *,
    child_parents: Mapping[str, str],
    indexes: _AttributionIndexes,
) -> bool:
    parent_id = child_parents.get(stage.entity_id)
    if parent_id is None:
        return False
    result_cursor = indexes.context_cursor_by_consumer_ref.get(
        (parent_id, stage.entity_id),
        0,
    )
    if result_cursor > stage.last_cursor:
        return True
    return any(
        indexes.context_cursor_by_consumer_ref.get((parent_id, message_id), 0)
        > message_cursor
        for message_id, message_cursor in indexes.messages_by_route.get(
            (stage.entity_id, parent_id),
            (),
        )
    )


def _repeated_tool_failure_evidence(
    terminals: list[tuple[TraceStage, ObservedEvent]],
) -> set[str]:
    failures: list[tuple[str, str]] = []
    for stage, terminal in terminals:
        raw_status = terminal.event.payload.get("status")
        if stage.status not in _FAILING_STATUSES and raw_status not in _TOOL_FAILURE_STATUSES:
            continue
        tool_name = terminal.event.payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            failures.append((tool_name, terminal.event.event_id))
    counts = Counter(tool_name for tool_name, _ in failures)
    return {
        evidence_id
        for tool_name, evidence_id in failures
        if counts[tool_name] >= 2
    }


def _context_fallback(item: ObservedEvent | None) -> bool:
    if item is None:
        return False
    fallback_from = item.event.payload.get("fallback_from")
    applied_level = item.event.payload.get("applied_level")
    return fallback_from in {"L3", "L4"} and applied_level == "L2"


def _interrupted_external_evidence(
    stages: tuple[TraceStage, ...],
    events: tuple[ObservedEvent, ...],
) -> set[str]:
    running_by_run: dict[str, list[TraceStage]] = {}
    for stage in stages:
        if (
            stage.run_id is not None
            and stage.kind in {TraceStageKind.MODEL, TraceStageKind.TOOL}
            and stage.status is TraceStageStatus.RUNNING
        ):
            running_by_run.setdefault(stage.run_id, []).append(stage)
    interruptions = tuple(
        item for item in events if item.event.type == "run.interrupted"
    )
    result: set[str] = set()
    for interruption in interruptions:
        open_external = tuple(
            stage
            for stage in running_by_run.get(interruption.event.run_id or "", ())
            if stage.first_cursor < interruption.cursor
        )
        if open_external:
            result.add(interruption.event.event_id)
            for stage in open_external:
                result.update(stage.evidence_event_ids)
    return result


def _terminal_event(
    stage: TraceStage,
    event_by_cursor: Mapping[int, ObservedEvent],
) -> ObservedEvent | None:
    if stage.status not in _TERMINAL_STATUSES:
        return None
    return event_by_cursor.get(stage.last_cursor)


def _sorted_evidence(
    evidence_ids: Iterable[str],
    cursor_by_id: Mapping[str, int],
) -> tuple[str, ...]:
    unique = {
        item
        for item in evidence_ids
        if item in cursor_by_id and is_public_evidence_id(item)
    }
    return tuple(sorted(unique, key=lambda item: cursor_by_id[item]))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)
