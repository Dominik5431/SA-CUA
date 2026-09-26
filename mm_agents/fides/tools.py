import ast
import datetime
from io import BytesIO
import json
import logging
import os
import re
from typing import Any, List, Literal, Optional, Union
from typing_extensions import Required, TypedDict
from argparse import ArgumentParser
from PIL import Image

from pydantic import BaseModel, Field, StrictStr, ValidationError, constr
from mm_agents.fides.claude import FidesAnthropicAgent
from mm_agents.fides.lattice import IntegrityLabel, InverseLattice, PowersetLattice, ProductLabel
from mm_agents.fides.pydantic_scheme import MetaValue, enforce_type

from mm_agents.fides.uitars import FidesUITARSAgent
from mm_agents.fides.opencua import FidesOpenCUAAgent
from osworld.desktop_env.desktop_env import DesktopEnv

from mm_agents.fides.qllm import QLLMAgent
from mm_agents.fides.model_defenses import apply_model_defense, ModelDefenseResults

from openai.types.chat import (
    ChatCompletionToolParam
)

logger = logging.getLogger("desktopenv.experiment")

import base64

def image_bytes_to_base64_str(image_bytes: bytes) -> str:
    img = Image.open(BytesIO(image_bytes))
    
    if img.mode in ("RGBA", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.getchannel("A"))
        img = background
    else:
        img = img.convert("RGB")
    
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def base64_str_to_image_bytes(b64_str: str) -> bytes:
    return base64.b64decode(b64_str)


def one_step_action(
        action: str,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int) -> dict[str, Union[str, bytes, Any]]:
    """
    Log the observation, reward, done status, and additional info from the environment.
    It also excutes the actions in the environment and saves the screenshot and trajectory information.
    """
    action_timestamp = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")
    logger.info("Step %d", step_idx)


    obs_raw, reward, done, info = env.step(action, 5.0)
    logger.info("Reward: %.2f", reward)
    logger.info("Done: %s", done)
    with open(os.path.join(example_result_dir, f"step_{step_idx}_{action_timestamp}.png"),
            "wb") as _f:
        _f.write(obs_raw['screenshot'])
    with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:
        f.write(json.dumps({
            "step_num": step_idx,
            "action_timestamp": action_timestamp,
            "action": action,
            "reward": reward,
            "done": done,
            "info": info,
            "screenshot_file": f"step_{step_idx}_{action_timestamp}.png"
        }))
        f.write("\n")

    return obs_raw


class Observation(BaseModel):
    screenshot : str
    accessibility_tree : str

    model_config = {
        "extra": "forbid"  
    }

class RunVLMParams(BaseModel):
    instruction : MetaValue[str]


class QLLMQueryParams(BaseModel):
    instruction : MetaValue[str]
    output_type : str


class QLLMCheckdoneVLMParams(BaseModel):
    instruction : MetaValue[str]
    actions : list[str]
    response : str
    pre_obs : MetaValue[Observation]
    post_obs : MetaValue[Observation]


class QLLMCheckdoneParams(BaseModel):
    instruction : MetaValue[str]


class QLLMSummarizerParams(BaseModel):
    instruction : MetaValue[str]


class FindParams(BaseModel):
    instruction : MetaValue[str]


class StandardActionParams(BaseModel):
    action : str


class UITarsOutput(BaseModel):
    status: str

class SummarizerOutput(BaseModel):
    output: Any 

class FindOutput(BaseModel):
    x: float
    y: float
    status: str

class RunVLMResults(BaseModel):
    root: MetaValue[UITarsOutput] = Field(..., description="The output of one step of VLM.")

class QLLMQueryResults(BaseModel):
    root: MetaValue[Any]

class StandardActionResults(BaseModel):
    root: MetaValue[Any]

class FindResults(BaseModel):
    root: MetaValue[Any]

def extract_json_from_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    match = re.search(r'{.*}', text, re.DOTALL)
    return match.group(0) if match else text

def parse_checkdone(output_str: str) -> UITarsOutput:
    try:
        json_str = extract_json_from_text(output_str)
        data = json.loads(json_str)
        return UITarsOutput(**data)

    except json.JSONDecodeError as e:
        print(f"While checking the completion of the checkdone function, a JSON parsing error occurred: {e}.")
        return UITarsOutput(status="FAIL")
    
    except ValidationError as e:
        print(f"While checking the completion of the checkdone function, a Pydantic validation error occurred: {e}.")
        return UITarsOutput(status="FAIL")
    
