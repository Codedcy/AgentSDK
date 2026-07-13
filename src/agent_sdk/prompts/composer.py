from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from importlib import resources
from typing import Any

from agent_sdk.context.models import ContextView
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.prompts.models import (
    BuiltPrompt,
    PromptLayer,
    PromptLayerManifest,
    PromptManifest,
)
from agent_sdk.tools.models import freeze_json, thaw_json

_PROFILE_ORDER = {
    "general": ("general",),
    "coding": ("general", "coding"),
}
_PROFILE_VERSION = "1"


class PromptComposer:
    def compose(
        self,
        *,
        profile: str,
        context_view: ContextView,
        model: str,
        application: str | None = None,
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> BuiltPrompt:
        profile_names = _PROFILE_ORDER.get(profile)
        if profile_names is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "unknown prompt profile",
                retryable=False,
            )
        layers = [self._load_profile(name) for name in profile_names]
        if application:
            layers.append(self._layer("application", _PROFILE_VERSION, application))
        messages = tuple(
            {"role": "system", "content": layer.text} for layer in layers
        )
        text = "\n\n".join(layer.text for layer in layers)
        manifest = PromptManifest(
            layers=tuple(
                PromptLayerManifest(
                    layer_id=layer.layer_id,
                    version=layer.version,
                    sha256=layer.sha256,
                )
                for layer in layers
            ),
            sha256=self._sha256(text),
            context_view_id=context_view.view_id,
            model=model,
            tools_sha256=self._tool_fingerprint(tools),
        )
        return BuiltPrompt(messages=messages, manifest=manifest)

    @staticmethod
    def _load_profile(name: str) -> PromptLayer:
        try:
            text = (
                resources.files("agent_sdk.prompts.profiles")
                .joinpath(name, "system.md")
                .read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ModuleNotFoundError) as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "prompt profile resource unavailable",
                retryable=False,
            ) from error
        return PromptComposer._layer(
            f"profile:{name}",
            _PROFILE_VERSION,
            text,
        )

    @staticmethod
    def _layer(layer_id: str, version: str, text: str) -> PromptLayer:
        return PromptLayer(
            layer_id=layer_id,
            version=version,
            text=text,
            sha256=PromptComposer._sha256(text),
        )

    @staticmethod
    def _tool_fingerprint(tools: Sequence[Mapping[str, Any]]) -> str:
        try:
            canonical_value = thaw_json(freeze_json(list(tools)))
            canonical = json.dumps(
                canonical_value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception as error:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "tool schemas must be canonical JSON",
                retryable=False,
            ) from error
        return PromptComposer._sha256(canonical)

    @staticmethod
    def _sha256(text: str) -> str:
        return sha256(text.encode("utf-8")).hexdigest()
