from __future__ import annotations

from hashlib import sha256


def hashed_identity(value: str) -> dict[str, str]:
    """Return a stable, bounded public identity without exposing its source value."""

    return {"sha256": sha256(value.encode("utf-8")).hexdigest()}


__all__ = ["hashed_identity"]
