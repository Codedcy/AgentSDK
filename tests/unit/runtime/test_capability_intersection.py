from __future__ import annotations

from pathlib import Path

import pytest

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.runtime.models import intersect_names, intersect_workspaces
from agent_sdk.tools.models import ToolSpec
from agent_sdk.tools.registry import ToolRegistry


def _tool(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} tool",
        input_schema={"type": "object", "additionalProperties": False},
        source="test",
        effects=(),
    )


def test_intersection_distinguishes_inherit_from_empty() -> None:
    available = ("bash", "read", "write")

    assert intersect_names(available, None, None) == available
    assert intersect_names(available, ("read", "write"), ("read",)) == ("read",)
    assert intersect_names(available, (), None) == ()


def test_workspace_intersection_requires_contained_roots(tmp_path: Path) -> None:
    root = (tmp_path / "workspace").resolve()
    child = root / "child"

    assert intersect_workspaces((root,), None, (str(child),)) == (child,)
    assert intersect_workspaces((root,), None, (str(tmp_path / "outside"),)) == ()


def test_intersections_normalize_dedupe_and_keep_descendant_scopes(tmp_path: Path) -> None:
    root = (tmp_path / "workspace").resolve()
    child = root / "child"
    grandchild = child / "nested"

    assert intersect_names(
        ("bash", "read", "write"),
        ("write", "read", "write"),
        ("read", "write"),
    ) == ("read", "write")
    assert intersect_workspaces(
        (root,),
        (str(root / "."), str(child)),
        (str(child), str(grandchild)),
    ) == (child, grandchild)


def test_empty_ancestor_capabilities_cannot_be_expanded(tmp_path: Path) -> None:
    root = (tmp_path / "workspace").resolve()
    child = root / "child"

    assert intersect_names(("read",), (), ("read",)) == ()
    assert intersect_workspaces((root,), (), (str(child),)) == ()


def test_workspace_intersection_keeps_the_narrower_scope_when_later_scope_is_wider(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "workspace").resolve()
    child = root / "child"

    assert intersect_workspaces(
        (root,),
        (str(child),),
        (str(root),),
    ) == (child,)


def test_catalog_selects_canonical_unique_names_and_rejects_unknown() -> None:
    registry = ToolRegistry()

    async def handler() -> None:
        return None

    registry.register(_tool("write"), handler)
    registry.register(_tool("read"), handler)

    assert tuple(spec.name for spec in registry.select(("write", "read", "write")).list()) == (
        "read",
        "write",
    )
    with pytest.raises(AgentSDKError) as error:
        registry.select(("unknown",))
    assert error.value.code is ErrorCode.NOT_FOUND
