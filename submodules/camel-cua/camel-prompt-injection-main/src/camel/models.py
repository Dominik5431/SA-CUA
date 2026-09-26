import os
import time
from pathlib import Path

import anthropic
import openai
from agentdojo import agent_pipeline, functions_runtime
from agentdojo.agent_pipeline.agent_pipeline import load_system_message
from agentdojo.models import MODEL_NAMES
from google import genai
from openai.types.chat import ChatCompletionReasoningEffort
from pydantic_ai.models import KnownModelName

from camel_osworld.osworld_sys_usr_prompt import (
    default_system_prompt_generator_osworld_pllm,
    baseline_system_prompt_generator_osworld_pllm,
)
from camel.interpreter.interpreter import MetadataEvalMode
from camel.pipeline_elements.anthropic_tool_filter import AnthropicLLMToolFilter
from camel.pipeline_elements.privileged_llm import PrivilegedLLM
from camel.pipeline_elements.replay_privileged_llm import PrivilegedLLMReplayer, UserInjectionTasksGetter
from camel.pipeline_elements.security_policies import (
    ADNoSecurityPolicyEngine,
    AgentDojoSecurityPolicyEngine,
    BankingSecurityPolicyEngine,
    SlackSecurityPolicyEngine,
    TravelSecurityPolicyEngine,
    WorkspaceSecurityPolicyEngine,
)
from camel_osworld.osworld_security_policies import OSWorldSecurityPolicyEngine

_thinking_efforts = ["low", "medium", "high"]
_oai_thinking_models = {
    "o4-mini-2025-04-16": "ChatGPT",
    "o4-mini": "ChatGPT",
    "o3-2025-04-16": "ChatGPT",
    "o3-mini-2025-01-31": "ChatGPT",
    "o1-2024-12-17": "ChatGPT",
    "codex-mini-latest": "ChatGPT",
}
_oai_thinking_models_with_effort = {
    f"{model}-{effort}": name for model, name in _oai_thinking_models.items() for effort in _thinking_efforts
}
_supported_model_names = {
    "gpt-oss-20b": "OpenAI GPT-OSS",
    "gpt-oss-120b": "OpenAI GPT-OSS",
    "gemini-2.5-flash": "AI model developed by Google",
    "gemini-2.5-flash-preview-05-20": "AI model developed by Google",
    "gemini-2.5-pro-preview-06-05": "AI model developed by Google",
    "gemini-2.5-pro": "AI model developed by Google",
    "gemini-2.0-flash-lite-001": "AI model developed by Google",
    "gemini-3-pro-preview": "AI model developed by Google",
    "claude-3-5-haiku-20241022": "Claude",
    "claude-4-5-haiku-20250805": "Claude",
    "claude-3-5-sonnet-20241022": "Claude",
    "claude-3-7-sonnet-20250219": "Claude",
    "claude-sonnet-4-20250514": "Claude",
    "claude-sonnet-4-5-20250929": "Claude",
    "claude-opus-4-20250514": "Claude",
    "claude-opus-4-1-20250805": "Claude",
    "gpt-4o-2024-08-06": "GPT-4",
    "gpt-4o-mini-2024-07-18": "GPT-4",
    "gpt-4.1-2025-04-14": "ChatGPT",
    "gpt-4.1-nano-2025-04-14": "ChatGPT",
    "gpt-5": "ChatGPT",
    "gpt-5.1": "ChatGPT",
    "kimi-k2-thinking": "Kimi",
    "grok-4": "Grok",
    "deepseek-r1-0528": "DeepSeek",
    "deepseek-r1": "DeepSeek",
} | _oai_thinking_models_with_effort
suffixes = ["", "+camel", "+camel+secpol", "+camel+secpol+strict"]

CAMEL_MODEL_NAMES = {f"{model}{suffix}": name for model, name in _supported_model_names.items() for suffix in suffixes}

_SECURITY_POLICY_ENGINES: dict[str, type[AgentDojoSecurityPolicyEngine]] = {
    "workspace": WorkspaceSecurityPolicyEngine,
    "travel": TravelSecurityPolicyEngine,
    "banking": BankingSecurityPolicyEngine,
    "slack": SlackSecurityPolicyEngine,
    "osworld": OSWorldSecurityPolicyEngine,
    "redteamcua": OSWorldSecurityPolicyEngine
}


def _is_oai_reasoning_model(model: str) -> bool:
    return "o4" in model or "o3" in model or "o1" in model or "codex" in model


MODEL_NAMES.update(CAMEL_MODEL_NAMES)


