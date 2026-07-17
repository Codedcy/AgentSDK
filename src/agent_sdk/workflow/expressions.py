from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Literal, TypeGuard

from agent_sdk.workflow.models import JsonValue, WorkflowExpression

_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_ORDERING_OPS = ("gt", "gte", "lt", "lte")
type _OrderingOp = Literal["gt", "gte", "lt", "lte"]


class WorkflowExpressionError(ValueError):
    """A stable error raised when a restricted expression cannot be evaluated."""


class MissingWorkflowValue(WorkflowExpressionError):
    """Internal path-resolution signal for a value that is not available."""

    def __init__(self, path: str) -> None:
        super().__init__("workflow expression value is missing")
        self.path = path


def resolve_path(scope: Mapping[str, JsonValue], path: str) -> JsonValue:
    parts = path.split(".")
    if not parts or parts[0] not in {"inputs", "outputs"}:
        raise WorkflowExpressionError("path must start with inputs or outputs")

    current: object = scope
    for part in parts:
        if not _SEGMENT.fullmatch(part) or part.startswith("__"):
            raise WorkflowExpressionError("invalid path segment")
        if isinstance(current, Mapping):
            if part not in current:
                raise MissingWorkflowValue(path)
            current = current[part]
        elif _is_json_array(current):
            if not part.isdecimal():
                raise MissingWorkflowValue(path)
            index = int(part)
            if index >= len(current):
                raise MissingWorkflowValue(path)
            current = current[index]
        else:
            raise MissingWorkflowValue(path)
    return current  # type: ignore[return-value]


def evaluate_expression(
    expression: WorkflowExpression,
    scope: Mapping[str, JsonValue],
) -> bool:
    try:
        actual = resolve_path(scope, expression.path)
    except MissingWorkflowValue:
        if expression.op == "exists":
            return False
        raise WorkflowExpressionError("workflow expression value is missing") from None

    if expression.op == "exists":
        return True
    if expression.op == "eq":
        _require_equality_compatible(actual, expression.value)
        return _json_equal(actual, expression.value)
    if expression.op == "ne":
        _require_equality_compatible(actual, expression.value)
        return not _json_equal(actual, expression.value)
    if expression.op == "contains":
        return _contains(actual, expression.value)
    if expression.op in _ORDERING_OPS:
        return _ordered_compare(actual, expression.value, expression.op)
    raise WorkflowExpressionError("workflow expression operator is unsupported")


def _is_json_array(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise WorkflowExpressionError("workflow expression number must be finite")


def _json_kind(value: object) -> str:
    _require_finite(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if _is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    if _is_json_array(value):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    raise WorkflowExpressionError("workflow expression operands are incompatible")


def _require_equality_compatible(left: object, right: object) -> None:
    if _json_kind(left) != _json_kind(right):
        raise WorkflowExpressionError("workflow expression operands are incompatible")


def _json_equal(left: object, right: object) -> bool:
    _require_finite(left)
    _require_finite(right)
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if _is_number(left) or _is_number(right):
        return _is_number(left) and _is_number(right) and left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if _is_json_array(left) or _is_json_array(right):
        return (
            _is_json_array(left)
            and _is_json_array(right)
            and len(left) == len(right)
            and all(
                _json_equal(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        )
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        return (
            isinstance(left, Mapping)
            and isinstance(right, Mapping)
            and left.keys() == right.keys()
            and all(_json_equal(left[key], right[key]) for key in left)
        )
    raise WorkflowExpressionError("workflow expression operands are incompatible")


def _ordered_compare(
    left: object,
    right: object,
    op: _OrderingOp,
) -> bool:
    _require_finite(left)
    _require_finite(right)
    if _is_number(left) and _is_number(right):
        if op == "gt":
            return left > right
        if op == "gte":
            return left >= right
        if op == "lt":
            return left < right
        return left <= right
    elif isinstance(left, str) and isinstance(right, str):
        if op == "gt":
            return left > right
        if op == "gte":
            return left >= right
        if op == "lt":
            return left < right
        return left <= right
    raise WorkflowExpressionError("workflow expression operands are incompatible")


def _contains(container: object, candidate: object) -> bool:
    if isinstance(container, str):
        if not isinstance(candidate, str):
            raise WorkflowExpressionError(
                "workflow expression operands are incompatible"
            )
        return candidate in container
    if _is_json_array(container):
        return any(_json_equal(item, candidate) for item in container)
    if isinstance(container, Mapping):
        if not isinstance(candidate, str):
            raise WorkflowExpressionError(
                "workflow expression operands are incompatible"
            )
        return candidate in container
    raise WorkflowExpressionError("workflow expression operands are incompatible")
