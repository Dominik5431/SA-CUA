"""
Build functions for RedTeamCUA tasks.

Two builders cover all practical cases:
- build_ui_redteamcua_vllm   — any model served via vLLM (OpenAI-compatible API)
- build_ui_redteamcua_anthropic — Claude via the Anthropic SDK (computer-use tools)

Both create a RedTeamCUA DesktopEnv (provider_name="docker") so that
DesktopEnv.reset() runs the full setup + adversary injection phase.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import Field

from camel_osworld.osworld_benchmark.suites.base_ui_uitars import BaseUI
from camel_osworld.osworld_benchmark.suites.base_ui_anthropic import BaseUI_Anthropic
from camel_osworld.agent_pipeline_vlms.uitars_vlm import UITarsVLM
from camel_osworld.agent_pipeline_vlms.anthropic_vlm import ClaudeVLM


class _BaseUI_AnyEnv(BaseUI):
    """BaseUI subclass that accepts any DesktopEnv-compatible object.

    RedTeamCUA ships its own desktop_env.DesktopEnv that does not inherit
    from osworld.desktop_env.DesktopEnv, so pydantic's isinstance check on
    the parent's `env` field rejects it.  Widening to Any bypasses that check
    while keeping all BaseUI behaviour intact.
    """
    env: Any = Field(exclude=True, repr=False)
    model_config = {"arbitrary_types_allowed": True, "extra": "forbid"}


class _BaseUI_Anthropic_AnyEnv(BaseUI_Anthropic):
    """Same widening for the Anthropic pipeline."""
    env: Any = Field(exclude=True, repr=False)
    model_config = {"arbitrary_types_allowed": True, "extra": "forbid"}


def _make_redteamcua_env(path_to_vm: str | None):
    """Instantiate RedTeamCUA's DesktopEnv with the docker provider."""
    from desktop_env.desktop_env import DesktopEnv
    return DesktopEnv(
        provider_name="docker",
        path_to_vm=path_to_vm or "",
        action_space="pyautogui",
        screen_size=(1920, 1080),
        headless=True,
        os_type="Ubuntu",
        require_a11y_tree=True,
    )


def build_ui_redteamcua_vllm(
    vllm_port: int = 8000,
    vllm_model: str = "Qwen/Qwen3.5-9B",
    defense_level: int = 0,
    q_llm: str | None = None,
    q_llm_second_check: str | None = None,
    path_to_vm: str | None = None,
    token_count_file: str | None = None,
) -> BaseUI:
    """
    Build a BaseUI backed by any vLLM-served model.

    Uses UITarsVLM as the VLM wrapper — it is a thin OpenAI-API call +
    Thought/Action parser that works with any instruction-following model
    when the system prompt (in BaseUI.run_single_uitars) guides the format.
    The vllm_port / vllm_model should match the serve_vllm.sh invocation.
    """
    import openai
    env = _make_redteamcua_env(path_to_vm)
    client = openai.OpenAI(
        base_url=f"http://localhost:{vllm_port}/v1",
        api_key=os.getenv("VLLM_API_KEY", "dummy"),
        timeout=300.0,
    )
    vlm = UITarsVLM(client=client, model=vllm_model, temperature=0.6, top_p=0.9)
    ui = _BaseUI_AnyEnv(env=env, vlm=vlm)
    ui.model_defense_level = defense_level
    _vllm_default = f"vllm:{vllm_model}"
    ui.q_llm = q_llm or _vllm_default
    ui.q_llm_second_check = q_llm_second_check or _vllm_default
    ui.defense_map = {i: getattr(ui, f"model_defense_{i}") for i in range(5)}
    ui.token_count_file = token_count_file
    return ui


def build_ui_redteamcua_anthropic(
    defense_level: int = 0,
    q_llm: str | None = None,
    q_llm_second_check: str | None = None,
    path_to_vm: str | None = None,
    path_to_vlm: str | None = "claude-sonnet-4-6",
    token_count_file: str | None = None,
) -> BaseUI_Anthropic:
    """
    Build a BaseUI_Anthropic backed by Claude via the Anthropic SDK.
    Uses Claude's native computer-use tool API rather than vLLM.
    """
    from anthropic import Anthropic
    env = _make_redteamcua_env(path_to_vm)
    vlm = ClaudeVLM(
        client=Anthropic(),
        model=path_to_vlm or "claude-sonnet-4-6",
    )
    ui = _BaseUI_Anthropic_AnyEnv(env=env, vlm=vlm)
    ui.model_defense_level = defense_level
    _vllm_default = "vllm:" + os.getenv("VLLM_MODEL", "Qwen/Qwen3.5-9B")
    ui.q_llm = q_llm or _vllm_default
    ui.q_llm_second_check = q_llm_second_check or _vllm_default
    ui.defense_map = {i: getattr(ui, f"model_defense_{i}") for i in range(5)}
    ui.token_count_file = token_count_file
    return ui
