"""An Anthropic-Messages-shaped adapter over this project's LLM clients.

UCM's proposal loop (pipeline/analyze_untrusted_elements.py:analyze_with_llm) is written against
the Anthropic SDK: it constructs `Anthropic(api_key=..., timeout=...)`, calls
`client.messages.create(model=..., max_tokens=..., messages=[...])`, and reads
`response.content[0].text`. Everything else it does -- the JSON-array extraction, the
salvage-individual-objects fallback, the dedupe on css_selector, the follow-up turn -- is
transport-independent.

So rather than reimplement that loop (and then have to argue that the reimplementation is
faithful), this module supplies an object that answers to those four things and is backed by
mm_agents.sa_agent.llm.build_client. `install()` rebinds the name `Anthropic` inside their
module; their code then runs byte-for-byte unmodified against whatever endpoint the run
specifies. The point of doing it this way is that the fidelity claim reduces to "we swapped the
transport", which is checkable by reading forty lines instead of four hundred.
"""
import os
import sys

_AT_SPI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_AT_SPI)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from mm_agents import openrouter
from mm_agents.sa_agent.llm import build_client


class _Block:
    __slots__ = ("text", "type")

    def __init__(self, text: str):
        self.text = text
        self.type = "text"


class _Response:
    __slots__ = ("content", "usage", "model")

    def __init__(self, text: str, model: str, usage=None):
        self.content = [_Block(text)]
        self.model = model
        self.usage = usage


class _Messages:
    def __init__(self, outer):
        self._outer = outer

    def create(self, *, model=None, max_tokens=None, messages=None, **kwargs):
        """`model` from the caller is IGNORED on purpose.

        UCM hardcodes LLM_MODEL = "claude-sonnet-4-5-20250929" at module level and passes it
        here. Honouring it would mean either failing (the endpoint does not serve that model) or
        silently querying Anthropic when the run was configured for a local server. The
        configured model wins, and the discarded name is recorded so a run's meta says plainly
        which model actually answered.
        """
        o = self._outer
        o.requested_models.add(model)
        kw = {}
        if o.client.provider == "vllm" and o.enable_thinking is not None:
            kw["extra_body"] = {"chat_template_kwargs": {"enable_thinking": o.enable_thinking}}
        resp = o.client._client.chat.completions.create(
            model=o.client.model,
            messages=list(messages or []),
            max_tokens=max_tokens if max_tokens is not None else o.default_max_tokens,
            temperature=o.temperature,
            **kw,
        )
        choice = resp.choices[0]
        text = choice.message.content or ""
        if not text:
            reasoning = getattr(choice.message, "reasoning", None) or \
                getattr(choice.message, "reasoning_content", None)
            if reasoning:
                raise RuntimeError(
                    f"model returned only reasoning ({len(reasoning)} chars) and no content -- "
                    f"raise max_tokens or disable thinking (finish_reason="
                    f"{getattr(choice, 'finish_reason', None)!r})")
        o.n_calls += 1
        o.finish_reasons.append(getattr(choice, "finish_reason", None))
        usage = getattr(resp, "usage", None)
        if usage is not None:
            o.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            o.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        return _Response(text, o.client.model, usage)


class ShimClient:
    """Quacks like `anthropic.Anthropic` for the two attributes UCM touches."""

    def __init__(self, api_key=None, timeout=None, **_):
        cfg = _CONFIG
        if cfg is None:
            raise RuntimeError("ucm_baseline.llm_shim.install() was never called")
        self.client = cfg["client"]
        self.temperature = cfg["temperature"]
        self.enable_thinking = cfg["enable_thinking"]
        self.default_max_tokens = cfg["max_tokens"]
        self.requested_models = set()
        self.finish_reasons = []
        self.n_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.messages = _Messages(self)
        _CONFIG["instances"].append(self)


_CONFIG = None


def install(model_str: str, vllm_port: int = None, temperature: float = 0.0,
            enable_thinking: bool = False, max_tokens: int = 8000):
    """Point the shim at an endpoint and rebind UCM's `Anthropic` name to it.

    Returns the module whose name was rebound, so a caller can undo it; nothing does yet.
    """
    global _CONFIG
    _CONFIG = {
        "client": build_client(model_str, vllm_port, history=openrouter.ALWAYS),
        "temperature": temperature,
        "enable_thinking": enable_thinking,
        "max_tokens": max_tokens,
        "instances": [],
        "model_str": model_str,
    }
    from . import ucm_import
    mod = ucm_import.analyze_untrusted_elements()
    mod.Anthropic = ShimClient
    return mod


def usage() -> dict:
    if _CONFIG is None:
        return {}
    inst = _CONFIG["instances"]
    return {
        "model": _CONFIG["model_str"],
        "resolved_model": _CONFIG["client"].model,
        "provider": _CONFIG["client"].provider,
        "temperature": _CONFIG["temperature"],
        "enable_thinking": _CONFIG["enable_thinking"],
        "n_calls": sum(i.n_calls for i in inst),
        "prompt_tokens": sum(i.prompt_tokens for i in inst),
        "completion_tokens": sum(i.completion_tokens for i in inst),
        "requested_models": sorted({m for i in inst for m in i.requested_models if m}),
        "finish_reasons": sorted({r for i in inst for r in i.finish_reasons if r}),
    }
