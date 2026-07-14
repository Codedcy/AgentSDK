import pytest
from pydantic import ValidationError

from agent_sdk import AgentSDKError, ErrorCode, SessionSnapshot, SessionStatus
from agent_sdk.runtime.state_machine import SessionStateMachine


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (SessionStatus.ACTIVE, SessionStatus.CLOSING),
        (SessionStatus.ACTIVE, SessionStatus.DELETING),
        (SessionStatus.CLOSING, SessionStatus.CLOSED),
        (SessionStatus.CLOSING, SessionStatus.DELETING),
        (SessionStatus.CLOSED, SessionStatus.DELETING),
    ],
)
def test_allowed_session_transition(source: SessionStatus, target: SessionStatus) -> None:
    assert SessionStateMachine.transition(source, target) is target


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source in SessionStatus
        for target in SessionStatus
        if (source, target)
        not in {
            (SessionStatus.ACTIVE, SessionStatus.CLOSING),
            (SessionStatus.ACTIVE, SessionStatus.DELETING),
            (SessionStatus.CLOSING, SessionStatus.CLOSED),
            (SessionStatus.CLOSING, SessionStatus.DELETING),
            (SessionStatus.CLOSED, SessionStatus.DELETING),
        }
    ],
)
def test_invalid_session_transition_has_stable_public_error(
    source: SessionStatus,
    target: SessionStatus,
) -> None:
    with pytest.raises(AgentSDKError) as raised:
        SessionStateMachine.transition(source, target)

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "session transition is invalid"
    assert raised.value.retryable is False


def test_active_to_closed_is_not_a_public_state_machine_transition() -> None:
    with pytest.raises(AgentSDKError) as raised:
        SessionStateMachine.transition(SessionStatus.ACTIVE, SessionStatus.CLOSED)

    assert raised.value.code is ErrorCode.INVALID_STATE


def test_closed_session_cannot_own_work() -> None:
    with pytest.raises(ValidationError):
        SessionSnapshot(
            session_id="ses_1",
            status="closed",
            workspaces=(),
            active_run_ids=("run_1",),
        )


@pytest.mark.parametrize("status", list(SessionStatus))
@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("active_run_ids", ("run_1", "run_1")),
        ("active_workflow_run_ids", ("workflow_1", "workflow_1")),
    ],
)
def test_session_rejects_duplicate_active_execution_ids(
    status: SessionStatus,
    field: str,
    values: tuple[str, str],
) -> None:
    with pytest.raises(ValidationError):
        SessionSnapshot(
            session_id="ses_1",
            status=status,
            workspaces=(),
            **{field: values},
        )


@pytest.mark.parametrize("version", [0, -1])
def test_session_version_must_be_positive(version: int) -> None:
    with pytest.raises(ValidationError):
        SessionSnapshot(session_id="ses_1", workspaces=(), version=version)


@pytest.mark.parametrize(
    "update",
    [
        {"version": 0},
        {"active_run_ids": ("run_1", "run_1")},
        {"status": SessionStatus.CLOSED, "active_run_ids": ("run_1",)},
    ],
)
def test_session_copy_revalidates_invariants(update: dict[str, object]) -> None:
    snapshot = SessionSnapshot(session_id="ses_1", workspaces=())

    with pytest.raises(ValidationError):
        snapshot.model_copy(update=update)
