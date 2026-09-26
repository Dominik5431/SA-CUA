
import ast
import base64
import io
import math
import re
from typing import Any, Dict, List, Literal, Optional
from PIL import Image
from io import BytesIO
from mm_agents.prompt_agent.opencua_agent import AGNET_SYS_PROMPT_L2, AGNET_SYS_PROMPT_L3, OpenCUAAgent, parse_response_to_cot_and_action, project_coordinate_to_absolute_scale
from mm_agents.prompt_agent.agent import linearize_accessibility_tree, trim_accessibility_tree
import numpy as np
from .prompts import SYST_PROMPT_CHECKDONE, SYST_PROMPT_CHECKDONE_VLM, SYST_PROMPT_FIND_OPENCUA, \
    SYST_PROMPT_SCREENSHOT_SUMMARIZER, SYST_PROMPT_FIND
import logging

logger = logging.getLogger("desktopenv.agent")

def encode_image(image: str) -> str:
    """Encode image to base64 string for model input."""
    image = Image.open(BytesIO(image))
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffered = BytesIO()
    image.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode()

def _to_image_bytes(screenshot: Any, fmt: str = "PNG") -> bytes:
    """Normalize screenshot input into raw bytes suitable for encode_image."""
    if isinstance(screenshot, Image.Image):
        buf = io.BytesIO()
        screenshot.save(buf, format=fmt)
        return buf.getvalue()
    if isinstance(screenshot, (bytes, bytearray)):
        return bytes(screenshot)
    if isinstance(screenshot, str):
        if screenshot.startswith("data:"):
            parts = screenshot.split(",", 1)
            if len(parts) == 2:
                return base64.b64decode(parts[1])
            raise ValueError("Invalid data URI for image")
        return base64.b64decode(screenshot)
    raise TypeError("screenshot must be PIL.Image, bytes, or base64 str")

def parse_click_actions(actions: List[str]) -> Optional[Dict[str, int]]:
    """
    Parse a list of strings like ['pyautogui.click(2544000, 216000)']
    and extract {'x': 2544000, 'y': 216000} from the first valid entry.

    Returns None if parsing fails.
    """
    click_pattern = re.compile(
        r"pyautogui\.click\s*\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)"
    )


    for act in actions:
        match = click_pattern.search(act)
        if match:
            x, y = match.groups()
            x_val = int(float(x)) if x.isdigit() else float(x)
            y_val = int(float(y)) if y.isdigit() else float(y)
            return {"x": x_val, "y": y_val}

    return None


def parse_computer_click_json(raw_response: str) -> Optional[Dict[str, float]]:
    """
    Fallback for models (e.g. generic Qwen chat checkpoints, not OpenCUA-finetuned)
    that ignore the requested PyAutoGUI-code-fence format and instead emit the click
    as a JSON tool-call blob echoing the action-space schema from the system prompt,
    e.g.:
      {"name": "computer.click", "parameters": {"properties": {"x": 267, "y": 80}}}
      {"name": "computer.triple_click", "parameters": {"type": "object", "properties": {"x": 985, "y": 80}}}
    or the flatter {"name": "computer.click", "parameters": {"x": 267, "y": 80}}.

    Matches any "computer.*click*" action name (click/triple_click/double_click/...)
    since observed models vary which one they echo back, not just "computer.click".
    Scans for x/y numbers following that marker rather than doing a full JSON parse,
    since the model's nesting of x/y under "parameters" has been observed to vary
    (flat vs. nested under "properties").

    Returns None if no computer.*click* action with both coordinates is found.
    """
    marker = re.search(r'"name"\s*:\s*"computer\.\w*[Cc]lick\w*"', raw_response)
    idx = marker.start() if marker else raw_response.find("computer.click")
    if idx == -1:
        return None
    segment = raw_response[idx:]
    x_match = re.search(r'"x"\s*:\s*(-?\d+(?:\.\d+)?)', segment)
    y_match = re.search(r'"y"\s*:\s*(-?\d+(?:\.\d+)?)', segment)
    if not x_match or not y_match:
        return None
    x, y = x_match.group(1), y_match.group(1)
    x_val = int(float(x)) if x.lstrip('-').isdigit() else float(x)
    y_val = int(float(y)) if y.lstrip('-').isdigit() else float(y)
    return {"x": x_val, "y": y_val}


FIND_THOUGHT_FALLBACK_PATTERN = re.compile(
    r'(?:^|\n)\s*#{0,2}\s*Thought\s*:?\s*\n?(.*?)(?=\n\s*#{0,2}\s*Action\s*:?|\Z)',
    re.DOTALL | re.IGNORECASE
)


