from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Generic, TypeAlias, TypeVar, cast

import litellm
from pydantic import BaseModel

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.runtime.models import TokenUsage

_ACompletion: TypeAlias = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    params: dict[str, Any] = field(default_factory=dict)
    purpose: str | None = None


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class UsageReported:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None

    def to_payload(self) -> dict[str, int | None]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    def to_usage(self) -> TokenUsage:
        return TokenUsage(**self.to_payload())


_StructuredModel = TypeVar("_StructuredModel", bound=BaseModel)


@dataclass(frozen=True)
class StructuredCompletion(Generic[_StructuredModel]):
    parsed: _StructuredModel
    usage: UsageReported

    @property
    def value(self) -> _StructuredModel:
        return self.parsed


@dataclass(frozen=True)
class ModelCompleted:
    finish_reason: str | None

    def to_payload(self) -> dict[str, str | None]:
        return {"finish_reason": self.finish_reason}


@dataclass(frozen=True)
class ToolCallCompleted:
    index: int
    call_id: str
    name: str
    arguments_json: str


@dataclass
class _ToolCallParts:
    call_id: str = ""
    name: str = ""
    arguments_json: str = ""


ModelEvent: TypeAlias = (
    TextDelta | ToolCallCompleted | UsageReported | ModelCompleted
)


def _value(container: object, name: str) -> Any:
    if isinstance(container, Mapping):
        return container.get(name)
    return getattr(container, name, None)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("usage token count must be an integer")
    normalized = int(cast(int, value))
    if normalized < 0:
        raise ValueError("usage token count must be non-negative")
    return normalized


class LiteLLMGateway:
    def __init__(self) -> None:
        self._acompletion: _ACompletion = litellm.acompletion

    @classmethod
    def _for_test(cls, acompletion: _ACompletion) -> LiteLLMGateway:
        gateway = cls.__new__(cls)
        gateway._acompletion = acompletion
        return gateway

    async def complete_structured(
        self,
        request: ModelRequest,
        schema: type[_StructuredModel],
    ) -> StructuredCompletion[_StructuredModel]:
        try:
            params = deepcopy(dict(request.params))
            params["stream"] = False
            params["response_format"] = schema
            response = await self._acompletion(
                model=request.model,
                messages=deepcopy(list(request.messages)),
                tools=deepcopy(list(request.tools)),
                **params,
            )
        except Exception as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "structured model call failed",
                retryable=False,
            ) from error

        try:
            choices = _value(response, "choices")
            if (
                not isinstance(choices, (list, tuple))
                or not choices
            ):
                raise ValueError("choices missing")
            message = _value(choices[0], "message")
            if message is None:
                raise ValueError("message missing")
            parsed = _value(message, "parsed")
            if isinstance(parsed, BaseModel):
                value = schema.model_validate(parsed.model_dump(mode="python"))
            elif isinstance(parsed, Mapping):
                value = schema.model_validate(deepcopy(dict(parsed)))
            elif parsed is not None:
                raise ValueError("parsed response has unsupported type")
            else:
                content = _value(message, "content")
                if not isinstance(content, str) or not content:
                    raise ValueError("content missing")
                value = schema.model_validate_json(content)
            raw_usage = _value(response, "usage")
            usage = UsageReported(
                prompt_tokens=_optional_int(
                    _value(raw_usage, "prompt_tokens")
                    if raw_usage is not None
                    else None
                ),
                completion_tokens=_optional_int(
                    _value(raw_usage, "completion_tokens")
                    if raw_usage is not None
                    else None
                ),
                total_tokens=_optional_int(
                    _value(raw_usage, "total_tokens")
                    if raw_usage is not None
                    else None
                ),
            )
        except Exception as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "structured model response invalid",
                retryable=False,
            ) from error
        return StructuredCompletion(parsed=value, usage=usage)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        response = await self._acompletion(
            model=request.model,
            messages=deepcopy(list(request.messages)),
            tools=deepcopy(list(request.tools)),
            stream=True,
            **dict(request.params),
        )
        finish_reason: str | None = None
        usage: UsageReported | None = None
        tool_calls: dict[int, _ToolCallParts] = {}

        async for chunk in response:
            choices = _value(chunk, "choices")
            if choices:
                choice = choices[0]
                delta = _value(choice, "delta")
                content = _value(delta, "content") if delta is not None else None
                if isinstance(content, str) and content:
                    yield TextDelta(content)
                raw_tool_calls = (
                    _value(delta, "tool_calls") if delta is not None else None
                )
                if raw_tool_calls:
                    for raw_tool_call in raw_tool_calls:
                        index = int(_value(raw_tool_call, "index") or 0)
                        parts = tool_calls.setdefault(index, _ToolCallParts())
                        call_id = _value(raw_tool_call, "id")
                        if isinstance(call_id, str):
                            parts.call_id += call_id
                        function = _value(raw_tool_call, "function")
                        if function is not None:
                            name = _value(function, "name")
                            if isinstance(name, str):
                                parts.name += name
                            arguments = _value(function, "arguments")
                            if isinstance(arguments, str):
                                parts.arguments_json += arguments
                current_finish_reason = _value(choice, "finish_reason")
                if current_finish_reason is not None:
                    finish_reason = str(current_finish_reason)

            raw_usage = _value(chunk, "usage")
            if raw_usage is not None:
                usage = UsageReported(
                    prompt_tokens=_optional_int(_value(raw_usage, "prompt_tokens")),
                    completion_tokens=_optional_int(_value(raw_usage, "completion_tokens")),
                    total_tokens=_optional_int(_value(raw_usage, "total_tokens")),
                )

        for index in sorted(tool_calls):
            parts = tool_calls[index]
            yield ToolCallCompleted(
                index=index,
                call_id=parts.call_id,
                name=parts.name,
                arguments_json=parts.arguments_json,
            )
        if usage is not None:
            yield usage
        yield ModelCompleted(finish_reason)
