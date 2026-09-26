from abc import ABC, abstractmethod
from dataclasses import Field, dataclass
import base64
import json
import re
import shlex
from typing import Any, Callable, Optional, Union
import uuid

from osworld.desktop_env.desktop_env import DesktopEnv
from osworld.mm_agents.old_uitars_agent import UITARSAgent
from osworld.mm_agents.opencua_agent import OpenCUAAgent
import openai
from pydantic import BaseModel
from mm_agents import token_usage
from mm_agents.sa_agent.claude import FidesAnthropicAgent
from mm_agents.sa_agent.hybrid_tools import extract_cloned_path
from mm_agents.sa_agent.opencua import FidesOpenCUAAgent
from mm_agents.sa_agent.uitars import FidesUITARSAgent
from mm_agents.sa_agent.pydantic_scheme import MetaValue
from mm_agents.sa_agent.endorse import VERIFIER_TOOLS
from mm_agents.sa_agent.labels import metadata_to_label
from mm_agents.sa_agent.qllm import QLLMAgent
from mm_agents.sa_agent.tool_scheme import Tool
from mm_agents.sa_agent.lattice import FidesLattice, IntegrityLabel, InverseLattice, PowersetLattice

from openai.types.chat import (
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageToolCallParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)

from mm_agents.sa_agent.policies import (
    Policy,
    PolicyViolation,
    Action,
    Query,
    ToolCall,
    Response,
    SAPolicy,
    TARGET_TIER_TOOLS,
)
from mm_agents.sa_agent.screen_labels import (
    apply_clipboard_taint,
    apply_endorsed_regions,
    SAScreenView,
    endorsed_public,
    is_untrusted,
    label_screen,
    make_metadata,
    select_action_list_items,
    trusted_public,
    untrusted_public,
)
from mm_agents.sa_agent.tools import (
    QLLMQueryResults,
    SummarizerOutput,
    ToolRefusal,
    active_endorsed_rects,
)
from mm_agents.accessibility_tree_wrap.coord_to_semantic import LabelTaint

import os
import time
from openai import OpenAI
from mm_agents.openrouter import WINDOWED, apply_prompt_cache_policy
from google import genai
from typing import List, Dict, Any
import logging

logger = logging.getLogger("desktopenv.experiment")

MAX_RETRIES = 20

def get_tool_response(model: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], token_log_path: str | None = None):
    """
    Send a tool-using chat request to the appropriate model provider (openai, openrouter, google, vllm).
    The model string must be in the format "<provider>:<model_name>".
    """

    try:
        provider, model_name = model.split(":", 1)
    except ValueError:
        raise ValueError(f"Invalid model format '{model}'. Expected '<provider>:<model_name>'")

    if provider == "openai":
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        api_func = lambda: client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=1.0
        )

    elif provider == "openrouter":
        client = apply_prompt_cache_policy(OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY")
        ), history=WINDOWED)
        api_func = lambda: client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=1.0
        )

    elif provider == "google":
        raise ValueError("Google Gemini is not supported yet.")
        client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        api_func = lambda: client.models.generate_content(
            model=model_name,
            contents=messages,
        )

    elif provider == "vllm":
        vllm_port = os.getenv("VLLM_PORT", "8000")
        vllm_base_url = os.getenv("VLLM_BASE_URL", f"http://localhost:{vllm_port}/v1")
        client = OpenAI(
            base_url=vllm_base_url,
            api_key=os.getenv("VLLM_API_KEY", "EMPTY"),
        )
        api_func = lambda: client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=1.0
        )

    else:
        raise ValueError(f"Unknown provider '{provider}'. Supported: openai, openrouter, google, vllm")

    attempt = 0
    current_msg = None

    retry_message = {
        "role": "assistant",
        "content": (
            "Your last response did not include a tool call. "
            "ALL RESPONSES SHOULD INCLUDE A TOOL CALL. "
            "Use the provided tools to interact with the environment."
        )
    }

    while attempt < MAX_RETRIES:
        try:
            response = api_func()

            if provider in ("openai", "openrouter", "vllm"):
                current_msg = response.choices[0].message

                reasoning_text = getattr(current_msg, "reasoning_content", None) or getattr(current_msg, "reasoning", None)
                if reasoning_text:
                    print(f"[P-LLM thinking]\n{reasoning_text}\n", flush=True)
                if current_msg.content:
                    print(f"[P-LLM content]\n{current_msg.content}\n", flush=True)

                if token_log_path is not None:
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        with open(token_log_path, "a") as f:
                            f.write(f"Model {model_name}, Function planner - Input tokens: {usage.prompt_tokens}, Output tokens: {usage.completion_tokens}, Total tokens: {usage.total_tokens}\n")

                if getattr(current_msg, "tool_calls", None):
                    for message in messages:
                        if message == retry_message:
                            messages.remove(message)
                    return current_msg

                if not getattr(current_msg, "tool_calls", None):

                    if not messages or messages[-1] != retry_message:
                        messages.append(retry_message)

            elif provider == "google":
                text = response.text
                if text:
                    return {"role": "assistant", "content": text}

        except Exception as e:
            print(f"[Attempt {attempt + 1}] Error: {e}")
            time.sleep(1)

        attempt += 1
        print(f"[Retry {attempt}] No valid tool call detected. Retrying...")

    raise RuntimeError(f"Model failed to produce a valid tool call after {MAX_RETRIES} attempts.")

def call_tool(
        tools: list[Tool],
        name: str,
        json_args: str,
        qllm_agent: QLLMAgent,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent,
        env: DesktopEnv, 
        example_result_dir: str,
        step_idx: int,
        observation_type: str,
        qllm_agent_second: QLLMAgent,
        model_defense_level: int = 0,
        obs_raw: dict | None = None,
        endorse_manager=None,
        recent_actions: list[str] | None = None,
        mask=None,
        ) -> type[BaseModel]:
    """
    Call a tool function with the given name using JSON-serialized arguments.

    Args:
        tools (list[Callable[..., ToolResult]]): List of Python functions representing tools.
        name (str): The name of the tool to call.
        json_args (str): A JSON string representing the arguments to pass to the tool.

    Returns:
        ToolResult: The result returned by the tool.

    Raises:
        ValueError: If no tool with the given name exists.
    """

    if name not in {t.name for t in tools}:
        content = """Your last tool call had an invalid tool name. Please check the tool documentation and try again. Tool call was: """ + "name" + str(name) + " with args " + str(json_args) + "."
        metadata = make_metadata(trusted_public())
        return QLLMQueryResults(
            root=MetaValue(value=SummarizerOutput(output=content), metadata=metadata)
        )

    tool = {t.name : t for t in tools}[name]
    args = json.loads(json_args)

    print(f"Calling {tool.name} with {args}")

    if obs_raw is None:
        obs_raw = env._get_obs()

    if tool.name == "summarize_screenshot_content":
        if "output_type" not in args:
            args["output_type"] = "str"

    if tool.name == "summarize_screenshot_content" and "output_type" not in args:
        args["output_type"] = "str"

    try:
        params = tool.parameter_model(**args)
    except:
        content = """Your last tool call had invalid arguments. Please check the tool documentation and try again. Tool call was: """ + "name" + str(name) + "."
        metadata = make_metadata(trusted_public())
        return QLLMQueryResults(
            root=MetaValue(value=SummarizerOutput(output=content), metadata=metadata)
        )

    if tool.name == "query_llm":
        return tool.callable(
            params, qllm_agent, observation_type
        )
    elif tool.name == 'run_vlm':
        return tool.callable(
            params, agent, qllm_agent, env, example_result_dir, step_idx, 
            observation_type, model_defense_level, qllm_agent_second
        )
    elif tool.name == "query_screen":
        return tool.callable(
            params, agent, model_defense_level,
            qllm_agent, qllm_agent_second, obs_raw, mask
        )
    elif tool.name == "regex_extract":
        return tool.callable(params)
    elif tool.name == "fetch_web_content":
        return tool.callable(params)
    elif tool.name == "run_terminal_command":
        return tool.callable(params, env)
    elif tool.name in ("locate_on_screen", "attest_screen_value", "find_by_text"):
        return tool.callable(params, obs_raw, mask)
    elif tool.name == "endorse_element":
        return tool.callable(params, obs_raw, endorse_manager, mask)
    elif tool.name in VERIFIER_TOOLS:
        return tool.callable(params, endorse_manager)
    else:
        return tool.callable(params, env, example_result_dir, step_idx, agent)
    

_MAX_CONSECUTIVE_TOOL_ERRORS = 6

TOOL_ERROR_PREFIX = "Your last tool call did not return a result: "


def tool_error_detail(name: str, exc: BaseException) -> str:
    """What the planner is told about a tool call that raised, under the TOOL_ERROR_PREFIX
    contract: this text is shown verbatim, so it may not carry content read off the screen.

    A ``ToolRefusal`` message is host-authored by construction (see its docstring) and is the
    useful case -- "the answer did not have the shape of a URL", "there is no region 4" -- so it
    passes through whole. Anything else is an exception from library or environment code whose
    string is not under our control: ``KeyError`` carries the key it was given, a parser can quote
    the text it choked on. Those are named by tool and exception class only. An exception string
    is not an exemption from the rule that untrusted content reaches the planner as an opaque
    token, and the full text is still logged host-side either way.
    """
    if isinstance(exc, ToolRefusal):
        return str(exc)
    return (f"{name} failed with {type(exc).__name__}. (The message is withheld: it is not "
            f"host-authored text and may quote page content.)")


_CLEAR_RESULT_KEYS = frozenset({"status", "integrity", "type", "variable", "message"})


def redact_leaves(obj, mapping, history, saving_to_memory, key="root", label_store=None, label=None):
    """
    Recursively walk a dict/list and replace leaf values with appropriate ids.

    When *label_store* is given, every memory token minted here also records
    *label* (the tool result's FidesLattice label) so the loop can later join
    the labels of the variables a planner tool call references (per-value
    taint tracking, see policies.py).
    """
    if isinstance(obj, dict):
        return {k: redact_leaves(v, mapping, history, saving_to_memory=saving_to_memory, key=k, label_store=label_store, label=label) for k, v in obj.items()}
    if key not in _CLEAR_RESULT_KEYS and not (
        isinstance(obj, str)
        and "Your last tool call had an invalid tool name." in obj
    ):
        tool_name = history[-1]["tool_calls"][0]["function"]["name"] if history and "tool_calls" in history[-1] and history[-1]["tool_calls"] else "unknown_tool"
        tool_call_number = sum([1 for msg in history if "tool_calls" in msg and msg["tool_calls"] and msg["tool_calls"][0]["function"]["name"] == tool_name])
        token = f"{tool_name}-result-{tool_call_number}.{key}"
        if saving_to_memory:
            mapping[token] = obj
            if label_store is not None and label is not None:
                label_store[token] = label
    else:
        token = obj
    return token


