
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from osworld.mm_agents.agent import PromptAgent
from mm_agents.openrouter import NO_PROMPT_CACHE


import os
from pathlib import Path

logger = logging.getLogger("desktopenv.agent")


class PromptAgentWithTokenCount(PromptAgent):
    """
    Extended PromptAgent that tracks input and output token usage across different LLM providers.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.token_log_path = None
    
    def call_llm_with_token_count(self, payload, token_log_path, function_name="unknown"):
        """
        Call LLM and log token usage to a file.
        
        Args:
            payload: The payload to send to the LLM
            token_log_path: Path to the file where token counts will be logged
            function_name: Name of the calling function for tracking purposes
            
        Returns:
            The LLM response content (same as call_llm)
        """
        self.token_log_path = token_log_path
        
        input_tokens = 0
        output_tokens = 0
        model_used = payload.get('model', self.model)
        
        if model_used.startswith("azure-gpt-4o") or self.model.startswith("gpt"):
            response_content = self._call_openai_with_tokens(payload, model_used, function_name)
            return response_content
            
        elif self.model.startswith("claude"):
            response_content = self._call_claude_with_tokens(payload, function_name)
            return response_content
            
        elif self.model.startswith("gemini"):
            response_content = self._call_gemini_with_tokens(payload, function_name)
            return response_content
            
        elif self.model.startswith("openrouter"):
            response_content = self._call_openrouter_with_tokens(payload, function_name)
            return response_content

        elif self.model.startswith("vllm"):
            response_content = self._call_vllm_with_tokens(payload, function_name)
            return response_content

        else:
            response_content = self.call_llm(payload)
            self._log_tokens(0, 0, model_used, function_name)
            return response_content
    
    def _call_openai_with_tokens(self, payload, model_used, function_name):
        """Handle OpenAI/Azure OpenAI calls with token tracking."""
        import requests
        from dotenv import load_dotenv
        
        if model_used.startswith("azure-gpt-4o"):
            load_dotenv()
            api_key = os.getenv('AZURE_OPENAI_API_KEY')
            endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
            headers = {
                "Content-Type": "application/json",
                "api-key": api_key
            }
            url = endpoint
        else:
            base_url = os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com')
            url = f"{base_url}/chat/completions" if base_url.endswith('/v1') else f"{base_url}/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"
            }
        
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code == 200:
            response_json = response.json()
            content = response_json['choices'][0]['message']['content']
            
            usage = response_json.get('usage', {})
            input_tokens = usage.get('prompt_tokens', 0)
            output_tokens = usage.get('completion_tokens', 0)
            total_tokens = usage.get('total_tokens', input_tokens + output_tokens)
            
            self._log_tokens(input_tokens, output_tokens, total_tokens, model_used, function_name)
            return content
        else:
            if response.json().get('error', {}).get('code') == "context_length_exceeded":
                payload["messages"] = [payload["messages"][0]] + payload["messages"][-1:]
                return self._call_openai_with_tokens(payload, model_used, function_name)
            return ""
    
    def _call_claude_with_tokens(self, payload, function_name):
        """Handle Claude API calls with token tracking."""
        import requests
        
        messages = payload["messages"]
        max_tokens = payload["max_tokens"]
        top_p = payload["top_p"]
        temperature = payload["temperature"]
        
        claude_messages = []
        for message in messages:
            claude_message = {"role": message["role"], "content": []}
            for part in message["content"]:
                if part['type'] == "image_url":
                    image_source = {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": part['image_url']['url'].replace("data:image/png;base64,", "")
                    }
                    claude_message['content'].append({"type": "image", "source": image_source})
                elif part['type'] == "text":
                    claude_message['content'].append({"type": "text", "text": part['text']})
            claude_messages.append(claude_message)
        
        if claude_messages[0]['role'] == "system":
            claude_messages[1]['content'].insert(0, claude_messages[0]['content'][0])
            claude_messages.pop(0)
        
        headers = {
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        
        api_payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": claude_messages,
            "temperature": temperature,
            "top_p": top_p
        }
        
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=api_payload
        )
        
        if response.status_code == 200:
            response_json = response.json()
            content = response_json['content'][0]['text']
            
            usage = response_json.get('usage', {})
            input_tokens = usage.get('input_tokens', 0)
            output_tokens = usage.get('output_tokens', 0)
            total_tokens = input_tokens + output_tokens
            
            self._log_tokens(input_tokens, output_tokens, total_tokens, self.model, function_name)
            return content
        else:
            return ""
    
    def _call_gemini_with_tokens(self, payload, function_name):
        """Handle Gemini API calls with token tracking."""
        import google.generativeai as genai
        
        response_content = self.call_llm(payload)
        
        approx_input_tokens = sum(len(str(m.get('content', ''))) for m in payload.get('messages', [])) // 4
        approx_output_tokens = len(response_content) // 4
        approx_total_tokens = approx_input_tokens + approx_output_tokens
        
        self._log_tokens(approx_input_tokens, approx_output_tokens, approx_total_tokens, 
                        self.model, function_name)
        
        return response_content
    
    def _call_openrouter_with_tokens(self, payload, function_name):
        """Handle OpenRouter API calls with token tracking."""
        import requests
        
        messages = payload["messages"]
        max_tokens = payload["max_tokens"]
        top_p = payload["top_p"]
        temperature = payload["temperature"]
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"
        }
        
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json={
                "model": self.model.removeprefix("openrouter:"),
                "messages": messages,
                "max_tokens": max_tokens,
                "top_p": top_p,
                "temperature": temperature,
                **NO_PROMPT_CACHE,
            }
        )
        
        if response.status_code == 200:
            response_json = response.json()
            content = response_json['choices'][0]['message']['content']
            
            usage = response_json.get('usage', {})
            input_tokens = usage.get('prompt_tokens', 0)
            output_tokens = usage.get('completion_tokens', 0)
            total_tokens = usage.get('total_tokens', input_tokens + output_tokens)
            
            self._log_tokens(input_tokens, output_tokens, total_tokens, self.model, function_name)
            return content
        else:
            return ""
    
    def _call_vllm_with_tokens(self, payload, function_name):
        """Handle local vLLM (OpenAI-compatible) calls with token tracking."""
        import requests

        vllm_port = os.getenv("VLLM_PORT", "8000")
        vllm_base_url = os.getenv("VLLM_BASE_URL", f"http://localhost:{vllm_port}/v1")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.getenv('VLLM_API_KEY', 'EMPTY')}"
        }

        response = requests.post(
            f"{vllm_base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.model.removeprefix("vllm:"),
                "messages": payload["messages"],
                "max_tokens": payload["max_tokens"],
                "top_p": payload["top_p"],
                "temperature": payload["temperature"],
            }
        )

        if response.status_code == 200:
            response_json = response.json()
            choice = response_json['choices'][0]
            message = choice['message']
            content = message['content']

            finish_reason = choice.get('finish_reason')
            if content is None:
                raise RuntimeError(
                    f"{function_name}: vLLM returned no content (finish_reason="
                    f"{finish_reason!r}) -- likely truncated mid-reasoning by "
                    f"max_tokens={payload.get('max_tokens')}."
                )

            reasoning_content = message.get('reasoning_content') or message.get('reasoning')
            if reasoning_content:
                logger.info(f"{function_name} Reasoning (reasoning_content): \n{reasoning_content}")
            if reasoning_content is None and content and "</think>" in content:
                logger.warning(
                    f"{function_name}: content contained an unparsed </think> tag -- "
                    "the vLLM reasoning parser is likely not enabled for this model; "
                    "stripping the leaked thinking block as a fallback."
                )
                content = content.split("</think>", 1)[1].lstrip("\n")

            usage = response_json.get('usage', {})
            input_tokens = usage.get('prompt_tokens', 0)
            output_tokens = usage.get('completion_tokens', 0)
            total_tokens = usage.get('total_tokens', input_tokens + output_tokens)

            self._log_tokens(input_tokens, output_tokens, total_tokens, self.model, function_name)
            return content
        else:
            return ""

    def _log_tokens(self, input_tokens, output_tokens, total_tokens, model_name, function_name):
        """
        Log token usage to the specified file in plain text format.
        
        Args:
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            total_tokens: Total number of tokens
            model_name: Model name used
            function_name: Name of the calling function
        """
        if self.token_log_path is not None:
            Path(self.token_log_path).parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.token_log_path, "a") as f:
                f.write(f"Model {model_name}, Function {function_name} - Input Tokens: {input_tokens}, Output Tokens: {output_tokens}, Total Tokens: {total_tokens}\n")


"""
agent = PromptAgentWithTokenCount(
    model="gpt-4-vision-preview",
    max_tokens=1500,
    temperature=0.5
)

payload = {
    "model": "gpt-4-vision-preview",
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Hello!"}]}
    ],
    "max_tokens": 1500,
    "temperature": 0.5,
    "top_p": 0.9
}

response = agent.call_llm_with_token_count(
    payload=payload,
    token_log_path="logs/token_usage.txt",
    function_name="test_function"
)

# Output in token_usage.txt will be:
# Model gpt-4-vision-preview, Function test_function - Input Tokens: 10, Output Tokens: 5, Total Tokens: 15
"""