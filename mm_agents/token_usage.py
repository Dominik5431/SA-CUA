"""
Per-episode LLM token accounting, recorded next to the success scores.

Every agent in this repo (PromptAgent, SA-Agent, FidesAgent, CamelAgent, HybridAgent,
QwenAgent, UCMAgent) reaches its model either through one of three SDKs -- the `openai`
client (Azure, vLLM, OpenRouter, OpenAI CUA), the `anthropic` client (direct +
`AnthropicBedrock`), or `google.genai` for the Gemini planners -- or by posting to an
OpenAI-compatible endpoint directly with `requests`/`httpx` (the Q-LLM in
mm_agents/*/agent_with_token_count.py, the OpenCUA and aguvis executors).  Each of those
funnels every request through a single method, so rather than editing the ~40 call sites
(which would also miss the ones inside `camel-cua`), this module patches those few methods
once and reads the `usage` block off each response.  An SDK call is counted once: the
raw-HTTP hooks skip requests issued from inside an already-instrumented SDK.

That makes the accounting agent-agnostic and defence-agnostic: a dual-LLM agent's planner
call, its Q-LLM call, its trust-detection call and the evaluator's LLM judge are all
captured, and each is attributed to the model, to the ROLE that model was given for the run
(planner / executor / q_llm / ..., registered by the agent loops -- see register_role), to
the agent module that made it (`call_site`), to the step it happened in, and to the episode
phase ("agent" vs "evaluation").  So a run whose planner and Q-LLM are different models is
reported separately for each, rather than as one merged total.

Written per episode by lib_run_single.run_single_example into the same directory as
result.txt / adversary_result.txt:

  token_usage.jsonl -- one line per model call, appended live (survives a crash mid-episode)
  token_usage.json  -- episode totals plus breakdowns by step / phase / model / role / call site

Nothing here may raise into the run: every hook body is wrapped, and a failure to account
for a call degrades to a missing record, never a failed episode.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("desktopenv.experiment")

_COUNTER_KEYS = (
    "calls",
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "text_output_tokens",
    "total_tokens",
    "reasoning_chars",
    "text_output_chars",
    "api_seconds",
)


def _new_counters() -> Dict[str, float]:
    return {k: 0 for k in _COUNTER_KEYS}


def _add(bucket: Dict[str, float], rec: Dict[str, Any]) -> None:
    bucket["calls"] += 1
    for key in _COUNTER_KEYS:
        if key == "calls":
            continue
        val = rec.get(key)
        if isinstance(val, (int, float)):
            bucket[key] += val


def _as_dict(usage: Any) -> Dict[str, Any]:
    """Best-effort plain-dict view of an SDK usage object (pydantic or already a dict)."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(usage, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return {k: v for k, v in vars(usage).items() if not k.startswith("_")}


def _int(d: Dict[str, Any], *path: str) -> Optional[int]:
    """Read a possibly-nested integer out of a usage dict, tolerating missing levels."""
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur if isinstance(cur, int) else None


class _Episode:
    def __init__(self, result_dir: str, meta: Dict[str, Any]):
        self.result_dir = result_dir
        self.meta = meta
        self.jsonl_path = os.path.join(result_dir, "token_usage.jsonl")
        self.json_path = os.path.join(result_dir, "token_usage.json")
        self.started = time.time()
        self.calls: List[Dict[str, Any]] = []
        self.total = _new_counters()
        self.by_step: Dict[int, Dict[str, float]] = {}
        self.by_phase: Dict[str, Dict[str, float]] = {}
        self.by_model: Dict[str, Dict[str, float]] = {}
        self.by_role: Dict[str, Dict[str, float]] = {}
        self.by_call_site: Dict[str, Dict[str, float]] = {}
        self.step = 0
        self.phase = "agent"


