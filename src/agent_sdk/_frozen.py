from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any


class FrozenMapping(Mapping[str, Any]):
    """Immutable SDK-owned wrapper whose mapping operations cannot call user code."""

    __slots__ = ("__values",)

    def __init__(self, values: dict[str, Any]) -> None:
        if type(values) is not dict:
            raise TypeError("FrozenMapping requires a built-in dict")
        self.__values = MappingProxyType(values.copy())

    def __getitem__(self, key: str) -> Any:
        return self.__values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__values)

    def __len__(self) -> int:
        return len(self.__values)

    @classmethod
    def trusted_items(cls, value: FrozenMapping) -> tuple[tuple[str, Any], ...]:
        values = object.__getattribute__(value, "_FrozenMapping__values")
        return tuple(values.items())