def redact_tool_result(content_dict, memory, history, label_store, label):
    """
    Trust-conditional redaction of a parsed tool-result dict (``{"root": {...}}``).

    The planner is the privileged principal and may see a TRUSTED result's real content
    directly -- redaction into an opaque variable token is only needed to keep an UNTRUSTED
    (attacker-influenceable) value out of the planner's raw context. This mirrors the same
    literal-vs-redacted split already used for ``action_list`` entries (see BLOCK_ACTION_LIST).

    UNTRUSTED results are passed through ``redact_leaves`` exactly as before (a FAIL-status
    result is not saved to memory, matching the pre-existing behavior). TRUSTED results are
    returned unmodified -- no token minted, nothing added to *memory*/*label_store*.

    The FAIL-status coordinate placeholder ("NO_COORDINATES_FOR_FAIL_STATUS") is applied either
    way: it exists to avoid feeding a stale/fabricated (x, y) forward after a failed find, which
    is a staleness concern independent of trust.
    """
    root = content_dict.get("root")
    if is_untrusted(label):
        saving_to_memory = True
        if isinstance(root, dict) and root.get("status") == "FAIL":
            saving_to_memory = False
        content_redacted = redact_leaves(content_dict, memory, history, saving_to_memory,
                                          label_store=label_store, label=label)
    else:
        content_redacted = content_dict

    redacted_root = content_redacted.get("root")
    if isinstance(redacted_root, dict) and redacted_root.get("status") == "FAIL":
        if "x" in redacted_root:
            redacted_root["x"] = "NO_COORDINATES_FOR_FAIL_STATUS"
        if "y" in redacted_root:
            redacted_root["y"] = "NO_COORDINATES_FOR_FAIL_STATUS"
    return content_redacted


def checking_task_done(history):
    tool_name = history[-1]["tool_calls"][0]["function"]["name"] if history and "tool_calls" in history[-1] and history[-1]["tool_calls"] else "unknown_tool"


def render_action_list(
    view: SAScreenView,
    planner: "LabeledBasicPlanner",
    turn: int,
    max_items: int = 50,
    mask=None,
) -> dict[str, Any]:
    """Build the planner-visible ``action_list`` block injected as a fresh,
    ephemeral user message right before every model query (``case Query`` in
    ``LabeledPlanningLoop.loop()``): a comprehensive, per-step snapshot of
    currently available actions derived from the accessibility tree (see
    coord_to_semantic.py). Never persisted into ``self.history`` -- rebuilt
    from the live screen on every turn, including the very first one.

    Trust filter (gated on ``label_taint``, not ``zone`` -- they currently
    coincide, but answer different questions): a DOCUMENT-zone element's
    label/description text is attacker-influenceable, so any action whose
    ``label_taint`` is ``LabelTaint.TAINTED`` is dropped entirely rather than
    shown -- the planner only ever sees interaction with trusted (chrome/OS
    or otherwise untainted) elements, never a reference to page content it
    can't read.
    """
    kept, omitted = select_action_list_items(view, max_items=max_items)
    interactable = []
    n_masked_out = 0
    for i, (action, label) in enumerate(kept):
        if mask is None and action.label_taint == LabelTaint.TAINTED:
            continue
        if mask is not None and mask.hit(action.x_center, action.y_center):
            n_masked_out += 1
            continue
        entry: dict[str, Any] = {
            "ref": i,
            "role": action.role,
            "action": action.action_type.value,
            "supported_actions": sorted(a.value for a in action.supported_actions),
            "capability": action.capability.value,
            "zone": action.zone.value,
            "trust": label.left.level.name,
            "label": action.label,
            "x": action.x_center,
            "y": action.y_center,
        }
        if action.region_role:
            entry["region"] = action.region_role
        if action.direction:
            entry["direction"] = action.direction
        if action.description:
            entry["hint"] = action.description
        if action.role_description:
            entry["role_hint"] = action.role_description
        if action.checked is not None:
            entry["checked"] = action.checked
        if action.selected is not None:
            entry["selected"] = action.selected
        if action.focused is not None:
            entry["focused"] = action.focused
        if action.has_popup:
            entry["has_popup"] = action.has_popup
        interactable.append(entry)

    globals_out = []
    for global_action, label in view.global_actions:
        if global_action.label_taint == LabelTaint.TAINTED:
            continue
        globals_out.append({
            "keys": list(global_action.keys),
            "description": global_action.description,
            "trust": label.left.level.name,
            "capability": global_action.capability.value,
            "zone": global_action.zone.value,
            "scope": global_action.scope,
        })

    out = {
        "step": turn,
        "interactable": interactable,
        "global": globals_out,
        "omitted": omitted,
    }
    if n_masked_out:
        out["behind_redaction"] = n_masked_out
    return out


class LabeledPlanner(ABC):
    @abstractmethod
    def next_action(self, message: ChatCompletionMessage | ChatCompletionMessageParam, label: FidesLattice) -> tuple[Action, FidesLattice]:
        """
        Given a message, determine the next action in the planning loop.
        """
        pass

non_ui_tool_list = [
    "query_llm", "done", "fail", "abstain", "wait",
    "query_screen", "endorse_by_crosscheck", "endorse_by_checksum", "endorse_by_recall",
    "endorse_by_task_match", "endorse_by_candidate_set", "endorse_by_registry",
    "endorse_by_directory", "endorse_by_domain_pin", "get_contacts",
    "endorse_by_procedure", "fetch_web_content",
    "locate_on_screen", "attest_screen_value",
    "endorse_element",
]

_PASTE_KEY_SETS = (frozenset({"ctrl", "v"}), frozenset({"shift", "insert"}),
                    frozenset({"command", "v"}), frozenset({"cmd", "v"}))
_PASTE_LABEL_RE = re.compile(r"\s*paste\b", re.IGNORECASE)

