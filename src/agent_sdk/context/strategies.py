from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Never

from agent_sdk.context.models import SourceMessage
from agent_sdk.tools.models import thaw_json


@dataclass(frozen=True)
class StrategyResult:
    items: tuple[SourceMessage, ...]
    source_refs: tuple[str, ...]
    transformations: tuple[str, ...]


def apply_l0(sources: tuple[SourceMessage, ...]) -> StrategyResult:
    refs = _source_refs(sources)
    return StrategyResult(sources, refs, ())


def apply_l1(
    sources: tuple[SourceMessage, ...],
    *,
    tool_preview_bytes: int,
) -> StrategyResult:
    refs = _source_refs(sources)
    _validate_non_negative_int(tool_preview_bytes, "tool_preview_bytes")
    seen_tools: dict[str, str] = {}
    rendered: list[SourceMessage] = []
    transformations: list[str] = []
    for source in sources:
        if _role(source) != "tool" or source.current or source.protected:
            rendered.append(source)
            continue
        digest = _tool_digest(source.message.get("content"))
        first_ref = seen_tools.get(digest)
        if first_ref is not None:
            rendered.append(
                _replace_content(source, f"[duplicate:{first_ref}]")
            )
            transformations.append(f"dedupe:{source.ref}")
            continue
        seen_tools[digest] = source.ref
        content = source.message.get("content")
        if (
            isinstance(content, str)
            and len(content.encode("utf-8")) > tool_preview_bytes
        ):
            rendered.append(
                _replace_content(
                    source,
                    _tool_preview(
                        content,
                        ref=source.ref,
                        preview_bytes=tool_preview_bytes,
                    ),
                )
            )
            transformations.append(f"tool_preview:{source.ref}")
            continue
        rendered.append(source)
    return StrategyResult(tuple(rendered), refs, tuple(transformations))


def apply_l2(
    sources: tuple[SourceMessage, ...],
    *,
    recent_messages: int,
    tool_preview_bytes: int,
) -> StrategyResult:
    _validate_non_negative_int(recent_messages, "recent_messages")
    _validate_non_negative_int(tool_preview_bytes, "tool_preview_bytes")
    l1 = apply_l1(sources, tool_preview_bytes=tool_preview_bytes)
    refs = l1.source_refs
    recent_start = max(0, len(sources) - recent_messages)
    rendered: list[SourceMessage] = []
    transformations = list(l1.transformations)
    for index, (source, l1_source) in enumerate(
        zip(sources, l1.items, strict=True)
    ):
        if source.protected or source.current or index >= recent_start:
            rendered.append(l1_source)
            continue
        role = _role(source)
        kind = "tool_result" if role == "tool" else "exchange"
        summary = (
            "Tool result detail omitted; retrieve it by source reference."
            if role == "tool"
            else "Older completed message omitted; retrieve it by source reference."
        )
        outcome = {
            "kind": kind,
            "role": role,
            "source_refs": [source.ref],
            "status": "completed",
            "summary": summary,
        }
        rendered.append(
            _replace_content(
                l1_source,
                json.dumps(
                    outcome,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
        transformations.append(f"outcome:{source.ref}")
    return StrategyResult(tuple(rendered), refs, tuple(transformations))


def _source_refs(sources: tuple[SourceMessage, ...]) -> tuple[str, ...]:
    refs = tuple(source.ref for source in sources)
    if len(refs) != len(set(refs)):
        raise ValueError("source message refs must be unique")
    return refs


def _validate_non_negative_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _role(source: SourceMessage) -> str:
    return source.role


def _replace_content(source: SourceMessage, content: str) -> SourceMessage:
    message = thaw_json(source.message)
    assert isinstance(message, dict)
    message["content"] = content
    return source.model_copy(update={"message": message})


def _tool_digest(content: Any) -> str:
    if isinstance(content, str):
        try:
            canonical_value = json.loads(
                content,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_object,
            )
            _validate_canonical_json(
                canonical_value,
                depth=0,
                entries=[0],
            )
            canonical = json.dumps(
                canonical_value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return hashlib.sha256(b"json\0" + canonical).hexdigest()
        except (ValueError, RecursionError):
            return hashlib.sha256(
                b"raw\0" + content.encode("utf-8")
            ).hexdigest()
    raw = json.dumps(
        content,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"raw\0" + raw).hexdigest()


def _reject_json_constant(_: str) -> Never:
    raise ValueError("nonstandard JSON constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _validate_canonical_json(
    value: Any,
    *,
    depth: int,
    entries: list[int],
) -> None:
    if isinstance(value, (dict, list)):
        if depth > 32:
            raise ValueError("JSON nesting exceeds canonicalization limit")
        entries[0] += len(value)
        if entries[0] > 20_000:
            raise ValueError("JSON entries exceed canonicalization limit")
        items = value.values() if isinstance(value, dict) else value
        for item in items:
            _validate_canonical_json(
                item,
                depth=depth + 1,
                entries=entries,
            )
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")


def _tool_preview(content: str, *, ref: str, preview_bytes: int) -> str:
    head_bytes = (preview_bytes + 1) // 2
    tail_bytes = preview_bytes // 2
    head = _utf8_prefix(content, head_bytes)
    tail = _utf8_suffix(content, tail_bytes)
    marker = f"[source:{ref}]"
    return f"{head}\n…\n{tail}\n{marker}"


def _utf8_prefix(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _utf8_suffix(value: str, limit: int) -> str:
    if limit == 0:
        return ""
    encoded = value.encode("utf-8")
    return encoded[-limit:].decode("utf-8", errors="ignore")
