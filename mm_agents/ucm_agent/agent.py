"""
UCMAgent — the model client half of ucm_agent.

Adapted from the source UCM repo's src/agent/agent_claude_client.py +
src/agent/agent_base.py, restructured to match this repo's convention
(seen in mm_agents/fides): the screen-acting/tool-calling client itself
never touches `env` or `env.step()` — it only turns screenshots + tool
results into the next requested action, one turn at a time. The
env-owning `UCMLoop` (loop.py) drives the actual `env.step()` calls and
resolves `quarantined_llm_analysis` tool calls via BrowserGuard, mirroring
how mm_agents/fides/loop_and_planner.py dispatches `ToolCall`s: a
quarantined-read tool call consumes a turn but never an `env.step()`.

Multi-provider: like every other agent in this repo (see
mm_agents/prompt_agent/agent.py, mm_agents/sa_agent/llm.py), `model` is a
"<provider> | <model_name>" string — e.g. "anthropic | claude-sonnet-4-6" or
"vllm | Qwen/Qwen3.5-9B" (this repo's own --model default). Two backends:

  - "anthropic": native Anthropic tool-calling (mirrors the source UCM
    repo's own approach — `computer_use` and `quarantined_llm_analysis` are
    both plain custom tools via `client.messages.create(tools=[...])`, no
    "computer use" beta feature needed).
  - "vllm" / "openai" / "openrouter": OpenAI-compatible Chat Completions
    tool-calling (`client.chat.completions.create(tools=[...])`), pointed at
    a local vLLM server the same way mm_agents/sa_agent/llm.py's
    build_client() does (VLLM_PORT/VLLM_BASE_URL/VLLM_API_KEY env vars) —
    so any model already being served for the other agents in this repo
    (e.g. Qwen/Qwen3.5-9B) works here too, as long as vLLM was launched with
    tool-calling enabled (`--enable-auto-tool-choice
    --tool-call-parser hermes` for the Qwen family; see vLLM's tool-calling
    docs). Falls back to plain-text output (no tool call parsed) if the
    server doesn't support tools — see `_parse_openai_response`.

Messages are kept in a small provider-agnostic canonical form (see
`_image_block`/`push_user_message`/`submit_tool_result` below) and only
converted to each provider's actual wire format at call time in
`_to_anthropic_messages` / `_to_openai_messages` — `loop.py` never needs to
know which backend is in use.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mm_agents.openrouter import apply_prompt_cache_policy
from mm_agents.ucm_agent.qllm import QLLMTool
from mm_agents.ucm_agent import prompts as ucm_prompts

logger = logging.getLogger("desktopenv.agent")

OPENAI_COMPATIBLE_PROVIDERS = {"vllm", "openai", "openrouter"}


def _parse_model_string(model_str: str) -> Tuple[str, str]:
    """'<provider> | <model_name>' -> (provider, model_name), matching the
    convention used across this repo (mm_agents/prompt_agent/agent.py,
    mm_agents/sa_agent/llm.py's build_client). A bare model name with no
    '|' defaults to the "anthropic" provider for backward compatibility."""
    if "|" in model_str:
        provider, _, name = model_str.partition("|")
        return provider.strip().lower(), name.strip()
    return "anthropic", model_str.strip()


def build_provider_client(provider: str, api_key: Optional[str] = None):
    """Build a raw SDK client for `provider` (anthropic / vllm / openai /
    openrouter), using the same env-var conventions as
    mm_agents/sa_agent/llm.py's build_client (VLLM_PORT/VLLM_BASE_URL/
    VLLM_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, ANTHROPIC_API_KEY).
    Shared by UCMAgent (the main screen-acting model) and UCMLoop's
    Quarantined-Model client, so both honor the same provider string and
    the same vLLM server without duplicating the branching."""
    if provider == "anthropic":
        import anthropic
        return anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        import httpx
        import openai

        if provider == "vllm":
            vllm_port = os.getenv("VLLM_PORT", "8000")
            base_url = os.getenv("VLLM_BASE_URL", f"http://localhost:{vllm_port}/v1")
            return openai.OpenAI(
                base_url=base_url,
                api_key=api_key or os.getenv("VLLM_API_KEY", "EMPTY"),
                timeout=httpx.Timeout(120, read=120, write=120, connect=10),
            )
        if provider == "openrouter":
            return apply_prompt_cache_policy(openai.OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=api_key or os.getenv("OPENROUTER_API_KEY"),
                timeout=httpx.Timeout(120, read=120, write=120, connect=10),
            ))
        return openai.OpenAI(
            api_key=api_key or os.getenv("OPENAI_API_KEY"),
            timeout=httpx.Timeout(60, read=60, write=60, connect=10),
        )

    raise ValueError(
        f"[ucm_agent] Unknown provider {provider!r}. "
        f"Supported: anthropic, {', '.join(sorted(OPENAI_COMPATIBLE_PROVIDERS))}"
    )



_COMPUTER_USE_DESCRIPTION = (
    "Control the computer by performing actions like clicking, typing, "
    "scrolling, etc. Always look at the latest screenshot before deciding "
    "the next action."
)

_COMPUTER_USE_PARAMS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "object",
            "description": "The computer action to perform",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "click", "double_click", "scroll", "type", "keypress",
                        "wait", "move", "drag", "screenshot",
                        "goto", "back", "forward",
                    ],
                    "description": "The type of action to perform",
                },
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
                "button": {"type": "string", "description": "Mouse button: left, right, middle"},
                "text": {"type": "string", "description": "Text to type"},
                "keys": {"type": "array", "items": {"type": "string"}, "description": "Keys to press, e.g. ['ctrl','a']"},
                "scroll_x": {"type": "integer", "description": "Horizontal scroll amount"},
                "scroll_y": {"type": "integer", "description": "Vertical scroll amount (positive = up)"},
                "ms": {"type": "integer", "description": "Milliseconds to wait"},
                "path": {
                    "type": "array",
                    "description": "List of {x,y} points for a drag operation",
                    "items": {
                        "type": "object",
                        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    },
                },
                "url": {"type": "string", "description": "URL to navigate to (goto only)"},
            },
            "required": ["type"],
        }
    },
    "required": ["action"],
}


def _computer_use_tool_def_anthropic() -> Dict[str, Any]:
    return {"name": "computer_use", "description": _COMPUTER_USE_DESCRIPTION, "input_schema": _COMPUTER_USE_PARAMS}


def _computer_use_tool_def_openai() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": "computer_use", "description": _COMPUTER_USE_DESCRIPTION, "parameters": _COMPUTER_USE_PARAMS},
    }


@dataclass
class ToolUse:
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class Turn:
    """One parsed model response."""
    text: str = ""
    stop_reason: Optional[str] = None
    tool_use: Optional[ToolUse] = None
    task_complete: bool = False
    task_unsolvable: bool = False


class UCMAgent:
    """Model client + message history for ucm_agent.

    No `env`/computer access — see module docstring. `UCMLoop` drives it
    turn by turn via `push_user_message`, `next_turn`, and
    `submit_tool_result`.
    """

    QLLM_MODEL = "claude-sonnet-4-5-20250929"

    def __init__(
        self,
        model: str = "anthropic | claude-sonnet-4-5-20250929",
        allow_unsolvable: bool = False,
        max_tokens: int = 4096,
        api_key: Optional[str] = None,
        results_dir: Optional[str] = None,
    ):
        self.provider, self.model = _parse_model_string(model)
        self.allow_unsolvable = allow_unsolvable
        self.max_tokens = max_tokens
        self.action_space = "pyautogui"
        self.client = build_provider_client(self.provider, api_key)

        self.system_prompt = ucm_prompts.build_ucm_defense_prompt(allow_unsolvable=allow_unsolvable)
        if allow_unsolvable:
            self.system_prompt += ucm_prompts.UNSOLVABLE_INSTRUCTION

        self.messages: List[Dict[str, Any]] = []
        self._anthropic_tools = [_computer_use_tool_def_anthropic(), QLLMTool.get_claude_format()]
        self._openai_tools = [_computer_use_tool_def_openai(), QLLMTool.get_openai_chat_format()]

        self.safety_check_cnt = 0

        self.results_dir = Path(results_dir) if results_dir else None
        self._log_handle = None
        if self.results_dir:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            self._log_handle = open(self.results_dir / "ucm_thoughts.jsonl", "w", encoding="utf-8")


    def reset(self, tags=None, _logger=None, runtime_logger=None):
        global logger
        chosen_logger = _logger or runtime_logger
        if chosen_logger:
            logger = chosen_logger
        self.messages = []

    def _log(self, entry: Dict[str, Any]):
        if self._log_handle:
            self._log_handle.write(json.dumps(entry, default=str) + "\n")
            self._log_handle.flush()

    def close(self):
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None


    @staticmethod
    def image_block(screenshot_bytes: bytes) -> Dict[str, Any]:
        import base64
        return {"type": "image", "data": base64.b64encode(screenshot_bytes).decode("utf-8")}

    def push_user_message(self, instruction: str, screenshot_bytes: bytes):
        """First turn of the episode."""
        self.messages.append({
            "role": "user",
            "content": [self.image_block(screenshot_bytes), {"type": "text", "text": instruction}],
        })

    def submit_tool_result(self, tool_use_id: str, content_blocks: List[Dict[str, Any]]):
        """Feed back the result of a tool call (computer action or QLLM
        query) so the next `next_turn()` call continues the conversation."""
        self.messages.append({"role": "tool_result", "tool_use_id": tool_use_id, "content": content_blocks})

    def push_plain_user_message(self, text: str):
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})


    @staticmethod
    def _anthropic_content_block(block: Dict[str, Any]) -> Dict[str, Any]:
        if block["type"] == "image":
            return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": block["data"]}}
        return {"type": "text", "text": block["text"]}

    def _to_anthropic_messages(self) -> List[Dict[str, Any]]:
        out = []
        for msg in self.messages:
            role = msg["role"]
            if role in ("user", "assistant"):
                content = [self._anthropic_content_block(b) for b in msg["content"]]
                if role == "assistant" and msg.get("tool_call"):
                    tc = msg["tool_call"]
                    content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]})
                out.append({"role": role, "content": content})
            elif role == "tool_result":
                content = [self._anthropic_content_block(b) for b in msg["content"]]
                out.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": msg["tool_use_id"], "content": content}],
                })
        return out

    @staticmethod
    def _openai_content_block(block: Dict[str, Any]) -> Dict[str, Any]:
        if block["type"] == "image":
            return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{block['data']}"}}
        return {"type": "text", "text": block["text"]}

    def _to_openai_messages(self) -> List[Dict[str, Any]]:
        """OpenAI Chat Completions tool-role messages can't carry images, so
        a tool_result with an image block becomes a `role: tool` text-only
        message immediately followed by a `role: user` message carrying the
        screenshot — the model still sees it, just one message later."""
        out = [{"role": "system", "content": self.system_prompt}]
        for msg in self.messages:
            role = msg["role"]
            if role == "user":
                out.append({"role": "user", "content": [self._openai_content_block(b) for b in msg["content"]]})
            elif role == "assistant":
                text = "\n".join(b["text"] for b in msg["content"] if b["type"] == "text")
                entry = {"role": "assistant", "content": text or None}
                if msg.get("tool_call"):
                    tc = msg["tool_call"]
                    entry["tool_calls"] = [{
                        "id": tc["id"], "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                    }]
                out.append(entry)
            elif role == "tool_result":
                text_parts = [b["text"] for b in msg["content"] if b["type"] == "text"]
                image_parts = [b for b in msg["content"] if b["type"] == "image"]
                out.append({
                    "role": "tool",
                    "tool_call_id": msg["tool_use_id"],
                    "content": "\n".join(text_parts) or "(no text result)",
                })
                if image_parts:
                    out.append({
                        "role": "user",
                        "content": [self._openai_content_block(b) for b in image_parts]
                                    + [{"type": "text", "text": "Screenshot after the action above."}],
                    })
        return out


    def next_turn(self, step: int) -> Turn:
        """Call the model with the current message history and parse the
        response into a single actionable Turn (only the first tool call is
        acted on if the model requested several — mirrors the source
        repo's `tool_uses[0]` convention)."""
        if self.provider == "anthropic":
            text_blocks, tool_call, stop_reason = self._call_anthropic()
        else:
            text_blocks, tool_call, stop_reason = self._call_openai_compatible()

        self.messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": t} for t in text_blocks],
            "tool_call": (
                {"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.input}
                if tool_call else None
            ),
        })

        combined_text = "\n".join(text_blocks)
        turn = Turn(text=combined_text, stop_reason=stop_reason, tool_use=tool_call)
        turn.task_complete = self._check_task_complete(combined_text)
        turn.task_unsolvable = self.allow_unsolvable and self._check_task_unsolvable(combined_text)

        self._log({
            "step": step, "text": combined_text, "stop_reason": stop_reason,
            "tool_use": {"name": tool_call.name, "input": tool_call.input} if tool_call else None,
            "task_complete": turn.task_complete, "task_unsolvable": turn.task_unsolvable,
        })
        return turn

    def _call_anthropic(self) -> Tuple[List[str], Optional[ToolUse], Optional[str]]:
        response = self._call_with_retry(
            lambda: self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system_prompt,
                messages=self._to_anthropic_messages(),
                tools=self._anthropic_tools,
            )
        )
        text_blocks = [b.text for b in response.content if b.type == "text"]
        tool_call = None
        for b in response.content:
            if b.type == "tool_use":
                tool_call = ToolUse(id=b.id, name=b.name, input=b.input or {})
                break
        return text_blocks, tool_call, response.stop_reason

    def _call_openai_compatible(self) -> Tuple[List[str], Optional[ToolUse], Optional[str]]:
        response = self._call_with_retry(
            lambda: self.client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=self._to_openai_messages(),
                tools=self._openai_tools,
            )
        )
        message = response.choices[0].message
        text_blocks = [message.content] if message.content else []
        tool_call = None
        raw_tool_calls = getattr(message, "tool_calls", None) or []
        if raw_tool_calls:
            first = raw_tool_calls[0]
            try:
                arguments = json.loads(first.function.arguments) if first.function.arguments else {}
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("[UCMAgent] Could not parse tool_call arguments as JSON: %s (%s)",
                               first.function.arguments, e)
                arguments = {}
            tool_call = ToolUse(id=first.id, name=first.function.name, input=arguments)
        return text_blocks, tool_call, response.choices[0].finish_reason

    def _call_with_retry(self, call_fn, max_retries: int = 5):
        retry = 0
        while True:
            try:
                return call_fn()
            except Exception as e:
                retry += 1
                if retry >= max_retries:
                    raise
                wait = min(2 ** retry, 30)
                logger.warning("[UCMAgent] API error (attempt %d/%d): %s — retrying in %ds",
                                retry, max_retries, e, wait)
                time.sleep(wait)

    @staticmethod
    def _check_task_complete(text: str) -> bool:
        upper = text.upper()
        return "TASK COMPLETE" in upper or "TASK COMPLETED" in upper

    @staticmethod
    def _check_task_unsolvable(text: str) -> bool:
        return "TASK UNSOLVABLE" in text.upper()
