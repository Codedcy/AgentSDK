from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_sdk.context import CompactionLevel, ContextView
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.prompts import PromptComposer
from agent_sdk.skills import SkillRegistry


def _view() -> ContextView:
    return ContextView(
        view_id="view_prompt",
        session_id="ses_prompt",
        message_refs=("evt_user",),
        capsule_id=None,
        estimated_tokens=10,
        recommended_level=CompactionLevel.L0,
        applied_level=CompactionLevel.L0,
    )


def test_coding_prompt_has_stable_order_hashes_and_provenance() -> None:
    built = PromptComposer().compose(
        profile="coding",
        application="Application instructions.",
        context_view=_view(),
        model="fake/model",
        tools=({"type": "function", "function": {"name": "lookup"}},),
    )

    assert tuple(layer.layer_id for layer in built.manifest.layers) == (
        "profile:general",
        "profile:coding",
        "application",
    )
    assert tuple(message["role"] for message in built.messages) == (
        "system",
        "system",
        "system",
    )
    assert built.messages[-1]["content"] == "Application instructions."
    for message, layer in zip(built.messages, built.manifest.layers, strict=True):
        assert sha256(message["content"].encode("utf-8")).hexdigest() == layer.sha256
    assert sha256(built.text.encode("utf-8")).hexdigest() == built.manifest.sha256
    assert built.manifest.context_view_id == "view_prompt"
    assert built.manifest.model == "fake/model"


def test_general_profile_is_first_and_application_is_last() -> None:
    composer = PromptComposer()
    general = composer.compose(
        profile="general",
        context_view=_view(),
        model="fake/model",
    )
    coding = composer.compose(
        profile="coding",
        application="Application layer.",
        context_view=_view(),
        model="fake/model",
    )

    assert tuple(layer.layer_id for layer in general.manifest.layers) == (
        "profile:general",
    )
    assert tuple(layer.layer_id for layer in coding.manifest.layers) == (
        "profile:general",
        "profile:coding",
        "application",
    )
    assert coding.messages[0] == general.messages[0]
    assert coding.messages[-1]["content"] == "Application layer."


def test_skill_layers_preserve_order_and_reject_duplicate_names() -> None:
    root = Path(__file__).parents[2] / "fixtures" / "skills"
    registry = SkillRegistry((root,))
    registry.discover()
    demo = registry.activate("demo")
    coding = registry.activate("coding-demo")
    composer = PromptComposer()

    built = composer.compose(
        profile="general",
        skills=(demo, coding),
        context_view=_view(),
        model="fake/model",
    )

    assert tuple(layer.layer_id for layer in built.manifest.layers[-2:]) == (
        "skill:demo",
        "skill:coding-demo",
    )
    assert tuple(layer.version for layer in built.manifest.layers[-2:]) == (
        demo.metadata.content_hash,
        coding.metadata.content_hash,
    )
    with pytest.raises(AgentSDKError) as raised:
        composer.compose(
            profile="general",
            skills=(demo, demo),
            context_view=_view(),
            model="fake/model",
        )
    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "duplicate prompt skill"


def test_tool_fingerprint_uses_canonical_json_independent_of_key_order() -> None:
    first = PromptComposer().compose(
        profile="general",
        context_view=_view(),
        model="fake/model",
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object", "properties": {"q": {}}},
                },
            },
        ),
    )
    second = PromptComposer().compose(
        profile="general",
        context_view=_view(),
        model="fake/model",
        tools=(
            {
                "function": {
                    "parameters": {"properties": {"q": {}}, "type": "object"},
                    "name": "lookup",
                },
                "type": "function",
            },
        ),
    )
    assert first.manifest.tools_sha256 == second.manifest.tools_sha256


def test_prompt_models_are_frozen_and_deeply_detached() -> None:
    tool = {
        "type": "function",
        "function": {"name": "before", "parameters": {"type": "object"}},
    }
    built = PromptComposer().compose(
        profile="general",
        context_view=_view(),
        model="fake/model",
        tools=(tool,),
    )
    fingerprint = built.manifest.tools_sha256
    tool["function"]["name"] = "after"

    assert built.manifest.tools_sha256 == fingerprint
    try:
        built.messages[0]["content"] = "mutated"
    except TypeError:
        pass
    else:
        raise AssertionError("messages must be deeply immutable")
    try:
        built.manifest.model = "changed"
    except (AttributeError, TypeError, ValidationError):
        pass
    else:
        raise AssertionError("manifest must be frozen")


def test_unknown_profile_fails_closed_without_opening_a_caller_path() -> None:
    with pytest.raises(AgentSDKError) as raised:
        PromptComposer().compose(
            profile="../../secrets",
            context_view=_view(),
            model="fake/model",
        )
    assert raised.value.code is ErrorCode.INVALID_STATE
    assert raised.value.message == "unknown prompt profile"


def test_profile_resource_os_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_files(_: str) -> object:
        raise PermissionError("resource path secret")

    monkeypatch.setattr("agent_sdk.prompts.composer.resources.files", failed_files)
    with pytest.raises(AgentSDKError) as raised:
        PromptComposer().compose(
            profile="general",
            context_view=_view(),
            model="fake/model",
        )
    assert raised.value.code is ErrorCode.INTERNAL
    assert raised.value.message == "prompt profile resource unavailable"
    assert "resource path secret" not in str(raised.value)


def test_default_resources_are_utf8_and_cover_required_behavior() -> None:
    general = PromptComposer().compose(
        profile="general",
        context_view=_view(),
        model="fake/model",
    ).messages[0]["content"]
    coding = PromptComposer().compose(
        profile="coding",
        context_view=_view(),
        model="fake/model",
    ).messages[1]["content"]
    assert general.encode("utf-8").decode("utf-8") == general
    assert coding.encode("utf-8").decode("utf-8") == coding

    general_text = general.casefold()
    for phrase in (
        "identity",
        "goal",
        "persist",
        "termination",
        "tool protocol",
        "permission",
        "wait",
        "evidence",
        "transparent error",
        "context view",
        "capsule",
        "workflow",
        "child",
        "final report",
    ):
        assert phrase in general_text
    coding_text = coding.casefold()
    for phrase in (
        "explore",
        "plan",
        "approve",
        "implement",
        "verify",
        "report",
        "workspace",
        "preserve changes",
    ):
        assert phrase in coding_text


def test_wheel_and_sdist_include_both_markdown_profiles(tmp_path: Path) -> None:
    root = Path(__file__).parents[3]
    uv = os.environ.get("UV") or shutil.which("uv")
    if uv is None:
        pytest.skip("uv executable is unavailable")
    subprocess.run(
        [
            uv,
            "build",
            "--out-dir",
            str(tmp_path),
            str(root),
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    sdist = next(tmp_path.glob("*.tar.gz"))
    markdown = {
        "agent_sdk/prompts/profiles/general/system.md",
        "agent_sdk/prompts/profiles/coding/system.md",
    }
    with zipfile.ZipFile(wheel) as archive:
        assert markdown <= set(archive.namelist())
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        assert all(any(name.endswith(path) for name in names) for path in markdown)