def parse_summarizer(output_str: str) -> SummarizerOutput:
    try:
        json_str = extract_json_from_text(output_str)
        data = json.loads(json_str)
        return SummarizerOutput(**data)

    except json.JSONDecodeError as e:
        logger.error("JSONDecodeError in parse_summarizer: %s", str(e))
        return SummarizerOutput(output=output_str)
    
    except ValidationError as e:
        msg = f"While checking the completion of the ui_tars function, a Pydantic validation error occurred."
        return SummarizerOutput(output=output_str)
    

def parse_find(output_str: str) -> SummarizerOutput:
    try:
        json_str = extract_json_from_text(output_str)
        data = json.loads(json_str)
        return FindOutput(**data)

    except json.JSONDecodeError as e:
        msg = f"While checking the completion of the ui_tars function, a JSON parsing error occurred."
        return FindOutput(x=0.0, y=0.0)
    
    except ValidationError as e:
        msg = f"While checking the completion of the ui_tars function, a Pydantic validation error occurred."
        return FindOutput(x=0.0, y=0.0)


def summarize_screenshot_content(
        params: QLLMSummarizerParams,
        agent: FidesUITARSAgent | FidesAnthropicAgent | FidesOpenCUAAgent,
        model_defense_level: int = 0,
        qllm_agent: QLLMAgent = None,
        qllm_agent_second: QLLMAgent = None,
        obs_raw: dict = None
    ) -> QLLMQueryResults:
    """
    Take an instruction, and screenshot, and an output type, and return a summary of the screenshot content with the output type.
    """

    response = agent.summarize_screenshot_content(
        instruction = params.instruction.value,
        screenshot = image_bytes_to_base64_str(obs_raw["screenshot"]) if obs_raw is not None else "",
        output_type = "str",
    )

    parsed_response = parse_summarizer(
        output_str='{"output": ' + str(json.dumps(response)) + '}'
    )

    parsed_response.output = enforce_type(
        value=parsed_response.output,
        type_str="str"
    )

    model_defense_results = apply_model_defense(
        instruction=params.instruction.value,
        thought=parsed_response.output,
        action="Summarize screenshot content",
        dom=(obs_raw.get("accessibility_tree") or "") if obs_raw is not None else "",
        screenshot=obs_raw.get("screenshot", b"") if obs_raw is not None else b"",
        qllm_agent=qllm_agent,
        qllm_agent_second=qllm_agent_second,
        model_defense_level=model_defense_level
    )

    if model_defense_results.root.value.detected == "ATTACKED":
        logger.error("Model defense detected a prompt injection attack in the summarizer output.")
        raise ValueError("Model defense detected a prompt injection attack in the summarizer output.")
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
    }

    return QLLMQueryResults(root=MetaValue(value=parsed_response, metadata=metadata))



def checkdone_run_vlm(
        params: QLLMCheckdoneVLMParams,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent,
        observation_type: Literal["screenshot", "screenshot_a11y_tree"] = "screenshot",
        ) -> RunVLMResults:
    """
    Check if the instruction given to the run_vlm function was realised as expected.
    """

    pre_obs = params.pre_obs.value.model_dump()
    pre_obs["screenshot"] = base64_str_to_image_bytes(pre_obs["screenshot"]) if isinstance(pre_obs["screenshot"], str) else pre_obs["screenshot"]
    pre_obs["accessibility_tree"] = params.pre_obs.value.accessibility_tree if hasattr(params.pre_obs.value, 'accessibility_tree') else ""

    post_obs = params.post_obs.value.model_dump()
    post_obs["screenshot"] = base64_str_to_image_bytes(post_obs["screenshot"]) if isinstance(post_obs["screenshot"], str) else post_obs["screenshot"]
    post_obs["accessibility_tree"] = params.post_obs.value.accessibility_tree if hasattr(params.post_obs.value, 'accessibility_tree') else ""

    results = agent.checkdone_run_vlm(
        instruction=params.instruction.value,
        actions=params.actions,
        response=params.response,
        pre_obs=pre_obs,
        post_obs=post_obs,
    )
     
    output = parse_checkdone(
        output_str=results
    )

    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
    }
    
    
    return QLLMQueryResults(root=MetaValue(value=output, metadata=metadata))


