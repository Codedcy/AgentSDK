from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agent_sdk.context.models import ContextView
from agent_sdk.context.planner import ContextPlanner
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.models.litellm_gateway import LiteLLMGateway
from agent_sdk.prompts.composer import PromptComposer
from agent_sdk.prompts.models import BuiltPrompt
from agent_sdk.prompts.persistence import PromptManifestPersistence
from agent_sdk.runtime.models import RunSnapshot
from agent_sdk.runtime.reconciliation import RunCheckpoint
from agent_sdk.skills.registry import SkillRegistry
from agent_sdk.storage.base import StateStore


@dataclass(frozen=True)
class PreparedContext:
    view: ContextView
    messages: tuple[dict[str, Any], ...]
    prompt: BuiltPrompt


class ContextMiddleware:
    def __init__(
        self,
        store: StateStore,
        models: LiteLLMGateway,
        skills: SkillRegistry,
    ) -> None:
        self._store = store
        self._models = models
        self._skills = skills
        self._prompts = PromptComposer()
        self._persistence = PromptManifestPersistence(store)

    async def prepare(
        self,
        *,
        run: RunSnapshot,
        checkpoint: RunCheckpoint,
        tools: tuple[dict[str, Any], ...],
    ) -> PreparedContext:
        descriptor = run.execution_descriptor
        if descriptor is None:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "run execution descriptor is required for context",
                retryable=False,
            )
        agent = descriptor.agent
        config = agent.context
        planner = ContextPlanner(
            self._store,
            self._models,
            model=agent.model,
            model_window=config.model_window,
            output_reserve=config.output_reserve,
            safety_reserve=config.safety_reserve,
            policy=config.policy,
            recent_messages=config.recent_messages,
            tool_preview_bytes=config.tool_preview_bytes,
        )
        planned = await planner.prepare(
            session_id=run.session_id,
            run_id=run.run_id,
            checkpoint=checkpoint,
            config=config,
        )
        activated = tuple(self._skills.activate(name) for name in agent.skills)
        prompt = self._prompts.compose(
            profile=agent.prompt_profile,
            application=agent.system_prompt,
            skills=activated,
            context_view=planned.view,
            model=agent.model,
            tools=tools,
        )
        await self._persistence.persist(
            prompt.manifest,
            session_id=run.session_id,
        )
        prompt_messages = tuple(
            deepcopy(dict(message))
            for message in prompt.messages
        )
        context_messages = tuple(
            deepcopy(dict(message))
            for message in planned.messages
        )
        return PreparedContext(
            view=planned.view,
            messages=(*prompt_messages, *context_messages),
            prompt=prompt,
        )


__all__ = ["ContextMiddleware", "PreparedContext"]