class LabeledPlanningLoop:
    def __init__(
            self,
            planner: LabeledPlanner,
            model: str,
            tools: list[Tool],
            policy: Policy,
            qllm_agent: QLLMAgent,
            qllm_agent_second: QLLMAgent | None,
            agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent,
            env: DesktopEnv,
            example_result_dir: str = "",
            step_idx: int = 0,
            max_steps: int = 15,
            observation_type: str = "screenshot_a11y_tree",
            model_defense_level: int = 0,
            token_log_path: str | None = None,
            sa_policy: SAPolicy | None = None,
            endorse_manager=None,
            principal_map=None,
            file_provenance_map=None,
            mask_config=None,
            history_window: int = 0,
            ):
        self.planner = planner
        self.model = model
        self.tools = tools
        self.policy = policy
        self.qllm_agent = qllm_agent
        self.qllm_agent_second = qllm_agent_second
        self.agent = agent
        self.env = env
        self.step_idx = step_idx
        self.turn = 0
        self.example_result_dir = example_result_dir
        self.max_steps = max_steps
        self.observation_type = observation_type
        self.model_defense_level = model_defense_level
        self.token_log_path = token_log_path
        self.sa_policy = sa_policy
        self.endorse_manager = endorse_manager
        self.principal_map = principal_map
        self.file_provenance_map = file_provenance_map
        self.mask_config = mask_config
        self._last_mask = None
        self._mask_base = None
        self._mask_rects_n = 0
        self._mask_obs = None
        self._mask_turn = None
        self._clipboard_label: FidesLattice = untrusted_public()
        self._last_clipboard_content: Optional[str] = None
        self._primary_label: FidesLattice = untrusted_public()
        self._last_primary_content: Optional[str] = None
        self._held_keys: set = set()
        self._xclip_available: Optional[bool] = None
        self._consecutive_blocks = 0
        self._last_action_list_repr: str | None = None
        self._stale_repeat_count = 0
        self._last_was_ui_action = False
        self._last_blocked_call: tuple[str, str] | None = None
        self._consecutive_tool_errors = 0
        self._last_failed_call: tuple[str, str] | None = None
        self._cached_obs_raw = None
        self.history_window = int(history_window or 0)
        self._windowed_mask = None
        self._dropped_history: list = []
        self._preamble_len = 0

    def _write_planner_state(self):
        """Write planner history and memory incrementally after every step."""
        if not self.example_result_dir:
            return
        h = self.planner.history
        pre = self._preamble_len
        full = list(h[:pre]) + list(self._dropped_history) + list(h[pre:])
        with open(f"{self.example_result_dir}/planner_history.json", "w", encoding="utf-8") as f:
            json.dump([self._msg_for_audit(msg) for msg in full], f, indent=2)

        with open(f"{self.example_result_dir}/memory.json", "w", encoding="utf-8") as f:
            json.dump(self.planner.memory, f, indent=2)

    @staticmethod
    def _msg_for_audit(msg):
        """One history message with any image payload replaced by a reference to the frame on disk."""
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            return msg
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                out.append({"type": "image_url",
                            "image_url": {"url": "<masked screenshot; see masked/>"}})
            else:
                out.append(part)
        return {**msg, "content": out}

    def _log_action_list(self, action_list: dict) -> None:
        """Debug trace of the per-turn action_list actually shown to the planner. Since the
        observation is ephemeral (never appended to self.planner.history, see the Query case
        below), it wouldn't otherwise be visible anywhere for post-hoc trajectory debugging --
        mirrors the existing SAPolicy._audit -> policy_trace.jsonl pattern."""
        if not self.example_result_dir:
            return
        try:
            with open(f"{self.example_result_dir}/action_list_trace.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"turn": self.turn, "action_list": action_list}) + "\n")
        except OSError:
            pass

    def _recent_action_summary(self, limit: int = 12) -> list[str]:
        """Plain-text summary of the planner's own recent tool calls, most recent last -- fed to
        checkdone's verifier as first-party evidence (see tools.py::checkdone and
        SYST_PROMPT_CHECKDONE) so it isn't judging task completion from a single, possibly
        ambiguous screenshot alone. Read from self.planner.history, which already accumulates
        every tool call for the whole episode -- nothing new to track."""
        history = getattr(self.planner, "history", None) or []
        calls: list[str] = []
        for msg in history:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {}) or {}
                    calls.append(f"{fn.get('name')}({fn.get('arguments')})")
        return calls[-limit:]

    def _current_obs(self) -> dict:
        """Return the current observation (screenshot + accessibility tree), fetching a fresh one
        from the VM only if the environment might actually have changed since the last call.

        ``env._last_settled_obs`` is populated by ``one_step_action`` (tools.py) if and only if the
        most recently dispatched tool call actually ran a GUI action via ``env.step()`` (click,
        type_text, scroll, ...). Every other tool -- ``query_screen``, ``endorse_by_crosscheck``,
        ``endorse_by_checksum``, ``fetch_web_content``, ``run_terminal_command``, ``find``, ... --
        never touches the visible screen (the VM's HTTP `/execute` endpoint runs shell commands
        invisibly server-side; the endorse/fetch tools are host-side network calls), so a fresh
        screenshot/a11y-tree round trip after one of those is pure overhead for a screen that
        provably hasn't changed. This is the single place both fetch sites (the pre-dispatch policy
        gate and the post-dispatch action_list refresh) go through, so both benefit from the same
        cache instead of each doing its own independent fresh fetch.
        """
        settled = getattr(self.env, "_last_settled_obs", None)
        if settled is not None:
            self.env._last_settled_obs = None
            self._cached_obs_raw = settled
        elif self._cached_obs_raw is None:
            self._cached_obs_raw = self._fetch_obs_with_a11y()
        if self._requires_a11y() and not self._obs_usable(self._cached_obs_raw):
            self._cached_obs_raw = self._fetch_obs_with_a11y()
        return self._cached_obs_raw

    def _requires_a11y(self) -> bool:
        """True when this turn's observation must carry an accessibility tree -- i.e. masking is
        on, which is the only consumer that treats a missing tree as fatal."""
        return bool(self.mask_config and self.mask_config.enabled)

    @staticmethod
    def _obs_has_a11y(obs: dict | None) -> bool:
        return bool(obs and obs.get("accessibility_tree") and obs.get("screenshot"))

    _URL_ATTR_RE = re.compile(r'url="([^"]+)"')

    @staticmethod
    def _host_of(u: "str | None") -> "str | None":
        if not u:
            return None
        u = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", u.strip())
        u = u.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        return u.lower() or None

    def _web_obs_ready(self, obs: dict | None) -> bool:
        """False when the omnibox reports a real web page but the a11y tree has NOT yet caught up
        to it -- the fresh-navigation race behind the unsubscribe web-page masking bug.

        On a navigation Chrome repaints the new page (so the screenshot is the loaded page) before
        it rebuilds the accessibility tree for it: the old/transitional document is still what the
        tree describes. `resolve_content_roots` then finds no document-web content root for the
        omnibox's host, the host's page-scope TRUST rule has nothing to bind to, and the rendered
        page is default-denied -- painted black -- for that turn. `_current_url` reads the omnibox
        (which updates first), so the log even labels it with the loaded host, masking the mismatch.

        Detection is structural and needs no per-page rule: a loaded page's own elements carry
        `url="<scheme>://<host>/..."` on its host (the document's self-URL and its same-site links),
        so when the omnibox names a real page but NOTHING in the tree carries a url= on that host,
        the tree is still the previous page. Internal/non-browser surfaces (omnibox None) are always
        ready -- there is nothing to wait for. Fail-safe in every ambiguous case: return True, i.e.
        mask exactly as before, so this can only ever ADD a bounded settle, never mask less.
        """
        tree = obs.get("accessibility_tree") if obs else None
        if not tree:
            return True
        try:
            from mm_agents.accessibility_tree_wrap.coord_to_semantic import (
                get_active_tab_url,
            )
            url = get_active_tab_url(tree, include_internal=False)
        except Exception:
            return True
        host = self._host_of(url)
        if not host:
            return True
        for m in self._URL_ATTR_RE.finditer(tree):
            h = self._host_of(m.group(1))
            if h and (h == host or h.endswith("." + host) or host.endswith("." + h)):
                return True
        return False

    def _obs_usable(self, obs: dict | None) -> bool:
        """An observation masking can trust: it carries a tree AND that tree matches the page on
        screen. Both checks collapse to True when masking is off (no tree consumer)."""
        if not self._requires_a11y():
            return True
        return self._obs_has_a11y(obs) and self._web_obs_ready(obs)

    def _fetch_obs_with_a11y(self, attempts: int = 4, delay: float = 2.0) -> dict | None:
        """Fetch a fresh observation, retrying when the accessibility capture transiently fails OR
        when the tree has not yet caught up to a freshly-navigated page (see `_web_obs_ready`).

        Returns as soon as a usable observation is obtained (or immediately, when masking is off
        and no tree is required). If every attempt fails it returns the last observation anyway --
        the caller/`_build_mask` then aborts the turn loudly or masks conservatively, never silently
        masking nothing.
        """
        obs = None
        for i in range(attempts):
            try:
                obs = self.env._get_obs()
            except Exception as e:
                logger.warning("[sa][obs] _get_obs failed (attempt %d/%d): %s", i + 1, attempts, e)
                obs = None
            if self._obs_usable(obs):
                return obs
            if i < attempts - 1:
                reason = ("lacked an accessibility tree" if not self._obs_has_a11y(obs)
                          else "tree has not caught up to the navigated page")
                logger.info("[sa][obs] observation %s -- refetching (%d/%d)", reason, i + 1, attempts)
                time.sleep(delay)
        return obs

    def _build_mask(self, obs_raw: dict, fallback: bool = False):
        """This turn's redaction, installed into the policy gate. None when masking is off.

        Never a quiet fallback to the raw screenshot: handing the planner the unmasked page because
        labeling failed would switch the defense off at exactly the moment nobody understands why.
        A failure raises MaskUnavailable, which the caller retries on a fresh observation; with
        `fallback=True` (its last try) the turn is instead shown through masking.fallback_mask --
        as if no rule had been proposed: default-deny, nothing granted -- rather than ending the
        episode. MaskConfigError (a misconfigured rules directory) always raises.
        """
        if self.mask_config is None or not self.mask_config.enabled:
            return None
        from . import masking
        _resolved = active_endorsed_rects(
            obs_raw.get("accessibility_tree"),
            getattr(self.endorse_manager, "endorsed_rects", None),
            getattr(self.endorse_manager, "self_identity", ""),
            getattr(self.endorse_manager, "self_display_names", None),
        ) if self.endorse_manager is not None else []
        rects = [r["bbox"] for r in _resolved if not r.get("click_only")]
        click_rects = [r["bbox"] for r in _resolved if r.get("click_only")]

        if (obs_raw is self._mask_obs and self._mask_base is not None
                and self._mask_rects_n == len(_resolved)):
            if self.sa_policy is not None:
                self.sa_policy.set_mask(self._last_mask)
            self._log_mask_reuse(self._last_mask)
            return self._last_mask

        if obs_raw is self._mask_obs and self._mask_base is not None:
            base = self._mask_base
            fresh_base = False
        else:
            url = self._current_url(obs_raw)
            window_trust = (self.file_provenance_map.window_trust
                            if self.file_provenance_map is not None else None)
            basename_trust = (self.file_provenance_map.basename_trust
                              if self.file_provenance_map is not None else None)
            try:
                base = masking.build_mask(obs_raw, url, self.mask_config,
                                          window_trust=window_trust,
                                          basename_trust=basename_trust)
            except masking.MaskUnavailable as e:
                logger.error("[sa][mask] %s", e)
                if not fallback or isinstance(e, masking.MaskConfigError):
                    raise
                base = masking.fallback_mask(obs_raw, url, str(e))
            fresh_base = True

        if base is None:
            self._mask_base = self._last_mask = self._mask_obs = None
            if self.sa_policy is not None:
                self.sa_policy.set_mask(None)
            return None

        mask = (masking.overlay_endorsed(base, obs_raw.get("screenshot"), rects, click_rects)
                if (rects or click_rects) else base)
        self._mask_base = base
        self._last_mask = mask
        self._mask_obs = obs_raw
        self._mask_turn = self.turn
        self._mask_rects_n = len(_resolved)
        if self.sa_policy is not None:
            self.sa_policy.set_mask(mask)
        try:
            from mm_agents.sa_agent.tools import record_clear_identifiers
            record_clear_identifiers(self.endorse_manager, obs_raw, mask, self.turn)
        except Exception as e:
            logger.warning("[sa] record_clear_identifiers failed: %s", e)
        _n_self = sum(1 for r in _resolved if r.get("self")) if self.endorse_manager else 0
        logger.info("[sa][mask] %s: %.1f%% of screen redacted, %d region(s), %d rule(s)%s%s",
                    mask.stats.get("domain"), mask.stats.get("masked_pct", 0.0),
                    mask.stats.get("n_regions", 0), mask.stats.get("n_rules", 0),
                    f", {len(rects) - _n_self} endorsed row(s) shown"
                    if len(rects) - _n_self else "",
                    f", {_n_self} self-authored region(s) shown" if _n_self else "")
        if fresh_base:
            self._save_observation_pair(obs_raw, mask)
        return mask

    def _log_mask_reuse(self, mask) -> None:
        """Record a turn that reused the previous turn's redaction.

        The frames are not rewritten -- they would be byte-identical -- so without this row the
        results directory would show a turn with no masking record and no screenshot at all, which
        reads as "the mask failed here" rather than "the screen had not changed". The row is the
        same shape `_save_observation_pair` writes, plus `reused_from_turn` naming the turn whose
        `masked/`/`unmasked/` frames the planner was actually looking at (viz/server.py follows it).
        """
        if not self.example_result_dir or self._mask_turn is None:
            return
        import os
        try:
            with open(os.path.join(self.example_result_dir, "mask_stats.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps({"turn": self.turn, "url": mask.url, "regions": mask.regions,
                                    "reused_from_turn": self._mask_turn, **mask.stats},
                                   default=str) + "\n")
        except OSError as e:
            logger.warning("[sa][mask] could not write the mask-reuse row for turn %s: %s",
                           self.turn, e)

    def _save_observation_pair(self, obs_raw: dict, mask) -> None:
        """Write both screenshots for this turn: what the planner saw, and what was really there.

        Two directories rather than a `_masked` suffix in one, because they answer different
        questions and get looked at differently. `masked/` is the agent's actual input -- replaying
        a decision means looking at exactly these pixels. `unmasked/` is the ground truth needed to
        ask the questions the masked set cannot answer: did the redaction cover the injection, did
        it bury a control the task needed, how much of the page went dark. A reviewer scrubbing one
        directory in filename order gets a coherent sequence either way.

        Same turn index in both, so a pair is addressable without parsing timestamps. Written at
        MASK-BUILD time, not action time, so the two really are the same instant -- the existing
        step_N_*.png files (tools.py's one_step_action) are post-action and keep their own numbering.

        Failure here is logged and swallowed: losing a debugging artifact must never abort a run.
        """
        if not self.example_result_dir:
            return
        import os
        try:
            for sub, payload in (("masked", mask.png), ("unmasked", obs_raw.get("screenshot"))):
                if not payload:
                    continue
                d = os.path.join(self.example_result_dir, sub)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, f"turn_{self.turn:03d}.png"), "wb") as f:
                    f.write(payload)
            if os.getenv("SA_SAVE_A11Y") and obs_raw.get("accessibility_tree"):
                d = os.path.join(self.example_result_dir, "a11y")
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, f"turn_{self.turn:03d}.xml"), "w", encoding="utf-8") as f:
                    f.write(obs_raw["accessibility_tree"])
            with open(os.path.join(self.example_result_dir, "mask_stats.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps({"turn": self.turn, "url": mask.url,
                                    "regions": mask.regions, **mask.stats}, default=str) + "\n")
        except OSError as e:
            logger.warning("[sa][mask] could not write observation pair for turn %s: %s",
                           self.turn, e)

    def _current_url(self, obs_raw: dict) -> str | None:
        """The active tab's URL, read from the omnibox in this turn's accessibility tree.

        Which rule set applies depends on where the agent has actually navigated, not on where the
        episode started, so this is read fresh every turn.

        FROM THE TREE, and from the SAME tree the mask is built from. That is the point: the URL
        decides which rules run, the tree decides what they run over, and taking both from one
        observation makes "which rules apply" and "which page is being redacted" the same tab by
        construction -- the same argument that makes the picture and the policy gate one
        mask_image() call. The omnibox belongs to the selected tab, so no tab-picking heuristic is
        involved.

        This replaced a CDP round trip through ucm_agent's BrowserGuard, which was wrong twice.
        It picked the tab by `pages[-1]` -- "the most recently created non-blank page", which its
        own docstring flags as "not a verified foreground-window check" -- so with two tabs open it
        could resolve rules for one page while masking another. And it left a
        `sync_playwright().start()` running for the life of the episode, which made the NEXT
        episode's `_login_setup` fail with "Playwright Sync API inside the asyncio loop".

        INTERNAL PAGES ARE NAMED HERE, unlike everywhere else -- `include_internal=True`. The
        default suppresses `chrome://` because a trusted-domain check must not compare Settings
        against a site allowlist, but this caller is choosing a RULE SET, and an unnamed surface is
        not a neutral outcome for it. `labeling_store.resolve_content_roots` selects a document-web
        subtree structurally, with no omnibox check, so chrome://settings and chrome://extensions
        arrive as ordinary web roots; with no URL their scope is empty and `_resolve_rules`
        default-denies them, masking the whole page. That contradicts the other ladder, where
        `_frame_is_privileged` puts the same content in Zone.CHROME and never masks it. Naming the
        page moves the decision into a reviewed rule file (at-spi/llm_rules_container_overrides)
        instead of leaving the two paths to disagree. `chrome://new-tab-page` still returns None --
        its Most Visited tiles carry earlier pages' titles, so default-deny is right there.

        Returns None on the New Tab Page, or when no omnibox is in the tree -- e.g. a non-browser
        app has focus. None is passed straight through to _resolve_rules, which treats an unknown
        domain under --sa_mask_on_unknown_domain rather than guessing. There is deliberately no
        fall back to the episode's starting URL: the agent may have navigated anywhere since, and
        masking one page under another page's rules is the failure this whole per-turn lookup
        exists to avoid.
        """
        from mm_agents.accessibility_tree_wrap.coord_to_semantic import (
            get_active_tab_url,
        )
        try:
            url = get_active_tab_url(obs_raw.get("accessibility_tree") or "", include_internal=True)
        except Exception as e:
            logger.warning("[sa][mask] could not read the omnibox (%s); falling back to the "
                           "episode URL", e)
            url = None
        return url

    def _variables_in_scope(self) -> str:
        """The redacted variables the planner may reference, recapped at the TAIL of the request.

        This used to live in the tool SCHEMA, as an `enum` of the live memory tokens repeated
        inside every parameter of every tool (make_custom_generator). That put a value which
        changes mid-episode at the very FRONT of the prompt, where tool definitions are
        rendered, so every minted variable invalidated the whole cached prefix -- and cost
        ~728 prompt tokens per variable on top. Here it rides on the ephemeral observation
        message instead: last in the request, already uncached because the screenshot changes
        every turn, and ~15 tokens per variable rather than 728.

        It also carries something the enum never could: each variable's integrity level, so
        the planner can see at a glance what still needs endorsing before a write.
        """
        memory = getattr(self.planner, "memory", None) or {}
        if not memory:
            return ""
        store = getattr(self.planner, "label_store", {}) or {}
        lines = []
        for token in memory:
            label = store.get(token)
            level = getattr(getattr(getattr(label, "left", None), "level", None), "name", "UNKNOWN")
            lines.append(f"- {token} [{level}]")
        return ("Variables in scope (pass one as a variable_name argument; you cannot read its "
                "value):\n" + "\n".join(lines))

    def _observation_content(self, text: str, mask):
        """The per-turn observation message: image first when there is one, then the text block.

        Image-first is the prompt_agent shape and the point of the whole design -- the planner
        looks at the screen and decides, rather than reading a serialised tree and guessing.

        Only used when the trajectory window is OFF (`--sa_history_window=0`), where the message
        is ephemeral: rebuilt every turn, never appended to history, so image tokens do not
        accumulate across an episode. With the window on, the picture is persisted separately by
        `_apply_history_window` and this message carries the text alone.
        """
        if mask is None:
            return text
        b64 = base64.b64encode(mask.png).decode("ascii")
        parts = [{"type": "image_url",
                  "image_url": {"url": f"data:image/png;base64,{b64}"}}]
        if text:
            parts.append({"type": "text", "text": text})
        return parts

    def _apply_history_window(self, mask) -> str:
        """Append this turn's picture, then trim the planner's context to the last W elements.

        This is PromptAgent's shape, in this planner's vocabulary. PromptAgent re-sends the last
        `--max_trajectory_length` (observation, action) turns and drops everything older outright;
        here the unit is an ELEMENT -- one assistant tool call, its tool result, and the
        screenshot that followed it if the screen changed -- and the last `history_window` of them
        survive. Older elements are deleted from the message list exactly as PromptAgent deletes
        older turns, so the two frameworks carry comparable trajectories and a token comparison
        between them is a comparison of agents rather than of window sizes.

        The system prompt and the task are the preamble and are never trimmed: they are what
        PromptAgent's own system message is, and dropping the task would leave the planner
        working on nothing.

        WHICH ELEMENTS CARRY A PICTURE is not a list of tool names. It is decided by `_build_mask`,
        which hands back the SAME object whenever neither the screen nor the endorsed set has
        changed since the previous call. That identity check gets all three cases right on its own
        and cannot drift from what the masker actually does:

        * a UI action (`click`, `type`, and `wait` -- which exists precisely because the planner
          expects the screen to change under it) steps the VM, so `_current_obs` yields a new
          observation, the mask is rebuilt, and a new image goes in;
        * a read (`query_screen`, `endorse_by_crosscheck`, `fetch_web_content`, ...) touches
          nothing, the mask is handed back unchanged, and no image is added -- re-sending a
          byte-identical screenshot is the cost this design exists to avoid;
        * `endorse_element` steps nothing either, but it un-masks a row, so the OVERLAY changes
          and `_build_mask` returns a new object. That element gets a picture, and it must: the
          point of a reveal is that the planner can now read what was behind the redaction.

        So a 10-element window carries between 0 and 10 screenshots, against PromptAgent's
        invariant 10.

        Returns a one-line note for the ephemeral tail when elements have been dropped, or "".
        It goes on the tail rather than into the history because it changes every turn: in the
        history it would break the cached prefix at whatever point it sat.
        """
        if not self.history_window:
            return ""
        h = self.planner.history
        if mask is not None and mask is not self._windowed_mask:
            b64 = base64.b64encode(mask.png).decode("ascii")
            h.append(ChatCompletionUserMessageParam(role="user", content=[
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": f"(the screen after turn {self.turn})"},
            ]))
            self._windowed_mask = mask
        pre = 0
        while pre < len(h) and h[pre].get("role") == "system":
            pre += 1
        if pre < len(h) and h[pre].get("role") == "user":
            pre += 1
        self._preamble_len = pre
        starts = [i for i in range(pre, len(h)) if h[i].get("role") == "assistant"]
        if len(starts) <= self.history_window:
            return ""
        cut = starts[-self.history_window]
        self._dropped_history.extend(h[pre:cut])
        del h[pre:cut]
        n = len(starts) - self.history_window
        return (f"[{n} earlier tool call(s) are no longer shown: this context holds the last "
                f"{self.history_window}. Anything you still need from them is in the variables "
                "below, or must be re-observed.]")

    def _clipboard_available(self) -> bool:
        """One-time-per-episode probe for `xclip` (installed on the provisioned VM image alongside
        `wmctrl`/`socat` per desktop_env/server/README.md's setup steps -- both of which this
        codebase already depends on elsewhere). Cached so a missing binary costs one extra round
        trip per episode rather than one per turn; disables clipboard polling (never raises) if
        it's ever absent, matching this module's fail-soft-but-never-silently-wrong posture
        elsewhere (e.g. FileProvenanceMap's empty-manifest handling)."""
        if self._xclip_available is not None:
            return self._xclip_available
        available = False
        if self.env is not None and getattr(self.env, "controller", None) is not None:
            try:
                result = self.env.controller.execute_command("command -v xclip")
                available = isinstance(result, dict) and result.get("returncode") == 0
            except Exception:
                available = False
        if not available:
            logger.warning("xclip not found in the VM -- clipboard content polling is disabled for this episode.")
        self._xclip_available = available
        return available

    def _resolve_clipboard_content_label(
        self, screen_view: SAScreenView, text: str, a11y_xml: Optional[str]
    ) -> FidesLattice:
        """Attribute a label to newly-observed clipboard content by checking whether it actually
        appears anywhere on the current screen -- a whitespace-normalized substring match against
        the raw a11y tree, since most copyable text (e.g. a code block, a chat message) has no
        InteractableAction of its own to look up a precise per-node label for (affordances only
        cover interactive elements; see SAScreenView.affordances). A match means "this really
        did come from what's on screen right now", graded with the same conservative whole-screen
        bound target_metadata() falls back to for an unresolved click target:
        SAScreenView.extraction_label() (TRUSTED iff the screen has no DOCUMENT-zone content at
        all, else the join of everything DOCUMENT-zone on it -- never a precise single-node label,
        since nothing here can identify exactly which node it was). No match at all -- the
        clipboard holds something from a screen that's since navigated away, or was written by a
        channel this can't see (a shell pipe, another VM app) -- fails closed to fully untrusted():
        an unattributable clipboard change is the single most suspicious case, never a safe
        default."""
        needle = " ".join(text.split())
        if not needle:
            return untrusted_public()
        haystack = " ".join((a11y_xml or "").split())
        if needle in haystack:
            return screen_view.extraction_label()
        return untrusted_public()

    def _poll_clipboard(self, screen_view: Optional[SAScreenView], a11y_xml: Optional[str]) -> None:
        """Trigger-agnostic clipboard tracking. Rather than trying to recognize *which* UI
        mechanism put something on the clipboard (a hotkey, a page's own "Copy" button of
        whatever role/capability it happens to classify as, a native right-click context menu, a
        mouse-drag text selection) -- and instead of gating any of those triggers on the trust of
        what they copy, which they don't need (a copy is never destructive: it sends nothing
        anywhere, executes nothing, and its only effect is trivially overwritten by the next copy)
        -- this reads the VM's real clipboard every turn and diffs it against the last known
        value. Permission-to-copy and label-of-the-copied-content are separate axes (mirroring how
        `run_terminal_command` for a structural command is always permitted to execute, while the
        file it produces gets its own derived provenance via FileProvenanceMap.register_download,
        independent of that permission check) -- this method only ever handles the label side.

        Runs on every tool call regardless of name: the copy that produced the current clipboard
        content may have happened via *last* turn's action (any tool, any UI mechanism) -- polling
        now, against this turn's still-current screen, is what attributes it correctly.
        """
        if screen_view is None or self.env is None or getattr(self.env, "controller", None) is None:
            return
        if not self._clipboard_available():
            return
        for selection in ("clipboard", "primary"):
            try:
                result = self.env.controller.execute_command(f"xclip -o -selection {selection}")
            except Exception:
                logger.warning("Clipboard poll failed (%s)", selection, exc_info=True)
                continue
            if not isinstance(result, dict) or result.get("returncode") != 0:
                continue
            content = result.get("output", "") or ""
            if selection == "clipboard":
                if content == self._last_clipboard_content:
                    continue
                self._last_clipboard_content = content
                self._clipboard_label = self._resolve_clipboard_content_label(
                    screen_view, content, a11y_xml)
            else:
                if content == self._last_primary_content:
                    continue
                self._last_primary_content = content
                self._primary_label = self._resolve_clipboard_content_label(
                    screen_view, content, a11y_xml)

    def clipboard_taint_sources(self):
        """(text, label) for each paste buffer currently holding something -- what
        screen_labels.apply_clipboard_taint follows onto whatever node ends up rendering it."""
        return [(t, lab) for t, lab in
                ((self._last_clipboard_content, self._clipboard_label),
                 (self._last_primary_content, self._primary_label)) if t]

    def _track_held_keys(self, name: str, args: dict[str, Any]) -> None:
        """Remember which keys key_down has pressed and key_up has released.

        Only needed because a modifier combination can be spelled two ways. `hotkey(['ctrl','v'])`
        names the whole chord in one call; `key_down('ctrl')` followed by `key_down('v')` sends
        exactly the same keystrokes across two calls, and a paste check that reads only the
        arguments of the current call sees a lone 'v'.
        """
        key = str(args.get("key", "")).strip().lower()
        if not key:
            return
        if name == "key_down":
            self._held_keys.add(key)
        elif name == "key_up":
            self._held_keys.discard(key)

    def _is_paste(self, name: str, args: dict[str, Any],
                  screen_view: Optional[SAScreenView]) -> Optional[str]:
        """Which paste buffer, if any, this call is about to empty onto the screen.

        Returns "clipboard", "primary", or None. Four mechanisms, because the action space really
        does expose four, and the one that was checked (`hotkey`) is not the easiest to reach.
        """
        if name == "hotkey":
            keys = args.get("keys")
            if isinstance(keys, list):
                key_set = frozenset(str(k).strip().lower() for k in keys)
                if key_set in _PASTE_KEY_SETS:
                    return "clipboard"
            return None
        if name == "key_down":
            key = str(args.get("key", "")).strip().lower()
            held = {k for k in self._held_keys if k != key}
            for chord in _PASTE_KEY_SETS:
                if key in chord and (chord - {key}) <= held:
                    return "clipboard"
            return None
        if name in ("click", "mouse_down") and str(args.get("button", "")).lower() == "middle":
            return "primary"
        if name in ("click", "right_click") and screen_view is not None:
            try:
                hit = screen_view.affordance_at(float(args.get("x")), float(args.get("y")))
            except (TypeError, ValueError):
                hit = None
            if hit is not None and _PASTE_LABEL_RE.match(str(getattr(hit[0], "label", "") or "")):
                return "clipboard"
        return None

    def _apply_clipboard(self, name: str, args: dict[str, Any], args_label: FidesLattice,
                         screen_view: Optional[SAScreenView] = None) -> FidesLattice:
        """Join the tracked clipboard label into a paste's args_label, so the existing
        REVERSIBLE_WRITE gate (SAPolicy.check_tool_call) grades a paste of untrusted clipboard
        content exactly like typing that same content directly would, and the resulting
        result_label (loop(), a few lines below) carries it into current_label like any other
        tainted write. Population of self._clipboard_label happens separately and unconditionally
        in _poll_clipboard -- this only handles the *consuming* side, recognizing that this
        specific call is a paste. There is no equivalent trigger-recognition problem to solve here
        the way copy's was: unlike copy (reachable via many different triggers in this action
        space), `hotkey(['ctrl','v'])` is currently the only paste path the tool set exposes. A
        page/app's own click-driven "Paste" menu item would have the same blind spot copy used to
        have, but isn't reachable through this agent's action space today."""
        self._track_held_keys(name, args)
        buffer_name = self._is_paste(name, args, screen_view)
        if buffer_name is None:
            return args_label
        label = self._clipboard_label if buffer_name == "clipboard" else self._primary_label
        logger.info("[sa][clipboard] '%s' pastes the %s selection (%s)",
                    name, buffer_name, label.left.level.name)
        return args_label.join(label)

    _MIN_LAUNDER_MATCH = 6

    _LAUNDERING_GUARD_ACTIVE = False

    def _fold_laundered_literal_label(self, tool_args: dict, args_label: FidesLattice) -> FidesLattice:
        """Literal-laundering guard (policy; see [[sa-agent-trust-gate-literal-laundering]]).

        DISABLED -- see ``_LAUNDERING_GUARD_ACTIVE`` above; returns ``args_label`` unchanged.

        args_label is built only from the variables the planner *referenced* by token. A value the
        planner has already seen (anything shown in the clear) can instead be re-typed as a
        hand-typed `{"kind":"value"}` literal -- which drops the var_ref, so the value's real label
        (integrity AND confidentiality) never reaches the gate, and an ENDORSED/untrusted value
        can slip a DESTRUCTIVE floor or ride a send that leaves the org. This scans every hand-typed
        literal in the call against the content of every labelled memory value and folds the label
        of any value a literal reproduces back into args_label. Re-typing is not blocked; it simply
        can no longer shed a value's label past the tier floor or the confidentiality gate, which is
        the confidentiality guarantee value-expansion would otherwise violate."""
        if not self._LAUNDERING_GUARD_ACTIVE:
            return args_label
        literals: list[str] = []

        def _collect(v):
            if isinstance(v, str):
                literals.append(v)
                return
            if not isinstance(v, dict):
                return
            kind = v.get("kind")
            if kind == "value":
                literals.append(str(v.get("value", "")))
            elif kind == "template":
                for p in v.get("parts", []) or []:
                    if isinstance(p, dict) and p.get("kind") == "value":
                        literals.append(str(p.get("value", "")))

        for v in (tool_args or {}).values():
            _collect(v)
        blob = " ".join(" ".join(literals).split()).casefold()
        if not blob:
            return args_label
        label_store = getattr(self.planner, "label_store", {})
        memory = getattr(self.planner, "memory", {})
        folded = []
        for token, lbl in label_store.items():
            val = memory.get(token)
            if not isinstance(val, str):
                continue
            norm = " ".join(val.split()).casefold()
            if len(norm) >= self._MIN_LAUNDER_MATCH and norm in blob:
                args_label = args_label.join(lbl)
                folded.append(token)
        if folded:
            logger.info("[sa][laundering] a hand-typed literal reproduced labelled value(s) %s; "
                        "folded their real label into args_label so re-typing cannot launder them", folded)
        return args_label

    def _maybe_register_download(self, name: str, arguments: str, output: Any, result_label: FidesLattice) -> None:
        """Give a file/directory created live by run_terminal_command (currently only `git
        clone`) a real, graded provenance entry -- see FileProvenanceMap.register_download --
        derived from the same result_label the policy gate already computed for this call, so a
        later GUI view of that file reflects its actual trust level instead of the accident of
        unrelated pre-episode setup state that _integrity_of_with_provenance would otherwise fall
        back on.

        This is the correct hook point: it's the only place with both the tool name and the
        fully-resolved result_label. That join matters here specifically: result_label's
        output-grading half (endorsed_public() iff _is_structural_command, from
        hybrid_tools.run_terminal_command) is computed from the command text itself, independent
        of args_label/var_refs -- so even if the args-side were laundered via a hand-typed
        literal, join() always keeps the *less*-trusted side, meaning the registered provenance
        can't come out falsely-better than the command's own real output grading.
        """
        if name != "run_terminal_command" or self.file_provenance_map is None or not isinstance(output, str):
            return
        path = extract_cloned_path(output)
        if not path:
            return
        try:
            command = json.loads(arguments).get("command", "")
        except (TypeError, ValueError):
            command = ""
        integrity = result_label.left
        self.file_provenance_map.register_download(path, integrity, source=command)
        if integrity.level != IntegrityLabel.Level.TRUSTED:
            self._strip_execute_bit(path)

    _NETWORK_COMMANDS = frozenset({
        "curl", "wget", "wget2", "http", "https", "xh", "aria2c", "lwp-request", "lynx", "w3m",
        "nc", "ncat", "netcat", "socat",
        "ssh", "telnet", "ftp", "sftp", "lftp", "tftp", "rsync",
        "host", "dig", "nslookup",
    })
    _FILE_READ_COMMANDS = frozenset({
        "cat", "tac", "nl", "rev", "head", "tail", "less", "more", "most", "pg", "bat", "batcat", "view",
        "zcat", "zless", "bzcat", "bzless", "xzcat", "xzless", "lzcat", "lzless",
        "strings", "xxd", "hexdump", "od", "hd",
        "nano", "pico", "vi", "vim", "nvim", "gvim", "emacs", "emacsclient", "ed", "ex",
        "micro", "joe", "mcedit", "ne", "kak", "hx", "gedit", "kate", "mousepad", "code", "subl",
        "grep", "egrep", "fgrep", "rg", "ag", "ack", "ug", "ugrep",
        "awk", "gawk", "mawk", "nawk", "sed",
        "sort", "uniq", "cut", "tr", "paste", "join", "comm", "column", "fold", "fmt", "pr", "expand", "unexpand",
        "diff", "sdiff", "colordiff", "delta", "wdiff",
        "jq", "yq", "xq", "gron", "mlr", "csvtool", "csvlook",
        "pdftotext", "pandoc", "antiword", "catdoc", "unrtf", "docx2txt",
        "tar", "unzip", "7z", "7za", "unrar", "ar", "cpio", "zipinfo", "unar",
        "python", "python2", "python3", "ipython", "pypy",
        "bash", "sh", "zsh", "dash", "ksh", "fish",
        "node", "nodejs", "deno", "bun",
        "perl", "ruby", "php", "lua", "tclsh", "rscript",
        "sqlite3", "mysql", "psql", "mongo", "mongosh", "redis-cli",
    })
    _URL_OPERAND_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
    _SUBSTITUTION_RE = re.compile(r"\$\(|`|\$\{|<\(|>\(")
    _INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--command", "--eval", "-p", "-E"})
    _INTERPRETERS = frozenset({
        "python", "python2", "python3", "ipython", "pypy", "bash", "sh", "zsh", "dash", "ksh",
        "fish", "node", "nodejs", "deno", "bun", "perl", "ruby", "php", "lua", "tclsh", "rscript",
    })

    def _terminal_output_label(self, name: str, arguments: str) -> Optional[FidesLattice]:
        """The terminal rule (the masking design), framed as *mask untrusted content*: the terminal
        is NOT masked by default; a `run_terminal_command` result is masked only when it actually
        prints user-generated (attacker-reachable) content -- a network fetch, or a
        content-printing command reading a URL or an UNTRUSTED file.

        Returns UNTRUSTED (masked) iff, for any simple segment of the command (split on shell
        operators, so `cat evil | grep x` is judged by its `cat evil` half): the program is a
        `_NETWORK_COMMANDS` fetch, OR it is a `_FILE_READ_COMMANDS` printer given a URL operand or a
        file whose `path_trust` is False (known-untrusted). Otherwise ENDORSED (not masked) -- an
        empty terminal, `cd`, `ls`, `git clone`, or a read of a TRUSTED file. Joined with args_label
        by the caller, so an untrusted argument variable still masks. Returns None (keep the caller's
        default) only for a non-terminal tool or when there is no command to inspect.

        Provenance-scoped and content-scoped by design; the known residuals are
        a command substitution (`echo $(cat evil)`), an interpreter's inline code (`python -c`),
        or an untrusted file this episode never registered are not caught here."""
        if name != "run_terminal_command" or self.file_provenance_map is None:
            return None
        try:
            command = (json.loads(arguments) or {}).get("command", "")
        except (TypeError, ValueError):
            return None
        if not isinstance(command, str) or not command.strip():
            return None
        return (untrusted_public() if self._terminal_prints_untrusted(command)
                else endorsed_public())

    def _terminal_prints_untrusted(self, command: str) -> bool:
        from mm_agents.sa_agent import terminal_taint
        return terminal_taint.command_taints(
            command, self.file_provenance_map.path_trust, taint_on_unknown_file=False)

    def _strip_execute_bit(self, path: str) -> None:
        """Best-effort hardening for a download that didn't reach full TRUSTED: strip the execute
        bit from every regular *file* under path (never directories -- a directory's execute bit
        means traversal, not "run me"; blanket `chmod -R a-x` would make the download itself
        inaccessible, breaking e.g. a follow-up `ls -la`/opening a file inside it). Narrower than
        the policy gate -- doesn't stop `bash x.sh`/`python x.py` (interpreter-invoked execution
        never needs the +x bit), and the two real vectors in this environment are already gated
        without this (shell execution via run_terminal_command's own COMMAND-tier DESTRUCTIVE
        default; GUI double-click-to-run via the TARGET-tier gate, which depends on this same
        registration's label already, see _integrity_of_with_provenance) -- but raises the bar
        against the single most direct vector (./path) as a third, independent layer.
        Harness-triggered directly against the VM controller, mirroring endorse.py's
        EndorsementManager._vm_exec pattern for system-initiated commands: never routed
        through the planner's own tool-call/policy path, since this is not a model action and
        must not consume a turn or be blockable."""
        if self.env is None or getattr(self.env, "controller", None) is None:
            return
        try:
            self.env.controller.execute_command(f"find {shlex.quote(path)} -type f -exec chmod a-x {{}} +")
        except Exception:
            logger.warning("Failed to strip execute bit from downloaded path %s", path, exc_info=True)

    def _log_planner_thinking(self, reasoning_text: str | None, content_text: str | None) -> None:
        """Persist the P-LLM's raw reasoning (previously only printed to stdout as
        "[P-LLM thinking]"/"[P-LLM content]" in get_tool_response(), never written to disk
        anywhere) so it survives past the live run and show_trajectory.py can display it.
        One call to get_tool_response() from loop() below = exactly one line here = exactly
        one new assistant turn appended to self.planner.history, so this file's Nth line
        lines up 1:1 with the Nth assistant turn in planner_history.json.

        Also mirrors the planner's visible text onto env.agent_responses, which is what
        mm_agents/utils/loop.py does for PromptAgent and what the ucm_utility evaluator
        reads (getters/ucm.py::_model_logs) to score the 57 answer-only tasks. Without
        it a SA-Agent run reports "0 model replies" and every such task scores 0 with no
        information in it. This is the right hook because it fires exactly once per
        assistant turn, so the recorded sequence matches PromptAgent's one-per-reply."""
        if isinstance(content_text, str) and content_text.strip():
            try:
                self.env.agent_responses.append(content_text)
            except AttributeError:
                pass
        if not self.example_result_dir:
            return
        if reasoning_text is None and content_text is None:
            return
        try:
            with open(f"{self.example_result_dir}/planner_thinking.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"reasoning": reasoning_text, "content": content_text}) + "\n")
        except OSError:
            pass

    def _dump_variable_store(self, where: str = "") -> None:
        """Debug window (OFF unless the env var ``SA_DEBUG_VARS`` is set): print every variable the
        planner holds -- its token, integrity level, and a short preview of the RAW value -- so a run
        can be read for WHICH value sits behind WHICH token at WHAT trust level.

        This deliberately prints the unredacted value the planner itself never sees in the clear, so
        it is a developer/debugging aid only. It reads ``planner.memory`` (token -> value) and
        ``planner.label_store`` (token -> ProductLabel); the integrity level is ``label.left.level.name``
        and confidentiality is ``label.right``.
        """
        if not os.environ.get("SA_DEBUG_VARS"):
            return
        memory = getattr(self.planner, "memory", {}) or {}
        label_store = getattr(self.planner, "label_store", {}) or {}
        tokens = list(dict.fromkeys(list(memory) + list(label_store)))
        if not tokens:
            return
        lines = [f"[sa][vars]{(' after ' + where) if where else ''}: {len(tokens)} variable(s)  "
                 f"(token  [integrity/confidentiality]  = value)"]
        for tok in tokens:
            lbl = label_store.get(tok)
            try:
                integ = lbl.left.level.name if lbl is not None else "no-label"
                conf = str(lbl.right) if lbl is not None else ""
            except Exception:
                integ, conf = "?", ""
            if tok in memory:
                preview = repr(memory.get(tok))
                if len(preview) > 100:
                    preview = preview[:97] + "..."
            else:
                preview = "<label only, no value in memory>"
            trust = integ + (f"/{conf}" if conf else "")
            lines.append(f"    {tok:<32} [{trust:<28}] = {preview}")
        logger.info("\n".join(lines))

    def loop(self, msg: ChatCompletionMessage | ChatCompletionMessageParam, label: FidesLattice) -> tuple[str, FidesLattice]:
        """
        Run the planning loop with dynamic taint-tracking.

        May raise PolicyViolation if the policy is violated.
        """

        trace: list[tuple[Action, FidesLattice]] = []
        current_msg = msg
        current_label = label
        while (self.max_steps is None or self.step_idx < self.max_steps) and self.turn < 70:
            token_usage.set_step(self.step_idx + 1)
            if current_msg is None:
                pass

            action, label = self.planner.next_action(current_msg, current_label)
            self._write_planner_state()
            trace.append((action, label))

            match action:
                case Query(messages, tools):
                    if self.sa_policy is not None:
                        from . import masking as _masking
                        _MASK_TRIES = 3
                        for _mtry in range(_MASK_TRIES):
                            obs_raw = self._current_obs()
                            screen_view = label_screen(obs_raw.get("accessibility_tree"), self.principal_map, self.file_provenance_map)
                            if self.endorse_manager is not None:
                                apply_endorsed_regions(screen_view, active_endorsed_rects(
                                    obs_raw.get("accessibility_tree"),
                                    self.endorse_manager.endorsed_rects,
                                    getattr(self.endorse_manager, "self_identity", ""),
                                    getattr(self.endorse_manager, "self_display_names", None)))
                            try:
                                mask = self._build_mask(obs_raw)
                                break
                            except _masking.MaskConfigError:
                                raise
                            except _masking.MaskUnavailable as e:
                                transient = any(s in str(e) for s in (
                                    "accessibility tree", "no screenshot", "no content root"))
                                if not transient or _mtry == _MASK_TRIES - 1:
                                    logger.warning("[sa][mask] MaskUnavailable (%s) -- continuing "
                                                   "with the no-rules fallback mask", e)
                                    mask = self._build_mask(obs_raw, fallback=True)
                                    break
                                logger.warning("[sa][mask] transient MaskUnavailable (%s) -- forcing "
                                               "a fresh observation and retrying (%d/%d)",
                                               e, _mtry + 1, _MASK_TRIES)
                                self._cached_obs_raw = None
                                if getattr(self, "env", None) is not None:
                                    self.env._last_settled_obs = None
                                time.sleep(2.0)
                        action_list = render_action_list(screen_view, self.planner, self.turn,
                                                         mask=mask)
                        self._log_action_list(action_list)
                        content = (
                            "" if mask is not None else
                            "Current action_list (fresh every turn, not saved to history):\n"
                            + json.dumps({"action_list": action_list})
                        )
                        action_list_repr = json.dumps(action_list, sort_keys=True, default=str)
                        if (self._last_action_list_repr is not None
                                and action_list_repr == self._last_action_list_repr
                                and self._last_was_ui_action):
                            self._stale_repeat_count += 1
                        else:
                            self._stale_repeat_count = 0
                        self._last_action_list_repr = action_list_repr
                        if self._stale_repeat_count >= 2:
                            content += (
                                f"\n\nNOTE: the screen's available controls have not changed for "
                                f"{self._stale_repeat_count} turns in a row despite your last "
                                "action(s) actually executing. The control you're trying to reach "
                                "may be inside a popup or confirmation dialog that the "
                                "accessibility tree isn't exposing. Stop repeating the same "
                                "action -- call `query_screen` to inspect the current screen "
                                "before deciding what to do next."
                                if mask is not None else
                                "\n\nNOTE: the action_list above has not changed for "
                                f"{self._stale_repeat_count} turns in a row despite your last "
                                "action(s) actually executing. The control you're trying to reach "
                                "may be inside a popup or confirmation dialog that the "
                                "accessibility tree isn't exposing. Stop repeating the same "
                                "action -- call `query_screen` to inspect the current screen "
                                "before deciding what to do next."
                            )
                        variables_block = self._variables_in_scope()
                        if variables_block:
                            content = (content + "\n\n" + variables_block) if content else variables_block
                        dropped_note = self._apply_history_window(mask)
                        if dropped_note:
                            content = (content + "\n\n" + dropped_note) if content else dropped_note
                        observation_msg = ChatCompletionUserMessageParam(
                            role="user",
                            content=(content if self.history_window
                                     else self._observation_content(content, mask)),
                        )
                        messages = (messages + [observation_msg]) if (
                            content or not self.history_window) else list(messages)
                    current_msg = get_tool_response(self.model, messages, tools, token_log_path=self.token_log_path)
                    self._log_planner_thinking(
                        getattr(current_msg, "reasoning_content", None) or getattr(current_msg, "reasoning", None),
                        current_msg.content,
                    )
                case ToolCall(id, name, arguments):
                    self.turn += 1
                    self.env._last_settled_obs = None

                    var_refs = list(getattr(action, "var_refs", []) or [])
                    label_store = getattr(self.planner, "label_store", {})
                    args_label = trusted_public()
                    for ref in var_refs:
                        if ref in label_store:
                            args_label = args_label.join(label_store[ref])

                    obs_raw = None
                    screen_view = None
                    if self.sa_policy is not None:
                        obs_raw = self._current_obs()
                        a11y_xml = obs_raw.get("accessibility_tree")
                        screen_view = label_screen(a11y_xml, self.principal_map, self.file_provenance_map)
                        self._poll_clipboard(screen_view, a11y_xml)
                        _retainted = apply_clipboard_taint(screen_view, self.clipboard_taint_sources())
                        if self.endorse_manager is not None:
                            apply_endorsed_regions(screen_view, active_endorsed_rects(
                                a11y_xml, self.endorse_manager.endorsed_rects,
                                getattr(self.endorse_manager, "self_identity", ""),
                                getattr(self.endorse_manager, "self_display_names", None)))
                        if _retainted:
                            logger.info("[sa][clipboard] %d on-screen node(s) re-labelled: they "
                                        "render content a paste buffer holds", _retainted)
                        tool_args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
                        args_label = self._apply_clipboard(name, tool_args, args_label, screen_view)
                        args_label = self._fold_laundered_literal_label(tool_args, args_label)
                        try:
                            self.sa_policy.check_tool_call(
                                name=name,
                                args=tool_args,
                                args_label=args_label,
                                screen_view=screen_view,
                                var_refs=var_refs,
                            )
                        except PolicyViolation as violation:
                            if self._last_blocked_call == (name, arguments):
                                self._consecutive_blocks += 1
                            else:
                                self._consecutive_blocks = 1
                                self._last_blocked_call = (name, arguments)
                            reason = violation.reason
                            if self._consecutive_blocks >= 3:
                                reason += (
                                    " You have repeated this blocked call; it will keep being blocked."
                                    " Choose a different approach that does not feed tainted data into"
                                    " a consequential action."
                                )
                            logger.warning(f"Policy blocked tool call '{name}' (refs={var_refs}): {reason}")
                            trace.append((action, args_label))
                            self._write_planner_state()
                            current_msg = ChatCompletionToolMessageParam(
                                role="tool", tool_call_id=id,
                                content="Policy blocked your last tool call: " + reason,
                            )
                            continue
                        self._last_blocked_call = None
                        self._consecutive_blocks = 0

                    if action.name not in non_ui_tool_list:
                        self.step_idx += 1
                    recent_actions = None
                    try:
                        result_tool = call_tool(self.tools, name, arguments, self.qllm_agent, self.agent, self.env, self.example_result_dir, self.step_idx, self.observation_type, self.qllm_agent_second, self.model_defense_level, obs_raw=obs_raw, endorse_manager=self.endorse_manager, recent_actions=recent_actions, mask=self._last_mask)
                    except PolicyViolation:
                        raise
                    except Exception as exc:
                        detail = tool_error_detail(name, exc)
                        logger.warning("Tool call '%s' raised %s: %s",
                                       name, type(exc).__name__, exc, exc_info=not isinstance(exc, ToolRefusal))
                        self._consecutive_tool_errors += 1
                        repeated = self._last_failed_call == (name, arguments)
                        self._last_failed_call = (name, arguments)
                        if action.name not in non_ui_tool_list:
                            self.step_idx -= 1
                        if self._consecutive_tool_errors >= _MAX_CONSECUTIVE_TOOL_ERRORS:
                            logger.error("%d consecutive tool errors -- giving up on this episode.",
                                         self._consecutive_tool_errors)
                            raise
                        if repeated:
                            detail += (" This is the same call that just failed, and it will keep"
                                       " failing. Change the question, the answer_type, or the"
                                       " tool -- do not retry it unchanged.")
                        elif self._consecutive_tool_errors >= 2:
                            detail += (" Several tool calls have now failed in a row; try a"
                                       " different approach to this step.")
                        trace.append((action, args_label))
                        self._write_planner_state()
                        current_msg = ChatCompletionToolMessageParam(
                            role="tool", tool_call_id=id,
                            content=TOOL_ERROR_PREFIX + detail,
                        )
                        continue
                    self._consecutive_tool_errors = 0
                    self._last_failed_call = None
                    self._last_was_ui_action = getattr(self.env, "_last_settled_obs", None) is not None

                    output = getattr(result_tool.root, "output", None)
                    if isinstance(output, str) and output.startswith("Your last tool call had invalid arguments.") and action.name not in non_ui_tool_list:
                        self.step_idx -= 1

                    _own_label = metadata_to_label(result_tool.root.metadata)
                    result_label = (_own_label if name in VERIFIER_TOOLS
                                    else _own_label.join(args_label))
                    _term_label = self._terminal_output_label(name, arguments)
                    if _term_label is not None:
                        result_label = _term_label.join(args_label)
                    self._maybe_register_download(name, arguments, output, result_label)
                    current_label = result_label.join(current_label)
                    if hasattr(self.planner, "pending_result_label"):
                        self.planner.pending_result_label = result_label

                    payload = result_tool.model_dump()
                    if name in getattr(self.planner, "clear_result_tools", ()):
                        current_msg = ChatCompletionToolMessageParam(
                            role="tool", tool_call_id=id,
                            content=output if isinstance(output, str) else json.dumps(output))
                        self._dump_variable_store(name)
                        continue
                    if self.sa_policy is not None and isinstance(payload, dict):
                        payload["integrity"] = result_label.left.level.name
                    current_msg = ChatCompletionToolMessageParam(role="tool", tool_call_id=id, content=json.dumps(payload))
                    self._dump_variable_store(name)

                case Response(response):
                    self._write_planner_state()
                    return response, current_label
                case _:
                    raise ValueError("Invalid action")
        self._write_planner_state()
        return "", current_label 


class LabeledBasicPlanner(LabeledPlanner):
    def __init__(self, state: list[ChatCompletionMessageParam], tools: list[Tool], model: str):
        self.tools = tools
        self.history = state
        self.model = model
        self.memory: dict[str, Any] = {}
        self.label_store: dict[str, FidesLattice] = {}
        self.pending_result_label: FidesLattice = trusted_public()
        self.last_var_refs: list[str] = []
        self.clear_result_tools: frozenset[str] = frozenset()

    _VAR_REF = re.compile(r"\$(?:\{([^}]*)\}|([A-Za-z_][A-Za-z0-9_.\-]*))")
    _TOKEN_SHAPED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*-result-\d+(\.|$)")

    def _note_var_ref(self, token: str) -> None:
        """Record that the tool call currently being expanded reads *token*."""
        if token not in self.last_var_refs:
            self.last_var_refs.append(token)

    def _resolve_dollar_refs(self, arg_name: str, text: str):
        """Resolve `$token` references in *text* against memory.

        This is the one way a variable's value reaches a tool argument. The planner types the
        TOKEN, never the value, which is what keeps provenance intact: every token resolved
        here goes through `_note_var_ref`, so its real label joins `args_label` and the policy
        gate still sees it (see LabeledPlanningLoop.loop). A value the planner hand-types
        instead carries no label at all and is invisible to the gate -- that is the laundering
        hole this convention exists to keep closed, and why "just type it yourself" is never an
        equivalent move.

        Two shapes, and the difference is deliberate:
          - the WHOLE argument is one reference -> the memory value is returned AS IS, so a
            non-string value keeps its type instead of being stringified;
          - the reference sits inside a larger string -> everything is concatenated, which is
            what the retired `template` argument kind used to do ("git clone $url_1").

        `$$` is a literal dollar sign.

        An unresolvable `$word` splits two ways, and the split is the safety-relevant part.
        A word shaped like a memory token (`<tool>-result-<n>.<field>`) that is NOT in memory is
        a typo'd reference, and it RAISES: letting it through as literal text would hand the
        tool an untracked string where the planner meant a labelled reference, which is the
        laundering hole in reverse. Anything else -- `$HOME`, `$PATH`, `$1` in a shell command
        the planner types into a terminal -- is ordinary text and is left exactly as written,
        because the terminal is a real destination for real shell syntax.
        """
        if "$" not in text:
            return text
        out: list[str] = []
        i, n = 0, len(text)
        whole = None
        while i < n:
            if text[i] != "$":
                out.append(text[i]); i += 1; continue
            if i + 1 < n and text[i + 1] == "$":
                out.append("$"); i += 2; continue
            m = self._VAR_REF.match(text, i)
            if not m:
                out.append("$"); i += 1; continue
            braced = m.group(1) is not None
            token = m.group(1) if braced else m.group(2)
            consumed = m.end()
            if not braced:
                while token and token not in self.memory and token[-1] in ".-":
                    token = token[:-1]
                consumed = i + 1 + len(token)
            if not token or token not in self.memory:
                if braced or self._TOKEN_SHAPED.match(token or ""):
                    raise KeyError(
                        f"Variable {token or m.group(0)} not found in memory for argument "
                        f"{arg_name}"
                    )
                out.append(m.group(0)); i = m.end(); continue
            self._note_var_ref(token)
            value = self.memory[token]
            if i == 0 and consumed == n:
                whole = value
            out.append(str(value))
            i = consumed
        return whole if whole is not None else "".join(out)

    def _resolve_template_parts(self, arg_name: str, parts: list) -> str:
        """Resolve a legacy 'template' argument's *parts* into one concatenated string.

        Superseded by `$token` interpolation (see _resolve_dollar_refs) and kept only so a model
        emitting the older wrapper shape still runs. Every variable part goes through
        `_note_var_ref` exactly as a `$` reference does, so the trust gate sees the same thing
        either way.
        """
        resolved = []
        for part in parts:
            if not isinstance(part, dict) or "kind" not in part:
                resolved.append(str(part))
                continue
            if part["kind"] == "value":
                resolved.append(str(part.get("value", "")))
            elif part["kind"] in ("variable", "variable_name"):
                var_name = part["variable"] if part["kind"] == "variable" else part.get("value")
                self._note_var_ref(var_name)
                try:
                    resolved.append(str(self.memory[var_name]))
                except KeyError:
                    raise KeyError(f"Variable {var_name} not found in memory for argument {arg_name} (template part)")
            else:
                raise ValueError(f"Invalid kind for template part in argument {arg_name}: {part['kind']}")
        return "".join(resolved)

    def _expand_one(self, arg_name: str, v):
        """One argument, from what the model emitted to what the tool receives."""
        if isinstance(v, str):
            return self._resolve_dollar_refs(arg_name, v)
        if isinstance(v, list):
            return [self._resolve_dollar_refs(arg_name, x) if isinstance(x, str) else x for x in v]
        if isinstance(v, dict) and "kind" in v:
            kind = v["kind"]
            if kind == "value":
                val = v.get("value")
                return self._resolve_dollar_refs(arg_name, val) if isinstance(val, str) else val
            if kind == "template":
                return self._resolve_template_parts(arg_name, v.get("parts", []))
            if kind in ("variable", "variable_name"):
                var_name = v["variable"] if kind == "variable" else v.get("value")
                self._note_var_ref(var_name)
                try:
                    return self.memory[var_name]
                except KeyError:
                    raise KeyError(f"Variable {var_name} not found in memory for argument {arg_name}")
            raise ValueError(f"Invalid kind for argument {arg_name}: {kind}")
        return v

    def _expand_args(self, args: dict[str, Any]) -> str:
        """Expand a tool call's arguments into what the tool receives, JSON-serialised.

        Arguments arrive as plain JSON values (the schema declares plain types) and a variable
        is a `$token` inside a string one. There is no per-model-family branch any more: the
        old gpt/non-gpt split existed only because the two families emitted different shapes of
        the `kind` wrapper, and with the wrapper gone there is one convention for everyone.
        """
        self.last_var_refs = []
        actual_args = {}
        for a, v in args.items():
            try:
                actual_args[a] = self._expand_one(a, v)
            except KeyError:
                if a == "obs":
                    actual_args[a] = {"screenshot": "", "accessibility_tree": ""}
                else:
                    raise
        return json.dumps(actual_args)

    def _simple_expand_args(self, args: dict[str, Any]) -> str:
        """Kept as an alias: there is one expansion path now (see _expand_args)."""
        return self._expand_args(args)

    def next_action(self, message: ChatCompletionMessage | ChatCompletionMessageParam, label: FidesLattice) -> tuple[Action, FidesLattice]:
        
        tools = [ tool.to_dict_var(list(self.memory.keys())) for tool in self.tools ]

        match message:
            case {"role": "user"}:
                self.history.append(message)
                return Query(
                    messages=self.history,
                    tools=tools
                ), label
            case {"role": "tool", "content": content, "tool_call_id": tool_call_id}:

                tool_name = self.history[-1]["tool_calls"][0]["function"]["name"] if self.history and "tool_calls" in self.history[-1] and self.history[-1]["tool_calls"] else "unknown_tool"

                if isinstance(content, str) and (
                    "Your last tool call had invalid arguments." in content
                    or "Your last tool call had an invalid tool name." in content
                    or content.startswith("Policy blocked your last tool call")
                    or content.startswith(TOOL_ERROR_PREFIX)
                ):
                    content_redacted = content
                elif tool_name in self.clear_result_tools:
                    n = sum(1 for msg in self.history
                            if "tool_calls" in msg and msg["tool_calls"]
                            and msg["tool_calls"][0]["function"]["name"] == tool_name)
                    token = f"{tool_name}-result-{n}.output"
                    self.memory[token] = content
                    self.label_store[token] = self.pending_result_label
                    content_redacted = content
                else:
                    try:
                        content_dict = json.loads(content)
                        content_redacted = redact_tool_result(
                            content_dict, self.memory, self.history,
                            label_store=self.label_store, label=self.pending_result_label,
                        )
                    except:
                        logger.warning(f"Tool output is not valid JSON: {content}. Storing entire output as a single variable.")
                        content_dict = {"root": content}
                        content_redacted = redact_leaves(content_dict, self.memory, self.history, saving_to_memory=True, label_store=self.label_store, label=self.pending_result_label)

                    logger.debug(f"Tool call '{tool_name}' raw (unredacted, hidden-from-planner) content: {content_dict}")

                logger.info(f"Tool call '{tool_name}' returned content: {content_redacted}")
                if tool_name.startswith("done") or tool_name.startswith("fail") or tool_name.startswith("abstain"):
                    return Response(response=tool_call_id), label
                self.history.append(ChatCompletionToolMessageParam(role="tool", content=str(content_redacted), tool_call_id=tool_call_id))
                return Query(
                    messages=self.history,
                    tools=[ tool.to_dict_var(list(self.memory.keys())) for tool in self.tools ]
                ), label
            case ChatCompletionMessage(role = "assistant", content = content, tool_calls = tool_calls) if tool_calls:
                if len(tool_calls) != 1:
                    print("Warning: Only one tool call is supported. Using the first tool call from the list.")
                    tool_calls = [tool_calls[0]] if tool_calls else None

                                
                tool_calls_param : list[ChatCompletionMessageToolCallParam] = [{ 
                        "id": tool_call.id,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments
                        },
                        "type": "function"
                    }
                    for tool_call in tool_calls
                ]
                self.history.append(ChatCompletionAssistantMessageParam(role="assistant", content=content, tool_calls=tool_calls_param))

                try: 
                    return ToolCall(
                        id=tool_calls[0].id,
                        name=tool_calls[0].function.name,
                        arguments=self._expand_args(json.loads(tool_calls[0].function.arguments)),
                        var_refs=list(self.last_var_refs),
                    ), label
                except Exception as e:
                    content = """Your last tool call had invalid arguments. Please check the tool documentation and try again. Tool call was: """ + str(tool_calls[0]) + ". The error value is redacted for security purposes. Note to always return tool call arguments in the anyOf format, with 'value', 'variable_name' or 'template' kinds."
                    self.history.append(ChatCompletionToolMessageParam(role="tool", content=content, tool_call_id=tool_calls[0].id))
                    return Query(
                        messages=self.history,
                        tools=tools
                    ), label
            case ChatCompletionMessage(role = "assistant", content = content, tool_calls = tool_calls) if content:
                assert not tool_calls, "Tool calls are not supported in this context"
                self.history.append(ChatCompletionAssistantMessageParam(role="assistant", content=content, tool_calls=None))
                return Response(
                    response=content
                ), label
            case _:
                raise ValueError("Invalid message format")