class TokenUsageTracker:
    """Process-wide accumulator. One episode is active at a time (run.py is sequential)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._episode: Optional[_Episode] = None
        self._call_seq = 0


    def start_episode(self, result_dir: str, **meta: Any) -> None:
        with self._lock:
            self._episode = _Episode(result_dir, meta)
            self._call_seq = 0

    def set_step(self, step: int) -> None:
        """Attribute subsequent calls to `step` (1-based; 0 = before the first step)."""
        ep = self._episode
        if ep is not None:
            try:
                ep.step = int(step)
            except (TypeError, ValueError):
                pass

    def current_step(self) -> int:
        """The step `set_step` last named, or 0 outside an episode."""
        ep = self._episode
        return ep.step if ep is not None else 0

    def set_phase(self, phase: str) -> None:
        """'agent' while the agent loop runs, 'evaluation' for LLM-judge evaluators, etc."""
        ep = self._episode
        if ep is not None:
            ep.phase = str(phase)

    def finish_episode(self) -> Optional[Dict[str, Any]]:
        """Write token_usage.json and clear the active episode. Returns the summary."""
        with self._lock:
            ep, self._episode = self._episode, None
        if ep is None:
            return None
        summary = self._summarize(ep)
        try:
            with open(ep.json_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=4)
        except OSError as e:
            logger.warning("Could not write token_usage.json: %s", e)
        return summary

    @staticmethod
    def _summarize(ep: _Episode) -> Dict[str, Any]:
        return {
            **ep.meta,
            "started_at": datetime.datetime.fromtimestamp(ep.started).strftime("%Y-%m-%d %H:%M:%S"),
            "wall_time_s": round(time.time() - ep.started, 2),
            "total": ep.total,
            "by_step": [
                {"step": step, **ep.by_step[step]} for step in sorted(ep.by_step)
            ],
            "by_phase": ep.by_phase,
            "by_model": ep.by_model,
            "by_role": ep.by_role,
            "role_models": dict(_ROLE_MODELS),
            "by_call_site": ep.by_call_site,
            "calls": ep.calls,
        }


    def record(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            ep = self._episode
            if ep is None:
                return
            self._call_seq += 1
            rec["call_index"] = self._call_seq
            rec["step"] = ep.step
            rec["phase"] = ep.phase
            rec["role"] = _role_for_model(rec.get("model"), rec.get("call_site"))
            ep.calls.append(rec)
            _add(ep.total, rec)
            _add(ep.by_step.setdefault(ep.step, _new_counters()), rec)
            _add(ep.by_phase.setdefault(ep.phase, _new_counters()), rec)
            _add(ep.by_model.setdefault(str(rec.get("model")), _new_counters()), rec)
            _add(ep.by_role.setdefault(str(rec.get("role")), _new_counters()), rec)
            _add(ep.by_call_site.setdefault(str(rec.get("call_site")), _new_counters()), rec)
            path = ep.jsonl_path
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError as e:
            logger.warning("Could not append token_usage.jsonl: %s", e)

    @property
    def active(self) -> bool:
        return self._episode is not None



_MODEL_ROLES: Dict[str, List[str]] = {}
_ROLE_MODELS: Dict[str, str] = {}

_PROVIDERS = frozenset({
    "azure", "aws", "bedrock", "vllm", "openrouter",
    "openai", "anthropic", "google", "gemini",
})


def _model_key(model_str: Optional[str]) -> Optional[str]:
    """The bare model name an SDK call will carry, out of any spelling run.py accepts.

    Strips a "<provider>|" or "<provider>:" prefix and a trailing "|cua" (which selects a
    provider's computer-use API, not a different model), so "aws | us.anthropic.claude-...-v1:0 | cua"
    and "us.anthropic.claude-...-v1:0" register as the same model.
    """
    if not model_str:
        return None
    parts = [p.strip() for p in str(model_str).split("|")]
    parts = [p for p in parts if p]
    if len(parts) > 1 and parts[-1].lower() == "cua":
        parts = parts[:-1]
    if not parts:
        return None
    name = "|".join(parts[1:]) if len(parts) > 1 and parts[0].lower() in _PROVIDERS else "|".join(parts)
    head, sep, tail = name.partition(":")
    if sep and head.lower() in _PROVIDERS:
        name = tail.strip()
    return name.strip() or None


def register_role(role: str, model_str: Optional[str]) -> None:
    """Record that `role` (e.g. "planner", "q_llm") runs on `model_str`.

    Called by the agent loops once, where they resolve each role's model and its defaults,
    so the mapping can never drift from what the run actually used. Idempotent.
    """
    name = _model_key(model_str)
    if not name or not role:
        return
    roles = _MODEL_ROLES.setdefault(name, [])
    if role not in roles:
        roles.append(role)
    _ROLE_MODELS[role] = name


def registered_roles() -> Dict[str, str]:
    """role -> bare model name, as registered by this run's agent loop."""
    return dict(_ROLE_MODELS)


_CALL_SITE_ROLES: tuple = (
    ("mm_agents.sa_agent.loop_and_planner", "planner"),
    ("mm_agents.fides.loop_and_planner", "planner"),
    ("mm_agents.sa_agent.agent_with_token_count", "q_llm"),
    ("mm_agents.fides.agent_with_token_count", "q_llm"),
    ("mm_agents.prompt_agent.opencua_agent", "q_vlm"),
    ("mm_agents.fides.opencua", "q_vlm"),
    ("mm_agents.sa_agent.trust_detection", "setup"),
    ("mm_agents.sa_agent.episode_capabilities", "setup"),
    ("biba", "rule_proposer"),
)


def _role_from_call_site(call_site: Optional[str]) -> Optional[str]:
    """The role the STACK implies, or None. Longest matching prefix wins."""
    if not call_site:
        return None
    best: Optional[tuple] = None
    for prefix, role in _CALL_SITE_ROLES:
        if call_site.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, role)
    return best[1] if best else None


