from __future__ import annotations

from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from agent_sdk.workflow import (
    MissingWorkflowValue,
    WorkflowExpression,
    WorkflowExpressionError,
    evaluate_expression,
    resolve_path,
)


SCOPE = {
    "inputs": {
        "mode": "fast",
        "enabled": True,
        "count": 4,
        "ratio": 4.5,
        "title": "agent-sdk",
        "tags": ["stable", {"kind": "release"}],
        "metadata": {"owner": "sdk"},
        "empty": None,
    },
    "outputs": {
        "score": {"value": 4},
        "items": ["a", ["b", "c"]],
    },
}


@pytest.mark.parametrize(
    ("expression", "expected"),
    (
        (WorkflowExpression(path="inputs.mode", op="eq", value="fast"), True),
        (WorkflowExpression(path="inputs.mode", op="ne", value="slow"), True),
        (WorkflowExpression(path="outputs.score.value", op="gt", value=3), True),
        (WorkflowExpression(path="outputs.score.value", op="gte", value=4), True),
        (WorkflowExpression(path="outputs.score.value", op="lt", value=5), True),
        (WorkflowExpression(path="outputs.score.value", op="lte", value=4), True),
        (WorkflowExpression(path="outputs.items.1", op="contains", value="b"), True),
        (WorkflowExpression(path="outputs.missing", op="exists"), False),
        (WorkflowExpression(path="inputs.empty", op="exists"), True),
    ),
)
def test_evaluate_expression(expression: WorkflowExpression, expected: bool) -> None:
    assert evaluate_expression(expression, SCOPE) is expected


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    (
        ("inputs.title", "sdk", True),
        ("inputs.title", "SDK", False),
        ("inputs.tags", "stable", True),
        ("inputs.tags", {"kind": "release"}, True),
        ("inputs.metadata", "owner", True),
        ("inputs.metadata", "sdk", False),
    ),
)
def test_contains_has_explicit_json_container_semantics(
    path: str,
    value: object,
    expected: bool,
) -> None:
    expression = WorkflowExpression.model_validate(
        {"path": path, "op": "contains", "value": value}
    )

    assert evaluate_expression(expression, SCOPE) is expected


@pytest.mark.parametrize(
    ("path", "value"),
    (
        ("inputs.count", 4),
        ("inputs.title", 1),
        ("inputs.metadata", 1),
    ),
)
def test_contains_rejects_scalar_or_incompatible_operands(
    path: str,
    value: object,
) -> None:
    expression = WorkflowExpression.model_validate(
        {"path": path, "op": "contains", "value": value}
    )

    with pytest.raises(
        WorkflowExpressionError,
        match="workflow expression operands are incompatible",
    ):
        evaluate_expression(expression, SCOPE)


@pytest.mark.parametrize(
    "expression",
    (
        WorkflowExpression(path="inputs.count", op="gt", value="3"),
        WorkflowExpression(path="inputs.enabled", op="gte", value=1),
        WorkflowExpression(path="inputs.count", op="lte", value=True),
        WorkflowExpression(path="inputs.empty", op="lt", value=None),
    ),
)
def test_ordering_rejects_type_mismatch_and_boolean_as_number(
    expression: WorkflowExpression,
) -> None:
    with pytest.raises(
        WorkflowExpressionError,
        match="workflow expression operands are incompatible",
    ):
        evaluate_expression(expression, SCOPE)


@pytest.mark.parametrize(
    "expression",
    (
        WorkflowExpression(path="inputs.enabled", op="eq", value=1),
        WorkflowExpression(path="inputs.count", op="ne", value=True),
        WorkflowExpression(path="inputs.count", op="eq", value="4"),
    ),
)
def test_equality_rejects_type_mismatch_and_boolean_as_number(
    expression: WorkflowExpression,
) -> None:
    with pytest.raises(
        WorkflowExpressionError,
        match="workflow expression operands are incompatible",
    ):
        evaluate_expression(expression, SCOPE)


@pytest.mark.parametrize(
    "path",
    (
        "",
        "mode",
        "state.mode",
        "inputs.__class__",
        "inputs.call()",
        "inputs.items[0]",
        "inputs..mode",
    ),
)
def test_resolve_path_rejects_empty_unsafe_or_function_like_paths(path: str) -> None:
    with pytest.raises(WorkflowExpressionError):
        resolve_path(SCOPE, path)


@pytest.mark.parametrize(
    "path",
    (
        "outputs.items.-1",
        "outputs.items.2",
        "outputs.items.word",
        "outputs.items.1.2",
        "outputs.unknown",
    ),
)
def test_resolve_path_normalizes_missing_and_invalid_array_locations(path: str) -> None:
    with pytest.raises(MissingWorkflowValue, match="workflow expression value is missing"):
        resolve_path(SCOPE, path)


@pytest.mark.parametrize("op", ("eq", "ne", "gt", "gte", "lt", "lte", "contains"))
def test_missing_path_is_normalized_for_every_non_exists_operator(op: str) -> None:
    expression = WorkflowExpression.model_validate(
        {"path": "outputs.unknown", "op": op, "value": 1}
    )

    with pytest.raises(
        WorkflowExpressionError,
        match="workflow expression value is missing",
    ) as raised:
        evaluate_expression(expression, SCOPE)

    assert type(raised.value) is WorkflowExpressionError


def test_exists_returns_false_for_missing_array_locations() -> None:
    for path in ("outputs.items.-1", "outputs.items.2", "outputs.items.word"):
        assert (
            evaluate_expression(WorkflowExpression(path=path, op="exists"), SCOPE)
            is False
        )


@pytest.mark.parametrize(
    "value",
    (
        float("nan"),
        float("inf"),
        float("-inf"),
        {"nested": [float("nan")]},
    ),
)
def test_expression_model_rejects_non_finite_json_numbers(value: object) -> None:
    with pytest.raises(ValidationError, match="JSON numbers must be finite"):
        WorkflowExpression.model_validate(
            {"path": "inputs.count", "op": "eq", "value": value}
        )


@pytest.mark.parametrize("value", ({1, 2}, {1: "non-string-key"}, b"bytes"))
def test_expression_model_rejects_non_json_values(value: object) -> None:
    with pytest.raises(ValidationError):
        WorkflowExpression.model_validate(
            {"path": "inputs.count", "op": "eq", "value": value}
        )


def test_expression_value_is_recursively_detached_frozen_and_serializable() -> None:
    source = {"items": ["a", {"enabled": True}]}
    expression = WorkflowExpression.model_validate(
        {"path": "inputs.metadata", "op": "eq", "value": source}
    )
    source["items"].append("mutated")

    assert isinstance(expression.value, Mapping)
    assert expression.model_dump(mode="json")["value"] == {
        "items": ["a", {"enabled": True}]
    }
    with pytest.raises(TypeError):
        expression.value["new"] = "value"  # type: ignore[index]


def test_expression_model_rejects_unknown_operators_and_extra_fields() -> None:
    for payload in (
        {"path": "inputs.mode", "op": "call"},
        {"path": "inputs.mode", "op": "eq", "extra": True},
    ):
        with pytest.raises(ValidationError):
            WorkflowExpression.model_validate(payload)
