from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_sdk.errors import AgentSDKError, ErrorCode


class LeaseHeldError(AgentSDKError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.CONFLICT, "run lease is held", retryable=True)


class LeaseLostError(AgentSDKError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.CONFLICT, "run lease is no longer current", retryable=False)


class Lease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    owner: str
    generation: int = Field(ge=1)
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime

    @field_validator("run_id", "owner")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("lease identity must be nonempty")
        return value

    @field_validator("acquired_at", "renewed_at", "expires_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _validate_order(self) -> Self:
        if self.renewed_at < self.acquired_at or self.expires_at <= self.renewed_at:
            raise ValueError("lease timestamps are out of order")
        return self


class LeaseStore(Protocol):
    async def acquire_lease(
        self, *, run_id: str, owner: str, now: datetime, expires_at: datetime
    ) -> Lease: ...

    async def renew_lease(
        self, lease: Lease, *, now: datetime, expires_at: datetime
    ) -> Lease: ...

    async def release_lease(self, lease: Lease) -> None: ...

    async def assert_current_lease(self, lease: Lease, *, now: datetime) -> None: ...


class LeaseManager:
    def __init__(self, store: LeaseStore, *, ttl: timedelta) -> None:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        self._store = store
        self._ttl = ttl

    async def acquire(
        self, run_id: str, owner: str, *, now: datetime | None = None
    ) -> Lease:
        acquired_at = _utc_now(now)
        return await self._store.acquire_lease(
            run_id=run_id,
            owner=owner,
            now=acquired_at,
            expires_at=acquired_at + self._ttl,
        )

    async def renew(self, lease: Lease, *, now: datetime | None = None) -> Lease:
        renewed_at = _utc_now(now)
        return await self._store.renew_lease(
            lease, now=renewed_at, expires_at=renewed_at + self._ttl
        )

    async def release(self, lease: Lease) -> None:
        await self._store.release_lease(lease)

    async def assert_current(
        self, lease: Lease, *, now: datetime | None = None
    ) -> None:
        await self._store.assert_current_lease(lease, now=_utc_now(now))


def _utc_now(value: datetime | None) -> datetime:
    candidate = datetime.now(UTC) if value is None else value
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise ValueError("lease timestamps must be timezone-aware")
    return candidate.astimezone(UTC)