def _role_for_model(model: Optional[str], call_site: Optional[str] = None) -> str:
    """Role label for a call, or "unregistered" for calls nobody claimed
    (an evaluator's LLM judge, a defense module building its own client).

    The registration the agent loop made is authoritative WHERE IT DECIDES: a model claimed by
    exactly one role is that role, whatever the stack says. The call site is consulted only when
    the model map cannot answer -- several roles share the model, or nothing registered it -- and
    that is precisely the single-model run the split is for. Deciding it the other way round would
    let this table silently overrule a loop that knows better.
    """
    name = _model_key(model) or (str(model).strip() if model else "")
    if not name:
        return "unknown"
    roles = _MODEL_ROLES.get(name)
    if roles is None:
        lowered = name.lower()
        for key, val in _MODEL_ROLES.items():
            if key.lower() == lowered:
                roles = val
                break
    if roles and len(roles) == 1:
        return roles[0]
    from_site = _role_from_call_site(call_site)
    if from_site:
        return from_site
    return "+".join(roles) if roles else "unregistered"


TRACKER = TokenUsageTracker()

def start_episode(result_dir: str, **meta: Any) -> None:
    TRACKER.start_episode(result_dir, **meta)


def set_step(step: int) -> None:
    TRACKER.set_step(step)


def current_step() -> int:
    return TRACKER.current_step()


def set_phase(phase: str) -> None:
    TRACKER.set_phase(phase)


def finish_episode() -> Optional[Dict[str, Any]]:
    return TRACKER.finish_episode()



_SKIP_FRAME_PREFIXES = (
    "mm_agents.token_usage",
    "mm_agents.openrouter",

    "openai",
    "anthropic",
    "google",
    "httpx",
    "httpcore",
    "requests",
    "backoff",
    "tenacity",
    "concurrent",
    "threading",
)


_ANONYMOUS_FRAMES = ("<lambda>", "<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>")


def _call_site() -> str:
    try:
        frame = sys._getframe(2)
    except ValueError:
        return "unknown"
    depth = 0
    fallback = None
    while frame is not None and depth < 40:
        module = frame.f_globals.get("__name__", "")
        name = frame.f_code.co_name
        if not module.startswith(_SKIP_FRAME_PREFIXES):
            if name not in _ANONYMOUS_FRAMES:
                return f"{module}:{name}"
            if fallback is None:
                fallback = f"{module}:{name}"
        frame = frame.f_back
        depth += 1
    return fallback or "unknown"


def _endpoint(resource: Any) -> Optional[str]:
    """Host of the base_url the SDK client is pointed at -- distinguishes Azure from
    vLLM from OpenRouter when several models share one `model` string."""
    try:
        client = getattr(resource, "_client", None)
        base = getattr(client, "base_url", None)
        return str(base) if base is not None else None
    except Exception:
        return None



