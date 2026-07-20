from __future__ import annotations

import json
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path

import pytest

from agent_sdk import (
    AgentNode,
    AgentSDK,
    ContextRuntimeConfig,
    PromptManifestPersistence,
    ToolSpec,
    WorkflowIR,
)
from agent_sdk.context import CompactionLevel, ContextView
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.models.litellm_gateway import LiteLLMGateway
from agent_sdk.prompts import PromptComposer
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.models import AgentSpec
from agent_sdk.skills import SkillRegistry
from agent_sdk.storage.base import CommitBatch, SnapshotWrite
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.subagents import SubagentService, TaskEnvelope


def _skill_root() -> Path:
    return Path(__file__).parents[2] / "fixtures" / "skills"


async def _unused_provider(**_: object) -> AsyncIterator[dict[str, object]]:
    raise AssertionError("provider must not be called")


async def _successful_provider(**_: object) -> AsyncIterator[dict[str, object]]:
    async def chunks() -> AsyncIterator[dict[str, object]]:
        yield {"choices": [{"delta": {"content": "done"}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        }

    return chunks()


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
async def test_public_run_events_never_expose_prompt_or_tool_sentinels(
    tmp_path: Path,
) -> None:
    skill_marker = "SKILL-INSTRUCTIONS-PRIVATE-7D01"
    application_marker = "APPLICATION-SYSTEM-PROMPT-PRIVATE-9A23"
    model_params_marker = "MODEL-PARAMS-PRIVATE-2C44"
    tool_marker = "TOOL-SCHEMA-PRIVATE-4B18"
    skill_root = tmp_path / "skills"
    skill_dir = skill_root / "private-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: private-skill\n"
        "description: private test skill\n"
        "---\n"
        f"# Private\n\n{skill_marker}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=_successful_provider,
        skill_roots=(skill_root,),
        enable_builtin_tools=False,
    )

    async def private_tool(**_: object) -> dict[str, object]:
        return {"ok": True}

    sdk.tools.register(
        ToolSpec(
            name="private_tool",
            description="private tool",
            input_schema={
                "type": "object",
                "properties": {
                    "secret": {"type": "string", "description": tool_marker}
                },
            },
        ),
        private_tool,
    )
    spec = AgentSpec(
        name="private-agent",
        model="test/model",
        model_params={"application_secret": model_params_marker},
        prompt_profile="coding",
        system_prompt=application_marker,
        skills=("private-skill",),
    )
    profile_texts = tuple(
        message["content"]
        for message in PromptComposer()
        .compose(
            profile="coding",
            context_view=ContextView(
                view_id="view_profile_sentinel",
                session_id="ses_profile_sentinel",
                message_refs=(),
                capsule_id=None,
                estimated_tokens=0,
            ),
            model=spec.model,
        )
        .messages
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        result = await (
            await sdk.runs.start(session.session_id, spec, "ordinary user input")
        ).result()
        snapshot = await store.get_snapshot("run", result.run_id)
        assert snapshot is not None
        private_snapshot = json.dumps(snapshot, sort_keys=True)
        assert application_marker in private_snapshot
        assert model_params_marker in private_snapshot
        assert tool_marker in private_snapshot

        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        public_events = json.dumps(
            [item.event.model_dump(mode="json") for item in events],
            sort_keys=True,
        )
        created = next(
            item.event for item in events if item.event.type == "run.created"
        )
        assert created.schema_version == 2
        assert "execution_descriptor" not in created.payload
        for raw_text in (
            application_marker,
            model_params_marker,
            skill_marker,
            tool_marker,
            *profile_texts,
        ):
            assert raw_text not in public_events
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_workflow_missing_skill_fails_before_node_run_or_provider_call(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    async def provider(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        calls.append(kwargs)
        raise AssertionError("provider must not be called")

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    store = InMemoryStore()
    sdk = AgentSDK.for_test(
        store=store,
        acompletion=provider,
        skill_roots=(skill_root,),
    )
    sdk.agents.define(
        AgentSpec(
            name="worker",
            revision="1",
            model="test/model",
            skills=("missing-skill",),
        )
    )
    try:
        session = await sdk.sessions.create(workspaces=[])
        workflow = WorkflowIR.create(
            name="missing-skill",
            nodes=(
                AgentNode(
                    id="work",
                    agent_revision="worker:1",
                    input="Do work.",
                ),
            ),
            edges=(),
        )
        handle = await sdk.workflows.start(session.session_id, workflow)

        with pytest.raises(AgentSDKError) as raised:
            await handle.result()

        assert raised.value.code is ErrorCode.INVALID_STATE
        assert raised.value.message == "configured agent skill unavailable"
        assert calls == []
        events = await store.read_events(
            after_cursor=0,
            session_id=session.session_id,
        )
        assert any(item.event.type == "workflow.started" for item in events)
        assert all(item.event.type != "run.created" for item in events)
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_subagent_missing_skill_fails_before_child_run_or_provider_call(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    async def provider(**kwargs: object) -> AsyncIterator[dict[str, object]]:
        calls.append(kwargs)
        raise AssertionError("provider must not be called")

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    skills = SkillRegistry((skill_root,))
    skills.discover()
    store = InMemoryStore()
    commands = RuntimeCommands(store, agent_preflight=skills.validate_agent)
    engine = RunEngine(store, LiteLLMGateway._for_test(provider))
    agents = AgentRegistry()
    agents.define(
        AgentSpec(
            name="worker",
            revision="1",
            model="test/model",
            skills=("missing-skill",),
        )
    )
    service = SubagentService(store, commands, engine, agents)
    session = await commands.create_session(workspaces=[])
    parent = await commands.start_run(
        session.session_id,
        agent_revision="parent:1",
        user_input="parent",
    )

    with pytest.raises(AgentSDKError) as raised:
        await service.spawn(
            session_id=session.session_id,
            parent_run_id=parent.run_id,
            workflow_run_id="wfr_missing_skill",
            workflow_node_id="work",
            agent_revision="worker:1",
            task=TaskEnvelope(
                objective="Do work.",
                success_criteria=("Complete.",),
            ),
        )

    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "configured agent skill unavailable"
    assert calls == []
    events = await store.read_events(
        after_cursor=0,
        session_id=session.session_id,
    )
    assert [
        item.event
        for item in events
        if item.event.type == "run.created"
    ] == [
        next(
            item.event
            for item in events
            if item.event.run_id == parent.run_id
            and item.event.type == "run.created"
        )
    ]


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
