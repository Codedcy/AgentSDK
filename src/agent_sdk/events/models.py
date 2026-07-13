from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer

from agent_sdk.ids import new_id


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


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

    @field_serializer("payload")
    def _serialize_payload(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return {str(key): _plain_json(item) for key, item in value.items()}

    @classmethod
    def new(cls, **values: Any) -> "EventEnvelope":
        return cls(event_id=new_id("evt"), occurred_at=datetime.now(UTC), **values)
