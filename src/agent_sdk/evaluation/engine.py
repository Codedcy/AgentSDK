from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, NoReturn

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.ids import new_id
from agent_sdk.observability import ObservedEvent, RunTimeline
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot
from agent_sdk.storage.base import (
    CommitBatch,
    EventPrecondition,
    EventPreconditionConflictError,
    EventPreconditionNotFoundError,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    SnapshotWrite,
    StateStore,
    StoredEvent,
    canonical_snapshot_data,
)
from agent_sdk.storage.validation import validate_event_page, validate_latest_cursor

from .evaluators import Evaluator
from .models import (
    EvaluationDecision,
    EvaluationResult,
    EvaluationSubject,
    validate_metadata_string,
)

_PAGE_SIZE = 100
_STABLE_READ_ATTEMPTS = 4


class _Failure(Enum):
    FAILED = "failed"


class _CommitFailure(Enum):
    PRECONDITION = "precondition"
    EVENT_NOT_FOUND = "event_not_found"
    EVENT_CONFLICT = "event_conflict"
    VALUE = "value"
    FAILED = "failed"


@dataclass(frozen=True)
class _SubjectObservation:
    subject: EvaluationSubject
    session_data: dict[str, Any]
    run_data: dict[str, Any]


@dataclass(frozen=True)
class _ValidatedRun:
    snapshot: RunSnapshot
    data: dict[str, Any]


@dataclass(frozen=True)
class _ValidatedSession:
    snapshot: SessionSnapshot
    data: dict[str, Any]


@dataclass(frozen=True)
class _ExtensionResult:
    evaluator_id: str
    evaluator_version: str
    method: str
    decision: EvaluationDecision


