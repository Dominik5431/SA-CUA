
import ast
import base64
import math
import re
from typing import Literal, Optional
from PIL import Image
from io import BytesIO
from osworld.mm_agents.old_uitars_agent import UITARSAgent, parse_action_to_structure_output, parsing_response_to_pyautogui_code, pil_to_base64
import numpy as np
from mm_agents.sa_agent.opencua import encode_image
from mm_agents.sa_agent.prompts import SYST_PROMPT_CHECKDONE, SYST_PROMPT_CHECKDONE_VLM, SYST_PROMPT_FIND_UITARS, SYST_PROMPT_SCREENSHOT_SUMMARIZER, SYST_PROMPT_QVLM_QUERY
import logging

logger = logging.getLogger("desktopenv.agent")


def parsing_response_to_coordinates(responses, image_height: int, image_width:int, input_swap:bool=True) -> str:

    if isinstance(responses, dict):
        responses = [responses]
    for response_id, response in enumerate(responses):
        x = None
        y = None

        action_dict = response
        action_type = action_dict.get("action_type")
        action_inputs = action_dict.get("action_inputs", {})
        
        if action_type in ["drag", "select"]:
            start_box = action_inputs.get("start_box")
            end_box = action_inputs.get("end_box")
            if start_box and end_box:
                x1, y1, x2, y2 = ast.literal_eval(start_box)
                sx = round(float((x1 + x2) / 2) * image_width, 3)
                sy = round(float((y1 + y2) / 2) * image_height, 3)
                x1, y1, x2, y2 = ast.literal_eval(end_box)
                ex = round(float((x1 + x2) / 2) * image_width, 3)
                ey = round(float((y1 + y2) / 2) * image_height, 3)

        elif action_type == "scroll":
            start_box = action_inputs.get("start_box")
            if start_box:
                x1, y1, x2, y2 = ast.literal_eval(start_box)
                x = round(float((x1 + x2) / 2) * image_width, 3)
                y = round(float((y1 + y2) / 2) * image_height, 3)

        elif action_type in ["click", "left_single", "left_double", "right_single", "hover"]:
            start_box = action_inputs.get("start_box")
            start_box = str(start_box)
            if start_box:
                start_box = ast.literal_eval(start_box)
                if len(start_box) == 4:
                    x1, y1, x2, y2 = start_box
                elif len(start_box) == 2:
                    x1, y1 = start_box
                    x2 = x1
                    y2 = y1
                x = round(float((x1 + x2) / 2) * image_width, 3)
                y = round(float((y1 + y2) / 2) * image_height, 3)

    if x == None or y == None:
        return {"x": 0, "y": 0}
    
    return {"x": x, "y": y}


