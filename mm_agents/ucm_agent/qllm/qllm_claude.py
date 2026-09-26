"""
Claude-specific QLLM provider implementation.

Ported from the source UCM repo's src/agent/qllm/qllm_claude.py.
``log_quarantined_llm_usage`` from that version POSTed to a set of
hardcoded custom-website log endpoints (e.g. http://forum.com/logs/write)
that only exist in that repo's own demo sites — dropped here since
RedTeamCUA's platforms (Reddit/OwnCloud/RocketChat) have no such endpoint.
Local audit logging still happens via agent.py's quarantined_llm.jsonl.
"""

from typing import Tuple


class ClaudeQLLMCaller:
    """
    Calls Claude API for quarantined LLM analysis.

    Thin wrapper around the Anthropic Claude API that:
    1. Takes system and user prompts
    2. Calls Claude with appropriate settings
    3. Returns the raw text response
    """

    def __init__(self, anthropic_client, model: str = "claude-sonnet-4-5-20250929", debug: bool = False):
        """
        Args:
            anthropic_client: Initialized anthropic.Anthropic() client
            model: Claude model to use for quarantined analysis
            debug: Enable debug logging
        """
        self.client = anthropic_client
        self.model = model
        self.debug = debug

    def call(self, system_prompt: str, user_prompt: str) -> Tuple[str, dict]:
        """
        Call Claude API and return raw text response with token usage.

        Returns:
            Tuple of (text_response, usage_dict) where usage_dict contains
            input_tokens, output_tokens, and optional cache token fields.
        """
        if self.debug:
            print(f"[ClaudeQLLMCaller] Calling model: {self.model}")
            print(f"[ClaudeQLLMCaller] System prompt length: {len(system_prompt)} chars")
            print(f"[ClaudeQLLMCaller] User prompt length: {len(user_prompt)} chars")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=500,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": [{"type": "text", "text": user_prompt}]
            }]
        )

        result_text = ""
        for block in response.content:
            if block.type == "text":
                result_text += block.text + "\n"

        usage = {}
        if response.usage:
            usage["input_tokens"] = response.usage.input_tokens
            usage["output_tokens"] = response.usage.output_tokens
            for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                val = getattr(response.usage, field, None)
                if val is not None:
                    usage[field] = val

        return result_text.strip(), usage
