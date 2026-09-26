from pathlib import Path
from typing import Annotated

from pydantic import Field

from agentdojo.functions_runtime import TaskEnvironment, make_function, Depends
from agentdojo.task_suite.task_suite import TaskSuite

from camel_osworld.osworld_benchmark.suites.redteamcua.build_ui import (
    build_ui_redteamcua_vllm,
    build_ui_redteamcua_anthropic,
)
from camel_osworld.osworld_benchmark.suites.base_ui_uitars import (
    BaseUI,
    find, run_single_uitars, check_done, drag, select, click,
    left_single, left_double, right_single, hover, scroll,
    type_text, hotkey, press, keydown, keyup, wait,
    mark_done, mark_fail, reset_memory, summarize_screenshot_content,
    no_op, get_google_credentials, get_page_elements, get_page_text,
    find_element_by_text, verify_hypothesis,
)
from camel_osworld.osworld_benchmark.suites.base_ui_anthropic import (
    BaseUI_Anthropic,
    left_single as a_left_single,
    left_double as a_left_double,
    right_single as a_right_single,
    left_triple, middle_click, left_press, left_mouse_down, left_mouse_up,
    hover as a_hover, drag as a_drag, scroll as a_scroll,
    type_text as a_type_text, key, hold_key,
    find as a_find, run_single_claude, check_done as a_check_done,
    summarize_screenshot_content as a_summarize,
    wait as a_wait, mark_done as a_mark_done, mark_fail as a_mark_fail,
    reset_memory as a_reset_memory, get_google_credentials as a_get_google_credentials,
    no_op as a_no_op, get_page_elements as a_get_page_elements,
    get_page_text as a_get_page_text, find_element_by_text as a_find_element_by_text,
    verify_hypothesis as a_verify_hypothesis,
)


class UIEnvironment(TaskEnvironment):
    base_ui: Annotated[
        BaseUI,
        Depends(build_ui_redteamcua_vllm),
    ] = Field(default=None, exclude=True)

    def model_copy(self, *, deep: bool = False) -> "UIEnvironment":
        return super().model_copy(deep=False)


class UIEnvironment_Anthropic(TaskEnvironment):
    base_ui: Annotated[
        BaseUI_Anthropic,
        Depends(build_ui_redteamcua_anthropic),
    ] = Field(default=None, exclude=True)

    def model_copy(self, *, deep: bool = False) -> "UIEnvironment_Anthropic":
        return super().model_copy(deep=False)


# vLLM tool sets — mirror osworld's levels
_VLLM_TOOLS_0 = [
    find, run_single_uitars, check_done, drag, select, click,
    left_single, left_double, right_single, hover, scroll,
    type_text, hotkey, press, keydown, keyup, wait,
    mark_done, mark_fail, reset_memory, summarize_screenshot_content,
    get_google_credentials, no_op, get_page_elements, get_page_text,
    find_element_by_text, verify_hypothesis,
]
_VLLM_DISALLOWED = {summarize_screenshot_content, get_page_elements, get_page_text, verify_hypothesis}
_VLLM_TOOLS_1 = [t for t in _VLLM_TOOLS_0 if t != run_single_uitars]
_VLLM_TOOLS_2 = [t for t in _VLLM_TOOLS_1 if t not in _VLLM_DISALLOWED]
VLLM_TOOLS = {0: _VLLM_TOOLS_0, 1: _VLLM_TOOLS_1, 2: _VLLM_TOOLS_2}

# Anthropic tool sets
_ANTHROPIC_TOOLS_0 = [
    a_left_single, a_left_double, a_right_single, left_triple,
    middle_click, left_press, left_mouse_down, left_mouse_up,
    a_hover, a_drag, a_scroll, a_type_text, key, hold_key,
    a_find, run_single_claude, a_check_done, a_summarize,
    a_wait, a_mark_done, a_mark_fail, a_reset_memory,
    a_get_google_credentials, a_no_op, a_get_page_elements,
    a_get_page_text, a_find_element_by_text, a_verify_hypothesis,
]
_ANTHROPIC_DISALLOWED = {a_summarize, a_get_page_elements, a_get_page_text, a_verify_hypothesis}
_ANTHROPIC_TOOLS_1 = [t for t in _ANTHROPIC_TOOLS_0 if t != run_single_claude]
_ANTHROPIC_TOOLS_2 = [t for t in _ANTHROPIC_TOOLS_1 if t not in _ANTHROPIC_DISALLOWED]
ANTHROPIC_TOOLS = {0: _ANTHROPIC_TOOLS_0, 1: _ANTHROPIC_TOOLS_1, 2: _ANTHROPIC_TOOLS_2}

HERE = Path(__file__).resolve().parent
SUITE_DATA = (HERE / ".." / ".." / "data" / "suites" / "redteamcua").resolve()


def make_redteamcua_task_suite(
    system_defense_level: int = 0,
) -> TaskSuite[UIEnvironment]:
    tools = VLLM_TOOLS.get(system_defense_level, _VLLM_TOOLS_0)
    return TaskSuite[UIEnvironment](
        "redteamcua",
        UIEnvironment,
        [make_function(t) for t in tools],
        SUITE_DATA,
    )


def make_redteamcua_task_suite_Anthropic(
    system_defense_level: int = 0,
) -> TaskSuite[UIEnvironment_Anthropic]:
    tools = ANTHROPIC_TOOLS.get(system_defense_level, _ANTHROPIC_TOOLS_0)
    return TaskSuite[UIEnvironment_Anthropic](
        "redteamcua",
        UIEnvironment_Anthropic,
        [make_function(t) for t in tools],
        SUITE_DATA,
    )


__all__ = [
    "UIEnvironment",
    "UIEnvironment_Anthropic",
    "make_redteamcua_task_suite",
    "make_redteamcua_task_suite_Anthropic",
    "SUITE_DATA",
]
