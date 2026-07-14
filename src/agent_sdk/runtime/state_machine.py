from collections.abc import Mapping

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.runtime.models import SessionStatus

_ALLOWED: Mapping[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.ACTIVE: frozenset(
        {SessionStatus.CLOSING, SessionStatus.DELETING}
    ),
    SessionStatus.CLOSING: frozenset(
        {SessionStatus.CLOSED, SessionStatus.DELETING}
    ),
    SessionStatus.CLOSED: frozenset({SessionStatus.DELETING}),
    SessionStatus.DELETING: frozenset(),
}


class SessionStateMachine:
    @staticmethod
    def transition(source: SessionStatus, target: SessionStatus) -> SessionStatus:
        if target not in _ALLOWED[source]:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "session transition is invalid",
                retryable=False,
            )
        return target
