from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_sdk.ids import new_id


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    schema_version: int = 1
    type: str
    session_id: str
    run_id: str | None
    sequence: int
    payload: dict[str, Any]
    occurred_at: datetime

    @classmethod
    def new(cls, **values: Any) -> "EventEnvelope":
        return cls(event_id=new_id("evt"), occurred_at=datetime.now(UTC), **values)
