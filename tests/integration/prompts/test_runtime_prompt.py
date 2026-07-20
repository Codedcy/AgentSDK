from __future__ import annotations

import json
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path

import pytest

from agent_sdk import AgentSDK, ContextRuntimeConfig, PromptManifestPersistence
from agent_sdk.context import CompactionLevel, ContextView
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.prompts import PromptComposer
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.models import AgentSpec
from agent_sdk.skills import SkillRegistry
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore


def _skill_root() -> Path:
    return Path(__file__).parents[2] / "fixtures" / "skills"


async def _unused_provider(**_: object) -> AsyncIterator[dict[str, object]]:
    raise AssertionError("provider must not be called")


@pytest.mark.asyncio
async def test_runtime_prompt_orders_layers_and_persists_manifest_by_reference() -> None:
    store = InMemoryStore()
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_unused_provider,
        skill_roots=(_skill_root(),),
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        view = ContextView(
            view_id="view_runtime_prompt",
            session_id=session.session_id,
            message_refs=("evt_user",),
            capsule_id=None,
            estimated_tokens=10,
            recommended_level=CompactionLevel.L0,
            applied_level=CompactionLevel.L0,
        )
        await store.commit(
            CommitBatch(
                events=(),
                snapshots=(
                    SnapshotWrite(
                        "context_view",
                        view.view_id,
                        session.session_id,
                        1,
                        view.model_dump(mode="json"),
                    ),
                )
            )
        )
        spec = AgentSpec(
            name="coding-agent",
            model="test/model",
            prompt_profile="coding",
            system_prompt="Application constraint.",
            skills=("coding-demo",),
        )
        activated = tuple(sdk.skills.activate(name) for name in spec.skills)
        tools = (
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object"},
                },
            },
        )

        built = PromptComposer().compose(
            profile=spec.prompt_profile,
            application=spec.system_prompt,
            skills=activated,
            context_view=view,
            model=spec.model,
            tools=tools,
        )
        context_messages = ({"role": "user", "content": "Current request."},)
        provider_messages = (*built.messages, *context_messages)
        await PromptManifestPersistence(store).persist(
            built.manifest,
            session_id=session.session_id,
        )

        assert tuple(layer.layer_id for layer in built.manifest.layers) == (
            "profile:general",
            "profile:coding",
            "application",
            "skill:coding-demo",
        )
        assert tuple(message["content"] for message in provider_messages[-2:]) == (
            activated[0].instructions,
            "Current request.",
        )
        assert built.manifest.layers[-1].version == activated[0].metadata.content_hash
        assert built.manifest.layers[-1].sha256 == sha256(
            activated[0].instructions.encode("utf-8")
        ).hexdigest()
        assert built.manifest.context_view_id == view.view_id
        assert built.manifest.model == spec.model
        assert built.manifest.tools_sha256

        snapshot = await store.get_snapshot(
            "prompt_manifest",
            built.manifest.manifest_id,
        )
        assert snapshot == built.manifest.model_dump(mode="json")
        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        created = next(
            item.event
            for item in events
            if item.event.type == "prompt.manifest.created"
        )
        assert created.payload == {
            "manifest_id": built.manifest.manifest_id,
            "context_view_id": view.view_id,
            "sha256": built.manifest.sha256,
            "model": spec.model,
            "tools_sha256": built.manifest.tools_sha256,
            "layers": [
                {"layer_id": layer.layer_id, "sha256": layer.sha256}
                for layer in built.manifest.layers
            ],
        }
        public_payload = json.dumps(created.payload, sort_keys=True)
        for raw_text in (
            "Application constraint.",
            activated[0].instructions,
            built.messages[0]["content"],
        ):
            assert raw_text not in public_payload
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_sdk_discovers_skills_once_and_missing_skill_blocks_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    discovery_calls = 0
    original_discover = SkillRegistry.discover

    def discover(registry: SkillRegistry) -> object:
        nonlocal discovery_calls
        discovery_calls += 1
        return original_discover(registry)

    monkeypatch.setattr(SkillRegistry, "discover", discover)

    async def provider(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        calls.append(kwargs)
        raise AssertionError("provider must not be called")

    store = InMemoryStore()
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=provider,
        skill_roots=(_skill_root(),),
    )
    try:
        assert discovery_calls == 1
        assert sdk.skills.activate("coding-demo").metadata.name == "coding-demo"
        session = await sdk.sessions.create(workspaces=[])

        with pytest.raises(AgentSDKError) as raised:
            await sdk.runs.start(
                session.session_id,
                AgentSpec(
                    name="coding-agent",
                    model="test/model",
                    skills=("missing-skill",),
                ),
                "Do work.",
            )

        assert raised.value.code is ErrorCode.INVALID_STATE
        assert raised.value.message == "configured agent skill unavailable"
        assert calls == []
        assert discovery_calls == 1
        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        assert all(item.event.type != "run.created" for item in events)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_prompt_manifest_survives_sqlite_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "prompt.db"
    store = await SQLiteStore.open(database_path)
    manifest_id = ""
    try:
        session = await RuntimeCommands(store).create_session(workspaces=[])
        view = ContextView(
            view_id="view_sqlite_prompt",
            session_id=session.session_id,
            message_refs=(),
            capsule_id=None,
            estimated_tokens=0,
        )
        await store.commit(
            CommitBatch(
                events=(),
                snapshots=(
                    SnapshotWrite(
                        "context_view",
                        view.view_id,
                        session.session_id,
                        1,
                        view.model_dump(mode="json"),
                    ),
                ),
            )
        )
        built = PromptComposer().compose(
            profile="general",
            context_view=view,
            model="test/model",
        )
        manifest_id = built.manifest.manifest_id
        await PromptManifestPersistence(store).persist(
            built.manifest,
            session_id=session.session_id,
        )
    finally:
        await store.close()

    reopened = await SQLiteStore.open(database_path)
    try:
        snapshot = await reopened.get_snapshot("prompt_manifest", manifest_id)
        assert snapshot is not None
        assert snapshot["manifest_id"] == manifest_id
        assert ContextRuntimeConfig().model_window == 128_000
    finally:
        await reopened.close()
