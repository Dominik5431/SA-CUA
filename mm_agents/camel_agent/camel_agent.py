"""
Minimal standalone CamelAgent that wraps the CaMeL prompt-injection defense
pipeline for OSWorld.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import openai
from agentdojo import agent_pipeline, functions_runtime
import agentdojo.task_suite.load_suites

_CAMEL_SRC = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..",
        "submodules", "camel-cua", "camel-prompt-injection-main", "src",
    )
)
if not os.path.isdir(_CAMEL_SRC):
    raise ImportError(
        "CamelAgent needs the CaMeL pipeline checkout, which this repository does not "
        f"ship. Expected it at {_CAMEL_SRC!r}. See 'Optional baselines' in the README; "
        "every other agent runs without it."
    )
if _CAMEL_SRC not in sys.path:
    sys.path.insert(0, _CAMEL_SRC)

import desktop_env.desktop_env as _rtc_desktop_env_mod
sys.modules.setdefault("osworld.desktop_env.desktop_env", _rtc_desktop_env_mod)


def _install_headless_pyautogui() -> None:
    """Let the camel submodule import pyautogui on a host with no X display.

    `import pyautogui` touches Xlib three times at module load (mouseinfo, then
    _pyautogui_x11, then the size() call at the bottom of its __init__), so on a headless
    host it dies with `KeyError: 'DISPLAY'` -- which is what aborted every CamelAgent run
    started without the Xvfb wrapper in run_qwen38_camel_reddit1.sh.

    Nothing here needs a host display. The submodule's base_ui_*.py import pyautogui but
    read exactly ONE name from it, `KEYBOARD_KEYS`, a static list of key names used to
    build the `Key` enum the planner is allowed to emit (base_ui_uitars.py:249). Every
    actual pyautogui CALL in those files is a *string* ("pyautogui.click(...)") shipped to
    the guest VM and executed by desktop_env/controllers/python.py, against the guest's own
    pyautogui and the guest's own display. So CamelAgent is in the same position as every
    other agent here (SA-Agent, PromptAgent, ...): the host drives the VM over HTTP and
    never automates its own screen -- only this one module-level import said otherwise.

    So when DISPLAY is unset we register a stub module carrying that one list, read out of
    the installed pyautogui's source without executing it. Any *other* attribute raises, so
    a host-side pyautogui call added later fails loudly rather than silently doing nothing.
    With DISPLAY set (the Xvfb wrapper, or a desktop session) nothing is stubbed and the
    real module is imported as before.
    """
    if os.environ.get("DISPLAY") or "pyautogui" in sys.modules:
        return

    import ast
    import importlib.util
    import types

    spec = importlib.util.find_spec("pyautogui")
    if spec is None or not spec.origin:
        return

    literals: dict = {}
    aliases: dict = {}
    with open(spec.origin, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if isinstance(node.value, ast.Name):
            for name in names:
                aliases[name] = node.value.id
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            continue
        for name in names:
            literals[name] = value

    name = "KEYBOARD_KEYS"
    for _ in range(len(aliases) + 1):
        if name in literals:
            break
        if name not in aliases:
            break
        name = aliases[name]
    keys = literals.get(name)
    if not isinstance(keys, list) or not keys:
        raise RuntimeError(
            f"CamelAgent: could not read KEYBOARD_KEYS out of {spec.origin} to build a "
            "headless pyautogui stub. Run with a display instead (start an Xvfb and export "
            "DISPLAY, as run_qwen38_camel_reddit1.sh does)."
        )

    stub = types.ModuleType("pyautogui")
    stub.__doc__ = (
        "Headless stand-in installed by mm_agents/camel_agent/camel_agent.py. Carries only "
        "KEYBOARD_KEYS; pyautogui actions run inside the guest VM, not on this host."
    )
    stub.KEYBOARD_KEYS = list(keys)
    stub.KEY_NAMES = list(keys)
    stub.FAILSAFE = False

    def _missing(attr: str):
        raise AttributeError(
            f"pyautogui.{attr} is not available: this host has no DISPLAY, so CamelAgent "
            "imported a stub carrying only KEYBOARD_KEYS. Host-side screen automation is "
            "never correct here -- actions belong in the guest VM, as a 'pyautogui.*' "
            "string passed to the controller. To use the real module, start an Xvfb and "
            "export DISPLAY (see run_qwen38_camel_reddit1.sh)."
        )

    stub.__getattr__ = _missing
    sys.modules["pyautogui"] = stub
    logging.getLogger("desktopenv.agent").info(
        "CamelAgent: no DISPLAY -- imported a headless pyautogui stub (%d key names); "
        "guest-side actions are unaffected.", len(keys),
    )


_install_headless_pyautogui()

from camel.pipeline_elements.privileged_llm import PrivilegedLLM
from camel_osworld.osworld_security_policies import OSWorldSecurityPolicyEngine
from camel_osworld.osworld_sys_usr_prompt import (
    default_system_prompt_generator_osworld_pllm,
)
from camel_osworld.osworld_benchmark.suites.base_ui_uitars import BaseUI
from camel_osworld.osworld_benchmark.suites.osworld.base_ui_task_suite_uitars import (
    UIEnvironment,
    TOOLS,
)
from camel_osworld.agent_pipeline_vlms.uitars_vlm import UITarsVLM
from camel.interpreter import namespace as ns
from camel.pipeline_elements.agentdojo_function import make_agentdojo_namespace

logger = logging.getLogger("desktopenv.agent")


def _patch_agentdojo_system_developer() -> None:
    """Keep 'system' messages as 'system' for non-reasoning OpenAI models."""
    import agentdojo.agent_pipeline.llms.openai_llm as _oai_llm
    from openai.types.chat import ChatCompletionSystemMessageParam

    _original = _oai_llm._message_to_openai

    def _patched(message, model_name: str):
        if message["role"] == "system" and not _oai_llm._is_reasoning_model(model_name):
            return ChatCompletionSystemMessageParam(
                role="system",
                content=_oai_llm._content_blocks_to_openai_content_blocks(message),
            )
        return _original(message, model_name)

    _oai_llm._message_to_openai = _patched


def _patch_agentdojo_max_tokens() -> None:
    """Bound the generation length of every agentdojo OpenAI call (P-LLM / Q-LLM / VLM-text).

    agentdojo's chat_completion_request issues create() with NO max_tokens. Against a vLLM
    server whose max_model_len is huge (Qwen3.8-27B advertises 262k), an omitted cap lets a
    reasoning model generate against the entire remaining context -- the P-LLM 'Prompting for
    code' step then runs for 15+ minutes and looks hung. Cap the completion budget so it stops
    in bounded time. THINKING STAYS ENABLED: we do not touch reasoning_effort or the chat
    template, only the total token budget (thinking + answer must fit within it). Tunable
    without a code edit via CAMEL_PLLM_MAX_TOKENS (default 16384). Mirrors the 8192 cap the
    SA/Prompt executors already hardcode for the same reason.
    """
    import agentdojo.agent_pipeline.llms.openai_llm as _oai_llm

    _cap = int(os.environ.get("CAMEL_PLLM_MAX_TOKENS", "16384"))

    def _capped(client, model, messages, tools, reasoning_effort, temperature=0.0):
        return client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools or _oai_llm.NOT_GIVEN,
            tool_choice="auto" if tools else _oai_llm.NOT_GIVEN,
            temperature=temperature or _oai_llm.NOT_GIVEN,
            reasoning_effort=reasoning_effort or _oai_llm.NOT_GIVEN,
            max_tokens=_cap,
        )

    _oai_llm.chat_completion_request = _capped


_QWEN_FIND_COORD_SPACE = """

