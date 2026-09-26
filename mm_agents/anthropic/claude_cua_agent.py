"""Adapts Claude CUA from OSWorld-V2.0 for RedTeamCUA compatibility."""

import logging
import os

from mm_agents.anthropic import AnthropicAgent
from mm_agents.anthropic.utils import APIProvider

logger = logging.getLogger("desktopenv.agent")

_SPECIAL_ACTIONS = {"WAIT", "FAIL", "DONE"}

_PROVIDER_MAP = {
    "anthropic": APIProvider.ANTHROPIC,
}


class ClaudeCUAAgent:
    def __init__(
        self,
        platform: str = "Ubuntu",
        model: str = "claude-sonnet-5",
        provider: str = "anthropic",
        max_tokens: int = 4096,
        api_key: str = None,
        system_prompt_suffix: str = "",
        only_n_most_recent_images: int = 10,
        action_space: str = "claude_computer_use",
        screen_width: int = 1920,
        screen_height: int = 1080,
        no_thinking: bool = False,
        use_isp: bool = False,
        effort: str = "max",
        temperature: float = None,
        top_p: float = None,
        max_steps: int = 15,
        **kwargs,
    ):
        if provider not in _PROVIDER_MAP:
            raise ValueError(
                f"Unknown Anthropic provider {provider!r}; expected one of "
                f"{sorted(_PROVIDER_MAP)}"
            )

        self.action_space = action_space

        self.cua = False

        self._agent = AnthropicAgent(
            platform=platform,
            model=model,
            provider=_PROVIDER_MAP[provider],
            max_tokens=max_tokens,
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            system_prompt_suffix=system_prompt_suffix,
            only_n_most_recent_images=only_n_most_recent_images,
            action_space=action_space,
            screen_size=(screen_width, screen_height),
            no_thinking=no_thinking,
            use_isp=use_isp,
            effort=effort,
            temperature=temperature,
            top_p=top_p,
            max_steps=max_steps,
        )

    def predict(self, instruction, obs):
        reasonings, raw_actions = self._agent.predict(instruction, obs)

        actions = []
        for action in raw_actions or []:
            if isinstance(action, dict):
                action_type = action.get("action_type")
                if action_type in _SPECIAL_ACTIONS:
                    actions.append(action_type)
                elif action.get("command"):
                    actions.append(action["command"])
                else:
                    logger.warning("Dropping unexecutable Claude action: %r", action)
            else:
                actions.append(action)

        return reasonings or "", actions, bool(raw_actions)

    def reset(self, tags=None, _logger=None):
        self._agent.reset(_logger=_logger)