class EvaluationEngine:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def evaluate(
        self,
        run_id: str,
        evaluator: Evaluator,
    ) -> EvaluationResult:
        observation = await self._load_subject(run_id)
        extension = await _invoke_evaluator(evaluator, observation.subject)
        evaluator = None  # type: ignore[assignment]
        if isinstance(extension, _Failure):
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "evaluator failed",
                retryable=False,
            )

        evaluation_id = new_id("evl")
        decision = extension.decision
        result = EvaluationResult(
            evaluation_id=evaluation_id,
            session_id=observation.subject.snapshot.session_id,
            subject_run_id=observation.subject.snapshot.run_id,
            evaluator_id=extension.evaluator_id,
            evaluator_version=extension.evaluator_version,
            method=extension.method,
            verdict=decision.verdict,
            metrics=decision.metrics,
            reason=decision.reason,
            confidence=decision.confidence,
            evidence_event_ids=decision.evidence_event_ids,
            created_at=datetime.now(UTC),
            subject_cursor=observation.subject.as_of_cursor,
        )
        data = result.model_dump(mode="json")
        event = EventEnvelope.new(
            type="evaluation.completed",
            session_id=result.session_id,
            run_id=evaluation_id,
            sequence=1,
            payload=data,
        )
        commit = await _commit(
            self._store,
            CommitBatch(
                events=(event,),
                snapshots=(
                    SnapshotWrite(
                        "evaluation",
                        evaluation_id,
                        result.session_id,
                        result.record_version,
                        data,
                    ),
                ),
                preconditions=(
                    SnapshotPrecondition(
                        "session",
                        result.session_id,
                        version=int(observation.session_data["version"]),
                        session_id=result.session_id,
                        data=observation.session_data,
                    ),
                    SnapshotPrecondition(
                        "run",
                        result.subject_run_id,
                        version=observation.subject.snapshot.version,
                        session_id=result.session_id,
                        data=observation.run_data,
                    ),
                ),
                event_preconditions=_evidence_preconditions(
                    observation.subject,
                    decision,
                ),
            ),
        )
        if commit is _CommitFailure.EVENT_NOT_FOUND:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "evaluation evidence no longer exists",
                retryable=False,
            )
        if commit is _CommitFailure.EVENT_CONFLICT:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "evaluation evidence changed",
                retryable=True,
            )
        if commit is _CommitFailure.PRECONDITION:
            await self._raise_changed_subject(observation)
        if commit is _CommitFailure.VALUE:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "evaluation id already exists",
                retryable=True,
            )
        if commit is _CommitFailure.FAILED:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "failed to persist evaluation",
                retryable=False,
            )
        return result

    async def _load_subject(self, run_id: str) -> _SubjectObservation:
        saw_change = False
        for _ in range(_STABLE_READ_ATTEMPTS):
            run_record = await _validated_run(self._store, run_id)
            if isinstance(run_record, _Failure):
                self._internal("failed to load evaluation subject")
            if run_record is None:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "run not found",
                    retryable=False,
                )
            run = run_record.snapshot
            run_data = run_record.data
            if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "run is not terminal",
                    retryable=False,
                )
            session_record = await _validated_session(self._store, run.session_id)
            if isinstance(session_record, _Failure):
                self._internal("failed to load evaluation subject")
            if session_record is None:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "run session not found",
                    retryable=False,
                )
            session = session_record.snapshot
            session_data = session_record.data
            if session.session_id != run.session_id:
                self._internal("failed to load evaluation subject")
            cursor = await _latest_cursor(self._store)
            if isinstance(cursor, _Failure):
                self._internal("failed to load evaluation subject")
            timeline = _subject_timeline(
                run,
                await _read_through(self._store, cursor),
                cursor,
            )
            if isinstance(timeline, _Failure):
                self._internal("failed to load evaluation subject")
            confirmed_run = await _validated_run(self._store, run_id)
            confirmed_session = await _validated_session(self._store, run.session_id)
            if (
                isinstance(confirmed_run, _Failure)
                or isinstance(confirmed_session, _Failure)
            ):
                self._internal("failed to load evaluation subject")
            if confirmed_run is None or confirmed_session is None:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "evaluation subject no longer exists",
                    retryable=False,
                )
            if (
                canonical_snapshot_data(confirmed_run.data)
                != canonical_snapshot_data(run_data)
                or canonical_snapshot_data(confirmed_session.data)
                != canonical_snapshot_data(session_data)
            ):
                saw_change = True
                continue
            subject = EvaluationSubject(
                snapshot=run,
                timeline=timeline,
                as_of_cursor=cursor,
            )
            return _SubjectObservation(
                subject=subject,
                session_data=session_data,
                run_data=run_data,
            )
        if saw_change:
            raise AgentSDKError(
                ErrorCode.CONFLICT,
                "run changed while preparing evaluation",
                retryable=True,
            )
        raise AssertionError("unreachable")

    async def _raise_changed_subject(
        self,
        observation: _SubjectObservation,
    ) -> None:
        session = await _validated_session(
            self._store,
            observation.subject.snapshot.session_id,
        )
        run = await _validated_run(
            self._store,
            observation.subject.snapshot.run_id,
        )
        if isinstance(session, _Failure) or isinstance(run, _Failure):
            self._internal("failed to verify evaluation subject")
        if session is None or run is None:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "evaluation subject no longer exists",
                retryable=False,
            )
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "evaluation subject changed",
            retryable=True,
        )

    @staticmethod
    def _internal(message: str) -> NoReturn:
        raise AgentSDKError(ErrorCode.INTERNAL, message, retryable=False)


async def _invoke_evaluator(
    evaluator: Evaluator,
    subject: EvaluationSubject,
) -> _ExtensionResult | _Failure:
    try:
        evaluator_id = _metadata(evaluator.id)
        version = _metadata(evaluator.version)
        method = _metadata(evaluator.method)
        raw_decision: object = await evaluator.evaluate(subject)
        if isinstance(raw_decision, EvaluationDecision):
            raw_decision = raw_decision.model_dump(mode="python", warnings="error")
        decision = EvaluationDecision.model_validate(raw_decision, strict=True)
        available_evidence = {
            item.event.event_id for item in subject.timeline.events
        }
        if not set(decision.evidence_event_ids).issubset(available_evidence):
            return _Failure.FAILED
        return _ExtensionResult(evaluator_id, version, method, decision)
    except Exception:
        return _Failure.FAILED