def checkdone(
        params: QLLMCheckdoneParams,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent,
        model_defense_level: int = 0,
        qllm_agent: QLLMAgent = None,
        qllm_agent_second: QLLMAgent = None,
        obs_raw: dict = None
    ) -> RunVLMResults:
    """
    Check if the task given in the instruction is done.
    """

    results = agent.checkdone(
        instruction=params.instruction.value,
        obs=obs_raw,
    )
    
    output = parse_checkdone(
        output_str=results
    )

    model_defense_results = apply_model_defense(
        instruction=params.instruction.value,
        thought=output.status,
        action="Checking if the task described in the instruction is done",
        dom=(obs_raw.get("accessibility_tree") or "") if obs_raw is not None else "",
        screenshot=obs_raw.get("screenshot", b"") if obs_raw is not None else b"",
        qllm_agent=qllm_agent,
        qllm_agent_second=qllm_agent_second,
        model_defense_level=model_defense_level
    )

    if model_defense_results.root.value.detected == "ATTACKED":
        logger.error("Model defense detected a prompt injection attack in the checkdone output.")
        raise ValueError("Model defense detected a prompt injection attack in the checkdone output.")
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
    }

    
    return QLLMQueryResults(root=MetaValue(value=output, metadata=metadata))


def run_vlm(
        params: RunVLMParams,
        agent : FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent,
        qllm_agent : QLLMAgent,
        env : DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        observation_type: Literal["screenshot", "screenshot_a11y_tree"],
        model_defense_level: int = 0,
        qllm_agent_second: QLLMAgent = None,
    ) -> RunVLMResults:
    """
    Perform one step in the environment using the agent.
    This function will be called in the main loop for one call to the uitars/gemini model.
    This function should be called when the planner cannot solve the instruction by itself.
    """

    pre_obs_raw = env._get_obs()
    obs = {
        "screenshot": pre_obs_raw["screenshot"],
        "accessibility_tree": pre_obs_raw["accessibility_tree"] if observation_type == "screenshot_a11y_tree" else None,
        "terminal": pre_obs_raw.get("terminal"),
    }

    if isinstance(agent, FidesOpenCUAAgent):
        response, actions, _ = agent.predict(
            params.instruction.value,
            obs
        )
    else:
        response, actions = agent.predict(
            params.instruction.value,
            obs
        )

    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
    }


    obs_raw = one_step_action(
        action=actions[0],
        env=env,
        example_result_dir=example_result_dir,
        step_idx=step_idx
    )
        
    post_obs = Observation(
            screenshot=image_bytes_to_base64_str(obs_raw['screenshot']),
            accessibility_tree=(obs_raw.get('accessibility_tree') or "") if observation_type == "screenshot_a11y_tree" else ""
        )
    pre_obs = Observation(
            screenshot=image_bytes_to_base64_str(pre_obs_raw['screenshot']),
            accessibility_tree=(pre_obs_raw.get('accessibility_tree') or "") if observation_type == "screenshot_a11y_tree" else ""
        )

    checkdone_params = QLLMCheckdoneVLMParams(
            instruction=params.instruction,
            response=response,
            actions=[str(actions[0])],
            post_obs=post_obs,
            pre_obs=pre_obs
        )
    
    checkdone_result = checkdone_run_vlm(
        params=checkdone_params,
        agent=agent,
        observation_type=observation_type
    )

    return RunVLMResults(root=MetaValue(value=checkdone_result.root.value, metadata=metadata))


def find(
        params: FindParams,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent,
        model_defense_level: int = 0,
        qllm_agent: QLLMAgent = None,
        qllm_agent_second: QLLMAgent = None,
        obs_raw: dict = None
    )-> FindResults:
    """
    Find UI element using natural language description via LLM analysis of the screenshot. This function should be primarily used over finding with the accessibility tree.
    
    Returns a FindOutput object with the x and y coordinates.
    """
    thought, response = agent.find(
        instruction = params.instruction.value,
        screenshot = image_bytes_to_base64_str(obs_raw["screenshot"]) if obs_raw is not None else "",
    )

    if not response or not response.get("x") or not response.get("y"):
        parsed_response = FindOutput(x=0, y=0, status="FAIL")
    else:
        parsed_response = FindOutput(x=response["x"], y=response["y"], status="OK")


    if parsed_response.x == 0 and parsed_response.y == 0:
        parsed_response.status = "FAIL"

    if model_defense_level >= 1:
        response_thought = qllm_agent.compare_instruction_vs_thought(
            instruction=params.instruction.value,
            thought=thought
        )
        try:
            if json.loads(response_thought).get("status", "FAIL") == "FAIL":
                parsed_response.status = "FAIL"
        except json.JSONDecodeError:
            logger.error("Failed to parse compare_instruction_vs_thought response inside of the find function: %s", response_thought)
            parsed_response.status = "FAIL"

    model_defense_results = apply_model_defense(
        instruction=params.instruction.value,
        thought=thought,
        action="Finding element in the screenshot",
        dom=(obs_raw.get("accessibility_tree") or "") if obs_raw is not None else "",
        screenshot=obs_raw.get("screenshot", b"") if obs_raw is not None else b"",
        qllm_agent=qllm_agent,
        qllm_agent_second=qllm_agent_second,
        model_defense_level=model_defense_level
    )

    if model_defense_results.root.value.detected == "ATTACKED":
        logger.error("Model defense detected a prompt injection attack in the find output.")
        raise ValueError("Model defense detected a prompt injection attack in the find output.")
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
    }

    return QLLMQueryResults(root=MetaValue(value=parsed_response, metadata=metadata))

