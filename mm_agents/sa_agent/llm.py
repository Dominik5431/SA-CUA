from __future__ import annotations

import base64
import logging
import os

import httpx
import openai
from anthropic import AnthropicBedrock
from openai import AzureOpenAI
from mm_agents.openrouter import SLIDING, apply_prompt_cache_policy

logger = logging.getLogger("desktopenv.SA")


def encode_screenshot(screenshot_bytes: bytes) -> str:
    """Base64-encode raw screenshot PNG bytes for embedding in messages."""
    return base64.b64encode(screenshot_bytes).decode("utf-8")


class _LLMClient:
    """
    Thin provider-agnostic wrapper for LLM calls.
    Supports text-only and multimodal (vision) messages in OpenAI message format.
    Image content blocks use the OpenAI image_url format; AWS Anthropic conversion
    is handled internally.
    """

    def __init__(self, provider: str, model: str, client):
        self.provider = provider
        self.model = model
        self._client = client

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        """
        Call the model with an OpenAI-format messages list, return the response text.
        Messages may include image_url content blocks for vision providers.
        """
        if self.provider in ("openai", "azure", "vllm", "openrouter"):
            return self._chat_openai(messages, max_tokens, temperature)
        elif self.provider == "aws":
            return self._chat_bedrock(messages, max_tokens, temperature)
        else:
            raise ValueError(f"Unsupported provider: {self.provider!r}")

    def _chat_openai(self, messages, max_tokens, temperature) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        return response.choices[0].message.content or ""

    def _chat_bedrock(self, messages, max_tokens, temperature) -> str:
        """Call AWS Bedrock (Anthropic Claude) with converted message format."""
        claude_messages = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            converted_parts = []
            for part in content:
                if part.get("type") == "image_url":
                    url: str = part["image_url"]["url"]
                    b64_data = url.replace("data:image/png;base64,", "")
                    converted_parts.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64_data,
                        },
                    })
                else:
                    converted_parts.append(part)
            claude_messages.append({"role": msg["role"], "content": converted_parts})

        if claude_messages and claude_messages[0]["role"] == "system":
            system_block = claude_messages[0]["content"][0]
            claude_messages[1]["content"].insert(0, system_block)
            claude_messages.pop(0)

        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=claude_messages,
        )
        return response.content[0].text


def build_client(model_str: str, vllm_port: int|None = None,
                 history: str = SLIDING) -> _LLMClient:
    """
    Parse a "provider|model_name" string and return a ready-to-use _LLMClient.
    Supported providers: openai, azure, vllm, openrouter, aws (Anthropic Bedrock).

    `history` is the OpenRouter prompt-cache policy for this client and is ignored by every
    other provider -- SLIDING (default, an agent loop re-sending a trajectory window), GROWING
    (an append-only planner history), or ALWAYS (not a loop; every request is a prefix
    extension or an exact repeat). See mm_agents/openrouter.py.
    """
    parts = [p.strip() for p in model_str.split("|", 1)]
    if len(parts) != 2:
        raise ValueError(
            f"model_str must be 'provider|model_name', got: {model_str!r}"
        )
    provider, model_name = parts[0].lower(), parts[1]

    if provider == "vllm":
        if vllm_port is None:
            vllm_port = os.getenv("VLLM_PORT", "8000")
        vllm_base_url = os.getenv("VLLM_BASE_URL", f"http://localhost:{vllm_port}/v1")
        vllm_timeout = float(os.getenv("VLLM_TIMEOUT", "120"))
        client = openai.OpenAI(
            base_url=vllm_base_url,
            api_key=os.getenv("VLLM_API_KEY", "EMPTY"),
            timeout=httpx.Timeout(vllm_timeout, read=vllm_timeout,
                                  write=vllm_timeout, connect=10),
        )

    elif provider == "openai":
        client = openai.OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            timeout=httpx.Timeout(60, read=60, write=60, connect=10),
        )

    elif provider == "azure":
        client = AzureOpenAI(
            api_key=os.getenv("AZURE_API_KEY"),
            api_version=os.getenv("AZURE_API_VERSION"),
            azure_endpoint=os.getenv("AZURE_ENDPOINT"),
            timeout=httpx.Timeout(60, read=60, write=60, connect=10),
        )

    elif provider == "openrouter":
        client = apply_prompt_cache_policy(openai.OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            timeout=httpx.Timeout(120, read=120, write=120, connect=10),
        ), history=history)

    elif provider == "aws":
        client = AnthropicBedrock(
            aws_region=os.getenv("AWS_REGION"),
            aws_access_key=os.getenv("AWS_ACCESS_KEY"),
            aws_secret_key=os.getenv("AWS_SECRET_KEY"),
        )

    else:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            "Supported: openai, azure, vllm, openrouter, aws"
        )

    return _LLMClient(provider=provider, model=model_name, client=client)
