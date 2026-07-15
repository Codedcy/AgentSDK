from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.ids import new_id
from agent_sdk.runtime.leases import Lease, LeaseHeldError, LeaseManager
from agent_sdk.runtime.models import RunSnapshot, RunStatus, SessionSnapshot
from agent_sdk.runtime.reconciliation import RecoveryStateConflictError
from agent_sdk.runtime.session_lifecycle import (
    exact_run_precondition,
    exact_session_precondition,
)
from agent_sdk.storage.base import (
    CommitResult,
    RunProgressBatch,
    SnapshotWrite,
    StateStore,
)


_SCANNER_LEASE_TTL = timedelta(seconds=30)


class RecoveryScanner:
    def __init__(
        self,
        store: StateStore,
        *,
        lease_manager: LeaseManager | None = None,
        _clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._leases = lease_manager or LeaseManager(store, ttl=_SCANNER_LEASE_TTL)
        self._clock = _clock or (lambda: datetime.now(UTC))
        self._scan_lock = asyncio.Lock()

    async def scan(self) -> None:
        public_error: tuple[ErrorCode, str, bool] | None = None
        try:
            await self._scan_private()
            return
        except asyncio.CancelledError:
            raise
        except AgentSDKError as error:
            public_error = (error.code, error.message, error.retryable)
        except Exception:
            public_error = (
                ErrorCode.INTERNAL,
                "failed to scan abandoned runs",
                False,
            )
        del self
        assert public_error is not None
        raise AgentSDKError(
            public_error[0],
            public_error[1],
            retryable=public_error[2],
        ) from None

    async def _scan_private(self) -> None:
        async with self._scan_lock:
            now = self._clock()
            run_ids = await self._store.list_abandoned_run_ids(now=now)
            for run_id in run_ids:
                await self._scan_run(run_id, now=now)

    async def _scan_run(self, run_id: str, *, now: datetime) -> None:
        try:
            lease = await self._leases.acquire(
                run_id,
                new_id("coord"),
                now=now,
            )
        except LeaseHeldError:
            return
        try:
            await self._interrupt_if_still_abandoned(run_id, lease, now=now)
        finally:
            release = asyncio.create_task(self._leases.release(lease))
            cancellation = await _settle_task(release)
            if cancellation is not None:
                raise cancellation from None

    async def _interrupt_if_still_abandoned(
        self,
        run_id: str,
        lease: Lease,
        *,
        now: datetime,
    ) -> None:
        run_data = await self._store.get_snapshot("run", run_id)
        if run_data is None:
            return
        try:
            run = RunSnapshot.model_validate(run_data)
        except ValueError:
            raise RecoveryStateConflictError from None
        if run.run_id != run_id:
            raise RecoveryStateConflictError
        if run.status not in {
            RunStatus.RUNNING,
            RunStatus.WAITING_PERMISSION,
        }:
            return
        session_data = await self._store.get_snapshot("session", run.session_id)
        if session_data is None:
            return
        try:
            session = SessionSnapshot.model_validate(session_data)
        except ValueError:
            raise RecoveryStateConflictError from None
        if (
            session.session_id != run.session_id
            or run.run_id not in session.active_run_ids
        ):
            raise RecoveryStateConflictError
        sequence = await self._store.latest_run_event_sequence(run.run_id)
        interrupted = run.model_copy(
            update={
                "status": RunStatus.INTERRUPTED,
                "version": run.version + 1,
            }
        )
        event = EventEnvelope(
            event_id=new_id("evt"),
            type="run.interrupted",
            session_id=run.session_id,
            run_id=run.run_id,
            sequence=1 if sequence is None else sequence + 1,
            payload={"status": RunStatus.INTERRUPTED.value},
            occurred_at=now,
        )
        batch = RunProgressBatch(
            lease=lease,
            now=now,
            events=(event,),
            snapshots=(
                SnapshotWrite(
                    "run",
                    interrupted.run_id,
                    interrupted.session_id,
                    interrupted.version,
                    interrupted.model_dump(mode="json"),
                ),
            ),
            preconditions=(
                exact_session_precondition(session),
                exact_run_precondition(run),
            ),
        )
        try:
            await _commit_progress(self._store, batch)
        except RecoveryStateConflictError:
            return


async def _commit_progress(
    store: StateStore,
    batch: RunProgressBatch,
) -> CommitResult:
    first = asyncio.create_task(store.commit_run_progress(batch))
    try:
        return await asyncio.shield(first)
    except asyncio.CancelledError as cancellation:
        await _settle_task(first)
        if (
            first.done()
            and not first.cancelled()
            and first.exception() is not None
            and not isinstance(first.exception(), RecoveryStateConflictError)
        ):
            replay = asyncio.create_task(store.commit_run_progress(batch))
            await _settle_task(replay)
        raise cancellation from None
    except RecoveryStateConflictError:
        raise
    except Exception as first_error:
        del first_error

    replay = asyncio.create_task(store.commit_run_progress(batch))
    try:
        return await asyncio.shield(replay)
    except asyncio.CancelledError as cancellation:
        await _settle_task(replay)
        raise cancellation from None
    except RecoveryStateConflictError:
        raise
    except Exception as replay_error:
        del replay_error
    raise AgentSDKError(
        ErrorCode.INTERNAL,
        "failed to commit interrupted run",
        retryable=False,
    ) from None


async def _settle_task(
    task: asyncio.Task[Any],
) -> asyncio.CancelledError | None:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.done() and task.cancelled():
                break
            if cancellation is None:
                cancellation = error
        except Exception:
            break
    if task.done() and not task.cancelled():
        task.exception()
    return cancellation
