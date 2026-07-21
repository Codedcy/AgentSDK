from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any


class FrozenMapping(Mapping[str, Any]):
    """Immutable SDK-owned wrapper whose mapping operations cannot call user code."""

    __slots__ = ("__values",)

    def __init__(self, values: dict[str, Any]) -> None:
        if type(values) is not dict:
            raise TypeError("FrozenMapping requires a built-in dict")
        self.__values = values.copy()

    def __getitem__(self, key: str) -> Any:
        values = self._builtin_values()
        if values is None:
            raise TypeError("FrozenMapping backing is invalid")
        return dict.__getitem__(values, key)

    def __iter__(self) -> Iterator[str]:
        values = self._builtin_values()
        if values is None:
            raise TypeError("FrozenMapping backing is invalid")
        return dict.__iter__(values)

    def __len__(self) -> int:
        values = self._builtin_values()
        if values is None:
            raise TypeError("FrozenMapping backing is invalid")
        return dict.__len__(values)

    def _builtin_values(self) -> dict[str, Any] | None:
        values = object.__getattribute__(self, "_FrozenMapping__values")
        return values if type(values) is dict else None
