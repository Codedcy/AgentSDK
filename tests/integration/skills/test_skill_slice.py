from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_sdk import AgentSDKError, ErrorCode
from agent_sdk.skills import SkillRegistry
from agent_sdk.skills.loader import MAX_SKILL_FILE_BYTES


def _skill_text(
    *,
    name: str = "demo",
    description: str = "Demonstrate strict progressive loading.",
    extra: str = "",
    body: str = "# Demo\n\nFollow this demo skill.\n",
) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{extra}"
        "---\n"
        f"{body}"
    )


def _write_skill(
    root: Path,
    directory: str = "demo",
    *,
    text: str | None = None,
    raw: bytes | None = None,
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(text or _skill_text(name=directory), encoding="utf-8")
    return path


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    fixture = Path(__file__).parents[2] / "fixtures" / "skills" / "demo"
    shutil.copytree(fixture, root / "demo")
    return root


def test_skill_loads_only_detached_metadata_before_activation(skill_root: Path) -> None:
    source = skill_root / "demo" / "SKILL.md"
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    skills = SkillRegistry([skill_root])

    catalog = skills.discover()

    assert [item.name for item in catalog] == ["demo"]
    metadata = catalog[0]
    assert metadata.description == "Demonstrate strict progressive skill loading."
    assert metadata.location == source.resolve()
    assert metadata.content_hash == expected_hash
    assert metadata.instructions is None
    assert "Follow this demo" not in str(metadata.model_dump(mode="json"))

    activated = skills.activate("demo")
    assert activated.metadata is metadata
    assert "Follow this demo skill." in activated.instructions
    assert activated.root == source.parent.resolve()


def test_official_optional_metadata_is_strict_and_allowed_tools_are_inert(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        text=_skill_text(
            extra=(
                "license: Apache-2.0\n"
                "compatibility: Requires Python 3.13\n"
                "metadata:\n"
                "  author: example\n"
                "  version: '1'\n"
                "allowed-tools: shell read-file\n"
            )
        ),
    )

    metadata = SkillRegistry([root]).discover()[0]

    assert metadata.license == "Apache-2.0"
    assert metadata.compatibility == "Requires Python 3.13"
    assert metadata.metadata == {"author": "example", "version": "1"}
    assert metadata.allowed_tools == ("shell", "read-file")
    assert not hasattr(metadata, "policy")

    source = {"author": "copied"}
    copied = metadata.model_copy(update={"metadata": source})
    source["author"] = "mutated"
    assert copied.metadata == {"author": "copied"}


@pytest.mark.parametrize(
    ("directory", "name"),
    [
        ("demo", ""),
        ("demo", "Demo"),
        ("demo", "under_score"),
        ("demo", "-demo"),
        ("demo", "demo-"),
        ("demo", "demo--skill"),
        ("demo", "a" * 65),
        ("directory", "different"),
    ],
)
def test_rejects_invalid_or_directory_mismatched_names(
    tmp_path: Path, directory: str, name: str
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, directory, text=_skill_text(name=name))

    with pytest.raises(AgentSDKError) as raised:
        SkillRegistry([root]).discover()

    assert raised.value.code is ErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    "text",
    [
        _skill_text(description=""),
        _skill_text(description="d" * 1025),
        _skill_text(extra=f"compatibility: {'c' * 501}\n"),
        _skill_text(extra="metadata:\n  answer: 42\n"),
        _skill_text(extra="allowed-tools:\n  - shell\n"),
        _skill_text(extra="license: 42\n"),
        _skill_text(extra="license: null\n"),
        _skill_text(extra="compatibility: null\n"),
        _skill_text(extra="allowed-tools: null\n"),
        "---\nname: true\ndescription: demo\n---\nbody\n",
    ],
    ids=[
        "empty-description",
        "long-description",
        "long-compatibility",
        "metadata-non-string-value",
        "allowed-tools-not-string",
        "license-not-string",
        "license-null",
        "compatibility-null",
        "allowed-tools-null",
        "coerced-name",
    ],
)
def test_rejects_invalid_official_field_types_and_bounds(tmp_path: Path, text: str) -> None:
    root = tmp_path / "skills"
    _write_skill(root, text=text)

    with pytest.raises(AgentSDKError) as raised:
        SkillRegistry([root]).discover()

    assert raised.value.code is ErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    "raw",
    [
        b"name: demo\ndescription: missing frontmatter\n",
        b"---\n- not\n- mapping\n---\nbody\n",
        b"---\nname: [unterminated\n---\nbody\n",
        b"---\nname: demo\ndescription: missing close\n",
        b"---\nname: demo\ndescription: ok\nunknown: no\n---\nbody\n",
        b"---\nname: demo\n---\nbody\n",
    ],
    ids=[
        "missing-frontmatter",
        "non-mapping-frontmatter",
        "malformed-yaml",
        "missing-closing-marker",
        "unknown-field",
        "missing-description",
    ],
)
def test_rejects_missing_malformed_or_unknown_frontmatter(tmp_path: Path, raw: bytes) -> None:
    root = tmp_path / "skills"
    _write_skill(root, raw=raw)

    with pytest.raises(AgentSDKError) as raised:
        SkillRegistry([root]).discover()

    assert raised.value.code is ErrorCode.INVALID_STATE