class FidesOpenCUAAgent(OpenCUAAgent):
    """
    Subclass of OpenCUAAgent exposing `find` and `summarize_screenshot_content`.
    Inherits OpenCUAAgent internals (call_llm, history, screen/coordinate state).
    """

    def __init__(self, *args, system_prompt_find: Optional[str] = None, system_prompt_summarize: Optional[str] = None, **kwargs):
        """
        Pass-through constructor — all OpenCUAAgent init args are accepted and forwarded.
        Optionally override the system prompts used by find/summarize.
        """
        super().__init__(*args, **kwargs)

        self._sys_find = system_prompt_find or SYST_PROMPT_FIND_OPENCUA
        self._sys_summarize = system_prompt_summarize or SYST_PROMPT_SCREENSHOT_SUMMARIZER

    def _build_messages_with_image(self, system_prompt: str, instruction: str, screenshot_bytes: bytes, accessibility_tree: Optional[str] = None):
        """
        Build the `messages` list used by OpenCUAAgent's call_llm payload.
        Follows the same format used inside opencua_agent (system + image message + user instruction).

        When running in "screenshot_a11y_tree" mode, also includes the linearized
        accessibility tree alongside the instruction -- mirroring OpenCUAAgent.predict()'s
        handling of `current_obs_content` -- so find() has the same real, absolute pixel
        positions available that the main policy loop reads element coordinates from
        (see the coordinate_type="absolute" rationale in run.py). Without this, find() only
        had the (resized) screenshot to visually estimate coordinates from, which uses a
        different coordinate space than "absolute" assumes.
        """
        data_uri = "data:image/png;base64," + encode_image(screenshot_bytes)
        messages = []
        messages.append({
            "role": "system",
            "content": system_prompt
        })
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": data_uri}
                }
            ]
        })
        instruction_content = [
            {
                "type": "text",
                "text": f"# Task Instruction:\n{instruction}"
            }
        ]
        if getattr(self, "observation_type", None) == "screenshot_a11y_tree" and accessibility_tree:
            linearized_accessibility_tree = linearize_accessibility_tree(
                accessibility_tree=accessibility_tree,
                platform=self.platform
            )
            if linearized_accessibility_tree:
                linearized_accessibility_tree = trim_accessibility_tree(
                    linearized_accessibility_tree,
                    self.a11y_tree_max_tokens
                )
                instruction_content.append({
                    "type": "text",
                    "text": f"Accessibility tree of the current screen:\n{linearized_accessibility_tree}"
                })
        messages.append({
            "role": "user",
            "content": instruction_content
        })
        return messages


    def find(
            self,
            instruction: str,
            screenshot: Any,
            accessibility_tree: Optional[str] = None
        ) -> Dict[str, Any]:
        """
        Locate UI elements or propose actions based on instruction + screenshot.

        Returns a dict with a likely shape like:
          {"status": "ok", "actions": [ { "action": "click", "x":..., "y":..., "confidence":... }, ... ]}

        On parse failure returns:
          {"status": "parse_failed", "raw": <llm_text>}
        """

        img_bytes = _to_image_bytes(screenshot)
        messages = self._build_messages_with_image(self._sys_find, instruction, img_bytes, accessibility_tree)

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": getattr(self, "max_tokens", 1500),
            "top_p": getattr(self, "top_p", 1.0),
            "temperature": float(self.temperature)
        }
        raw_response = self.call_llm(payload, self.model)
        logger.info("FIND RAW RESPONSE: %s", raw_response)
        print(f"FIND RAW RESPONSE: {raw_response!r}")

        try:
            low_level_instruction, pyautogui_actions, other_cot = parse_response_to_cot_and_action(raw_response, getattr(self, "screen_size", None), getattr(self, "coordinate_type", None))
            out = {
                "status": "ok",
                "low_level_instruction": low_level_instruction,
                "actions": pyautogui_actions,
                "cot": other_cot
            }
        except Exception as e:
            logger.debug("parse_response_to_cot_and_action failed: %s", e)
            print(f"FIND parse_response_to_cot_and_action failed: {e!r}")
            return {"status": "parse_failed", "raw": raw_response}

        logger.info("FIND PARSED pyautogui_actions: %s", pyautogui_actions)
        print(f"FIND PARSED pyautogui_actions: {pyautogui_actions!r}")

        try:
            action = parse_click_actions(pyautogui_actions)
        except Exception as e:
            logger.debug("Custom parsing failed: %s", e)
            print(f"FIND Custom parsing failed: {e!r}")
            action = None

        if action is None:
            action = parse_computer_click_json(raw_response)
            if action is not None:
                screen_size = getattr(self, "screen_size", None)
                coordinate_type = getattr(self, "coordinate_type", None)
                if screen_size and coordinate_type:
                    scaled_code = project_coordinate_to_absolute_scale(
                        f"pyautogui.click({action['x']}, {action['y']})",
                        screen_width=screen_size[0],
                        screen_height=screen_size[1],
                        coordinate_type=coordinate_type,
                    )
                    action = parse_click_actions([scaled_code])
                logger.info("FIND fallback JSON parsing recovered action: %s", action)
                print(f"FIND fallback JSON parsing recovered action: {action!r}")

        logger.info("FIND PARSED action: %s", action)
        print(f"FIND PARSED action: {action!r}")

        thought = other_cot.get("thought") if other_cot else None
        if thought is None:
            thought_match = FIND_THOUGHT_FALLBACK_PATTERN.search(raw_response)
            thought = thought_match.group(1).strip() if thought_match and thought_match.group(1).strip() else \
                "(model produced no parseable Thought section)"
            logger.info("FIND fallback thought extraction recovered: %s", thought[:200])

        return thought, action


    def summarize_screenshot_content(
            self,
            instruction: str,
            screenshot: str,
            output_type: Literal["int", "str", "bool", "float", "list", "dict", "tuple", "set"] = "str"
    ) -> str:
        """
        Summarize screenshot according to `instruction`. Returns the raw assistant text.
        """
        messages = []
        messages.append({
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": SYST_PROMPT_SCREENSHOT_SUMMARIZER
                },
            ]
        })
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": instruction
                },
            ]
        })
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"You need to return your answer with this output format: {output_type}. "
                },
            ]
        })
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{screenshot}"}
                }
            ]
        })
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": getattr(self, "max_tokens", 800),
            "top_p": getattr(self, "top_p", 1.0),
            "temperature": float(self.temperature)
        }
        response = self.call_llm(payload, self.model)
        if isinstance(response, str):
            return response.strip()
        return str(response)
    

    def checkdone_run_vlm(
            self,
            instruction: str,
            actions: list[str],
            response: str,
            pre_obs: dict,
            post_obs: dict,
        ) -> str:
        """
        Check if the action performed by running the run_vlm function is successful.
        """

        messages = []

        messages.append({
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": SYST_PROMPT_CHECKDONE_VLM
                },
            ]
        })
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": instruction
                },
            ]
        })

        _screenshot = encode_image(pre_obs["screenshot"])

        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "## PRE-OBS\nYou are given the screenshot of the environment before action below:"
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{_screenshot}"}
                }
            ]
        })

        messages.append({
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "## RESPONSE\n" + (response.strip() if len(response) > 0 else "No valid action")
                },
            ]
        })

        messages.append({
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "## ACTION" + (actions[0].strip() if len(actions) > 0 else "No valid action")
                },
            ]
        })

        base64_image = encode_image(post_obs["screenshot"])
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "## POST-OBS\nYou are given the screenshot of the environment after action below:"
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{base64_image}"}
                }
            ]
        })

        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": getattr(self, "max_tokens", 800),
                "top_p": getattr(self, "top_p", 1.0),
                "temperature": float(self.temperature)
            }
            checkdone = self.call_llm(payload, self.model)
        except Exception as e:
            logger.error("Failed to call" + self.model + ", Error: " + str(e))
            checkdone = ""

        logger.info("RESPONSE: %s", checkdone)

        return checkdone


    def checkdone(
            self,
            instruction: str,
            obs: dict,
        ) -> str:
        """
        Check if the action performed by running the run_vlm function is successful.
        """

        messages = []

        messages.append({
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": SYST_PROMPT_CHECKDONE
                },
            ]
        })
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": instruction
                },
            ]
        })

        _screenshot = encode_image(obs["screenshot"])

        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "## PRE-OBS\nYou are given the screenshot of the environment before action below:"
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{_screenshot}"}
                }
            ]
        })

        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": getattr(self, "max_tokens", 800),
                "top_p": getattr(self, "top_p", 1.0),
                "temperature": float(self.temperature)
            }
            checkdone = self.call_llm(payload, self.model)
        except Exception as e:
            logger.error("Failed to call" + self.model + ", Error: " + str(e))
            checkdone = ""

        logger.info("RESPONSE: %s", checkdone)

        return checkdone