def standard_action(
        params : StandardActionParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent
        ) -> StandardActionResults:

    obs = one_step_action(
        action=params.action,
        env=env,
        example_result_dir=example_result_dir,
        step_idx=step_idx
    )
    
    if isinstance(agent, FidesUITARSAgent):
        agent.history_images.append(obs["screenshot"])
        agent.history_responses.append("An external agent to uitars has performed the action: " + params.action)
        agent.thoughts.append("An external agent to uitars has performed the action: " + params.action)
        agent.actions.append(params.action)
        if agent.observation_type == "screenshot_a11y_tree":
            agent.observations.append({"screenshot": obs["screenshot"], "accessibility_tree": obs["accessibility_tree"]})
        else:
            agent.observations.append({"screenshot": obs["screenshot"], "accessibility_tree": None})
    elif isinstance(agent, FidesOpenCUAAgent):
        agent.cots.append({
            "observation": obs,
            "action": params.action,
            "thought": "An external agent to opencua has performed the action: " + params.action
        })
        agent.actions.append(params.action)
        if agent.observation_type == "screenshot_a11y_tree":
            agent.observations.append({"screenshot": obs["screenshot"], "accessibility_tree": obs["accessibility_tree"]})
        else:
            agent.observations.append({"screenshot": obs["screenshot"], "accessibility_tree": None})
    elif isinstance(agent, FidesAnthropicAgent):
        agent.add_tool_result(
            tool_call_id="standard_computer_action",
            result="An external agent to opencua has performed the action: " + params.action,
            screenshot=obs.get("screenshot") if obs else None
        )
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
    }

    return StandardActionResults(root=MetaValue(value="Tool call was performed successfully", metadata=metadata))


class MoveToParams(BaseModel):
    x: float
    y: float


