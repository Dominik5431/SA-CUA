"""
OpenAI-compatible QLLM provider implementation (vLLM / OpenAI / OpenRouter).

Mirrors qllm_claude.py's ClaudeQLLMCaller interface
(`.call(system_prompt, user_prompt) -> (text, usage_dict)`) so
QLLMExecutor.analyze_html doesn't need to know which backend the
Quarantined Model runs on. Lets the Q-Model (like the main ucm_agent) run
against a local vLLM server instead of requiring Anthropic — see
mm_agents/ucm_agent/agent.py's module docstring and
mm_agents/sa_agent/llm.py's build_client for the same convention.
"""

from typing import Tuple


class OpenAIQLLMCaller:
    """
    Calls an OpenAI-compatible Chat Completions endpoint (vLLM/OpenAI/
    OpenRouter) for quarantined LLM analysis.
    """

    def __init__(self, openai_client, model: str, debug: bool = False):
        """
        Args:
            openai_client: an already-constructed `openai.OpenAI(...)` client
                (see mm_agents.ucm_agent.agent.build_provider_client)
            model: model name to pass to `chat.completions.create`
            debug: enable debug logging
        """
        self.client = openai_client
        self.model = model
        self.debug = debug

    def call(self, system_prompt: str, user_prompt: str) -> Tuple[str, dict]:
        """
        Call the model and return raw text response with token usage.

        Returns:
            Tuple of (text_response, usage_dict). usage_dict keys are
            normalized to match ClaudeQLLMCaller's ("input_tokens",
            "output_tokens") so the quarantined_llm.jsonl audit log has a
            consistent shape regardless of backend.
        """
        if self.debug:
            print(f"[OpenAIQLLMCaller] Calling model: {self.model}")
            print(f"[OpenAIQLLMCaller] System prompt length: {len(system_prompt)} chars")
            print(f"[OpenAIQLLMCaller] User prompt length: {len(user_prompt)} chars")

        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=500,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        result_text = response.choices[0].message.content or ""

        usage = {}
        if response.usage:
            usage["input_tokens"] = response.usage.prompt_tokens
            usage["output_tokens"] = response.usage.completion_tokens

        return result_text.strip(), usage
