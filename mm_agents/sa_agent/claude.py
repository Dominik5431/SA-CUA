"""
Custom class inheriting from AnthropicAgent with additional methods for finding 
elements and summarizing screenshot content.

This version uses the computer use tool for precise coordinate detection with click actions.
"""

import base64
import json
import logging
import math
from typing import Literal, Dict, Any, Optional
from PIL import Image
from io import BytesIO

from anthropic import Anthropic
from anthropic.types.beta import BetaMessageParam

from osworld.mm_agents.anthropic import AnthropicAgent
from mm_agents.sa_agent.opencua import encode_image
from mm_agents.sa_agent.prompts import SYST_PROMPT_CHECKDONE, SYST_PROMPT_CHECKDONE_VLM, SYST_PROMPT_FIND_CLAUDE, SYST_PROMPT_SCREENSHOT_SUMMARIZER, SYST_PROMPT_QVLM_QUERY

logger = logging.getLogger("desktopenv.agent")


class FidesAnthropicAgent(AnthropicAgent):
    """
    Custom Anthropic agent for Fides, extending AnthropicAgent with additional methods
    for finding UI elements and summarizing screenshot content.
    
    Uses computer use tool with click actions for maximum precision in coordinate detection.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.find_client = Anthropic(api_key=self.api_key, max_retries=4)
        
        self.standard_width = 1280
        self.standard_height = 720

        self.observation_type = kwargs.get("observation_type", "screenshot")
    
    def find(
        self,
        instruction: str,
        screenshot: str,
        accessibility_tree: Optional[str] = None
    ) -> tuple[str, Dict[str, float]]:
        """
        Find a UI element in a screenshot based on a description.

        This method uses Claude with the computer use tool to locate UI elements
        by their description. The tool provides precise coordinate detection with
        awareness of screen dimensions. Uses LEFT_CLICK action for maximum precision.

        Args:
            instruction (str): Description of the element to find
            screenshot (bytes): PNG-encoded screenshot image
            accessibility_tree: accepted for interface parity with FidesOpenCUAAgent.find()
                (which uses it for absolute-pixel grounding); unused here since Claude's
                computer-use tool already returns precise pixel coordinates from the image.

        Returns:
            tuple: (thought: str, coordinates: Dict[str, float])
                   thought: Claude's reasoning about finding the element
                   coordinates: Dict with 'x' and 'y' pixel coordinates in original resolution
        """
        screenshot_bytes = base64.b64decode(screenshot)
        try:
            image = Image.open(BytesIO(screenshot_bytes))
            original_width = image.width
            original_height = image.height
            
            resized_image = image.resize(
                (self.standard_width, self.standard_height), 
                Image.Resampling.LANCZOS
            )
            
            output_buffer = BytesIO()
            if resized_image.mode != "RGB":
                resized_image = resized_image.convert("RGB")
            resized_image.save(output_buffer, format='PNG')
            screenshot_bytes = output_buffer.getvalue()
            screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            
        except Exception as e:
            logger.error(f"Error processing screenshot: {e}")
            return f"Error processing screenshot: {e}", {
                "x": self.screen_size[0] / 2,
                "y": self.screen_size[1] / 2
            }
        
        messages: list[BetaMessageParam] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_base64
                        }
                    },
                    {
                        "type": "text",
                        "text": f"""Find this UI element: {instruction}

The screenshot is already provided above. DO NOT take a new screenshot.