def _metadata(value: object) -> str:
    return validate_metadata_string(value)


async def _validated_run(
    store: StateStore,
    run_id: str,
) -> _ValidatedRun | None | _Failure:
    try:
        data = await store.get_snapshot("run", run_id)
        if data is None:
            return None
        return _ValidatedRun(snapshot=RunSnapshot.model_validate(data), data=data)
    except Exception:
        return _Failure.FAILED


async def _validated_session(
    store: StateStore,
    session_id: str,
) -> _ValidatedSession | None | _Failure:
    try:
        data = await store.get_snapshot("session", session_id)
        if data is None:
            return None
        if set(data) != set(SessionSnapshot.model_fields):
            return _Failure.FAILED
        return _ValidatedSession(
            snapshot=SessionSnapshot.model_validate(data),
            data=data,
        )
    except Exception:
        return _Failure.FAILED


async def _latest_cursor(store: StateStore) -> int | _Failure:
    try:
        return validate_latest_cursor(await store.latest_cursor())
    except Exception:
        return _Failure.FAILED


async def _read_through(
    store: StateStore,
    up_to_cursor: int,
) -> list[StoredEvent] | _Failure:
    events: list[StoredEvent] = []
    current = 0
    try:
        while current < up_to_cursor:
            page = validate_event_page(
                await store.read_events(
                    after_cursor=current,
                    up_to_cursor=up_to_cursor,
                    limit=_PAGE_SIZE,
                ),
                after_cursor=current,
                up_to_cursor=up_to_cursor,
                limit=_PAGE_SIZE,
            )
            if not page:
                break
            if page[0].cursor <= current or page[-1].cursor > up_to_cursor:
                return _Failure.FAILED
            events.extend(page)
            current = page[-1].cursor
        return events
    except Exception:
        return _Failure.FAILED


def _subject_timeline(
    run: RunSnapshot,
    stored_events: list[StoredEvent] | _Failure,
    cursor: int,
) -> RunTimeline | _Failure:
    try:
        if isinstance(stored_events, _Failure):
            return _Failure.FAILED
        selected: list[ObservedEvent] = []
        for stored in stored_events:
            if stored.event.run_id != run.run_id:
                continue
            if stored.event.session_id != run.session_id:
                return _Failure.FAILED
            selected.append(ObservedEvent(cursor=stored.cursor, event=stored.event))
        terminal_type = (
            "run.completed" if run.status is RunStatus.COMPLETED else "run.failed"
        )
        terminal = [item for item in selected if item.event.type == terminal_type]
        if (
            len(terminal) != 1
            or terminal[-1] != selected[-1]
            or terminal[0].event.schema_version != 1
        ):
            return _Failure.FAILED
        return RunTimeline(
            run_id=run.run_id,
            events=tuple(selected),
            as_of_cursor=cursor,
        )
    except Exception:
        return _Failure.FAILED


def _evidence_preconditions(
    subject: EvaluationSubject,
    decision: EvaluationDecision,
) -> tuple[EventPrecondition, ...]:
    required = set(decision.evidence_event_ids)
    required.add(subject.timeline.events[-1].event.event_id)
    return tuple(
        EventPrecondition(
            event_id=item.event.event_id,
            cursor=item.cursor,
            session_id=item.event.session_id,
            run_id=item.event.run_id,
            type=item.event.type,
            sequence=item.event.sequence,
        )
        for item in subject.timeline.events
        if item.event.event_id in required
    )


async def _commit(
    store: StateStore,
    batch: CommitBatch,
) -> None | _CommitFailure:
    try:
        await store.commit(batch)
        return None
    except EventPreconditionNotFoundError:
        return _CommitFailure.EVENT_NOT_FOUND
    except EventPreconditionConflictError:
        return _CommitFailure.EVENT_CONFLICT
    except SnapshotPreconditionError:
        return _CommitFailure.PRECONDITION
    except ValueError:
        return _CommitFailure.VALUE
    except Exception:
        return _CommitFailure.FAILED