def test_rejects_duplicate_yaml_frontmatter_keys(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        raw=(
            b"---\nname: demo\ndescription: first\ndescription: second\n---\nbody\n"
        ),
    )

    with pytest.raises(AgentSDKError) as raised:
        SkillRegistry([root]).discover()

    assert raised.value.code is ErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    "raw",
    [b"\xff\xfe\x00", b"x" * (MAX_SKILL_FILE_BYTES + 1)],
    ids=["non-utf8", "oversized"],
)
def test_rejects_non_utf8_and_oversized_files(tmp_path: Path, raw: bytes) -> None:
    root = tmp_path / "skills"
    _write_skill(root, raw=raw)

    with pytest.raises(AgentSDKError) as raised:
        SkillRegistry([root]).discover()

    assert raised.value.code is ErrorCode.INVALID_STATE


def test_duplicate_names_across_roots_never_override(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_skill(first)
    _write_skill(second)

    with pytest.raises(AgentSDKError) as raised:
        SkillRegistry([first, second]).discover()

    assert raised.value.code is ErrorCode.CONFLICT
    assert raised.value.message == "duplicate skill name"


def test_catalog_order_is_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "zeta")
    _write_skill(root, "alpha")

    assert [item.name for item in SkillRegistry([root]).discover()] == ["alpha", "zeta"]


def test_activation_rejects_changed_digest_and_unknown_name(skill_root: Path) -> None:
    skills = SkillRegistry([skill_root])
    skills.discover()
    (skill_root / "demo" / "SKILL.md").write_text(
        _skill_text(body="changed\n"), encoding="utf-8"
    )

    with pytest.raises(AgentSDKError) as changed:
        skills.activate("demo")
    assert changed.value.code is ErrorCode.CONFLICT
    assert changed.value.message == "skill changed after discovery"
    with pytest.raises(AgentSDKError) as missing:
        skills.activate("unknown")
    assert missing.value.code is ErrorCode.NOT_FOUND


def test_resolve_member_rechecks_real_path_containment(skill_root: Path, tmp_path: Path) -> None:
    reference = skill_root / "demo" / "references" / "guide.md"
    reference.parent.mkdir()
    reference.write_text("safe guide", encoding="utf-8")
    directory = skill_root / "demo" / "assets"
    directory.mkdir()
    activated = SkillRegistry([skill_root]).discover()
    del activated
    skill = SkillRegistry([skill_root])
    skill.discover()
    selected = skill.activate("demo")

    assert selected.resolve_member("references/guide.md") == reference.resolve()
    assert selected.read_text("references/guide.md") == "safe guide"
    for invalid in ("../outside.txt", str((tmp_path / "absolute.txt").resolve())):
        with pytest.raises(AgentSDKError):
            selected.resolve_member(invalid)
    with pytest.raises(AgentSDKError):
        selected.resolve_member("missing.md")
    with pytest.raises(AgentSDKError):
        selected.read_text("assets")


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    if os.name == "nt" and directory:
        try:
            subprocess.run(
                [
                    "cmd.exe",
                    "/c",
                    "mklink",
                    "/J",
                    str(link),
                    str(target),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return
        except (OSError, subprocess.CalledProcessError) as error:
            pytest.skip(f"junctions unavailable: {error}")
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks unavailable: {error}")


def test_rejects_skill_directory_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside" / "escape"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text(_skill_text(name="escape"), encoding="utf-8")
    _symlink_or_skip(root / "escape", outside, directory=True)

    with pytest.raises(AgentSDKError) as raised:
        SkillRegistry([root]).discover()

    assert raised.value.code is ErrorCode.INVALID_STATE


def test_rejects_member_symlink_escape(skill_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = skill_root / "demo" / "references" / "escape"
    link.parent.mkdir()
    _symlink_or_skip(link, outside, directory=True)
    skills = SkillRegistry([skill_root])
    skills.discover()

    with pytest.raises(AgentSDKError) as raised:
        skills.activate("demo").read_text("references/escape/secret.txt")

    assert raised.value.code is ErrorCode.INVALID_STATE


def test_activated_root_cannot_be_rebound_to_external_junction(
    skill_root: Path, tmp_path: Path
) -> None:
    skills = SkillRegistry([skill_root])
    skills.discover()
    activated = skills.activate("demo")
    outside = tmp_path / "outside-replacement"
    (outside / "references").mkdir(parents=True)
    (outside / "references" / "secret.txt").write_text("secret", encoding="utf-8")
    shutil.rmtree(skill_root / "demo")
    _symlink_or_skip(skill_root / "demo", outside, directory=True)

    with pytest.raises(AgentSDKError) as raised:
        activated.read_text("references/secret.txt")

    assert raised.value.code is ErrorCode.INVALID_STATE


def test_activation_rejects_skill_directory_rebound_with_same_digest(
    skill_root: Path, tmp_path: Path
) -> None:
    skills = SkillRegistry([skill_root])
    skills.discover()
    original = (skill_root / "demo" / "SKILL.md").read_bytes()
    outside = tmp_path / "outside-same-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(original)
    shutil.rmtree(skill_root / "demo")
    _symlink_or_skip(skill_root / "demo", outside, directory=True)

    with pytest.raises(AgentSDKError) as raised:
        skills.activate("demo")

    assert raised.value.code is ErrorCode.INVALID_STATE