## Coordinate Space
All coordinates you output in start_box / end_box MUST be relative coordinates on a normalized 1000x1000 screen, where (0,0) is the top-left corner of the screenshot and (1000,1000) is the bottom-right corner, regardless of the screen's real pixel resolution. Do NOT output raw screen pixels."""


def _patch_qwen_coordinate_space(vlm_model: str) -> None:
    """Put a Qwen VLM's find() coordinates on the 0-1000 grid it was trained on.

    base_ui_uitars converts between the model's coordinates and screen pixels through
    BaseUI._smart_resize(), which returns the Qwen2.5-VL / UI-TARS-1.5 *absolute pixels in
    the smart-resized image* grid -- 1092x1932 for a 1920x1080 screen, i.e. a factor of
    0.994, essentially the identity. A Qwen VL emits the normalised 0-1000 grid instead, so
    every click landed at 0.52x its intended x (y survived only because 1080 is close to
    1000): in the thunderbird-phishing-gmail run the VLM's (472, 134) meant the composer's
    Attach button at (906, 145) and was clicked at (469, 132), on empty toolbar.

    Overriding _smart_resize() to 1000x1000 is the whole fix, because both conversions read
    it: _coords_to_pixels() then computes value / 1000 * screen -- the arithmetic
    PromptAgent's _scale_coords() already applies to this model family -- and
    find_element_by_text()'s forward conversion maps accessibility-tree pixel boxes onto the
    same grid, so the two locators stay in one coordinate space. The screenshot itself is
    untouched: it is sized by _threshold_resize(), which does not consult _smart_resize().

    Gated on uses_normalized_coords() -- the repo's single source of truth for which models
    speak that grid -- and paired with the prompt block above, because a prompt and a
    rescaler that disagree misplace every click by exactly the width/1000 factor.
    """
    from mm_agents.utils.code_parsing import uses_normalized_coords

    if not uses_normalized_coords(vlm_model):
        return

    from camel_osworld.osworld_benchmark.suites import base_ui_uitars as _bu

    if "## Coordinate Space" not in _bu.UITARS_USR_PROMPT_FIND:
        _bu.UITARS_USR_PROMPT_FIND = (
            _bu.UITARS_USR_PROMPT_FIND.rstrip() + _QWEN_FIND_COORD_SPACE
        )
    _bu.BaseUI._smart_resize = lambda self: (1000, 1000)
    logger.info(
        "CamelAgent: VLM %r is on the normalised 0-1000 coordinate grid (prompt + "
        "rescaling), matching PromptAgent.", vlm_model,
    )