def _openai_chat_usage(response: Any) -> Dict[str, Any]:
    usage = _as_dict(getattr(response, "usage", None))
    reasoning = _int(usage, "completion_tokens_details", "reasoning_tokens")
    inp = _int(usage, "prompt_tokens") or 0
    out = _int(usage, "completion_tokens") or 0
    rec: Dict[str, Any] = {
        "input_tokens": inp,
        "cached_input_tokens": _int(usage, "prompt_tokens_details", "cached_tokens") or 0,
        "cache_creation_input_tokens": _int(usage, "prompt_tokens_details", "cache_write_tokens") or 0,
        "output_tokens": out,
        "reasoning_tokens": reasoning,
        "text_output_tokens": out - (reasoning or 0),
        "total_tokens": _int(usage, "total_tokens") or (inp + out),
        "usage_raw": usage,
    }
    try:
        choice = (getattr(response, "choices", None) or [None])[0]
        message = getattr(choice, "message", None)
        rec["finish_reason"] = getattr(choice, "finish_reason", None)
        thinking = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None)
        rec["reasoning_chars"] = len(thinking) if isinstance(thinking, str) else 0
        content = getattr(message, "content", None)
        rec["text_output_chars"] = len(content) if isinstance(content, str) else 0
    except Exception:
        pass
    return rec


def _openai_responses_usage(response: Any) -> Dict[str, Any]:
    usage = _as_dict(getattr(response, "usage", None))
    reasoning = _int(usage, "output_tokens_details", "reasoning_tokens")
    inp = _int(usage, "input_tokens") or 0
    out = _int(usage, "output_tokens") or 0
    return {
        "input_tokens": inp,
        "cached_input_tokens": _int(usage, "input_tokens_details", "cached_tokens") or 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": out,
        "reasoning_tokens": reasoning,
        "text_output_tokens": out - (reasoning or 0),
        "total_tokens": _int(usage, "total_tokens") or (inp + out),
        "finish_reason": getattr(response, "status", None),
        "usage_raw": usage,
    }


