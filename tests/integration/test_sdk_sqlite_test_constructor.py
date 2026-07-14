from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_sdk import AgentSDK, AgentSDKError, ErrorCode, EventFilter
from agent_sdk.api import _LazySQLiteStore
from agent_sdk.runtime.leases import LeaseManager
from agent_sdk.storage.memory import InMemoryStore


async def _unused_provider(**_: Any) -> object:
    raise AssertionError("reopen-only test must not call LiteLLM")


@pytest.mark.asyncio
async def test_for_test_database_path_is_owned_lazy_and_reopenable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "owned.db"
    sdk = AgentSDK.for_test(database_path=database, acompletion=_unused_provider)
    session = await sdk.sessions.create(workspaces=[])
    await sdk.close()
    await sdk.close()

    reopened = AgentSDK.for_test(database_path=database, acompletion=_unused_provider)
    result = await reopened.queries.query_events(
        EventFilter(session_id=session.session_id)
    )
    assert [item.event.type for item in result.events] == ["session.created"]
    await reopened.close()


@pytest.mark.asyncio
async def test_for_test_does_not_own_injected_store() -> None:
    store = InMemoryStore()
    sdk = AgentSDK.for_test(store=store, acompletion=_unused_provider)
    session = await sdk.sessions.create(workspaces=[])
    await sdk.close()

    reused = AgentSDK.for_test(store=store, acompletion=_unused_provider)
    result = await reused.queries.query_events(
        EventFilter(session_id=session.session_id)
    )
    assert [item.event.type for item in result.events] == ["session.created"]
    await reused.close()


@pytest.mark.asyncio
async def test_lazy_sqlite_store_delegates_lease_surface(tmp_path: Path) -> None:
    store = _LazySQLiteStore(tmp_path / "lazy-leases.db")
    manager = LeaseManager(store, ttl=timedelta(seconds=30))
    now = datetime(2026, 7, 14, tzinfo=UTC)
    try:
        lease = await manager.acquire("run_1", "coordinator_1", now=now)
        renewed = await manager.renew(lease, now=now + timedelta(seconds=1))
        await manager.assert_current(renewed, now=renewed.renewed_at)
        await manager.release(renewed)
    finally:
        await store.close()


@pytest.mark.parametrize("case", ("neither", "both"))
def test_for_test_requires_exactly_one_store_source(
    case: str,
    tmp_path: Path,
) -> None:
    kwargs: dict[str, object] = {"acompletion": _unused_provider}
    if case == "both":
        kwargs.update(store=InMemoryStore(), database_path=tmp_path / "bad.db")
    with pytest.raises(AgentSDKError) as captured:
        AgentSDK.for_test(**kwargs)  # type: ignore[arg-type]
    assert captured.value.code is ErrorCode.INVALID_STATE
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