If you find the element, use the computer tool's LEFT_CLICK action to indicate its center position.
If you cannot find the element (it's hidden, not visible, or would require other actions first), use LEFT_CLICK at position [0, 0].

Be precise - the coordinates you provide will be used for automation.
Use LEFT_CLICK (not mouse_move) for maximum precision from the computer use tool."""
                    }
                ]
            }
        ]
        
        tools = [{
            'name': 'computer',
            'type': 'computer_20250124',
            'display_width_px': self.standard_width,
            'display_height_px': self.standard_height,
            'display_number': 1
        }]
        
        if self.model_name in ["claude-3-7-sonnet-20250219", "claude-4-opus-20250514", 
                               "claude-4-sonnet-20250514", "claude-4-5-sonnet-20250929", "claude-sonnet-4-5-20250929"]:
            betas = ["computer-use-2025-01-24"]
        else:
            betas = ["computer-use-2024-10-22"]
            tools[0]['type'] = 'computer_20241022'
        
        try_times = 3
        temperature = 0.5
        
        while try_times > 0:
            try:
                response = self.find_client.beta.messages.create(
                    model=self.model_name,
                    max_tokens=2048,
                    temperature=temperature,
                    system=SYST_PROMPT_FIND_CLAUDE,
                    messages=messages,
                    tools=tools,
                    betas=betas
                )
                
                thought = ""
                coordinates_1280x720 = None
                action_used = None
                
                for block in response.content:
                    if block.type == "text":
                        thought += block.text
                    elif block.type == "tool_use" and block.name == "computer":
                        tool_input = block.input
                        action = tool_input.get("action", "")
                        action_used = action
                        
                        if "coordinate" in tool_input:
                            coordinates_1280x720 = tool_input["coordinate"]
                            logger.info(f"Tool returned action '{action}' with coordinates (1280x720 space): {coordinates_1280x720}")
                        
                        if not thought and action:
                            thought = f"Using {action} action to indicate position"
                
                if coordinates_1280x720:
                    scale_x = original_width / self.standard_width
                    scale_y = original_height / self.standard_height
                    
                    actual_x = round(coordinates_1280x720[0] * scale_x, 3)
                    actual_y = round(coordinates_1280x720[1] * scale_y, 3)
                    
                    logger.info(f"Scaled coordinates to original resolution ({original_width}x{original_height}): ({actual_x}, {actual_y})")
                    
                    if coordinates_1280x720[0] == 0 and coordinates_1280x720[1] == 0:
                        thought = thought or "Element not found in the current view"
                        logger.warning(f"Element not found: {instruction}")
                    else:
                        logger.info(f"Element found using {action_used}: {instruction} at ({actual_x}, {actual_y})")
                    
                    return thought or "Element located", {
                        "x": actual_x,
                        "y": actual_y
                    }
                else:
                    logger.warning("No coordinates returned from tool use - Claude may not have used the tool")
                    logger.warning(f"Response content blocks: {[b.type for b in response.content]}")
                    return thought or "No coordinates provided - tool not used", {
                        "x": original_width / 2,
                        "y": original_height / 2
                    }
                
            except Exception as e:
                logger.error(f"Error calling Claude for find: {e}")
                try_times -= 1
                if try_times > 0:
                    temperature = 0.7
                    logger.info(f"Retrying with temperature {temperature} ({try_times} attempts left)")
                else:
                    logger.error(f"All retry attempts exhausted for finding: {instruction}")
                    return f"Error: Could not find element - {str(e)}", {
                        "x": original_width / 2,
                        "y": original_height / 2
                    }
    
    def summarize_screenshot_content(
        self,
        instruction: str,
        screenshot: str,
        output_type: Literal["int", "str", "bool", "float", "list", "dict", "tuple", "set"] = "str"
    ) -> str:
        """
        Summarize or extract information from a screenshot using Claude.
        
        This method does NOT use the computer use tool - it's pure vision analysis.
        
        Args:
            instruction (str): The instruction for what to extract/summarize
            screenshot (bytes): PNG-encoded screenshot image
            output_type (str): Expected output type (int, str, bool, float, list, dict, tuple, set)
        
        Returns:
            str: The extracted information or summary from the screenshot
        """        
        messages: list[BetaMessageParam] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"""INSTRUCTION: {instruction}

OUTPUT TYPE: {output_type}

Please analyze the screenshot and return your answer in the following JSON format:
{{
    "output": <your_answer_here>
}}

Where <your_answer_here> should be of type {output_type}.
"""
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot
                        }
                    }
                ]
            }
        ]
        
        try_times = 3
        temperature = 0.7
        
        while try_times > 0:
            try:
                response = self.find_client.messages.create(
                    model=self.model_name,
                    max_tokens=4096,
                    temperature=temperature,
                    system=SYST_PROMPT_SCREENSHOT_SUMMARIZER,
                    messages=messages
                )
                
                response_text = ""
                for block in response.content:
                    if block.type == "text":
                        response_text += block.text
                
                logger.info(f"Summarize response: {response_text}")
                
                return response_text.strip()
                
            except Exception as e:
                logger.error(f"Error calling Claude for summarize: {e}")
                try_times -= 1
                if try_times > 0:
                    temperature = 1.0
                else:
                    return f"Error: Could not summarize screenshot - {str(e)}"

    def query_qvlm(
        self,
        instruction: str,
        screenshot: str,
    ) -> str:
        """
        Ask an open-ended question about a screenshot using Claude.

        This method does NOT use the computer use tool - it's pure vision analysis.

        Args:
            instruction (str): The question to answer about the screenshot
            screenshot (bytes): PNG-encoded screenshot image

        Returns:
            str: Claude's direct answer to the question
        """
        messages: list[BetaMessageParam] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": instruction
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot
                        }
                    }
                ]
            }
        ]

        try_times = 3
        temperature = 0.7

        while try_times > 0:
            try:
                response = self.find_client.messages.create(
                    model=self.model_name,
                    max_tokens=4096,
                    temperature=temperature,
                    system=SYST_PROMPT_QVLM_QUERY,
                    messages=messages
                )

                response_text = ""
                for block in response.content:
                    if block.type == "text":
                        response_text += block.text

                logger.info(f"query_qvlm response: {response_text}")

                return response_text.strip()

            except Exception as e:
                logger.error(f"Error calling Claude for query_qvlm: {e}")
                try_times -= 1
                if try_times > 0:
                    temperature = 1.0
                else:
                    return f"Error: Could not query screenshot - {str(e)}"

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
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _screenshot
                    }
                }
            ]
        })
        messages.append({
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "## RESPONSE\n" + (response.strip() if len(response) > 0 else "No valid response")
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
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64_image
                        }
                    }
            ]
        })
        try:
            response = self.find_client.messages.create(
                model=self.model_name,
                max_tokens=4096,
                temperature=0.5,
                system=SYST_PROMPT_CHECKDONE_VLM,
                messages=messages
            )
            
            checkdone = ""
            for block in response.content:
                if block.type == "text":
                    checkdone += block.text

        except Exception as e:
            logger.error("Failed to call" + self.model + ", Error: " + str(e))
            checkdone = ""

        logger.info("RESPONSE: %s", checkdone)
    

        return checkdone.strip()
    

    def checkdone(
            self,
            instruction: str,
            obs: dict,
            recent_actions: list[str] | None = None,
        ) -> str:
        """
        Check if the action performed by running the run_vlm function is successful.

        recent_actions (optional): the planner's own recent tool calls, first-party/trustworthy,
        so this judgment isn't purely screenshot-based -- see SYST_PROMPT_CHECKDONE for how it's
        meant to be weighed (many actions leave no persistent visual confirmation).
        """

        messages = []
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": instruction
                },
            ]
        })

        if recent_actions:
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "## RECENT ACTIONS (most recent last)\n" + "\n".join(recent_actions),
                    },
                ]
            })

        _screenshot = encode_image(obs["screenshot"])

        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "## SCREENSHOT\nCurrent state of the environment, after the actions above:"
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _screenshot
                    }
                }
            ]
        })
        try:
            response = self.find_client.messages.create(
                model=self.model_name,
                max_tokens=4096,
                temperature=0.5,
                system=SYST_PROMPT_CHECKDONE,
                messages=messages
            )
            
            checkdone = ""
            for block in response.content:
                if block.type == "text":
                    checkdone += block.text

        except Exception as e:
            logger.error("Failed to call" + self.model_name + ", Error: " + str(e))
            checkdone = ""

        logger.info("RESPONSE: %s", checkdone)
    

        return checkdone.strip()