_patch_agentdojo_system_developer()
_patch_agentdojo_max_tokens()


class CamelAgent:
    """OSWorld agent backed by the CaMeL prompt-injection defense.

    vlm -> UiTarsVLm with any VLM model, used to execute functions from plan like find, summarize_screenshot etc.
    q_llm -> Q-LLM used for defense/checking, should support images when model_defense_level=4 
    q_llm_second_model -> Q-LLM used for second check, should support images, used when model_defense_level >= 3
    
    model_level_defense = 0 -> no defense
    model_level_defense = 1 -> DOM Consistency (from paper, I think)
    model_level_defense = 3 -> Multi-Modal Consensus (not sure, but closest to paper I think)
    """

    def __init__(
        self,
        *,
        p_llm_base_url: str,
        p_llm_model: str,
        p_llm_api_key: str | None = None,
        vlm_base_url: str,
        vlm_model: str,
        vlm_api_key: str | None = None,
        q_llm_base_url: str | None,
        q_llm_model: str,
        q_llm_second_model: str,
        q_llm_api_key: str | None = None,
        model_defense_level: int = 0,
        system_defense_level: int = 1,
        pllm_exposure_level: int = 0,
        replan: bool = False,
        plan_only: bool = False,
        vlm_use_relative_coords: bool = False,
    ) -> None:
        self._p_llm_base_url = p_llm_base_url
        self._p_llm_model = p_llm_model
        self._p_llm_api_key = p_llm_api_key

        self._vlm_base_url = vlm_base_url
        self._vlm_model = vlm_model
        self._vlm_api_key = vlm_api_key

        self._q_llm_base_url = q_llm_base_url
        self._q_llm_model = q_llm_model
        self._q_llm_second_model = q_llm_second_model
        self._q_llm_api_key = q_llm_api_key
        if q_llm_base_url:
            os.environ["OPENAI_BASE_URL"] = q_llm_base_url
            os.environ["OPENAI_API_KEY"] = q_llm_api_key or "EMPTY"
            logger.info(
                "CamelAgent: Q-LLM endpoint -> OPENAI_BASE_URL=%s (model %s)",
                q_llm_base_url, q_llm_model,
            )

        self._model_defense_level = model_defense_level
        self._system_defense_level = system_defense_level
        self._pllm_exposure_level = pllm_exposure_level
        self._replan = replan
        self._plan_only = plan_only
        self._vlm_use_relative_coords = vlm_use_relative_coords

        self.env: Any = None
        self._ran = False
        self._stored_result: List[str] = ["FAIL"]

        self.cua = False
        self.safety_check_cnt = 0
        self.action_space = "pyautogui" 
        self.observation_type = "screenshot_a11y_tree"
        self.step_idx: int = 0
        self.example_result_dir: Path | None = None


    @staticmethod
    def _make_openai_client(
        base_url: str, api_key: str | None = None
    ) -> openai.OpenAI:
        client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key or "EMPTY",
            timeout=900.0,
        )
        if "openrouter.ai" in (base_url or ""):
            from mm_agents.openrouter import GROWING, apply_prompt_cache_policy

            apply_prompt_cache_policy(client, history=GROWING)
        _cap = int(os.environ.get("CAMEL_PLLM_MAX_TOKENS", "16384"))
        _orig_create = client.chat.completions.create

        def _capped_create(*args, **kwargs):
            mt = kwargs.get("max_tokens")
            if not isinstance(mt, int):
                kwargs["max_tokens"] = _cap
            return _orig_create(*args, **kwargs)

        client.chat.completions.create = _capped_create
        logger.info("CamelAgent: completion budget capped at max_tokens=%d (base_url=%s)", _cap, base_url)
        return client

    @property
    def _plan_cache_path(self) -> Path:
        return self.example_result_dir / "plan_code.txt"

    def _plan_cache_exists(self) -> bool:
        return self._plan_cache_path.exists()

    def _save_plan_cache(self) -> None:
        """Copy the generated plan to a stable path under the example dir."""
        for pattern in ["**/plan_0.txt", "**/model_output_0.txt"]:
            candidates = list(self.example_result_dir.glob(pattern))
            if candidates:
                code = candidates[0].read_text(encoding="utf-8")
                self._plan_cache_path.write_text(code, encoding="utf-8")
                return
        logger.warning("CamelAgent: no plan file found to cache.")

    def _build_pipeline(self):
        """Build VLM, BaseUI, UIEnvironment, tools runtime, and PrivilegedLLM."""
        p_llm_client = self._make_openai_client(self._p_llm_base_url, self._p_llm_api_key)
        p_llm = agent_pipeline.OpenAILLM(p_llm_client, self._p_llm_model, None)

        vlm_client = self._make_openai_client(self._vlm_base_url, self._vlm_api_key)
        vlm = UITarsVLM(client=vlm_client, model=self._vlm_model, temperature=0.6)
        _patch_qwen_coordinate_space(self._vlm_model)

        base_ui = BaseUI(env=self.env, vlm=vlm)
        base_ui.model_defense_level = self._model_defense_level
        if "use_relative_coords" in type(base_ui).model_fields:
            base_ui.use_relative_coords = self._vlm_use_relative_coords
        elif self._vlm_use_relative_coords:
            logger.warning(
                "CamelAgent: --camel_vlm_use_relative_coords set but this BaseUI has no such "
                "field; ignoring (coordinate mode is fixed in this submodule version)."
            )
        base_ui.q_llm = self._q_llm_model
        base_ui.q_llm_second_check = self._q_llm_second_model
        base_ui.defense_map = {
            0: base_ui.model_defense_0,
            1: base_ui.model_defense_1,
            2: base_ui.model_defense_2,
            3: base_ui.model_defense_3,
            4: base_ui.model_defense_4,
        }

        ui_env = UIEnvironment(base_ui=base_ui)

        tool_list = TOOLS[self._system_defense_level]
        runtime = functions_runtime.FunctionsRuntime()
        for tool in tool_list:
            runtime.register_function(tool)

        privileged_llm = PrivilegedLLM(
            llm=p_llm,
            security_policy_engine=OSWorldSecurityPolicyEngine,
            quarantined_llm_model=self._q_llm_model,
            system_prompt_generator=default_system_prompt_generator_osworld_pllm,
            max_attempts=1,
            pllm_exposure_level=self._pllm_exposure_level,
            system_defense_level=self._system_defense_level,
            part_path=self.example_result_dir,
            plan_analysis_only=self._plan_only,
        )

        return privileged_llm, ui_env, runtime

    def _execute_cached_plan(
        self,
        privileged_llm: PrivilegedLLM,
        ui_env: UIEnvironment,
        runtime: functions_runtime.FunctionsRuntime,
    ) -> None:
        """Load the cached plan from disk and execute it via the interpreter."""
        code = self._plan_cache_path.read_text(encoding="utf-8")
        logger.info("CamelAgent: reusing cached plan (%d chars)", len(code))

        privileged_llm._part_path = self.example_result_dir
        privileged_llm._path = self.example_result_dir

        ui_env.base_ui.path = self.example_result_dir

        builtins_namespace = ns.Namespace.with_builtins()
        namespace = builtins_namespace.add_variables(
            make_agentdojo_namespace(builtins_namespace, runtime, ui_env)
        )

        _, _, interpretation_error, _, _, _ = privileged_llm.run_code(
            code, ui_env, namespace, ()
        )

        if interpretation_error:
            logger.error("CamelAgent: cached plan execution error: %s", interpretation_error)
            raise RuntimeError(f"Cached plan execution failed: {interpretation_error}")

        logger.info("CamelAgent: cached plan executed successfully.")

    def predict(
        self, instruction: str, obs: Dict[str, Any]
    ) -> Tuple[str, List[str], bool]:
        """Run the CaMeL defence pipeline and return ``["DONE"]`` or ``["FAIL"]``."""
        self.example_result_dir = Path(os.environ.get("CURRENT_EXAMPLE_DIR", "."))
        if self._ran:
            return "", self._stored_result, False

        if self.env is None:
            raise RuntimeError("CamelAgent.env not set – inject DesktopEnv before predict()")

        logger.info("CamelAgent: building CaMeL pipeline …")

        try:
            privileged_llm, ui_env, runtime = self._build_pipeline()

            if self._plan_only:
                if not self._replan and self._plan_cache_exists():
                    logger.info("CamelAgent: plan-only + replan=False, cache hit – nothing to do.")
                else:
                    logger.info("CamelAgent: plan-only – generating plan via P-LLM …")
                    privileged_llm.query(
                        query=instruction,
                        runtime=runtime,
                        env=ui_env,
                    )
                    self._save_plan_cache()
                self._stored_result = ["DONE"]
            elif not self._replan and self._plan_cache_exists():
                logger.info("CamelAgent: replan=False, cache hit – executing cached plan …")
                self._execute_cached_plan(privileged_llm, ui_env, runtime)
                self._stored_result = ["DONE"]

            else:
                logger.info("CamelAgent: running full PrivilegedLLM pipeline …")
                privileged_llm.query(
                    query=instruction,
                    runtime=runtime,
                    env=ui_env,
                )
                self._save_plan_cache()
                self._stored_result = ["DONE"]

            logger.info("CamelAgent: pipeline finished successfully.")

        except Exception as exc:
            logger.exception("CamelAgent: pipeline failed – %s", exc)
            self._stored_result = ["FAIL"]

        self._ran = True
        return "", self._stored_result, False

    def reset(
        self,
        _logger: logging.Logger | None = None,
        tags: Dict[str, Any] | None = None,
    ) -> None:
        """Reset internal state so predict() runs again on the next example."""
        self._ran = False
        self._stored_result = ["FAIL"]
        if _logger is not None:
            global logger
            logger = _logger