class FidesUITARSAgent(UITARSAgent):
    """
    Custom UITARS agent for Fides, extending UITARSAgent with additional methods
    specific to the Fides environment.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def find(
                self,
                instruction: str,
                screenshot: str,
                accessibility_tree: Optional[str] = None,
        ) -> str:
        """
        Summarize the content of a screenshot using the LLM.

        Args:
            instruction (str): The instruction for summarizing the screenshot.
            screenshot (str): The base64 encoded screenshot image.
            output_type (str): The expected output type from the LLM.
            accessibility_tree: accepted for interface parity with FidesOpenCUAAgent.find();
                unused here.

        Returns:
            str: The response from the LLM.
        """
        screenshot_bytes = base64.b64decode(screenshot)
        self.history_images.append(screenshot_bytes)
        if len(self.history_images) > self.history_n:
            self.history_images = self.history_images[-self.history_n :]
        if len(self.history_responses) > self.history_n:
            self.history_responses = self.history_responses[-self.history_n :]

        images = []
        if isinstance(self.history_images, bytes):
            self.history_images = [self.history_images]
        elif isinstance(self.history_images, np.ndarray):
            self.history_images = list(self.history_images)
        elif isinstance(self.history_images, list):
            pass
        else:
            raise TypeError(f"Unidentified images type: {type(self.history_images)}")

        for turn, image in enumerate(self.history_images):
            if len(images) >= self.history_n:
                break
            try:
                image = Image.open(BytesIO(image))
            except Exception as e:
                raise RuntimeError(f"Error opening image: {e}")

            if image.width * image.height > self.max_pixels:
                """
                如果图片超过/低于像素限制，则计算一个缩放因子resize_factor，使图片的像素数缩小到等于或小于max_pixels。这个缩放因子是通过开平方根计算的，确保纵横比保持不变,这样原始的相对坐标可以不经转换直接复用
                """
                resize_factor = math.sqrt(self.max_pixels / (image.width * image.height))
                width, height = int(image.width * resize_factor), int(image.height * resize_factor)
                image = image.resize((width, height))
            if image.width * image.height < self.min_pixels:
                resize_factor = math.sqrt(self.min_pixels / (image.width * image.height))
                width, height = math.ceil(image.width * resize_factor), math.ceil(image.height * resize_factor)
                image = image.resize((width, height))

            if image.mode != "RGB":
                image = image.convert("RGB")

            images.append(image)


        messages = []
        messages.append({
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": SYST_PROMPT_FIND_UITARS
                },
            ]
        })
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Description of the element: " + instruction
                },
            ]
        })
        cur_b64 = pil_to_base64(images[-1])
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{cur_b64}",
                        "detail": "high"
                    }
                }
            ]
        })

        try_times = 3

        origin_resized_height = images[-1].height
        origin_resized_width = images[-1].width
        temperature = self.temperature
        while True:
            if try_times <= 0:
                self.logger.error(f"Reach max retry times to fetch response from client, as error flag.")
                return "client error", ["DONE"]
            response = None
            try:
                response = self.vlm.chat.completions.create(
                    model=self.vlm_path,
                    messages=messages,
                    frequency_penalty=1,
                    max_tokens=self.max_tokens,
                    temperature=temperature,
                    top_p=self.top_p
                )
                if self.token_log_path is not None:
                    with open(self.token_log_path, "a") as f:
                        f.write(f"Model UITARS, Function find - Input Tokens: {response.usage.prompt_tokens}, Output Tokens: {response.usage.completion_tokens}, Total Tokens: {response.usage.total_tokens}\n")

                prediction = response.choices[0].message.content.strip()
            except Exception as e:
                self.logger.exception("LLM call failed:", e)
                prediction = None
                try_times -= 1
            try:
                parsed_responses = parse_action_to_structure_output(
                    prediction,
                    self.action_parse_res_factor,
                    origin_resized_height,
                    origin_resized_width,
                    self.model_type,
                    self.max_pixels,
                    self.min_pixels
                )
                break
            except Exception as e:
                self.logger.error(f"Error when parsing response from client, with response: {response}")
                prediction = None
                try_times -= 1
                temperature = 1
                top_k = -1


        try:
            parsed_responses = parse_action_to_structure_output(
                prediction,
                self.action_parse_res_factor,
                origin_resized_height,
                origin_resized_width,
                self.model_type,
                self.max_pixels,
                self.min_pixels
            )
        except Exception as e:
            self.logger.error(f"Parsing action error: {prediction}, with error:\n{e}")
            return f"Parsing action error: {prediction}, with error:\n{e}", ["DONE"]
        

        actions = []
        last_image = Image.open(BytesIO(self.history_images[-1]))
        obs_image_height = last_image.height
        obs_image_width = last_image.width
        for parsed_response in parsed_responses:
            pyautogui_code = parsing_response_to_coordinates(
                parsed_response,
                obs_image_height,
                obs_image_width,
                self.input_swap
            )
            actions.append(pyautogui_code)


        self.history_images.pop()

        
        return parsed_response["thought"], actions[0]
    

    def summarize_screenshot_content(
            self,
            instruction: str,
            screenshot: str,
            output_type: Literal["int", "str", "bool", "float", "list", "dict", "tuple", "set"] = "str"
    ) -> str:
        """
        Summarize the content of a screenshot using the LLM.
        
        Args:
            instruction (str): The instruction for summarizing the screenshot.
            screenshot (str): The base64 encoded screenshot image.
            output_type (str): The expected output type from the LLM.
        
        Returns:
            str: The response from the LLM.
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
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot}",
                        "detail": "high"
                    }
                }
            ]
        })
        try_times = 3
        temperature = self.temperature
        while True:
            if try_times <= 0:
                self.logger.error(f"Reach max retry times to fetch response from client, as error flag.")
                return "client error", ["DONE"]
            response = None
            try:
                response = self.vlm.chat.completions.create(
                    model=self.vlm_path,
                    messages=messages,
                    frequency_penalty=1,
                    max_tokens=self.max_tokens,
                    temperature=1.0,
                    top_p=self.top_p
                )
                if self.token_log_path is not None:
                    with open(self.token_log_path, "a") as f:
                        f.write(f"Model UITARS, Function summarize_screenshot_content - Input Tokens: {response.usage.prompt_tokens}, Output Tokens: {response.usage.completion_tokens}, Total Tokens: {response.usage.total_tokens}\n")

                prediction = response.choices[0].message.content.strip()
                break
            except Exception as e:
                self.logger.exception("LLM call failed:", e)
                prediction = None
                try_times -= 1

        logger.info("RESPONSE: %s", prediction)

        return prediction


    def query_qvlm(
            self,
            instruction: str,
            screenshot: str,
    ) -> str:
        """
        Ask an open-ended question about a screenshot using the LLM.

        Args:
            instruction (str): The question to answer about the screenshot.
            screenshot (str): The base64 encoded screenshot image.

        Returns:
            str: The response from the LLM.
        """
        messages = []
        messages.append({
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": SYST_PROMPT_QVLM_QUERY
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
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot}",
                        "detail": "high"
                    }
                }
            ]
        })
        try_times = 3
        temperature = self.temperature
        while True:
            if try_times <= 0:
                self.logger.error(f"Reach max retry times to fetch response from client, as error flag.")
                return "client error", ["DONE"]
            response = None
            try:
                response = self.vlm.chat.completions.create(
                    model=self.vlm_path,
                    messages=messages,
                    frequency_penalty=1,
                    max_tokens=self.max_tokens,
                    temperature=1.0,
                    top_p=self.top_p
                )
                if self.token_log_path is not None:
                    with open(self.token_log_path, "a") as f:
                        f.write(f"Model UITARS, Function query_qvlm - Input Tokens: {response.usage.prompt_tokens}, Output Tokens: {response.usage.completion_tokens}, Total Tokens: {response.usage.total_tokens}\n")

                prediction = response.choices[0].message.content.strip()
                break
            except Exception as e:
                self.logger.exception("LLM call failed:", e)
                prediction = None
                try_times -= 1

        logger.info("RESPONSE: %s", prediction)

        return prediction


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
                    "image_url": {
                        "url": f"data:image/png;base64,{_screenshot}",
                        "detail": "high"
                    }
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
                    "image_url": {
                        "url": f"data:image/png;base64,{base64_image}",
                        "detail": "high"
                    }
                }
            ]
        })

        try:
            response = self.vlm.chat.completions.create(
                model=self.vlm_path,
                messages=messages,
                frequency_penalty=1,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                top_p=self.top_p
            )
            checkdone = response.choices[0].message.content.strip()
        except Exception as e:
            logger.error("Failed to call" + self.model + ", Error: " + str(e))
            checkdone = ""

        logger.info("RESPONSE: %s", checkdone)


        return checkdone
    

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
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{_screenshot}",
                        "detail": "high"
                    }
                }
            ]
        })

        try:
            response = self.vlm.chat.completions.create(
                model=self.vlm_path,
                messages=messages,
                frequency_penalty=1,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                top_p=self.top_p
            )
            checkdone = response.choices[0].message.content.strip()
            if self.token_log_path is not None:
                with open(self.token_log_path, "a") as f:
                    f.write(f"Model UITARS, Function checkdone - Input Tokens: {response.usage.prompt_tokens}, Output Tokens: {response.usage.completion_tokens}, Total Tokens: {response.usage.total_tokens}\n")
            checkdone = checkdone.replace("{{", "{").replace("}}", "}")
        except Exception as e:
            logger.error("Failed to call" + self.model + ", Error: " + str(e))
            checkdone = ""

        logger.info("RESPONSE: %s", checkdone)


        return checkdone