class Sleep(agent_pipeline.BasePipelineElement):
    def __init__(self, amount: int) -> None:
        super().__init__()
        self._amount = amount

    def query(
        self,
        query: str,
        runtime,
        env=functions_runtime.EmptyEnv(),
        messages=[],
        extra_args={},
    ) -> tuple:
        if self._amount > 0:
            time.sleep(self._amount)
        return query, runtime, env, messages, extra_args


def make_tools_pipeline(
    model: KnownModelName,
    use_original: bool,
    replay_with_policies: bool,
    attack_name: str,
    reasoning_effort: ChatCompletionReasoningEffort,
    thinking_budget_tokens: int | None,
    suite: str,
    ad_defense: str | None,
    eval_mode: MetadataEvalMode,
    q_llm: KnownModelName | None,
    part_path: Path = Path(),
    max_attempts: int = 10,
    pllm_exposure_level: int = 0,
    system_defense_level: int = 2,
    unoptimized_cua: bool = False,
    plan_analysis_only: bool = False,
) -> agent_pipeline.AgentPipeline:
    if "openrouter:" in model:
        # model string should be like "openrouter:openai/gpt-5"
        router_model = model.split(":")[1]

        client = openai.OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )
        # OSS models use the standard Chat Completions interface (no reasoning_effort)
        llm = agent_pipeline.OpenAILLM(client, router_model, None)
    elif "google:" in model:
        # vertexai.init(project=os.getenv("GCP_PROJECT"), location=os.getenv("GCP_LOCATION"))
        # llm = GoogleLLM(model.split(":")[1])
        # client = genai.Client(vertexai=True, project=os.getenv("GCP_PROJECT"), location=os.getenv("GCP_LOCATION"))
        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        if model == "google:gemini-2.0-flash-lite-001":
            max_tokens = 8192
        else:
            max_tokens = 65535
        llm = agent_pipeline.GoogleLLM(model.split(":")[1], client, max_tokens=max_tokens)
    elif "openai:" in model:
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        # reasoning models do not support temperature and their "system" message is called "developer" message
        if _is_oai_reasoning_model(model):
            llm = agent_pipeline.OpenAILLM(client, model.split(":")[1], reasoning_effort, None)
        else:
            llm = agent_pipeline.OpenAILLM(client, model.split(":")[1], None)
    elif "anthropic:" in model:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        if thinking_budget_tokens:
            max_tokens = 8192 + thinking_budget_tokens
        else:
            max_tokens = 8192
        llm = agent_pipeline.AnthropicLLM(
            client, model.split(":")[1], thinking_budget_tokens=thinking_budget_tokens, max_tokens=max_tokens
        )
    elif "vllm:" in model:
        vllm_port = os.getenv("VLLM_PORT", "8000")
        vllm_base_url = os.getenv("VLLM_BASE_URL", f"http://localhost:{vllm_port}/v1")
        client = openai.OpenAI(
            base_url=vllm_base_url,
            api_key=os.getenv("VLLM_API_KEY", "EMPTY"),
        )
        llm = agent_pipeline.OpenAILLM(client, model.split(":")[1], None)
    else:
        raise ValueError("Invalid model")

    llm.name = model.split(":")[1]

    engine = _SECURITY_POLICY_ENGINES[suite]

    if suite in ("osworld", "redteamcua"):
        osworld_prompt_gen = (
            baseline_system_prompt_generator_osworld_pllm
            if unoptimized_cua
            else default_system_prompt_generator_osworld_pllm
        )
        # Need to add add env and stuff on init vlm somewhere but maybe has to do with tasksuite? uitars will not be llm or q_llm so different init needed
        tools_pipeline = agent_pipeline.AgentPipeline(
            [
                # Adds the user query to the history
                agent_pipeline.InitQuery(),
                # Generates the code and writes it to `extra_args`
                PrivilegedLLM(
                    llm,
                    OSWorldSecurityPolicyEngine,
                    q_llm or model,
                    osworld_prompt_gen,
                    part_path=part_path,
                    max_attempts = max_attempts,
                    pllm_exposure_level = pllm_exposure_level,
                    system_defense_level=system_defense_level,
                    plan_analysis_only=plan_analysis_only,
                ),

            ]
        )
        # Used for logging
        if "openai" in model and _is_oai_reasoning_model(model):
            tools_pipeline.name = f"{model.split(':')[1]}-{reasoning_effort}+camel"
        elif "anthropic" in model and (("3-7-sonnet" in model or "sonnet-4" in model) and thinking_budget_tokens):
            tools_pipeline.name = f"{model.split(':')[1]}-{thinking_budget_tokens}+camel"
        else:
            tools_pipeline.name = f"{model.split(':')[1]}+camel"

        if unoptimized_cua:
            tools_pipeline.name += "+unopt"

        if plan_analysis_only:
            tools_pipeline.name += "+plan_only"

        if q_llm:
            tools_pipeline.name += f"-q:{q_llm.split(':')[1]}"
    elif use_original:
        if "exp" in model:
            print("Adding 'Sleep' pipeline element.")
            tools_loop = agent_pipeline.ToolsExecutionLoop([agent_pipeline.ToolsExecutor(), Sleep(6), llm])
        else:
            tools_loop = agent_pipeline.ToolsExecutionLoop([agent_pipeline.ToolsExecutor(), llm])

        tools_pipeline = agent_pipeline.AgentPipeline(
            [agent_pipeline.SystemMessage(load_system_message(None)), agent_pipeline.InitQuery(), llm, tools_loop]
        )

        if ad_defense == "tool_filter" and isinstance(llm, agent_pipeline.AnthropicLLM):
            tools_pipeline = agent_pipeline.AgentPipeline(
                [
                    agent_pipeline.SystemMessage(load_system_message(None)),
                    agent_pipeline.InitQuery(),
                    AnthropicLLMToolFilter(llm.client, llm.name),
                    llm,
                    tools_loop,
                ]
            )
        elif ad_defense is not None:
            tools_pipeline = agent_pipeline.AgentPipeline.from_config(
                agent_pipeline.PipelineConfig(
                    llm=llm, defense=ad_defense, system_message_name=None, system_message=None
                )
            )

        if _is_oai_reasoning_model(model):
            tools_pipeline.name = f"{model.split(':')[1]}-{reasoning_effort}"
        elif "anthropic" in model and (("3-7-sonnet" in model or "sonnet-4" in model) and thinking_budget_tokens):
            tools_pipeline.name = f"{model.split(':')[1]}-{thinking_budget_tokens}"
        else:
            tools_pipeline.name = model.split(":")[1]
        if ad_defense is not None:
            tools_pipeline.name += f"+{ad_defense}"

    elif replay_with_policies:
        if _is_oai_reasoning_model(model):
            pipeline_name = f"{model.split(':')[1]}-{reasoning_effort}+camel"
        else:
            pipeline_name = f"{model.split(':')[1]}+camel"
        tools_pipeline = agent_pipeline.AgentPipeline(
            [
                agent_pipeline.InitQuery(),
                UserInjectionTasksGetter(),
                PrivilegedLLMReplayer(pipeline_name, attack_name, engine, eval_mode),
            ]
        )
        # Used for logging
        if "openai" in model and _is_oai_reasoning_model(model):
            tools_pipeline.name = f"{model.split(':')[1]}-{reasoning_effort}+camel+secpol"
        elif "anthropic" in model and (("3-7-sonnet" in model or "sonnet-4" in model) and thinking_budget_tokens):
            tools_pipeline.name = f"{model.split(':')[1]}-{thinking_budget_tokens}+camel+secpol"
        else:
            tools_pipeline.name = f"{model.split(':')[1]}+camel+secpol"
        if eval_mode == MetadataEvalMode.STRICT:
            tools_pipeline.name += "+strict"
    else:
        tools_pipeline = agent_pipeline.AgentPipeline(
            [
                # Adds the user query to the history
                agent_pipeline.InitQuery(),
                # Generates the code and writes it to `extra_args`
                PrivilegedLLM(
                    llm,
                    ADNoSecurityPolicyEngine,
                    q_llm or model,
                    part_path=part_path,
                    max_attempts=max_attempts,
                    plan_analysis_only=plan_analysis_only,
                ),
            ]
        )
        # Used for logging
        if "openai" in model and _is_oai_reasoning_model(model):
            tools_pipeline.name = f"{model.split(':')[1]}-{reasoning_effort}+camel"
        elif "anthropic" in model and (("3-7-sonnet" in model or "sonnet-4" in model) and thinking_budget_tokens):
            tools_pipeline.name = f"{model.split(':')[1]}-{thinking_budget_tokens}+camel"
        else:
            tools_pipeline.name = f"{model.split(':')[1]}+camel"

        if plan_analysis_only:
            tools_pipeline.name += "+plan_only"

        if q_llm:
            tools_pipeline.name += f"-q:{q_llm.split(':')[1]}"

    return tools_pipeline