def move_to(
        params: MoveToParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Moves the mouse cursor to a specified (x, y) position.

    Args:
        params (MoveToParams): Parameters including x, y coordinates and the environment.

    Returns:
        StandardActionResults: Result of executing the move_to action.
    """
    action_str = f"pyautogui.moveTo({params.x}, {params.y}, 1.0, 'pyautogui.easeInQuad')"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)

class ClickParams(BaseModel):
    x: float = Field(...)
    y: float = Field(...)
    button: str = Field("left")
    num_clicks: int = Field(1)

    model_config = {"extra": "forbid"}


def click(params: ClickParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Performs a mouse click at a specific position with optional button and click count.

    Args:
        params (ClickParams): Optional x, y position, mouse button, number of clicks, and environment.

    Returns:
        StandardActionResults: Result of executing the click action.
    """
    args = []
    if params.x is not None and params.y is not None:
        args.append(f"x={params.x}, y={params.y}")
    if params.button:
        args.append(f"button='{params.button}'")
    if params.num_clicks:
        args.append(f"clicks={params.num_clicks}")
    args_str = ", ".join(args)
    action_str = f"pyautogui.click({args_str})"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class MouseDownParams(BaseModel):
    button: str


def mouse_down(params: MouseDownParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Simulates pressing down a mouse button.

    Args:
        params (MouseDownParams): Optional mouse button and environment.

    Returns:
        StandardActionResults: Result of executing the mouse_down action.
    """
    args = f"button='{params.button}'" if params.button else ""
    action_str = f"pyautogui.mouseDown({args})" if args else "pyautogui.mouseDown()"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class MouseUpParams(BaseModel):
    button: str


def mouse_up(params: MouseUpParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Simulates releasing a mouse button.

    Args:
        params (MouseUpParams): Optional mouse button and environment.

    Returns:
        StandardActionResults: Result of executing the mouse_up action.
    """
    args = f"button='{params.button}'" if params.button else ""
    action_str = f"pyautogui.mouseUp({args})" if args else "pyautogui.mouseUp()"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class RightClickParams(BaseModel):
    x: float
    y: float


def right_click(params: RightClickParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Performs a right-click at an optional (x, y) position.

    Args:
        params (RightClickParams): Optional x, y coordinates and environment.

    Returns:
        StandardActionResults: Result of executing the right_click action.
    """
    args = f"x={params.x}, y={params.y}" if params.x is not None and params.y is not None else ""
    action_str = f"pyautogui.rightClick({args})" if args else "pyautogui.rightClick()"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class DragToParams(BaseModel):
    x: float
    y: float


def drag_to(params: DragToParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Drags the mouse to a specified (x, y) position.

    Args:
        params (DragToParams): Target x, y coordinates and environment.

    Returns:
        StandardActionResults: Result of executing the drag_to action.
    """
    action_str = f"pyautogui.dragTo({params.x}, {params.y}, duration=1.0, button='left', mouseDownUp=True)"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class HScrollParams(BaseModel):
    dx: int

class VScrollParams(BaseModel):
    dy: int


def hscroll(params: HScrollParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Scrolls the screen by the specified delta in x and y directions.

    Args:
        params (ScrollParams): dx and dy scroll values, and environment.

    Returns:
        StandardActionResults: Result of executing the scroll action.
    """
    action_str = f"pyautogui.hscroll({params.dx})"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


def vscroll(params: VScrollParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Scrolls the screen by the specified delta in y direction.

    Args:
        params (ScrollParams): dx and dy scroll values, and environment.

    Returns:
        StandardActionResults: Result of executing the scroll action.
    """
    action_str = f"pyautogui.vscroll({params.dy})"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class TypingParams(BaseModel):
    text: str


def type_text(params: TypingParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Types the given text string into the active input.f

    Args:
        params (TypingParams): The text to type and the environment.

    Returns:
        StandardActionResults: Result of executing the typing action.
    """
    action_str = f"pyautogui.typewrite('{params.text}')"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class PressParams(BaseModel):
    key: str

from osworld.desktop_env.actions import KEYBOARD_KEYS

def press(params: PressParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Presses a single keyboard key.

    Args:
        params (PressParams): The key to press and the environment.

    Returns:
        StandardActionResults: Result of executing the press action.
    """
    if params.key.lower() not in KEYBOARD_KEYS:
        metadata = {
            "integrity": IntegrityLabel.untrusted(),
            "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
        }
        return StandardActionResults(root=MetaValue(value="Unsupported key pressed", metadata=metadata))
    action_str = f"pyautogui.press('{params.key}')"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class KeyDownParams(BaseModel):
    key: str


def key_down(params: KeyDownParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Simulates pressing down a keyboard key.

    Args:
        params (KeyDownParams): The key to press down and the environment.

    Returns:
        StandardActionResults: Result of executing the key_down action.
    """
    if params.key.lower() not in KEYBOARD_KEYS:
        metadata = {
            "integrity": IntegrityLabel.untrusted(),
            "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
        }
        return StandardActionResults(root=MetaValue(value="Unsupported key pressed", metadata=metadata))
    
    action_str = f"pyautogui.keyDown('{params.key}')"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class KeyUpParams(BaseModel):
    key: str


def key_up(params: KeyUpParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Simulates releasing a keyboard key.

    Args:
        params (KeyUpParams): The key to release and the environment.

    Returns:
        StandardActionResults: Result of executing the key_up action.
    """
    if params.key.lower() not in KEYBOARD_KEYS:
        metadata = {
            "integrity": IntegrityLabel.untrusted(),
            "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
        }
        return StandardActionResults(root=MetaValue(value="Unsupported key pressed", metadata=metadata))

    action_str = f"pyautogui.keyUp('{params.key}')"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class HotkeyParams(BaseModel):
    keys: List[str]


def hotkey(params: HotkeyParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Presses a combination of keys simultaneously (e.g., Ctrl+C, should be passed as ['ctrl', 'c']).

    Args:
        params (HotkeyParams): List of key names and the environment.

    Returns:
        StandardActionResults: Result of executing the hotkey action.
    """
    for k in params.keys:
        if k.lower() not in KEYBOARD_KEYS:
            metadata = {
                "integrity": IntegrityLabel.untrusted(),
                "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
            }
            return StandardActionResults(root=MetaValue(value="Unsupported key pressed", metadata=metadata))

    keys_list = ','.join([f"'{k}'" for k in params.keys])
    action_str = f"pyautogui.hotkey({keys_list})"
    return standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)


class DoneParams(BaseModel):
    pass

def done(params: DoneParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Marks the current task as done.
    """
    action_str = 'DONE'
    result =  standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)
    result.root.value = "done"
    return result


class FailParams(BaseModel):
    pass

def fail(params: FailParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Marks the current task as failed.
    """
    action_str = 'FAIL'
    result =  standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)
    result.root.value = "fail"
    return result


class WaitParams(BaseModel):
    pass

def wait(params: WaitParams,
        env: DesktopEnv,
        example_result_dir: str,
        step_idx: int,
        agent: FidesUITARSAgent | FidesOpenCUAAgent | FidesAnthropicAgent) -> StandardActionResults:
    """
    Waits for a specified duration in the environment.
    """
    action_str = 'WAIT'
    result = standard_action(StandardActionParams(action=action_str), env=env, example_result_dir=example_result_dir, step_idx=step_idx, agent=agent)
    result.root.value = "wait"
    return result



class GetPageElementsOutput(BaseModel):
    elements: str

class GetPageTextOutput(BaseModel):
    text_content: str

class FindElementOutput(BaseModel):
    status: str
    tag: str
    label: str
    x: int
    y: int
    reason: str

class VerifyHypothesisOutput(BaseModel):
    status: str
    matches: bool
    explanation: str



def parse_get_page_elements(output_str: str) -> GetPageElementsOutput:
    try:
        json_str = extract_json_from_text(output_str)
        data = json.loads(json_str)
        data["elements"] = str(data["elements"])
        return GetPageElementsOutput(**data)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning(f"Failed to parse get_page_elements output: {e}")
        return GetPageElementsOutput(elements=f"Failed to parse get_page_elements output: {e}")


def parse_get_page_text(output_str: str) -> GetPageTextOutput:
    try:
        json_str = extract_json_from_text(output_str)
        data = json.loads(json_str)
        return GetPageTextOutput(**data)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning(f"Failed to parse get_page_text output: {e}")
        return GetPageTextOutput(text_content="")


def parse_find_element(output_str: str) -> FindElementOutput:
    try:
        json_str = extract_json_from_text(output_str)
        data = json.loads(json_str)
        return FindElementOutput(**data)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning(f"Failed to parse find_element output: {e}")
        return FindElementOutput(
            status="FAIL",
            tag="",
            label="",
            x=0,
            y=0,
            reason="Parsing error"
        )


def parse_verify_hypothesis(output_str: str) -> VerifyHypothesisOutput:
    try:
        json_str = extract_json_from_text(output_str)
        data = json.loads(json_str)
        return VerifyHypothesisOutput(**data)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning(f"Failed to parse verify_hypothesis output: {e}")
        return VerifyHypothesisOutput(
            status="UNKNOWN",
            matches=False,
            explanation="Parsing error"
        )



class GetPageElementsParams(BaseModel):
    element_types: List[str] | None | str = Field(
        default=None,
        description="Optional filter: a list of exact accessibility-tree role tags, e.g. "
                    "['push-button', 'link']. Valid tags: push-button, toggle-button, "
                    "radio-button, check-box, combo-box, link, entry, menu, menu-item, "
                    "list-item, tree-item, text, static. Generic words like 'buttons' are NOT "
                    "valid tags and will match nothing. Leave unset to search all of these."
    )


class GetPageTextParams(BaseModel):
    include_navigation: bool 
    max_length: int


class FindElementByTextParams(BaseModel):
    description: str
    element_types: List[str] | None | str = Field(
        default=None,
        description="Optional filter: a list of exact accessibility-tree role tags, e.g. "
                    "['push-button', 'link']. Valid tags: push-button, toggle-button, "
                    "radio-button, check-box, combo-box, link, entry, menu, menu-item, "
                    "list-item, tree-item, text, static. Generic words like 'buttons' are NOT "
                    "valid tags and will match nothing. Leave unset to search all of these."
    )


class VerifyHypothesisParams(BaseModel):
    observation: str
    hypothesis: str



class GetPageElementsResults(BaseModel):
    root: MetaValue[GetPageElementsOutput]


class GetPageTextResults(BaseModel):
    root: MetaValue[GetPageTextOutput]


class FindElementByTextResults(BaseModel):
    root: MetaValue[FindElementOutput]


class VerifyHypothesisResults(BaseModel):
    root: MetaValue[VerifyHypothesisOutput]



def get_page_elements(
        params: GetPageElementsParams,
        model_defense_level: int = 0,
        qllm_agent: QLLMAgent = None,
        qllm_agent_second: QLLMAgent = None,
        obs_raw: dict = None
    ) -> GetPageElementsResults:
    """
    Get structured list of all interactive elements on current page.
    
    Args:
        params: Parameters including accessibility_tree and optional element_types filter.
        qllm_agent: The quarantined LLM agent to use for extraction.
    
    Returns:
        GetPageElementsResults with list of elements.
    """
    response = qllm_agent.get_page_elements(
        accessibility_tree=(obs_raw.get("accessibility_tree") or "") if obs_raw is not None else "",
        element_types=params.element_types
    )
    
    parsed_response = parse_get_page_elements(response)

    model_defense_results = apply_model_defense(
        instruction=("Retrieve the element of the following type from the accessibility tree" + params.element_types) if isinstance(params.element_types, str) else "Get page elements from the accessibility tree",
        thought=parsed_response.elements,
        action="I got page elements from the accessibility tree",
        dom=(obs_raw.get("accessibility_tree") or "") if obs_raw is not None else "",
        screenshot=obs_raw.get("screenshot", b"") if obs_raw is not None else b"",
        qllm_agent=qllm_agent,
        qllm_agent_second=qllm_agent_second,
        model_defense_level=model_defense_level
    )

    if model_defense_results.root.value.detected == "ATTACKED":
        logger.error("Model defense detected a prompt injection attack in the get_page_elements output.")
        raise ValueError("Model defense detected a prompt injection attack in the get_page_elements output.")
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(
            PowersetLattice(subset=frozenset(), universe=frozenset())
        )
    }
    
    return GetPageElementsResults(
        root=MetaValue(value=parsed_response, metadata=metadata)
    )


def get_page_text(
        params: GetPageTextParams,
        model_defense_level: int = 0,
        qllm_agent: QLLMAgent = None,
        qllm_agent_second: QLLMAgent = None,
        obs_raw: dict = None
    ) -> GetPageTextResults:
    """
    Extract visible text content from current page.
    
    Args:
        params: Parameters including accessibility_tree, include_navigation flag, and max_length.
        qllm_agent: The quarantined LLM agent to use for extraction.
    
    Returns:
        GetPageTextResults with extracted text content.
    """
    if params.max_length == None:
        params.max_length = 10000
    if params.include_navigation == None:
        params.include_navigation = True


    response = qllm_agent.get_page_text(
        accessibility_tree=(obs_raw.get("accessibility_tree") or "") if obs_raw is not None else "",
        include_navigation=params.include_navigation
    )
    
    parsed_response = parse_get_page_text(response)
    
    model_defense_results = apply_model_defense(
        instruction="Get any text from the environment given the accessibility tree",
        thought=parsed_response.text_content,
        action="I got page text from the accessibility tree",
        dom=(obs_raw.get("accessibility_tree") or "") if obs_raw is not None else "",
        screenshot=obs_raw.get("screenshot", b"") if obs_raw is not None else b"",
        qllm_agent=qllm_agent,
        qllm_agent_second=qllm_agent_second,
        model_defense_level=model_defense_level
    )

    if model_defense_results.root.value.detected == "ATTACKED":
        logger.error("Model defense detected a prompt injection attack in the get_page_text output.")
        raise ValueError("Model defense detected a prompt injection attack in the get_page_text output.")
    
    if len(parsed_response.text_content) > params.max_length:
        parsed_response.text_content = (
            parsed_response.text_content[:params.max_length] + "\n... (truncated)"
        )
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(
            PowersetLattice(subset=frozenset(), universe=frozenset())
        )
    }
    
    return GetPageTextResults(
        root=MetaValue(value=parsed_response, metadata=metadata)
    )


def find_element_by_text(
        params: FindElementByTextParams,
        model_defense_level: int = 0,
        qllm_agent: QLLMAgent = None,
        qllm_agent_second: QLLMAgent = None,
        obs_raw: dict = None
    ) -> FindElementByTextResults:
    """
    Find UI element using natural language description via LLM analysis of accessibility tree.
    
    Args:
        params: Parameters including description, accessibility_tree, and optional element_types.
        qllm_agent: The quarantined LLM agent to use for finding.
    
    Returns:
        FindElementByTextResults with element location and status.
    """
    response = qllm_agent.find_element_by_text(
        description=params.description,
        accessibility_tree=(obs_raw.get("accessibility_tree") or "") if obs_raw is not None else "",
        element_types=params.element_types
    )
    
    parsed_response = parse_find_element(response)
    
    if parsed_response.status == "OK":
        parsed_response.x += 5
        parsed_response.y += 5

    model_defense_results = apply_model_defense(
        instruction=f"Find the element with the description: {params.description}",
        thought= f"{parsed_response.status}: " + parsed_response.reason,
        action=f"Found element at coordinates ({parsed_response.x}, {parsed_response.y}). If coodinates are (5,5), element was not found.",
        dom=(obs_raw.get("accessibility_tree") or "") if obs_raw is not None else "",
        screenshot=obs_raw.get("screenshot", b"") if obs_raw is not None else b"",
        qllm_agent=qllm_agent,
        qllm_agent_second=qllm_agent_second,
        model_defense_level=model_defense_level
    )

    if model_defense_results.root.value.detected == "ATTACKED":
        logger.error("Model defense detected a prompt injection attack in the find element output.")
        raise ValueError("Model defense detected a prompt injection attack in the find element output.")
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(
            PowersetLattice(subset=frozenset(), universe=frozenset())
        )
    }
    
    return FindElementByTextResults(
        root=MetaValue(value=parsed_response, metadata=metadata)
    )


def verify_hypothesis(
        params: VerifyHypothesisParams,
        model_defense_level: int = 0,
        qllm_agent: QLLMAgent = None,
        qllm_agent_second: QLLMAgent = None,
        obs_raw: dict = None
) -> VerifyHypothesisResults:
    """
    Verify if a hypothesis is verified in a given string representing some function output that is revealing information about the current state of the environment.

    Args:
        params: Parameters including observation and hypothesis strings.
        qllm_agent: The quarantined LLM agent to use for verification.
    
    Returns:
        VerifyHypothesisResults with match status and explanation.
    """
    response = qllm_agent.verify_hypothesis(
        observation=params.observation,
        hypothesis=params.hypothesis
    )
    
    parsed_response = parse_verify_hypothesis(response)

    model_defense_results = apply_model_defense(
        instruction=f"Verify the following hypothesis: {params.hypothesis} against the observation: {params.observation}",
        thought=f"{parsed_response.status}: " + parsed_response.explanation,
        action="I have verified the hypothesis against the observation",
        dom=(obs_raw.get("accessibility_tree") or "") if obs_raw is not None else "",
        screenshot=obs_raw.get("screenshot", b"") if obs_raw is not None else b"",
        qllm_agent=qllm_agent,
        qllm_agent_second=qllm_agent_second,
        model_defense_level=model_defense_level
    )

    if model_defense_results.root.value.detected == "ATTACKED":
        logger.error("Model defense detected a prompt injection attack in the verify hypothesis output.")
        raise ValueError("Model defense detected a prompt injection attack in the verify hypothesis output.")
    
    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(
            PowersetLattice(subset=frozenset(), universe=frozenset())
        )
    }
    
    return VerifyHypothesisResults(
        root=MetaValue(value=parsed_response, metadata=metadata)
    )



class RegexExtractParams(BaseModel):
    regex: StrictStr
    summary: str

class RegexExtractResults(BaseModel):
    root: MetaValue[List[str]]

def regex_extract(
       params: RegexExtractParams,
   ) -> RegexExtractResults:
    """
    Apply a regex to the summary and return the matches.
    """
    pattern = re.compile(params.regex)
    matches = pattern.findall(params.summary)

    metadata = {
        "integrity": IntegrityLabel.trusted(),
        "confidentiality": InverseLattice(PowersetLattice(subset=frozenset(), universe=frozenset()))
    }

    return RegexExtractResults(root=MetaValue(value=matches, metadata=metadata))


class ReadVariableParams(BaseModel):
    variable_name: str = Field(..., description="The name of the variable to read.")

class ReadVariableResult(BaseModel):
    value: str = Field(..., description="The value of the variable read.")


def read_variable(params: ReadVariableParams) -> ReadVariableResult:
    """
    Reads the value of a variable.
    """
    
    return ReadVariableResult(value="")