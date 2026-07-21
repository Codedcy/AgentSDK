from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

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
from agent_sdk.events.models import EventEnvelope
from agent_sdk.models.litellm_gateway import LiteLLMGateway
from agent_sdk.observability.queries import QueryService
from agent_sdk.permissions.policy import PolicyEngine
from agent_sdk.prompts import PromptComposer
from agent_sdk.runtime.agents import AgentRegistry
from agent_sdk.runtime.commands import RuntimeCommands
from agent_sdk.runtime.engine import RunEngine
from agent_sdk.runtime.execution import (
    ExecutionDescriptor,
    ExecutionPolicyDescriptor,
)
from agent_sdk.runtime.models import (
    AgentSpec,
    RunSnapshot,
    RunStatus,
    run_created_event_matches,
)
from agent_sdk.runtime.recovery import RunRecoveryService
from agent_sdk.runtime.reconciliation import RecoveryStateConflictError
from agent_sdk.skills import SkillRegistry
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    SnapshotWrite,
)
from agent_sdk.storage.memory import InMemoryStore
from agent_sdk.storage.sqlite import SQLiteStore
from agent_sdk.subagents import SubagentService, TaskEnvelope
from agent_sdk.tools.registry import ToolRegistry


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


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _r2_execution_descriptor(
    spec: AgentSpec,
    user_input: str,
) -> dict[str, Any]:
    current = ExecutionDescriptor.create(
        agent=spec,
        messages=({"role": "user", "content": user_input},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    ).model_dump(mode="json")
    for field in ("prompt_profile", "system_prompt", "skills", "context"):
        current["agent"].pop(field)
    current["agent_hash"] = _canonical_hash(current["agent"])
    current["descriptor_hash"] = _canonical_hash(
        {
            key: value
            for key, value in current.items()
            if key != "descriptor_hash"
        }
    )
    return current


async def _seed_r2_schema_v1_run(
    store: SQLiteStore,
    spec: AgentSpec,
    *,
    tamper: Literal[
        "agent_hash",
        "descriptor_hash",
        "identity",
        "cross_session",
    ]
    | None = None,
) -> tuple[str, str]:
    session = await RuntimeCommands(store).create_session(workspaces=[])
    run_id = f"run_r2_{tamper or 'valid'}"
    user_input = "recover genuine R2 run"
    current_descriptor = ExecutionDescriptor.create(
        agent=spec,
        messages=({"role": "user", "content": user_input},),
        tools=(),
        policy=ExecutionPolicyDescriptor.create(permission_default="allow"),
    )
    current = RunSnapshot(
        run_id=run_id,
        session_id=session.session_id,
        agent_revision=f"{spec.name}:{spec.revision}",
        status=RunStatus.CREATED,
        user_input=user_input,
        execution_compatibility="current",
        execution_descriptor=current_descriptor,
    )
    raw_snapshot = current.model_dump(mode="json")
    raw_snapshot["execution_descriptor"] = _r2_execution_descriptor(
        spec,
        user_input,
    )
    event_payload = deepcopy(raw_snapshot)
    event_session_id = session.session_id
    if tamper == "agent_hash":
        event_payload["execution_descriptor"]["agent_hash"] = "a" * 64
    elif tamper == "descriptor_hash":
        event_payload["execution_descriptor"]["descriptor_hash"] = "d" * 64
    elif tamper == "identity":
        event_payload["parent_run_id"] = "run_forged_parent"
    elif tamper == "cross_session":
        event_session_id = "ses_cross_session"

    updated_session = session.model_copy(
        update={
            "active_run_ids": (run_id,),
            "version": session.version + 1,
        }
    )
    await store.commit(
        CommitBatch(
            events=(
                EventEnvelope.new(
                    type="session.run.attached",
                    session_id=session.session_id,
                    run_id=None,
                    sequence=updated_session.version,
                    payload={"run_id": run_id},
                ),
                EventEnvelope.new(
                    schema_version=1,
                    type="run.created",
                    session_id=event_session_id,
                    run_id=run_id,
                    sequence=1,
                    payload=event_payload,
                ),
            ),
            snapshots=(
                SnapshotWrite(
                    "session",
                    session.session_id,
                    session.session_id,
                    updated_session.version,
                    updated_session.model_dump(mode="json"),
                ),
                SnapshotWrite(
                    "run",
                    run_id,
                    session.session_id,
                    1,
                    raw_snapshot,
                ),
            ),
        )
    )
    return session.session_id, run_id


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
                    {
                        "layer_id": layer.layer_id,
                        "version": layer.version,
                        "sha256": layer.sha256,
                    }
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
        assert created.schema_version == 3
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
async def test_genuine_r2_schema_v1_run_recovers_and_builds_tree_after_sqlite_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "r2-v1.db"
    spec = AgentSpec(name="r2-agent", revision="1", model="test/model")
    store = await SQLiteStore.open(database_path)
    try:
        _, run_id = await _seed_r2_schema_v1_run(store, spec)
    finally:
        await store.close()

    reopened = await SQLiteStore.open(database_path)
    agents = AgentRegistry()
    agents.define(spec)
    tools = ToolRegistry()
    policy = PolicyEngine("allow")
    engine = RunEngine(
        reopened,
        LiteLLMGateway._for_test(_successful_provider),
        tools,
        policy,
    )
    recovery = RunRecoveryService(
        reopened,
        engine,
        agents,
        tools,
        policy,
    )
    try:
        persisted = RunSnapshot.model_validate(
            await reopened.get_snapshot("run", run_id)
        )
        assert persisted.execution_descriptor is not None
        assert persisted.execution_descriptor.agent.prompt_profile == "general"
        assert persisted.execution_descriptor.agent.system_prompt is None
        assert persisted.execution_descriptor.agent.skills == ()
        tree = await QueryService(reopened).execution_tree(run_id)
        assert tuple(node.snapshot.run_id for node in tree.nodes) == (run_id,)

        plan = await recovery.plan(run_id)
        assert plan.request is not None
        result = await recovery.execute(plan)

        assert result.run_id == run_id
        assert result.output_text == "done"
        assert RunSnapshot.model_validate(
            await reopened.get_snapshot("run", run_id)
        ).status is RunStatus.COMPLETED
    finally:
        await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    ("agent_hash", "descriptor_hash", "identity", "cross_session"),
)
async def test_r2_schema_v1_authentication_rejects_tampered_event_after_reopen(
    tmp_path: Path,
    tamper: Literal[
        "agent_hash",
        "descriptor_hash",
        "identity",
        "cross_session",
    ],
) -> None:
    database_path = tmp_path / f"r2-v1-{tamper}.db"
    spec = AgentSpec(name="r2-agent", revision="1", model="test/model")
    store = await SQLiteStore.open(database_path)
    try:
        session_id, run_id = await _seed_r2_schema_v1_run(
            store,
            spec,
            tamper=tamper,
        )
    finally:
        await store.close()

    reopened = await SQLiteStore.open(database_path)
    try:
        snapshot = RunSnapshot.model_validate(
            await reopened.get_snapshot("run", run_id)
        )
        events = await reopened.read_events(after_cursor=0)
        created = next(
            stored.event
            for stored in events
            if stored.event.type == "run.created"
        )
        assert created.schema_version == 1
        payload_matches = run_created_event_matches(
            snapshot,
            created.payload,
            schema_version=created.schema_version,
        )
        assert payload_matches is (tamper == "cross_session")
        assert snapshot.session_id == session_id
        with pytest.raises(AgentSDKError) as raised:
            await QueryService(reopened).execution_tree(run_id)
        assert raised.value.code is ErrorCode.INTERNAL
    finally:
        await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    ("agent_hash", "descriptor_hash", "noncanonical_json"),
)
async def test_r2_private_snapshot_recovery_validation_rejects_tampering(
    tmp_path: Path,
    tamper: Literal["agent_hash", "descriptor_hash", "noncanonical_json"],
) -> None:
    database_path = tmp_path / f"r2-private-{tamper}.db"
    spec = AgentSpec(name="r2-agent", revision="1", model="test/model")
    store = await SQLiteStore.open(database_path)
    try:
        _, run_id = await _seed_r2_schema_v1_run(store, spec)
        async with store._connection.execute(
            """
            SELECT data_json FROM snapshots
            WHERE kind = 'run' AND entity_id = ?
            """,
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        stored_json = str(row[0])
        if tamper == "noncanonical_json":
            replacement = stored_json + " "
        else:
            raw = json.loads(stored_json)
            raw["execution_descriptor"][tamper] = tamper[0] * 64
            replacement = json.dumps(
                raw,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        await store._connection.execute(
            """
            UPDATE snapshots SET data_json = ?
            WHERE kind = 'run' AND entity_id = ?
            """,
            (replacement, run_id),
        )
        await store._connection.commit()
    finally:
        await store.close()

    if tamper != "noncanonical_json":
        with pytest.raises(ValueError, match="incompatible current projections"):
            await SQLiteStore.open(database_path)
        return

    reopened = await SQLiteStore.open(database_path)
    try:
        with pytest.raises(RecoveryStateConflictError):
            await reopened.list_external_operations(run_id)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_r2_authenticated_event_allows_normalized_snapshot_precondition(
    tmp_path: Path,
) -> None:
    store = await SQLiteStore.open(tmp_path / "r2-precondition-valid.db")
    spec = AgentSpec(name="r2-agent", revision="1", model="test/model")
    try:
        session_id, run_id = await _seed_r2_schema_v1_run(store, spec)
        normalized = RunSnapshot.model_validate(
            await store.get_snapshot("run", run_id)
        )

        await store.commit(
            CommitBatch(
                events=(),
                preconditions=(
                    SnapshotPrecondition(
                        "run",
                        run_id,
                        version=1,
                        session_id=session_id,
                        data=normalized.model_dump(mode="json"),
                    ),
                ),
            )
        )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    (
        "event_session",
        "sequence",
        "schema_version",
        "payload",
        "noncanonical_payload",
        "old_hash",
        "multiple_created",
    ),
)
async def test_r2_normalized_snapshot_precondition_rejects_invalid_creation_event(
    tmp_path: Path,
    tamper: Literal[
        "event_session",
        "sequence",
        "schema_version",
        "payload",
        "noncanonical_payload",
        "old_hash",
        "multiple_created",
    ],
) -> None:
    store = await SQLiteStore.open(tmp_path / f"r2-precondition-{tamper}.db")
    spec = AgentSpec(name="r2-agent", revision="1", model="test/model")
    try:
        session_id, run_id = await _seed_r2_schema_v1_run(store, spec)
        normalized = RunSnapshot.model_validate(
            await store.get_snapshot("run", run_id)
        )
        events = await store.read_events(after_cursor=0)
        created = next(
            stored.event
            for stored in events
            if stored.event.type == "run.created"
        )
        if tamper == "event_session":
            await store._connection.execute(
                "UPDATE events SET session_id = ? WHERE event_id = ?",
                ("ses_forged", created.event_id),
            )
        elif tamper == "sequence":
            await store._connection.execute(
                "UPDATE events SET sequence = 2 WHERE event_id = ?",
                (created.event_id,),
            )
        elif tamper == "schema_version":
            await store._connection.execute(
                "UPDATE events SET schema_version = 2 WHERE event_id = ?",
                (created.event_id,),
            )
        elif tamper == "payload":
            await store._connection.execute(
                "UPDATE events SET payload_json = ? WHERE event_id = ?",
                ('{"forged":"payload"}', created.event_id),
            )
        elif tamper == "noncanonical_payload":
            await store._connection.execute(
                "UPDATE events SET payload_json = payload_json || ' ' WHERE event_id = ?",
                (created.event_id,),
            )
        elif tamper == "old_hash":
            raw_payload = deepcopy(created.payload)
            raw_payload["execution_descriptor"]["agent_hash"] = "a" * 64
            await store._connection.execute(
                "UPDATE events SET payload_json = ? WHERE event_id = ?",
                (
                    json.dumps(
                        raw_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    created.event_id,
                ),
            )
        else:
            await store.commit(
                CommitBatch(
                    events=(
                        EventEnvelope.new(
                            schema_version=1,
                            type="run.created",
                            session_id=session_id,
                            run_id=run_id,
                            sequence=2,
                            payload=created.payload,
                        ),
                    ),
                )
            )
        await store._connection.commit()

        with pytest.raises(SnapshotPreconditionError):
            await store.commit(
                CommitBatch(
                    events=(),
                    preconditions=(
                        SnapshotPrecondition(
                            "run",
                            run_id,
                            version=1,
                            session_id=session_id,
                            data=normalized.model_dump(mode="json"),
                        ),
                    ),
                )
            )
    finally:
        await store.close()


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
