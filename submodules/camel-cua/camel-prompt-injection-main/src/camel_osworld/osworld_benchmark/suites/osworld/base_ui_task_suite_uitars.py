from pathlib import Path
import os
import openai
from typing import Annotated

from pydantic import Field

from agentdojo.functions_runtime import TaskEnvironment, make_function, Depends
from agentdojo.task_suite.task_suite import TaskSuite

from osworld.desktop_env.desktop_env import DesktopEnv

from camel_osworld.osworld_benchmark.suites.base_ui_uitars import BaseUI, find, run_single_uitars, check_done, drag, select, click, left_single, left_double, right_single, hover, scroll, type_text, hotkey, press, keydown, keyup, wait, mark_done, mark_fail, reset_memory, summarize_screenshot_content, no_op, get_google_credentials,get_page_elements,get_page_text,find_element_by_text, verify_hypothesis 
from camel_osworld.agent_pipeline_vlms.uitars_vlm import UITarsVLM


def build_ui(
        defense_level: int = 0,
        q_llm: str | None = None,
        q_llm_second_check: str | None = None,
        path_to_vm: str | None = None,
        path_to_vlm: str | None = None,
        port: int = 15001,
        token_count_file: str | None = None,
    ) -> BaseUI:  # <--- add argument
    if path_to_vm is None:
        raise ValueError("path_to_vm must be provided as an absolute path.")
    if not Path(path_to_vm).is_absolute():
        raise ValueError(f"path_to_vm must be an absolute path, got relative path: {path_to_vm}")
    env = DesktopEnv(
        path_to_vm=path_to_vm,
        action_space="pyautogui",
        screen_size=(1920,1080),
        headless=True,
        os_type="Ubuntu",
        require_a11y_tree=True,
    )

    client = openai.OpenAI(
        base_url=f"http://localhost:{port}/v1",
        api_key=os.getenv("UITARS_API_KEY"),
        timeout=300.0, 
    )

    vlm = UITarsVLM(
        client=client,
        model=path_to_vlm,
        temperature=0.6,
    )

    ui = BaseUI(env=env, vlm=vlm)
    ui.model_defense_level = defense_level
    ui.q_llm = q_llm
    ui.q_llm_second_check = q_llm_second_check
    ui.defense_map = {
        0: ui.model_defense_0,
        1: ui.model_defense_1,
        2: ui.model_defense_2,
        3: ui.model_defense_3,
        4: ui.model_defense_4,
    }
    ui.token_count_file = token_count_file
    return ui


class UIEnvironment(TaskEnvironment):
    base_ui: Annotated[
        BaseUI,
        Depends(build_ui),
    ] = Field(default=None, exclude=True)
    
    def model_copy(self, *, deep: bool = False) -> "UIEnvironment":
        # always do a shallow copy, even if deep=True
        return super().model_copy(deep=False)



TOOLS_0 = [
    find,
    run_single_uitars,
    check_done,
    drag,
    select,
    click,
    left_single,
    left_double,
    right_single,
    hover, 
    scroll, 
    type_text, 
    hotkey, 
    press, 
    keydown, 
    keyup, 
    wait, 
    mark_done, 
    mark_fail,
    reset_memory,
    summarize_screenshot_content,
    get_google_credentials,
    no_op,
    get_page_elements,
    get_page_text,
    find_element_by_text,
    verify_hypothesis
]
DISALLOWED = {
    summarize_screenshot_content,
    get_page_elements,
    get_page_text,
    verify_hypothesis
}
TOOLS_1 = [tool for tool in TOOLS_0 if tool != run_single_uitars]
TOOLS_2 = [tool for tool in TOOLS_1 if tool not in DISALLOWED]
TOOLS_BASELINE = [tool for tool in TOOLS_1 if tool != verify_hypothesis]

TOOLS = {j: v for j, v in enumerate([TOOLS_0, TOOLS_1, TOOLS_2])}

HERE = Path(__file__).resolve().parent
SUITE_DATA = (
    HERE
    / ".."  # up from camel_osworld/
    / ".."  # up from src/
    / "data"
    / "suites"
    / "osworld"
)
SUITE_DATA = SUITE_DATA.resolve()


def make_osworld_task_suite(
        system_defense_level: int = 0,
        unoptimized_cua: bool = False) -> TaskSuite[UIEnvironment]:

    tools = TOOLS_BASELINE if unoptimized_cua else TOOLS[system_defense_level]
    return TaskSuite[UIEnvironment](
        "osworld",
        UIEnvironment,
        [make_function(tool) for tool in tools],
        SUITE_DATA,
    )