def _anthropic_usage(response: Any) -> Dict[str, Any]:
    usage = _as_dict(getattr(response, "usage", None))
    inp = _int(usage, "input_tokens") or 0
    out = _int(usage, "output_tokens") or 0
    cache_read = _int(usage, "cache_read_input_tokens") or 0
    rec: Dict[str, Any] = {
        "input_tokens": inp + cache_read + (_int(usage, "cache_creation_input_tokens") or 0),
        "cached_input_tokens": cache_read,
        "cache_creation_input_tokens": _int(usage, "cache_creation_input_tokens") or 0,
        "output_tokens": out,
        "reasoning_tokens": None,
        "text_output_tokens": out,
        "total_tokens": inp + cache_read + (_int(usage, "cache_creation_input_tokens") or 0) + out,
        "finish_reason": getattr(response, "stop_reason", None),
        "usage_raw": usage,
    }
    try:
        thinking_chars, text_chars = 0, 0
        for block in getattr(response, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "thinking":
                thinking_chars += len(getattr(block, "thinking", "") or "")
            elif btype == "text":
                text_chars += len(getattr(block, "text", "") or "")
        rec["reasoning_chars"] = thinking_chars
        rec["text_output_chars"] = text_chars
    except Exception:
        pass
    return rec


def _genai_usage(response: Any) -> Dict[str, Any]:
    usage = _as_dict(getattr(response, "usage_metadata", None))
    inp = _int(usage, "prompt_token_count") or 0
    out = _int(usage, "candidates_token_count") or 0
    reasoning = _int(usage, "thoughts_token_count")
    return {
        "input_tokens": inp,
        "cached_input_tokens": _int(usage, "cached_content_token_count") or 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": out + (reasoning or 0),
        "reasoning_tokens": reasoning,
        "text_output_tokens": out,
        "total_tokens": _int(usage, "total_token_count") or (inp + out + (reasoning or 0)),
        "usage_raw": usage,
    }



_installed = False
_install_lock = threading.Lock()
_patched_apis: set = set()


def _wrap(owner: Any, name: str, api: str, extract) -> None:
    original = getattr(owner, name, None)
    if original is None or getattr(original, "_token_usage_wrapped", False):
        if original is not None and getattr(original, "_token_usage_wrapped", False):
            _patched_apis.add(api)
        return

    def wrapper(self, *args, **kwargs):
        started = time.time()
        response = original(self, *args, **kwargs)
        try:
            if TRACKER.active:
                rec = {
                    "api": api,
                    "model": kwargs.get("model") or getattr(response, "model", None),
                    "endpoint": _endpoint(self),
                    "call_site": _call_site(),
                    "api_seconds": round(time.time() - started, 3),
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                rec.update(extract(response))
                TRACKER.record(rec)
        except Exception as e:
            logger.warning("Token accounting failed for %s: %s", api, e)
        return response

    wrapper._token_usage_wrapped = True
    wrapper.__name__ = getattr(original, "__name__", name)
    wrapper.__doc__ = getattr(original, "__doc__", None)
    setattr(owner, name, wrapper)
    _patched_apis.add(api)



def _llm_api_for_url(url: str) -> Optional[str]:
    u = str(url).split("?", 1)[0].rstrip("/").lower()
    if u.endswith(":generatecontent") or u.endswith(":streamgeneratecontent"):
        return "http.google.genai"
    if u.endswith("/messages") or u.endswith("/complete"):
        return "http.anthropic.messages"
    if u.endswith("/responses"):
        return "http.openai.responses"
    if u.endswith("/chat/completions") or u.endswith("/completions"):
        return "http.openai.chat.completions"
    return None


_GENAI_REST_KEYS = {
    "promptTokenCount": "prompt_token_count",
    "candidatesTokenCount": "candidates_token_count",
    "thoughtsTokenCount": "thoughts_token_count",
    "cachedContentTokenCount": "cached_content_token_count",
    "totalTokenCount": "total_token_count",
}


def _genai_rest_body(body: Dict[str, Any]) -> Dict[str, Any]:
    usage = body.get("usageMetadata") or body.get("usage_metadata") or {}
    return {
        "model": body.get("modelVersion") or body.get("model"),
        "usage_metadata": {_GENAI_REST_KEYS.get(k, k): v for k, v in usage.items()},
    }


_HTTP_EXTRACTORS = {
    "http.openai.chat.completions": _openai_chat_usage,
    "http.openai.responses": _openai_responses_usage,
    "http.anthropic.messages": _anthropic_usage,
    "http.google.genai": _genai_usage,
}


class _JsonObj:
    """Attribute view over a decoded JSON body.

    The extractors above are written against SDK response objects (attribute access,
    `.model_dump()`-able usage); wrapping the raw JSON in this lets a raw-HTTP response reuse
    them verbatim instead of duplicating four usage layouts.
    """

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw
        for key, val in raw.items():
            try:
                setattr(self, key, _wrap_json(val))
            except (AttributeError, TypeError):
                pass

    def to_dict(self) -> Dict[str, Any]:
        return self._raw


def _wrap_json(val: Any) -> Any:
    if isinstance(val, dict):
        return _JsonObj(val)
    if isinstance(val, list):
        return [_wrap_json(v) for v in val]
    return val


def _from_instrumented_sdk() -> bool:
    """True when this HTTP call was issued by an SDK already wrapped above.

    The openai and anthropic clients speak httpx underneath, so without this check every
    SDK call would be counted twice -- once at the SDK method, once at httpx.Client.send.
    """
    try:
        frame = sys._getframe(2)
    except ValueError:
        return False
    depth = 0
    while frame is not None and depth < 40:
        module = frame.f_globals.get("__name__", "")
        if module.startswith(("openai.", "anthropic.", "google.genai")) or module in (
            "openai", "anthropic",
        ):
            return True
        frame = frame.f_back
        depth += 1
    return False


def _request_model(payload: Any) -> Optional[str]:
    """The model named in an outgoing request body, used when the response omits it."""
    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    return payload.get("model") if isinstance(payload, dict) else None


def _record_http(url: str, response: Any, body: Any, started: float,
                 request_model: Optional[str] = None) -> None:
    """Account for one raw-HTTP model call. `body` is the already-decoded JSON response."""
    api = _llm_api_for_url(url)
    if api is None or not isinstance(body, dict) or (
        "usage" not in body and "usageMetadata" not in body
    ):
        return
    rec: Dict[str, Any] = {
        "api": api,
        "model": request_model or body.get("model"),
        "endpoint": str(url),
        "call_site": _call_site(),
        "api_seconds": round(time.time() - started, 3),
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if api == "http.google.genai":
        body = _genai_rest_body(body)
        rec["model"] = request_model or body.get("model")
    rec.update(_HTTP_EXTRACTORS[api](_JsonObj(body)))
    TRACKER.record(rec)


def _wrap_requests_session() -> None:
    from requests.sessions import Session

    original = getattr(Session, "request", None)
    if original is None or getattr(original, "_token_usage_wrapped", False):
        if original is not None:
            _patched_apis.add("requests")
        return

    def wrapper(self, method, url, *args, **kwargs):
        started = time.time()
        response = original(self, method, url, *args, **kwargs)
        try:
            if (
                TRACKER.active
                and not kwargs.get("stream")
                and getattr(response, "_content_consumed", True)
                and _llm_api_for_url(url)
                and getattr(response, "status_code", None) == 200
                and not _from_instrumented_sdk()
            ):
                _record_http(url, response, response.json(), started,
                             _request_model(kwargs.get("json") or kwargs.get("data")))
        except Exception as e:
            logger.warning("Token accounting failed for requests %s: %s", url, e)
        return response

    wrapper._token_usage_wrapped = True
    Session.request = wrapper
    _patched_apis.add("requests")


def _wrap_httpx_client() -> None:
    import httpx

    original = getattr(httpx.Client, "send", None)
    if original is None or getattr(original, "_token_usage_wrapped", False):
        if original is not None:
            _patched_apis.add("httpx")
        return

    def wrapper(self, request, *args, **kwargs):
        started = time.time()
        response = original(self, request, *args, **kwargs)
        try:
            url = str(getattr(request, "url", ""))
            if (
                TRACKER.active
                and not kwargs.get("stream")
                and getattr(response, "is_closed", True)
                and _llm_api_for_url(url)
                and getattr(response, "status_code", None) == 200
                and not _from_instrumented_sdk()
            ):
                _record_http(url, response, response.json(), started,
                             _request_model(getattr(request, "content", None)))
        except Exception as e:
            logger.warning("Token accounting failed for httpx %s: %s", getattr(request, "url", "?"), e)
        return response

    wrapper._token_usage_wrapped = True
    httpx.Client.send = wrapper
    _patched_apis.add("httpx")


def install() -> None:
    """Patch the SDK resource methods. Idempotent; safe to call from every entry point."""
    global _installed
    with _install_lock:
        if _installed:
            return
        _installed = True

    try:
        from openai.resources.chat.completions import Completions
        _wrap(Completions, "create", "openai.chat.completions", _openai_chat_usage)
        _wrap(Completions, "parse", "openai.chat.completions.parse", _openai_chat_usage)
    except Exception as e:
        logger.warning("Token accounting: could not instrument openai chat completions: %s", e)

    try:
        from openai.resources.responses import Responses
        _wrap(Responses, "create", "openai.responses", _openai_responses_usage)
    except Exception as e:
        logger.warning("Token accounting: could not instrument openai responses: %s", e)

    try:
        from anthropic.resources.messages import Messages
        _wrap(Messages, "create", "anthropic.messages", _anthropic_usage)
    except Exception as e:
        logger.warning("Token accounting: could not instrument anthropic messages: %s", e)

    try:
        from anthropic.resources.beta.messages import Messages as BetaMessages
        _wrap(BetaMessages, "create", "anthropic.beta.messages", _anthropic_usage)
    except Exception as e:
        logger.warning("Token accounting: could not instrument anthropic beta messages: %s", e)

    try:
        from google.genai.models import Models
        _wrap(Models, "generate_content", "google.genai", _genai_usage)
    except Exception:
        pass

    for hook in (_wrap_requests_session, _wrap_httpx_client):
        try:
            hook()
        except Exception as e:
            logger.warning("Token accounting: could not instrument %s: %s", hook.__name__, e)


def installed_apis() -> set:
    """The SDK API labels this process actually instrumented (empty until install())."""
    return set(_patched_apis)


_PROVIDER_SDK = {
    "azure": "openai", "vllm": "openai",
    "openrouter": "openai", "openai": "openai", "aws": "anthropic",
}


def enforce(model: str) -> None:
    """Fail fast unless token accounting is actually live for this run's provider.

    Guards against the exact gap that let an evaluation run for days with no token_usage.jsonl:
    a process that never activated the instrumentation (e.g. started on older code). install()
    is idempotent, so calling it here is safe. Raises RuntimeError if the SDK this run's
    provider uses did not get wrapped, so `python run.py ...` refuses to start rather than
    silently producing results with no token accounting.
    """
    install()
    provider = (model or "").split("|", 1)[0].strip().lower()
    need_openai = {"openai.chat.completions", "openai.responses"}
    need_anthropic = {"anthropic.messages", "anthropic.beta.messages"}
    sdk = _PROVIDER_SDK.get(provider)
    have = installed_apis()
    ok = bool(have & (need_anthropic if sdk == "anthropic" else need_openai)) if sdk \
        else bool(have - {"requests", "httpx"})
    if not ok:
        raise RuntimeError(
            f"Token accounting is NOT active for provider '{provider}' "
            f"(instrumented APIs: {sorted(have) or 'none'}). Every evaluation must record "
            f"token usage; refusing to run. Check that the model SDK imported cleanly and "
            f"that this process is running current code."
        )
    logger.info("Token accounting ACTIVE for '%s' (instrumented: %s).", provider, sorted(have))



def load_episode_total(example_result_dir: str) -> Optional[Dict[str, Any]]:
    """Episode totals from token_usage.json, or reconstructed from token_usage.jsonl if
    the episode crashed before the summary was written. None when neither exists."""
    json_path = os.path.join(example_result_dir, "token_usage.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, encoding="utf-8") as f:
                return json.load(f).get("total")
        except (OSError, ValueError):
            pass
    jsonl_path = os.path.join(example_result_dir, "token_usage.jsonl")
    if not os.path.exists(jsonl_path):
        return None
    total = _new_counters()
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    _add(total, json.loads(line))
    except (OSError, ValueError):
        return None
    return total


def load_episode_breakdown(example_result_dir: str, key: str = "by_model") -> Dict[str, Dict[str, float]]:
    """Per-model ("by_model") or per-role ("by_role") totals for one episode.

    Same fallback as load_episode_total: rebuilt from token_usage.jsonl when the episode
    crashed before its summary was written, and empty when neither file exists. Episodes
    recorded before roles existed have no by_role section; their calls are re-bucketed from
    the jsonl's own "role" field, and report as "unregistered" when that is absent too.
    """
    field = "role" if key == "by_role" else "model"
    json_path = os.path.join(example_result_dir, "token_usage.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, encoding="utf-8") as f:
                summary = json.load(f)
            existing = summary.get(key)
            if existing:
                return existing
            calls = summary.get("calls") or []
            if calls:
                out: Dict[str, Dict[str, float]] = {}
                for rec in calls:
                    _add(out.setdefault(str(rec.get(field) or "unregistered"), _new_counters()), rec)
                return out
        except (OSError, ValueError):
            pass
    jsonl_path = os.path.join(example_result_dir, "token_usage.jsonl")
    if not os.path.exists(jsonl_path):
        return {}
    out = {}
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    _add(out.setdefault(str(rec.get(field) or "unregistered"), _new_counters()), rec)
    except (OSError, ValueError):
        return {}
    return out


def load_role_models(example_result_dir: str) -> Dict[str, str]:
    """role -> model recorded for one episode, or {} when the episode predates roles."""
    json_path = os.path.join(example_result_dir, "token_usage.json")
    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f).get("role_models") or {}
    except (OSError, ValueError):
        return {}
